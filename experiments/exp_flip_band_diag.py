#!/usr/bin/env python3
"""exp_flip_band_diag.py: 逐带解剖 s50 翻转带 —— 翻转带 token 的组颜色构成。

回答：加权为什么无效？翻转带的 token 是否大多落在"颜色对半组"（q≈0.5，
加权应压它）？还是落在倾斜组（q 高仍翻转，说明翻转是样本随机性而非
组构成）？这决定"组构成加权"是否对症。

用法: python experiments/exp_flip_band_diag.py
"""
from __future__ import annotations

import random
import sys
from collections import Counter

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from aawm.synonym_data import ZH_SYNONYMS_RAW
from dict_build import build_cilin_dict
from exp_paws_attack import KEY, SALT, N_SENT, N_DOCS, load_paws_positive
from exp_real_corpus import synonym_attack
from exp_weighted_detect import build_d3r, build_weight_tables

N_TEST = N_DOCS // 2


def group_color_profile(codec):
    """组 -> (q_green, size)"""
    prof = {}
    for head, members in codec._groups.items():
        ng = sum(1 for w in members if codec.green(w))
        prof[head] = (ng / len(members), len(members))
    return prof


def main():
    codec, test_docs, null_docs = build_d3r()
    true_uids = [(0x1000 + i * 0x0111) & 0xFFFF for i in range(N_TEST)]
    candidates = sorted(set(range(1, 33)) | set(true_uids))
    w1, w2, w3 = build_weight_tables(codec)
    prof = group_color_profile(codec)

    # s50 失守文档（v0 等权 soft_match 口径）
    lost = [0, 1, 9, 14, 15, 16, 17, 19, 21, 24, 29]

    def soft_uid(z_by_band):
        scored = sorted(
            ((sum(z * (1 if ((c >> b) & 1) else -1) for b, z in z_by_band.items()), c)
             for c in candidates), key=lambda x: x[0], reverse=True)
        return scored[0][1]

    agg_flip_tok = Counter()
    agg_keep_tok = Counter()
    print(f"{'篇':>2s} | {'翻转带':>12s} | 翻转带内 token 的 q 分布(绿/红)")
    for i in lost:
        uid = true_uids[i]
        marked = codec.embed(test_docs[i], uid, bias=1.0, rng=random.Random(i))
        rw, _ = synonym_attack(codec, marked, 0.50, 100 + i)

        # 记录攻击前各 token 的颜色（marked 中绿词 = bit=1 证据）
        pre = {}
        for raw, norm in codec._tokenizer(marked):
            b = codec._w2band.get(norm)
            if b is not None:
                pre[b] = pre.get(b, []) + [norm]

        rep = codec.detect(rw, min_n=1)
        z0 = {st.band: st.z for st in rep.bands if st.has_signal}
        flipped = [b for b, z in z0.items() if (((uid >> b) & 1) == 1) != (z > 0)]

        # 每个翻转带的 token 颜色 + 组构成
        tok_stats = []
        for b in flipped:
            post = {}
            for raw, norm in codec._tokenizer(rw):
                bb = codec._w2band.get(norm)
                if bb == b:
                    post[norm] = post.get(norm, 0) + 1
            for w, cnt in post.items():
                grp = codec._w2group[w]
                head = next(h for h, m in codec._groups.items() if m == grp)
                qg, size = prof[head]
                c = codec.green(w)
                tok_stats.append((qg if c else 1 - qg, size, cnt, c))

        # 汇总：翻转带 token 的"同色比例"分布
        qvals = [q for q, *_ in tok_stats for _ in range(tok_stats[0][2] if False else 1)]
        s = ", ".join(f"{q:.2f}{'G' if c else 'R'}({sz})" for q, sz, _, c in tok_stats)
        print(f"{i:2d} | {str(flipped):>12s} | {s}")
        for q, sz, _, c in tok_stats:
            key = ("绿" if c else "红", "tilt" if q >= 0.6 else ("half" if q >= 0.4 else "rev"))
            agg_flip_tok[key] += 1

    # 对照：存活文档的带内 token 构成（非翻转带）
    print("\n===== 存活文档对照（s50 后正确匹配的带）=====")
    for i, doc in enumerate(test_docs):
        if i in lost:
            continue
        uid = true_uids[i]
        marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
        rw, _ = synonym_attack(codec, marked, 0.50, 100 + i)
        rep = codec.detect(rw, min_n=1)
        z0 = {st.band: st.z for st in rep.bands if st.has_signal}
        good = [b for b, z in z0.items() if (((uid >> b) & 1) == 1) == (z > 0)]
        for b in good[:3]:
            for raw, norm in codec._tokenizer(rw):
                if codec._w2band.get(norm) != b:
                    continue
                grp = codec._w2group[norm]
                head = next(h for h, m in codec._groups.items() if m == grp)
                qg, size = prof[head]
                c = codec.green(norm)
                q = qg if c else 1 - qg
                key = ("绿" if c else "红", "tilt" if q >= 0.6 else ("half" if q >= 0.4 else "rev"))
                agg_keep_tok[key] += 1
        if i > 18:
            break

    print(f"\n翻转带 token 构成: {dict(agg_flip_tok)}")
    print(f"存活带 token 构成: {dict(agg_keep_tok)}")

    # 每带 token 数对翻转的影响
    print("\n===== 翻转带 vs 存活带的带内 n 分布 =====")
    fn, kn = [], []
    for i, doc in enumerate(test_docs):
        uid = true_uids[i]
        marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
        rw, _ = synonym_attack(codec, marked, 0.50, 100 + i)
        rep = codec.detect(rw, min_n=1)
        z0 = {st.band: st.z for st in rep.bands if st.has_signal}
        pre = Counter(codec._w2band[n] for _, n in codec._tokenizer(marked) if n in codec._w2band)
        for b, z in z0.items():
            flip = (((uid >> b) & 1) == 1) != (z > 0)
            (fn if flip else kn).append(pre[b])
    import statistics
    print(f"翻转带 n 分布: 均值={statistics.mean(fn):.1f} 中位={statistics.median(fn):.0f} "
          f"n<=2 占比={sum(1 for x in fn if x<=2)/len(fn)*100:.0f}%")
    print(f"存活带 n 分布: 均值={statistics.mean(kn):.1f} 中位={statistics.median(kn):.0f} "
          f"n<=2 占比={sum(1 for x in kn if x<=2)/len(kn)*100:.0f}%")


if __name__ == "__main__":
    main()
