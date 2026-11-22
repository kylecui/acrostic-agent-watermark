#!/usr/bin/env python3
"""paper_ea0_unedited_baseline.py: E-A0——真实 LLM 语料未编辑泄露基线。

embed -> trace（显式盐+meta 路径，无攻击）的 TPR 与归因正确率；
零感词典域内标定 vs 未标定双配置。zh 语料 n 可全量。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

from aawm.plugins import Watermarker

OUT = os.path.join(pc._REPO_ROOT, "papers", "paperA", "data")


def run(wm, texts, uid=7):
    tp = attr_ok = n = 0
    for t in texts:
        emb = wm.embed(t, user_id=uid, language="zh")
        r = wm.trace(emb.watermarked_text, session_salt=emb.session_salt,
                     bands=emb.bands, language="zh")
        n += 1
        if r.watermarked:
            tp += 1
        if r.user == f"user-{uid}" or r.uid == uid:
            attr_ok += 1
    return {"tpr": round(tp / n, 4), "attr_exact": round(attr_ok / n, 4), "n": n}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    args = ap.parse_args()

    texts = []
    for line in open(os.path.join(OUT, "llm_corpus_zh.jsonl"), encoding="utf-8"):
        d = json.loads(line)
        if len(d["text"]) >= 800:
            texts.append(d["text"])
        if len(texts) >= args.n:
            break

    half = max(10, len(texts) // 2)
    wm_uncal = Watermarker(master_key=bytes(range(32)), codec_mode="zero_cost")
    wm_cal = Watermarker(master_key=bytes(range(32)), codec_mode="zero_cost")
    pc.fit_calibration(wm_cal, texts[:half])

    reg_results = {}
    from aawm.plugins.registry import UIDRegistry
    for name, wm in (("uncalibrated", wm_uncal), ("calibrated", wm_cal)):
        reg = UIDRegistry()
        reg.register("user-7", uid=7)
        wm._registry = reg
        reg_results[name] = run(wm, texts[half:])
        print(name, reg_results[name])

    path = pc.save_result(
        "ea0_unedited_baseline", 20260911,
        {"n_texts": len(texts) - half, "uid": 7,
         "config": "zero_cost zh, explicit salt+meta path, no attack"},
        reg_results, path=os.path.join(OUT, "ea0_unedited_baseline.json"))
    print(f"数据: {path}")


if __name__ == "__main__":
    main()
