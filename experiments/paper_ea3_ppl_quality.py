#!/usr/bin/env python3
"""paper_ea3_ppl_quality.py: E-A3——嵌入质量验证（本地 Qwen2.5-7B-AWQ 困惑度）。

指标：同一批 zh 语料的原文 vs AAWM 水印文，token 平均 NLL（sliding
window, stride=512, max_len=2048）。报告 mean/median NLL 比与差值。
声明边界：PPL 是质量代理之一，人工盲评另列（本实验不含）。

前置（I-5）：.venv 装好 torch/transformers，Qwen2.5-7B-Instruct-AWQ 已下载。
可复现: python experiments/paper_ea3_ppl_quality.py [--n 60]
产出: papers/paperA/data/ea3_ppl_quality.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

OUT = os.path.join("papers", "paperA", "data")
MODEL = "models/models/Qwen--Qwen2.5-3B-Instruct/snapshots/master"


@torch.no_grad()
def text_nll(model, tok, text, max_len=2048, stride=512, device="cuda"):
    enc = tok(text, return_tensors="pt").input_ids[0]
    if len(enc) < 8:
        return None
    nlls, prev_end = [], 0
    for begin in range(0, len(enc), stride):
        end = min(begin + max_len, len(enc))
        window = enc[begin:end].unsqueeze(0).to(device)
        out = model(window, labels=window)
        nll = out.loss.float() * (end - begin)
        if begin == 0:
            nlls.append(nll)
        else:  # 重叠区只计新 token，避免重复计权
            nlls.append(out.loss.float() * (end - prev_end))
        prev_end = end
        if end == len(enc):
            break
    return (sum(nlls) / len(enc)).item()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--uid", type=int, default=7)
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, device_map="cuda", torch_dtype=torch.float16).eval()

    from aawm.plugins import Watermarker
    wm = Watermarker(master_key=bytes(range(32)), codec_mode="zero_cost")

    rows = []
    with open(os.path.join(OUT, "llm_corpus_zh.jsonl"), encoding="utf-8") as f:
        for k, line in enumerate(f):
            if k >= args.n:
                break
            d = json.loads(line)
            t = d["text"]
            if len(t) < 800:
                continue
            emb = wm.embed(t, user_id=args.uid)
            nll0 = text_nll(model, tok, t)
            nll1 = text_nll(model, tok, emb.watermarked_text)
            rows.append({"nll_orig": round(nll0, 4),
                         "nll_marked": round(nll1, 4),
                         "ratio": round(nll1 / nll0, 4)})
            print(f"[{len(rows)}/{args.n}] nll {nll0:.3f} -> {nll1:.3f}", flush=True)

    import statistics
    ratios = [r["ratio"] for r in rows]
    results = {"n": len(rows),
               "nll_ratio_mean": round(statistics.mean(ratios), 4),
               "nll_ratio_median": round(statistics.median(ratios), 4),
               "nll_ratio_p95": round(sorted(ratios)[int(0.95 * (len(ratios) - 1))], 4),
               "rows": rows}
    print(results["nll_ratio_mean"], results["nll_ratio_median"])
    path = pc.save_result(
        "ea3_ppl_quality", 20260911,
        {"model": MODEL, "n": len(rows), "uid": args.uid},
        results, path=os.path.join(OUT, "ea3_ppl_quality.json"))
    print(f"数据: {path}")


if __name__ == "__main__":
    main()
