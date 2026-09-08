"""aawm-enterprise：AAWM 企业组件（BUSL-1.1，独立于 MIT 核心）。

组件一：聚合水印缓冲层——会话级文档延迟嵌入（只处理最终交付物）。
"""

from .aggregator import AggregateWatermarker, FlushResult
from .buffer import BufferedSegment, SessionBuffer
from .full_stream import FullTextWatermarker
from .policy import FlushPolicy

__version__ = "0.1.0"

__all__ = [
    "AggregateWatermarker",
    "FlushResult",
    "SessionBuffer",
    "BufferedSegment",
    "FlushPolicy",
    "FullTextWatermarker",
]
