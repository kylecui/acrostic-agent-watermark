"""ContextProvider 协议 + 三级解析链。

水印嵌入需要知道"当前是谁在用 Agent"，这个上下文在不同框架里来源不同：
    - LangChain v1：request.runtime.context["user_id"]
    - LiteLLM Proxy：user_api_key_dict.metadata["user_id"]
    - 自研 Agent：contextvars.ContextVar
    - 代理层：HTTP 请求头 X-AAWM-User-Id

三级解析链按优先级尝试，首个非 None 胜出：
    1. FrameworkContextProvider（框架原生 context）
    2. EnvVarContextProvider（环境变量 / contextvars）
    3. HeaderContextProvider（请求头）

如果都解析不出 user_id，嵌入会被 fail-open 跳过（不嵌入，但透传原文）。
"""
from __future__ import annotations

import contextvars
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Union

# contextvars：自研 Agent 可在请求入口 set，水印中间件 get
_USER_ID: contextvars.ContextVar[Optional[Union[int, str]]] = contextvars.ContextVar(
    "aawm_user_id", default=None
)
_SESSION_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "aawm_session_id", default=None
)
_LANGUAGE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "aawm_language", default=None
)


@dataclass(frozen=True)
class Context:
    """水印嵌入上下文。

    Attributes:
        user_id: 用户标识。int=直接 UID；str=别名（经注册库映射）；None=未指定（跳过嵌入）
        session_id: 会话 ID。用于 session_salt 派生（None→自动生成新 salt）
        language: 语言提示。"en" / "zh" / None（自动检测）
        metadata: 透传的额外元数据
    """

    user_id: Optional[Union[int, str]] = None
    session_id: Optional[str] = None
    language: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_valid(self) -> bool:
        """是否有足够的上下文做嵌入。"""
        return self.user_id is not None

    def language_tag(self) -> bytes:
        """返回 GreenlistCodec 用的 language_tag。"""
        if self.language == "zh":
            return b"zh"
        return b"en"


class ContextProvider(Protocol):
    """ContextProvider 协议：从某种来源解析 Context。"""

    def resolve(self, **kwargs: Any) -> Optional[Context]:
        """尝试解析上下文。解析不出返回 None。"""
        ...


# ----------------------------------------------------------------------
# 三级实现
# ----------------------------------------------------------------------

class FrameworkContextProvider:
    """从框架原生 context 提取 user_id。

    支持的框架 context 形态：
        - LangChain v1：request.runtime.context (dict)
        - LiteLLM Proxy：user_api_key_dict.metadata (dict)
        - 通用 dict：任何含 "user_id" key 的 dict-like 对象
    """

    # 已知的 user_id key 名（按优先级）
    _USER_ID_KEYS = ("user_id", "userId", "user", "uid")
    _SESSION_KEYS = ("session_id", "sessionId", "session")
    _LANG_KEYS = ("language", "lang")

    def resolve(self, **kwargs: Any) -> Optional[Context]:
        # 尝试从各种框架 context 形态提取
        ctx_dict = self._extract_context_dict(kwargs)
        if ctx_dict is None:
            return None
        user_id = self._first_value(ctx_dict, self._USER_ID_KEYS)
        if user_id is None:
            return None
        session_id = self._first_value(ctx_dict, self._SESSION_KEYS)
        language = self._first_value(ctx_dict, self._LANG_KEYS)
        return Context(user_id=user_id, session_id=session_id, language=language)

    def _extract_context_dict(self, kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """从 kwargs 的各种框架形态里提取 context dict。"""
        # LangChain: request=request → request.runtime.context
        request = kwargs.get("request")
        if request is not None:
            ctx = self._try_get(request, ["runtime", "context"])
            if isinstance(ctx, dict):
                return ctx
            # 某些版本直接在 request 上有 context
            if isinstance(request, dict) and "context" in request:
                return request["context"]
        # LiteLLM: user_api_key_dict=user_api_key_dict → .metadata
        uak = kwargs.get("user_api_key_dict")
        if uak is not None:
            meta = self._try_get(uak, ["metadata"])
            if isinstance(meta, dict):
                return meta
        # 直接传 context dict
        context = kwargs.get("context")
        if isinstance(context, dict):
            return context
        # 通用：kwargs 本身可能含 user_id
        if any(k in kwargs for k in self._USER_ID_KEYS):
            return kwargs
        return None

    @staticmethod
    def _try_get(obj: Any, path: List[str]) -> Any:
        for p in path:
            if obj is None:
                return None
            if isinstance(obj, dict):
                obj = obj.get(p)
            else:
                obj = getattr(obj, p, None)
        return obj

    @staticmethod
    def _first_value(d: Dict[str, Any], keys: tuple) -> Any:
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return None


class EnvVarContextProvider:
    """从环境变量 / contextvars 提取。

    环境变量：
        AAWM_USER_ID, AAWM_SESSION_ID, AAWM_LANGUAGE

    contextvars（自研 Agent 在请求入口 set）：
        aawm_user_id, aawm_session_id, aawm_language
    """

    def resolve(self, **kwargs: Any) -> Optional[Context]:
        # 1. contextvars 优先（请求级隔离）
        user_id = _USER_ID.get()
        if user_id is None:
            # 2. 环境变量兜底
            raw = os.environ.get("AAWM_USER_ID")
            if raw is not None:
                user_id = self._parse_user_id(raw)
        if user_id is None:
            return None
        session_id = _SESSION_ID.get() or os.environ.get("AAWM_SESSION_ID")
        language = _LANGUAGE.get() or os.environ.get("AAWM_LANGUAGE")
        return Context(user_id=user_id, session_id=session_id, language=language)

    @staticmethod
    def _parse_user_id(raw: str) -> Union[int, str]:
        """数字→int，否则保留字符串。"""
        raw = raw.strip()
        # hex 格式 0x1234
        if raw.lower().startswith("0x"):
            try:
                return int(raw, 16)
            except ValueError:
                return raw
        try:
            return int(raw)
        except ValueError:
            return raw


class HeaderContextProvider:
    """从 HTTP 请求头提取（代理/sidecar 场景）。

    请求头：
        X-AAWM-User-Id: <int 或 str>
        X-AAWM-Session-Id: <str>
        X-AAWM-Language: en | zh
    """

    _HDR_USER = "X-AAWM-User-Id"
    _HDR_SESSION = "X-AAWM-Session-Id"
    _HDR_LANG = "X-AAWM-Language"

    def resolve(self, **kwargs: Any) -> Optional[Context]:
        headers = kwargs.get("headers")
        if headers is None:
            return None
        # headers 可能是 dict 或 httpx.Headers
        def _get(name: str) -> Optional[str]:
            if hasattr(headers, "get"):
                return headers.get(name) or headers.get(name.lower())
            return None

        raw = _get(self._HDR_USER)
        if not raw:
            return None
        user_id = EnvVarContextProvider._parse_user_id(raw)
        session_id = _get(self._HDR_SESSION)
        language = _get(self._HDR_LANG)
        return Context(user_id=user_id, session_id=session_id, language=language)


# ----------------------------------------------------------------------
# 解析链
# ----------------------------------------------------------------------

class ContextChain:
    """按优先级尝试多个 provider，首个非 None 胜出。

    用法::

        chain = ContextChain.default()  # Framework → EnvVar → Header
        ctx = chain.resolve(request=request)
        if ctx and ctx.is_valid():
            watermarker.embed(text, ctx.user_id)
    """

    def __init__(self, providers: List[ContextProvider]):
        self._providers = list(providers)

    @classmethod
    def default(cls) -> "ContextChain":
        """默认三级链：Framework → EnvVar → Header。"""
        return cls([
            FrameworkContextProvider(),
            EnvVarContextProvider(),
            HeaderContextProvider(),
        ])

    def resolve(self, **kwargs: Any) -> Context:
        """按优先级尝试，返回首个有效的 Context；都失败返回空 Context。"""
        for p in self._providers:
            try:
                ctx = p.resolve(**kwargs)
            except Exception:
                continue
            if ctx is not None and ctx.is_valid():
                return ctx
        return Context()  # 空 context

    def prepend(self, provider: ContextProvider) -> "ContextChain":
        """在链头插入 provider（高优先级）。"""
        return ContextChain([provider] + self._providers)


# ----------------------------------------------------------------------
# contextvars 便捷函数（自研 Agent 用）
# ----------------------------------------------------------------------

def set_user_context(
    user_id: Union[int, str],
    session_id: Optional[str] = None,
    language: Optional[str] = None,
) -> contextvars.Token:
    """在请求入口设置用户上下文（自研 Agent 场景）。

    返回 Token，用 reset_user_context(token) 恢复。
    """
    t1 = _USER_ID.set(user_id)
    t2 = _SESSION_ID.set(session_id)
    t3 = _LANGUAGE.set(language)
    return (t1, t2, t3)  # type: ignore


def reset_user_context(tokens: Any) -> None:
    """恢复 set_user_context 之前的上下文。"""
    t1, t2, t3 = tokens
    _USER_ID.reset(t1)
    _SESSION_ID.reset(t2)
    _LANGUAGE.reset(t3)


def _detect_language(text: str) -> str:
    """简单语言检测：含 CJK 字符→zh，否则 en。"""
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            return "zh"
    return "en"
