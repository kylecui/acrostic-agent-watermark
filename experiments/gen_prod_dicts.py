#!/usr/bin/env python3
"""gen_prod_dicts.py: 生成生产默认词典数据文件（v0.9 词典扩容）。

从 corpus/dict 的词林/WordNet 原始数据构建大词典，与现有手工策划词典
合并（策划组优先，只补新词），写入 src/aawm/data/*.json 供
synonym_data.load_default_*_dictionary() 加载。

构建规则（exp_dict_expansion 实验定稿，D3r / E3 配置）：
  ZH: 生产 ZH_SYNONYMS_RAW ∪ 词林 '=' 严格同义组（新词）
      —— '#' 近义组语义纯度不足（篮球→铅球类替换），不纳入默认
  EN: 生产 EN_SYNONYMS_RAW+EXTRA ∪ WordNet synset（组>=3、单词、>=2 字符）
      —— 组=2 的 synset 在随机替换攻击下必然翻转颜色，剔除；
         单字母词命中罗马数字 synset（I→One），剔除

实测收益（30 篇真实语料攻击谱，soft n>=1 匹配）：
  ZH: paws 27→30、s30 23→30、s50 12→23、存在性间隔 +7.6→+19.2
  EN: s30 28→29、s50 21→27、存在性间隔 +13.3→+42.0
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.synonym_data import EN_SYNONYMS_EXTRA, EN_SYNONYMS_RAW, ZH_SYNONYMS_RAW
from dict_build import build_cilin_dict, build_wordnet_dict

DATA_DIR = os.path.join("src", "aawm", "data")


def merge_dicts(primary: dict, extra: dict, min_new_group: int) -> dict:
    """primary 组优先；extra 只补充 primary 未覆盖的新词。"""
    merged = dict(primary)
    used = {w for ws in primary.values() for w in ws}
    for k, ws in extra.items():
        ws2 = [w for w in ws if w not in used]
        if len(ws2) >= min_new_group:
            merged[k] = ws2
            used.update(ws2)
    return merged


def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)

    # --- ZH: 生产 ∪ 词林 '=' ---
    cilin = build_cilin_dict("corpus/dict/cilin_extended.txt")
    zh = merge_dicts(ZH_SYNONYMS_RAW, cilin, min_new_group=2)
    zh_groups = sorted(sorted(ws) for ws in zh.values() if len(ws) >= 2)
    zh_words = {w for ws in zh_groups for w in ws}
    with open(os.path.join(DATA_DIR, "zh_synonyms.json"), "w", encoding="utf-8") as f:
        json.dump(zh_groups, f, ensure_ascii=False, separators=(",", ":"))
    print(f"ZH: {len(zh_groups)} 组 / {len(zh_words)} 词 "
          f"(原 {len(ZH_SYNONYMS_RAW)} 组 / {len({w for ws in ZH_SYNONYMS_RAW.values() for w in ws})} 词)")

    # --- EN: 生产 ∪ WordNet>=3 单词组 ---
    wn = build_wordnet_dict(single_word_only=True)
    wn3 = {k: ws for k, ws in wn.items() if len(ws) >= 3}
    en_prod = {**EN_SYNONYMS_RAW, **EN_SYNONYMS_EXTRA}
    en = merge_dicts(en_prod, wn3, min_new_group=3)
    en_groups = sorted(sorted(ws) for ws in en.values() if len(ws) >= 2)
    en_words = {w for ws in en_groups for w in ws}
    with open(os.path.join(DATA_DIR, "en_synonyms.json"), "w", encoding="utf-8") as f:
        json.dump(en_groups, f, ensure_ascii=False, separators=(",", ":"))
    print(f"EN: {len(en_groups)} 组 / {len(en_words)} 词 "
          f"(原 {len(en_prod)} 组 / {len({w for ws in en_prod.values() for w in ws})} 词)")


if __name__ == "__main__":
    main()
