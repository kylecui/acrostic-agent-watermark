#!/usr/bin/env python3
"""exp_ai_text_deep.py: AI 文本判别能力深度分析。

针对 exp_ai_text.py 的初步结果做三个深度对照：

1. **密度归一化对比**：AI 文本词典命中密度远高于文学（EN: 437 vs 68），
   改写攻击按"词典词比例"换算，但绝对换词数 AI >> 文学。
   对比"相同绝对换词数"下的汉明距。

2. **UID 注册库匹配**：改写后逐位解码汉明距高，但 UID 注册库最近邻
   匹配（≤2 bit 容错）能否还原？这是部署策略的关键。

3. **短文本边界**：AI 输出常是短回答（100-200词）。测试不同长度下的
   判别能力下界。
"""
from __future__ import annotations

import json
import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from exp_ai_text import (
    make_ai_en_text, make_ai_zh_text, synonym_attack,
)

KEY = bytes(range(32))
SALT = b"ai-text-deep-2026"


def test_uid_registry_match(codec, text, uid, max_hamming=2):
    """模拟 UID 注册库匹配：解出 UID 后查库，容许 ≤max_hamming 的误差。

    部署场景：已知所有合法 UID（≤65536），检测时解出 UID 后在注册库中
    找汉明距 ≤max_hamming 的条目。若唯一匹配则溯源成功。
    """
    rep = codec.detect(text)
    dist, active = codec.masked_hamming(text, uid)
    # 模拟注册库匹配：距离 ≤max_hamming 视为命中
    return dist <= max_hamming, dist, active


def run_density_normalized():
    """实验 1: 密度归一化对比。

    AI 文本词典密度高，30% 改写换的绝对词数远多于文学。
    在相同绝对换词数下对比汉明距。
    """
    print("\n" + "=" * 70)
    print("深度实验 1: 密度归一化 — 相同绝对换词数下的汉明距")
    print("=" * 70)

    en_codec = GreenlistCodec(KEY, SALT, language_tag=b"en")
    zh_codec = GreenlistCodec(KEY, SALT, language_tag=b"zh")

    # 生成文本
    en_docs = [make_ai_en_text(200, seed=i) for i in range(500, 510)]
    zh_docs = [make_ai_zh_text(30, seed=i) for i in range(600, 610)]

    en_codec.calibrate_p0(en_docs[5:])
    zh_codec.calibrate_p0(zh_docs[5:])

    # 绝对换词数梯度
    en_results = {n: [] for n in [5, 10, 20, 30, 50]}
    zh_results = {n: [] for n in [5, 10, 20, 30]}

    for i, doc in enumerate(en_docs[:5]):
        uid = (0x5000 + i) & 0xFFFF
        marked = en_codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
        n_dict = sum(1 for _, n in en_codec._tokenizer(marked) if n is not None)
        for abs_n in en_results:
            frac = abs_n / max(n_dict, 1)
            rw, changed = synonym_attack(en_codec, marked, frac, 700 + i)
            d, a = en_codec.masked_hamming(rw, uid)
            en_results[abs_n].append((d, a, changed))

    for i, doc in enumerate(zh_docs[:5]):
        uid = (0x6000 + i) & 0xFFFF
        marked = zh_codec.embed(doc, uid, bias=1.0, rng=random.Random(i + 200))
        n_dict = sum(1 for _, n in zh_codec._tokenizer(marked) if n is not None)
        for abs_n in zh_results:
            frac = abs_n / max(n_dict, 1)
            rw, changed = synonym_attack(zh_codec, marked, frac, 800 + i)
            d, a = zh_codec.masked_hamming(rw, uid)
            zh_results[abs_n].append((d, a, changed))

    print("\n英文 AI 文本（绝对换词数梯度）:")
    print(f"{'换词数':>8} {'实际换':>6} {'汉明距':>8} {'≤2占比':>8} {'注册库匹配':>10}")
    for abs_n, bucket in en_results.items():
        d_mean = sum(d for d, _, _ in bucket) / len(bucket)
        le2 = sum(1 for d, _, _ in bucket if d <= 2) / len(bucket)
        reg = sum(1 for d, _, _ in bucket if d <= 2) / len(bucket)
        changed_mean = sum(c for _, _, c in bucket) / len(bucket)
        print(f"{abs_n:>8} {changed_mean:>6.1f} {d_mean:>8.2f} {le2*100:>7.0f}% {reg*100:>9.0f}%")

    print("\n中文 AI 文本（绝对换词数梯度）:")
    print(f"{'换词数':>8} {'实际换':>6} {'汉明距':>8} {'≤2占比':>8} {'注册库匹配':>10}")
    for abs_n, bucket in zh_results.items():
        d_mean = sum(d for d, _, _ in bucket) / len(bucket)
        le2 = sum(1 for d, _, _ in bucket if d <= 2) / len(bucket)
        changed_mean = sum(c for _, _, c in bucket) / len(bucket)
        print(f"{abs_n:>8} {changed_mean:>6.1f} {d_mean:>8.2f} {le2*100:>7.0f}% {le2*100:>9.0f}%")

    return en_results, zh_results


def run_uid_registry():
    """实验 2: UID 注册库最近邻匹配还原率。

    改写后逐位解码有误差，但注册库匹配容许 ≤2 bit 容错。
    测试不同改写强度下的注册库匹配还原率。
    """
    print("\n" + "=" * 70)
    print("深度实验 2: UID 注册库最近邻匹配还原率")
    print("=" * 70)

    en_codec = GreenlistCodec(KEY, SALT, language_tag=b"en")
    zh_codec = GreenlistCodec(KEY, SALT, language_tag=b"zh")

    en_docs = [make_ai_en_text(200, seed=i) for i in range(700, 720)]
    zh_docs = [make_ai_zh_text(30, seed=i) for i in range(800, 820)]

    en_codec.calibrate_p0(en_docs[15:])
    zh_codec.calibrate_p0(zh_docs[15:])

    fracs = [0.0, 0.15, 0.30, 0.45, 0.60]
    en_reg = {f: {"exact": 0, "le1": 0, "le2": 0, "le3": 0, "total": 0} for f in fracs}
    zh_reg = {f: {"exact": 0, "le1": 0, "le2": 0, "le3": 0, "total": 0} for f in fracs}

    for i, doc in enumerate(en_docs[:15]):
        uid = (0x7000 + i * 0x0101) & 0xFFFF
        marked = en_codec.embed(doc, uid, bias=1.0, rng=random.Random(i + 300))
        for frac in fracs:
            rw, _ = synonym_attack(en_codec, marked, frac, 900 + i)
            d, a = en_codec.masked_hamming(rw, uid)
            en_reg[frac]["total"] += 1
            if d == 0: en_reg[frac]["exact"] += 1
            if d <= 1: en_reg[frac]["le1"] += 1
            if d <= 2: en_reg[frac]["le2"] += 1
            if d <= 3: en_reg[frac]["le3"] += 1

    for i, doc in enumerate(zh_docs[:15]):
        uid = (0x8000 + i * 0x0101) & 0xFFFF
        marked = zh_codec.embed(doc, uid, bias=1.0, rng=random.Random(i + 400))
        for frac in fracs:
            rw, _ = synonym_attack(zh_codec, marked, frac, 1000 + i)
            d, a = zh_codec.masked_hamming(rw, uid)
            zh_reg[frac]["total"] += 1
            if d == 0: zh_reg[frac]["exact"] += 1
            if d <= 1: zh_reg[frac]["le1"] += 1
            if d <= 2: zh_reg[frac]["le2"] += 1
            if d <= 3: zh_reg[frac]["le3"] += 1

    print("\n英文 AI 文本 — UID 注册库匹配还原率:")
    print(f"{'改写%':>7} {'精确':>8} {'≤1bit':>8} {'≤2bit':>8} {'≤3bit':>8}")
    for frac in fracs:
        r = en_reg[frac]
        t = r["total"]
        print(f"{int(frac*100):>6}% {r['exact']/t*100:>7.0f}% {r['le1']/t*100:>7.0f}% "
              f"{r['le2']/t*100:>7.0f}% {r['le3']/t*100:>7.0f}%")

    print("\n中文 AI 文本 — UID 注册库匹配还原率:")
    print(f"{'改写%':>7} {'精确':>8} {'≤1bit':>8} {'≤2bit':>8} {'≤3bit':>8}")
    for frac in fracs:
        r = zh_reg[frac]
        t = r["total"]
        print(f"{int(frac*100):>6}% {r['exact']/t*100:>7.0f}% {r['le1']/t*100:>7.0f}% "
              f"{r['le2']/t*100:>7.0f}% {r['le3']/t*100:>7.0f}%")

    return en_reg, zh_reg


def run_length_boundary():
    """实验 3: 短文本判别边界。

    AI 输出常是短回答。测试不同长度下的判别能力下界。
    """
    print("\n" + "=" * 70)
    print("深度实验 3: 文本长度对判别能力的影响")
    print("=" * 70)

    en_codec = GreenlistCodec(KEY, SALT, language_tag=b"en")
    zh_codec = GreenlistCodec(KEY, SALT, language_tag=b"zh")

    # 标定用大量文本
    en_cal = [make_ai_en_text(200, seed=i) for i in range(900, 930)]
    zh_cal = [make_ai_zh_text(30, seed=i) for i in range(1000, 1030)]
    en_codec.calibrate_p0(en_cal)
    zh_codec.calibrate_p0(zh_cal)

    # 不同目标词数（模拟不同长度 AI 输出）
    en_targets = [50, 100, 150, 200, 300]
    zh_targets = [10, 20, 30, 50]

    print("\n英文 AI 文本 — 长度 vs 判别能力:")
    print(f"{'目标词':>8} {'实际词典词':>10} {'往返精确':>8} {'≤1bit':>8} {'≤2bit':>8} {'Σ|z|标记':>10} {'Σ|z|null':>10}")
    for target in en_targets:
        docs = [make_ai_en_text(target, seed=1100 + i) for i in range(10)]
        marked_scores, null_scores = [], []
        exact, le1, le2 = 0, 0, 0
        dict_words = []
        for i, doc in enumerate(docs):
            uid = (0x9000 + i) & 0xFFFF
            marked = en_codec.embed(doc, uid, bias=1.0, rng=random.Random(i + 500))
            rep = en_codec.detect(marked)
            d, a = en_codec.masked_hamming(marked, uid)
            marked_scores.append(rep.existence_score)
            null_scores.append(en_codec.detect(doc).existence_score)
            dict_words.append(rep.n_dict_words)
            if d == 0: exact += 1
            if d <= 1: le1 += 1
            if d <= 2: le2 += 1
        n = len(docs)
        print(f"{target:>8} {sum(dict_words)/n:>10.0f} {exact}/{n:>3} {le1/n*100:>7.0f}% "
              f"{le2/n*100:>7.0f}% {sum(marked_scores)/n:>10.1f} {sum(null_scores)/n:>10.1f}")

    print("\n中文 AI 文本 — 长度 vs 判别能力:")
    print(f"{'目标slot':>8} {'实际词典词':>10} {'往返精确':>8} {'≤1bit':>8} {'≤2bit':>8} {'Σ|z|标记':>10} {'Σ|z|null':>10}")
    for target in zh_targets:
        docs = [make_ai_zh_text(target, seed=1200 + i) for i in range(10)]
        marked_scores, null_scores = [], []
        exact, le1, le2 = 0, 0, 0
        dict_words = []
        for i, doc in enumerate(docs):
            uid = (0xA000 + i) & 0xFFFF
            marked = zh_codec.embed(doc, uid, bias=1.0, rng=random.Random(i + 600))
            rep = zh_codec.detect(marked)
            d, a = zh_codec.masked_hamming(marked, uid)
            marked_scores.append(rep.existence_score)
            null_scores.append(zh_codec.detect(doc).existence_score)
            dict_words.append(rep.n_dict_words)
            if d == 0: exact += 1
            if d <= 1: le1 += 1
            if d <= 2: le2 += 1
        n = len(docs)
        print(f"{target:>8} {sum(dict_words)/n:>10.0f} {exact}/{n:>3} {le1/n*100:>7.0f}% "
              f"{le2/n*100:>7.0f}% {sum(marked_scores)/n:>10.1f} {sum(null_scores)/n:>10.1f}")


def main():
    results = {}
    en_dens, zh_dens = run_density_normalized()
    en_reg, zh_reg = run_uid_registry()
    run_length_boundary()
    print("\n深度实验完成。")


if __name__ == "__main__":
    main()
