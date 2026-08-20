#!/usr/bin/env python3
"""exp_pku_del_diagnosis.py: PKU "删除"的物理命运诊断 + del 实现对照实验。

背景：exp_paws_attack.paraphrase_style_attack 把转移矩阵里的
"删除/组外"(del_p) 实现为 **反色同组替换**（green(c) != green(norm)），
注释称"信号全丢"。但 transfer_matrix 对 del 的标定语义是
"原词及其同组词都不出现在改写句 s2"——攻击者放弃了同义表达。
真实物理下 del 的词典词应该消失或换成非词典词（不计入统计），
而非留在同组内反色。两种物理的统计命运不同：

  反色替换: 带内 n 不变、绿率 1.0→0.5，z 均值归零 → UID 随机
  真删除  : 带内 n 减半、绿率保持 ~1.0，z 符号保持 → UID 大多可解

实验内容：
  1. 真实 PKU 改写对中 del 词的物理命运（s2 对应表达的词典命中率）
  2. 同一批 marked 文档在三种 del 实现下的 PKU 存活率对照：
       flip        —— 现状：反色同组替换（文档历史口径）
       remove      —— 真删除：token 消失（不计入统计）
       rand_dict   —— 换成词典内随机组词（颜色随机，最悲观污染）
  3. 结论判断：若 remove/rand_dict 显著优于 flip，则 PKU 0/30 边界
     部分来自模拟过严；真实重度改写下存活率需重新标定。
"""
from __future__ import annotations

import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from dict_build import build_cilin_dict
from exp_paws_attack import (
    KEY, SALT, N_SENT, N_DOCS,
    build_codec, load_paws_positive, load_pku_pairs,
)

PKU_LIMIT = 20000


def classify_del_fate(codec, pairs, limit=20000):
    """真实 PKU 改写对：del 词的 s2 表达在词典内的命中率。

    对 sentence1 的词典词 d1，按转移语义分类：
      keep   —— 字面出现在 s2
      grp    —— 同组词出现在 s2（组内同义换）
      del    —— 原词与同组词都不在 s2
    对 del 的词：检查 s2 中"新出现的词典词"（含与 d1 不同组的词）——
    用整个 s2 的词典词集合减去出现在 s1 的词典词集合，即"替换表达
    若用了词典词，会以 new 身份出现"。统计 del 词数与 new 词数之比。
    """
    w2g = codec._w2group
    all_words = set(codec._all_words)
    cnt = {"n1": 0, "keep": 0, "grp": 0, "del": 0, "new_in_s2": 0,
           "pairs": 0, "n2": 0}
    for s1, s2 in pairs[:limit]:
        t1 = [n for _, n in codec._tokenizer(s1) if n]
        t2 = [n for _, n in codec._tokenizer(s2) if n]
        d1 = [w for w in t1 if w in w2g]
        d2 = [w for w in t2 if w in w2g]
        if not d1:
            continue
        cnt["pairs"] += 1
        cnt["n1"] += len(d1)
        cnt["n2"] += len(d2)
        s2_words = set(t2)
        d1_set = set(d1)
        for w in d1:
            if w in s2_words:
                cnt["keep"] += 1
            elif any(x in s2_words for x in w2g[w] if x != w):
                cnt["grp"] += 1
            else:
                cnt["del"] += 1
        # del 词的替代表达若落在词典内，必然以"非 d1 的词典词"身份出现在 s2
        cnt["new_in_s2"] += sum(1 for w in d2 if w not in d1_set)
    tot = cnt["n1"]
    return {
        "pairs": cnt["pairs"], "n1": tot, "n2": cnt["n2"],
        "keep": f"{cnt['keep']/tot*100:.1f}%" if tot else "0",
        "grp_sub": f"{cnt['grp']/tot*100:.2f}%",
        "del": f"{cnt['del']/tot*100:.1f}%",
        "new_in_s2": f"{cnt['new_in_s2']/tot*100:.1f}%",
        "del_to_dict_ratio": round(cnt["new_in_s2"] / max(cnt["del"], 1), 3),
    }


def attack_del(codec, text, seed, p_del, p_grp, mode):
    """参数化重度改写；mode ∈ {flip, remove, rand_dict}。

    - flip    ：del → 反色同组替换（历史口径，信号翻转）
    - remove  ：del → token 消失（真实"删除"物理，不计入统计）
    - rand_dict：del → 词典内随机组随机词（颜色随机污染，最悲观）
    """
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
                    continue  # token 消失
                else:  # rand_dict
                    head = r.choice(list(codec._groups))
                    out.append(r.choice(codec._groups[head]))
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
    raw = build_cilin_dict("corpus/dict/cilin_extended.txt")

    def n_dict(s: str) -> int:
        return sum(1 for _, n in base._tokenizer(s) if n and n in base._w2group)

    kept = [p for p in paws if n_dict(p[0]) >= 2]
    rng = random.Random(7)
    rng.shuffle(kept)
    docs = [" ".join(kept[i * N_SENT:(i + 1) * N_SENT][j][0]
                     for j in range(N_SENT)) + " "
            for i in range(N_DOCS)]
    test_docs = docs[:N_DOCS // 2]

    codec = build_codec(base, docs, raw)
    pku = load_pku_pairs(PKU_LIMIT)

    print(f"词典: {len(codec._groups)} 组 / {codec.stats['n_words']} 词")

    # ---- 1. del 词物理命运 ----
    fate = classify_del_fate(codec, pku, PKU_LIMIT)
    print("\n[真实 PKU 改写对 del 词物理命运] "
          f"({fate['pairs']} 对 / {fate['n1']} 词典词, s2 词典词 {fate['n2']}):")
    print(f"  keep={fate['keep']}  组内换={fate['grp_sub']}  del={fate['del']}")
    print(f"  s2 新增词典词(new_in_s2)={fate['new_in_s2']}  "
          f"del 词中实际落到词典的比例≈{fate['del_to_dict_ratio']}")

    # ---- 2. 三种 del 实现存活率对照 ----
    tm_pku = dict(del_p=0.505, grp_sub_p=0.0284)
    true_uids = [0x1000 + i * 0x0111 for i in range(len(test_docs))]
    cands = sorted(set(range(0x1000, 0x2000, 0x111)) | set(true_uids))

    print("\n[PKU 重度改写 del 实现对照]（30 篇，soft n≥1 + 掩码汉明）")
    print(f"{'del实现':11s} | {'soft匹配':>8s} | {'汉明≤1':>6s} | {'汉明≤2':>6s} | {'汉明均值':>7s} | {'Σ|z|均值':>8s}")
    for mode in ("flip", "remove", "rand_dict"):
        soft_ok = ham_le1 = ham_le2 = 0
        ham_sum = zsum = 0.0
        for i, doc in enumerate(test_docs):
            uid = true_uids[i]
            marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
            attacked = attack_del(codec, marked, 300 + i, tm_pku["del_p"],
                                  tm_pku["grp_sub_p"], mode)
            best, _, _ = codec.soft_match(attacked, cands, min_n=1, margin=0.0)
            if best == uid:
                soft_ok += 1
            d, _ = codec.masked_hamming(attacked, uid)
            if d <= 1:
                ham_le1 += 1
            if d <= 2:
                ham_le2 += 1
            ham_sum += d
            zsum += codec.detect(attacked).existence_score
        n = len(test_docs)
        print(f"{mode:11s} | {soft_ok:2d}/{n:<5d} | {ham_le1:2d}/{n:<4d} | "
              f"{ham_le2:2d}/{n:<4d} | {ham_sum/n:6.2f}  | {zsum/n:7.1f}")

    # ---- 3. 真实 PKU 对与"remove 物理"的信号代数 ----
    print("\n[信号代数参考]")
    print("  保持率≈0.466 → 带内 n 减半但绿率保持 → z 符号正确率随 n_keep 增高；")
    print("  若翻转率 f 由 grp_sub(2.84%×0.5)+rand_dict 污染贡献，f 远小于 0.5。")

    # 保存参考值供文档
    with open("experiments/pku_del_diagnosis_result.txt", "w", encoding="utf-8") as f:
        f.write(f"fate={fate}\n")
        f.write(f"tm_pku={tm_pku}\n")
        f.write("flip/remove/rand_dict 三行结果见 stdout\n")


if __name__ == "__main__":
    main()
