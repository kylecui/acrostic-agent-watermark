"""proxy_ext：enterprise 会话缓冲端点 + A/S 模式注册表。

部署方在核心 proxy app 上挂载（核心代码零逻辑改动）::

    from aawm_enterprise.proxy_ext import mount_enterprise
    mount_enterprise(app, agg)   # agg: AggregateWatermarker

端点：
    POST /v1/aawm/buffer/{session}          body={"text":..., "user_id":..., "language"?}
    POST /v1/aawm/buffer/{session}/close    → {"watermarked_document", "meta", ...}
    PUT  /v1/aawm/mode/{session}            body={"mode": "aggregate"|"sentence"}

A/S 护栏（设计 §5.2.1）：session 注册为 sentence 后，buffer append 返回 409
（fail-open，绝不双嵌）。请求级 full/sentence 切换由核心 proxy 的
X-AAWM-Watermark-Mode 头 + streamer_factory 注入承担；本注册表提供
会话级显式声明（应用侧自证模式）。

注意：本模块需要 fastapi（`pip install aawm-enterprise[proxy]`）。
Pydantic 请求体模型必须定义在模块级——`from __future__ import annotations`
下函数内定义的模型无法被 FastAPI 的 get_type_hints 解析（会降级成 query
参数），这是本项目实测踩过的坑。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aawm.plugins.context import Context

from .aggregator import AggregateWatermarker

logger = logging.getLogger("aawm_enterprise.proxy_ext")


class AppendBody(BaseModel):
    text: str
    user_id: Union[int, str]
    language: Optional[str] = None


class CloseBody(BaseModel):
    user_id: Optional[Union[int, str]] = None


class ModeBody(BaseModel):
    mode: str


class ModeRegistry:
    """会话级水印模式注册表（A/S 二选一的单向检查）。"""

    def __init__(self) -> None:
        self._modes: Dict[str, str] = {}

    def register(self, session_id: str, mode: str) -> None:
        if mode not in ("aggregate", "sentence"):
            raise ValueError("mode must be 'aggregate' or 'sentence'")
        self._modes[session_id] = mode

    def mode_of(self, session_id: str) -> Optional[str]:
        return self._modes.get(session_id)

    def conflicts(self, session_id: str, wanted: str) -> bool:
        """session 已注册为另一模式 → 冲突。"""
        existing = self._modes.get(session_id)
        return existing is not None and existing != wanted


def create_enterprise_router(agg: AggregateWatermarker):
    """构建 enterprise FastAPI router。"""
    router = APIRouter(prefix="/v1/aawm", tags=["aawm-enterprise"])
    registry = ModeRegistry()

    @router.put("/mode/{session}")
    async def put_mode(session: str, body: ModeBody) -> Dict[str, Any]:
        try:
            registry.register(session, body.mode.lower())
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {"session": session, "mode": body.mode.lower()}

    @router.post("/buffer/{session}")
    async def append_buffer(session: str, body: AppendBody) -> Dict[str, Any]:
        if registry.conflicts(session, "aggregate"):
            # A/S 护栏：该会话已声明 sentence 整流 → 禁止聚合（防双水印）
            raise HTTPException(
                status_code=409,
                detail=f"session '{session}' is registered as sentence mode; "
                       f"aggregate buffer append is forbidden (dual-band guard)",
            )
        ctx = Context(user_id=body.user_id, session_id=session,
                      language=body.language)
        passthrough = agg.append(session, body.text, ctx)
        return {
            "ok": True,
            "session": session,
            "text": passthrough,           # 原文透传（零干扰）
            "mode": registry.mode_of(session) or "aggregate",
        }

    @router.post("/buffer/{session}/close")
    async def close_buffer(session: str,
                           body: Optional[CloseBody] = None) -> Dict[str, Any]:
        if registry.conflicts(session, "aggregate"):
            raise HTTPException(status_code=409,
                                detail="session registered as sentence mode")
        ctx = (Context(user_id=body.user_id, session_id=session)
               if body is not None and body.user_id is not None else None)
        result = agg.close(session, ctx)
        if result is None:
            raise HTTPException(status_code=404,
                                detail=f"no buffered content for session '{session}'")
        return {
            "session": session,
            "abstained": result.abstained,
            "reason": result.reason,
            "watermarked_document": result.watermarked_document,
            "reliability": result.result.reliability if result.result else result.precheck_tier,
            "precheck_capacity": result.precheck_capacity,
            "meta": result.meta,
        }

    return router, registry


def mount_enterprise(app: Any, agg: AggregateWatermarker) -> ModeRegistry:
    """把 enterprise 端点挂到（核心 proxy）FastAPI app 上。返回注册表。"""
    router, registry = create_enterprise_router(agg)
    app.include_router(router)
    return registry
