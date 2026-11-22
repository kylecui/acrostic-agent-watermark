#!/usr/bin/env python3
"""paper_eb1_correlated_votes.py: E-B1——F1 相关票攻击曲线。

构造：无水印文本把单个词典词 w 重复 N 次（N ∈ {1,10,40,160,640}）。
两条曲线同图：
  uncapped  v0.14 行为（出现次数全计，复现历史缺陷）
  capped    v0.15 行为（PER_WORD_CAP=3，当前主包行为）
对照线：默认模式阈值 max(8, 2√n) 与自适应阈值线（演示越阈）。

可复现: python experiments/paper_eb1_correlated_votes.py [--seed 20260911]
产出: papers/paperB/data/eb1_correlated_votes.json + figures/fig_eb1_*.pdf
"""
from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import paper_common as pc

from aawm.greenlist import build_zero_cost_zh_codec

KEY = bytes(range(32))
SALT = bytes(range(16))
WORD_IDX = 0  # sorted(_w2band)[0]，与 v0.15 修复验证时同一词

N_VALUES = [1, 10, 40, 160, 640]
TEMPLATE = "我们要重视{w}建设。"


def detect_uncapped(codec, text: str) -> float:
    """v0.14 口径：出现次数全计（无封顶）。"""
    n_per = [0] * codec.n_bands
    g_per = [0] * codec.n_bands
    for _raw, norm in codec._tokenizer(text):
        if norm is None:
            continue
        b = codec._w2band.get(norm)
        if b is not None:
            n_per[b] += 1
            g_per[b] += codec.green(norm)
    total = 0.0
    for b in range(codec.n_bands):
        n, g = n_per[b], g_per[b]
        if n < 1:
            continue
        p0 = codec._p0_of(b)
        var = p0 * (1.0 - p0) * n
        if var > 0:
            total += abs((g - p0 * n) / var ** 0.5)
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=pc.DEFAULT_SEED)
    args = ap.parse_args()

    codec = build_zero_cost_zh_codec(KEY, SALT)
    word = sorted(codec._w2band)[WORD_IDX]
    band = codec._w2band[word]
    green = codec.green(word)

    rows = []
    for n_rep in N_VALUES:
        text = "".join(TEMPLATE.format(w=word) for _ in range(n_rep))
        capped = codec.detect(text, min_n=1).existence_score
        uncapped = detect_uncapped(codec, text)
        n_dict = codec.detect(text, min_n=1).n_dict_words
        rows.append({
            "n_repetitions": n_rep,
            "existence_capped_v015": round(capped, 3),
            "existence_uncapped_v014": round(uncapped, 3),
            "n_dict_words": n_dict,
            "default_threshold": round(max(8.0, 2.0 * math.sqrt(n_dict)), 3),
            "crosses_default_capped": capped >= max(8.0, 2.0 * math.sqrt(n_dict)),
            "crosses_default_uncapped": uncapped >= max(8.0, 2.0 * math.sqrt(n_dict)),
        })
        print(f"N={n_rep:4d} capped={capped:7.2f} uncapped={uncapped:8.2f} "
              f"thr={max(8.0, 2.0 * math.sqrt(n_dict)):.2f}")

    results = {"word": word, "band": band, "green": green, "rows": rows}
    path = pc.save_result(
        "eb1_correlated_votes", args.seed,
        {"key": "bytes(range(32))", "salt": "bytes(range(16))",
         "codec": "zero_cost_zh", "word_index": WORD_IDX,
         "template": TEMPLATE, "n_values": N_VALUES},
        results)
    print(f"\n数据: {path}")

    # 图（matplotlib 由 make_figs 统一规范，这里直接出 EB1 单图）
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    xs = [r["n_repetitions"] for r in rows]
    fig, ax = plt.subplots(figsize=(5.2, 3.4))
    ax.plot(xs, [r["existence_uncapped_v014"] for r in rows], "o--",
            label="uncapped counting (v0.14 behavior)")
    ax.plot(xs, [r["existence_capped_v015"] for r in rows], "s-",
            label=f"capped at 3 votes/word (v0.15)")
    ax.axhline(8.0, color="gray", lw=0.8, ls=":",
               label="default existence threshold")
    ax.set_xscale("log")
    ax.set_xlabel("repetitions of one dictionary word (unwatermarked text)")
    ax.set_ylabel(r"existence score $\Sigma|z|$")
    ax.legend(fontsize=8)
    fig.tight_layout()
    out = os.path.join("papers", "paperB", "figures", "fig_eb1_correlated_votes.pdf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out)
    print(f"图: {out}")


if __name__ == "__main__":
    main()
