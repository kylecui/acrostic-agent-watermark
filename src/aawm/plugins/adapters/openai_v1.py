"""OpenAI SDK 适配器：包装客户端，create() 输出自动嵌水印。

OpenAI 官方 SDK（openai>=1.0）是最主流的直接调用方式：
``openai.OpenAI().chat.completions.create(...)``。本适配器不依赖真实
openai 安装（纯 duck-typing 包装），对同步 ``OpenAI`` 与异步
``AsyncOpenAI`` 客户端分别提供 ``wrap_openai_client`` /
``wrap_async_openai_client``，在 create 返回后改写 message.content。

接入方式::

    from openai import OpenAI, AsyncOpenAI
    from aawm.plugins.adapters.openai_v1 import (
        wrap_openai_client, wrap_async_openai_client,
    )

    wm = Watermarker.from_config("key.json", "registry.json")

    client = wrap_openai_client(OpenAI(), wm)       # 同步
    async_client = wrap_async_openai_client(AsyncOpenAI(), wm)  # 异步

    resp = client.chat.completions.create(...)      # content 已嵌水印

与中间件层一致的核心铁律——**Fail-open**：任何嵌入异常都透传原文，
绝不影响 API 响应。

说明：
    - 非流式（stream=False）：改写返回的 ChatCompletion.message.content
    - 流式（stream=True）：包装 stream 对象，逐句缓冲后改写 chunk delta
    - user_id 的解析复用 ContextChain（请求头 / env / contextvars /
      显式 ``user_id=`` 参数），与其余适配器行为一致
"""
from __future__ import annotations

from typing import Any, Optional

from ..context import Context, ContextChain
from ..facade import Watermarker
from ..middleware import WatermarkMiddleware


def _build_middleware(
    watermarker: Watermarker,
    context_chain: Optional[ContextChain],
    *,
    min_text_length: int,
    skip_if_no_context: bool,
) -> WatermarkMiddleware:
    return WatermarkMiddleware(
        watermarker,
        context_chain or ContextChain.default(),
        min_text_length=min_text_length,
        skip_if_no_context=skip_if_no_context,
    )


def _post_process(mw: WatermarkMiddleware, response: Any, kwargs: dict) -> Any:
    """对已完成调用的非流式响应做水印处理（fail-open）。"""
    if not mw.should_embed(response):
        return response
    text = mw.extract_text(response)
    if not text:
        return response
    # create(**kwargs) 可能带 user_id= / headers= 等上下文线索
    ctx = mw.context_chain.resolve(**kwargs)
    marked, _ = mw.transform(text, ctx)
    return mw.write_back(response, marked)


def wrap_openai_client(
    client: Any,
    watermarker: Watermarker,
    context_chain: Optional[ContextChain] = None,
    *,
    min_text_length: int = 50,
    skip_if_no_context: bool = True,
) -> Any:
    """包装同步 ``openai.OpenAI`` 客户端。

    Args:
        client: OpenAI 客户端实例（原地包装，返回同一个对象）
        watermarker: Watermarker 实例
        context_chain: ContextProvider 链（None 用默认三级链）
        min_text_length: 最小嵌入文本长度
        skip_if_no_context: 无 user_id 上下文时是否跳过嵌入

    Returns:
        同一个 client（chat.completions.create 已被包装）
    """
    mw = _build_middleware(watermarker, context_chain,
                           min_text_length=min_text_length,
                           skip_if_no_context=skip_if_no_context)
    original = client.chat.completions.create

    def create(*args: Any, **kwargs: Any) -> Any:
        response = original(*args, **kwargs)
        if kwargs.get("stream"):
            return _wrap_stream(response, mw, kwargs)
        return _post_process(mw, response, kwargs)

    client.chat.completions.create = create  # type: ignore[method-assign]
    return client


def wrap_async_openai_client(
    client: Any,
    watermarker: Watermarker,
    context_chain: Optional[ContextChain] = None,
    *,
    min_text_length: int = 50,
    skip_if_no_context: bool = True,
) -> Any:
    """包装异步 ``openai.AsyncOpenAI`` 客户端。

    用法同 :func:`wrap_openai_client`，包装后 ``await client.chat.
    completions.create(...)`` 的输出自动嵌水印。
    """
    mw = _build_middleware(watermarker, context_chain,
                           min_text_length=min_text_length,
                           skip_if_no_context=skip_if_no_context)
    original = client.chat.completions.create

    async def create(*args: Any, **kwargs: Any) -> Any:
        response = await original(*args, **kwargs)
        if kwargs.get("stream"):
            return _wrap_async_stream(response, mw, kwargs)
        return _post_process(mw, response, kwargs)

    client.chat.completions.create = create  # type: ignore[method-assign]
    return client


# ----------------------------------------------------------------------
# 流式 chunk 包装（同步 / 异步）
# ----------------------------------------------------------------------

def _resolve_ctx(mw: WatermarkMiddleware, kwargs: dict) -> Context:
    """解析嵌入上下文（首次调用后由 StreamingWatermarker 沿用）。"""
    return mw.context_chain.resolve(**kwargs)


def _wrap_stream(raw_stream: Any, mw: WatermarkMiddleware, kwargs: dict) -> Any:
    """包装同步流式 stream 对象，逐句改写 delta。"""
    from ..streaming import StreamingWatermarker

    streamer = StreamingWatermarker(mw)
    ctx = _resolve_ctx(mw, kwargs)

    def _iter() -> Any:
        for chunk in raw_stream:
            delta = _extract_delta(chunk)
            if delta:
                marked = streamer.feed(delta, ctx)
                if marked:
                    _write_delta(chunk, marked)
                    yield chunk
                # 未到句末：chunk 不输出，内容在缓冲区
            else:
                yield chunk
        tail = streamer.flush()
        if tail:
            yield _make_tail_chunk(tail)

    return _iter()


def _wrap_async_stream(raw_stream: Any, mw: WatermarkMiddleware, kwargs: dict) -> Any:
    """包装异步流式 stream 对象，逐句改写 delta。"""
    from ..streaming import StreamingWatermarker

    streamer = StreamingWatermarker(mw)
    ctx = _resolve_ctx(mw, kwargs)

    async def _aiter() -> Any:
        async for chunk in raw_stream:
            delta = _extract_delta(chunk)
            if delta:
                marked = streamer.feed(delta, ctx)
                if marked:
                    _write_delta(chunk, marked)
                    yield chunk
            else:
                yield chunk
        tail = streamer.flush()
        if tail:
            yield _make_tail_chunk(tail)

    return _aiter()


# ----------------------------------------------------------------------
# 内部：流式 chunk 处理
# ----------------------------------------------------------------------

def _extract_delta(chunk: Any) -> str:
    """从流式 chunk 提取 delta 文本。"""
    # OpenAI 格式：chunk.choices[0].delta.content
    choices = WatermarkMiddleware._try_get(chunk, ["choices"])
    if choices and isinstance(choices, list) and len(choices) > 0:
        delta = WatermarkMiddleware._try_get(choices[0], ["delta"])
        if delta is not None:
            content = WatermarkMiddleware._try_get(delta, ["content"])
            if isinstance(content, str):
                return content
    # 通用格式：chunk.content
    content = WatermarkMiddleware._try_get(chunk, ["content"])
    if isinstance(content, str):
        return content
    return ""


def _write_delta(chunk: Any, new_text: str) -> None:
    """把改写后的文本写回 chunk。"""
    choices = WatermarkMiddleware._try_get(chunk, ["choices"])
    if choices and isinstance(choices, list) and len(choices) > 0:
        delta = WatermarkMiddleware._try_get(choices[0], ["delta"])
        if delta is not None:
            try:
                delta.content = new_text
                return
            except (AttributeError, TypeError):
                pass
    try:
        chunk.content = new_text
    except (AttributeError, TypeError):
        pass


def _make_tail_chunk(text: str) -> Any:
    """构造尾 chunk（剩余缓冲的嵌入结果）。"""
    class _TailChunk:
        choices = [{"delta": {"content": text}, "finish_reason": "stop"}]
    return _TailChunk()
