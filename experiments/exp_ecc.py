#!/usr/bin/env python3
"""exp_ecc.py: 冗余编码（(16,11) SECDED 汉明码）实验（方向 1）。

背景：当前 16-bit UID ↔ 16 band 一一对应，零冗余。攻击导致个别带
z 符号翻转时，硬判决直接错（s30 失败多为 1-bit 翻转）。
(16,11) 扩展汉明码：11-bit 数据 → 16-bit 码字（4 校验位 + 1 全局偶校验），
能纠 1 位错、检 2 位错。码字位数 = n_bands = 16，不增加 band、
每带词数不变、信号强度不变，仅 UID 空间 16-bit → 11-bit（注册库场景够用）。

实验内容：
  1. 编解码正确性 + 单错纠回 / 双错检出
  2. 完整攻击流程（PAWS 30 篇）对比：
     A0 无 ECC 硬判决（当前） | A1 ECC 硬判决（纠错后匹配）
"""
from __future__ import annotations

import random
import sys
from typing import Optional, Tuple

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from dict_build import build_cilin_dict
from exp_paws_attack import (
    KEY, SALT, N_SENT, N_DOCS,
    build_codec, load_paws_positive, paraphrase_style_attack,
)
from exp_real_corpus import synonym_attack

# (16,11) SECDED 汉明码（1-indexed 位置）
_DATA_POS = [3, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15]  # 数据位（1-indexed）
_PARITY_POS = [1, 2, 4, 8]  # 校验位（1-indexed）
_GLOBAL_POS = 16  # 全局偶校验位


def hamming_encode(data11: int) -> int:
    assert 0 <= data11 < (1 << 11)
    code = 0
    for i, p in enumerate(_DATA_POS):
        if (data11 >> i) & 1:
            code |= 1 << (p - 1)
    for j in range(4):
        pos = _PARITY_POS[j]
        par = 0
        for k in range(1, 16):  # 不含全局位
            if k != pos and (k & pos) and ((code >> (k - 1)) & 1):
                par ^= 1
        if par:
            code |= 1 << (pos - 1)
    if bin(code).count("1") & 1:  # 全局偶校验
        code |= 1 << (_GLOBAL_POS - 1)
    return code


def hamming_decode(code16: int) -> Tuple[Optional[int], int]:
    """返回 (data11 或 None(双错不可纠), 纠错后码字)。"""
    syn = 0
    for k in range(1, 16):
        if (code16 >> (k - 1)) & 1:
            syn ^= k
    total = bin(code16).count("1") & 1
    corrected = code16
    if syn != 0:
        if total == 1:
            corrected ^= 1 << (syn - 1)  # 单错，翻回
        else:
            return None, code16  # 双错不可纠
    else:
        if total == 1:
            corrected ^= 1 << (_GLOBAL_POS - 1)  # 全局位翻转
    data = 0
    for i, p in enumerate(_DATA_POS):
        if (corrected >> (p - 1)) & 1:
            data |= 1 << i
    return data, corrected


def test_codec() -> None:
    rng = random.Random(1)
    ok = 0
    n_uni = 0
    for _ in range(2000):
        d = rng.randrange(1 << 11)
        c = hamming_encode(d)
        assert hamming_decode(c)[0] == d
        # 单错
        for _e in range(3):
            e = rng.randrange(16)
            c2 = c ^ (1 << e)
            d2, c2c = hamming_decode(c2)
            assert d2 == d, f"单错未纠回: {e}"
        # 双错
        e1, e2 = rng.sample(range(16), 2)
        c3 = c ^ (1 << e1) ^ (1 << e2)
        d3, _ = hamming_decode(c3)
        if d3 is None:
            n_uni += 1
        ok += 1
    print(f"编解码验证: {ok} 组通过, 双错检出 {n_uni}/2000")
    # 码字间最小汉明距
    dists = []
    cc = [hamming_encode(i) for i in range(0, 2048, 137)]
    for i in range(len(cc)):
        for j in range(i + 1, len(cc)):
            dists.append(bin(cc[i] ^ cc[j]).count("1"))
    print(f"码字最小汉明距: {min(dists)} (SECDED 应为 >=3)")


def main() -> None:
    test_codec()

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

    true_uids = [i + 1 for i in range(N_DOCS // 2)]  # 11-bit 空间
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
    print(f"\n{'攻击':5s} | {'A0 无ECC':>9s} | {'A1 ECC硬':>9s} | {'ECC纠错':>8s} | {'双错':>4s} | {'B1 soft':>8s}")
    for tag in tags:
        texts = []
        for i, doc in enumerate(test_docs):
            uid = true_uids[i]
            # ECC 接入：嵌入的是码字 hamming_encode(uid)，候选池也全部编码
            marked = codec.embed(doc, hamming_encode(uid), bias=1.0, rng=random.Random(i))
            texts.append(attack_fn(tag, marked, i))

        cand_code = [hamming_encode(c) for c in candidates]  # 候选码字池
        n = len(test_docs)
        ok0 = 0  # 无 ECC：解码码字与候选码字掩码汉明
        ok1 = 0  # ECC：纠错后 11-bit 与候选比
        okb = 0  # soft_match（码字空间，B1 参照）
        n_corr = 0
        n_double = 0
        for i, t in enumerate(texts):
            rep = codec.detect(t, min_n=1)
            uid16 = rep.uid
            active = sum((1 << st.band) for st in rep.bands if st.has_signal)
            # A0：掩码汉明（候选已是码字）
            best0 = min(cand_code, key=lambda cw: bin((uid16 ^ cw) & active).count("1"))
            # A1：ECC 纠错（全码字）→ 11-bit
            data, c16 = hamming_decode(uid16)
            if data is None:
                n_double += 1
                best1 = None
            else:
                if c16 != uid16:
                    n_corr += 1
                best1 = min(candidates, key=lambda c: bin(data ^ c).count("1"))
            # B1：soft_match 在码字空间
            bestb = codec.soft_match(t, cand_code, min_n=1, margin=0.0)[0]
            if best0 == hamming_encode(true_uids[i]):
                ok0 += 1
            if best1 == true_uids[i]:
                ok1 += 1
            if bestb == hamming_encode(true_uids[i]):
                okb += 1
        print(f"{tag:5s} | {ok0:2d}/{n:<6d} | {ok1:2d}/{n:<6d} | {n_corr:>8d} | {n_double:>4d} | {okb:2d}/{n:<5d}")


if __name__ == "__main__":
    main()
