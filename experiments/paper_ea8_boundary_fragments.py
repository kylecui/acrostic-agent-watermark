#!/usr/bin/env python3
"""paper_ea8_boundary_fragments.py: E-A8——跨词界碎片率实测（v0.15.2 守卫）。

在 E-A1 zh 语料上统计：词典组命中位点中被碎片守卫拦下的比例
（=v0.15.2 之前会变成病句的替换占比）。供论文 A quality 节引用。

可复现: python experiments/paper_ea8_boundary_fragments.py
产出: papers/paperA/data/ea8_boundary_fragments.json
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

from aawm.greenlist import build_zero_cost_zh_codec
from aawm.keys import derive_session_salt

KEY = bytes(range(32))


def main() -> None:
    texts = []
    for line in open(os.path.join(pc._REPO_ROOT, "papers", "paperA", "data",
                              "llm_corpus_zh.jsonl"), encoding="utf-8"):
        d = json.loads(line)
        if len(d["text"]) >= 800:
            texts.append(d["text"])
        if len(texts) >= 100:
            break

    total_hits = guarded = 0
    examples = []
    for t in texts:
        salt = derive_session_salt(KEY, t, nonce=0)
        codec = build_zero_cost_zh_codec(KEY, salt)
        blx = codec._boundary_lexicon()
        parts = codec._tokenizer(t)
        for i, (raw, norm) in enumerate(parts):
            if norm is None or norm not in codec._w2group:
                continue
            total_hits += 1
            last_char = parts[i - 1][0][-1] if i > 0 and parts[i - 1][0] else ""
            next_char = parts[i + 1][0][:1] if i + 1 < len(parts) else ""
            if ((next_char and (raw[-1] + next_char) in blx)
                    or (last_char and (last_char + raw[0]) in blx)):
                guarded += 1
                if len(examples) < 5:
                    examples.append({"raw": raw, "context":
                                     (last_char + "[" + raw + "]" + next_char)})

    results = {"n_texts": len(texts), "group_hits": total_hits,
               "guarded": guarded,
               "fragment_rate": round(guarded / max(1, total_hits), 4),
               "examples": examples}
    print(json.dumps(results, ensure_ascii=False, indent=1))
    path = pc.save_result(
        "ea8_boundary_fragments", 20260911,
        {"n_texts": len(texts), "codec": "zero_cost zh, guard v0.15.2"},
        results, path=os.path.join(pc._REPO_ROOT, "papers", "paperA", "data",
                                   "ea8_boundary_fragments.json"))
    print(f"数据: {path}")


if __name__ == "__main__":
    main()
