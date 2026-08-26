"""CrewAI 适配器：注册 LLM call hooks，输出自动嵌水印。

CrewAI 提供 LLM Call Hooks：``after_llm_call`` 钩子在每次 LLM 调用返回后
执行，返回字符串即替换该次响应。本适配器注册一个全局 after hook，
对 LLM 输出嵌水印（fail-open：嵌入失败返回 None，保持原响应）。

接入方式::

    from aawm.plugins.adapters.crewai_v1 import setup_hooks

    wm = Watermarker.from_config("key.json", "registry.json")
    setup_hooks(wm, user_id="agent-bob")   # 一次注册，全局生效

    result = crew.kickoff()   # 所有 agent 的 LLM 输出自动嵌水印

user_id 解析优先级（与其余适配器一致）：
    1. ``user_id=`` 显式参数（每套 crew 对应一个身份时最常用）
    2. contextvars（``aawm_user_id``，请求级隔离）
    3. 环境变量 ``AAWM_USER_ID``
    4. 都无 → 跳过嵌入（不嵌入错误身份）

CrewAI 未安装时 import 本模块不报错，但 ``setup_hooks`` 会抛清晰的
ImportError。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ..context import ContextChain
from ..facade import Watermarker
from ..middleware import WatermarkMiddleware

try:
    from crewai.hooks import (  # type: ignore
        clear_all_global_hooks,
        register_after_llm_call_hook,
    )
    _HAS_CREWAI = True
except ImportError:  # pragma: no cover
    _HAS_CREWAI = False
    clear_all_global_hooks = None  # type: ignore[assignment]
    register_after_llm_call_hook = None  # type: ignore[assignment]


# 模块级中间件（hook 函数使用）
_mw: Optional[WatermarkMiddleware] = None
_user_id: Any = None


def setup_hooks(
    watermarker: Watermarker,
    context_chain: Optional[ContextChain] = None,
    *,
    min_text_length: int = 50,
    on_embed: Optional[Callable[[Any, Any], None]] = None,
    user_id: Any = None,
) -> None:
    """注册 CrewAI 全局 after_llm_call hook。

    Args:
        watermarker: Watermarker 实例
        context_chain: ContextProvider 链（None 用默认）
        min_text_length: 最小嵌入文本长度
        on_embed: 嵌入成功回调 ``(EmbedResult, Context)``，用于存档 session_salt
        user_id: 固定用户身份（int UID 或注册库别名）。None 时走
            contextvars / 环境变量；仍无则跳过嵌入
    """
    global _mw, _user_id
    if not _HAS_CREWAI:
        raise ImportError(
            "crewai is not installed. "
            "Install with: pip install 'aawm[crewai]'"
        )
    _mw = WatermarkMiddleware(
        watermarker,
        context_chain or ContextChain.default(),
        min_text_length=min_text_length,
        on_embed=on_embed,
    )
    _user_id = user_id
    register_after_llm_call_hook(_after_llm_call)


def clear_hooks() -> None:
    """清空已注册的全局 hook（测试 / 热更新用）。"""
    if _HAS_CREWAI and clear_all_global_hooks is not None:
        clear_all_global_hooks()
    global _mw, _user_id
    _mw = None
    _user_id = None


def _after_llm_call(context: Any) -> Optional[str]:
    """CrewAI after_llm_call hook：对 LLM 响应嵌水印。

    返回字符串替换响应，返回 None 保持原响应（fail-open）。
    """
    if _mw is None:
        return None

    response = getattr(context, "response", None)
    if not isinstance(response, str) or not response.strip():
        return None

    # 解析上下文：显式 user_id 优先，其次 contextvars/env
    ctx = _mw.context_chain.resolve(
        user_id=_user_id,
        crew=getattr(context, "crew", None),
        agent=getattr(context, "agent", None),
        task=getattr(context, "task", None),
    )
    if ctx is None or not ctx.is_valid():
        return None  # 无有效身份——不嵌入

    marked, _ = _mw.transform(response, ctx)
    if marked == response:
        return None  # 未变化（跳过/失败）→ 保持原响应
    return marked
