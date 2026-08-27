#!/usr/bin/env python3
"""gen_en_zero_cost_dict.py: 生成英文零感词典正式数据文件。

从 experiments/en_zero_cost_dict.py（单一事实源）构建
src/aawm/data/en_zero_cost.json，供 synonym_data 的
load_zero_cost_en_dictionary() 加载。

数据文件结构与中文零感词典一致：
{
  "groups": [["analyze", "analyse"], ["because", ...], ...],
  "block_words": []   # 英文无单字语素误切问题，恒为空
}
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from aawm.synonym_data import validate_dictionary
from en_zero_cost_dict import ZERO_COST_EN, group_counts

DATA_DIR = os.path.join("src", "aawm", "data")
OUT_FILE = os.path.join(DATA_DIR, "en_zero_cost.json")

KEY = bytes(range(32))
SALT = b"gen-en-zero-cost-2026"


def main() -> None:
    validate_dictionary(ZERO_COST_EN)

    # 组键必须是语义代表（head），band = HMAC(K_band, head) 派生，
    # 不能 sorted(ws) 打乱组首词。英文词典键已全小写。
    groups = [
        [head] + sorted(w for w in ws if w != head)
        for head, ws in ZERO_COST_EN.items()
    ]

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"groups": groups, "block_words": []},
            f, ensure_ascii=False, separators=(",", ":"),
        )

    # 密钥下单色组过滤预估（必修课 2 随机损耗）
    codec = GreenlistCodec(
        KEY, SALT, n_bands=16,
        dictionary=ZERO_COST_EN, language_tag=b"en",
    )
    n_total = len(ZERO_COST_EN)
    n_keep = codec.stats["n_groups"]
    n_words = codec.stats["n_words"]
    print(f"词典规模: {n_total} 组 / {n_words} 词 ({group_counts()})")
    print(f"密钥下可编码: {n_keep} 组（单色过滤 {n_total - n_keep}，"
          f"{100 * (n_total - n_keep) / n_total:.0f}%）")
    print(f"平均组大小: {n_words / n_keep:.2f}")
    print(f"已写入 {OUT_FILE}")


if __name__ == "__main__":
    main()
