"""FullTextWatermarker：请求级全文后嵌（feed 不泄露 / flush 整篇嵌入）。"""

from __future__ import annotations

from aawm.plugins.context import Context
from aawm.plugins.middleware import WatermarkMiddleware
from aawm_enterprise.full_stream import FullTextWatermarker

UID = 7


def _mw(wm) -> WatermarkMiddleware:
    return WatermarkMiddleware(wm)


def test_feed_never_leaks(wm):
    fs = FullTextWatermarker(_mw(wm))
    ctx = Context(user_id=UID, language="zh")
    for chunk in ["第一段", "第二段", "第三段。"]:
        assert fs.feed(chunk, ctx) == ""     # 缓冲期间零输出
    assert fs.buffered_length > 0


def test_flush_embeds_full_text(wm, long_zh_doc):
    fs = FullTextWatermarker(_mw(wm))
    ctx = Context(user_id=UID, language="zh")
    size = len(long_zh_doc) // 10
    for i in range(10):
        fs.feed(long_zh_doc[i * size:(i + 1) * size], ctx)
    out = fs.flush()
    assert out != long_zh_doc                # 已嵌入
    assert fs.buffered_length == 0


def test_flush_short_text_passthrough(wm):
    fs = FullTextWatermarker(_mw(wm))
    fs.feed("短句。", Context(user_id=UID))
    assert fs.flush() == "短句。"            # 太短 fail-open 透传


def test_flush_empty(wm):
    fs = FullTextWatermarker(_mw(wm))
    assert fs.flush() == ""


def test_flush_fail_open(wm, long_zh_doc, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("inject failure")
    monkeypatch.setattr(wm, "embed", boom)
    fs = FullTextWatermarker(_mw(wm))
    fs.feed(long_zh_doc, Context(user_id=UID, language="zh"))
    assert fs.flush() == long_zh_doc         # 嵌入失败 → 原文
