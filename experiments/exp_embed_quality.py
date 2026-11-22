#!/usr/bin/env python3
"""exp_embed_quality.py: 嵌入文本质量量化（v0.15 质量通行证条目 8）。

回答"零感"到底零不零感——用可离线计算的客观代理指标，不依赖 LLM 评判：

指标 1 替换率：被改 token 数 / 词典命中 token 数（bias=1.0 下理论 ~50%）
指标 2 邻居兼容率：替换结果中，替换词的语料左/右邻居集包含实际邻居的
        比例（build_char_context 索引）——搭配自然度的客观代理。
        对比 随机选择 vs 搭配打分选择（choice_scorer，v0.15）。
指标 3 鲁棒性不回退：打分选择 vs 随机选择在 15% 同义攻击下的 UID
        还原率必须相当（质量提升不得以鲁棒性为代价）。

离线限制声明：无 LLM/困惑度模型可用，本报告指标是代理而非终判；
接 LLM 后的 PPL/人工评审为后续工作。

用法: python experiments/exp_embed_quality.py
"""
from __future__ import annotations

import glob
import json
import os
import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import build_zero_cost_zh_codec
from aawm.collocation import build_char_context
from aawm.plugins import Watermarker

KEY = bytes(range(32))
SEED = 20260911


def demo_texts():
    base = "src/aawm/data/demo_corpus"
    return [open(f, encoding="utf-8").read()
            for f in sorted(glob.glob(os.path.join(base, "*.md")))]


def make_scorer(corpus, codec):
    words = set(codec._raw_word_owner)
    left, right = build_char_context(corpus, words)

    def scorer(prev_char, choice, next_char):
        s = 0.0
        if not prev_char or prev_char in left.get(choice, ()):
            s += 0.5
        if not next_char or next_char in right.get(choice, ()):
            s += 0.5
        return s
    return scorer, left, right


def compat_rate(text, codec, left, right):
    """替换词邻居兼容率：逐词检查（仅词典词，且该词在语料有索引）。"""
    ok = known = 0
    for raw, norm in codec._tokenizer(text):
        if norm is None or norm not in codec._raw_word_owner:
            continue
        idx = text.find(raw)
        if left.get(norm) is None and right.get(norm) is None:
            continue  # 语料未索引，不计
        known += 1
        i = text.find(raw)
        p = text[i - 1] if i > 0 else ""
        nxt = text[i + len(raw)] if i + len(raw) < len(text) else ""
        l_ok = (not p) or (p in left.get(norm, ()))
        r_ok = (not nxt) or (nxt in right.get(norm, ()))
        if l_ok and r_ok:
            ok += 1
    return (ok / known) if known else None, known


def substitution_rate(orig, marked, codec):
    to, tm = codec._tokenizer(orig), codec._tokenizer(marked)
    hits = changed = 0
    for (r0, n0), (r1, n1) in zip(to, tm):
        if n0 is not None and n0 in codec._raw_word_owner:
            hits += 1
            if r0 != r1:
                changed += 1
    return (changed, hits)


def synonym_attack(text, codec, frac, rng):
    """组内随机替换 frac 比例的词典词（对齐 exp_real_corpus 的攻击模型）。"""
    parts = codec._tokenizer(text)
    out = []
    for raw, norm in parts:
        if (norm is not None and norm in codec._w2group
                and rng.random() < frac):
            pool = [w for w in codec._w2group[norm] if w != norm]
            if pool:
                out.append(rng.choice(pool))
                continue
        out.append(raw)
    return "".join(out)


def main():
    corpus = demo_texts()
    texts = corpus  # 5 篇长文
    codec = build_zero_cost_zh_codec(KEY, generate_salt())

    results = {}
    for mode in ("random", "scored"):
        wm = Watermarker(master_key=KEY, codec_mode="zero_cost",
                         calibrate_corpus=corpus)
        scorer = wm._choice_scorer(b"zh") if mode == "scored" else None
        sub_changed = sub_hits = 0
        compat_vals = []
        recover = attacked_n = 0
        for i, t in enumerate(texts):
            rng = random.Random(SEED + i)
            uid = 7
            marked, bands = codec.embed_adaptive(
                t, uid, rng=rng, choice_scorer=scorer)
            c, h = substitution_rate(t, marked, codec)
            sub_changed += c
            sub_hits += h
            _, left, right = make_scorer(corpus, codec)
            cr, _ = compat_rate(marked, codec, left, right)
            if cr is not None:
                compat_vals.append(cr)
            # 鲁棒性对照：15% 同义攻击后 UID 还原
            atk = synonym_attack(marked, codec, 0.15, random.Random(SEED + i))
            dec, _, _ = codec.detect_adaptive(atk, bands, min_n=1)
            attacked_n += 1
            if dec == (uid & ((1 << len(bands)) - 1)):
                recover += 1
        results[mode] = {
            "substitution_rate": round(sub_changed / max(1, sub_hits), 4),
            "neighbor_compat_rate": round(sum(compat_vals) / len(compat_vals), 4)
            if compat_vals else None,
            "uid_recovery_under_15pct_attack": f"{recover}/{attacked_n}",
        }
        print(f"[{mode}] {results[mode]}")

    with open("experiments/embed_quality_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print()
    print("结果已写入 experiments/embed_quality_result.json")


def generate_salt():
    from aawm.keys import generate_session_salt
    return generate_session_salt()


if __name__ == "__main__":
    main()
