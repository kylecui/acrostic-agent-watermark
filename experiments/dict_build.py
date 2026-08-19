#!/usr/bin/env python3
"""dict_build.py: 从词林(cilin)/WordNet 构建绿名单大词典。

词林格式（自动识别两种）:
  1. 人民日报标注版（4 位编码）: 词/词性/词林编码(4位)，编码去重后按组聚合。
  2. 哈工大扩展版（8 位原子词群）: `编码7位+标记(=/#/@) 词1 词2 ...`，
     只取 '='（严格同义）行；'#'（同类近义）、'@'（独立词）语义纯度不足，
     弃用。
过滤规则（语义纯度优先，宁缺毋滥）:
  - 排除虚词/关联/助语大类: K(助语) L(敬语) M(语气) J(关联)
    —— C(时空) 保留（地名同义在新闻语料常见）
  - 组大小 2-20（8 位原子词群为严格同义词，上限可比 4 位聚合版放宽）
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
# 8 位词林原子词群编码：A-Z + a-z + 2 位数字 + A-Z + 2 位数字（共 7 位）
_CILIN8_CODE_RE = re.compile(r"^[A-Z][a-z]\d{2}[A-Z]\d{2}$")


def _parse_cilin8(path: str) -> Dict[str, List[str]]:
    """解析哈工大扩展版（8 位原子词群），只取 '=' 同义行。

    行格式: `Aa01A01= 人 士 人物 人士 人氏 人选`
    `#`（同类）与 `@`（独立）语义纯度不足，不构成可替换同义组。
    """
    groups: Dict[str, List[str]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            code, mark, words = line.partition("=")
            if mark != "=":
                continue
            code = code.strip()
            if not _CILIN8_CODE_RE.match(code):
                continue
            candidates = words.strip().split()
            bg = sorted(w for w in candidates if _HAN_RE.match(w))
            if code[0] in _ZH_EXCLUDE_CATS:
                continue
            if 2 <= len(bg) <= _CILIN8_MAX_GROUP:
                groups[f"cilin:{code}"] = bg
    return groups


def _parse_cilin4(path: str) -> Dict[str, List[str]]:
    """解析人民日报标注版（4 位编码），编码去重后按组聚合。"""
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


# 8 位原子词群最大组大小（严格同义词，上限比 4 位聚合版放宽）
_CILIN8_MAX_GROUP = 20


def build_cilin_dict(path: str | None = None) -> Dict[str, List[str]]:
    """从词林构建中文同义词组 {组键: [双字词...]}。

    自动识别两种格式：行内含 '=' 且首 token 为 7 位原子词群编码 → 8 位
    扩展版；否则按人民日报标注版解析。
    """
    path = path or os.path.join(CORPUS, "dict", "cilin_utf8.txt")
    with open(path, encoding="utf-8") as f:
        head = f.readline()
    code_part = head.split("=", 1)[0].strip()
    if _CILIN8_CODE_RE.match(code_part):
        return _parse_cilin8(path)
    return _parse_cilin4(path)


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
    # 语义抽查（组键随格式变化：8 位原子词群 / 4 位聚合编码）
    for k in list(zh_dict)[:1]:
        print(f"  示例组 {k}: {zh_dict[k][:8]}")
    # 语义抽查：找含"人民/美丽/建设"的组验证同义纯度
    targets = ("人民", "美丽", "建设")
    hits = [ws for ws in zh_dict.values() if any(w in targets for w in ws)]
    for ws in hits[:3]:
        print(f"  语义组: {ws[:8]}")

    wn = build_wordnet_dict()
    n_wn = len({w for ws in wn.values() for w in ws})
    print(f"\nWordNet 英文词典: {len(wn)} 组 / {n_wn} 词形")
    import itertools
    for k, ws in itertools.islice(wn.items(), 5):
        print(f"  {k}: {ws}")
