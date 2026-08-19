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
    - 嵌入失败 fail-open 释放原文
    - flush() 处理剩余缓冲（即使无句末标点也强制嵌入）
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .context import Context
from .facade import EmbedResult, Watermarker
from .middleware import WatermarkMiddleware

logger = logging.getLogger("aawm.plugin.streaming")

# 句末标点（英文 .!? + 中文 。！？；）
_SENT_END_RE = re.compile(r"[.!?。！？；\n]")

# 最小可嵌入长度（短于这个值不嵌入，避免碎片）
_MIN_EMBED_LEN = 50


class StreamingWatermarker:
    """句子级流式水印器。

    缓冲到句末标点，整句嵌入后释放。
    """

    def __init__(
        self,
        middleware: WatermarkMiddleware,
        *,
        flush_timeout_ms: int = 2000,
    ) -> None:
        self._mw = middleware
        self._flush_timeout_ms = flush_timeout_ms
        self._buffer = ""
        self._ctx: Optional[Context] = None
        self._total_emitted = 0
        self._total_buffered = 0
        # 单次嵌入的最小文本量：低于此值不触发嵌入
        # 流式场景词数可能不足以往返，但我们仍尝试

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

        # 尝试切出完整句并嵌入
        output = []
        while True:
            # 找最后一个句末标点（保留标点在句内）
            m = None
            for m in _SENT_END_RE.finditer(self._buffer):
                pass  # 找到最后一个
            if m is None:
                break
            # 切到标点之后
            cut = m.end()
            sentence = self._buffer[:cut]
            self._buffer = self._buffer[cut:]

            # 嵌入这一句
            marked = self._embed_chunk(sentence)
            output.append(marked)

        result = "".join(output)
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

    def _embed_chunk(self, text: str) -> str:
        """嵌入一个文本块。Fail-open：失败返回原文。"""
        if not text or not text.strip():
            return text

        # 短文本不嵌入——累积到足够长度再处理
        # 但流式场景下，单句可能就是全部内容，所以我们对短句也尝试
        # 只在极短时跳过
        if len(text.strip()) < 20:
            return text

        marked, _ = self._mw.transform(text, self._ctx)
        return marked
