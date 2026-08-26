"""AutoGen 适配器：包装 AssistantAgent，输出自动嵌水印。

AutoGen（autogen-agentchat，v0.4+）的 ``AssistantAgent`` 通过
``on_messages()`` 返回 ``Response``，其中 ``response.chat_message.content``
是最终文本。本适配器包装 ``on_messages``：在模型返回后、交还给调用方前，
对 ``content`` 嵌水印（fail-open：嵌入失败透传原文）。

接入方式::

    from autogen_agentchat.agents import AssistantAgent
    from aawm.plugins.adapters.autogen_v1 import wrap_autogen_agent

    wm = Watermarker.from_config("key.json", "registry.json")
    agent = wrap_autogen_agent(AssistantAgent(...), wm, user_id="agent-bob")

    response = await agent.on_messages(messages, cancellation_token)
    # response.chat_message.content 已嵌水印

user_id 解析优先级（与其余适配器一致）：
    1. ``user_id=`` 显式参数（单租户 / 一个 agent 对一个用户时最常用）
    2. contextvars（``aawm_user_id``，请求级隔离）
    3. 环境变量 ``AAWM_USER_ID``
    4. 都无 → 跳过嵌入（不嵌入错误身份）

AutoGen 未安装时 import 本模块不报错，但 ``wrap_autogen_agent`` 会抛
清晰的 ImportError。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ..context import ContextChain
from ..facade import Watermarker
from ..middleware import WatermarkMiddleware

try:
    from autogen_agentchat.agents import AssistantAgent  # type: ignore
    _HAS_AUTOGEN = True
except ImportError:  # pragma: no cover
    _HAS_AUTOGEN = False
    AssistantAgent = object  # type: ignore[misc,assignment]


def _build_middleware(
    watermarker: Watermarker,
    context_chain: Optional[ContextChain],
    *,
    min_text_length: int,
    on_embed: Optional[Callable[[Any, Any], None]],
) -> WatermarkMiddleware:
    return WatermarkMiddleware(
        watermarker,
        context_chain or ContextChain.default(),
        min_text_length=min_text_length,
        on_embed=on_embed,
    )


def wrap_autogen_agent(
    agent: Any,
    watermarker: Watermarker,
    context_chain: Optional[ContextChain] = None,
    *,
    min_text_length: int = 50,
    on_embed: Optional[Callable[[Any, Any], None]] = None,
    user_id: Any = None,
) -> Any:
    """包装 AutoGen ``AssistantAgent``，on_messages 输出自动嵌水印。

    Args:
        agent: AutoGen AssistantAgent 实例（原地包装，返回同一个对象）
        watermarker: Watermarker 实例
        context_chain: ContextProvider 链（None 用默认）
        min_text_length: 最小嵌入文本长度
        on_embed: 嵌入成功回调 ``(EmbedResult, Context)``，用于存档 session_salt
        user_id: 固定用户身份（int UID 或注册库别名）。None 时走
            contextvars / 环境变量；仍无则跳过嵌入

    Returns:
        同一个 agent（on_messages 已被包装）
    """
    if not _HAS_AUTOGEN:
        raise ImportError(
            "autogen-agentchat is not installed. "
            "Install with: pip install 'aawm[autogen]'"
        )
    mw = _build_middleware(watermarker, context_chain,
                           min_text_length=min_text_length,
                           on_embed=on_embed)
    original = agent.on_messages

    async def on_messages(messages: Any, cancellation_token: Any = None) -> Any:
        response = await original(messages, cancellation_token)
        # AutoGen Response.chat_message 是 ChatMessage（有 .content）
        chat_message = getattr(response, "chat_message", None)
        if chat_message is None:
            return response
        text = mw.extract_text(chat_message)
        if not text or not text.strip():
            return response
        # 解析上下文：显式 user_id 优先，其次 contextvars/env
        ctx = mw.context_chain.resolve(
            user_id=user_id, messages=messages, agent=agent,
        )
        if ctx is None or not ctx.is_valid():
            # 无有效身份——跳过嵌入（不嵌入错误身份）
            return response
        marked, _ = mw.transform(text, ctx)
        if marked != text:
            mw.write_back(chat_message, marked)
        return response

    agent.on_messages = on_messages  # type: ignore[method-assign]
    return agent
