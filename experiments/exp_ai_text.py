#!/usr/bin/env python3
"""exp_ai_text.py: AI 生成文本经 Agent 嵌入后的判别能力实验。

核心问题：用户真正要判别的是「AI 吐出来 → 经 Agent 一环加工后」的文本，
而非既有文学。本实验构造真实 AI 风格文本（基于 LLM 生成文体的词法特征），
测 AAWM 信道 B 在这类文本上的嵌入-判别往返与改写鲁棒性。

AI 生成文本的词法特征（WebFetch 调研归纳）：
  EN:
    1. 模板化句首（"It is important to note that..."）
    2. 模糊量化词高频（various/significant/numerous/essential/crucial）
    3. 过渡词堆叠（Furthermore/Moreover/Additionally）
    4. 被动+名词化（"It is..."、"There are..."）
    5. 通用抽象词集中（important/relevant/effective/approach/aspect/factor）
    6. 情态对冲（may/might/could potentially）
    7. 主语+is/are+形容词+名词短语固定句式
  ZH:
    1. "首先"、"其次"、"最后"的三段式
    2. "我们需要"、"我们应该"的排比
    3. "在...的过程中"、"通过...的方式"名词化
    4. "值得注意"、"不容忽视"模板
    5. 通用词集中（重要/关键/有效/相关/必要）
    6. "可以"、"应该"、"可能"情态高频
    7. "不仅...而且"、"既...又"关联套式

实验设计：
  - 对照组：既有文学语料（红楼梦/人民日报/Gutenberg）已测数据
  - 实验组：AI 风格文本（手工构造模板 + 词典内词填充，覆盖多文体）
  - 对比维度：词典命中密度、往返解码、改写鲁棒性、存在性得分分离度
  - 目标：给出"AI 文本经嵌入后能否判别"的明确结论
"""
from __future__ import annotations

import json
import random
import re
import sys
from collections import Counter

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from aawm.synonym_data import EN_SYNONYMS_RAW, EN_SYNONYMS_EXTRA, ZH_SYNONYMS_RAW

KEY = bytes(range(32))
SALT = b"ai-text-exp-2026"


# ===========================================================================
# AI 风格英文文本生成器
# ===========================================================================
# 基于 LLM 生成文体的真实句式，用词典内词填充可锚定位
# 文体覆盖：technical report / essay / email / summary / analysis

EN_AI_TEMPLATES = [
    # 模板化句首 + 名词化
    "It is important to note that the {adj} nature of the {n} requires careful consideration.",
    "It is worth mentioning that various {n} factors play a crucial role in this process.",
    "There are numerous {adj} aspects to consider when examining this issue.",
    "It is generally acknowledged that the {adj} {n} approach is essential for success.",
    # 过渡词堆叠
    "Furthermore, the {adj} {n} demonstrates significant potential in this context.",
    "Moreover, it is necessary to highlight the {adj} impact of this {n}.",
    "Additionally, the {n} in question shows {adj} characteristics that merit attention.",
    "Consequently, the {adj} {n} remains a vital component of the overall strategy.",
    # 通用抽象词 + 情态对冲
    "This {adj} {n} may potentially affect the outcome in various ways.",
    "The {adj} {n} could be considered a key factor in this analysis.",
    "It should be noted that the {n} presents both {adj} and challenging aspects.",
    "The {adj} nature of this {n} might influence the final decision.",
    # 主语+is/are+形容词+名词短语
    "The {n} is a significant factor that requires {adj} attention.",
    "This approach is crucial for understanding the {adj} dynamics at play.",
    "The {adj} {n} represents an important step forward in this field.",
    "A {adj} {n} framework is essential for effective implementation.",
    # 结论/总结模板
    "In conclusion, the {adj} {n} approach offers a valuable perspective on this matter.",
    "Overall, the {n} demonstrates {adj} qualities that justify further investigation.",
    "The evidence suggests that the {adj} {n} will continue to be relevant.",
    "These findings indicate that the {n} plays a substantial role in the process.",
]

# 词典内的形容词和名词（保证可锚定）
EN_AI_ADJ_POOL = [
    "important", "crucial", "significant", "essential", "vital",
    "common", "typical", "standard", "effective", "practical",
    "complex", "difficult", "challenging", "demanding",
    "new", "fresh", "modern", "novel", "current",
    "old", "prior", "previous", "former",
    "strong", "powerful", "robust", "weak", "fragile",
    "clear", "obvious", "evident", "apparent", "plain",
    "big", "large", "sizable", "small", "minor", "modest",
    "fast", "quick", "rapid", "slow", "gradual",
    "good", "solid", "decent", "bad", "poor", "weak",
    "easy", "simple", "straightforward", "hard", "tough",
    "beautiful", "lovely", "strange", "odd", "unusual",
    "safe", "secure", "dangerous", "risky",
    "rare", "scarce", "bright", "dark", "dim",
]

EN_AI_NOUN_POOL = [
    "approach", "method", "process", "system", "framework",
    "strategy", "analysis", "assessment", "evaluation", "review",
    "context", "perspective", "factor", "aspect", "element",
    "component", "feature", "characteristic", "quality", "property",
    "structure", "pattern", "trend", "development", "progress",
    "result", "outcome", "conclusion", "finding", "observation",
    "change", "shift", "transition", "transformation", "modification",
    "group", "number", "place", "office", "position",
    "target", "goal", "objective", "purpose", "function",
    "benefit", "advantage", "improvement", "enhancement", "refinement",
]


def make_ai_en_text(target_words: int, seed: int) -> str:
    """生成 AI 风格英文文本，保证 ≥ target_words 个词典命中词。"""
    r = random.Random(seed)
    sents = []
    dict_hit = 0
    while dict_hit < target_words:
        tmpl = r.choice(EN_AI_TEMPLATES)
        # 从词典池填词（确保落在可锚定词典内）
        adj = r.choice(EN_AI_ADJ_POOL)
        noun = r.choice(EN_AI_NOUN_POOL)
        # 双占位符模板填两次
        n_placeholders = tmpl.count("{")
        if n_placeholders >= 2:
            noun2 = r.choice(EN_AI_NOUN_POOL)
            sent = tmpl.format(adj=adj, n=noun2)
        else:
            sent = tmpl.format(adj=adj, n=noun)
        sents.append(sent)
        # 粗估词典命中数（实际由 codec 精确计）
        dict_hit += n_placeholders
    return " ".join(sents)


# ===========================================================================
# AI 风格中文文本生成器
# ===========================================================================

ZH_AI_TEMPLATES = [
    "首先，我们需要认识到{adj}的{w}在整个过程中扮演着重要角色。",
    "其次，{adj}的{w}可能会对最终结果产生{adj2}的影响。",
    "值得注意的{w}是，这一{w}具有{adj}的特征，需要进一步分析。",
    "通过{adj}的{w}方式，我们可以更好地理解这一{w}的{adj2}意义。",
    "在当前的{w}背景下，{adj}的{w}显得尤为{adj2}。",
    "不容忽视的是，{adj}的{w}往往与{adj2}的{w2}密切相关。",
    "从{w}的角度来看，这种{adj}的{w}具有{adj2}的价值。",
    "综合以上分析，{adj}的{w}在未来的{w2}中将继续发挥{adj2}作用。",
    "具体而言，该{w}展现出{adj}的特点，值得深入探讨。",
    "总的来说，{adj}的{w}为{adj2}的{w2}提供了重要的支撑。",
    "在实践中，{adj}的{w}往往需要结合{adj2}的{w2}来综合考虑。",
    "此外，我们还需要关注{w}在不同{w2}下的{adj}表现。",
]

ZH_AI_ADJ_POOL = [
    "重要", "关键", "主要", "核心", "基本",
    "复杂", "困难", "简单", "容易", "清楚",
    "强大", "有效", "实际", "现实", "具体",
    "必要", "紧迫", "长期", "全面", "深入",
    "积极", "稳定", "良好", "明显", "显著",
    "普遍", "常见", "特殊", "独特", "新颖",
    "传统", "现代", "先进", "成熟", "完善",
    "重要", "关键", "主要", "核心", "基本",  # 重复以提高命中率
]

ZH_AI_NOUN_POOL = [
    "方法", "过程", "系统", "框架", "结构",
    "策略", "分析", "评估", "结果", "结论",
    "方面", "因素", "元素", "特点", "特征",
    "模式", "趋势", "发展", "变化", "转变",
    "目标", "目的", "功能", "作用", "价值",
    "影响", "效果", "能力", "水平", "质量",
    "需求", "要求", "标准", "原则", "基础",
    "问题", "挑战", "机遇", "方向", "途径",
]


def make_ai_zh_text(target_slots: int, seed: int) -> str:
    """生成 AI 风格中文文本。"""
    r = random.Random(seed)
    sents = []
    slots_made = 0
    while slots_made < target_slots:
        tmpl = r.choice(ZH_AI_TEMPLATES)
        # 按占位符数量填词
        n_adj = tmpl.count("{adj}") + tmpl.count("{adj2}")
        n_n = tmpl.count("{w}") + tmpl.count("{w2}")
        for _ in range(tmpl.count("{adj}")):
            tmpl = tmpl.replace("{adj}", r.choice(ZH_AI_ADJ_POOL), 1)
        for _ in range(tmpl.count("{adj2}")):
            tmpl = tmpl.replace("{adj2}", r.choice(ZH_AI_ADJ_POOL), 1)
        for _ in range(tmpl.count("{w}")):
            tmpl = tmpl.replace("{w}", r.choice(ZH_AI_NOUN_POOL), 1)
        for _ in range(tmpl.count("{w2}")):
            tmpl = tmpl.replace("{w2}", r.choice(ZH_AI_NOUN_POOL), 1)
        sents.append(tmpl)
        slots_made += n_adj + n_n
    return "\n".join(sents)


# ===========================================================================
# 攻击：同义改写（复用 exp_real_corpus 的逻辑，简化版）
# ===========================================================================
def synonym_attack(codec: GreenlistCodec, text: str, frac: float, seed: int):
    r = random.Random(seed)
    toks = codec._tokenizer(text)
    n_dict = sum(1 for _, n in toks if n is not None)
    n_target = int(n_dict * frac)
    out, changed = [], 0
    for raw, norm in toks:
        grp = codec._w2group.get(norm) if norm else None
        if grp and changed < n_target and r.random() < frac + 0.35:
            alts = [x for x in grp if x != norm]
            if alts:
                out.append(r.choice(alts))
                changed += 1
            else:
                out.append(raw)
        else:
            out.append(raw)
    return "".join(out), changed


# ===========================================================================
# 主实验
# ===========================================================================
def run_experiment():
    results = {}

    # -----------------------------------------------------------------------
    # 英文 AI 文本
    # -----------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("实验 1: 英文 AI 生成文本")
    print("=" * 70)

    en_codec = GreenlistCodec(KEY, SALT, language_tag=b"en")
    print(f"英文词典: {en_codec.stats}")

    # 生成 AI 文本，null/test 互斥
    en_ai_docs = [make_ai_en_text(target_words=200, seed=100 + i) for i in range(30)]
    en_test = en_ai_docs[:15]
    en_null = en_ai_docs[15:30]

    # 标定 p0
    en_codec.calibrate_p0(en_null)

    # 逐文档往返
    rt_results = []
    attack_results = {0.3: [], 0.5: [], 0.7: []}
    sumz_marked, sumz_null = [], []
    dict_counts = []

    for i, doc in enumerate(en_test):
        uid = (0x1000 + i * 0x0111) & 0xFFFF
        marked = en_codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
        rep = en_codec.detect(marked)
        dist, active = en_codec.masked_hamming(marked, uid)
        rt_results.append((uid, rep.uid, dist, active))
        dict_counts.append(rep.n_dict_words)
        sumz_marked.append(rep.existence_score)
        sumz_null.append(en_codec.detect(doc).existence_score)

        for frac, bucket in attack_results.items():
            rw, _ = synonym_attack(en_codec, marked, frac, 200 + i)
            d, a = en_codec.masked_hamming(rw, uid)
            bucket.append((uid, d, a))

    n_exact = sum(1 for _, _, d, _ in rt_results if d == 0)
    print(f"\n[往返] masked 汉明=0: {n_exact}/{len(rt_results)} = {n_exact/len(rt_results)*100:.0f}%")
    print(f"[词典词/文档] 均值={sum(dict_counts)/len(dict_counts):.0f} "
          f"min={min(dict_counts)} max={max(dict_counts)}")
    print(f"[Σ|z|] 嵌入后={sum(sumz_marked)/len(sumz_marked):.1f} "
          f"vs null={sum(sumz_null)/len(sumz_null):.1f} "
          f"分离度={sum(sumz_marked)/len(sumz_marked) - sum(sumz_null)/len(sumz_null):.1f}")

    for frac, bucket in attack_results.items():
        d_mean = sum(d for _, d, _ in bucket) / len(bucket)
        le1 = sum(1 for _, d, _ in bucket if d <= 1) / len(bucket)
        le2 = sum(1 for _, d, _ in bucket if d <= 2) / len(bucket)
        print(f"[改写{int(frac*100)}%] masked 汉明均值={d_mean:.2f} "
              f"≤1占比={le1*100:.0f}% ≤2占比={le2*100:.0f}%")

    # 示例
    doc0 = en_test[0]
    uid0 = 0x1000
    marked0 = en_codec.embed(doc0, uid0, bias=1.0, rng=random.Random(0))
    diffs = []
    for (r1, n1), (r2, n2) in zip(en_codec._tokenizer(doc0), en_codec._tokenizer(marked0)):
        if n1 and n1 != n2:
            diffs.append(f"{r1} → {r2}")
        if len(diffs) >= 6:
            break
    print(f"[替换示例 uid=0x{uid0:04X}]: {diffs}")

    results["en_ai"] = {
        "tag": "EN: AI 风格文本 × WordNet",
        "n_groups": en_codec.stats["n_groups"],
        "n_words": en_codec.stats["n_words"],
        "roundtrip_exact": f"{n_exact}/{len(rt_results)}",
        "dict_words_mean": round(sum(dict_counts) / len(dict_counts), 1),
        "sumz_marked": round(sum(sumz_marked) / len(sumz_marked), 1),
        "sumz_null": round(sum(sumz_null) / len(sumz_null), 1),
        "separation": round(sum(sumz_marked) / len(sumz_marked) - sum(sumz_null) / len(sumz_null), 1),
        "ham30_mean": round(sum(d for _, d, _ in attack_results[0.3]) / len(attack_results[0.3]), 2),
        "ham30_le1": f"{sum(1 for _, d, _ in attack_results[0.3] if d <= 1)/len(attack_results[0.3])*100:.0f}%",
        "ham50_mean": round(sum(d for _, d, _ in attack_results[0.5]) / len(attack_results[0.5]), 2),
        "ham50_le2": f"{sum(1 for _, d, _ in attack_results[0.5] if d <= 2)/len(attack_results[0.5])*100:.0f}%",
        "ham70_mean": round(sum(d for _, d, _ in attack_results[0.7]) / len(attack_results[0.7]), 2),
        "sample_diffs": diffs,
    }

    # -----------------------------------------------------------------------
    # 中文 AI 文本
    # -----------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("实验 2: 中文 AI 生成文本")
    print("=" * 70)

    zh_codec = GreenlistCodec(KEY, SALT, language_tag=b"zh")
    print(f"中文词典: {zh_codec.stats}")

    zh_ai_docs = [make_ai_zh_text(target_slots=30, seed=200 + i) for i in range(30)]
    zh_test = zh_ai_docs[:15]
    zh_null = zh_ai_docs[15:30]

    zh_codec.calibrate_p0(zh_null)

    rt_results_zh = []
    attack_results_zh = {0.3: [], 0.5: [], 0.7: []}
    sumz_marked_zh, sumz_null_zh = [], []
    dict_counts_zh = []

    for i, doc in enumerate(zh_test):
        uid = (0x2000 + i * 0x0111) & 0xFFFF
        marked = zh_codec.embed(doc, uid, bias=1.0, rng=random.Random(i + 50))
        rep = zh_codec.detect(marked)
        dist, active = zh_codec.masked_hamming(marked, uid)
        rt_results_zh.append((uid, rep.uid, dist, active))
        dict_counts_zh.append(rep.n_dict_words)
        sumz_marked_zh.append(rep.existence_score)
        sumz_null_zh.append(zh_codec.detect(doc).existence_score)

        for frac, bucket in attack_results_zh.items():
            rw, _ = synonym_attack(zh_codec, marked, frac, 300 + i)
            d, a = zh_codec.masked_hamming(rw, uid)
            bucket.append((uid, d, a))

    n_exact_zh = sum(1 for _, _, d, _ in rt_results_zh if d == 0)
    print(f"\n[往返] masked 汉明=0: {n_exact_zh}/{len(rt_results_zh)} = {n_exact_zh/len(rt_results_zh)*100:.0f}%")
    print(f"[词典词/文档] 均值={sum(dict_counts_zh)/len(dict_counts_zh):.0f} "
          f"min={min(dict_counts_zh)} max={max(dict_counts_zh)}")
    print(f"[Σ|z|] 嵌入后={sum(sumz_marked_zh)/len(sumz_marked_zh):.1f} "
          f"vs null={sum(sumz_null_zh)/len(sumz_null_zh):.1f} "
          f"分离度={sum(sumz_marked_zh)/len(sumz_marked_zh) - sum(sumz_null_zh)/len(sumz_null_zh):.1f}")

    for frac, bucket in attack_results_zh.items():
        d_mean = sum(d for _, d, _ in bucket) / len(bucket)
        le1 = sum(1 for _, d, _ in bucket if d <= 1) / len(bucket)
        le2 = sum(1 for _, d, _ in bucket if d <= 2) / len(bucket)
        print(f"[改写{int(frac*100)}%] masked 汉明均值={d_mean:.2f} "
              f"≤1占比={le1*100:.0f}% ≤2占比={le2*100:.0f}%")

    # 中文示例
    doc0z = zh_test[0]
    uid0z = 0x2000
    marked0z = zh_codec.embed(doc0z, uid0z, bias=1.0, rng=random.Random(50))
    diffs_zh = []
    for (r1, n1), (r2, n2) in zip(zh_codec._tokenizer(doc0z), zh_codec._tokenizer(marked0z)):
        if n1 and n1 != n2:
            diffs_zh.append(f"{r1} → {r2}")
        if len(diffs_zh) >= 6:
            break
    print(f"[替换示例 uid=0x{uid0z:04X}]: {diffs_zh}")

    results["zh_ai"] = {
        "tag": "ZH: AI 风格文本 × 词林",
        "n_groups": zh_codec.stats["n_groups"],
        "n_words": zh_codec.stats["n_words"],
        "roundtrip_exact": f"{n_exact_zh}/{len(rt_results_zh)}",
        "dict_words_mean": round(sum(dict_counts_zh) / len(dict_counts_zh), 1),
        "sumz_marked": round(sum(sumz_marked_zh) / len(sumz_marked_zh), 1),
        "sumz_null": round(sum(sumz_null_zh) / len(sumz_null_zh), 1),
        "separation": round(sum(sumz_marked_zh) / len(sumz_marked_zh) - sum(sumz_null_zh) / len(sumz_null_zh), 1),
        "ham30_mean": round(sum(d for _, d, _ in attack_results_zh[0.3]) / len(attack_results_zh[0.3]), 2),
        "ham30_le1": f"{sum(1 for _, d, _ in attack_results_zh[0.3] if d <= 1)/len(attack_results_zh[0.3])*100:.0f}%",
        "ham50_mean": round(sum(d for _, d, _ in attack_results_zh[0.5]) / len(attack_results_zh[0.5]), 2),
        "ham50_le2": f"{sum(1 for _, d, _ in attack_results_zh[0.5] if d <= 2)/len(attack_results_zh[0.5])*100:.0f}%",
        "ham70_mean": round(sum(d for _, d, _ in attack_results_zh[0.7]) / len(attack_results_zh[0.7]), 2),
        "sample_diffs": diffs_zh,
    }

    # -----------------------------------------------------------------------
    # 错误密钥测试（密钥安全）
    # -----------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("实验 3: 错误密钥下 AI 文本水印不可见性")
    print("=" * 70)

    wrong_key = bytes(range(1, 33))  # 偏移一位的错误密钥
    wrong_en = GreenlistCodec(wrong_key, SALT, language_tag=b"en")
    wrong_en.calibrate_p0(en_null)
    wrong_zh = GreenlistCodec(wrong_key, SALT, language_tag=b"zh")
    wrong_zh.calibrate_p0(zh_null)

    # 用错误密钥读真水印文本
    en_wrong_z = []
    zh_wrong_z = []
    for i in range(5):
        doc = en_test[i]
        uid = (0x1000 + i * 0x0111) & 0xFFFF
        marked = en_codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
        rep_wrong = wrong_en.detect(marked)
        en_wrong_z.append(rep_wrong.existence_score)

        docz = zh_test[i]
        uidz = (0x2000 + i * 0x0111) & 0xFFFF
        markedz = zh_codec.embed(docz, uidz, bias=1.0, rng=random.Random(i + 50))
        rep_wrongz = wrong_zh.detect(markedz)
        zh_wrong_z.append(rep_wrongz.existence_score)

    print(f"[EN 错误密钥 Σ|z|] 均值={sum(en_wrong_z)/len(en_wrong_z):.1f} "
          f"(真密钥={results['en_ai']['sumz_marked']})")
    print(f"[ZH 错误密钥 Σ|z|] 均值={sum(zh_wrong_z)/len(zh_wrong_z):.1f} "
          f"(真密钥={results['zh_ai']['sumz_marked']})")

    results["key_security"] = {
        "en_wrong_key_sumz": round(sum(en_wrong_z) / len(en_wrong_z), 1),
        "en_right_key_sumz": results["en_ai"]["sumz_marked"],
        "zh_wrong_key_sumz": round(sum(zh_wrong_z) / len(zh_wrong_z), 1),
        "zh_right_key_sumz": results["zh_ai"]["sumz_marked"],
    }

    # -----------------------------------------------------------------------
    # 无水印 AI 文本误报率
    # -----------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("实验 4: 无水印 AI 文本误报率")
    print("=" * 70)

    # 用一批未嵌入水印的 AI 文本测存在性得分
    en_fp_scores = [en_codec.detect(d).existence_score for d in en_null[:20]]
    zh_fp_scores = [zh_codec.detect(d).existence_score for d in zh_null[:20]]

    en_marked_scores = []
    zh_marked_scores = []
    for i in range(20):
        doc = make_ai_en_text(200, seed=500 + i)
        uid = (0x3000 + i) & 0xFFFF
        marked = en_codec.embed(doc, uid, bias=1.0, rng=random.Random(i + 100))
        en_marked_scores.append(en_codec.detect(marked).existence_score)

        docz = make_ai_zh_text(30, seed=600 + i)
        uidz = (0x4000 + i) & 0xFFFF
        markedz = zh_codec.embed(docz, uidz, bias=1.0, rng=random.Random(i + 150))
        zh_marked_scores.append(zh_codec.detect(markedz).existence_score)

    # 用中位数作为阈值，计算分离度
    en_null_med = sorted(en_fp_scores)[len(en_fp_scores)//2]
    en_marked_med = sorted(en_marked_scores)[len(en_marked_scores)//2]
    zh_null_med = sorted(zh_fp_scores)[len(zh_fp_scores)//2]
    zh_marked_med = sorted(zh_marked_scores)[len(zh_marked_scores)//2]

    # FPR@TPR 计算：阈值取 null 分布某分位
    def fpr_at_tpr(null_scores, marked_scores, tpr_target=1.0):
        threshold = sorted(marked_scores)[int((1 - tpr_target) * len(marked_scores))]
        fp = sum(1 for s in null_scores if s >= threshold)
        return fp / len(null_scores)

    en_fpr = fpr_at_tpr(en_fp_scores, en_marked_scores)
    zh_fpr = fpr_at_tpr(zh_fp_scores, zh_marked_scores)

    print(f"[EN] null median={en_null_med:.1f}, marked median={en_marked_med:.1f}, "
          f"FPR@100%TPR={en_fpr*100:.0f}%")
    print(f"[ZH] null median={zh_null_med:.1f}, marked median={zh_marked_med:.1f}, "
          f"FPR@100%TPR={zh_fpr*100:.0f}%")

    results["false_positive"] = {
        "en_null_median": round(en_null_med, 1),
        "en_marked_median": round(en_marked_med, 1),
        "en_fpr_at_100tpr": f"{en_fpr*100:.0f}%",
        "zh_null_median": round(zh_null_med, 1),
        "zh_marked_median": round(zh_marked_med, 1),
        "zh_fpr_at_100tpr": f"{zh_fpr*100:.0f}%",
    }

    # 保存
    out_path = "/tmp/ai_text_result.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已写入 {out_path}")
    return results


if __name__ == "__main__":
    run_experiment()
