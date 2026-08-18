"""统计检验模块：z-test + p-value。

纯数学模块。v0.2 起 DetectionResult 增加 payload 字段，
用于携带模式特定的结果对象（如 DecodeResult）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class DetectionResult:
    """水印检测结果。

    Attributes:
        hits: 一致位数（与码字对齐的锚点数）
        total: 有效锚点总数
        z_score: 标准化检验统计量
        p_value: p-value（H0 下观测到不更极端结果的概率）
        detected: 检出判定（v0.2 起以 CRC 校验为准）
        payload: 模式特定的附带结果（如 DecodeResult）
    """
    hits: int
    total: int
    z_score: float
    p_value: float
    detected: bool
    payload: Optional[Any] = None


def _normal_cdf(x: float) -> float:
    """标准正态分布 CDF，用 erf 实现。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def z_test(
    hits: int,
    total: int,
    alpha: float = 0.01,
    p_null: float = 0.5,
) -> DetectionResult:
    """单边 z-检验。

    H0: 谓词满足率 = p_null（无水印随机情况）
    H1: 谓词满足率 > p_null（存在水印）

    Args:
        hits: 实际命中的锚点数
        total: 有效锚点总数
        alpha: 显著性水平（默认 0.01）
        p_null: 零假设下的满足率（默认 0.5）

    Returns:
        DetectionResult
    """
    if total <= 0:
        return DetectionResult(
            hits=hits, total=total, z_score=0.0, p_value=1.0, detected=False
        )
    mean = p_null * total
    std = math.sqrt(total * p_null * (1.0 - p_null))
    if std == 0:
        z = 0.0
    else:
        z = (hits - mean) / std
    p_value = 1.0 - _normal_cdf(z)
    return DetectionResult(
        hits=hits,
        total=total,
        z_score=z,
        p_value=p_value,
        detected=p_value < alpha,
    )
