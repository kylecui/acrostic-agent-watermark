"""AggregateWatermarker：会话聚合 → 文档级延迟嵌入的编排器。

职责（设计 §3.2/§4）：
    append 透传累积 → flush/close 时拼整篇 → estimate_capacity 预检（守门员）
    → min_tier 出口 → wm.embed(uid_redundancy=r) → meta 编排 → 交付

铁律：
    - 零算法发明：嵌入/预检/分级全部调用 MIT 核心 Watermarker
    - fail-open：嵌入异常不吞片段——flush 失败把原文放回缓冲，抛给调用方
    - 诚实分级：reliability 如实来自核心，不覆盖、不掩盖
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

from aawm.plugins.context import Context
from aawm.plugins.facade import EmbedResult, Watermarker

from .buffer import SessionBuffer, SessionKey, make_key
from .policy import FlushPolicy

logger = logging.getLogger("aawm_enterprise.aggregator")


@dataclass
class FlushResult:
    """一次 flush/close 的交付。

    Attributes:
        session_id / user_id: 会话与归因用户
        watermarked_document: 整篇水印文档（abstained 时为 None）
        result: 核心 EmbedResult（abstained 时为 None）
        abstained: 预检档位低于 min_tier 时 True（不嵌入）
        precheck_capacity: 预检容量 k
        precheck_tier: 预检可靠性档（high/medium/low）
        reason: abstained 时的人类可读原因
        meta: 溯源元数据（trace 所需全部字段 + 聚合统计）
        flushed_at: flush 时间戳
    """

    session_id: str
    user_id: Union[int, str]
    watermarked_document: Optional[str] = None
    result: Optional[EmbedResult] = None
    abstained: bool = False
    precheck_capacity: int = 0
    precheck_tier: str = "low"
    reason: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    flushed_at: float = field(default_factory=time.time)


class AggregateWatermarker:
    """会话级聚合水印器（aawm-enterprise 组件一）。

    Args:
        watermarker: MIT 核心 Watermarker 实例（建议先 calibrate_corpus 标定）
        policy: FlushPolicy（None → 默认）
        uid_redundancy: UID 冗余份数 r，默认 3（P2-8 段落裁剪存活 5/5）。
            可配置非 hardcode（作者拍板）；r>1 时 UID 位空间 /r。
        on_flush: flush 成功后的回调（存档 meta），fail-open 不阻断。
    """

    def __init__(
        self,
        watermarker: Watermarker,
        *,
        policy: Optional[FlushPolicy] = None,
        uid_redundancy: int = 3,
        on_flush: Optional[Callable[[FlushResult], None]] = None,
    ) -> None:
        if uid_redundancy < 1:
            raise ValueError("uid_redundancy must be >= 1")
        self.wm = watermarker
        self.policy = policy or FlushPolicy()
        self.uid_redundancy = uid_redundancy
        self.on_flush = on_flush
        self._buffers: Dict[SessionKey, SessionBuffer] = {}
        self._pending: Dict[str, List[FlushResult]] = {}

    # ------------------------------------------------------------------
    # 追加（透传，不拖延输出）
    # ------------------------------------------------------------------

    def append(self, session_id: str, text: str, ctx: Context) -> str:
        """追加一个片段，原文透传返回（零干扰）。

        幂等：同片段重复 append 只存一份。缓冲超限（max_buffer_chars）
        时自动 flush，结果进 pending 队列（pending_results 取）。
        """
        if ctx is None or not ctx.is_valid():
            raise ValueError("ctx must carry a valid user_id")
        key = make_key(session_id, ctx.user_id)
        buf = self._buffers.get(key)
        if buf is None:
            buf = self._buffers[key] = SessionBuffer(key=key)
        buf.append(text)

        if buf.char_count >= self.policy.max_buffer_chars:
            logger.info("session %s buffer overflow (%d chars), auto flush",
                        session_id, buf.char_count)
            result = self._flush_buffer(buf, ctx)
            if result is not None:
                self._pending.setdefault(session_id, []).append(result)
        return text

    # ------------------------------------------------------------------
    # flush / close
    # ------------------------------------------------------------------

    def flush(self, session_id: str, ctx: Optional[Context] = None) -> Optional[FlushResult]:
        """把会话缓冲拼成整篇嵌入并清空缓冲（缓冲保留）。

        Returns:
            FlushResult；缓冲为空返回 None。
        Raises:
            RuntimeError: 嵌入失败（片段已放回缓冲，不丢失）。
        """
        buf = self._find(session_id, ctx)
        if buf is None or not buf.segments:
            return None
        return self._flush_buffer(buf, ctx, keep_buffer=False)

    def close(self, session_id: str, ctx: Optional[Context] = None) -> Optional[FlushResult]:
        """收官：flush 并删除会话缓冲。"""
        buf = self._find(session_id, ctx)
        if buf is None:
            # 允许 close 一个只出现在 pending 的会话（超限自动 flush 后）
            return None
        result = self._flush_buffer(buf, ctx, keep_buffer=False)
        self._buffers.pop(buf.key, None)
        return result

    def pending_results(self, session_id: str) -> List[FlushResult]:
        """取走（并清空）该会话超限自动 flush 的结果。"""
        return self._pending.pop(session_id, [])

    # ------------------------------------------------------------------
    # 空闲清扫（兜底收官，外部定时调用；不内置后台线程）
    # ------------------------------------------------------------------

    def sweep_idle(self) -> List[str]:
        """关闭空闲超时的会话（结果进 pending）。返回触发的 session_id 列表。"""
        swept: List[str] = []
        for key, buf in list(self._buffers.items()):
            if buf.idle_seconds >= self.policy.idle_timeout_s:
                result = self._flush_buffer(buf, None, keep_buffer=False)
                if result is not None:
                    self._pending.setdefault(key[0], []).append(result)
                self._buffers.pop(key, None)
                swept.append(key[0])
        return swept

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _find(self, session_id: str, ctx: Optional[Context]) -> Optional[SessionBuffer]:
        if ctx is not None and ctx.is_valid():
            return self._buffers.get(make_key(session_id, ctx.user_id))
        # 未给 ctx：找该 session_id 的唯一缓冲（单用户场景便利）
        matches = [b for k, b in self._buffers.items() if k[0] == session_id]
        return matches[0] if len(matches) == 1 else None

    def _flush_buffer(
        self,
        buf: SessionBuffer,
        ctx: Optional[Context],
        *,
        keep_buffer: bool = True,
    ) -> Optional[FlushResult]:
        text = buf.text
        segments_n = len(buf.segments)
        if not keep_buffer:
            buf.segments.clear()
            buf._seen.clear()  # noqa: SLF001 — 同包内部重置

        if not text.strip():
            return None

        # 1. 预检（守门员）：同一盐下预检容量与 embed 容量可复现一致
        from aawm.keys import generate_session_salt
        salt = generate_session_salt()
        lang = ctx.language if ctx is not None else None
        k = self.wm.estimate_capacity(text, language=lang, session_salt=salt)
        tier = self.wm.reliability_tier(k, weak_embed=False)

        user_id = buf.user_id
        base = FlushResult(
            session_id=buf.key[0], user_id=user_id,
            precheck_capacity=k, precheck_tier=tier,
        )

        # 2. min_tier 出口：预检档位不足 → abstain（不嵌入，不硬拒）
        if not self.policy.tier_admissible(tier):
            base.abstained = True
            base.reason = (
                f"precheck capacity k={k} -> tier={tier} below min_tier="
                f"{self.policy.min_tier}; fragments restored to buffer"
                if keep_buffer else
                f"precheck capacity k={k} -> tier={tier} below min_tier="
                f"{self.policy.min_tier}"
            )
            if keep_buffer:
                # abstain 时片段放回缓冲，等更多内容再试
                restored = SessionBuffer(key=buf.key)
                restored.append(text)
                self._buffers[buf.key] = restored
            self._notify(base)
            return base

        # 3+4. 整篇嵌入：不固定盐——核心 embed 内建 4 次换盐重试（挑
        #   honor + uid_ok + margin>=1.5 的盐），enterprise 只做参数决策：
        #   - n_bits = UID 实际位宽（int 的 bit_length；str 别名交核心自动）
        #   - r_eff = min(请求 r, k // n_bits)，保证 n_bits*r <= k（honor）
        #   预检 k 是随机盐近似值，仅用于 r 决策；嵌入质量由核心重试保证，
        #   weak 仍发生时如实交付（reliability 如实标注，不掩盖）。
        user_id_int = user_id if isinstance(user_id, int) else None
        nbits = max(1, user_id_int.bit_length()) if user_id_int is not None else None
        r_eff = self._decide_redundancy(k, nbits)
        try:
            result = self.wm.embed(
                text, user_id,
                session_salt=None,      # 不固定盐 → 核心内建换盐重试生效
                sign=True,
                language=lang,
                uid_redundancy=r_eff,
                **({"n_bits": nbits} if nbits is not None else {}),
            )
        except Exception as e:
            # fail-open：嵌入失败 = 未交付，片段放回缓冲不丢失
            restored = SessionBuffer(key=buf.key)
            restored.append(text)
            self._buffers[buf.key] = restored
            logger.error("aggregate embed failed for session %s: %s",
                         buf.key[0], e)
            raise RuntimeError(f"aggregate embed failed: {e}") from e

        base.watermarked_document = result.watermarked_text
        base.result = result
        base.meta = self._build_meta(base, result, segments_n, len(text))
        base.meta["aggregate"]["uid_redundancy_requested"] = self.uid_redundancy
        base.meta["aggregate"]["uid_redundancy_effective"] = r_eff
        base.meta["aggregate"]["uid_bits"] = result.n_bits
        if r_eff < self.uid_redundancy:
            logger.warning(
                "session %s: uid_redundancy downgraded %d -> %d "
                "(precheck k=%d, n_bits=%s)", buf.key[0],
                self.uid_redundancy, r_eff, k, result.n_bits)
        self._notify(base)
        return base

    def _decide_redundancy(self, k: int, nbits: Optional[int]) -> int:
        """按预检容量决定实际冗余份数：保证 n_bits*r <= k（nbits 显式时）。"""
        r = self.uid_redundancy
        if nbits is not None:
            budget = k // nbits
            return max(1, min(r, budget)) if budget >= 1 else 1
        return r if k >= r else max(1, k)

    @staticmethod
    def _build_meta(
        fr: FlushResult, result: EmbedResult, segments_n: int, chars: int
    ) -> Dict[str, Any]:
        """trace 所需全部字段的存档视图（设计 §4.4）。"""
        return {
            "session_id": fr.session_id,
            "user_id": result.user_id,
            "user_alias": result.user_alias,
            "session_salt": result.session_salt.hex(),
            "bands": list(result.bands),
            "n_bits": result.n_bits,
            "uid_layout": result.uid_layout,
            "key_version": result.key_version,
            "dict_version": result.dict_version,
            "codec_mode": result.codec_mode,
            "language": result.language,
            "reliability": result.reliability,
            "aggregate": {
                "segments": segments_n,
                "chars": chars,
                "precheck_capacity": fr.precheck_capacity,
                "precheck_tier": fr.precheck_tier,
                "uid_redundancy_note": "set by AggregateWatermarker",
            },
        }

    def _notify(self, result: FlushResult) -> None:
        if self.on_flush is None:
            return
        try:
            self.on_flush(result)
        except Exception as e:  # fail-open：回调异常不阻断交付
            logger.warning("on_flush callback failed: %s", e)
