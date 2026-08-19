#!/usr/bin/env python3
"""exp_gap_scale2.py: 归一化因子对比——gap / Σ√n_b vs gap / √n_dict。

gap 尺度理论：真候选 vs 次优候选（汉明距 d）的得分差
gap ≈ Σ_{diff 带} |E[z_b]| ≈ δ·Σ_{diff} √n_b。因此正确的归一化
是 Σ_b √n_b（active 带），而非 √n_dict（每带不均时失真）。

验证：在 大n_dict（合成600词）和 小n_dict（PAWS 30篇）两个语料上，
对比 gap/√n_dict 与 gap/Σ√n_b 对"正确匹配"的下界和"错误匹配"的上界，
找可迁移阈值（使 正确通过 & 错误拒绝）。
"""
from __future__ import annotations

import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from dict_build import build_cilin_dict
from exp_capability_demo import make_en_text, en_paraphrase, KEY, SALT
from exp_paws_attack import (
    KEY as KEY2, SALT as SALT2, N_SENT, N_DOCS,
    build_codec, load_paws_positive, paraphrase_style_attack,
)
from exp_real_corpus import synonym_attack


def scale_features(codec: GreenlistCodec, text: str):
    """返回 (n_dict, sqrt_sum, gap) 用于归一化对比。"""
    rep = codec.detect(text, min_n=1)
    sqrt_sum = sum(st.n ** 0.5 for st in rep.bands if st.has_signal)
    return rep.n_dict_words, sqrt_sum, rep


def run_corpus(docs, codec, attack_fn, true_uids, candidates, label):
    print(f"\n=== {label} ===")
    rows = []  # (gap, gap/√nd, gap/Σ√n, correct)
    for i, doc in enumerate(docs):
        uid = true_uids[i]
        marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
        t = attack_fn(marked, i)
        nd, ss, rep = scale_features(codec, t)
        uid_s, score, gap = codec.soft_match(t, candidates, min_n=1, margin=0.0)
        correct = uid_s == uid
        rows.append((gap, gap / (nd ** 0.5), gap / ss, correct))
    ok = [r for r in rows if r[3]]
    bad = [r for r in rows if not r[3]]
    def stat(name, f):
        okv = [f(r) for r in ok] or [0]
        badv = [f(r) for r in bad] or [0]
        print(f"  {name:>10s}: 正确 min={min(okv):.3f} 均值={sum(okv)/len(okv):.3f} | "
              f"错误 max={max(badv):.3f} 均值={sum(badv)/len(badv):.3f} | "
              f"正确率={len(ok)}/{len(rows)}")
    stat("gap", lambda r: r[0])
    stat("gap/√n_dict", lambda r: r[1])
    stat("gap/Σ√n_b", lambda r: r[2])
    return rows


def main() -> None:
    # --- 语料 A：英文合成大 n_dict ---
    random.seed(0)
    en = GreenlistCodec(KEY, SALT, language_tag=b"en")
    null_corpus = [make_en_text(600, s, en) for s in range(100, 108)]
    en.calibrate_p0(null_corpus)
    cands_en = [0x1000, 0x1234, 0x2000, 0xABCD, 0x00FF, 0xF0F0]
    docs_en = [make_en_text(600, s, en) for s in range(600, 624)]
    uids_en = [cands_en[i % len(cands_en)] for i in range(len(docs_en))]

    def atk_en(marked, i):
        if i % 2 == 0:
            rw, _ = en_paraphrase(en, marked, 0.30, 1000 + i)
        else:
            rw, _ = en_paraphrase(en, marked, 0.50, 1000 + i)
        return rw

    run_corpus(docs_en, en, atk_en, uids_en, cands_en,
               f"英文合成 n_dict≈{en.detect(docs_en[0]).n_dict_words} (30%/50% 攻击)")

    # --- 语料 B：PAWS 真实小 n_dict ---
    paws = load_paws_positive()
    base = GreenlistCodec(KEY2, SALT2, language_tag=b"zh")
    raw = build_cilin_dict("corpus/dict/cilin_extended.txt")

    def nd(s: str) -> int:
        return sum(1 for _, n in base._tokenizer(s) if n and n in base._w2group)

    kept = [p for p in paws if nd(p[0]) >= 2]
    rng = random.Random(7)
    rng.shuffle(kept)
    docs = [" ".join(kept[i * N_SENT:(i + 1) * N_SENT][j][0]
                     for j in range(N_SENT)) + " "
            for i in range(N_DOCS)]
    test_docs = docs[:N_DOCS // 2]
    codec = build_codec(base, docs, raw)
    true_uids = [((0x1000 + i * 0x0111) & 0xFFFF) for i in range(N_DOCS // 2)]
    candidates = sorted(set(range(1, 33)) | set(true_uids))
    tm_paws = dict(del_p=0.141, grp_sub_p=0.0214)

    def atk_paws(marked, i):
        return paraphrase_style_attack(codec, marked, 200 + i,
                                       tm_paws["del_p"], tm_paws["grp_sub_p"])

    run_corpus(test_docs, codec, atk_paws, true_uids, candidates,
               f"PAWS 真实 n_dict≈{codec.detect(test_docs[0], min_n=1).n_dict_words} (PAWS 攻击)")


if __name__ == "__main__":
    main()
