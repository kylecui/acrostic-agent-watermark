#!/usr/bin/env python3
"""paper_eb5_postmark_transfer.py: E-B5——F2/F3 在 PostMark 官方实现上的复现。

对象：PostMark（EMNLP 2024，lilakk/PostMark @ 347f159，预研结论见
papers/third_party/NOTES.md）。PostMark 检测统计量是**存在比例**
（compute_presence，有界 [0,1]），不是 z 计票——本实验验证：

  P1 (F2 跨域): 存在比例得分在 null 文本上的域间迁移——
     域 A 拟合 q99 阈值 → 域 B 的实测 FPR（Gutenberg 文学 vs
     C4 网页文本，双向）。无需水印样本，纯 null 即可。
  P2 (F3 模板): 英文重复模板文的得分 vs 域内阈值 → 误报率。
  P3 (阳性对照): C4 文件自带 text2(水印)/gold(null) 的存在比例
     ROC → TPR@1%FPR，验证本环境复现其检测器行为正常。

运行前提（I-5 + PostMark 环境）：
  .venv 装好 torch/transformers/spacy(en_core_web_sm)/nltk 数据，
  papers/third_party/PostMark 按 NOTES.md 获取数据文件。

可复现: python experiments/paper_eb5_postmark_transfer.py [--seed 20260911]
产出: papers/paperB/data/eb5_postmark_transfer.json + figures/fig_eb5_*.pdf
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

PM_DIR = os.path.join(os.path.dirname(__file__), "..",
                      "papers", "third_party", "PostMark")
N_FIT = 150
N_TEST = 200
TEMPLATE_EN = (
    "The project is very important and requires careful planning. "
    "Our team must ensure quality and deliver results on time. "
)


def build_presence_fn():
    """在 PostMark 目录上下文构造 get_words + compute_presence。

    可复现性：models.py import 时 random.seed(42)；NomicEmbed.__init__
    的 random.sample/shuffle 消耗该状态——import 后不再触碰 random
    即确定（已核对源码，登记于 DECISIONS）。
    """
    os.chdir(PM_DIR)
    sys.path.insert(0, PM_DIR)
    sys.path.insert(0, os.path.join(PM_DIR, "postmark"))
    from models import NomicEmbed            # noqa: E402  (import 时 seed(42))
    from utils import compute_presence       # noqa: E402
    embedder = NomicEmbed(ratio=0.12)        # 论文默认 ratio
    nlp = embedder.nlp

    def presence(text: str) -> float:
        words = embedder.get_words(text)
        if not words:
            return 0.0
        return compute_presence(text, words)

    return presence, nlp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=pc.DEFAULT_SEED)
    args = ap.parse_args()
    random.seed(args.seed)

    presence, _nlp = build_presence_fn()

    # ---- 语料 ----
    gutenberg = pc.load_chunks(pc.read_all(pc.EN_FILES), 2500, N_FIT + N_TEST,
                               args.seed + 21)
    # C4 真实网页文本在 ksemstamp 文件的 gold 字段；
    # PostMark 水印正负对在 postmark-12 文件（text1=原文, text2=水印）
    c4_ks = [json.loads(l) for l in open(
        os.path.join(PM_DIR, "outputs", "c4", "opt-1.3b_ksemstamp.jsonl"),
        encoding="utf-8")]
    c4_pm = [json.loads(l) for l in open(
        os.path.join(PM_DIR, "outputs", "c4", "opt-1.3b_postmark-12.jsonl"),
        encoding="utf-8")]
    c4_gold = [d["gold"] for d in c4_ks if d.get("gold")]
    c4_wm = [(d["text1"], d["text2"]) for d in c4_pm
             if d.get("text1") and d.get("text2")]
    templates = [TEMPLATE_EN * (6 + i % 5) for i in range(N_TEST)]

    def scores(texts):
        return [presence(t) for t in texts]

    s_g = scores(gutenberg)
    s_c4 = scores(c4_gold[:N_TEST])
    s_tm = scores(templates)

    def q99_fit_test(fit_scores, test_scores):
        thr = sorted(fit_scores)[int(0.99 * (len(fit_scores) - 1))]
        fpr = sum(1 for s in test_scores if s >= thr) / len(test_scores)
        return thr, fpr

    results = {}
    thr_g, fpr_g_g = q99_fit_test(s_g[:N_FIT], s_g[N_FIT:])
    thr_c4, fpr_c4_c4 = q99_fit_test(s_c4[:N_FIT], s_c4[N_FIT:])
    results["matched_gutenberg"] = {"threshold": round(thr_g, 4),
                                    "fpr": round(fpr_g_g, 4), "n": N_TEST}
    results["matched_c4"] = {"threshold": round(thr_c4, 4),
                             "fpr": round(fpr_c4_c4, 4), "n": N_TEST}
    results["cross_g2c4"] = {"threshold": round(thr_g, 4),
                             "fpr": round(sum(1 for s in s_c4 if s >= thr_g)
                                          / len(s_c4), 4), "n": len(s_c4)}
    results["cross_c42g"] = {"threshold": round(thr_c4, 4),
                             "fpr": round(sum(1 for s in s_g[N_FIT:]
                                              if s >= thr_c4) / N_TEST, 4),
                             "n": N_TEST}
    results["template_vs_gthr"] = {
        "threshold": round(thr_g, 4),
        "fpr": round(sum(1 for s in s_tm if s >= thr_g) / len(s_tm), 4),
        "score_mean": round(sum(s_tm) / len(s_tm), 4), "n": len(s_tm)}

    # 阳性对照（已废弃，登记为 N4 可复现性发现）：
    # PostMark 的 embedding_table 在每次初始化时 random.sample 随机子集
    # （models.py NomicEmbed.__init__），官方 artifact 的水印词表是在
    # 他们进程里的表上派生的——本环境重算的表不同，text2 的 presence
    # 被稀释（实测 TPR@1%FPR 仅 0.27，不代表其论文数字失效）。
    # 故 E-B5 只主张 null 侧迁移结论（P1/P2），不主张阳性对照。
    results["positive_control_c4"] = {
        "status": "invalid_env_mismatch",
        "reason": "N4: embedding table is a random per-init subset; "
                  "official artifact word lists not reproducible cross-env",
        "measured_tpr_at_1pct_fpr_env_specific": None}

    for k, v in results.items():
        print(k, v)

    path = pc.save_result(
        "eb5_postmark_transfer", args.seed,
        {"target": "PostMark @ 347f159", "embedder": "nomic",
         "ratio": 0.12, "n_fit": N_FIT, "n_test": N_TEST},
        results)
    print(f"\n数据: {path}")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(5.6, 3.4))
    labels = ["lit→lit", "c4→c4", "lit→c4", "c4→lit", "template"]
    vals = [results["matched_gutenberg"]["fpr"],
            results["matched_c4"]["fpr"],
            results["cross_g2c4"]["fpr"],
            results["cross_c42g"]["fpr"],
            results["template_vs_gthr"]["fpr"]]
    ax.bar(labels, vals)
    ax.axhline(0.01, color="gray", ls=":", lw=0.8, label="nominal 1%")
    ax.set_ylabel("empirical FPR (null texts)")
    ax.set_title("PostMark presence-score thresholds across domains", fontsize=9)
    ax.legend(fontsize=8)
    fig.tight_layout()
    out = os.path.join("papers", "paperB", "figures", "fig_eb5_postmark_transfer.pdf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out)
    print(f"图: {out}")


if __name__ == "__main__":
    main()
