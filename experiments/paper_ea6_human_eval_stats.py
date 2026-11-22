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
        for idx, rec in r.get("pairs", {}).items():
            pool["pair_items"][int(idx)]  # 校验 item 存在
            pair_n += 1
            # 记录语义（survey_template v1.1）：c=被选侧, m=水印侧
            if isinstance(rec, dict) and rec.get("c") == rec.get("m"):
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
    print("\n预注册判据对照（PROTOCOL.md §4）：")
    pr = results["pair"]["rate"]
    print(f"  pair 水印侧更自然率 {pr:.3f}（不可察觉=0.50, "
          f"binom p={results['pair']['binom_p_gt_50pct']}）")
    dr = results["spot"]["detection_rate"] - results["spot"]["false_alarm_rate"]
    print(f"  spot 检出-误报差 {dr:.3f}（<0.10 不可察觉）")
    lm = results["likert"]["mean"]
    print(f"  likert 均分 {lm}（≥4.0 自然）")


if __name__ == "__main__":
    main()
