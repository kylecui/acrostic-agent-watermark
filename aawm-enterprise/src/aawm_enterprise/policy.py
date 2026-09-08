"""FlushPolicy：聚合缓冲的 flush 触发策略与诚实分级出口。

参数全部可配置（设计 §4.2/§4.3，作者拍板"可手工调整而非 hardcode"）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FlushPolicy:
    """聚合 flush 的触发与出口策略。

    Attributes:
        target_capacity: 预检容量目标（k>=target → high 档达标）。
            默认 10（reliability_tier 的 high 锚点，中文标定后约 1200 字）。
        min_tier: 最低接受嵌入档（"low"/"medium"/"high"）。预检档位低于
            此值时 flush **不嵌入**（abstain 出口），调用方拿到 None +
            abstained 说明——默认 "low"（总是尽力嵌，如实分级，不硬拒，
            与 8-28 短文档策略一致）。
        idle_timeout_s: 空闲超时（秒）。超时会话由 sweep_idle() 收官，
            防悬挂缓冲。仅作为兜底——显式 close() 才是主要收官语义。
        max_buffer_chars: 单会话缓冲字符上限。到限后 append 触发自动
            flush（结果进 pending 队列），防无限内存累积。
    """

    target_capacity: int = 10
    min_tier: str = "low"
    idle_timeout_s: float = 300.0
    max_buffer_chars: int = 200_000

    _TIER_ORDER = {"low": 0, "medium": 1, "high": 2}

    def tier_admissible(self, tier: str) -> bool:
        """预检档位是否达到最低接受档。"""
        return self._TIER_ORDER[tier] >= self._TIER_ORDER[self.min_tier]
