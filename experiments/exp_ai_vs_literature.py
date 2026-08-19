#!/usr/bin/env python3
"""exp_ai_vs_literature.py: AI 文本 vs 文学语料判别能力直接对比。

exp_ai_text 发现 AI 文本改写鲁棒性远低于文学语料（英文 30% 改写 ≤1bit 占比
0% vs 文学 44%）。本实验做严格对照，找出根因。

假设：差异来自 UID 分布。exp_real_corpus 用 uid=(0x1000+i*0x0111)，
高 bit 位接近 0；exp_ai_text 也用类似分布。真正的变量是**文本词法分布**——
AI 文本的词典词集中在少数高频带，导致部分带样本量极低、部分带极高，
改写时高样本带翻转概率大。

对照设计：
  1. 同一 codec、同一 UID 生成策略、同一改写函数
  2. 文本来源：AI 生成 vs Gutenberg 文学，词数对齐
  3. 对比逐带样本量分布、改写后逐带 z 衰减
"""
from __future__ import annotations

import glob
import json
import random
import re
import sys
from collections import Counter

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

# 确保相对路径在项目根目录下解析
import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_PROJECT_ROOT)

from aawm.greenlist import GreenlistCodec
from exp_ai_text import make_ai_en_text, synonym_attack

KEY = bytes(range(32))
SALT = b"ai-vs-lit-2026"


def load_gutenberg_window(target_words=10000, skip=2000):
    """加载 Gutenberg 文学语料窗口。每本书取 target_words 词，切 600 词窗口。"""
    texts = []
    for f in sorted(glob.glob("corpus/en/*.txt")):
        raw = open(f, encoding="utf-8", errors="ignore").read()
        m = re.search(r"\*\*\* ?START OF.*?\*\*\*(.*?)\*\*\* ?END OF", raw, re.S)
        body = m.group(1) if m else raw
        words = re.sub(r"\s+", " ", body).strip().split()[skip:]
        texts.append(" ".join(words[:target_words]))
    # 切成 ~600 词文档
    lit_windows = []
    for doc in texts:
        words = doc.split()
        for i in range(0, len(words) - 600, 600):
            lit_windows.append(" ".join(words[i:i+600]))
    return lit_windows


def analyze_band_distribution(codec, text, label):
    """分析文本在各频带的样本量分布。"""
    rep = codec.detect(text)
    band_n = [st.n for st in rep.bands]
    print(f"\n[{label}] 逐带样本量分布:")
    print(f"  min={min(band_n)} max={max(band_n)} mean={sum(band_n)/len(band_n):.1f} "
          f"std={(sum((x-sum(band_n)/len(band_n))**2 for x in band_n)/len(band_n))**0.5:.1f}")
    print(f"  逐带: {band_n}")
    # 逐带 z 值
    band_z = [round(st.z, 1) for st in rep.bands]
    print(f"  逐带 z: {band_z}")
    print(f"  词典词总数: {rep.n_dict_words}")
    return band_n, band_z


def run_comparison():
    print("=" * 70)
    print("AI 文本 vs 文学语料 — 判别能力直接对比")
    print("=" * 70)

    codec = GreenlistCodec(KEY, SALT, language_tag=b"en")
    print(f"词典: {codec.stats}")

    # 1. 加载文学语料
    lit_windows = load_gutenberg_window(target_words=10000)
    random.Random(42).shuffle(lit_windows)
    lit_null = lit_windows[:15]
    lit_test = lit_windows[15:30]
    print(f"文学语料: {len(lit_windows)} 个 600 词窗口")

    # 2. 生成 AI 文本（对齐词数）
    ai_docs = [make_ai_en_text(200, seed=i) for i in range(200, 230)]
    ai_null = ai_docs[15:30]
    ai_test = ai_docs[:15]

    # 3. 合并标定（用两组 null 合并）
    codec.calibrate_p0(lit_null + ai_null)

    # 4. 逐带分布对比
    print("\n--- 逐带样本量分布对比 ---")
    lit_band = analyze_band_distribution(codec, lit_test[0], "文学")
    ai_band = analyze_band_distribution(codec, ai_test[0], "AI")

    # 5. 嵌入 + 改写对照
    print("\n--- 嵌入 + 改写对照 ---")
    for label, docs in [("文学", lit_test), ("AI", ai_test)]:
        rt = []
        ham30 = []
        sumz_m, sumz_n = [], []
        for i, doc in enumerate(docs):
            uid = (0xB000 + i * 0x0111) & 0xFFFF
            marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
            d, a = codec.masked_hamming(marked, uid)
            rt.append(d)
            sumz_m.append(codec.detect(marked).existence_score)
            sumz_n.append(codec.detect(doc).existence_score)
            # 30% 改写
            rw, _ = synonym_attack(codec, marked, 0.30, 500 + i)
            d30, _ = codec.masked_hamming(rw, uid)
            ham30.append(d30)

        n_exact = sum(1 for d in rt if d == 0)
        n_le1 = sum(1 for d in ham30 if d <= 1)
        n_le2 = sum(1 for d in ham30 if d <= 2)
        print(f"\n[{label}]")
        print(f"  往返精确: {n_exact}/{len(docs)}")
        print(f"  词典词/文档: {sum(codec.detect(d).n_dict_words for d in docs)/len(docs):.0f}")
        print(f"  Σ|z| 标记: {sum(sumz_m)/len(sumz_m):.1f}, null: {sum(sumz_n)/len(sumz_n):.1f}")
        print(f"  30%改写 汉明距均值: {sum(ham30)/len(ham30):.2f}")
        print(f"  30%改写 ≤1bit: {n_le1}/{len(docs)} ({n_le1/len(docs)*100:.0f}%)")
        print(f"  30%改写 ≤2bit: {n_le2}/{len(docs)} ({n_le2/len(docs)*100:.0f}%)")

    # 6. 关键诊断：逐带改写敏感度
    print("\n--- 关键诊断: 逐带改写敏感度（AI 文本）---")
    doc = ai_test[0]
    uid = 0xB000
    marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(0))
    rep_before = codec.detect(marked)
    rw, changed = synonym_attack(codec, marked, 0.30, 999)
    rep_after = codec.detect(rw)
    print(f"改写 {changed} 词后逐带 z 变化:")
    print(f"{'带':>4} {'n':>4} {'z前':>8} {'z后':>8} {'Δz':>8} {'翻转?':>6}")
    for before, after in zip(rep_before.bands, rep_after.bands):
        flipped = ((rep_before.uid >> before.band) & 1) != ((rep_after.uid >> after.band) & 1)
        print(f"{before.band:>4} {before.n:>4} {before.z:>8.1f} {after.z:>8.1f} "
              f"{after.z - before.z:>8.1f} {'✓' if flipped else '':>6}")
    print(f"UID 前: 0x{rep_before.uid:04X}, 后: 0x{rep_after.uid:04X}, "
          f"汉明距: {bin(rep_before.uid ^ rep_after.uid).count('1')}")


if __name__ == "__main__":
    run_comparison()
