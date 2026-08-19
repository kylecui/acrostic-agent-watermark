"""AAWM 插件层：通用 Agent 水印插件。

提供统一的 Facade / 中间件 / 流式 / 框架适配器，
把任意 Agent 的输出自动嵌入用户 ID 水印，事后可复原追溯。

快速开始::

    from aawm.plugins import Watermarker

    wm = Watermarker()
    result = wm.embed(text, user_id=42)
    # 发布 result.watermarked_text

    trace = wm.trace(suspect_text)
    if trace.watermarked:
        print(f"溯源到用户: {trace.user}")
"""
from __future__ import annotations

from .context import (
    Context,
    ContextChain,
    ContextProvider,
    EnvVarContextProvider,
    FrameworkContextProvider,
    HeaderContextProvider,
    reset_user_context,
    set_user_context,
)
from .facade import (
    DetectionThresholds,
    EmbedResult,
    TraceResult,
    Watermarker,
)
from .keystore import KeyStore, generate_key
from .middleware import WatermarkMiddleware
from .registry import UIDRegistry
from .streaming import StreamingWatermarker

__all__ = [
    # Facade
    "Watermarker",
    "EmbedResult",
    "TraceResult",
    "DetectionThresholds",
    # 密钥
    "KeyStore",
    "generate_key",
    # 注册库
    "UIDRegistry",
    # 上下文
    "Context",
    "ContextProvider",
    "ContextChain",
    "FrameworkContextProvider",
    "EnvVarContextProvider",
    "HeaderContextProvider",
    "set_user_context",
    "reset_user_context",
    # 中间件
    "WatermarkMiddleware",
    # 流式
    "StreamingWatermarker",
]
