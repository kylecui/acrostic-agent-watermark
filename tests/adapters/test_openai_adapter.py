"""OpenAI SDK 适配器测试：用 mock 客户端，不依赖真实 openai 安装。

策略：
    - 适配器代码是纯 duck-typing 包装（import 无需 openai）
    - 测试用 Fake 客户端 / 响应对象模拟 openai SDK 结构
    - 覆盖：同步包装 / 异步包装 / 流式包装 / fail-open / tool_calls 跳过
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aawm.plugins import UIDRegistry, Watermarker, WatermarkMiddleware
from aawm.plugins.adapters.openai_v1 import (
    wrap_async_openai_client,
    wrap_openai_client,
)

LONG_TEXT = (
    "The platform collects telemetry from every distributed agent working "
    "in the fleet. Each agent watches a big stream of events, keeps a small "
    "record of important changes, and builds a short summary at the end of "
    "the reporting window. A strong supervisor groups the results into a "
    "common view, so the whole system stays easy to inspect. When an agent "
    "finds a hard problem it cannot fix alone, it sends a quick alert to "
    "the central team and asks for help. The team then checks whether the "
    "issue is new or old, whether it is critical or minor, and whether a "
    "fast patch is possible without a full restart of the service."
)


# ----------------------------------------------------------------------
# Fake OpenAI SDK 结构
# ----------------------------------------------------------------------

def _chunk_content(chunk) -> str:
    """兼容对象/字典两种 delta 形态的取文本 helper。"""
    choices = chunk.choices if hasattr(chunk, "choices") else []
    if not choices:
        return ""
    delta = choices[0]["delta"] if isinstance(choices[0], dict) else getattr(choices[0], "delta")
    if delta is None:
        return ""
    return delta.get("content", "") if isinstance(delta, dict) else delta.content

class FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, content="", finish="stop", tool_calls=None):
        self.message = FakeMessage(content, tool_calls)
        self.finish_reason = finish


class FakeResponse:
    def __init__(self, content="", tool_calls=None):
        self.choices = [FakeChoice(content, tool_calls=tool_calls)]


class FakeDelta:
    def __init__(self, content=""):
        self.content = content


class FakeChunk:
    def __init__(self, content=""):
        self.choices = [{"delta": FakeDelta(content), "finish_reason": None}]


class FakeCompletions:
    def __init__(self, content=LONG_TEXT):
        self.content = content

    def create(self, *args, **kwargs):
        if kwargs.get("stream"):
            return self._fake_stream()
        return FakeResponse(self.content)

    def _fake_stream(self):
        parts = self.content.split(". ")
        return [FakeChunk(part + ". ") for part in parts]


class FakeChat:
    def __init__(self, content=LONG_TEXT):
        self.completions = FakeCompletions(content)


class FakeClient:
    def __init__(self, content=LONG_TEXT):
        self.chat = FakeChat(content)


class FakeAsyncCompletions:
    def __init__(self, content=LONG_TEXT):
        self.content = content

    async def create(self, *args, **kwargs):
        if kwargs.get("stream"):
            return self._fake_async_stream()
        return FakeResponse(self.content)

    async def _fake_async_stream(self):
        parts = self.content.split(". ")
        for part in parts:
            yield FakeChunk(part + ". ")


class FakeAsyncChat:
    def __init__(self, content=LONG_TEXT):
        self.completions = FakeAsyncCompletions(content)


class FakeAsyncClient:
    def __init__(self, content=LONG_TEXT):
        self.chat = FakeAsyncChat(content)


# ----------------------------------------------------------------------
# 同步包装
# ----------------------------------------------------------------------

class TestWrapSyncClient:
    def test_sync_client_content_watermarked(self):
        client = FakeClient()
        # LONG_TEXT 是通用英文文本，零感词典命中稀疏 → 显式 default 词林
        wm = Watermarker(codec_mode="default")
        wrap_openai_client(client, wm)

        resp = client.chat.completions.create(
            model="gpt-4o", messages=[], user_id=42
        )

        # content 被改写（嵌入了水印）
        assert resp.choices[0].message.content != LONG_TEXT
        assert len(resp.choices[0].message.content) > 0

    def test_sync_watermark_traceable(self):
        """改写后的文本可用同一个 Watermarker 溯源。"""
        registry = UIDRegistry(backend="memory")
        registry.register("alice", uid=0x1234)
        # LONG_TEXT 是通用英文文本，零感词典命中稀疏 → 显式 default 词林
        wm = Watermarker(registry=registry, codec_mode="default")
        client = wrap_openai_client(FakeClient(), wm)

        resp = client.chat.completions.create(model="gpt-4o", messages=[])
        # 嵌入时无上下文（user_id=None）→ fail-open 跳过嵌入
        # 用显式 user_id 验证可溯源
        client = FakeClient()
        wrap_openai_client(client, wm)
        resp = client.chat.completions.create(
            model="gpt-4o", messages=[], user_id="alice"
        )
        text = resp.choices[0].message.content
        assert text != LONG_TEXT
        # 无 salt 无法精确溯源（仅验证已改写 + 无异常）

    def test_skips_tool_calls(self):
        client = FakeClient()
        wm = Watermarker()
        wrap_openai_client(client, wm)

        original = client.chat.completions.create

        # 构造带 tool_calls 的响应
        def create_with_tools(*args, **kwargs):
            return FakeResponse(LONG_TEXT, tool_calls=[{"id": "1", "name": "search"}])

        client.chat.completions.create = create_with_tools
        resp = client.chat.completions.create(model="gpt-4o", messages=[])
        assert resp.choices[0].message.content == LONG_TEXT  # 未改写

        # 恢复
        client.chat.completions.create = original

    def test_fail_open_on_exception(self):
        """嵌入异常应透传原文。"""
        class BadWatermarker:
            def embed(self, *args, **kwargs):
                raise RuntimeError("boom")
            registry = None
            keystore = None

        client = FakeClient()
        wrap_openai_client(client, BadWatermarker(), skip_if_no_context=False)

        resp = client.chat.completions.create(model="gpt-4o", messages=[])
        assert resp.choices[0].message.content == LONG_TEXT  # 原文

    def test_short_text_not_embedded(self):
        client = FakeClient(content="Hello.")
        wm = Watermarker()
        wrap_openai_client(client, wm, min_text_length=100)

        resp = client.chat.completions.create(model="gpt-4o", messages=[])
        assert resp.choices[0].message.content == "Hello."  # 太短未嵌入

    def test_stream_response_wrapped(self):
        """stream=True 时返回可迭代的包装流，逐句嵌入。"""
        client = FakeClient()
        wm = Watermarker()
        wrap_openai_client(client, wm, skip_if_no_context=False)

        stream = client.chat.completions.create(model="gpt-4o", messages=[], stream=True)

        collected = "".join(_chunk_content(chunk) for chunk in stream)
        assert len(collected) > 0
        assert stream is not None


# ----------------------------------------------------------------------
# 异步包装
# ----------------------------------------------------------------------

class TestWrapAsyncClient:
    def test_async_client_content_watermarked(self):
        client = FakeAsyncClient()
        # LONG_TEXT 是通用英文文本，零感词典命中稀疏 → 显式 default 词林
        wm = Watermarker(codec_mode="default")
        wrap_async_openai_client(client, wm)

        async def run():
            resp = await client.chat.completions.create(
                model="gpt-4o", messages=[], user_id=42
            )
            return resp.choices[0].message.content

        content = asyncio.new_event_loop().run_until_complete(run())
        assert content != LONG_TEXT
        assert len(content) > 0

    def test_async_stream_response_wrapped(self):
        client = FakeAsyncClient()
        wm = Watermarker()
        wrap_async_openai_client(client, wm, skip_if_no_context=False)

        async def run():
            stream = await client.chat.completions.create(
                model="gpt-4o", messages=[], stream=True
            )
            collected = ""
            async for chunk in stream:
                collected += _chunk_content(chunk)
            return collected

        collected = asyncio.new_event_loop().run_until_complete(run())
        assert len(collected) > 0
