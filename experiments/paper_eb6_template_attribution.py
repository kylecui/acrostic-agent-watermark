#!/usr/bin/env python3
"""paper_eb6_template_attribution.py: E-B6——模板文误归因机理。

矩阵: 模板文 × 注册库规模 {1, 10, 50, 200}
指标: 存在性 FPR、归因 FPR（user 非空）、胜出候选的得分占比
      best_score / Σ|z|（机理量：>0.5 说明信号与某候选真实对齐，
      <0.5 是噪声随机游走撞中——守门阈值恰好卡在这里）
机理诊断: 每个 (文本, 库规模) 记录逐带 z 与胜出候选位方向的一致率。

可复现: python experiments/paper_eb6_template_attribution.py [--seed 20260911]
产出: papers/paperB/data/eb6_template_attribution.json + figures/fig_eb6_*.pdf
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

from aawm.keys import generate_session_salt
from aawm.plugins import Watermarker

N_TEXTS = 100
SIZES = [1, 10, 50, 200]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=pc.DEFAULT_SEED)
    args = ap.parse_args()

    texts = pc.repetitive_template_texts(N_TEXTS, args.seed)
    results = {}
    for size in SIZES:
        wm = Watermarker(codec_mode="zero_cost",
                         registry=pc.make_registry(size))
        fp_exist = fp_attr = 0
        score_ratios = []
        align_rates = []
        n = 0
        for t in texts:
            salt = generate_session_salt()
            codec = wm._build_codec(salt, b"zh")
            rep = codec.detect(t, min_n=1)
            cands = sorted(wm.registry.list_all())
            best, best_score, gap = codec.soft_match(t, cands, min_n=1)
            tr = wm.trace(t, session_salt=salt)
            n += 1
            if tr.watermarked:
                fp_exist += 1
            if tr.user is not None:
                fp_attr += 1
            if best is not None and rep.existence_score > 0:
                score_ratios.append(best_score / rep.existence_score)
                # 机理量: 胜出候选位方向与逐带 z 符号的一致率
                agrees = sum(
                    1 for st in rep.bands if st.has_signal
                    and (((best >> st.band) & 1) == 1) == (st.z > 0))
                k = sum(1 for st in rep.bands if st.has_signal)
                if k:
                    align_rates.append(agrees / k)
        results[str(size)] = {
            "registry_size": size, "n": n,
            "fpr_existence": round(fp_exist / n, 4),
            "fpr_attribution": round(fp_attr / n, 4),
            "score_ratio_mean": round(sum(score_ratios) / len(score_ratios), 4)
            if score_ratios else None,
            "band_align_rate_mean": round(sum(align_rates) / len(align_rates), 4)
            if align_rates else None,
        }
        r = results[str(size)]
        print(f"registry={size:4d} FPR={r['fpr_existence']:.3f} "
              f"attr-FPR={r['fpr_attribution']:.3f} "
              f"score_ratio={r['score_ratio_mean']} "
              f"align={r['band_align_rate_mean']}")

    path = pc.save_result(
        "eb6_template_attribution", args.seed,
        {"registry_sizes": SIZES, "n_texts": N_TEXTS,
         "config": "zero_cost zh, uncalibrated"},
        results)
    print(f"\n数据: {path}")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(5.2, 3.4))
    ax.plot(SIZES, [results[str(s)]["fpr_existence"] for s in SIZES],
            "o--", label="existence FPR")
    ax.plot(SIZES, [results[str(s)]["fpr_attribution"] for s in SIZES],
            "s-", label="attribution FPR")
    ax.set_xscale("log")
    ax.set_xlabel("registry size (users)")
    ax.set_ylabel("false positive rate (template texts)")
    ax.set_ylim(0, 1.05)
    ax.legend(fontsize=8)
    fig.tight_layout()
    out = os.path.join("papers", "paperB", "figures",
                       "fig_eb6_template_attribution.pdf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out)
    print(f"图: {out}")


if __name__ == "__main__":
    main()
