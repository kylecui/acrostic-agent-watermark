"""FullTextWatermarker：请求级全文后嵌（proxy streamer_factory 注入实现）。

与核心 StreamingWatermarker 同接口（feed/flush/buffered_length），行为不同：

    feed(delta)  -> ""        # 只缓冲，不提前泄露任何文本
    flush()      -> str       # [DONE]/completed 时整篇嵌入，返回 marked 全文

设计依据（§5.2.1 模式矩阵）：整流逐句嵌入 = 句内局部池 + 高替换密度，
自然性劣于全文嵌入；proxy 下游是应用代码，TTFB=全文生成时长可接受。

fail-open：嵌入异常返回原文（绝不产出半截水印）。
"""

from __future__ import annotations

import logging
from typing import Optional

from aawm.plugins.context import Context
from aawm.plugins.middleware import WatermarkMiddleware

logger = logging.getLogger("aawm_enterprise.full_stream")


class FullTextWatermarker:
    """请求级全文缓冲 → [DONE] 后整篇嵌入。"""

    def __init__(
        self,
        middleware: WatermarkMiddleware,
        *,
        min_embed_chars: int = 50,
    ) -> None:
        self._mw = middleware
        self._min_embed_chars = min_embed_chars
        self._buffer = ""
        self._ctx: Optional[Context] = None
        self._total_buffered = 0

    def feed(self, delta: str, ctx: Optional[Context] = None) -> str:
        """缓冲流式 chunk。始终返回 ""——客户端等全文，不提前泄露。"""
        if ctx is not None:
            self._ctx = ctx
        if delta:
            self._buffer += delta
            self._total_buffered += len(delta)
        return ""

    def flush(self) -> str:
        """流结束：整篇嵌入并返回 marked 全文（fail-open 返回原文）。"""
        text = self._buffer
        self._buffer = ""
        if not text or not text.strip():
            return ""
        if len(text.strip()) < self._min_embed_chars:
            return text  # 太短：嵌入无意义，fail-open 透传
        try:
            marked, _ = self._mw.transform(text, self._ctx)
            return marked
        except Exception as e:
            logger.warning("full-text embed failed, fail-open: %s", e)
            return text

    @property
    def buffered_length(self) -> int:
        return len(self._buffer)

    @property
    def total_buffered(self) -> int:
        return self._total_buffered
