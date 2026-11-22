#!/usr/bin/env python3
"""paper_eb2_cap_tradeoff.py: E-B2——计票封顶的安全-信号权衡曲线。

横轴: PER_WORD_CAP ∈ {1, 2, 3, 5, 1000(=无封顶)}
两组因变量:
  A. 分离度: demo 语料（域内, zero_cost zh），8 盐 × 5 文档，
     marked vs null 的 Σ|z|/m（min_n=1）均值与 min
  B. 运维数字: 未标定默认阈值下，域内 20 块 null 的 FPR 与同批块
     嵌入后的 TPR（显式盐+meta 路径）

可复现: python experiments/paper_eb2_cap_tradeoff.py [--seed 20260911]
产出: papers/paperB/data/eb2_cap_tradeoff.json + figures/fig_eb2_*.pdf
"""
from __future__ import annotations

import argparse
import contextlib
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

import aawm.greenlist as gl
from aawm.greenlist import build_zero_cost_zh_codec
from aawm.keys import generate_session_salt
from aawm.plugins import Watermarker

KEY = bytes(range(32))
CAPS = [1, 2, 3, 5, 1000]
CAP_LABEL = {1: "1", 2: "2", 3: "3", 5: "5", 1000: "no cap"}


@contextlib.contextmanager
def cap_set(v: int):
    old = gl.PER_WORD_CAP
    gl.PER_WORD_CAP = v
    try:
        yield
    finally:
        gl.PER_WORD_CAP = old


def separation_part(texts) -> dict:
    """A. marked/null Σ|z|/m over salts。"""
    null_r, marked_r = [], []
    for i in range(8):
        codec = build_zero_cost_zh_codec(KEY, generate_session_salt())
        for t in texts:
            rep = codec.detect(t, min_n=1)
            m = sum(1 for st in rep.bands if st.has_signal)
            if m:
                null_r.append(rep.existence_score / m)
            marked, _b = codec.embed_adaptive(t, 0x1, rng=random.Random(i))
            rep2 = codec.detect(marked, min_n=1)
            m2 = sum(1 for st in rep2.bands if st.has_signal)
            if m2:
                marked_r.append(rep2.existence_score / m2)
    return {
        "null_mean": round(statistics.mean(null_r), 3),
        "null_p95": round(sorted(null_r)[int(0.95 * (len(null_r) - 1))], 3),
        "marked_mean": round(statistics.mean(marked_r), 3),
        "marked_min": round(min(marked_r), 3),
        "n_samples": len(null_r),
    }


def operational_part(chunks) -> dict:
    """B. FPR/TPR（未标定默认阈值，域内）。"""
    wm = Watermarker(master_key=KEY, codec_mode="zero_cost")
    fpr = pc.existence_metrics(wm, chunks, "zh")
    tpr = pc.detection_rates(wm, chunks, "zh")
    return {"fpr_existence": round(fpr["fpr_existence"], 4),
            "tpr": round(tpr["tpr"], 4), "n": fpr["n"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=pc.DEFAULT_SEED)
    args = ap.parse_args()

    docs = pc.load_chunks(pc.demo_corpus_text(), 4000, 5, args.seed + 4)
    chunks = pc.load_chunks(pc.demo_corpus_text(), 2500, 20, args.seed + 5)

    per_cap = {}
    for cap in CAPS:
        with cap_set(cap):
            sep = separation_part(docs)
            ops = operational_part(chunks)
        per_cap[str(cap)] = {"label": CAP_LABEL[cap], "separation": sep,
                             "operational": ops}
        print(f"cap={CAP_LABEL[cap]:>7} null_mean={sep['null_mean']:.3f} "
              f"null_p95={sep['null_p95']:.3f} marked_mean={sep['marked_mean']:.3f} "
              f"marked_min={sep['marked_min']:.3f} | FPR={ops['fpr_existence']:.3f} "
              f"TPR={ops['tpr']:.3f}")

    path = pc.save_result(
        "eb2_cap_tradeoff", args.seed,
        {"caps": CAPS, "docs": len(docs), "chunks": len(chunks),
         "salts": 8, "config": "zero_cost zh, uncalibrated defaults"},
        per_cap)
    print(f"\n数据: {path}")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    labels = [per_cap[str(c)]["label"] for c in CAPS]
    null_p95 = [per_cap[str(c)]["separation"]["null_p95"] for c in CAPS]
    marked_min = [per_cap[str(c)]["separation"]["marked_min"] for c in CAPS]
    fpr = [per_cap[str(c)]["operational"]["fpr_existence"] for c in CAPS]
    tpr = [per_cap[str(c)]["operational"]["tpr"] for c in CAPS]

    fig, axes = plt.subplots(1, 2, figsize=(8.6, 3.4))
    ax = axes[0]
    ax.plot(labels, null_p95, "o--", label="null p95")
    ax.plot(labels, marked_min, "s-", label="marked min")
    ax.set_xlabel("per-word vote cap")
    ax.set_ylabel(r"existence score per active band $\Sigma|z|/m$")
    ax.set_title("(a) separation (in-domain, 8 salts)", fontsize=9)
    ax.legend(fontsize=8)
    ax = axes[1]
    ax.plot(labels, tpr, "s-", label="TPR (marked)")
    ax.plot(labels, fpr, "o--", label="FPR (null)")
    ax.set_xlabel("per-word vote cap")
    ax.set_ylabel("rate (uncalibrated defaults)")
    ax.set_title("(b) operational trade-off (in-domain)", fontsize=9)
    ax.legend(fontsize=8)
    fig.tight_layout()
    out = os.path.join("papers", "paperB", "figures", "fig_eb2_cap_tradeoff.pdf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out)
    print(f"图: {out}")


if __name__ == "__main__":
    main()
