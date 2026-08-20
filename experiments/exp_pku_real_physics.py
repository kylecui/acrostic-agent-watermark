#!/usr/bin/env python3
"""exp_pku_real_physics.py: 生产词典(D3r)口径下 PKU"删除"物理的存活率对照。

结论前置（exp_pku_del_diagnosis 在小词典上的发现）：
paraphrase_style_attack 把 del_p=0.505 实现为**反色同组替换**，其统计
命运（带内 n 不变、绿率 1.0→0.5，z 均值归零）与真实删除（token 消失、
绿率保持，z 符号保持）截然不同。本实验在生产词典（生产∪词林=，v0.9
默认）上复跑对照，并加入混合物理：

  flip      —— del → 反色同组替换（文档历史口径）
  remove    —— del → token 消失（真实删除物理）
  mix(α)    —— del 词以概率 α 变成词典内随机词（颜色随机污染），
               (1-α) 真删除。真实 PKU 改写对实测 α≈0.36~0.72。
  rand_dict —— del → 全部变词典内随机词（α=1.0 上限）

指标：soft n≥1 匹配率 + 掩码汉明≤1 + 汉明均值 + Σ|z|。
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

N_TEST = N_DOCS // 2
TM_PKU = dict(del_p=0.505, grp_sub_p=0.0284)


def attack_del(codec, text, seed, p_del, p_grp, mode, alpha=0.5):
    """重度改写攻击。mode ∈ {flip, remove, mix, rand_dict}。"""
    r = random.Random(seed)
    out = []
    for raw, norm in codec._tokenizer(text):
        grp = codec._w2group.get(norm) if norm else None
        if grp:
            x = r.random()
            if x < p_del:
                if mode == "flip":
                    alts = [c for c in grp if c != norm and codec.green(c) != codec.green(norm)]
                    out.append(r.choice(alts) if alts else raw)
                elif mode == "remove":
                    continue
                elif mode == "rand_dict":
                    head = r.choice(list(codec._groups))
                    out.append(r.choice(codec._groups[head]))
                else:  # mix: α 变词典随机词，否则消失
                    if r.random() < alpha:
                        head = r.choice(list(codec._groups))
                        out.append(r.choice(codec._groups[head]))
                    else:
                        continue
            elif x < p_del + p_grp:
                alts = [c for c in grp if c != norm]
                out.append(r.choice(alts) if alts else raw)
            else:
                out.append(raw)
        else:
            out.append(raw)
    return "".join(out)


def main() -> None:
    paws = load_paws_positive()
    base = GreenlistCodec(KEY, SALT, language_tag=b"zh")

    def n_dict(s):
        return sum(1 for _, n in base._tokenizer(s) if n and n in base._w2group)

    kept = [p for p in paws if n_dict(p[0]) >= 2]
    rng = random.Random(7)
    rng.shuffle(kept)
    docs = [" ".join(kept[i * N_SENT:(i + 1) * N_SENT][j][0] for j in range(N_SENT)) + " "
            for i in range(N_DOCS)]
    test_docs = docs[:N_TEST]
    null_docs = docs[N_TEST:]

    # D3r 生产词典：生产策划组优先 ∪ 词林'='只补新词（与 gen_prod_dicts 同规则）
    raw_equal = build_cilin_dict("corpus/dict/cilin_extended.txt")
    merged = dict(ZH_SYNONYMS_RAW)
    used = {w for ws in ZH_SYNONYMS_RAW.values() for w in ws}
    for k, ws in raw_equal.items():
        ws2 = [w for w in ws if w not in used]
        if len(ws2) >= 2:
            merged[k] = ws2
            used.update(ws2)

    codec = GreenlistCodec(KEY, SALT, dictionary=merged, language_tag=b"zh")
    codec.calibrate_p0(null_docs)
    nw = len({w for ws in codec._groups.values() for w in ws})
    nd = [sum(1 for _, n in codec._tokenizer(d) if n and n in codec._w2group)
          for d in test_docs]
    print(f"[D3r 生产∪词林=] 组={len(codec._groups)} 词={nw} "
          f"n_dict/篇={sum(nd)/len(nd):.1f} min={min(nd)}")

    true_uids = [(0x1000 + i * 0x0111) & 0xFFFF for i in range(N_TEST)]
    candidates = sorted(set(range(1, 33)) | set(true_uids))

    configs = [
        ("flip(历史口径)", dict(mode="flip")),
        ("remove(真删除)", dict(mode="remove")),
        ("mix α=0.36(下界)", dict(mode="mix", alpha=0.36)),
        ("mix α=0.50(代表)", dict(mode="mix", alpha=0.50)),
        ("mix α=0.72(上界)", dict(mode="mix", alpha=0.72)),
        ("rand_dict(α=1.0)", dict(mode="rand_dict")),
    ]
    print(f"\n[PKU 重度改写 del 物理对照]（{N_TEST} 篇，soft n≥1）")
    print(f"{'配置':18s} | {'soft匹配':>8s} | {'汉明≤1':>6s} | {'汉明≤2':>6s} | {'汉明均值':>7s} | {'Σ|z|均值':>8s}")

    results = {}
    for name, kw in configs:
        soft_ok = ham_le1 = ham_le2 = 0
        ham_sum = zsum = 0.0
        for i, doc in enumerate(test_docs):
            uid = true_uids[i]
            marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
            attacked = attack_del(codec, marked, 300 + i, TM_PKU["del_p"],
                                  TM_PKU["grp_sub_p"], **kw)
            best, _, _ = codec.soft_match(attacked, candidates, min_n=1, margin=0.0)
            if best == uid:
                soft_ok += 1
            d, _ = codec.masked_hamming(attacked, uid)
            if d <= 1:
                ham_le1 += 1
            if d <= 2:
                ham_le2 += 1
            ham_sum += d
            zsum += codec.detect(attacked).existence_score
        n = N_TEST
        print(f"{name:18s} | {soft_ok:2d}/{n:<5d} | {ham_le1:2d}/{n:<4d} | "
              f"{ham_le2:2d}/{n:<4d} | {ham_sum/n:6.2f}  | {zsum/n:7.1f}")
        results[name] = dict(soft=soft_ok, le1=ham_le1, le2=ham_le2,
                             ham=round(ham_sum / n, 2), sumz=round(zsum / n, 1))

    # 存在性分离度（remove 物理下 null/marked 是否仍可区分）
    marked5 = [codec.embed(d, true_uids[i], bias=1.0, rng=random.Random(i))
               for i, d in enumerate(test_docs[:5])]
    nulls = [codec.detect(d).existence_score for d in null_docs[:8]]
    marks = [codec.detect(d).existence_score for d in marked5]
    print(f"\n存在性分离（移除口径无关）：null 均值={sum(nulls)/len(nulls):.1f} "
          f"marked 均值={sum(marks)/len(marks):.1f} 最小间隔={min(marks)-max(nulls):+.1f}")

    with open("experiments/pku_real_physics_result.txt", "w", encoding="utf-8") as f:
        f.write(f"n_dict_per_doc={sum(nd)/len(nd):.1f}\n")
        for k, v in results.items():
            f.write(f"{k}: {v}\n")


if __name__ == "__main__":
    main()
