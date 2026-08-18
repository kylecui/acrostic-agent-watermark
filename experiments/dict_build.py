#!/usr/bin/env python3
"""dict_build.py: 从词林(cilin)/WordNet 构建绿名单大词典。

词林格式（人民日报标注版）: 词/词性/词林编码(4位)，编码去重后按组聚合。
过滤规则（语义纯度优先，宁缺毋滥）:
  - 编码必须大写字母开头（剔除解析噪声）
  - 排除虚词/关联/助语大类: K(助语) L(敬语) M(语气) J(关联) C(时空)?
    —— C 保留（地名同义在新闻语料常见），K/L/M/J 排除
  - 组大小 2-8（太大概率语义混杂）
  - 仅双字词（与现有 zh tokenizer 兼容，边界漂移可控）
  - 词必须全为汉字

WordNet 格式: data.noun/adj/verb/adv 的 synset 行，同 synset = 同义词组。
  - 组大小 2-10，词形为字母串（含下划线归一为空格）
"""
from __future__ import annotations

import glob
import os
import re
from collections import defaultdict
from typing import Dict, List

CORPUS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "corpus")

_ZH_EXCLUDE_CATS = set("KLMJ")  # 助语/敬语/语气/关联
_HAN_RE = re.compile(r"^[\u4e00-\u9fff]{2}$")


def build_cilin_dict(path: str | None = None) -> Dict[str, List[str]]:
    """从词林构建中文同义词组 {组键: [双字词...]}。"""
    path = path or os.path.join(CORPUS, "dict", "cilin_utf8.txt")
    codes: Dict[str, set] = defaultdict(set)
    with open(path, encoding="utf-8") as f:
        for line in f:
            for item in line.split():
                parts = item.split("/")
                if len(parts) >= 3:
                    w, code = parts[0], parts[-1]
                    if code and code[0].isupper() and len(code) == 4:
                        codes[code].add(w)
    groups: Dict[str, List[str]] = {}
    for code, words in codes.items():
        if code[0] in _ZH_EXCLUDE_CATS:
            continue
        bg = sorted(w for w in words if _HAN_RE.match(w))
        if 2 <= len(bg) <= 8:
            groups[f"cilin:{code}"] = bg
    return groups


def build_wordnet_dict(wn_dir: str | None = None) -> Dict[str, List[str]]:
    """从 WordNet 3.x data.* 构建 {synset_id: [词形...]}。

    行格式（无缩进才为 synset 行）:
      offset lex_filenum ss_type w_cnt w1 lex_id1 w2 lex_id2 ... ptr_cnt ... | gloss
    ss_type: n/v/a/r/s（s=形容词卫星，并入 a 处理）。
    """
    wn_dir = wn_dir or os.path.join(CORPUS, "dict", "wordnet")
    groups: Dict[str, List[str]] = {}
    for path in sorted(glob.glob(os.path.join(wn_dir, "data.*"))):
        fname = os.path.basename(path)
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line[:1].isspace() or line.startswith("  "):
                    continue  # license 块为缩进行
                parts = line.split()
                if len(parts) < 4 or len(parts[0]) != 8 or not parts[0].isdigit():
                    continue
                offset, ss_type, w_cnt = parts[0], parts[2], parts[3]
                if ss_type not in ("n", "v", "a", "r", "s") or not w_cnt.isdigit():
                    continue
                w_cnt = int(w_cnt)
                words = []
                idx = 4
                for _ in range(w_cnt):
                    if idx >= len(parts):
                        break
                    w = parts[idx].lower().replace("_", " ")
                    if re.fullmatch(r"[a-z][a-z' -]*", w):
                        words.append(w)
                    idx += 2  # 跳过 lex_id
                words = sorted(set(words))
                if 2 <= len(words) <= 10:
                    groups[f"wn:{fname}:{offset}:{ss_type}"] = words
    return groups


if __name__ == "__main__":
    zh_dict = build_cilin_dict()
    n_zh_words = len({w for ws in zh_dict.values() for w in ws})
    print(f"词林中文词典: {len(zh_dict)} 组 / {n_zh_words} 双字词")
    from collections import Counter
    cat = Counter(k.split(":")[1][0] for k in zh_dict)
    print(f"  大类分布: {dict(sorted(cat.items()))}")
    # 语义抽查
    for k in ["cilin:Aa05", "cilin:Ed01", "cilin:Hc10"]:
        if k in zh_dict:
            print(f"  {k}: {zh_dict[k][:8]}")

    wn = build_wordnet_dict()
    n_wn = len({w for ws in wn.values() for w in ws})
    print(f"\nWordNet 英文词典: {len(wn)} 组 / {n_wn} 词形")
    import itertools
    for k, ws in itertools.islice(wn.items(), 5):
        print(f"  {k}: {ws}")
