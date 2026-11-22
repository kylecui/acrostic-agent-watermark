#!/usr/bin/env python3
"""paper_ea2b_default_dict.py: E-A2b——default 全词林词典下的改写攻击复测。

E-A2（zero_cost）全灭；本实验检验 default 词典（2363+ 组）是否更抗
真实改写。范围声明：default 词典扰动更大（病句率高），这是质量-鲁棒
权衡的实测点，结果只作论文边界讨论，不进核心 claim。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

from aawm.plugins import Watermarker

OUT = os.path.join(pc._REPO_ROOT, "papers", "paperA", "data")


def main() -> None:
    texts = []
    for line in open(os.path.join(OUT, "llm_corpus_zh.jsonl"), encoding="utf-8"):
        d = json.loads(line)
        if len(d["text"]) >= 800:
            texts.append(d["text"])
        if len(texts) >= 60:
            break
    half = 30
    wm = Watermarker(master_key=bytes(range(32)), codec_mode="default")
    from aawm.plugins.registry import UIDRegistry
    reg = UIDRegistry()
    reg.register("user-7", uid=7)
    wm._registry = reg

    # 无攻击基线
    tp = attr = n = 0
    for t in texts[half:]:
        emb = wm.embed(t, user_id=7, language="zh")
        r = wm.trace(emb.watermarked_text, session_salt=emb.session_salt,
                     language="zh")
        n += 1
        if r.watermarked:
            tp += 1
        if r.user == "user-7":
            attr += 1
    results = {"baseline_no_attack": {"tpr": round(tp / n, 4),
                                      "attr": round(attr / n, 4), "n": n}}
    print(results["baseline_no_attack"])
    path = pc.save_result(
        "ea2b_default_dict_baseline", 20260911,
        {"n_texts": n, "codec": "default full lexicon"},
        results, path=os.path.join(OUT, "ea2b_default_dict_baseline.json"))
    print(f"数据: {path}")


if __name__ == "__main__":
    main()
