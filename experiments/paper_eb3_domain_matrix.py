#!/usr/bin/env python3
"""paper_eb3_domain_matrix.py: E-B3——F2 域矩阵扩展版。

域: demo_in_domain / en_literature / zh_literature / code_comment /
    short_fragments / repetitive_template（E-B6/E-B7 共用该域）
配置: uncalibrated / calibrated_xdomain(demo 标定) / calibrated_matched
      (同域前半拟合,后半测试)
指标: FPR_existence / FPR_attribution（注册库 50 用户）/ TPR（同批文本
      嵌入对照）。阴性结果全量入库（PLAN §3.3）。

可复现: python experiments/paper_eb3_domain_matrix.py [--seed 20260911]
产出: papers/paperB/data/eb3_domain_matrix.json + figures/fig_eb3_*.pdf
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

from aawm.plugins import Watermarker

N_TEXTS = 100
SIZE = 2500
N_USERS = 50
DOMAINS = ["demo_in_domain", "en_literature", "zh_literature",
           "code_comment", "short_fragments"]
LANG = {"demo_in_domain": "zh", "en_literature": "en",
        "zh_literature": "zh", "code_comment": "zh",
        "short_fragments": "zh", "repetitive_template": "zh"}


def run_config(texts, lang, calib):
    wm = Watermarker(codec_mode="zero_cost",
                     registry=pc.make_registry(N_USERS))
    if calib:
        pc.fit_calibration(wm, calib)
    fpr = pc.existence_metrics(wm, texts, lang)
    tpr = pc.detection_rates(wm, texts, lang)
    return {**{k: round(v, 4) if isinstance(v, float) else v
               for k, v in fpr.items()},
            "tpr": round(tpr["tpr"], 4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=pc.DEFAULT_SEED)
    ap.add_argument("--texts", type=int, default=N_TEXTS)
    args = ap.parse_args()

    demo_calib = pc.load_chunks(pc.demo_corpus_text(), 1200, 20, args.seed + 9)

    results = {}
    all_domains = DOMAINS + ["repetitive_template"]
    for name in all_domains:
        lang = LANG[name]
        if name == "repetitive_template":
            texts = pc.repetitive_template_texts(args.texts, args.seed)
        elif name == "short_fragments":
            texts = pc.make_domain_corpus(name, 800, args.texts, args.seed)
        elif name == "demo_in_domain":
            # demo 语料总量仅 ~24k 字：2500 字块只有 ~16 个，域内标定
            # 的 half ≥10 约束不满足；改用 1500 字块（~30 个）
            texts = pc.make_domain_corpus(name, 1500, args.texts, args.seed)
        else:
            texts = pc.make_domain_corpus(name, SIZE, args.texts, args.seed)
        n = len(texts)
        half = max(5, n // 2)

        row = {"n": n,
               "uncalibrated": run_config(texts, lang, None),
               "calibrated_xdomain": run_config(texts, lang, demo_calib),
               "calibrated_matched": run_config(texts[half:], lang,
                                                texts[:half])}
        results[name] = row
        print(f"[{name}] n={n}")
        for cfg in ("uncalibrated", "calibrated_xdomain", "calibrated_matched"):
            r = row[cfg]
            print(f"    {cfg}: FPR={r['fpr_existence']:.3f} "
                  f"attr-FPR={r['fpr_attribution']:.3f} TPR={r['tpr']:.3f}")

    path = pc.save_result(
        "eb3_domain_matrix", args.seed,
        {"domains": all_domains, "size": SIZE, "n_users": N_USERS,
         "codec": "zero_cost", "config": "explicit-salt path"},
        results)
    print(f"\n数据: {path}")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    cfgs = ["uncalibrated", "calibrated_xdomain", "calibrated_matched"]
    labels = [d.replace("_", "\n") for d in all_domains]
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.6))
    for ax, metric, title in (
            (axes[0], "fpr_existence", "(a) existence FPR (null)"),
            (axes[1], "tpr", "(b) TPR (marked control)")):
        x = range(len(all_domains))
        width = 0.26
        for i, cfg in enumerate(cfgs):
            vals = [results[d][cfg][metric] for d in all_domains]
            ax.bar([xi + (i - 1) * width for xi in x], vals, width,
                   label=cfg.replace("_", " "))
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, fontsize=7)
        ax.set_title(title, fontsize=9)
        ax.legend(fontsize=7)
        ax.set_ylim(0, 1.05)
    fig.tight_layout()
    out = os.path.join("papers", "paperB", "figures", "fig_eb3_domain_matrix.pdf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out)
    print(f"图: {out}")


if __name__ == "__main__":
    main()
