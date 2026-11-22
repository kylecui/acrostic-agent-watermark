#!/usr/bin/env python3
"""paper_ea4_postmark_pipeline.py: E-A4——PostMark 嵌入管线在本环境的复现。

动机：E-B5 的阳性对照因 embedding table 跨环境不可逐位复现（N4）被废弃。
本实验用**本环境的表**走完整管线：NomicEmbed 选词（本地 GPU）→
SiliconFlow Qwen2.5-32B 按官方 insert.txt 提示插入（忠实复刻官方
v2 迭代循环与 presence>=0.5 重试≤3 的验收）→ compute_presence 打分。
指标：ROC / TPR@1%FPR（gold 为负、text2 为正）。

可复现: python experiments/paper_ea4_postmark_pipeline.py [--n 60]
产出: papers/paperA/data/ea4_postmark_pipeline.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

PM_DIR = os.path.join(pc._REPO_ROOT, "papers", "third_party", "PostMark")
OUT = os.path.join(pc._REPO_ROOT, "papers", "paperA", "data")
MODEL = "Qwen/Qwen2.5-32B-Instruct"


def call_api(prompt, key, max_tokens=1600, temperature=0.0, max_retries=4):
    req = urllib.request.Request(
        "https://api.siliconflow.cn/v1/chat/completions",
        data=json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    last = None
    for a in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (a + 1))
    raise last


def build_postmark():
    os.chdir(PM_DIR)
    sys.path.insert(0, PM_DIR)
    sys.path.insert(0, os.path.join(PM_DIR, "postmark"))
    from models import NomicEmbed            # noqa: E402
    from utils import compute_presence       # noqa: E402
    embedder = NomicEmbed(ratio=0.12)
    template = open(os.path.join(PM_DIR, "prompts", "insert.txt"),
                    encoding="utf-8").read()
    return embedder, compute_presence, template


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--workers", type=int, default=5)
    args = ap.parse_args()
    key = os.environ["SILICONFLOW_API_KEY"]
    os.makedirs(OUT, exist_ok=True)

    embedder, compute_presence, template = build_postmark()

    c4_ks = [json.loads(l) for l in open(
        os.path.join(PM_DIR, "outputs", "c4", "opt-1.3b_ksemstamp.jsonl"),
        encoding="utf-8")]
    golds = [d["gold"] for d in c4_ks if d.get("gold")][:args.n]
    print(f"gold texts: {len(golds)}")

    lock = threading.Lock()
    usage_f = open(os.path.join(OUT, "api_usage_ea4.jsonl"), "a", encoding="utf-8")
    rows = []

    gpu_lock = threading.Lock()

    def worker(i_gold):
        i, gold = i_gold
        gold = gold[:2500]
        with gpu_lock:
            words = embedder.get_words(gold)
        if not words:
            return
        # 官方 v2 迭代插入（忠实复刻：10 词子表 / presence>=0.5 / 重试<=3）
        cur = gold
        for s0 in range(0, len(words), 10):
            sub = words[s0:s0 + 10]
            sub_str = ", ".join(sub)
            presence, attempt = 0.0, 0
            while presence < 0.5 and attempt < 3:
                prompt = template.format(cur, sub_str)
                resp = call_api(prompt, key)
                cand = resp["choices"][0]["message"]["content"]
                with lock:
                    usage_f.write(json.dumps(
                        {"i": i, "attempt": attempt, "usage": resp["usage"]})
                        + chr(10))
                presence = sum(1 for w in sub if w.lower() in cand.lower()) / len(sub)
                if presence >= 0.5 or attempt == 2:
                    cur = cand
                attempt += 1
        marked = cur
        with gpu_lock:
            s_gold = compute_presence(gold, words)
            words2 = embedder.get_words(marked)
            s_marked = compute_presence(marked, words2) if words2 else 0.0
        with lock:
            rows.append({"i": i, "presence_gold": round(s_gold, 4),
                         "presence_marked": round(s_marked, 4),
                         "n_words": len(words)})
            print(f"[{len(rows)}/{len(golds)}] {s_gold:.3f} -> {s_marked:.3f}",
                  flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(worker, enumerate(golds)))
    usage_f.close()

    pos = sorted(r["presence_marked"] for r in rows)
    neg = sorted(r["presence_gold"] for r in rows)
    thr1 = neg[int(0.99 * (len(neg) - 1))]
    tpr1 = sum(1 for s in pos if s >= thr1) / len(pos)
    # text1(null model output) 混入看 ROC 的 AUC 近似
    results = {"n": len(rows),
               "tpr_at_1pct_fpr": round(tpr1, 4),
               "threshold_at_1pct_fpr": round(thr1, 4),
               "presence_gold_mean": round(sum(neg) / len(neg), 4),
               "presence_marked_mean": round(sum(pos) / len(pos), 4),
               "rows": rows}
    print(results)
    path = pc.save_result(
        "ea4_postmark_pipeline", 20260911,
        {"n": len(rows), "inserter": MODEL, "embedder": "nomic",
         "ratio": 0.12, "loop": "official v2 (10-word sublists, presence>=0.5, <=3 tries)"},
        results, path=os.path.join(OUT, "ea4_postmark_pipeline.json"))
    print(f"数据: {path}")


if __name__ == "__main__":
    main()
