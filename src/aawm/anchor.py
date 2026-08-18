"""锚点位置选择模块。

用密钥派生 + 可变性过滤，选出水印锚点位置。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class AnchorConfig:
    """锚点选择配置。

    Attributes:
        alpha: 锚点占可变位置的比例（默认 0.2）
        min_anchors: 最少锚点数（防止短文本退化）
        max_anchors: 最多锚点数（控制开销）
    """
    alpha: float = 0.2
    min_anchors: int = 8
    max_anchors: int = 64


def select_anchors(
    mutable_positions: Sequence[int],
    anchor_seed: bytes,
    config: AnchorConfig = AnchorConfig(),
) -> List[int]:
    """从可变位置集合中选锚点。

    算法：对每个可变位置 i，计算 PRF(anchor_seed, i)，
    按 PRF 值排序，取前 k 个作为锚点。

    Args:
        mutable_positions: 可变 token 位置列表
        anchor_seed: 密钥派生的种子
        config: 锚点配置

    Returns:
        锚点位置列表（按原位置排序）
    """
    if not mutable_positions:
        return []

    # 计算每个位置的 PRF 分数
    scored = []
    for pos in mutable_positions:
        h = hashlib.sha256(anchor_seed + b":" + str(pos).encode()).digest()
        # 取前 8 字节作为 uint64
        score = int.from_bytes(h[:8], "big")
        scored.append((pos, score))

    # 按 PRF 分数升序排序（保证密钥相关、位置无关）
    scored.sort(key=lambda x: x[1])

    # 取前 k 个
    n_mutable = len(mutable_positions)
    k = max(config.min_anchors, min(config.max_anchors, int(config.alpha * n_mutable)))
    k = min(k, n_mutable)

    anchors = sorted(pos for pos, _ in scored[:k])
    return anchors
