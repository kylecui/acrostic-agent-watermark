#!/usr/bin/env python3
"""exp_bandwidth_d3r.py: 扩容后重验编码结构 —— n_bands∈{8,12,16} 的鲁棒性 trade-off。

§13.11 在小词典（n_dict≈51/篇，带内 n≈3）证伪"低码率 n_bands∈{8,12}"：
带数减少使 Σ|z| 下降、单带错误占比上升。但 D3r 扩容后 n_dict≈90/篇、
带内 n≈6，二项式波动的统计极限被推高——翻转带期望数可能随带数减少
而大幅下降。本实验重验该 trade-off。

同时对比 8/12/16 band 下：
  1. s30/s50/s70 同义替换攻击的 soft 匹配率（UID 位数相应为 8/12/16）
  2. null vs marked 存在性分离度（Σ|z| 间隔）

用法: python experiments/exp_bandwidth_d3r.py
"""
from __future__ import annotations

import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from aawm.synonym_data import ZH_SYNONYMS_RAW
from dict_build import build_cilin_dict
from exp_paws_attack import KEY, SALT, N_SENT, N_DOCS, load_paws_positive
from exp_real_corpus import synonym_attack
from exp_weighted_detect import soft_match_w

N_TEST = N_DOCS // 2


def build_d3r_codec(n_bands):
    """D3r 词典 + 指定 band 数（与 exp_dict_expansion.zh_section 同口径）。"""
    paws = load_paws_positive()
    base = GreenlistCodec(KEY, SALT, language_tag=b"zh")

    def n_dict(s):
        return sum(1 for _, n in base._tokenizer(s) if n and n in base._w2group)

    kept = [p for p in paws if n_dict(p[0]) >= 2]
    rng = random.Random(7)
    rng.shuffle(kept)
    docs = [" ".join(kept[i * N_SENT:(i + 1) * N_SENT][j][0] for j in range(N_SENT)) + " "
            for i in range(N_DOCS)]
    test_docs, null_docs = docs[:N_TEST], docs[N_TEST:]

    raw_equal = build_cilin_dict("corpus/dict/cilin_extended.txt")
    merged = dict(ZH_SYNONYMS_RAW)
    used = {w for ws in ZH_SYNONYMS_RAW.values() for w in ws}
    for k, ws in raw_equal.items():
        ws2 = [w for w in ws if w not in used]
        if len(ws2) >= 2:
            merged[k] = ws2
            used.update(ws2)
    codec = GreenlistCodec(KEY, SALT, n_bands=n_bands,
                           dictionary=merged, language_tag=b"zh")
    codec.calibrate_p0(null_docs)
    return codec, test_docs, null_docs


def run(n_bands, test_docs, null_docs):
    codec, *_ = build_d3r_codec(n_bands)
    max_uid = 1 << n_bands
    true_uids = [(0x111 * (i + 1)) % max_uid for i in range(N_TEST)]
    candidates = sorted(set(range(1, min(33, max_uid))) | set(true_uids))

    n_dict_mean = 0
    for frac in (0.30, 0.50, 0.70):
        ok = 0
        ham = []
        for i, doc in enumerate(test_docs):
            uid = true_uids[i]
            marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
            if frac == 0.30:
                n_dict_mean += codec.detect(marked).n_dict_words
            rw, _ = synonym_attack(codec, marked, frac, 100 + i)
            rep = codec.detect(rw, min_n=1)
            z = {st.band: st.z for st in rep.bands if st.has_signal}
            best, *_ = soft_match_w(z, candidates)
            if best == uid:
                ok += 1
            ham.append(bin(best ^ uid).count("1"))
        tag = f"s{int(frac*100)}"
        print(f"    {tag}: soft {ok:2d}/30  汉明均值 {sum(ham)/len(ham):.2f}")

    nulls = [codec.detect(d).existence_score for d in null_docs[:10]]
    marks = [codec.detect(codec.embed(d, true_uids[i], bias=1.0,
                                      rng=random.Random(i))).existence_score
             for i, d in enumerate(test_docs[:10])]
    gap = min(marks) - max(nulls)
    print(f"    存在性: null均值={sum(nulls)/len(nulls):.1f} "
          f"marked均值={sum(marks)/len(marks):.1f} 最小间隔={gap:+.1f}")
    return n_dict_mean / N_TEST


if __name__ == "__main__":
    codec, test_docs, null_docs = build_d3r_codec(16)
    print("D3r 词典", codec.stats)
    print(f"n_dict/篇 均值="
          f"{sum(codec.detect(d).n_dict_words for d in test_docs)/len(test_docs):.1f}")
    for nb in (16, 12, 8):
        print(f"\n===== n_bands={nb} =====")
        run(nb, test_docs, null_docs)
