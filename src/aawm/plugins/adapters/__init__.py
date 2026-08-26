"""框架适配器子包。

接入 agent 框架的现成适配器：

    - openai_v1    : 包装 openai.OpenAI / AsyncOpenAI 客户端（同步/异步、流式/非流式）
    - langchain_v1 : LangChain v1 AgentMiddleware
    - litellm_proxy: LiteLLM Proxy post-call hooks
    - autogen_v1   : AutoGen (autogen-agentchat) AssistantAgent 包装
    - crewai_v1    : CrewAI LLM call hooks

所有适配器共享同一套 WatermarkMiddleware（fail-open），并支持
``on_embed`` 回调存档 session_salt（中间件嵌入模式下溯源必需）。
"""
from __future__ import annotations

__all__ = [
    "openai_v1",
    "langchain_v1",
    "litellm_proxy",
    "autogen_v1",
    "crewai_v1",
]
