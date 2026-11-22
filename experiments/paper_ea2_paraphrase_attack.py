#!/usr/bin/env python3
"""paper_ea2_paraphrase_attack.py: E-A2——真实 LLM 改写攻击下的检出/归因存活。

流程（每篇语料文本）：
  1. AAWM 嵌入（显式 uid，存档 salt+bands）
  2. 经 SiliconFlow 用 LLM 做 X% 强度改写（X ∈ {15, 30, 50}）——
     指令对齐经典 syn-attack：保持语义与篇幅，替换约 X% 的实词
  3. 以存档 meta trace 改写文本：记录 watermarked / user / abstain

配置：zero_cost zh + 域内标定（语料前半拟合） vs 未标定，双配置。
费用：落 api_usage_ea2.jsonl；预算并入 E-A1 上限（总 ¥200）。

可复现: python experiments/paper_ea2_paraphrase_attack.py [--n 100] [--strengths 15,30,50]
产出: papers/paperA/data/ea2_paraphrase_attack.json
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

from aawm.plugins import Watermarker

OUT = os.path.join("papers", "paperA", "data")
STRENGTH_INSTR = {
    15: "保持原意与篇幅基本不变，只替换约15%的实词为同义词或近义表达，"
        "句式尽量不动。直接输出改写后的全文。",
    30: "保持原意与篇幅基本不变，替换约30%的实词为同义词，并调整部分句式。"
        "直接输出改写后的全文。",
    50: "保持核心意思不变，改写约一半的句子（换句式、换同义词），"
        "篇幅相近。直接输出改写后的全文。",
}


def call_api(prompt, key, model="Qwen/Qwen2.5-32B-Instruct", max_retries=4):
    req = urllib.request.Request(
        "https://api.siliconflow.cn/v1/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 1600,
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="zh 语料使用篇数")
    ap.add_argument("--strengths", default="15,30,50")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--uid", type=int, default=7)
    args = ap.parse_args()
    strengths = [int(x) for x in args.strengths.split(",")]
    key = os.environ["SILICONFLOW_API_KEY"]
    os.makedirs(OUT, exist_ok=True)

    texts = []
    for line in open(os.path.join(OUT, "llm_corpus_zh.jsonl"), encoding="utf-8"):
        d = json.loads(line)
        if len(d["text"]) >= 800:
            texts.append(d["text"])
        if len(texts) >= args.n:
            break

    half = max(10, len(texts) // 2)
    wm_cal = Watermarker(master_key=bytes(range(32)), codec_mode="zero_cost")
    pc.fit_calibration(wm_cal, texts[:half])
    wm_uncal = Watermarker(master_key=bytes(range(32)), codec_mode="zero_cost")

    results = {s: {"calibrated": {"tp": 0, "attr_ok": 0, "abstain": 0, "n": 0},
                   "uncalibrated": {"tp": 0, "attr_ok": 0, "abstain": 0, "n": 0}}
               for s in strengths}
    usage_f = open(os.path.join(OUT, "api_usage_ea2.jsonl"), "a", encoding="utf-8")
    lock = threading.Lock()
    jobs = [(i, s) for i in range(half, len(texts)) for s in strengths]

    def worker(job):
        i, s = job
        t = texts[i]
        emb = wm_cal.embed(t, user_id=args.uid)
        resp = call_api(
            "请改写以下文本。" + STRENGTH_INSTR[s] + chr(10) + chr(10) + t, key)
        para = resp["choices"][0]["message"]["content"]
        with lock:
            usage_f.write(json.dumps(
                {"i": i, "strength": s, "usage": resp["usage"]}) + chr(10))
            for mode, wm in (("calibrated", wm_cal), ("uncalibrated", wm_uncal)):
                r = wm.trace(para, session_salt=emb.session_salt, bands=emb.bands)
                st = results[s][mode]
                st["n"] += 1
                if r.watermarked:
                    st["tp"] += 1
                if r.user is not None:
                    st["attr_ok"] += 1
                if r.attribution_abstain:
                    st["abstain"] += 1

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(worker, jobs))
    usage_f.close()

    for s in strengths:
        for mode in ("calibrated", "uncalibrated"):
            st = results[s][mode]
            n = max(1, st["n"])
            print(f"s{s:02d} {mode:12s} TPR={st['tp']/n:.3f} "
                  f"attr={st['attr_ok']/n:.3f} abstain={st['abstain']/n:.3f} n={st['n']}")

    path = pc.save_result(
        "ea2_paraphrase_attack", 20260911,
        {"n_texts": len(texts) - half, "strengths": strengths,
         "uid": args.uid, "model": "Qwen2.5-32B"},
        results, path=os.path.join(OUT, "ea2_paraphrase_attack.json"))
    print(f"数据: {path}")


if __name__ == "__main__":
    main()
