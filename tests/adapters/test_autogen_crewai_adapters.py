"""AutoGen / CrewAI 适配器测试：用 monkeypatch 模拟，不依赖真实安装。

策略（与 test_framework_adapters.py 一致）：
    - 适配器代码正常写
    - 测试用假 agent / 假 hook context 模拟框架接口
    - CI 无需装 autogen-agentchat / crewai
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aawm.plugins import UIDRegistry, Watermarker, WatermarkMiddleware

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


# ======================================================================
# 假 AutoGen 结构
# ======================================================================

class FakeChatMessage:
    def __init__(self, content: str):
        self.content = content


class FakeResponse:
    def __init__(self, content: str):
        self.chat_message = FakeChatMessage(content)


class FakeAutogenAgent:
    """模拟 autogen-agentchat AssistantAgent。"""

    def __init__(self, content: str = LONG_TEXT):
        self._content = content

    async def on_messages(self, messages, cancellation_token=None):
        return FakeResponse(self._content)


def _install_fake_autogen(monkeypatch):
    """注入假的 autogen_agentchat 模块结构。"""
    import importlib

    fake_agents = MagicMock()
    fake_agents.AssistantAgent = object
    fake_mod = MagicMock()
    fake_mod.agents = fake_agents
    monkeypatch.setitem(sys.modules, "autogen_agentchat", fake_mod)
    monkeypatch.setitem(sys.modules, "autogen_agentchat.agents", fake_agents)
    # 重新 import 以拿到 _HAS_AUTOGEN=True
    sys.modules.pop("aawm.plugins.adapters.autogen_v1", None)
    return importlib.import_module("aawm.plugins.adapters.autogen_v1")


class TestAutoGenAdapter:
    def test_import_error_without_autogen(self):
        """未安装 autogen-agentchat 时 wrap 抛清晰 ImportError。"""
        if "autogen_agentchat" in sys.modules:
            pytest.skip("autogen installed, skipping import-error test")
        from aawm.plugins.adapters.autogen_v1 import wrap_autogen_agent
        wm = Watermarker()
        with pytest.raises(ImportError, match="autogen-agentchat is not installed"):
            wrap_autogen_agent(MagicMock(), wm)

    def test_wrap_embeds_watermark(self, monkeypatch):
        """包装后 on_messages 的 content 被嵌水印，且能溯源。"""
        autogen_v1 = _install_fake_autogen(monkeypatch)

        reg = UIDRegistry(backend="memory")
        reg.register("bob", uid=42)
        # 此测试验证 adapter 包装机制（文本 LONG_TEXT 为通用英文，对
        # zero_cost 零感词典覆盖稀疏），显式用 default 词林兼容路径。
        wm = Watermarker(registry=reg, codec_mode="default")
        archived = {}

        def on_embed(result, ctx):
            archived[result.user_id] = result.session_salt

        agent = FakeAutogenAgent()
        autogen_v1.wrap_autogen_agent(agent, wm, user_id="bob", on_embed=on_embed)

        response = asyncio.new_event_loop().run_until_complete(
            agent.on_messages(MagicMock())
        )
        marked = response.chat_message.content
        assert marked != LONG_TEXT  # 被改写

        # 用 on_embed 存档的 salt 溯源
        assert 42 in archived
        t = wm.trace(marked, session_salt=archived[42])
        assert t.watermarked
        assert t.user == "bob"

    def test_wrap_skip_without_user_id(self, monkeypatch):
        """无 user_id / contextvars / env 时跳过嵌入（不嵌入错误身份）。"""
        autogen_v1 = _install_fake_autogen(monkeypatch)

        wm = Watermarker()
        agent = FakeAutogenAgent()
        autogen_v1.wrap_autogen_agent(agent, wm)  # 不传 user_id

        response = asyncio.new_event_loop().run_until_complete(
            agent.on_messages(MagicMock())
        )
        assert response.chat_message.content == LONG_TEXT  # 未改写

    def test_wrap_fail_open(self, monkeypatch):
        """嵌入异常时透传原文。"""
        autogen_v1 = _install_fake_autogen(monkeypatch)

        class BadWatermarker:
            def embed(self, *args, **kwargs):
                raise RuntimeError("boom")
            registry = None
            keystore = MagicMock()

        agent = FakeAutogenAgent()
        autogen_v1.wrap_autogen_agent(agent, BadWatermarker(), user_id=42)  # type: ignore

        response = asyncio.new_event_loop().run_until_complete(
            agent.on_messages(MagicMock())
        )
        assert response.chat_message.content == LONG_TEXT  # fail-open

    def test_wrap_short_text_skip(self, monkeypatch):
        """短文本不嵌入。"""
        autogen_v1 = _install_fake_autogen(monkeypatch)

        wm = Watermarker()
        agent = FakeAutogenAgent(content="too short")
        autogen_v1.wrap_autogen_agent(agent, wm, user_id=42, min_text_length=50)

        response = asyncio.new_event_loop().run_until_complete(
            agent.on_messages(MagicMock())
        )
        assert response.chat_message.content == "too short"


# ======================================================================
# 假 CrewAI hook 环境
# ======================================================================

def _install_fake_crewai(monkeypatch):
    """注入假的 crewai.hooks 模块结构。"""
    import importlib

    fake_hooks = MagicMock()
    fake_mod = MagicMock()
    fake_mod.hooks = fake_hooks
    monkeypatch.setitem(sys.modules, "crewai", fake_mod)
    monkeypatch.setitem(sys.modules, "crewai.hooks", fake_hooks)
    sys.modules.pop("aawm.plugins.adapters.crewai_v1", None)
    return importlib.import_module("aawm.plugins.adapters.crewai_v1")


class FakeHookContext:
    """模拟 LLMCallHookContext。"""

    def __init__(self, response: Any = LONG_TEXT, agent=None, crew=None):
        self.response = response
        self.agent = agent
        self.crew = crew
        self.task = None


class TestCrewAIAdapter:
    def test_import_error_without_crewai(self):
        """未安装 crewai 时 setup_hooks 抛清晰 ImportError。"""
        if "crewai" in sys.modules:
            pytest.skip("crewai installed, skipping import-error test")
        from aawm.plugins.adapters.crewai_v1 import setup_hooks
        wm = Watermarker()
        with pytest.raises(ImportError, match="crewai is not installed"):
            setup_hooks(wm)

    def test_setup_registers_hook_and_embeds(self, monkeypatch):
        """setup_hooks 注册 hook；after_llm_call 对响应嵌水印。"""
        crewai_v1 = _install_fake_crewai(monkeypatch)

        reg = UIDRegistry(backend="memory")
        reg.register("carol", uid=7)
        # LONG_TEXT 是通用英文文本，零感词典命中稀疏 → 显式 default 词林，
        # 否则随机盐下可能"无词可改"返回原文 → 断言偶发失败。
        wm = Watermarker(registry=reg, codec_mode="default")
        crewai_v1.setup_hooks(wm, user_id="carol")

        # 确认注册到了 crewai 的 register_after_llm_call_hook
        from crewai.hooks import register_after_llm_call_hook
        assert register_after_llm_call_hook.called

        # 直接调用 hook 函数
        ctx = FakeHookContext()
        result = crewai_v1._after_llm_call(ctx)
        assert isinstance(result, str)
        assert result != LONG_TEXT  # 被改写

    def test_after_llm_call_skip_without_user_id(self, monkeypatch):
        """无 user_id 时 hook 返回 None（不嵌入）。"""
        crewai_v1 = _install_fake_crewai(monkeypatch)

        wm = Watermarker()
        crewai_v1.setup_hooks(wm)  # 不传 user_id

        ctx = FakeHookContext()
        result = crewai_v1._after_llm_call(ctx)
        assert result is None

    def test_after_llm_call_fail_open(self, monkeypatch):
        """嵌入异常时 hook 返回 None（保持原响应）。"""
        crewai_v1 = _install_fake_crewai(monkeypatch)

        class BadWatermarker:
            def embed(self, *args, **kwargs):
                raise RuntimeError("boom")
            registry = None
            keystore = MagicMock()

        crewai_v1.setup_hooks(BadWatermarker(), user_id=42)  # type: ignore

        ctx = FakeHookContext()
        result = crewai_v1._after_llm_call(ctx)
        assert result is None

    def test_clear_hooks(self, monkeypatch):
        """clear_hooks 后 hook 不再嵌入。"""
        crewai_v1 = _install_fake_crewai(monkeypatch)

        wm = Watermarker()
        crewai_v1.setup_hooks(wm, user_id=42)
        crewai_v1.clear_hooks()
        assert crewai_v1._mw is None

        ctx = FakeHookContext()
        assert crewai_v1._after_llm_call(ctx) is None
