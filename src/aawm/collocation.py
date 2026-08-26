"""语料上下文兼容性过滤（减病句）。

词林同义词组中很多词虽词典义相同但搭配域不同（领域↔版图、内容↔要义），
替换后产生病句。两道互补信号：

1. 字符集上下文兼容率：对每对词，从语料中收集左邻/右邻字符集，
   计算双向重叠率的最小值。大语料下覆盖率显著提升。
2. 语素共享 bonus：同字（共享汉字）的词对更可能安全互换
   （标志/标记→共享"标"+"记"，近邻/邻居→共享"邻"）。

综合分 = max(上下文兼容率, 0.3 if 语素共享 else 0)。
threshold 以下剔除。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set, Tuple


def build_char_context(
    texts: List[str], words: Set[str]
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """对每个词，分别收集左邻字符集和右邻字符集。

    Args:
        texts: 语料文本列表
        words: 需要索引的词集合

    Returns:
        (left, right): 词 -> 字符集 的映射
    """
    left: Dict[str, Set[str]] = defaultdict(set)
    right: Dict[str, Set[str]] = defaultdict(set)
    for text in texts:
        for w in words:
            start = 0
            while True:
                idx = text.find(w, start)
                if idx == -1:
                    break
                if idx > 0:
                    left[w].add(text[idx - 1])
                end = idx + len(w)
                if end < len(text):
                    right[w].add(text[end])
                start = idx + 1
    return dict(left), dict(right)


def pair_compat(
    left: Dict[str, Set[str]],
    right: Dict[str, Set[str]],
    a: str,
    b: str,
) -> float:
    """双向字符集兼容率：最保守方向的最弱重叠。

    对左邻和右邻各做 src→dst 和 dst→src 的重叠率，
    取四个值中的最小值（最保守估计）。
    """
    la, lb = left.get(a, set()), left.get(b, set())
    ra, rb = right.get(a, set()), right.get(b, set())
    scores: List[float] = []
    for src, dst in [(la, lb), (lb, la), (ra, rb), (rb, ra)]:
        if not src:
            scores.append(0.0)
        else:
            scores.append(len(src & dst) / len(src))
    return min(scores)


def has_shared_morpheme(a: str, b: str) -> bool:
    """两词是否共享至少一个汉字（语素）。"""
    return bool(set(a) & set(b))


def group_score(
    group: List[str],
    left: Dict[str, Set[str]],
    right: Dict[str, Set[str]],
) -> float:
    """综合兼容分 = max(上下文兼容率, 0.3 if 语素共享 else 0)。

    取组内最差词对的分数。
    """
    if len(group) < 2:
        return 1.0
    worst = 1.0
    for i in range(len(group)):
        for j in range(i + 1, len(group)):
            ctx_ov = pair_compat(left, right, group[i], group[j])
            morph = 0.3 if has_shared_morpheme(group[i], group[j]) else 0.0
            score = max(ctx_ov, morph)
            if score < worst:
                worst = score
    return worst


def filter_groups(
    groups: Dict[str, List[str]],
    left: Dict[str, Set[str]],
    right: Dict[str, Set[str]],
    threshold: float = 0.15,
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """按兼容性过滤词林组。

    Args:
        groups: 待过滤的词典组
        left, right: build_char_context 的输出
        threshold: 低于此分数的组被剔除

    Returns:
        (survived, dropped): 存活组和剔除组
    """
    survived: Dict[str, List[str]] = {}
    dropped: Dict[str, List[str]] = {}
    for key, words in groups.items():
        sc = group_score(words, left, right)
        if sc >= threshold:
            survived[key] = words
        else:
            dropped[key] = words
    return survived, dropped
