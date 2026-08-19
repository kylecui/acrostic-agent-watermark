"""流式水印器增强测试：超限/超时保护 + 可配置最小嵌入长度。

覆盖 StreamingWatermarker 的三处增强：
    1. max_buffer_len：无句末标点的长流强制嵌入，防无限缓冲
    2. min_embed_len：最小嵌入长度可配置
    3. flush_timeout_ms：超时强制嵌入（fake clock 模拟）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm.plugins import Context, StreamingWatermarker, WatermarkMiddleware, Watermarker


# ----------------------------------------------------------------------
# fake clock（monkeypatch time.monotonic）
# ----------------------------------------------------------------------

class FakeClock:
    def __init__(self, t0=1000.0):
        self.t = t0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ----------------------------------------------------------------------
# 超限保护
# ----------------------------------------------------------------------

class TestMaxBufferGuard:
    def test_long_run_flushes_and_clears_buffer(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm, min_text_length=20)
        streamer = StreamingWatermarker(mw, max_buffer_len=200, min_embed_len=10)
        ctx = Context(user_id=42)

        # 喂入远超过 200 字、无句末标点的文本
        chunk = "a" * 100  # 无标点
        out = ""
        for _ in range(10):  # 累计 1000 字符
            out += streamer.feed(chunk, ctx if _ == 0 else None)

        # 超限触发强制嵌入，缓冲被清空
        assert streamer.buffered_length < 200
        assert len(out) > 0
        assert streamer.total_buffered == 1000


# ----------------------------------------------------------------------
# 最小嵌入长度可配置
# ----------------------------------------------------------------------

class TestMinEmbedLen:
    def test_min_embed_len_configurable(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm, min_text_length=20)
        streamer = StreamingWatermarker(mw, min_embed_len=50)
        ctx = Context(user_id=42)

        # 30 字符的句子：短于 min_embed_len=50 → 原样释放
        out = streamer.feed("This is a thirty-character sentence here.", ctx)
        assert "This is a thirty-character sentence here." in out

        # 60 字符的句子：超过 min_embed_len → 尝试嵌入
        long = ("This sentence has more than fifty characters in total to embed "
                "into the output stream.")
        out2 = streamer.feed(long, ctx)
        # 嵌入成功（文本被改写）或 fail-open（原文）——必须非空
        assert out2


# ----------------------------------------------------------------------
# 超时保护
# ----------------------------------------------------------------------

class TestTimeoutGuard:
    def test_timeout_forces_flush(self, monkeypatch):
        clock = FakeClock()
        monkeypatch.setattr("aawm.plugins.streaming.time.monotonic", clock.monotonic)

        wm = Watermarker()
        mw = WatermarkMiddleware(wm, min_text_length=20)
        # 50ms 超时
        streamer = StreamingWatermarker(mw, flush_timeout_ms=50, min_embed_len=10)
        ctx = Context(user_id=42)

        # 首次喂入无标点文本 → 缓冲（未超时）
        out = streamer.feed("no punctuation here at all", ctx)
        assert out == ""  # 全部缓冲
        assert streamer.buffered_length > 0

        # 时间推进超过超时阈值
        clock.advance(0.2)

        # 再喂入 → 触发超时强制嵌入
        out2 = streamer.feed(" more", ctx)
        assert len(out2) > 0  # 缓冲被强制嵌入输出
        assert streamer.buffered_length == 0

    def test_no_timeout_keeps_buffering(self, monkeypatch):
        clock = FakeClock()
        monkeypatch.setattr("aawm.plugins.streaming.time.monotonic", clock.monotonic)

        wm = Watermarker()
        mw = WatermarkMiddleware(wm, min_text_length=20)
        streamer = StreamingWatermarker(mw, flush_timeout_ms=5000, min_embed_len=10)
        ctx = Context(user_id=42)

        out = streamer.feed("no punctuation here at all", ctx)
        assert out == ""  # 仍在缓冲

        clock.advance(1.0)  # 1s < 5s
        out2 = streamer.feed(" more text", ctx)
        assert out2 == ""  # 未超时，继续缓冲
        assert streamer.buffered_length > 0
