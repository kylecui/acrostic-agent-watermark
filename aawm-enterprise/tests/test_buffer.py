"""SessionBuffer：幂等去重 / 拼接 / 空闲计时。"""

from __future__ import annotations

from aawm_enterprise.buffer import SessionBuffer, make_key


def test_append_and_join():
    buf = SessionBuffer(key=make_key("s1", 7))
    buf.append("第一段内容。")
    buf.append("第二段内容。")
    assert buf.text == "第一段内容。\n\n第二段内容。"
    assert buf.char_count == len(buf.text)


def test_idempotent_dedup():
    buf = SessionBuffer(key=make_key("s1", 7))
    assert buf.append("同一段内容。") is True
    assert buf.append("同一段内容。") is False   # 重复片段不重复存储
    assert buf.append("同一段内容。") is False
    assert len(buf.segments) == 1
    assert buf.char_count == len("同一段内容。")


def test_idle_seconds_monotonic():
    import time
    buf = SessionBuffer(key=make_key("s1", 7))
    buf.append("x" * 10)
    first = buf.idle_seconds
    time.sleep(0.01)
    assert buf.idle_seconds >= first
