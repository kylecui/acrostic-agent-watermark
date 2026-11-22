"""v0.15 质量通行证回归测试。

覆盖本轮质量修复的每一项，防止回退：
1. 重复词去相关（PER_WORD_CAP）：无水印模板文不能靠单词重复伪造存在性信号
2. 全大写保留：embed 不再把 "SYSTEM" 退化成 "System"
3. 派生盐找回：meta 散失时 trace 可从嫌疑文本重算盐恢复溯源
4. 证据掩码存档校验：置位带无词典词 != 失真（无攻击不弃权）
5. 候选盐扫描多重比较校正：null 文本扫描路径不误报
"""
from __future__ import annotations

import random

from aawm.greenlist import (
    DEFAULT_MIN_N,
    PER_WORD_CAP,
    GreenlistCodec,
    build_zero_cost_en_codec,
    build_zero_cost_zh_codec,
)
from aawm.keys import derive_session_salt
from aawm.plugins import Watermarker

KEY = bytes(range(32))
SALT = bytes(range(16))

ZH_TEXT = (
    "项目管理不仅需要规划，而且需要持续跟踪。团队成员应当明确目标，"
    "并且在执行过程中及时调整。质量标准必须统一，因此沟通非常重要。"
    "风险控制不但要有预案，而且要有复盘机制。我们应当记录每个决策。"
) * 6


def _demo_corpus_text():
    """真实散文语料（demo_corpus 全文拼接）——非重复、非词典密集的
    正常 null/嵌入文本。注意 ZH_TEXT 那类'短句复制+零感词饱和'文本
    是病态 null：默认阈值下候选盐扫描会对它误报（跨域 FPR 报告
    docs/cross_domain_fpr_report.md 的实测案例），不能当正常文本用。"""
    import glob
    import os

    base = os.path.join(os.path.dirname(__file__), "..",
                        "src", "aawm", "data", "demo_corpus")
    parts = []
    for f in sorted(glob.glob(os.path.join(base, "*.md"))):
        with open(f, encoding="utf-8") as fh:
            parts.append(fh.read())
    return "\n\n".join(parts)


class TestPerWordCap:
    """v0.15 统计缺陷修复：同一词重复贡献完全相关票，z 线性膨胀。"""

    def test_repeated_word_cannot_forge_existence(self):
        """攻击复现：无水印文本把单个词典词重复 160 次。

        v0.15 前 Σ|z| 达 12.65（真水印量级，可越阈）；封顶后必须
        显著低于默认存在性阈值。
        """
        codec = build_zero_cost_zh_codec(KEY, SALT)
        word = sorted(codec._w2band)[0]
        attack = "".join(f"我们要重视{word}建设。" for _ in range(160))
        report = codec.detect(attack, min_n=1)
        assert report.existence_score < 8.0, (
            f"重复词伪造信号回归：Σ|z|={report.existence_score:.2f}"
        )

    def test_normal_embed_signal_preserved(self):
        """封顶不影响正常文本的嵌入信号（回归护栏：别修过头）。"""
        codec = build_zero_cost_zh_codec(KEY, SALT)
        marked = codec.embed(ZH_TEXT, 0x1, rng=random.Random(7))
        report = codec.detect(marked, min_n=1)
        assert report.existence_score >= 6.0

    def test_cap_is_three(self):
        """封顶值语义锁定：PER_WORD_CAP=3（调参须同步阈值重标定）。"""
        assert PER_WORD_CAP == 3

    def test_min_n_constants(self):
        """硬判决/软判决 min_n 默认值是有意的实验结论（常量化锁定）。"""
        assert DEFAULT_MIN_N == 2
        codec = GreenlistCodec(KEY, SALT)
        assert codec.detect.__kwdefaults__["min_n"] == DEFAULT_MIN_N
        assert codec.soft_match.__kwdefaults__["min_n"] == 1


class TestCasePreservation:
    """v0.15 大小写修复：全大写原文不再被 capitalize 退化。"""

    def test_all_caps_preserved(self):
        codec = build_zero_cost_en_codec(KEY, SALT)
        # uid 全 1：所有带目标 green，非绿词必被替换——替换须保大写
        words = sorted(codec._w2band)[:8]
        text = " ".join(w.upper() for w in words)
        marked = codec.embed(text, 0xFFFF, rng=random.Random(3))
        for original, new in zip(text.split(), marked.split()):
            if original != new:
                assert new.isupper(), f"大写退化: {original} -> {new}"

    def test_title_case_uses_capitalize(self):
        codec = build_zero_cost_en_codec(KEY, SALT)
        words = sorted(codec._w2band)[:8]
        text = " ".join(w.capitalize() for w in words)
        marked = codec.embed(text, 0xFFFF, rng=random.Random(3))
        for original, new in zip(text.split(), marked.split()):
            if original != new:
                assert new[:1].isupper() and not new.isupper(), (
                    f"首字母大写风格破坏: {original} -> {new}"
                )


class TestDerivedSaltRecovery:
    """v0.15 派生盐：meta 散失后 trace 从嫌疑文本找回盐。"""

    def _make_wm(self):
        return Watermarker(master_key=KEY, codec_mode="zero_cost")

    def test_trace_without_salt_recovers(self):
        from aawm.plugins.registry import UIDRegistry
        reg = UIDRegistry()
        reg.register("u7", uid=7)
        wm = Watermarker(master_key=KEY, codec_mode="zero_cost",
                         registry=reg)
        r = wm.embed(ZH_TEXT, user_id=7)
        # 不传 session_salt / bands / seal——模拟 meta 全部散失
        t = wm.trace(r.watermarked_text)
        assert t.watermarked, "派生盐找回失败：无 meta 应可溯源"
        # 无 meta → 无 bands 元数据 → 解码在文本自身活动带的 k-bit 空间，
        # 注册库 soft_match 映射回全宽 UID（自适应截断语标的文档行为）
        assert t.user == "u7"
        assert t.uid == 7

    def test_trace_without_salt_no_registry_gives_kbit(self):
        """无注册库时无 meta 解码给出 k-bit 截断值（文档化语义）。"""
        wm = self._make_wm()
        r = wm.embed(ZH_TEXT, user_id=7)
        t = wm.trace(r.watermarked_text)
        assert t.watermarked
        if t.n_bits:
            assert t.uid == 7 & ((1 << t.n_bits) - 1)

    def test_trace_with_meta_still_works(self):
        wm = self._make_wm()
        r = wm.embed(ZH_TEXT, user_id=42)
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     bands=r.bands, seal=r.seal)
        assert t.watermarked
        assert t.uid == 42
        assert t.tampered is False

    def test_derived_salt_is_deterministic(self):
        """同一 key+text 派生同一盐（可复现性）；nonce 区分重试。"""
        s0 = derive_session_salt(KEY, ZH_TEXT, nonce=0)
        s0b = derive_session_salt(KEY, ZH_TEXT, nonce=0)
        s1 = derive_session_salt(KEY, ZH_TEXT, nonce=1)
        assert s0 == s0b
        assert s0 != s1
        assert len(s0) == 16

    def test_salt_candidates_cover_embed_nonces(self):
        """_salt_candidates 必须覆盖 embed 自检重试的全部 nonce，
        否则重试选中的盐在 meta 散失后无法找回。"""
        wm = self._make_wm()
        cands = wm._salt_candidates(ZH_TEXT, KEY, b"zh")
        canon = wm._canonical_text(ZH_TEXT, b"zh")
        embed_nonces = {derive_session_salt(KEY, canon, nonce=n)
                        for n in range(6)}
        assert embed_nonces.issubset(set(cands))

    def test_canonicalize_is_embed_invariant(self):
        """派生盐根基：canonicalize(原文) == canonicalize(水印文本)。"""
        wm = self._make_wm()
        r = wm.embed(ZH_TEXT, user_id=3)
        c0 = wm._canonical_text(ZH_TEXT, b"zh")
        c1 = wm._canonical_text(r.watermarked_text, b"zh")
        assert c0 == c1, "嵌入不变性破坏：trace 将无法重算嵌入盐"


class TestEvidenceMask:
    """v0.15 证据掩码：无词带的默认 bit=0 是'无证据'而非'矛盾'。"""

    def test_missing_bit_band_not_distortion(self):
        """置位带恰好无词典词时，无攻击往返不得因存档校验弃权。"""
        wm = Watermarker(master_key=KEY, codec_mode="default")
        text = (
            "The team released a new version of the platform this week. "
            "Developers reported significant improvements in performance "
            "and stability across all supported environments and regions. "
        ) * 3
        # 选一个'在文本中有词带的 UID'：先探测哪些带有词
        probe = wm._build_codec(
            derive_session_salt(KEY, wm._canonical_text(text, b"en"), nonce=0),
            b"en")
        rep = probe.detect(text, min_n=1)
        active = [st.band for st in rep.bands if st.has_signal]
        assert active, "测试文本需至少命中一个带"
        uid = 1 << active[0]
        r = wm.embed(text, user_id=uid)
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     archived_uid=uid)
        assert t.watermarked
        assert not t.attribution_abstain, (
            "证据掩码回归：无攻击场景因无词带弃权"
        )
        assert t.uid == uid
        # 证据掩码应只标记有词带
        assert t.evidence_mask & (1 << active[0])

    def test_evidence_conflict_still_abstains(self):
        """有证据带内与存档 UID 矛盾 → 仍必须弃权（攻击防护不回退）。"""
        wm = Watermarker(master_key=KEY, codec_mode="zero_cost")
        r = wm.embed(ZH_TEXT, user_id=1)
        # 翻转一个已归档 UID 的置位（与嵌入真值矛盾）
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     bands=r.bands, archived_uid=r.user_id ^ 0x1)
        # uid=1 的 bit0 带必有词（容量>=1 才嵌入成功），矛盾在证据带内
        assert t.attribution_abstain or t.uid != (r.user_id ^ 0x1)


class TestSaltScanCorrection:
    """候选盐扫描的多重比较校正：null 文本不误报。"""

    def test_null_text_not_flagged_under_scan(self):
        wm = Watermarker(master_key=KEY, codec_mode="zero_cost")
        # null 文本（真实散文，未嵌入），不传盐走候选扫描路径
        t = wm.trace(_demo_corpus_text())
        assert not t.watermarked

    def test_marked_text_detected_under_scan(self):
        wm = Watermarker(master_key=KEY, codec_mode="zero_cost")
        r = wm.embed(ZH_TEXT, user_id=7)
        t = wm.trace(r.watermarked_text)  # 无盐候选扫描
        assert t.watermarked, "扫描+校正后真水印应仍可检出（余量需 >=1.3x）"


class TestConfidenceSemantics:
    """v0.15 confidence：阈值相对归一化，magic 常数 40 退役。"""

    def test_confidence_threshold_relative(self):
        wm = Watermarker(master_key=KEY, codec_mode="zero_cost")
        r = wm.embed(ZH_TEXT, user_id=3)
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     bands=r.bands)
        assert t.watermarked
        # 判决阈值处 = 1/3，3x阈值 = 1：检出文本应在 [1/3, 1]
        assert 1.0 / 3.0 - 1e-6 <= t.confidence <= 1.0

    def test_confidence_below_threshold_is_sub_third(self):
        """null 文本 confidence < 1/3（阈下线性衰减是设计行为，非概率）。"""
        wm = Watermarker(master_key=KEY, codec_mode="zero_cost")
        t = wm.trace(_demo_corpus_text())  # null
        assert not t.watermarked
        assert 0.0 <= t.confidence < 1.0 / 3.0


class TestCalibrationGuards:
    """v0.15.1 标定护栏（跨域 FPR 实测教训的工程化）。"""

    def test_small_corpus_warns(self):
        import warnings as warn_mod
        wm = Watermarker(master_key=KEY, codec_mode="zero_cost")
        with warn_mod.catch_warnings(record=True) as w:
            warn_mod.simplefilter("always")
            wm.calibrate_null_model([ZH_TEXT, ZH_TEXT[:200], ZH_TEXT[200:400]])
        assert any("标定语料仅" in str(x.message) for x in w), \
            "少于 10 篇标定语料必须告警"

    def test_language_mismatch_warns(self):
        import warnings as warn_mod
        wm = Watermarker(master_key=KEY, codec_mode="zero_cost")
        corpus = [ZH_TEXT * 20 for _ in range(12)]
        with warn_mod.catch_warnings(record=True) as w:
            warn_mod.simplefilter("always")
            wm.calibrate_null_model(corpus)
        assert any("零词典命中" in str(x.message) for x in w), \
            "语料语言与 codec 语言不匹配必须告警"

    def test_adequate_matched_corpus_silent_enough(self):
        import warnings as warn_mod
        wm = Watermarker(master_key=KEY, codec_mode="zero_cost")
        corpus = [_demo_corpus_text()[i * 1500:(i + 1) * 1500]
                  for i in range(12)]
        with warn_mod.catch_warnings(record=True) as w:
            warn_mod.simplefilter("always")
            wm.calibrate_null_model(corpus)
        assert not any("零词典命中" in str(x.message) for x in w)


class TestBoundaryFragmentGuard:
    """v0.15.2 跨词界碎片守卫（owner 实测"注价值务"病句的回归锁定）。

    前向最大匹配会把 注意|义务 切成 注意|意义|务——"意义"是横跨两个
    真词边界的假词元，直接替换同组词会产生病句。守卫：raw 尾字+右邻
    字（或左邻字+raw 首字）构成大词典真词即保守跳过。
    """

    CASE = ("同时对侵权行为应负有注意义务，根据法律法规的规定，"
            "承担相应民事责任。")
    KEY = bytes(range(32))

    def test_spurious_fragment_not_replaced(self):
        from aawm.keys import derive_session_salt
        wm_text = None
        # 与盲评页同一派生盐路径（canonical 不变式），确定性复现
        canon_probe = self.CASE  # 单句场景 canonical 与原文相同词集合
        salt = derive_session_salt(self.KEY, canon_probe, nonce=0)
        from aawm.greenlist import build_zero_cost_zh_codec
        codec = build_zero_cost_zh_codec(self.KEY, salt)
        marked = codec.embed(self.CASE, 0x1, rng=random.Random(1))
        assert "注价值务" not in marked, f"跨界替换病句回归: {marked}"
        assert "注意义务" in marked

    def test_standalone_word_still_replaces(self):
        """守卫不能误伤合法替换：独立语境的 意义 在目标色不符时仍被换。"""
        from aawm.greenlist import build_zero_cost_zh_codec
        from aawm.keys import derive_session_salt, generate_session_salt
        text = "这件事的意义不大，可以后续再讨论。"
        replaced = False
        for n in range(8):
            salt = derive_session_salt(self.KEY, text, nonce=n)
            codec = build_zero_cost_zh_codec(self.KEY, salt)
            marked = codec.embed(text, 0xFFFF, rng=random.Random(n))
            if "意义" not in marked:
                replaced = True
                break
        assert replaced, "8 个盐下独立意义的竟无一次被替换——守卫过严？"

    def test_guard_conservative_only_skips(self):
        """守卫触发时文本必须原样保留（跳过≠改写）。"""
        from aawm.greenlist import build_zero_cost_zh_codec
        from aawm.keys import derive_session_salt
        salt = derive_session_salt(self.KEY, self.CASE, nonce=0)
        codec = build_zero_cost_zh_codec(self.KEY, salt)
        marked = codec.embed(self.CASE, 0x1, rng=random.Random(1))
        # 除合法替换外，句子骨架（去词典词后）应保持一致
        import re
        strip = lambda t: re.sub(r"[，。]", "", t)
        assert len(strip(marked)) == len(strip(self.CASE))
