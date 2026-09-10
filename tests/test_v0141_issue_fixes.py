"""第 9 轮外部验证问题修复回归测试（v0.14.1）。

对应 GitHub issues：
- #18 UIDRegistry.register() int 别名类型守卫
- #19 trace().uid 归因成功时返回注册库全宽 UID（soft/hard 路径一致）
- #20 embed(uid_redundancy=r) UID 位宽溢出 fail-fast（未显式传 n_bits 时）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.test_cli_server import LONG_TEXT  # noqa: E402


# ======================================================================
# issue #18：register() 类型守卫
# ======================================================================

class TestRegisterTypeGuard:
    def test_int_alias_raises_type_error(self):
        from aawm.plugins.registry import UIDRegistry
        reg = UIDRegistry()
        with pytest.raises(TypeError, match="user_alias"):
            reg.register(1001)
        # 误用不应污染注册库
        assert len(reg) == 0

    def test_int_alias_error_hints_register_uid(self):
        from aawm.plugins.registry import UIDRegistry
        reg = UIDRegistry()
        with pytest.raises(TypeError, match="uid="):
            reg.register(1001)

    def test_resolve_alias_int_also_guarded(self):
        from aawm.plugins.registry import UIDRegistry
        reg = UIDRegistry()
        with pytest.raises(TypeError):
            reg.resolve_alias(1001)

    def test_bool_alias_rejected(self):
        from aawm.plugins.registry import UIDRegistry
        reg = UIDRegistry()
        with pytest.raises(TypeError):
            reg.register(True)  # bool 也不是合法别名

    def test_correct_usage_unaffected(self):
        from aawm.plugins.registry import UIDRegistry
        reg = UIDRegistry()
        # 自动分配
        assert reg.register("张三") == 1
        # 指定 UID
        assert reg.register("李四", uid=1001) == 1001
        assert reg.list_all() == {1: "张三", 1001: "李四"}


# ======================================================================
# issue #19：trace().uid 全宽语义
# ======================================================================

class TestTraceFullWidthUid:
    def _make(self):
        from aawm.plugins.facade import Watermarker
        from aawm.plugins.registry import UIDRegistry
        reg = UIDRegistry()
        reg.register("张三", uid=1001)
        reg.register("李四", uid=2002)
        wm = Watermarker(master_key="42" * 32, registry=reg,
                         codec_mode="zero_cost")
        return wm, reg

    def test_soft_path_returns_full_width_uid(self):
        from tests.test_e2e_integration import _long_zh_text
        wm, _ = self._make()
        # 容量随盐波动，重试直到归因成功（非 abstain）
        for _ in range(8):
            r = wm.embed(_long_zh_text(), user_id=1001)
            t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                         bands=r.bands, n_bits=r.n_bits)
            if t.watermarked and not t.attribution_abstain:
                # 归因成功：uid 必须是注册库全宽 UID，与 user 同一语义层
                assert t.user == "张三"
                assert t.uid == 1001
                return
        pytest.fail("8 次尝试均未归因成功（abstain），测试前提不成立")

    def test_hard_path_returns_full_width_uid(self):
        from tests.test_e2e_integration import _long_zh_text
        wm, _ = self._make()
        for _ in range(8):
            r = wm.embed(_long_zh_text(), user_id=1001)
            t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                         bands=r.bands, n_bits=r.n_bits, soft_match=False)
            if t.watermarked and not t.attribution_abstain:
                # 硬路径同样回填全宽 UID（旧版留在 k-bit 解码空间）
                assert t.user == "张三"
                assert t.uid == 1001
                return
        pytest.fail("8 次尝试均未归因成功（abstain），测试前提不成立")

    def test_unmatched_uid_user_consistent(self):
        # uid/user 语义一致性（issue #19 的核心诉求）：user 非空时
        # uid 必须等于注册库全宽 UID，绝不出现"uid=105 → 张三"式分裂
        from tests.test_e2e_integration import _long_zh_text
        from aawm.plugins.facade import Watermarker
        from aawm.plugins.registry import UIDRegistry
        reg = UIDRegistry()
        reg.register("外人", uid=0x5555)
        wm = Watermarker(master_key="43" * 32, registry=reg,
                         codec_mode="zero_cost")
        r = wm.embed(_long_zh_text(), user_id=1001)
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     bands=r.bands, n_bits=r.n_bits)
        if t.watermarked and not t.attribution_abstain:
            if t.user is not None:
                assert t.user == "外人"
                assert t.uid == 0x5555


# ======================================================================
# issue #20：uid_redundancy 位宽溢出守门
# ======================================================================

class TestRedundancyWidthGuard:
    def test_oversized_uid_raises_without_explicit_nbits(self):
        # 1001 需 10 bit；_long_zh_text 容量 7~14，r=2 下每份最多 7 bit
        # → 任何盐都放不下 → 必抛 ValueError
        from aawm.plugins.facade import Watermarker
        from tests.test_e2e_integration import _long_zh_text
        wm = Watermarker(master_key="44" * 32, codec_mode="zero_cost")
        with pytest.raises(ValueError, match="uid_redundancy"):
            wm.embed(_long_zh_text(), user_id=1001, uid_redundancy=2)

    def test_fitting_uid_still_embeds(self):
        # UID 7（3 bit）在 r=2 下放得下 → 不受守门影响
        from aawm.plugins.facade import Watermarker
        from tests.test_e2e_integration import _long_zh_text
        wm = Watermarker(master_key="45" * 32, codec_mode="zero_cost")
        r = wm.embed(_long_zh_text(), user_id=7, uid_redundancy=2)
        assert r.uid_layout, "可容纳时不应拒绝嵌入"

    def test_explicit_nbits_keeps_truncation_semantics(self):
        # 显式 n_bits=6：保留文档化的"取低 n_bits 位"语义，不抛错。
        # 容量随盐波动（k_uid=k//2 可能 5~7），重试到兑现 6 位为止
        from aawm.plugins.facade import Watermarker
        from tests.test_e2e_integration import _long_zh_text
        wm = Watermarker(master_key="46" * 32, codec_mode="zero_cost")
        r = None
        for _ in range(8):
            cand = wm.embed(_long_zh_text(), user_id=0x1234,
                            uid_redundancy=2, n_bits=6)
            if len(cand.uid_layout) == 6:
                r = cand
                break
        assert r is not None, "8 次尝试仍未兑现 6 位冗余容量"

    def test_no_redundancy_unaffected(self):
        # 非冗余路径的 n_bits 截断是文档化行为，不受守门影响
        from aawm.plugins.facade import Watermarker
        wm = Watermarker(master_key="47" * 32, codec_mode="zero_cost")
        r = wm.embed(LONG_TEXT, user_id=5)
        assert r.user_id == 5
