#!/usr/bin/env python3
"""exp_existence_detection.py: 存在性检测的鲁棒性。

核心问题：用户要判别"这段 AI 文本是否经 Agent 嵌入过水印"。
这不需要解出 UID，只需"有/无水印"的二分判决。

之前的实验聚焦 UID 解码（改写 30% 就崩），但存在性检测（Σ|z| 对比 null）
更鲁棒——因为即使逐位翻转，只要 z 幅值仍高于 null，就能判"有水印"。

本实验测存在性检测在不同改写强度下的 TPR/FPR。
"""
from __future__ import annotations

import json
import os
import random
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_PROJECT_ROOT)
sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from exp_ai_text import make_ai_en_text, make_ai_zh_text, synonym_attack

KEY = bytes(range(32))
SALT = b"existence-2026"


def run_existence_test():
    print("=" * 70)
    print("存在性检测鲁棒性测试（AI 文本）")
    print("=" * 70)

    # 准备 codec
    en_codec = GreenlistCodec(KEY, SALT, language_tag=b"en")
    zh_codec = GreenlistCodec(KEY, SALT, language_tag=b"zh")

    # 大量 null 文本标定
    en_null_cal = [make_ai_en_text(200, seed=i) for i in range(2000, 2060)]
    zh_null_cal = [make_ai_zh_text(30, seed=i) for i in range(3000, 3060)]
    en_codec.calibrate_p0(en_null_cal)
    zh_codec.calibrate_p0(zh_null_cal)

    # 生成测试集
    n_test = 50
    en_marked = []
    en_unmarked = []
    zh_marked = []
    zh_unmarked = []

    for i in range(n_test):
        # 有水印：嵌入后施改写
        doc = make_ai_en_text(200, seed=4000 + i)
        uid = (0xC000 + i) & 0xFFFF
        marked = en_codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
        en_marked.append(marked)
        en_unmarked.append(doc)

        docz = make_ai_zh_text(30, seed=5000 + i)
        uidz = (0xD000 + i) & 0xFFFF
        markedz = zh_codec.embed(docz, uidz, bias=1.0, rng=random.Random(i + 100))
        zh_marked.append(markedz)
        zh_unmarked.append(docz)

    # 各改写强度下的存在性得分
    fracs = [0.0, 0.15, 0.30, 0.45, 0.60, 0.75]

    print("\n--- 英文 AI 文本 ---")
    print(f"{'改写%':>7} {'水印文本Σ|z|':>14} {'无水印Σ|z|':>14} {'阈值':>8} {'TPR':>8} {'FPR':>8} {'分离度':>8}")
    en_results = {}
    for frac in fracs:
        m_scores = []
        u_scores = []
        for i in range(n_test):
            if frac == 0:
                m = en_marked[i]
            else:
                m, _ = synonym_attack(en_codec, en_marked[i], frac, 6000 + i)
            m_scores.append(en_codec.detect(m).existence_score)
            u_scores.append(en_codec.detect(en_unmarked[i]).existence_score)

        # 阈值取无水印分布的 95 分位
        u_sorted = sorted(u_scores)
        threshold = u_sorted[int(0.95 * len(u_sorted))]
        tp = sum(1 for s in m_scores if s > threshold)
        fp = sum(1 for s in u_scores if s > threshold)
        tpr = tp / len(m_scores)
        fpr = fp / len(u_scores)
        sep = (sum(m_scores)/len(m_scores)) - (sum(u_scores)/len(u_scores))
        print(f"{int(frac*100):>6}% {sum(m_scores)/len(m_scores):>14.1f} "
              f"{sum(u_scores)/len(u_scores):>14.1f} {threshold:>8.1f} "
              f"{tpr*100:>7.0f}% {fpr*100:>7.0f}% {sep:>8.1f}")
        en_results[frac] = {
            "marked_mean": round(sum(m_scores)/len(m_scores), 1),
            "unmarked_mean": round(sum(u_scores)/len(u_scores), 1),
            "threshold": round(threshold, 1),
            "tpr": round(tpr * 100, 1),
            "fpr": round(fpr * 100, 1),
            "separation": round(sep, 1),
        }

    print("\n--- 中文 AI 文本 ---")
    print(f"{'改写%':>7} {'水印文本Σ|z|':>14} {'无水印Σ|z|':>14} {'阈值':>8} {'TPR':>8} {'FPR':>8} {'分离度':>8}")
    zh_results = {}
    for frac in fracs:
        m_scores = []
        u_scores = []
        for i in range(n_test):
            if frac == 0:
                m = zh_marked[i]
            else:
                m, _ = synonym_attack(zh_codec, zh_marked[i], frac, 7000 + i)
            m_scores.append(zh_codec.detect(m).existence_score)
            u_scores.append(zh_codec.detect(zh_unmarked[i]).existence_score)

        u_sorted = sorted(u_scores)
        threshold = u_sorted[int(0.95 * len(u_sorted))]
        tp = sum(1 for s in m_scores if s > threshold)
        fp = sum(1 for s in u_scores if s > threshold)
        tpr = tp / len(m_scores)
        fpr = fp / len(u_scores)
        sep = (sum(m_scores)/len(m_scores)) - (sum(u_scores)/len(u_scores))
        print(f"{int(frac*100):>6}% {sum(m_scores)/len(m_scores):>14.1f} "
              f"{sum(u_scores)/len(u_scores):>14.1f} {threshold:>8.1f} "
              f"{tpr*100:>7.0f}% {fpr*100:>7.0f}% {sep:>8.1f}")
        zh_results[frac] = {
            "marked_mean": round(sum(m_scores)/len(m_scores), 1),
            "unmarked_mean": round(sum(u_scores)/len(u_scores), 1),
            "threshold": round(threshold, 1),
            "tpr": round(tpr * 100, 1),
            "fpr": round(fpr * 100, 1),
            "separation": round(sep, 1),
        }

    # 保存
    results = {"en": en_results, "zh": zh_results}
    with open("/tmp/existence_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n结果已写入 /tmp/existence_result.json")
    return results


if __name__ == "__main__":
    run_existence_test()
