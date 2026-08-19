"""流式水印器：句子级缓冲重写。

利用现有算法层 _BOS/_EOS 句子感知锚点的特性——重写单句只损失该句的票，
不污染邻句锚点。所以流式场景可以"缓冲到句末，整句嵌入后释放"。

用法（适配器内部）::

    streamer = StreamingWatermarker(middleware)
    async for chunk in response:
        delta = extract_delta(chunk)
        marked = streamer.feed(delta, ctx)
        if marked:
            yield marked_chunk(marked)
    tail = streamer.flush()
    if tail:
        yield marked_chunk(tail)

策略：
    - 累积 delta 到缓冲区
    - 遇到句末标点（.!?。！？；）触发整句嵌入
    - 缓冲区超限 / 超时强制嵌入（防无限缓冲）
    - 嵌入失败 fail-open 释放原文
    - flush() 处理剩余缓冲（即使无句末标点也强制嵌入）
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from .context import Context
from .facade import EmbedResult, Watermarker
from .middleware import WatermarkMiddleware

logger = logging.getLogger("aawm.plugin.streaming")

# 句末标点（英文 .!? + 中文 。！？；）
_SENT_END_RE = re.compile(r"[.!?。！？；\n]")


class StreamingWatermarker:
    """句子级流式水印器。

    缓冲到句末标点，整句嵌入后释放。带超限/超时保护，防无限缓冲。
    """

    def __init__(
        self,
        middleware: WatermarkMiddleware,
        *,
        min_embed_len: int = 20,
        max_buffer_len: int = 4096,
        flush_timeout_ms: int = 2000,
    ) -> None:
        self._mw = middleware
        self._min_embed_len = min_embed_len
        self._max_buffer_len = max_buffer_len
        self._flush_timeout_s = flush_timeout_ms / 1000.0
        self._buffer = ""
        self._ctx: Optional[Context] = None
        self._total_emitted = 0
        self._total_buffered = 0
        self._last_emit = time.monotonic()

    def feed(
        self,
        delta: str,
        ctx: Optional[Context] = None,
    ) -> str:
        """喂入流式 chunk，返回可安全输出的已定型文本。

        Args:
            delta: 新到的文本片段
            ctx: 上下文（首次调用设置，后续沿用）

        Returns:
            可安全输出的已嵌入文本（可能为空，表示还在缓冲）
        """
        if ctx is not None:
            self._ctx = ctx
        if not delta:
            return ""

        self._buffer += delta
        self._total_buffered += len(delta)

        out: list[str] = []

        # 超时保护：距上次输出超过阈值 → 强制嵌入当前缓冲
        now = time.monotonic()
        if self._buffer and (now - self._last_emit) > self._flush_timeout_s:
            out.append(self._emit_buffer())

        # 超限保护：缓冲超过上限 → 强制嵌入
        if len(self._buffer) >= self._max_buffer_len:
            out.append(self._emit_buffer())

        # 一次性扫描定位所有句末标点（O(n)，避免逐句重扫 buffer 的 O(n²)）
        last = 0
        for m in _SENT_END_RE.finditer(self._buffer):
            sentence = self._buffer[last:m.end()]
            last = m.end()
            out.append(self._embed_chunk(sentence))
        if last:
            self._buffer = self._buffer[last:]

        result = "".join(out)
        if result:
            self._last_emit = time.monotonic()
        self._total_emitted += len(result)
        return result

    def flush(self) -> str:
        """流结束，嵌入并返回剩余缓冲。

        Returns:
            最后一段已嵌入文本（可能为空）
        """
        if not self._buffer:
            return ""
        marked = self._embed_chunk(self._buffer)
        self._buffer = ""
        return marked

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    @property
    def buffered_length(self) -> int:
        """当前缓冲区长度。"""
        return len(self._buffer)

    @property
    def total_emitted(self) -> int:
        """已输出总长度。"""
        return self._total_emitted

    @property
    def total_buffered(self) -> int:
        """累计接收总长度。"""
        return self._total_buffered

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _emit_buffer(self) -> str:
        """强制嵌入当前整个缓冲区并清空。"""
        if not self._buffer:
            return ""
        text = self._buffer
        self._buffer = ""
        return self._embed_chunk(text)

    def _embed_chunk(self, text: str) -> str:
        """嵌入一个文本块。Fail-open：失败返回原文。"""
        if not text or not text.strip():
            return text

        # 太短不嵌入——原样释放（避免碎片嵌入破坏锚点）
        if len(text.strip()) < self._min_embed_len:
            return text

        marked, _ = self._mw.transform(text, self._ctx)
        return marked
