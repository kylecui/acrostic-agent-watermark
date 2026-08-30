"""FastAPI 检测服务端点。

用法::

    from aawm.server.api import create_app, set_watermarker
    from aawm.plugins import Watermarker

    set_watermarker(Watermarker.from_config("key.json", "registry.json"))
    app = create_app()
    # 用 uvicorn.run(app, ...) 启动

或通过 CLI::

    aawm serve --key key.json --registry registry.json --port 8765
"""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel

from ..plugins import Watermarker
from ..audit import suppress_sdk_audit
from .metrics import Metrics

# 模块级 watermarker 单例
_watermarker: Optional[Watermarker] = None

# v0.13 进程级指标（/metrics 端点数据源；reset_watermarker 时一并复位）
_metrics = Metrics()


def set_watermarker(wm: Watermarker) -> None:
    """设置模块级 Watermarker 实例。"""
    global _watermarker
    _watermarker = wm


def reset_watermarker() -> None:
    """重置模块级 Watermarker（测试用）。"""
    global _watermarker, _metrics
    _watermarker = None
    _metrics = Metrics()


def get_metrics() -> Metrics:
    """进程级指标注册表（测试/自定义暴露用）。"""
    return _metrics


# ----------------------------------------------------------------------
# 请求/响应模型
# ----------------------------------------------------------------------

class TraceRequest(BaseModel):
    text: str
    session_salt: Optional[str] = None  # hex
    seal: Optional[dict] = None
    language: Optional[str] = None
    # 自适应路径元数据（embed 返回，需存档回传）
    bands: Optional[List[int]] = None
    n_bits: Optional[int] = None
    archived_uid: Optional[int] = None  # 盐外证据：嵌入时存档 UID（解码交叉校验用）


class TraceResponse(BaseModel):
    watermarked: bool
    uid: Optional[int] = None
    user: Optional[str] = None
    hamming_dist: int = -1
    confidence: float = 0.0
    tampered: Optional[bool] = None
    tampered_paragraphs: List[int] = []
    existence_score: float = 0.0
    n_dict_words: int = 0
    # 自适应路径信息
    codec_mode: str = "default"
    capacity: int = 0
    n_bits: int = 0
    active_bands: int = 0
    soft_uid: Optional[int] = None
    soft_gap: float = -1.0
    # v0.10 归因置信度（独立于存在性 confidence）
    attribution_confidence: float = 0.0
    attribution_abstain: bool = False


class EmbedRequest(BaseModel):
    text: str
    user_id: Any  # int 或 str
    session_salt: Optional[str] = None
    sign: bool = True
    language: Optional[str] = None
    n_bits: Optional[int] = None


class EmbedResponse(BaseModel):
    watermarked_text: str
    session_salt: str
    user_id: int
    user_alias: Optional[str] = None
    has_seal: bool = False
    existence_score: float = 0.0
    # 自适应路径元数据（检测时需回传）
    codec_mode: str = "default"
    bands: List[int] = []
    capacity: int = 0
    n_bits: int = 0
    # v0.10 弱嵌入警示：自检余量 <1.5 时 weak_embed=True（trace 可能漏检）
    margin_ratio: float = 0.0
    weak_embed: bool = False
    # v0.12 可靠性分级（default 模式恒为 high；adaptive 模式按容量分级）：
    # high  - 容量 ≥10（中文约 ≥1200 字），检出与归因均稳定
    # medium- 容量 6-9，检出常存活但归因可能失败
    # low   - 容量 <6 或 weak_embed=True，任何结论仅供参考
    reliability: str = "high"


class FindMetaCandidate(BaseModel):
    """单份候选存档（与 CLI meta.json / proxy salt-archive JSONL 记录字段一致）。"""
    session_salt: str  # hex
    bands: Optional[List[int]] = None
    n_bits: Optional[int] = None
    codec_mode: Optional[str] = None
    seal: Optional[dict] = None
    label: Optional[str] = None  # 调用方标记（如文件名/行号）
    archived_uid: Optional[int] = None  # 盐外证据：嵌入时的存档 UID（解码交叉校验用）


class FindMetaRequest(BaseModel):
    text: str
    candidates: List[FindMetaCandidate]
    language: Optional[str] = None
    max_trace: int = 10  # 信道 B 验证最多尝试的候选数


class FindMetaResponse(BaseModel):
    watermarked: bool
    matched_index: Optional[int] = None
    matched_label: Optional[str] = None
    uid: Optional[int] = None
    user: Optional[str] = None
    hamming_dist: int = -1
    confidence: float = 0.0
    existence_score: float = 0.0
    tampered: Optional[bool] = None
    tampered_paragraphs: List[int] = []
    # 段哈希匹配（免密钥定位信号）
    para_overlap: int = 0
    para_total: int = 0
    # v0.10 归因置信度：abstain 时 uid/user 置 None（"不可判定"）
    attribution_confidence: float = 0.0
    attribution_abstain: bool = False


# ----------------------------------------------------------------------
# app
# ----------------------------------------------------------------------

def create_app():
    """创建 FastAPI app。"""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import PlainTextResponse

    from ..audit import audit, text_fingerprint

    app = FastAPI(title="AAWM Watermark Service", version="0.13.1")

    @app.get("/v1/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "watermarker_initialized": _watermarker is not None,
        }

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        """Prometheus 文本格式指标（P1-4 可观测性）。

        嵌入量/weak_embed 率/abstain 率/检索延迟——产品健康度核心指标。
        """
        return PlainTextResponse(
            _metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.post("/v1/trace", response_model=TraceResponse)
    async def trace(req: TraceRequest) -> TraceResponse:
        if _watermarker is None:
            raise HTTPException(status_code=503, detail="watermarker not initialized")

        session_salt = bytes.fromhex(req.session_salt) if req.session_salt else None
        seal = None
        if req.seal:
            from ..binding import BindingSeal
            seal = BindingSeal(
                merkle_root=bytes.fromhex(req.seal["merkle_root"]),
                para_hashes=[bytes.fromhex(h) for h in req.seal["para_hashes"]],
                aad=bytes.fromhex(req.seal.get("aad", "")),
                version=req.seal.get("version", 1),
            )

        with _metrics.time_it("aawm_request_latency_seconds", op="trace"):
            # suppress_sdk_audit：trace 请求由本 handler 自行审计
            # （下方 audit(source=server)），避免 facade 再写一条 SDK 事件。
            with suppress_sdk_audit():
                result = _watermarker.trace(
                    req.text,
                    session_salt=session_salt,
                    seal=seal,
                    language=req.language,
                    bands=req.bands,
                    n_bits=req.n_bits,
                    archived_uid=req.archived_uid,
                )
        _metrics.inc("aawm_trace_requests_total")
        _metrics.inc("aawm_text_chars_total", len(req.text), op="trace")
        if result.watermarked:
            _metrics.inc("aawm_trace_watermarked_total")
        if result.attribution_abstain:
            _metrics.inc("aawm_trace_abstain_total")
        audit({
            "op": "trace", "source": "server",
            "text_sha256": text_fingerprint(req.text),
            "text_chars": len(req.text),
            "watermarked": result.watermarked,
            "uid": result.uid,
            "user": result.user,
            "attribution_abstain": result.attribution_abstain,
            "attribution_confidence": round(result.attribution_confidence, 4),
            "confidence": round(result.confidence, 4),
            "existence_score": round(result.existence_score, 2),
        })

        return TraceResponse(
            watermarked=result.watermarked,
            uid=result.uid,
            user=result.user,
            hamming_dist=result.hamming_dist,
            confidence=result.confidence,
            tampered=result.tampered,
            tampered_paragraphs=result.tampered_paragraphs,
            existence_score=result.existence_score,
            n_dict_words=result.n_dict_words,
            codec_mode=result.codec_mode,
            capacity=result.capacity,
            n_bits=result.n_bits,
            active_bands=result.active_bands,
            soft_uid=result.soft_uid,
            soft_gap=result.soft_gap,
            attribution_confidence=result.attribution_confidence,
            attribution_abstain=result.attribution_abstain,
        )

    @app.post("/v1/embed", response_model=EmbedResponse)
    async def embed(req: EmbedRequest) -> EmbedResponse:
        if _watermarker is None:
            raise HTTPException(status_code=503, detail="watermarker not initialized")

        session_salt = bytes.fromhex(req.session_salt) if req.session_salt else None
        with _metrics.time_it("aawm_request_latency_seconds", op="embed"):
            # 同 /v1/trace：embed 请求由本 handler 自行审计，抑制 facade SDK 审计
            with suppress_sdk_audit():
                result = _watermarker.embed(
                    req.text,
                    user_id=req.user_id,
                    session_salt=session_salt,
                    sign=req.sign,
                    language=req.language,
                    n_bits=req.n_bits,
                )
        _metrics.inc("aawm_embed_requests_total", reliability=result.reliability)
        _metrics.inc("aawm_text_chars_total", len(req.text), op="embed")
        if result.weak_embed:
            _metrics.inc("aawm_embed_weak_total")
        audit({
            "op": "embed", "source": "server",
            "text_sha256": text_fingerprint(req.text),
            "text_chars": len(req.text),
            "user_id": result.user_id,
            "user_alias": result.user_alias,
            "reliability": result.reliability,
            "weak_embed": result.weak_embed,
            "capacity": result.capacity,
            "n_bits": result.n_bits,
        })

        return EmbedResponse(
            watermarked_text=result.watermarked_text,
            session_salt=result.session_salt.hex(),
            user_id=result.user_id,
            user_alias=result.user_alias,
            has_seal=result.seal is not None,
            existence_score=result.existence_score,
            codec_mode=result.codec_mode,
            bands=result.bands,
            capacity=result.capacity,
            n_bits=result.n_bits,
            margin_ratio=result.margin_ratio,
            weak_embed=result.weak_embed,
            reliability=result.reliability,
        )

    @app.post("/v1/find-meta", response_model=FindMetaResponse)
    async def find_meta(req: FindMetaRequest) -> FindMetaResponse:
        """为嫌疑文本在候选存档中找出匹配的一份。

        两级策略（与 CLI `aawm find-meta` 一致）：
        1. 段落哈希匹配（免密钥）：候选 seal.para_hashes 与嫌疑文本
           段落 SHA256 求交集排序
        2. 信道 B 验证：逐候选用其 salt+bands 解码（用服务端配置的
           codec；异模式嵌入的候选仅靠段哈希定位）
        """
        if _watermarker is None:
            raise HTTPException(status_code=503, detail="watermarker not initialized")
        import hashlib
        import time

        from ..audit import audit, text_fingerprint
        from ..binding import split_paragraphs

        t0 = time.perf_counter()

        def _finish_findmeta(kind: str, label, uid, user) -> None:
            _metrics.observe(
                "aawm_request_latency_seconds",
                time.perf_counter() - t0, op="find_meta")
            _metrics.inc("aawm_findmeta_requests_total", result=kind)
            audit({
                "op": "find_meta", "source": "server",
                "text_sha256": text_fingerprint(req.text),
                "text_chars": len(req.text),
                "result": kind,
                "matched_label": label,
                "uid": uid,
                "user": user,
                "n_candidates": len(req.candidates),
            })

        paras = split_paragraphs(req.text)
        text_hashes = {hashlib.sha256(p.encode("utf-8")).digest() for p in paras}

        ranked = []  # (overlap, index, candidate)
        for i, cand in enumerate(req.candidates):
            phs = set()
            if cand.seal and cand.seal.get("para_hashes"):
                phs = {bytes.fromhex(h) for h in cand.seal["para_hashes"]}
            ranked.append((len(text_hashes & phs), i, cand))
        ranked.sort(key=lambda r: -r[0])

        # 信道 B：优先验证段哈希命中的候选；收集全部检出再裁决
        # （不"只取第一个检出"——攻击下存在性盐无关，错误盐也会"检出"，
        #  必须用段哈希证据 + 存档 UID 交叉校验后才可归因）
        # 候选逐条 trace 的审计由本 handler 统一写（find_meta 事件），
        # 抑制 facade 对每条候选的 SDK 审计，避免噪声。
        to_try = [r for r in ranked if r[0] > 0] or ranked
        detections = []  # (overlap, index, t, archived_uid)
        with suppress_sdk_audit():
            for k, (overlap, i, cand) in enumerate(to_try):
                if k >= req.max_trace:
                    break
                seal = None
                if cand.seal:
                    from ..binding import BindingSeal
                    seal = BindingSeal(
                        merkle_root=bytes.fromhex(cand.seal["merkle_root"]),
                        para_hashes=[bytes.fromhex(h) for h in cand.seal["para_hashes"]],
                        aad=bytes.fromhex(cand.seal.get("aad", "")),
                        version=cand.seal.get("version", 1),
                    )
                t = _watermarker.trace(
                    req.text,
                    session_salt=bytes.fromhex(cand.session_salt),
                    seal=seal,
                    language=req.language,
                    bands=cand.bands,
                    n_bits=cand.n_bits,
                )
                if t.watermarked:
                    detections.append((overlap, i, t, cand.archived_uid))

        # 最终裁决（与 CLI aawm find-meta 同规则）：段哈希内容证据优先 +
        # 解码 UID 与存档交叉校验；不确定即 abstain，不输出可能错误的 UID。
        from ..cli import _adjudicate_find_meta
        kind, label, t, reason = _adjudicate_find_meta(
            [(ov, 0, i, cand) for ov, i, cand in ranked],
            detections)

        def _find_cand(idx):
            return next((c for ov, i, c in ranked if i == idx), None)

        def _overlap(idx):
            return next((ov for ov, i, c in ranked if i == idx), 0)

        if kind == "none":
            best_overlap, best_i, best_cand = ranked[0] if ranked else (0, None, None)
            _finish_findmeta("none", best_cand.label if best_overlap > 0 else None,
                             None, None)
            return FindMetaResponse(
                watermarked=False,
                matched_index=best_i if best_overlap > 0 else None,
                matched_label=best_cand.label if best_overlap > 0 else None,
                para_overlap=best_overlap,
                para_total=len(paras),
            )
        if kind == "abstain":
            cand = _find_cand(label) if label is not None else None
            _finish_findmeta("abstain",
                             cand.label if cand else None, None, None)
            return FindMetaResponse(
                watermarked=bool(t and t.watermarked),
                matched_index=label,
                matched_label=cand.label if cand else None,
                uid=None,
                user=None,
                hamming_dist=t.hamming_dist if t else -1,
                confidence=t.confidence if t else 0.0,
                existence_score=t.existence_score if t else 0.0,
                tampered=t.tampered if t else None,
                tampered_paragraphs=list(t.tampered_paragraphs) if t else [],
                para_overlap=_overlap(label) if label is not None else 0,
                para_total=len(paras),
                attribution_confidence=t.attribution_confidence if t else 0.0,
                attribution_abstain=True,
            )

        cand = _find_cand(label)
        _finish_findmeta("match", cand.label if cand else None, t.uid, t.user)
        return FindMetaResponse(
            watermarked=True,
            matched_index=label,
            matched_label=cand.label if cand else None,
            uid=t.uid,
            user=t.user,
            hamming_dist=t.hamming_dist,
            confidence=t.confidence,
            existence_score=t.existence_score,
            tampered=t.tampered,
            tampered_paragraphs=t.tampered_paragraphs,
            para_overlap=_overlap(label),
            para_total=len(paras),
            attribution_confidence=t.attribution_confidence,
            attribution_abstain=False,
        )

    return app
