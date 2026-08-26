"""LiteLLM Proxy 适配器：post-call hooks 实现。

LiteLLM Proxy 提供两个改写钩子：
    - ``async_post_call_success_hook``：非流式，可改写 response
    - ``async_post_call_streaming_iterator_hook``：流式，可改写流

本适配器在这两个钩子里给 LLM 输出嵌水印。

接入方式（LiteLLM Proxy 配置文件）::

    # proxy_config.yaml
    litellm_settings:
      callbacks: aawm_proxy_hooks

    # 或在 Python 中
    from aawm.plugins.adapters.litellm_proxy import setup_hooks
    setup_hooks(watermarker)

LiteLLM 未安装时本模块仍可 import（hooks 函数签名与 LiteLLM 解耦），
但实际接入需要 LiteLLM Proxy 运行时。
"""
from __future__ import annotations

from typing import Any, Optional

from ..context import ContextChain
from ..facade import Watermarker
from ..middleware import WatermarkMiddleware

# 模块级单例（setup_hooks 设置后供 hook 函数使用）
_mw: Optional[WatermarkMiddleware] = None
_chain: Optional[ContextChain] = None


def setup_hooks(
    watermarker: Watermarker,
    context_chain: Optional[ContextChain] = None,
    *,
    min_text_length: int = 50,
    on_embed: Optional[Any] = None,
) -> None:
    """初始化水印中间件（在 LiteLLM Proxy 启动时调用）。

    Args:
        watermarker: Watermarker 实例
        context_chain: ContextProvider 链（None 用默认）
        min_text_length: 最小嵌入文本长度
        on_embed: 嵌入成功回调 ``(EmbedResult, Context)``，用于存档 session_salt
    """
    global _mw, _chain
    _mw = WatermarkMiddleware(
        watermarker,
        context_chain or ContextChain.default(),
        min_text_length=min_text_length,
        on_embed=on_embed,
    )
    _chain = _mw.context_chain


# ----------------------------------------------------------------------
# LiteLLM Proxy hook 函数
# ----------------------------------------------------------------------

async def async_post_call_success_hook(
    data: dict,
    user_api_key_dict: Any,
    response: Any,
) -> Any:
    """非流式：改写 response.choices[0].message.content。

    LiteLLM Proxy 的 post-call 钩子签名。
    """
    if _mw is None:
        return response  # 未初始化，透传

    if not _mw.should_embed(response):
        return response

    text = _mw.extract_text(response)
    if not text:
        return response

    # 从 user_api_key_dict.metadata 解析上下文
    ctx = _chain.resolve(user_api_key_dict=user_api_key_dict)

    # 嵌入（fail-open）
    marked, _ = _mw.transform(text, ctx)

    # 写回 response
    return _mw.write_back(response, marked)


async def async_post_call_streaming_iterator_hook(
    data: dict,
    user_api_key_dict: Any,
    response: Any,
):
    """流式：用 StreamingWatermarker 逐句重写。

    LiteLLM Proxy 的流式后处理钩子。yield 改写后的 chunks。
    """
    if _mw is None:
        async for chunk in response:
            yield chunk
        return

    from ..streaming import StreamingWatermarker

    streamer = StreamingWatermarker(_mw)
    ctx = _chain.resolve(user_api_key_dict=user_api_key_dict)

    async for chunk in response:
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
    # LangChain 格式
    content = WatermarkMiddleware._try_get(chunk, ["content"])
    if isinstance(content, str):
        return content
    return ""


def _write_delta(chunk: Any, new_text: str) -> None:
    """把重写后的文本写回 chunk。"""
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
    """构造尾 chunk。"""
    class _TailChunk:
        choices = [{"delta": {"content": text}, "finish_reason": "stop"}]
    return _TailChunk()
