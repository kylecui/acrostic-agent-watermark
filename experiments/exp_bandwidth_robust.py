#!/usr/bin/env python3
"""exp_bandwidth_robust.py: n_bands∈{16,12,8} 多 seed 稳健性 + EN 跨语言验证。

回答两个问题：
  1. n_bands=12 在 s50 的 24/30 提升是否稳健（跨攻击 seed）？
  2. EN 是否一致？（扩容后减带同样有利？）

对照口径与 exp_bandwidth_d3r 一致（soft_match 幅度积分）。

用法: python experiments/exp_bandwidth_robust.py
"""
from __future__ import annotations

import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from aawm.synonym_data import EN_SYNONYMS_EXTRA, EN_SYNONYMS_RAW, ZH_SYNONYMS_RAW
from dict_build import build_cilin_dict, build_wordnet_dict
from exp_paws_attack import KEY, SALT, N_SENT, N_DOCS, load_paws_positive
from exp_dict_expansion import load_en_docs
from exp_real_corpus import synonym_attack
from exp_weighted_detect import soft_match_w

N_TEST = N_DOCS // 2


def zh_setup():
    paws = load_paws_positive()
    base = GreenlistCodec(KEY, SALT, language_tag=b"zh")

    def n_dict(s):
        return sum(1 for _, n in base._tokenizer(s) if n and n in base._w2group)

    kept = [p for p in paws if n_dict(p[0]) >= 2]
    rng = random.Random(7)
    rng.shuffle(kept)
    docs = [" ".join(kept[i * N_SENT:(i + 1) * N_SENT][j][0] for j in range(N_SENT)) + " "
            for i in range(N_DOCS)]
    raw_equal = build_cilin_dict("corpus/dict/cilin_extended.txt")
    merged = dict(ZH_SYNONYMS_RAW)
    used = {w for ws in ZH_SYNONYMS_RAW.values() for w in ws}
    for k, ws in raw_equal.items():
        ws2 = [w for w in ws if w not in used]
        if len(ws2) >= 2:
            merged[k] = ws2
            used.update(ws2)
    return docs[:N_TEST], docs[N_TEST:], merged, b"zh"


def en_setup():
    docs = load_en_docs(N_DOCS)
    prod = {**EN_SYNONYMS_RAW, **EN_SYNONYMS_EXTRA}
    wn = build_wordnet_dict(single_word_only=True)
    wn3 = {k: ws for k, ws in wn.items() if len(ws) >= 3}
    merged = dict(prod)
    used = {w for ws in prod.values() for w in ws}
    for k, ws in wn3.items():
        ws2 = [w for w in ws if w not in used]
        if len(ws2) >= 3:
            merged[k] = ws2
            used.update(ws2)
    return docs[:N_TEST], docs[N_TEST:], merged, b"en"


def run_one(n_bands, test_docs, null_docs, raw, lang, seeds, fracs=(0.30, 0.50)):
    results = {f"s{int(f*100)}": {s: 0 for s in seeds} for f in fracs}
    for frac in fracs:
        for base in seeds:
            codec = GreenlistCodec(KEY, SALT, n_bands=n_bands,
                                   dictionary=raw, language_tag=lang)
            codec.calibrate_p0(null_docs)
            max_uid = 1 << n_bands
            true_uids = [(0x111 * (i + 1)) % max_uid for i in range(N_TEST)]
            candidates = sorted(set(range(1, min(33, max_uid))) | set(true_uids))
            ok = 0
            for i, doc in enumerate(test_docs):
                uid = true_uids[i]
                marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
                rw, _ = synonym_attack(codec, marked, frac, base + i)
                rep = codec.detect(rw, min_n=1)
                z = {st.band: st.z for st in rep.bands if st.has_signal}
                best, *_ = soft_match_w(z, candidates)
                if best == uid:
                    ok += 1
            results[f"s{int(frac*100)}"][base] = ok
    return results


def report(title, results, seeds):
    print(f"\n[{title}]")
    for tag, by_seed in results.items():
        mean = sum(by_seed.values()) / len(by_seed)
        vals = " ".join(f"{by_seed[s]:2d}" for s in seeds)
        print(f"  {tag}: {vals}  → 均值 {mean:.1f}/30")


if __name__ == "__main__":
    seeds = (100, 500, 900)
    for lang_name, setup, raw_name in (("ZH D3r", zh_setup, "策划∪词林="),
                                       ("EN E3", en_setup, "策划∪WN≥3")):
        test_docs, null_docs, merged, lang = setup()
        print("=" * 60)
        print(f"{lang_name} 词典 {raw_name}")
        for nb in (16, 12, 8):
            r = run_one(nb, test_docs, null_docs, merged, lang, seeds)
            report(f"n_bands={nb}", r, seeds)
