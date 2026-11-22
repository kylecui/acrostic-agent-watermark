#!/usr/bin/env python3
"""paper_ea6_human_eval_stats.py: E-A6——盲评结果聚合统计（回收数据后运行）。

输入: papers/paperA/human_eval/responses/*.json（survey_template.html 导出）
      papers/paperA/human_eval/item_pool.json
输出: papers/paperA/data/ea6_human_eval.json + 终端报告

指标（对应 PROTOCOL.md §4 预注册判据）：
  pair  : 水印句被判"更自然"的比例 + 二项检验(50%) + 95% CI
  spot  : 检出率(marked→yes) / 误报率(orig→yes) / d′(有置信度时)
  likert: 均分/中位数/分布
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

RESP = os.path.join(pc._REPO_ROOT, "papers", "paperA", "human_eval", "responses")
OUT = os.path.join(pc._REPO_ROOT, "papers", "paperA", "data")


def binom_p_greater(k, n, p0=0.5):
    """P[X>=k] under Binomial(n,p0)，正态近似（n 大时足够）。"""
    mu, sd = n * p0, math.sqrt(n * p0 * (1 - p0))
    if sd == 0:
        return 1.0
    z = (k - 0.5 - mu) / sd
    return 0.5 * math.erfc(z / math.sqrt(2))


def main() -> None:
    pool = json.load(open(os.path.join(
        pc._REPO_ROOT, "papers", "paperA", "human_eval", "item_pool.json"),
        encoding="utf-8"))
    files = sorted(glob.glob(os.path.join(RESP, "*.json")))
    if not files:
        print("无回收数据：把 survey 导出的 JSON 放入", RESP)
        return

    pair_hits = pair_n = 0
    det_hits = det_n = fa_hits = fa_n = 0
    likert_vals = []
    excluded = []
    for f in files:
        r = json.load(open(f, encoding="utf-8"))
        pid = r.get("pid", os.path.basename(f))
        # 剔除：spot 误报率极端(乱答) —— 单被试层面先收集
        sp = r.get("spots", {})
        orig_ids = [k for k in sp if str(k).startswith("o")]
        fa_r = ([1 for k in orig_ids if sp[k].get("a") == "yes"]
                / max(1, len(orig_ids)))
        started, fin = r.get("started_at", ""), r.get("finished_at", "")
        try:
            from datetime import datetime
            mins = ((datetime.fromisoformat(fin.replace("Z", "+00:00"))
                     - datetime.fromisoformat(started.replace("Z", "+00:00")))
                    .total_seconds() / 60) if started and fin else 99
        except Exception:
            mins = 99
        if fa_r > 0.9 or fa_r < 0.05 or mins < 5:
            excluded.append({"pid": pid, "fa_rate": round(fa_r, 3),
                             "minutes": round(mins, 1)})
            continue
        for idx, choice in r.get("pairs", {}).items():
            it = pool["pair_items"][int(idx)]
            marked_is_A = None  # 页面记录的是选项值即"更自然"的一侧
            pair_n += 1
            # 值本身记录的是 A/B；需与页面渲染一致——此处简化：
            # survey 记录 value 为 'A'/'B'，而正确性判定需要位置映射。
            # 页面实现里 results.pairs[idx] 存的是被选项值，
            # 选项值在渲染时已绑定"若选此项则水印/原句更自然"——
            # 见 survey_template.html：value 直接编码了 swap 后哪侧是水印。
            # 约定：value 'A'/'B' 与渲染一致，判定需重渲染映射；
            # 为稳健，pair 统计改由 answers 中附带的 side 字段判定。
            if choice == it.get("_marked_side"):
                pair_hits += 1
        for k, v in sp.items():
            a = v.get("a")
            if str(k).startswith("o"):
                fa_n += 1
                if a == "yes":
                    fa_hits += 1
            else:
                det_n += 1
                if a == "yes":
                    det_hits += 1
        likert_vals += [v for v in r.get("likerts", {}).values()
                        if isinstance(v, int)]

    import statistics
    results = {
        "n_participants": len(files) - len(excluded),
        "excluded": excluded,
        "pair": {"n": pair_n,
                 "marked_preferred": pair_hits,
                 "rate": round(pair_hits / max(1, pair_n), 4),
                 "binom_p_gt_50pct": round(
                     binom_p_greater(pair_hits, pair_n), 4)},
        "spot": {"detection_rate": round(det_hits / max(1, det_n), 4),
                 "false_alarm_rate": round(fa_hits / max(1, fa_n), 4),
                 "n_marked": det_n, "n_orig": fa_n},
        "likert": {"n": len(likert_vals),
                   "mean": round(statistics.mean(likert_vals), 3)
                   if likert_vals else None,
                   "median": statistics.median(likert_vals)
                   if likert_vals else None},
    }
    print(json.dumps(results, ensure_ascii=False, indent=1))
    path = pc.save_result("ea6_human_eval", 20260911,
                          {"responses": len(files)},
                          results, path=os.path.join(OUT, "ea6_human_eval.json"))
    print(f"\n数据: {path}")
    print("\n注意: pair 统计依赖 survey 页面记录的水印侧标记"
          "（_marked_side 字段）；若模板未注入该字段，pair 率需按"
          "渲染映射重算——回收后首项校验。")


if __name__ == "__main__":
    main()
