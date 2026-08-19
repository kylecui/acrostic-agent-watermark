#!/usr/bin/env python3
"""exp_dict_expansion.py: 词典扩容对水印鲁棒性的影响（方向 1 落地实验）。

背景（design §13.11 结论）：16 band 全信息位 + 掩码 + soft_match 已是
最优编码结构；突破 PKU 重度改写边界的唯一途径是扩大词典覆盖
（提高 n_dict_words）——本实验验证该结论。

对比配置：
  ZH: D0 生产小词典(260 组) | D1 词林'='(6.6k 组) | D2 词林'='+'#'(含近义)
  EN: E0 生产词典(677 组)   | E1 WordNet 单词组(23.2k 组)

指标：
  1. 真实语料 n_dict_words（信号容量）
  2. 攻击谱匹配率（A1 硬 n>=1 / B1 soft n>=1，与 exp_soft_match 同口径）
  3. 存在性分离度：null vs marked 的 Σ|z| 间隔（扩容不能牺牲存在性）

语料：ZH 用 PAWS 正例拼接（同 exp_soft_match）；EN 用 Gutenberg 三书。
"""
from __future__ import annotations

import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from aawm.synonym_data import (
    EN_SYNONYMS_EXTRA,
    EN_SYNONYMS_RAW,
    ZH_SYNONYMS_RAW,
)
from dict_build import build_cilin_dict, build_wordnet_dict
from exp_real_corpus import synonym_attack
from exp_paws_attack import (
    KEY, SALT, N_SENT, N_DOCS,
    build_codec, load_paws_positive, paraphrase_style_attack,
)

N_TEST = N_DOCS // 2
TM_PAWS = dict(del_p=0.141, grp_sub_p=0.0214)
TM_PKU = dict(del_p=0.505, grp_sub_p=0.0284)


def hard_match(codec, text, candidates, min_n=1):
    rep = codec.detect(text, min_n=min_n)
    active = sum((1 << st.band) for st in rep.bands if st.has_signal)
    uid, best_d = None, None
    for c in candidates:
        d = bin((rep.uid ^ c) & active).count("1")
        if best_d is None or d < best_d:
            uid, best_d = c, d
    return uid


def run_spectrum(codec, test_docs, true_uids, candidates, attacks):
    """attacks: {tag: fn(marked, i) -> attacked_text}"""
    rows = {}
    for tag, fn in attacks.items():
        ok_hard = ok_soft = 0
        for i, doc in enumerate(test_docs):
            marked = codec.embed(doc, true_uids[i], bias=1.0,
                                 rng=random.Random(i))
            t = fn(marked, i)
            if hard_match(codec, t, candidates, 1) == true_uids[i]:
                ok_hard += 1
            if codec.soft_match(t, candidates, min_n=1, margin=0.0)[0] == true_uids[i]:
                ok_soft += 1
        rows[tag] = (ok_hard, ok_soft)
    return rows


def separation(codec, null_docs, marked_docs):
    """null vs marked 的 Σ|z| 分离度（存在性）。"""
    nulls = [codec.detect(d).existence_score for d in null_docs]
    marks = [codec.detect(d).existence_score for d in marked_docs]
    return min(marks) - max(nulls), sum(nulls) / len(nulls), sum(marks) / len(marks)


def load_en_docs(n_docs=30, words_per_doc=900):
    """Gutenberg 三本书切 900 词文档。"""
    import glob
    import os
    texts = []
    for p in sorted(glob.glob("corpus/en/pg*.txt")):
        with open(p, encoding="utf-8", errors="ignore") as f:
            body = f.read()
        # 去 Gutenberg 头尾（*** START/END 标记）
        if "*** START" in body:
            body = body.split("*** START", 1)[1]
        if "*** END" in body:
            body = body.split("*** END", 1)[0]
        toks = body.split()
        texts.append(toks)
    docs, idx = [], [0] * len(texts)
    for _ in range(n_docs):
        src = len(docs) % len(texts)
        i0 = idx[src]
        chunk = texts[src][i0:i0 + words_per_doc]
        idx[src] = i0 + words_per_doc
        docs.append(" ".join(chunk))
    return docs


def zh_section():
    print("=" * 72)
    print("ZH：PAWS 真实语料攻击谱（30 篇，与 exp_soft_match 同口径）")
    print("=" * 72)
    paws = load_paws_positive()
    base = GreenlistCodec(KEY, SALT, language_tag=b"zh")

    def n_dict(s):
        return sum(1 for _, n in base._tokenizer(s) if n and n in base._w2group)

    kept = [p for p in paws if n_dict(p[0]) >= 2]
    rng = random.Random(7)
    rng.shuffle(kept)
    docs = [" ".join(kept[i * N_SENT:(i + 1) * N_SENT][j][0] for j in range(N_SENT)) + " "
            for i in range(N_DOCS)]
    test_docs = docs[:N_TEST]
    null_docs = docs[N_TEST:]
    true_uids = [(0x1000 + i * 0x0111) & 0xFFFF for i in range(N_TEST)]
    candidates = sorted(set(range(1, 33)) | set(true_uids))

    raw_equal = build_cilin_dict("corpus/dict/cilin_extended.txt")
    raw_near = build_cilin_dict("corpus/dict/cilin_extended.txt", include_near=True)
    # 合并：生产策划组优先（语义纯度高、组大），词林 '=' 只补新词
    merged = dict(ZH_SYNONYMS_RAW)
    used = {w for ws in ZH_SYNONYMS_RAW.values() for w in ws}
    for k, ws in raw_equal.items():
        ws2 = [w for w in ws if w not in used]
        if len(ws2) >= 2:
            merged[k] = ws2
            used.update(ws2)

    def raw_codec(raw):
        c = GreenlistCodec(KEY, SALT, dictionary=raw, language_tag=b"zh")
        c.calibrate_p0(docs[N_TEST:])
        return c

    configs = [
        ("D0 生产(260组)", ZH_SYNONYMS_RAW, None),
        ("D1r 词林=原始", raw_equal, None),
        ("D2r 词林=+#原始", raw_near, None),
        ("D3r 生产∪词林=", merged, None),
    ]
    tm_paws, tm_pku = TM_PAWS, TM_PKU
    for name, raw, mode in configs:
        if mode == "filtered":
            codec = build_codec(base, docs, raw)
        else:
            codec = raw_codec(raw)
        nd = [sum(1 for _, n in codec._tokenizer(d) if n and n in codec._w2group)
              for d in test_docs]

        def atk(tag, marked, i):
            if tag == "rt":
                return marked
            if tag == "paws":
                return paraphrase_style_attack(codec, marked, 200 + i,
                                               tm_paws["del_p"], tm_paws["grp_sub_p"])
            if tag == "pku":
                return paraphrase_style_attack(codec, marked, 300 + i,
                                               tm_pku["del_p"], tm_pku["grp_sub_p"])
            if tag == "s30":
                return synonym_attack(codec, marked, 0.30, 100 + i)[0]
            if tag == "s50":
                return synonym_attack(codec, marked, 0.50, 100 + i)[0]

        attacks = {tag: (lambda mk, i, tag=tag: atk(tag, mk, i))
                   for tag in ("rt", "paws", "s30", "s50", "pku")}
        rows = run_spectrum(codec, test_docs, true_uids, candidates, attacks)

        marked_texts = [codec.embed(d, true_uids[i], bias=1.0, rng=random.Random(i))
                        for i, d in enumerate(test_docs[:5])]
        gap, null_m, mark_m = separation(codec, null_docs[:8], marked_texts)

        nw = len({w for ws in codec._groups.values() for w in ws})
        print(f"\n[{name}] 词典组={len(codec._groups)} 词条={nw} "
              f"n_dict/篇 均值={sum(nd)/len(nd):.1f} min={min(nd)}")
        print(f"  存在性: null均值={null_m:.1f} marked均值={mark_m:.1f} "
              f"最小间隔={gap:+.1f}")
        print(f"  {'攻击':5s} | {'A1 硬':>7s} | {'B1 soft':>7s}")
        for tag in ("rt", "paws", "s30", "s50", "pku"):
            h, s = rows[tag]
            print(f"  {tag:5s} | {h:2d}/{N_TEST:<4d} | {s:2d}/{N_TEST:<4d}")


def en_section():
    print("\n" + "=" * 72)
    print("EN：Gutenberg 真实语料攻击谱（30 篇，synonym_attack 模拟改写）")
    print("=" * 72)
    docs = load_en_docs(N_DOCS)
    test_docs, null_docs = docs[:N_TEST], docs[N_TEST:]
    true_uids = [(0x1000 + i * 0x0111) & 0xFFFF for i in range(N_TEST)]
    candidates = sorted(set(range(1, 33)) | set(true_uids))

    wn = build_wordnet_dict(single_word_only=True)
    wn3 = {k: ws for k, ws in wn.items() if len(ws) >= 3}
    prod = {**EN_SYNONYMS_RAW, **EN_SYNONYMS_EXTRA}
    # 合并：生产组优先（手工策划、组大），WordNet≥3 只补新词
    merged = dict(prod)
    used = {w for ws in prod.values() for w in ws}
    for k, ws in wn3.items():
        ws2 = [w for w in ws if w not in used]
        if len(ws2) >= 3:
            merged[k] = ws2
            used.update(ws2)
    configs = [
        ("E0 生产(677组)", prod),
        ("E1 WordNet(23k组)", wn),
        ("E2 WordNet>=3(9.4k)", wn3),
        ("E3 生产∪WN>=3", merged),
    ]
    for name, raw in configs:
        codec = GreenlistCodec(KEY, SALT, dictionary=raw, language_tag=b"en")
        codec.calibrate_p0(null_docs)
        nd = [sum(1 for _, n in codec._tokenizer(d) if n and n in codec._w2group)
              for d in test_docs]

        def atk(tag, marked, i):
            if tag == "rt":
                return marked
            return synonym_attack(codec, marked, 0.30 if tag == "s30" else 0.50,
                                  100 + i)[0]

        attacks = {tag: (lambda mk, i, tag=tag: atk(tag, mk, i))
                   for tag in ("rt", "s30", "s50")}
        rows = run_spectrum(codec, test_docs, true_uids, candidates, attacks)

        marked_texts = [codec.embed(d, true_uids[i], bias=1.0, rng=random.Random(i))
                        for i, d in enumerate(test_docs[:5])]
        gap, null_m, mark_m = separation(codec, null_docs[:8], marked_texts)

        nw = len({w for ws in codec._groups.values() for w in ws})
        print(f"\n[{name}] 词典组={len(codec._groups)} 词条={nw} "
              f"n_dict/篇 均值={sum(nd)/len(nd):.1f} min={min(nd)}")
        print(f"  存在性: null均值={null_m:.1f} marked均值={mark_m:.1f} "
              f"最小间隔={gap:+.1f}")
        print(f"  {'攻击':5s} | {'A1 硬':>7s} | {'B1 soft':>7s}")
        for tag in ("rt", "s30", "s50"):
            h, s = rows[tag]
            print(f"  {tag:5s} | {h:2d}/{N_TEST:<4d} | {s:2d}/{N_TEST:<4d}")


if __name__ == "__main__":
    zh_section()
    en_section()
