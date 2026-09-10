"""issue #23 修复回归测试：soft 路径真值缺席守门。

场景：embed() 不要求 UID 已注册；嵌入 UID 不在注册库时，旧版 soft
默认路径以高置信错怪注册用户（实测 4/5），hard 路径正确弃权。

修复：
1. trace soft 路径把硬解码值注入候选集竞争——其得分 s(T)=Σ|z| 是
   理论最大值，赢下打分即证明注册库不含信号真身 → 弃权归因；
2. embed 对未注册 int UID 打 UserWarning（提示先注册）。
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.test_e2e_integration import _long_zh_text  # noqa: E402


def _make_wm(key: str = "52"):
    from aawm.plugins.facade import Watermarker
    from aawm.plugins.registry import UIDRegistry
    reg = UIDRegistry()
    reg.register("张三", uid=1001)
    reg.register("李四", uid=2002)
    wm = Watermarker(master_key=key * 32, registry=reg, codec_mode="zero_cost")
    return wm, reg


class TestTruthAbsentGuard:
    def test_unregistered_uid_not_misattributed(self):
        """核心回归：未注册 UID 干净往返，soft 默认路径不得错怪注册用户。

        旧版 5 组独立嵌入 4/5 高置信归因到张三/李四（issue #23 复现表）。
        """
        wm, _ = _make_wm()
        misattributed = []
        for i in range(5):
            r = wm.embed(_long_zh_text(), user_id=7)   # 7 未注册
            t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                         bands=r.bands, n_bits=r.n_bits)
            if t.user is not None:
                misattributed.append((i, t.uid, t.user))
        assert not misattributed, (
            f"真值缺席时错怪注册用户：{misattributed}")

    def test_unregistered_uid_abstains_with_correct_salt(self):
        """持正确盐+bands（非裸扫描）也不得归因——比已文档化裸 API 边界
        更近一步的场景（issue #23 关联节）。"""
        wm, _ = _make_wm()
        r = wm.embed(_long_zh_text(), user_id=7)
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     bands=r.bands, n_bits=r.n_bits)
        if t.watermarked:
            assert t.user is None
            assert t.attribution_abstain or t.uid is None

    def test_registered_uid_still_attributed(self):
        """对照组：嵌入已注册 UID，双路径均正常归因（修复不伤正常路径）。"""
        wm, _ = _make_wm()
        attributed = False
        for _ in range(8):
            r = wm.embed(_long_zh_text(), user_id=1001)
            t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                         bands=r.bands, n_bits=r.n_bits)
            if t.watermarked and not t.attribution_abstain:
                assert t.user == "张三"
                assert t.uid == 1001
                attributed = True
                break
        assert attributed, "已注册 UID 应正常归因"

    def test_redundancy_path_guarded(self):
        """冗余布局路径同样注入硬解码值守门。"""
        wm, _ = _make_wm("53")
        for _ in range(8):
            r = wm.embed(_long_zh_text(), user_id=7, uid_redundancy=2)
            t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                         bands=r.bands, n_bits=r.n_bits,
                         uid_layout=r.uid_layout)
            if t.watermarked:
                assert t.user is None, \
                    f"冗余路径真值缺席仍归因：uid={t.uid} user={t.user}"
                break
        else:
            pytest.fail("8 次尝试均未检出水印，测试前提不成立")

    def test_hard_path_unaffected(self):
        """hard 路径（soft_match=False）行为不变：汉明拒绝 → abstain。"""
        wm, _ = _make_wm()
        r = wm.embed(_long_zh_text(), user_id=7)
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     bands=r.bands, n_bits=r.n_bits, soft_match=False)
        if t.watermarked:
            assert t.user is None


class TestUnregisteredUidWarning:
    def test_embed_warns_for_unregistered_int_uid(self):
        wm, _ = _make_wm("54")
        with pytest.warns(UserWarning, match="未在注册库注册"):
            wm.embed(_long_zh_text(), user_id=7)

    def test_no_warning_for_registered_uid(self):
        wm, _ = _make_wm("55")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            wm.embed(_long_zh_text(), user_id=1001)  # 已注册，不告警

    def test_no_warning_without_registry(self):
        from aawm.plugins.facade import Watermarker
        wm = Watermarker(master_key="56" * 32, codec_mode="zero_cost")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            wm.embed(_long_zh_text(), user_id=7)  # 无注册库无从误归因
