"""框架无关的水印中间件：所有适配器调用的统一入口。

核心铁律——**Fail-open**：任何嵌入异常都 catch，返回原文。
这是生产中间件的硬约束：水印不能影响 Agent 的正常响应。

用法（适配器内部）::

    mw = WatermarkMiddleware(watermarker, context_chain)
    text = extract_text(response)
    ctx = context_chain.resolve(request=request)
    marked, result = mw.transform(text, ctx)
    if result is not None:
        write_back(response, marked)
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from .context import Context, ContextChain
from .facade import EmbedResult, Watermarker

logger = logging.getLogger("aawm.plugin.middleware")


class WatermarkMiddleware:
    """框架无关的水印中间件。

    所有框架适配器（LangChain / LiteLLM / ...）都调用这个统一入口。
    Fail-open：任何异常→透传原文 + 记录警告。

    Attributes:
        watermarker: Watermarker 实例
        context_chain: ContextProvider 解析链
        min_text_length: 最小文本长度（短于此不嵌入）
        skip_if_no_context: 无有效上下文时是否跳过（True=跳过不嵌入）
    """

    def __init__(
        self,
        watermarker: Watermarker,
        context_chain: Optional[ContextChain] = None,
        *,
        min_text_length: int = 50,
        skip_if_no_context: bool = True,
    ) -> None:
        self.watermarker = watermarker
        self.context_chain = context_chain or ContextChain.default()
        self.min_text_length = min_text_length
        self.skip_if_no_context = skip_if_no_context

    # ------------------------------------------------------------------
    # 核心：嵌入
    # ------------------------------------------------------------------

    def transform(
        self,
        text: str,
        ctx: Optional[Context] = None,
        **ctx_kwargs: Any,
    ) -> Tuple[str, Optional[EmbedResult]]:
        """嵌入水印。Fail-open：失败返回 (原文, None)。

        Args:
            text: 原始文本
            ctx: 上下文（None 时从 ctx_kwargs 解析）
            **ctx_kwargs: 传给 context_chain.resolve 的参数（request=, headers= 等）

        Returns:
            (watermarked_text, EmbedResult or None)
            失败/跳过时返回 (original_text, None)
        """
        if not text or not text.strip():
            return text, None

        # 文本太短不嵌入
        if len(text) < self.min_text_length:
            return text, None

        # 解析上下文
        if ctx is None:
            ctx = self.context_chain.resolve(**ctx_kwargs)
        if not ctx.is_valid():
            if self.skip_if_no_context:
                return text, None
            # 不跳过但无上下文：用 fallback UID 0
            user_id = 0
        else:
            user_id = ctx.user_id

        # Fail-open：嵌入异常绝不影响用户响应
        try:
            result = self.watermarker.embed(
                text,
                user_id=user_id,
                language=ctx.language,
            )
            return result.watermarked_text, result
        except Exception as e:
            logger.warning("watermark embed failed, fail-open to original text: %s", e)
            return text, None

    # ------------------------------------------------------------------
    # 响应判定
    # ------------------------------------------------------------------

    def should_embed(self, response: Any) -> bool:
        """判断响应是否应嵌入水印。

        规则：
            - tool_calls 非空 → False（工具调用参数不应改）
            - finish_reason == "tool_calls" → False
            - 文本为空 → False
            - 否则 → True
        """
        if response is None:
            return False

        # OpenAI 格式：response.choices[0].message.tool_calls
        choices = self._try_get(response, ["choices"])
        if choices and isinstance(choices, list) and len(choices) > 0:
            # 非流式用 message，流式用 delta
            msg = self._try_get(choices[0], ["message"])
            if msg is None:
                msg = self._try_get(choices[0], ["delta"])
            if msg is not None:
                tool_calls = self._try_get(msg, ["tool_calls"])
                if tool_calls:
                    return False
            finish_reason = self._try_get(choices[0], ["finish_reason"])
            if finish_reason == "tool_calls":
                return False

        # LangChain 格式：response.tool_calls
        tool_calls = self._try_get(response, ["tool_calls"])
        if tool_calls:
            return False

        # 检查是否有文本内容
        text = self.extract_text(response)
        if not text or not text.strip():
            return False

        return True

    def extract_text(self, response: Any) -> str:
        """从框架响应对象提取文本。

        支持多种格式：
            - OpenAI: response.choices[0].message.content
            - LangChain: response.content
            - 纯字符串: response 本身
        """
        if isinstance(response, str):
            return response

        # OpenAI 格式
        choices = self._try_get(response, ["choices"])
        if choices and isinstance(choices, list) and len(choices) > 0:
            # 非流式用 message，流式用 delta
            msg = self._try_get(choices[0], ["message"])
            if msg is None:
                msg = self._try_get(choices[0], ["delta"])
            if msg is not None:
                content = self._try_get(msg, ["content"])
                if isinstance(content, str):
                    return content

        # LangChain 格式：response.content
        content = self._try_get(response, ["content"])
        if isinstance(content, str):
            return content

        return ""

    def write_back(self, response: Any, new_text: str) -> Any:
        """把嵌入后的文本写回响应对象（原地修改）。

        支持多种格式：
            - OpenAI: response.choices[0].message.content = new_text
            - LangChain: response.content = new_text
        """
        if isinstance(response, str):
            return new_text  # 字符串场景返回新字符串

        # OpenAI 格式
        choices = self._try_get(response, ["choices"])
        if choices and isinstance(choices, list) and len(choices) > 0:
            msg = self._try_get(choices[0], ["message"])
            if msg is not None:
                try:
                    msg.content = new_text
                    return response
                except (AttributeError, TypeError):
                    pass

        # LangChain 格式
        try:
            response.content = new_text
            return response
        except (AttributeError, TypeError):
            pass

        # 无法写回——返回原文（fail-open）
        logger.warning("cannot write back to response, returning as-is")
        return response

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _try_get(obj: Any, path: list) -> Any:
        for p in path:
            if obj is None:
                return None
            if isinstance(obj, dict):
                obj = obj.get(p)
            else:
                obj = getattr(obj, p, None)
        return obj
