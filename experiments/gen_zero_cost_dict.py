#!/usr/bin/env python3
"""gen_zero_cost_dict.py: 生成零感词典正式数据文件。

从 experiments/zh_zero_cost_dict.py（单一事实源）构建
src/aawm/data/zh_zero_cost.json，供 synonym_data 的
load_zero_cost_zh_dictionary() / load_zero_cost_zh_block_words() 加载。

数据文件结构（可读性优先，组首词即语义代表）：
{
  "groups": [["因为", "由于"], ["提高", "提升", "增强"], ...],   # 有序
  "block_words": ["和平", "和尚", ...]
}

构建时执行完整性自检（validate_dictionary），并打印密钥下
单色组过滤损耗预估（必修课 2 的随机损耗）。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec, make_zh_tokenizer
from aawm.synonym_data import validate_dictionary
from zh_zero_cost_dict import (
    ZERO_COST_ZH,
    load_block_words,
    group_counts,
)

DATA_DIR = os.path.join("src", "aawm", "data")
OUT_FILE = os.path.join(DATA_DIR, "zh_zero_cost.json")

KEY = bytes(range(32))
SALT = b"gen-zero-cost-2026"


def main() -> None:
    validate_dictionary(ZERO_COST_ZH)

    # 组键必须是语义代表（head），band = HMAC(K_band, head) 派生，
    # 定向补带依赖组键精确性——不能 sorted(ws) 打乱组首词。
    groups = [
        [head] + sorted(w for w in ws if w != head)
        for head, ws in ZERO_COST_ZH.items()
    ]
    block_words = sorted(load_block_words())

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"groups": groups, "block_words": block_words},
            f, ensure_ascii=False, separators=(",", ":"),
        )

    # 密钥下单色组过滤预估（必修课 2）
    all_words = {w for ws in ZERO_COST_ZH.values() for w in ws} | set(block_words)
    tokenizer = make_zh_tokenizer(dict_words=all_words)
    codec = GreenlistCodec(
        KEY, SALT, n_bands=16,
        dictionary=ZERO_COST_ZH, language_tag=b"zh", tokenizer=tokenizer,
    )
    n_total = len(ZERO_COST_ZH)
    n_keep = codec.stats["n_groups"]
    n_words = codec.stats["n_words"]
    print(f"词典规模: {n_total} 组 / {codec.stats['n_words']} 词 "
          f"({group_counts()})")
    print(f"密钥下可编码: {n_keep} 组（单色过滤 {n_total - n_keep}，"
          f"{100 * (n_total - n_keep) / n_total:.0f}%）")
    print(f"平均组大小: {n_words / n_keep:.2f}")
    print(f"已写入 {OUT_FILE}")


if __name__ == "__main__":
    main()
