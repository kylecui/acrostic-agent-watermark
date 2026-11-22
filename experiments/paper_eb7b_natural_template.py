#!/usr/bin/env python3
"""paper_eb7b_natural_template.py: E-B7b——自然度模板域补测。

E-B7 的病态域（纯句复制）让预过滤拒绝率饱和在 100%，无法区分过滤
器灵敏度。本实验构造**有变化的业务模板**（槽位填充 + 句序扰动，
unique-sentence 比例 ∈ [0.3, 0.9]），测量：
  - 预过滤拒绝率 vs 文本自然度（unique-sentence 比例）
  - 放行子集的 FPR（未标定 / 域内标定）

可复现: python experiments/paper_eb7b_natural_template.py [--seed 20260911]
产出: papers/paperB/data/eb7b_natural_template.json
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

from aawm.keys import generate_session_salt
from aawm.plugins import Watermarker

N_TEXTS = 100
N_USERS = 50

OPENERS = ["尊敬的{name}，您好", "您好{name}", "{name}，感谢联系"]
BODIES = [
    "关于{topic}的事项，我们需要进一步确认细节。",
    "您提出的{topic}需求已收到，我们会尽快处理。",
    "此次{topic}会议定于周五下午三点，请准时参加。",
    "附件是{topic}的相关材料，请查收。",
    "关于{topic}的进度，目前完成了百分之{pct}。",
]
CLOSERS = ["祝好", "此致敬礼", "顺祝商祺", "谢谢"]
NAMES = ["王经理", "李总", "张老师", "陈工", "刘主任", "赵老师"]
TOPICS = ["项目排期", "预算审批", "合同审核", "系统升级", "客户回访", "培训安排"]


def make_natural_template(rng, n_sents=24, vary=0.7):
    """槽位填充模板：vary=句级变化率（1-vary 概率复用已生成句）。"""
    used = []
    for i in range(n_sents):
        if used and rng.random() > vary:
            s = rng.choice(used)
        else:
            s = (rng.choice(OPENERS).format(name=rng.choice(NAMES)) + "。"
                 + rng.choice(BODIES).format(topic=rng.choice(TOPICS),
                                             pct=rng.randint(10, 95))
                 + rng.choice(CLOSERS) + "。")
            used.append(s)
    return "".join(used)


def unique_sentence_ratio(text: str) -> float:
    sents = [s.strip() for s in re.split(r"[。！？!?]", text) if s.strip()]
    return len(set(sents)) / len(sents) if sents else 1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=pc.DEFAULT_SEED)
    args = ap.parse_args()
    rng = random.Random(args.seed + 30)

    texts = [make_natural_template(rng, vary=rng.choice([0.5, 0.7, 0.9]))
             for _ in range(N_TEXTS)]
    uniq = [unique_sentence_ratio(t) for t in texts]

    # 自然度分桶的拒绝率
    buckets = {"<0.5": [], "0.5-0.7": [], ">=0.7": []}
    for t, u in zip(texts, uniq):
        key = "<0.5" if u < 0.5 else ("0.5-0.7" if u < 0.7 else ">=0.7")
        buckets[key].append((t, u))

    half = N_TEXTS // 2

    def eval_mode(mode, pool):
        wm = Watermarker(codec_mode="zero_cost",
                         registry=pc.make_registry(N_USERS))
        if mode in ("calibrated", "prefilter"):
            pc.fit_calibration(wm, pool)
        passed = fp_exist = fp_attr = 0
        skipped_fp = skipped = 0
        for t in pool:
            if mode == "prefilter" and unique_sentence_ratio(t) < 0.5:
                skipped += 1
                r0 = wm.trace(t, session_salt=generate_session_salt())
                if r0.watermarked:
                    skipped_fp += 1
                continue
            passed += 1
            r = wm.trace(t, session_salt=generate_session_salt())
            if r.watermarked:
                fp_exist += 1
            if r.user is not None:
                fp_attr += 1
        return {"mode": mode, "n_passed": passed, "n_skipped": skipped,
                "fpr_existence_passed": round(fp_exist / max(1, passed), 4),
                "fpr_attribution_passed": round(fp_attr / max(1, passed), 4),
                "reject_rate": round(skipped / max(1, passed + skipped), 4),
                "would_be_fp_skipped": skipped_fp}

    results = {"unique_ratio_buckets": {
                   k: {"n": len(v),
                       "uniq_mean": round(sum(u for _t, u in v) / len(v), 3)
                       if v else None}
                   for k, v in buckets.items()}}
    pool = texts[half:]
    results["eval_pool_uniq_mean"] = round(
        sum(unique_sentence_ratio(t) for t in pool) / len(pool), 3)
    for mode in ("none", "calibrated", "prefilter"):
        results[mode] = eval_mode(mode, pool)
        print(results[mode])

    path = pc.save_result("eb7b_natural_template", args.seed,
                          {"n_texts": N_TEXTS, "vary": [0.5, 0.7, 0.9],
                           "prefilter": "unique-sentence < 0.5"},
                          results)
    print(f"\n数据: {path}")
    print("分桶:", results["unique_ratio_buckets"])


if __name__ == "__main__":
    main()
