#!/usr/bin/env python3
"""exp_bandwidth.py: 低码率冗余实验（方向 1 修正版）。

ECC 实验结论：在稀疏语料 + 候选池匹配场景，(16,11) SECDED 硬判决
反而比掩码硬判决差——零覆盖带的伪 0 被汉明码当错误去纠。掩码 + soft
已是更优的错误容忍机制（exp_ecc.py）。

本实验转向"低码率"方向：n_bands 从 16 降到 12/8，每带词数翻倍、
z 信号增强，UID 空间相应缩为 12/8-bit（注册库场景够用）。
对比 16/12/8 band 在攻击谱（rt/paws/s30/s50/pku）上的：
  A0 掩码硬判决匹配率 | B1 soft_match 匹配率 | Σ|z| 存在性得分

假设：信号衰减型攻击（s50/pku 删词）下，每带证据翻倍能扛更高攻击强度。
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
    load_paws_positive, paraphrase_style_attack,
)
from exp_real_corpus import filter_dict_by_corpus, synonym_attack


def build_codec_nb(base: GreenlistCodec, docs: list[str], raw_dict: dict,
                   n_bands: int) -> GreenlistCodec:
    groups = filter_dict_by_corpus(raw_dict, docs, base._tokenizer,
                                   max_group=20, zh_mode=True)
    codec = GreenlistCodec(KEY, SALT, n_bands=n_bands, dictionary=groups,
                           language_tag=b"zh")
    codec.calibrate_p0(docs[len(docs) // 2:])
    return codec


def run_attack_matrix(n_bands: int) -> dict:
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

    codec = build_codec_nb(base, docs, raw, n_bands)
    tm_paws = dict(del_p=0.141, grp_sub_p=0.0214)
    tm_pku = dict(del_p=0.505, grp_sub_p=0.0284)

    n_bits = n_bands
    # UID 空间内均匀分布的真 UID，避免低位集中
    span = (1 << n_bits)
    step = max(1, span // (N_DOCS // 2 + 1))
    true_uids = [(0x111 + i * step) % span for i in range(N_DOCS // 2)]
    true_uids = sorted(set(true_uids))
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
    result = {}
    for tag in tags:
        texts = []
        for i, doc in enumerate(test_docs):
            uid = true_uids[i]
            marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
            texts.append(attack_fn(tag, marked, i))
        n = len(test_docs)
        ok0 = okb = 0
        exs = []
        for i, t in enumerate(texts):
            rep = codec.detect(t, min_n=1)
            active = sum((1 << st.band) for st in rep.bands if st.has_signal)
            best0 = min(candidates, key=lambda c: bin((rep.uid ^ c) & active).count("1"))
            bestb = codec.soft_match(t, candidates, min_n=1, margin=0.0)[0]
            exs.append(rep.existence_score)
            if best0 == true_uids[i]:
                ok0 += 1
            if bestb == true_uids[i]:
                okb += 1
        result[tag] = (ok0, okb, sum(exs) / len(exs))
    return result, codec.stats


def main() -> None:
    print("=== 低码率冗余：n_bands 对比（信号衰减型攻击下的证据密度） ===")
    for nb in (16, 12, 8):
        result, stats = run_attack_matrix(nb)
        print(f"\n--- n_bands={nb}  UID 空间={1 << nb}  {stats} ---")
        print(f"{'攻击':5s} | {'A0 掩码硬':>9s} | {'B1 soft':>8s} | {'Σ|z|均值':>8s}")
        for tag in ("rt", "paws", "s30", "s50", "pku"):
            ok0, okb, ex = result[tag]
            print(f"{tag:5s} | {ok0:2d}/30      | {okb:2d}/30    | {ex:>8.1f}")


if __name__ == "__main__":
    main()
