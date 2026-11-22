#!/usr/bin/env python3
"""paper_ea5_human_eval_pool.py: E-A5——人工盲评测评材料生成。

从 E-A1 zh 语料嵌入并抽取"被改动的句子"作为盲评 item：
  - pair 项：原句 vs 水印句（二选一自然度）
  - spot 项：单句（原或水印，各半）问"有无人为改动"+ 置信度
  - likert 项：水印句 1-5 自然度
  - attention 项：2 个明显病句（插入重复词）做注意力检查
输出: papers/paperA/human_eval/item_pool.json（含分配方案）

可复现: python experiments/paper_ea5_human_eval_pool.py [--n-texts 80]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

from aawm.plugins import Watermarker

OUT = os.path.join(pc._REPO_ROOT, "papers", "paperA", "human_eval")
SENT_RE = re.compile(r"[^。！？!?]+[。！？!?]?")


def split_sents(t: str):
    return [s.strip() for s in SENT_RE.findall(t) if s.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-texts", type=int, default=80)
    args = ap.parse_args()

    texts = []
    for line in open(os.path.join(pc._REPO_ROOT, "papers", "paperA", "data",
                                  "llm_corpus_zh.jsonl"), encoding="utf-8"):
        d = json.loads(line)
        if len(d["text"]) >= 800:
            texts.append(d["text"])
        if len(texts) >= args.n_texts:
            break

    wm = Watermarker(master_key=bytes(range(32)), codec_mode="zero_cost")
    rng = random.Random(20260911)

    changed = []   # (orig_sent, marked_sent, n_char_diff)
    untouched = []  # 原句（spot 项的"无改动"填充用，取自同批文本）
    for t in texts:
        emb = wm.embed(t, user_id=7)
        so, sm = split_sents(t), split_sents(emb.watermarked_text)
        for a, b in zip(so, sm):
            if a != b and 12 <= len(a) <= 80 and 12 <= len(b) <= 80:
                changed.append({"orig": a, "marked": b})
            elif a == b and 12 <= len(a) <= 80 and rng.random() < 0.06:
                untouched.append({"orig": a, "marked": a})
    rng.shuffle(changed)
    rng.shuffle(untouched)

    # 分配（每项用不同句子，避免跨任务污染）
    pair_items = changed[:15]
    spot_marked = changed[15:35]
    spot_orig = untouched[:20]
    likert_items = changed[35:55]
    rest = changed[55:]

    pool = {
        "meta": {
            "generated_by": "paper_ea5_human_eval_pool.py",
            "seed": 20260911,
            "n_changed_sentences_total": len(changed),
            "pair_n": len(pair_items),
            "spot_marked_n": len(spot_marked),
            "spot_orig_n": len(spot_orig),
            "likert_n": len(likert_items),
            "usage": "pair=二选一自然度; spot=改动探测(含原句对照); "
                     "likert=1-5绝对自然度; attention见 survey_template",
        },
        "pair_items": pair_items,
        "spot_marked": spot_marked,
        "spot_orig": spot_orig,
        "likert_items": likert_items,
        "reserve": rest,
    }
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "item_pool.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=1)
    print(f"changed={len(changed)} untouched={len(untouched)}")
    print(f"pool: 15 pair / 20 spot-marked / 20 spot-orig / 20 likert")
    print(f"数据: {path}")


if __name__ == "__main__":
    main()
