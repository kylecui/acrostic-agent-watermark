"""LangChain v1 适配器：AgentMiddleware 实现。

LangChain v1 的 ``AgentMiddleware`` 提供洋葱圈式钩子：
    - ``wrap_model_call``：包裹模型调用（可改入参/出参）
    - ``after_model``：模型输出后、返回 agent 前（可改写 response）
    - ``after_agent``：agent 完成后

本适配器在 ``after_model`` 钩子里给 LLM 输出嵌水印。

接入方式（LangChain v1）::

    from langchain.agents import create_agent
    from aawm.plugins.adapters.langchain_v1 import AAWMMiddleware

    watermarker = Watermarker.from_config("key.json", "registry.json")
    middleware = AAWMMiddleware(watermarker)

    agent = create_agent(
        model=model,
        tools=[...],
        middleware=[middleware],
    )

LangChain v1 未安装时 import 本模块会抛 ImportError。
"""
from __future__ import annotations

from typing import Any, Optional

from ..context import ContextChain
from ..facade import Watermarker
from ..middleware import WatermarkMiddleware

try:
    from langchain.agents.middleware import AgentMiddleware  # type: ignore
    _HAS_LANGCHAIN = True
except ImportError:  # pragma: no cover
    _HAS_LANGCHAIN = False
    AgentMiddleware = object  # type: ignore[misc,assignment]


class AAWMMiddleware(AgentMiddleware):  # type: ignore[misc]
    """LangChain v1 中间件：自动给 agent 输出嵌水印。

    用法::

        middleware = AAWMMiddleware(
            watermarker=Watermarker.from_config("key.json", "registry.json"),
        )
        agent = create_agent(model=model, tools=[...], middleware=[middleware])

    Fail-open：嵌入失败时透传原文，绝不影响 agent 响应。
    """

    def __init__(
        self,
        watermarker: Watermarker,
        context_chain: Optional[ContextChain] = None,
        *,
        min_text_length: int = 50,
        skip_if_no_context: bool = True,
        on_embed: Optional[Any] = None,
    ) -> None:
        if not _HAS_LANGCHAIN:
            raise ImportError(
                "langchain is not installed. "
                "Install with: pip install 'aawm[langchain]'"
            )
        self._mw = WatermarkMiddleware(
            watermarker,
            context_chain or ContextChain.default(),
            min_text_length=min_text_length,
            skip_if_no_context=skip_if_no_context,
            on_embed=on_embed,
        )

    # ------------------------------------------------------------------
    # AgentMiddleware 钩子
    # ------------------------------------------------------------------

    async def after_model(
        self,
        request: Any,
        response: Any,
        handler: Any,
    ) -> Any:
        """模型输出后、返回 agent 前——给输出嵌水印。

        LangChain v1 的 after_model 钩子签名。
        """
        if not self._mw.should_embed(response):
            return response

        text = self._mw.extract_text(response)
        if not text:
            return response

        # 从 request 解析上下文（user_id 等）
        ctx = self._mw.context_chain.resolve(request=request)

        # 嵌入（fail-open）
        marked, _ = self._mw.transform(text, ctx)

        # 写回 response
        return self._mw.write_back(response, marked)

    # ------------------------------------------------------------------
    # 流式支持
    # ------------------------------------------------------------------

    async def after_model_stream(
        self,
        request: Any,
        response: Any,
        handler: Any,
    ) -> Any:
        """流式输出的后处理钩子（如 LangChain v1 支持）。

        用 StreamingWatermarker 逐句重写。
        """
        from ..streaming import StreamingWatermarker

        streamer = StreamingWatermarker(self._mw)
        ctx = self._mw.context_chain.resolve(request=request)

        async for chunk in response:
            # 提取 delta 文本
            delta = self._extract_delta(chunk)
            if delta:
                marked = streamer.feed(delta, ctx)
                if marked:
                    self._write_delta(chunk, marked)
                    yield chunk
            else:
                yield chunk

        tail = streamer.flush()
        if tail:
            yield self._make_tail_chunk(tail)

    # ------------------------------------------------------------------
    # 内部：流式 chunk 处理
    # ------------------------------------------------------------------

    def _extract_delta(self, chunk: Any) -> str:
        """从流式 chunk 提取 delta 文本。"""
        # LangChain AIMessageChunk: chunk.content
        content = self._mw._try_get(chunk, ["content"])
        if isinstance(content, str):
            return content
        # OpenAI 格式
        choices = self._mw._try_get(chunk, ["choices"])
        if choices and isinstance(choices, list) and len(choices) > 0:
            delta = self._mw._try_get(choices[0], ["delta"])
            if delta is not None:
                content = self._mw._try_get(delta, ["content"])
                if isinstance(content, str):
                    return content
        return ""

    def _write_delta(self, chunk: Any, new_text: str) -> None:
        """把重写后的文本写回 chunk。"""
        try:
            chunk.content = new_text
        except (AttributeError, TypeError):
            pass

    def _make_tail_chunk(self, text: str) -> Any:
        """构造尾 chunk（简化实现）。"""
        class _TailChunk:
            content = text
        return _TailChunk()
