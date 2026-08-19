#!/usr/bin/env python3
"""exp_soft_match.py: 软判决注册库匹配（v0.7 鲁棒性增强）。

背景：硬判决（masked_hamming）只比较解码 UID 与候选的汉明距，
逐带 z 的幅度信息被丢弃；且零覆盖带被掩码后候选无法区分
（exp_real_corpus 实测 4/30 失败源于此）。

本实验验证两个增强假设并标定正式 API（GreenlistCodec.soft_match）：
  1. n>=1 弱证据带参与检测 —— 单词带 z 符号噪声 79%~100% 正确，
     净贡献为正（min_n=1 比 2 显著提升）
  2. soft-dot 匹配 —— 逐带 z 对候选打点积分 argmax，
     利用幅度信息而非只比符号

结果矩阵（30 篇拼接文档，每篇 20 句 ≈ 900 字，词典词均值 51）：
  攻击   | A0 硬n>=2 | A1 硬n>=1 | B0 soft_n2 | B1 soft_n1
  rt     |   26/30   |   29/30   |   26/30    |   29/30
  paws   |   22/30   |   23/30   |   22/30    |   25/30
  s30    |   20/30   |   23/30   |   24/30    |   27/30
  s50    |    7/30   |    6/30   |    6/30    |    6/30
  pku    |    1/30   |    1/30   |    1/30    |    1/30

结论：
  - B1（soft_dot + min_n=1）最优：rt 29/30、paws 25/30、s30 27/30
  - s50/pku ≈ null（汉明 5.97），超过词典级水印物理边界
  - margin 阈值（实测 margin=2.0）把错误匹配全部转 abstain，
    precision→100%，代价是部分召回转"低置信"
  - soft_match 是"候选区分器"：null 文本 z 随机游走也可能与某候选
    方向对齐（实测 40/40 误匹配），存在性判定必须由 Σ|z| 门控
    （null 40.2 vs marked 78.2，无重叠）
"""
from __future__ import annotations

import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from dict_build import build_cilin_dict
from exp_real_corpus import synonym_attack
from exp_paws_attack import (
    KEY, SALT, N_SENT, N_DOCS,
    build_codec, load_paws_positive, load_pku_pairs, paraphrase_style_attack,
)


def detect_v(codec: GreenlistCodec, text: str, min_n: int):
    """复刻 detect，min_n 控制参与检测的最小带内词数。"""
    n_per = [0] * codec.n_bands
    g_per = [0] * codec.n_bands
    for _raw, norm in codec._tokenizer(text):
        if norm is None:
            continue
        b = codec._w2band.get(norm)
        if b is not None:
            n_per[b] += 1
            g_per[b] += codec.green(norm)
    zs = [0.0] * codec.n_bands
    uid = 0
    active = 0
    for b in range(codec.n_bands):
        n, g = n_per[b], g_per[b]
        if n < min_n:
            continue
        p0 = codec._p0_of(b)
        var = p0 * (1.0 - p0) * n
        z = (g - p0 * n) / (var ** 0.5) if var > 0 else 0.0
        zs[b] = z
        active |= 1 << b
        if z > 0:
            uid |= 1 << b
    return zs, uid, active


def hard_match(codec: GreenlistCodec, text: str, candidates, min_n: int):
    """硬判决：解码 UID 与候选比带掩码汉明距。"""
    zs, uid, active = detect_v(codec, text, min_n)
    best, best_d = None, None
    for c in candidates:
        diff = (uid ^ c) & active
        d = bin(diff).count("1")
        if best_d is None or d < best_d:
            best, best_d = c, d
    return best, best_d, bin(active).count("1")


def soft_match(codec: GreenlistCodec, text: str, candidates, min_n: int):
    """软判决：逐带 z 对候选打点积分 argmax。"""
    zs, uid, active = detect_v(codec, text, min_n)
    best, best_s = None, None
    for c in candidates:
        s = 0.0
        for b in range(codec.n_bands):
            if (active >> b) & 1:
                s += zs[b] * (1 if ((c >> b) & 1) else -1)
        if best_s is None or s > best_s:
            best, best_s = c, s
    return best, best_s


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
    print(f"词典: {codec.stats}")
    tm_paws = dict(del_p=0.141, grp_sub_p=0.0214)
    tm_pku = dict(del_p=0.505, grp_sub_p=0.0284)

    true_uids = [((0x1000 + i * 0x0111) & 0xFFFF) for i in range(N_DOCS // 2)]
    candidates = sorted(set(range(1, 33)) | set(true_uids))

    def attack_fn(tag: str, marked: str, i: int) -> str:
        if tag == "rt":
            return marked
        if tag == "paws":
            return paraphrase_style_attack(codec, marked, 200 + i,
                                           tm_paws["del_p"], tm_paws["grp_sub_p"])
        if tag == "pku":
            return paraphrase_style_attack(codec, marked, 300 + i,
                                           tm_pku["del_p"], tm_pku["grp_sub_p"])
        if tag == "s30":
            rw, _ = synonym_attack(codec, marked, 0.30, 100 + i)
            return rw
        if tag == "s50":
            rw, _ = synonym_attack(codec, marked, 0.50, 100 + i)
            return rw

    tags = ["rt", "paws", "s30", "s50", "pku"]
    print(f"{'攻击':5s} | A0 硬n>=2 | A1 硬n>=1 | B0 soft_n2 | B1 soft_n1")
    for tag in tags:
        texts = []
        for i, doc in enumerate(test_docs):
            uid = true_uids[i]
            marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
            texts.append(attack_fn(tag, marked, i))

        n = len(test_docs)
        res = {}
        for key, fn in (
            ("A0", lambda t, c, i: hard_match(c, t, candidates, 2)[0]),
            ("A1", lambda t, c, i: hard_match(c, t, candidates, 1)[0]),
            ("B0", lambda t, c, i: soft_match(c, t, candidates, 2)[0]),
            ("B1", lambda t, c, i: soft_match(c, t, candidates, 1)[0]),
        ):
            ok = sum(1 for i, t in enumerate(texts) if fn(t, codec, i) == true_uids[i])
            res[key] = ok
        print(f"{tag:5s} | {res['A0']:2d}/{n}        | {res['A1']:2d}/{n}        | "
              f"{res['B0']:2d}/{n}        | {res['B1']:2d}/{n}")


if __name__ == "__main__":
    main()
