#!/usr/bin/env python3
"""exp_margin_ratio.py: margin_ratio 自适应阈值标定（方向 2 落地验证）。

exp_margin_scale 发现：正确匹配 gap ≈ k·√n_dict（0%: 2.71, 30%: ~1.0），
错误匹配 gap/√n_dict ≈ 0.16~0.18。固定 margin=2.0 对长文本偏松
（50% 攻击错误 gap=4.5>2 仍自信地错）。

本实验在 PAWS 真实语料 30 篇上测 margin_ratio ∈ {0, 0.3, 0.5, 0.8}：
  margin_eff = max(margin_abs, margin_ratio · √n_dict_words)
指标：正确匹配 / abstain / 错误匹配（三态，错误匹配应压到 0）。
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
    build_codec, load_paws_positive, paraphrase_style_attack,
)
from exp_real_corpus import synonym_attack


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
    ratios = [None, 0.2, 0.3, 0.4, 0.5]

    # 预生成全部攻击文本
    texts_by_tag = {}
    for tag in tags:
        texts = []
        for i, doc in enumerate(test_docs):
            uid = true_uids[i]
            marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
            texts.append(attack_fn(tag, marked, i))
        texts_by_tag[tag] = texts

    print("=== margin_ratio 自适应阈值（margin_eff = max(2.0, ratio·√n_dict)） ===")
    print(f"{'攻击':5s} | " + " | ".join(
        f"{'None':>4s} " for _ in ratios))
    print("  三态：正确/abstain/错误")
    for tag in tags:
        texts = texts_by_tag[tag]
        cells = []
        for ratio in ratios:
            ok = abst = err = 0
            for i, t in enumerate(texts):
                rep = codec.detect(t, min_n=1)
                margin_eff = 2.0
                if ratio is not None:
                    margin_eff = max(2.0, ratio * (rep.n_dict_words ** 0.5))
                uid_s, s, gap = codec.soft_match(t, candidates, min_n=1,
                                                 margin=margin_eff)
                if uid_s is None:
                    abst += 1
                elif uid_s == true_uids[i]:
                    ok += 1
                else:
                    err += 1
            cells.append(f"{ok:>3d}/{abst:<2d}/{err}")
        print(f"{tag:5s} | " + " | ".join(cells))

    print("\n注：正确/abstain/错误 三态计数（/30）。目标：错误=0 且正确尽量高。")
    print("n_dict 均值:", round(sum(
        codec.detect(t, min_n=1).n_dict_words for t in texts_by_tag["rt"]) / 30))


if __name__ == "__main__":
    main()
