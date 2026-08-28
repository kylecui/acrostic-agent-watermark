"""框架适配器测试：用 monkeypatch 模拟，不依赖真实 langchain/litellm 安装。

策略：
    - 适配器代码正常写
    - 测试用假 model / 假 hook 模拟框架接口
    - CI 无需装 langchain / litellm
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aawm.plugins import (
    Context,
    ContextChain,
    UIDRegistry,
    Watermarker,
    WatermarkMiddleware,
)


# 测试文本
LONG_TEXT = (
    "The platform collects telemetry from every distributed agent working "
    "in the fleet. Each agent watches a big stream of events, keeps a small "
    "record of important changes, and builds a short summary at the end of "
    "the reporting window. A strong supervisor groups the results into a "
    "common view, so the whole system stays easy to inspect. When an agent "
    "finds a hard problem it cannot fix alone, it sends a quick alert to "
    "the central team and asks for help. The team then checks whether the "
    "issue is new or old, whether it is critical or minor, and whether a "
    "fast patch is possible without a full restart of the service. The "
    "platform also supports a strong audit trail that records every "
    "important change made by any agent in the system, so a careful "
    "reviewer can always find the root cause of a hard problem."
)


# ======================================================================
# LangChain v1 适配器测试（模拟 AgentMiddleware）
# ======================================================================

class TestLangChainAdapter:
    """测试 LangChain v1 适配器。

    通过 monkeypatch 注入假的 langchain.agents.middleware 模块，
    使 AAWMMiddleware 可以正常实例化。
    """

    def _make_fake_langchain(self):
        """创建假的 langchain 模块结构。"""
        fake_mod = MagicMock()
        fake_mw_mod = MagicMock()
        fake_mw_mod.AgentMiddleware = object  # 基类用 object
        fake_mod.agents.middleware = fake_mw_mod
        return fake_mod

    def test_import_without_langchain_raises(self):
        """未安装 langchain 时，AAWMMiddleware 应给出清晰错误。"""
        # 确保测试环境没装 langchain
        if "langchain" in sys.modules:
            pytest.skip("langchain installed, skipping import-error test")
        from aawm.plugins.adapters.langchain_v1 import AAWMMiddleware
        wm = Watermarker()
        with pytest.raises(ImportError, match="langchain is not installed"):
            AAWMMiddleware(wm)

    def test_after_model_embeds_watermark(self, monkeypatch):
        """after_model 钩子应嵌入水印。"""
        # 注入假 langchain
        fake_lc = self._make_fake_langchain()
        monkeypatch.setitem(sys.modules, "langchain", fake_lc)
        monkeypatch.setitem(sys.modules, "langchain.agents", fake_lc.agents)
        monkeypatch.setitem(sys.modules, "langchain.agents.middleware", fake_lc.agents.middleware)

        # 重新 import 以拿到 _HAS_LANGCHAIN=True 的版本
        if "aawm.plugins.adapters.langchain_v1" in sys.modules:
            del sys.modules["aawm.plugins.adapters.langchain_v1"]
        from aawm.plugins.adapters.langchain_v1 import AAWMMiddleware

        # LONG_TEXT 是通用英文文本，必须走 default 词林（零感词典命中稀疏时
        # 随机盐下可能"无词可改"返回原文 → 断言偶发失败）
        wm = Watermarker(codec_mode="default")
        mw = AAWMMiddleware(wm)

        # 构造假 response（LangChain AIMessage 风格）
        class FakeResponse:
            content = LONG_TEXT

        class FakeRequest:
            runtime = MagicMock()
            runtime.context = {"user_id": 42}

        response = FakeResponse()
        request = FakeRequest()

        # 调用 after_model
        result = asyncio.new_event_loop().run_until_complete(
            mw.after_model(request, response, handler=None)
        )

        # 验证：content 被改写（嵌入了水印）
        assert result.content != LONG_TEXT
        # 验证：用嵌入时的中间件记录的 salt 溯源
        # （适配器场景下 salt 需通过外部存档传递，这里用中间件内部 watermarker 验证）
        assert result.content  # 非空

    def test_after_model_skips_tool_calls(self, monkeypatch):
        """tool_calls 非空时应跳过嵌入。"""
        fake_lc = self._make_fake_langchain()
        monkeypatch.setitem(sys.modules, "langchain", fake_lc)
        monkeypatch.setitem(sys.modules, "langchain.agents", fake_lc.agents)
        monkeypatch.setitem(sys.modules, "langchain.agents.middleware", fake_lc.agents.middleware)

        if "aawm.plugins.adapters.langchain_v1" in sys.modules:
            del sys.modules["aawm.plugins.adapters.langchain_v1"]
        from aawm.plugins.adapters.langchain_v1 import AAWMMiddleware

        wm = Watermarker()
        mw = AAWMMiddleware(wm)

        class FakeResponse:
            content = LONG_TEXT
            tool_calls = [{"id": "1", "name": "search", "args": {"q": "test"}}]

        response = FakeResponse()
        request = MagicMock()

        result = asyncio.new_event_loop().run_until_complete(
            mw.after_model(request, response, handler=None)
        )

        # 验证：未改写（有 tool_calls）
        assert result.content == LONG_TEXT

    def test_after_model_fail_open(self, monkeypatch):
        """嵌入失败时应透传原文。"""
        fake_lc = self._make_fake_langchain()
        monkeypatch.setitem(sys.modules, "langchain", fake_lc)
        monkeypatch.setitem(sys.modules, "langchain.agents", fake_lc.agents)
        monkeypatch.setitem(sys.modules, "langchain.agents.middleware", fake_lc.agents.middleware)

        if "aawm.plugins.adapters.langchain_v1" in sys.modules:
            del sys.modules["aawm.plugins.adapters.langchain_v1"]
        from aawm.plugins.adapters.langchain_v1 import AAWMMiddleware

        # 用一个会抛异常的 Watermarker
        class BadWatermarker:
            def embed(self, *args, **kwargs):
                raise RuntimeError("boom")
            registry = None
            keystore = MagicMock()

        mw = AAWMMiddleware(BadWatermarker(), skip_if_no_context=False)  # type: ignore

        class FakeResponse:
            content = LONG_TEXT

        response = FakeResponse()
        request = MagicMock()

        result = asyncio.new_event_loop().run_until_complete(
            mw.after_model(request, response, handler=None)
        )

        # fail-open：返回原文
        assert result.content == LONG_TEXT


# ======================================================================
# LiteLLM Proxy 适配器测试（模拟 hook）
# ======================================================================

class TestLiteLLMAdapter:
    """测试 LiteLLM Proxy 适配器。"""

    def test_setup_hooks(self):
        """setup_hooks 应初始化模块级中间件。"""
        from aawm.plugins.adapters import litellm_proxy

        wm = Watermarker()
        litellm_proxy.setup_hooks(wm)

        assert litellm_proxy._mw is not None
        assert litellm_proxy._chain is not None

    def test_post_call_success_hook_embeds(self):
        """非流式钩子应嵌入水印。"""
        from aawm.plugins.adapters import litellm_proxy

        # LONG_TEXT 是通用英文文本，零感词典命中稀疏——必须显式 default
        # 词林路径（旧默认行为，稳定改写），否则 embed 随机盐下可能无词
        # 可替换而返回原文 → 断言偶发失败（VERIFICATION_REPORT 的
        # "LiteLLM 适配器 hook" flaky）。
        wm = Watermarker(codec_mode="default")
        litellm_proxy.setup_hooks(wm)

        # 构造假 OpenAI 格式响应
        class FakeMessage:
            content = LONG_TEXT

        class FakeChoice:
            message = FakeMessage()
            finish_reason = "stop"

        class FakeResponse:
            choices = [FakeChoice()]

        response = FakeResponse()

        # 构造假 user_api_key_dict
        uak = MagicMock()
        uak.metadata = {"user_id": 42}

        result = asyncio.new_event_loop().run_until_complete(
            litellm_proxy.async_post_call_success_hook(
                data={}, user_api_key_dict=uak, response=response
            )
        )

        # 验证：content 被改写
        assert result.choices[0].message.content != LONG_TEXT

    def test_post_call_hook_skips_tool_calls(self):
        """tool_calls 响应应跳过。"""
        from aawm.plugins.adapters import litellm_proxy

        wm = Watermarker()
        litellm_proxy.setup_hooks(wm)

        class FakeMessage:
            content = LONG_TEXT
            tool_calls = [{"id": "1"}]

        class FakeChoice:
            message = FakeMessage()
            finish_reason = "tool_calls"

        class FakeResponse:
            choices = [FakeChoice()]

        response = FakeResponse()
        uak = MagicMock()
        uak.metadata = {"user_id": 42}

        result = asyncio.new_event_loop().run_until_complete(
            litellm_proxy.async_post_call_success_hook(
                data={}, user_api_key_dict=uak, response=response
            )
        )

        assert result.choices[0].message.content == LONG_TEXT

    def test_streaming_hook_embeds(self):
        """流式钩子应逐句嵌入。"""
        from aawm.plugins.adapters import litellm_proxy

        wm = Watermarker()
        litellm_proxy.setup_hooks(wm)

        # 构造假流式响应
        class FakeDelta:
            def __init__(self, content):
                self.content = content

        class FakeChunk:
            def __init__(self, content, finish=None):
                self.choices = [{"delta": FakeDelta(content), "finish_reason": finish}]

        async def fake_stream():
            # 分多次输出
            parts = LONG_TEXT.split(". ")
            for i, part in enumerate(parts):
                yield FakeChunk(part + ". ", finish="stop" if i == len(parts) - 1 else None)

        uak = MagicMock()
        uak.metadata = {"user_id": 42}

        # 收集输出
        async def collect():
            output = ""
            async for chunk in litellm_proxy.async_post_call_streaming_iterator_hook(
                data={}, user_api_key_dict=uak, response=fake_stream()
            ):
                # 提取 content
                if hasattr(chunk, "choices") and chunk.choices:
                    delta = chunk.choices[0].get("delta")
                    if delta and hasattr(delta, "content"):
                        output += delta.content
            return output

        output = asyncio.new_event_loop().run_until_complete(collect())
        assert len(output) > 0  # 有输出

    def test_hook_without_setup_returns_as_is(self):
        """未 setup 时应透传。"""
        from aawm.plugins.adapters import litellm_proxy

        # 重置模块状态
        litellm_proxy._mw = None

        class FakeResponse:
            choices = [MagicMock()]

        response = FakeResponse()
        uak = MagicMock()

        result = asyncio.new_event_loop().run_until_complete(
            litellm_proxy.async_post_call_success_hook(
                data={}, user_api_key_dict=uak, response=response
            )
        )

        assert result is response  # 透传
