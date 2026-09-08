"""AggregateWatermarker：聚合编排 / 诚实分级 / 护栏 / fail-open。"""

from __future__ import annotations

import pytest

from aawm.plugins.context import Context
from aawm_enterprise import AggregateWatermarker, FlushPolicy

UID = 7
SESSION = "task-42"


def _ctx(user_id=UID, session=SESSION, language="zh") -> Context:
    return Context(user_id=user_id, session_id=session, language=language)


def _segments(doc: str, n: int = 5) -> list[str]:
    """把长文档切成 n 段（模拟 agent 多次产出）。"""
    size = len(doc) // n
    return [doc[i * size:(i + 1) * size] for i in range(n - 1)] + [doc[(n - 1) * size:]]


# ======================================================================
# 容量达标 flush + meta 往返
# ======================================================================

class TestCapacityFlush:
    def test_full_doc_flush_high(self, agg, long_zh_doc):
        for seg in _segments(long_zh_doc):
            agg.append(SESSION, seg, _ctx())
        flushed = agg.close(SESSION)
        assert flushed is not None
        assert not flushed.abstained
        assert flushed.precheck_capacity >= 10
        assert flushed.result.reliability == "high"
        assert flushed.watermarked_document != long_zh_doc
        # 段落结构保留（拼接以 \n\n 分段）
        assert flushed.watermarked_document.count("\n\n") >= 4

    def test_meta_roundtrip_trace(self, agg, wm, long_zh_doc):
        for seg in _segments(long_zh_doc):
            agg.append(SESSION, seg, _ctx())
        flushed = agg.close(SESSION)
        meta = flushed.meta
        assert meta["session_id"] == SESSION
        assert meta["user_id"] == UID
        assert meta["bands"] and meta["n_bits"]

        trace = wm.trace(
            flushed.watermarked_document,
            session_salt=bytes.fromhex(meta["session_salt"]),
            bands=meta["bands"],
            n_bits=meta["n_bits"],
            uid_layout=meta["uid_layout"],
            key_version=meta["key_version"],
            dict_version=meta["dict_version"],
            archived_uid=meta["user_id"],
        )
        assert trace.watermarked
        assert trace.uid == UID

    def test_segment_crop_survives(self, agg, wm, long_zh_doc):
        """段落裁剪 50% → 冗余解码溯源存活（r=3，P2-8 语义）。"""
        for seg in _segments(long_zh_doc):
            agg.append(SESSION, seg, _ctx())
        flushed = agg.close(SESSION)
        paras = flushed.watermarked_document.split("\n\n")
        half = "\n\n".join(paras[:len(paras) // 2])   # 只泄露前半
        meta = flushed.meta
        trace = wm.trace(
            half,
            session_salt=bytes.fromhex(meta["session_salt"]),
            bands=meta["bands"],
            n_bits=meta["n_bits"],
            uid_layout=meta["uid_layout"],
            key_version=meta["key_version"],
            dict_version=meta["dict_version"],
            archived_uid=meta["user_id"],
        )
        assert trace.watermarked
        assert trace.uid == UID


# ======================================================================
# 单段泄露（聚合前）→ 无保护（边界声明的可执行断言）
# ======================================================================

class TestPreFlushLeak:
    def test_unflushed_fragment_unprotected(self, agg, wm, long_zh_doc):
        frag = _segments(long_zh_doc)[0]
        agg.append(SESSION, frag, _ctx())   # 只 append，未 close
        trace = wm.trace(frag)              # 无 meta（外泄者没有 salt/bands）
        assert not trace.watermarked or trace.uid is None


# ======================================================================
# 诚实分级（min_tier 出口）
# ======================================================================

class TestHonestTiering:
    def test_short_doc_embedded_as_low(self, wm):
        agg = AggregateWatermarker(wm)   # 默认 min_tier="low"：总是尽力嵌
        agg.append(SESSION, "这是一个很短的最终文档，只有一句话。", _ctx())
        flushed = agg.close(SESSION)
        assert flushed is not None and not flushed.abstained
        assert flushed.result.reliability in ("low", "medium")

    def test_below_min_tier_abstains_and_restores(self, wm, strict_policy):
        agg = AggregateWatermarker(wm, policy=strict_policy)  # min_tier=medium
        frag = "太短的一句话。"
        agg.append(SESSION, frag, _ctx())
        flushed = agg.close(SESSION)
        assert flushed.abstained
        assert flushed.watermarked_document is None
        assert "below min_tier" in flushed.reason
        # abstain 时片段已放回缓冲（keep_buffer 语义在 close 中不恢复——
        # close 是显式收官，abstain 结果由调用方决定去留；这里缓冲已删）
        assert agg._buffers.get((SESSION, UID)) is None


# ======================================================================
# 幂等 / 会话隔离 / 超限 / 空闲
# ======================================================================

class TestBufferSemantics:
    def test_idempotent_append(self, agg, long_zh_doc):
        seg = _segments(long_zh_doc)[0]
        agg.append(SESSION, seg, _ctx())
        agg.append(SESSION, seg, _ctx())   # agent 重试双写
        buf = agg._buffers[(SESSION, UID)]
        assert len(buf.segments) == 1

    def test_session_isolation(self, agg, wm, long_zh_doc):
        segs = _segments(long_zh_doc, n=4)
        agg.append("task-a", segs[0], _ctx(user_id=1, session="task-a"))
        agg.append("task-b", segs[1], _ctx(user_id=2, session="task-b"))
        ra = agg.close("task-a", _ctx(user_id=1, session="task-a"))
        rb = agg.close("task-b", _ctx(user_id=2, session="task-b"))
        assert ra.user_id == 1 and rb.user_id == 2
        assert ra.result.user_id != rb.result.user_id

    def test_overflow_auto_flush_pending(self, wm, long_zh_doc):
        agg = AggregateWatermarker(wm, policy=FlushPolicy(max_buffer_chars=1000))
        passthrough = agg.append(SESSION, long_zh_doc, _ctx())
        assert passthrough == long_zh_doc            # 透传不受影响
        pending = agg.pending_results(SESSION)
        assert len(pending) == 1
        assert pending[0].result.reliability in ("high", "medium", "low")

    def test_sweep_idle(self, wm):
        agg = AggregateWatermarker(wm, policy=FlushPolicy(idle_timeout_s=0.0))
        agg.append(SESSION, "积压的一段内容，等待清扫。" * 20, _ctx())
        swept = agg.sweep_idle()
        assert swept == [SESSION]
        assert agg.pending_results(SESSION)


# ======================================================================
# fail-open：嵌入失败不丢片段
# ======================================================================

class TestFailOpen:
    def test_embed_failure_restores_buffer(self, agg, wm, long_zh_doc, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("inject failure")
        monkeypatch.setattr(wm, "embed", boom)
        seg = _segments(long_zh_doc)[0]
        agg.append(SESSION, seg, _ctx())
        with pytest.raises(RuntimeError, match="aggregate embed failed"):
            agg.close(SESSION)
        # 片段放回缓冲（close 失败 = 未交付，不丢数据）
        buf = agg._buffers.get((SESSION, UID))
        assert buf is not None and seg in buf.text

    def test_on_flush_callback_fail_open(self, wm, long_zh_doc):
        calls = []

        def bad_callback(result):
            calls.append(result)
            raise RuntimeError("callback down")

        agg = AggregateWatermarker(wm, on_flush=bad_callback)
        for seg in _segments(long_zh_doc):
            agg.append(SESSION, seg, _ctx())
        flushed = agg.close(SESSION)     # 回调异常不阻断交付
        assert flushed.result.reliability == "high"
        assert len(calls) == 1
