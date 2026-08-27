#!/usr/bin/env python3
"""en_zero_cost_dict.py: 英文零感词典 —— 拼写变体 + 功能副词 + 高自然安全对。

动机（用户方向性决策 2026-08-27：英文也要一版 zero_cost）：
    英文此前只能走 default 词林（big→sizable 等），自然度差于中文零感。
    本词典复刻中文 zero_cost 的设计哲学，针对英文语言特性选词。

英文专属优势：**拼写变体（美/英）**是语义零差异、人类几乎无感的
替换对（organize/organise、color/colour），这是中文没有的零感王牌，
作为 TIER1 主打。

三档设计（对齐中文 zh_zero_cost_dict.py 的 A 值体系）：
    TIER1 拼写变体（A=1.0）：美/英式拼写对，语义零差异
    TIER2 功能副词（A≥0.9）：连接副词/程度/频率/时间副词，语义一致
    TIER3 高自然精选组（A≈0.8-0.85）：动词/形容词/名词安全对，
          全部通过"所有常见语境可互换"重审

扩展原则（语感质量把关，中文 v1 崩坏教训重演警告）：
    1. 组内词必须"所有常见语境可互换"——只保证部分语境可换的宽泛词
       （significant/important/problem/issue/short 等多义词）全部剔除
    2. 组键（第一个词）即语义代表，必在组内；全部小写
    3. 避免跨组共享词（必修课 1 先到先得会静默吞组，构建时自检）
    4. 组大小 2-3 词（3 词组可对抗必修课 2 单色过滤的密钥随机损耗）

关键实现约束（en_tokenizer 正则 [A-Za-z]+(?:'[A-Za-z]+)? 切词）：
    1. 词典词必须是**单个 token**（不含空格/撇号）——"do not"、"as a
       result" 等短语切不出来，不可作词典词；缩写对（doesn't/do not）
       因此被排除，只剩 can't/cannot 这类单 token 对
    2. 词典键值全部小写（en_tokenizer 的 norm = p.lower()）
    3. 英文无中文单字语素误切问题，不需要阻断词表
"""
from __future__ import annotations

from typing import Dict

# ---------------------------------------------------------------------------
# TIER1 拼写变体（A=1.0）：美式 → 英式，语义零差异、读者几乎无感。
# 组键用美式拼写（更普遍）。替换后文本可能美英混拼——这正是零感所在。
# ---------------------------------------------------------------------------
TIER1_SPELLING: Dict[str, list] = {
    # --- 动词 -ize/-ise 变体 ---
    "analyze": ["analyze", "analyse"],
    "organize": ["organize", "organise"],
    "recognize": ["recognize", "recognise"],
    "realize": ["realize", "realise"],
    "emphasize": ["emphasize", "emphasise"],
    "minimize": ["minimize", "minimise"],
    "maximize": ["maximize", "maximise"],
    "optimize": ["optimize", "optimise"],
    "summarize": ["summarize", "summarise"],
    "prioritize": ["prioritize", "prioritise"],
    "customize": ["customize", "customise"],
    "visualize": ["visualize", "visualise"],
    "standardize": ["standardize", "standardise"],
    "specialize": ["specialize", "specialise"],
    "authorize": ["authorize", "authorise"],
    "utilize": ["utilize", "utilise"],
    # --- 名词 -or/-our、-er/-re 变体 ---
    "color": ["color", "colour"],
    "center": ["center", "centre"],
    "meter": ["meter", "metre"],
    "theater": ["theater", "theatre"],
    "behavior": ["behavior", "behaviour"],
    "favorite": ["favorite", "favourite"],
    "honor": ["honor", "honour"],
    "labor": ["labor", "labour"],
    "neighbor": ["neighbor", "neighbour"],
    "favor": ["favor", "favour"],
    # --- 副词/介词 -ward(s)、-st 变体 ---
    "toward": ["toward", "towards"],
    "among": ["among", "amongst"],
    "amid": ["amid", "amidst"],
    "while": ["while", "whilst"],
}

# ---------------------------------------------------------------------------
# TIER2 功能副词（A≥0.9）：连接副词/句首副词，语义零差异。
# 全部是句子/段落级连接词，位置与语义一致。
# ---------------------------------------------------------------------------
TIER2_CONNECTORS: Dict[str, list] = {
    # --- 转折 ---
    "however": ["however", "nevertheless", "nonetheless"],
    # --- 因果 ---
    "therefore": ["therefore", "thus", "hence"],
    "consequently": ["consequently", "accordingly"],
    # --- 递进 ---
    "additionally": ["additionally", "moreover", "furthermore"],
    "subsequently": ["subsequently", "afterward"],
    # --- 类比 ---
    "similarly": ["similarly", "likewise"],
}

# ---------------------------------------------------------------------------
# TIER2 程度/频率/时间副词（A≥0.9）：语义一致的功能副词。
# 剔除完全/几乎等强度漂移词（always/constantly、very/really）。
# ---------------------------------------------------------------------------
TIER2_ADVERBS: Dict[str, list] = {
    "frequently": ["frequently", "often"],
    "rarely": ["rarely", "seldom"],
    "usually": ["usually", "typically"],
    "generally": ["generally", "commonly"],
    "sometimes": ["sometimes", "occasionally"],
    "almost": ["almost", "nearly"],
    "approximately": ["approximately", "roughly"],
    "precisely": ["precisely", "exactly"],
    "partially": ["partially", "partly"],
    "completely": ["completely", "entirely"],
    "quickly": ["quickly", "rapidly"],
    "immediately": ["immediately", "instantly"],
    "previously": ["previously", "earlier"],
    "recently": ["recently", "lately"],
    "especially": ["especially", "particularly"],
    "considerably": ["considerably", "substantially"],
    "clearly": ["clearly", "obviously", "evidently"],
    "essentially": ["essentially", "fundamentally"],
    "primarily": ["primarily", "chiefly"],
    "eventually": ["eventually", "ultimately"],
    "until": ["until", "till"],
    # --- 介词（关涉/方位，语义一致）---
    "regarding": ["regarding", "concerning"],
    "beneath": ["beneath", "underneath"],
}

# ---------------------------------------------------------------------------
# TIER3 高自然动词安全对（A≈0.8-0.85）。
# 全部通过"所有常见语境可互换"重审。以下候选被否决：
#   help/assist（assist 偏"协助人"，help the environment 不通）
#   support/back、check/verify、ensure/guarantee（语义强度/范围不同）
#   remove/eliminate、improve/enhance、create/generate（部分语境病）
#   begin/commence（commence 罕见）、continue/proceed（介词框架不同）
#   cause/reason（cause of / reason for 介词搭配不同）
# ---------------------------------------------------------------------------
TIER3_VERB: Dict[str, list] = {
    "begin": ["begin", "start"],
    "finish": ["finish", "complete"],
    "choose": ["choose", "select"],
    "gather": ["gather", "collect"],
    "decrease": ["decrease", "reduce"],
    "solve": ["solve", "resolve"],
    "verify": ["verify", "confirm"],
    "obtain": ["obtain", "acquire"],
    "evaluate": ["evaluate", "assess"],
    "alter": ["alter", "modify"],
    "postpone": ["postpone", "delay"],
    "achieve": ["achieve", "accomplish"],
    "demonstrate": ["demonstrate", "show"],
    "emphasize": ["emphasize", "stress"],
    "highlight": ["highlight", "underscore"],
    "strengthen": ["strengthen", "reinforce"],
    "connect": ["connect", "link"],
    "publish": ["publish", "release"],
    "correct": ["correct", "rectify"],
    "amend": ["amend", "revise"],
    "allocate": ["allocate", "assign"],
    "admit": ["admit", "acknowledge"],
    "refuse": ["refuse", "decline"],
    "protect": ["protect", "safeguard"],
    "rebuild": ["rebuild", "reconstruct"],
}

# ---------------------------------------------------------------------------
# TIER3 高自然形容词安全对（A≈0.8-0.85）。
# 否决：important/crucial（important person 通而 crucial person 不通）、
#   significant（statistically significant 固定搭配）、wrong（多义）、
#   short（矮/短两义）、regular（regular customer 固定）、
#   global/worldwide 之外的 broad/wide、strong/powerful 等。
# ---------------------------------------------------------------------------
TIER3_ADJ: Dict[str, list] = {
    "accurate": ["accurate", "precise"],
    "appropriate": ["appropriate", "suitable"],
    "obvious": ["obvious", "evident"],
    "comprehensive": ["comprehensive", "thorough"],
    "quick": ["quick", "fast", "rapid"],
    "sudden": ["sudden", "abrupt"],
    "complex": ["complex", "complicated"],
    "simple": ["simple", "straightforward"],
    "useful": ["useful", "helpful"],
    "cautious": ["cautious", "careful"],
    "specific": ["specific", "particular"],
    "previous": ["previous", "prior"],
    "whole": ["whole", "entire"],
    "modern": ["modern", "contemporary"],
    "traditional": ["traditional", "conventional"],
    "huge": ["huge", "enormous"],
    "beneficial": ["beneficial", "advantageous"],
    "widespread": ["widespread", "prevalent"],
    "remarkable": ["remarkable", "notable"],
    "nice": ["nice", "pleasant"],
    "urgent": ["urgent", "pressing"],
    "harmful": ["harmful", "detrimental"],
    "global": ["global", "worldwide"],
    "genuine": ["genuine", "authentic"],
    "annual": ["annual", "yearly"],
    "ready": ["ready", "prepared"],
    "optimal": ["optimal", "optimum"],
    "considerable": ["considerable", "substantial"],
    "reliable": ["reliable", "dependable"],
    "sufficient": ["sufficient", "adequate"],
}

# ---------------------------------------------------------------------------
# TIER3 高自然名词安全对（A≈0.8-0.85）。
# 否决：problem/issue（issue 多义）、challenge/difficulty（challenge 可作
#   动词）、method/approach（介词框架不同）、situation/circumstance
#   （单复数不一致）、benefit/advantage 保留（互通度高）。
# ---------------------------------------------------------------------------
TIER3_NOUN: Dict[str, list] = {
    "goal": ["goal", "objective"],
    "outcome": ["outcome", "result"],
    "factor": ["factor", "element"],
    "error": ["error", "mistake"],
    "type": ["type", "kind"],
    "feature": ["feature", "characteristic"],
    "process": ["process", "procedure"],
    "evidence": ["evidence", "proof"],
    "word": ["word", "term"],
    "phrase": ["phrase", "expression"],
    "perspective": ["perspective", "viewpoint"],
    "option": ["option", "alternative"],
    "period": ["period", "phase"],
    "benefit": ["benefit", "advantage"],
    "impact": ["impact", "effect"],
    "example": ["example", "instance"],
    "purpose": ["purpose", "aim"],
    "opinion": ["opinion", "view"],
    "achievement": ["achievement", "accomplishment"],
    "aid": ["aid", "assistance"],
}

ZERO_COST_EN: Dict[str, list] = {
    **TIER1_SPELLING,
    **TIER2_CONNECTORS,
    **TIER2_ADVERBS,
    **TIER3_VERB,
    **TIER3_ADJ,
    **TIER3_NOUN,
}


def load_zero_cost_dictionary() -> Dict[str, list]:
    """返回英文零感词典（组键 → 候选词）。"""
    return dict(ZERO_COST_EN)


def group_counts() -> Dict[str, int]:
    """各档组数（诊断用）。"""
    return {
        "TIER1_SPELLING": len(TIER1_SPELLING),
        "TIER2_CONNECTORS": len(TIER2_CONNECTORS),
        "TIER2_ADVERBS": len(TIER2_ADVERBS),
        "TIER3_VERB": len(TIER3_VERB),
        "TIER3_ADJ": len(TIER3_ADJ),
        "TIER3_NOUN": len(TIER3_NOUN),
        "total": len(ZERO_COST_EN),
    }
