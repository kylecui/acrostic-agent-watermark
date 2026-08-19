#!/usr/bin/env python3
"""exp_margin_scale.py: margin 自适应标定研究（方向 2）。

问题：soft_match 的 margin 是绝对值（默认 2.0）。但 soft 得分
s(c) = Σ z_b·(±1)，z 是标准正态统计量，其总和尺度随证据量（词典词数）
增长。实测 600 词合成文本 gap 可达 26.9~81.7，而短文本/弱证据下
gap 可能 <2。固定 margin 对长文本偏松（自信地错）、对短文本偏紧。

本实验测量不同证据量下 正确/错误/随机 匹配的 gap 分布，
验证 gap 是否随 √n 增长，并为归一化 margin 提供数据。

验证目标：
  1. gap_correct ≈ O(√n)？ gap_wrong ≈ O(√n)？两者比率是否稳定？
  2. 归一化 gap' = gap / √n_active 是否让阈值变得可迁移（不随文本长度变）？
  3. 相对 gap' = (best - second) / |best| 是否更稳定？
"""
from __future__ import annotations

import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from exp_capability_demo import make_en_text, en_paraphrase, KEY, SALT


def collect(codec: GreenlistCodec, docs, attack_frac, seed0, candidates):
    """跑一组样本，返回每样本的 (n_dict, n_active, gap, correct, best, second)。"""
    rows = []
    for i, doc in enumerate(docs):
        uid = candidates[i % len(candidates)]
        marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(seed0 + i))
        rw, _ = en_paraphrase(codec, marked, attack_frac, seed0 + 1000 + i)
        rep = codec.detect(rw, min_n=1)
        n_active = sum(1 for st in rep.bands if st.has_signal)
        _, s_best, gap = codec.soft_match(rw, candidates, min_n=1, margin=0.0)
        correct = 1 if codec.soft_match(rw, candidates, min_n=1, margin=0.0)[0] == uid else 0
        rows.append((rep.n_dict_words, n_active, gap, correct))
    return rows


def main() -> None:
    random.seed(0)
    en = GreenlistCodec(KEY, SALT, language_tag=b"en")
    null_corpus = [make_en_text(600, s, en) for s in range(100, 108)]
    en.calibrate_p0(null_corpus)

    candidates = [0x1000, 0x1234, 0x2000, 0xABCD, 0x00FF, 0xF0F0]

    print("=== 不同文本长度 x 攻击强度 的 gap 尺度 ===")
    print(f"{'长度':>4s} {'攻击':>4s} | {'n_dict':>6s} {'n_act':>5s} | "
          f"{'正确gap':>8s} {'错gap':>7s} | {'gap/√n':>7s} {'ratio':>6s}")
    for length in (150, 300, 600):
        docs = [make_en_text(length, s, en) for s in range(200, 206)]
        for frac in (0.0, 0.30, 0.50):
            rows = collect(en, docs, frac, 300, candidates)
            ok = [r[2] for r in rows if r[3]]
            bad = [r[2] for r in rows if not r[3]]
            n_dict = sum(r[0] for r in rows) / len(rows)
            n_act = sum(r[1] for r in rows) / len(rows)
            g_ok = sum(ok) / len(ok) if ok else float("nan")
            g_bad = sum(bad) / len(bad) if bad else float("nan")
            g_norm = g_ok / (n_act ** 0.5) if ok else float("nan")
            ratio = g_bad / g_ok if ok and bad else float("nan")
            print(f"{length:>4d} {int(frac*100):>3d}% | {n_dict:>6.0f} {n_act:>5.0f} | "
                  f"{g_ok:>8.1f} {g_bad:>7.1f} | {g_norm:>7.2f} {ratio:>6.2f}")

    print("\n=== 归一化候选：gap/√n_active 的阈值可迁移性 ===")
    print("若 gap/√n ≈ 常数，则阈值可按 √n 缩放（margin = k·√n_active）")
    # null 对照：无嵌入文本上 soft_match 的 gap 尺度
    print("\n=== null（未嵌）文本的 gap 尺度 ===")
    for length in (150, 300, 600):
        docs = [make_en_text(length, s, en) for s in range(400, 408)]
        gaps, nacts = [], []
        for d in docs:
            rep = en.detect(d, min_n=1)
            nacts.append(sum(1 for st in rep.bands if st.has_signal))
            _, _, gap = en.soft_match(d, candidates, min_n=1, margin=0.0)
            gaps.append(gap)
        mu = sum(gaps) / len(gaps)
        sd = (sum((g - mu) ** 2 for g in gaps) / len(gaps)) ** 0.5
        na = sum(nacts) / len(nacts)
        print(f"{length:>4d} | null gap 均值={mu:>5.1f} 标准差={sd:>5.1f} "
              f"max={max(gaps):>5.1f} | √n_active≈{na**0.5:.1f} | gap/√n≈{mu/(na**0.5):.2f}")

    print("\n=== 相对 gap：(best-second)/best 的分布 ===")
    for frac in (0.30, 0.50):
        docs = [make_en_text(600, s, en) for s in range(500, 506)]
        rows = collect(en, docs, frac, 600, candidates)
        ok = [r[2] for r in rows if r[3]]
        bad = [r[2] for r in rows if not r[3]]
        m_ok = sum(ok) / len(ok) if ok else float("nan")
        m_bad = sum(bad) / len(bad) if bad else float("nan")
        print(f"攻击{int(frac*100)}%: 正确gap均值={m_ok:.1f} 错误gap均值={m_bad:.1f} "
              f"匹配率={len(ok)}/6")


if __name__ == "__main__":
    main()
