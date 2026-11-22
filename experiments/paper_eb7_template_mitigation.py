#!/usr/bin/env python3
"""paper_eb7_template_mitigation.py: E-B7——模板文攻击的缓解分级。

三档缓解 × 模板文（n=100）：
  none        未标定默认（基线）
  calibrated  域内标定（同域前半拟合）
  prefilter   域内标定 + 业务层相似度预过滤（unique-sentence 比例
              < 0.5 的文本跳过水印判定，路由人工）
指标: 存在性 FPR / 归因 FPR / 预过滤拒绝率 / 拒绝集与放行集的 FPR 分列

可复现: python experiments/paper_eb7_template_mitigation.py [--seed 20260911]
产出: papers/paperB/data/eb7_template_mitigation.json
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

from aawm.plugins import Watermarker

N_TEXTS = 100
N_USERS = 50


def unique_sentence_ratio(text: str) -> float:
    """业务层模板检测器：去重句比例。模板文大量重复 → 比例低。"""
    sents = [s.strip() for s in re.split(r"[。！？!?]", text) if s.strip()]
    if not sents:
        return 1.0
    return len(set(sents)) / len(sents)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=pc.DEFAULT_SEED)
    args = ap.parse_args()

    texts = pc.repetitive_template_texts(N_TEXTS, args.seed)
    half = N_TEXTS // 2
    results = {}

    def run(mode):
        wm = Watermarker(codec_mode="zero_cost",
                         registry=pc.make_registry(N_USERS))
        if mode in ("calibrated", "prefilter"):
            pc.fit_calibration(wm, texts[:half])
        passed = skipped = 0
        fp_exist = fp_attr = fp_exist_skipped = 0
        for t in texts[half:]:
            if mode == "prefilter" and unique_sentence_ratio(t) < 0.5:
                skipped += 1
                # 被路由走的文本若做检测会是什么结果（纸面数字，不入运营指标）
                r0 = wm.trace(t, session_salt=__import__(
                    "aawm.keys", fromlist=["generate_session_salt"]
                ).generate_session_salt())
                if r0.watermarked:
                    fp_exist_skipped += 1
                continue
            passed += 1
            r = wm.trace(t, session_salt=__import__(
                "aawm.keys", fromlist=["generate_session_salt"]
            ).generate_session_salt())
            if r.watermarked:
                fp_exist += 1
            if r.user is not None:
                fp_attr += 1
        return {
            "mode": mode, "n_passed": passed, "n_skipped": skipped,
            "fpr_existence_passed": round(fp_exist / max(1, passed), 4),
            "fpr_attribution_passed": round(fp_attr / max(1, passed), 4),
            "prefilter_reject_rate": round(skipped / (passed + skipped), 4),
            "would_be_fp_in_skipped": fp_exist_skipped,
        }

    for mode in ("none", "calibrated", "prefilter"):
        results[mode] = run(mode)
        print(results[mode])

    path = pc.save_result(
        "eb7_template_mitigation", args.seed,
        {"n_texts": N_TEXTS, "n_users": N_USERS,
         "prefilter": "unique-sentence ratio < 0.5"},
        results)
    print(f"\n数据: {path}")


if __name__ == "__main__":
    main()
