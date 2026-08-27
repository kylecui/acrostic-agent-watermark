"""软判决注册库匹配（v0.7 鲁棒性增强）单元测试。

验证：
  1. soft_match 对嵌入文本正确选出真 UID（min_n=1 弱证据带参与）
  2. margin 阈值把低置信匹配转为 abstain（None）
  3. 未嵌水印文本不误匹配
  4. detect(min_n=1) 与默认 min_n=2 的行为差异
  5. Watermarker.trace(soft_match=True) 端到端
"""
import random

import pytest

from aawm.greenlist import GreenlistCodec
from aawm.plugins import UIDRegistry, Watermarker
from aawm.plugins.facade import TraceResult


def make_codec(seed: int = 42) -> GreenlistCodec:
    rng = random.Random(seed)
    master = bytes(rng.randrange(256) for _ in range(32))
    salt = bytes(rng.randrange(256) for _ in range(16))
    return GreenlistCodec(master, salt)


def make_text(n_words: int, seed: int = 7) -> str:
    """模拟文本：约 35% 词典词 + 65% 填充词（与 test_greenlist 一致）。"""
    rng = random.Random(seed)
    codec = make_codec()
    filler = [
        "system", "process", "value", "human", "between", "during",
        "though", "while", "should", "would", "might", "place",
        "group", "number", "change", "right",
    ]
    heads = list(codec._groups.keys())
    out = []
    for _ in range(n_words):
        if rng.random() < 0.35:
            out.append(rng.choice(codec._groups[rng.choice(heads)]))
        else:
            out.append(rng.choice(filler))
    return " ".join(out)


# ---------------------------------------------------------------------------
# soft_match 正确性
# ---------------------------------------------------------------------------

class TestSoftMatch:
    def test_selects_true_uid(self):
        """嵌入文本的 soft_match 应选出真 UID。"""
        codec = make_codec()
        uid = 0x1234
        marked = codec.embed(make_text(600, seed=5), uid, rng=random.Random(0))
        cands = [0x0000, 0x1234, 0xFFFF, 0xABCD, 0x5678]
        best, score, gap = codec.soft_match(marked, cands)
        assert best == uid
        assert score > 0
        assert gap > 0

    def test_margin_abstains_tie(self):
        """margin 过滤候选不可区分的情形（得分差 < margin → abstain）。"""
        codec = make_codec()
        marked = codec.embed(make_text(600, seed=11), 0x0F0F, rng=random.Random(0))
        # 极大 margin 必然 abstain（双候选）
        best, score, gap = codec.soft_match(marked, [0x0F0F, 0xF0F0], margin=999.0)
        assert best is None
        assert gap < 999.0

    def test_single_candidate_never_abstains(self):
        """单候选无次优可比，恒返回该候选（margin 无意义）。"""
        codec = make_codec()
        marked = codec.embed(make_text(600, seed=11), 0x0F0F, rng=random.Random(0))
        best, _, _ = codec.soft_match(marked, [0x0F0F], margin=999.0)
        assert best == 0x0F0F

    def test_null_existence_well_below_marked(self):
        """null 与 marked 的存在性得分应有大间距（存在性门控的依据）。

        soft_match 是候选区分器：null 文本的 z 随机游走也可能与某候选
        方向对齐（实测 margin=2 下 40/40 误匹配）。区分"未嵌"靠的是
        existence_score，不是 soft_match 的 gap。
        """
        codec = make_codec()
        null_exs = [codec.detect(make_text(600, seed=seed)).existence_score
                    for seed in range(100, 110)]
        marked_exs = [
            codec.detect(codec.embed(make_text(600, seed=seed), 0x0F0F,
                                     rng=random.Random(seed))).existence_score
            for seed in range(100, 110)]
        assert max(null_exs) < min(marked_exs), (
            f"null/marked 存在性重叠: null {max(null_exs):.1f} >= marked {min(marked_exs):.1f}")

    def test_margin_keeps_confident(self):
        """margin 阈值不应阻止高置信匹配。"""
        codec = make_codec()
        uid = 0xABCD
        marked = codec.embed(make_text(600, seed=6), uid, rng=random.Random(0))
        cands = [0x0000, uid, 0xFFFF, 0x1111, 0x2222]
        best, score, gap = codec.soft_match(marked, cands, margin=4.0)
        assert best == uid
        assert gap >= 4.0

    def test_duplicate_candidates_deduped(self):
        """重复候选应去重，不影响匹配。"""
        codec = make_codec()
        uid = 0x2468
        marked = codec.embed(make_text(600, seed=7), uid, rng=random.Random(0))
        cands = [uid, uid, 0xFFFF, uid, 0x1234]
        best, _, _ = codec.soft_match(marked, cands)
        assert best == uid

    def test_empty_candidates(self):
        """空候选返回 (None, 0, 0)。"""
        codec = make_codec()
        assert codec.soft_match(make_text(600), []) == (None, 0.0, 0.0)

    def test_zero_margin_returns_argmax(self):
        """margin=0（默认）时始终返回 argmax，即使并列。"""
        codec = make_codec()
        marked = codec.embed(make_text(600, seed=8), 0xFFFF, rng=random.Random(0))
        best, _, gap = codec.soft_match(marked, [0xFFFF, 0x8000], margin=0.0)
        assert best == 0xFFFF


# ---------------------------------------------------------------------------
# detect(min_n) 弱证据带
# ---------------------------------------------------------------------------

class TestDetectMinN:
    def test_min_n1_reuses_weak_band(self):
        """min_n=1 时单词带参与解码（弱证据带回收）。"""
        codec = make_codec()
        uid = 0x3333
        marked = codec.embed(make_text(600, seed=9), uid, rng=random.Random(0))
        rep2 = codec.detect(marked)           # min_n=2（默认）
        rep1 = codec.detect(marked, min_n=1)  # min_n=1
        # min_n=1 参与带数不少于 min_n=2，存在性得分不降
        n2 = sum(1 for st in rep2.bands if st.has_signal)
        n1 = sum(1 for st in rep1.bands if st.has_signal)
        assert n1 >= n2
        assert rep1.existence_score >= rep2.existence_score
        # 两个都能正确解码
        assert rep2.uid == uid
        assert rep1.uid == uid

    def test_detect_default_compat(self):
        """detect 默认 min_n=2，不传参数行为与 v0.6 一致。"""
        codec = make_codec()
        marked = codec.embed(make_text(600, seed=10), 0x5A5A, rng=random.Random(0))
        rep = codec.detect(marked)
        assert rep.uid == 0x5A5A
        assert rep.existence_score > 0


# ---------------------------------------------------------------------------
# Watermarker.trace(soft_match=True) 端到端
# ---------------------------------------------------------------------------

class TestTraceSoftMatch:
    def test_trace_soft_matches_user(self):
        """trace(soft_match=True) 端到端匹配注册用户。"""
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        reg.register("bob", uid=0x5678)
        wm = Watermarker(registry=reg, codec_mode="default")  # make_text 用 default 词林词
        res = wm.embed(make_text(600, seed=3), user_id="alice")
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                     soft_match=True)
        assert isinstance(t, TraceResult)
        assert t.uid == 0x1234
        assert t.user == "alice"
        assert t.watermarked
        assert t.soft_gap > 0
        assert t.soft_uid == 0x1234

    def test_trace_soft_abstains_null(self):
        """trace(soft_match=True) 对未嵌文本 abstain（存在性门控）。"""
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        # 固定密钥 + 固定 salt：null 判定必须确定性（随机 salt 下 40 词短
        # 文本的存在性得分会跨阈值抖动，导致 ~10% 概率的 flaky）。
        # 实测组合：master_key=bytes(range(32)) + session_salt=bytes(16)
        # → ex=8.5 < thr=10.4，watermarked 恒 False。
        wm = Watermarker(master_key=bytes(range(32)), registry=reg)
        null_text = make_text(40, seed=2)
        t = wm.trace(null_text, session_salt=bytes(16), soft_match=True)
        assert not t.watermarked
        assert t.uid is None
        assert t.user is None

    def test_trace_soft_on_by_default(self):
        """v0.10 起默认启用软判决：注册库存在时默认走 soft 路径。"""
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        wm = Watermarker(registry=reg, codec_mode="default")  # make_text 用 default 词林词
        res = wm.embed(make_text(600, seed=4), user_id="alice")
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt)
        assert t.soft_uid == 0x1234       # 默认已启用软判决
        assert t.soft_gap >= 0
        assert t.user == "alice"
        assert t.attribution_confidence >= 0.5
        assert not t.attribution_abstain

    def test_trace_soft_without_registry_falls_back(self):
        """无注册库时 soft_match 应安全回退（不抛异常）。"""
        wm = Watermarker()  # 无 registry
        res = wm.embed(make_text(600, seed=5), user_id=42)
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                     soft_match=True)
        assert t.soft_uid is None
        assert t.soft_gap == -1.0


# ---------------------------------------------------------------------------
# margin_ratio 自适应置信阈值（v0.8）
# ---------------------------------------------------------------------------

class TestMarginRatio:
    """margin_ratio：生效阈值 max(margin, ratio·√n_dict)。

    背景（exp_margin_scale/exp_margin_ratio 实测）：gap 统计尺度随
    √n_dict 增长，固定绝对 margin 对长文本偏松（"自信地错"）。ratio
    提供按证据量放大的置信余量，是"宁可 abstain 也不错"的权衡旋钮。
    """

    def test_ratio_none_keeps_abs_compat(self):
        """margin_ratio=None 与纯绝对 margin 行为完全一致（v0.7 兼容）。"""
        codec = make_codec()
        marked = codec.embed(make_text(600, seed=12), 0x0F0F, rng=random.Random(0))
        cands = [0x0F0F, 0xF0F0]
        b1, s1, g1 = codec.soft_match(marked, cands, margin=2.0, margin_ratio=None)
        b2, s2, g2 = codec.soft_match(marked, cands, margin=2.0)
        assert (b1, s1, g1) == (b2, s2, g2)

    def test_ratio_zero_keeps_abs(self):
        """margin_ratio=0 退化为纯绝对 margin。"""
        codec = make_codec()
        marked = codec.embed(make_text(600, seed=13), 0x2468, rng=random.Random(0))
        cands = [0x2468, 0xFFFF]
        b1, s1, g1 = codec.soft_match(marked, cands, margin=2.0, margin_ratio=0.0)
        b2, s2, g2 = codec.soft_match(marked, cands, margin=2.0, margin_ratio=None)
        assert (b1, s1, g1) == (b2, s2, g2)

    def test_ratio_scales_margin_up(self):
        """ratio 把生效阈值放大到 ratio·√n_dict，使原通过的高 gap 转 abstain。"""
        codec = make_codec()
        marked = codec.embed(make_text(600, seed=14), 0x5A5A, rng=random.Random(0))
        cands = [0x5A5A, 0xF0F0]
        n_dict = codec.detect(marked).n_dict_words
        b_abs, _, gap = codec.soft_match(marked, cands, margin=0.0)
        b_scaled, _, _ = codec.soft_match(marked, cands, margin=0.0, margin_ratio=10.0)
        assert b_abs == 0x5A5A              # 无自适应时正常匹配
        assert b_scaled is None             # 自适应放大后 abstain
        assert gap < 10.0 * (n_dict ** 0.5)  # gap 确实低于放大后的阈值

    def test_ratio_abs_dominant_for_small_corpus(self):
        """ratio·√n_dict < margin 时 max 取绝对项，行为不变。"""
        codec = make_codec()
        marked = codec.embed(make_text(80, seed=15), 0x00FF, rng=random.Random(0))
        cands = [0x00FF, 0xFF00]
        b1, s1, g1 = codec.soft_match(marked, cands, margin=2.0, margin_ratio=0.001)
        b2, s2, g2 = codec.soft_match(marked, cands, margin=2.0, margin_ratio=None)
        assert (b1, s1, g1) == (b2, s2, g2)


class TestTraceMarginRatio:
    def test_trace_forwards_margin_ratio(self):
        """trace 的 match_margin_ratio 透传到 soft_match，超大 ratio 全部 abstain。"""
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        reg.register("bob", uid=0x5678)
        wm = Watermarker(registry=reg, codec_mode="default")  # make_text 用 default 词林词
        res = wm.embed(make_text(600, seed=16), user_id="alice")
        # 巨大 ratio：生效阈值远超任何 gap → soft_uid=None，不误指用户
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                     soft_match=True, match_margin_ratio=100.0)
        assert t.soft_uid is None
        assert t.user is None
        assert t.soft_gap >= 0

    def test_trace_default_ratio_keeps_correct_match(self):
        """match_margin_ratio 默认 0.3（v0.10）：干净正确匹配仍通过。"""
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        wm = Watermarker(registry=reg, codec_mode="default")  # make_text 用 default 词林词
        res = wm.embed(make_text(600, seed=17), user_id="alice")
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                     soft_match=True)
        assert t.soft_uid == 0x1234
        assert t.user == "alice"
        assert not t.attribution_abstain


# ---------------------------------------------------------------------------
# attribution_confidence 归因置信度（v0.10）
# ---------------------------------------------------------------------------

class TestAttributionConfidence:
    """归因置信度：对抗场景"高置信度错误归因"的防护。

    attribution_confidence 独立于存在性 confidence，低于
    attribution_floor（默认 0.5）时置 attribution_abstain=True
    且 uid/user=None——输出"不可判定"而非可能错误的用户。
    """

    def test_clean_match_high_confidence(self):
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        wm = Watermarker(registry=reg, codec_mode="default")
        res = wm.embed(make_text(600, seed=4), user_id="alice")
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt)
        assert t.watermarked
        assert t.user == "alice"
        assert t.attribution_confidence >= 0.5
        assert not t.attribution_abstain

    def test_null_text_zero_confidence(self):
        """未检出 → AC=0、abstain=False、uid=None。"""
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        wm = Watermarker(master_key=bytes(range(32)), registry=reg)
        # 固定密钥+固定 salt（同 test_trace_soft_abstains_null 的确定性组合）
        t = wm.trace(make_text(40, seed=2), session_salt=bytes(16),
                     soft_match=True)
        assert not t.watermarked
        assert t.attribution_confidence == 0.0
        assert not t.attribution_abstain
        assert t.uid is None

    def test_margin_abstain_sets_uid_none(self):
        """soft margin 拒绝时 uid 置 None——不回退硬解码（防"自信地错"）。"""
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        reg.register("bob", uid=0x5678)
        wm = Watermarker(registry=reg, codec_mode="default")
        res = wm.embed(make_text(600, seed=16), user_id="alice")
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                     soft_match=True, match_margin_ratio=100.0)
        assert t.watermarked
        assert t.soft_uid is None            # margin 门限拒绝
        assert t.uid is None                 # 关键：不再回退硬解码 0x1234
        assert t.user is None
        assert t.attribution_abstain
        assert t.attribution_confidence < 0.5

    def test_no_registry_hard_path_keeps_uid(self):
        """无注册库：无候选对比 → AC=0.5（踩门槛下缘），不清除 uid。"""
        wm = Watermarker(codec_mode="default")
        res = wm.embed(make_text(600, seed=5), user_id=42)
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt)
        assert t.watermarked
        assert t.uid is not None
        assert not t.attribution_abstain
        assert t.attribution_confidence == 0.5  # hard_no_cands_cap

    def test_hard_path_hamming_confidence(self):
        """显式软判决关闭：注册库汉明距驱动 AC（dist=0 → 1.0）。"""
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        wm = Watermarker(registry=reg, codec_mode="default")
        res = wm.embed(make_text(600, seed=6), user_id="alice")
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                     soft_match=False)
        assert t.watermarked
        assert t.uid == 0x1234
        assert t.user == "alice"
        assert t.hamming_dist == 0
        assert t.attribution_confidence == 1.0
        assert not t.attribution_abstain

    def test_masked_collision_abstains(self):
        """低容量掩码碰撞：两用户低 k 位相同 → k-bit 空间不可区分 → abstain。

        复现 VERIFICATION_REPORT 的低容量误解码场景：k 位空间内两用户
        掩码相同（如 n_bits=6 下 UID 1 与 65 的 1 & 0b111111 == 65 & 0b111111
        == 1），解出的 k-bit UID 无法区分二者——归因在数学上不可能对，
        必须由容量项 cap=0 兜底 abstain，否则就是"自信地错"。

        注：不用原始报告的 n_bits=2 + UID 5 场景，因为 n_bits 远小于
        容量时零感冗余嵌入存在性 margin 天然饱和，embed 自检必不过、
        trace 漏检 watermarked=False（弱水印固有属性）——测试必须先
        保证检出水印，才能验证"检出了但不可归因"的 abstain 行为。
        """
        from tests.test_e2e_integration import _long_zh_text

        reg = UIDRegistry()
        reg.register("alice", uid=1)
        reg.register("bob", uid=65)  # 65 & 0b111111 == 1 & 0b111111 → 掩码碰撞
        wm = Watermarker(master_key=bytes(range(32)), registry=reg,
                         language="zh", codec_mode="zero_cost")
        res = wm.embed(_long_zh_text(), user_id="alice", n_bits=6)
        assert res.n_bits == 6, f"未按请求容量嵌入: {res.n_bits}"

        # 前提自检：注册库两 UID 在 6-bit 空间确实碰撞
        mask = (1 << res.n_bits) - 1
        assert 1 & mask == 65 & mask
        assert len({u & mask for u in (1, 65)}) == 1

        t = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                     bands=res.bands, n_bits=res.n_bits)
        assert t.watermarked                    # 存在性存活（否则测试无效）
        assert t.attribution_abstain            # 掩码碰撞 → 归因不可靠
        assert t.uid is None                    # 关键：不输出"可能错"的 UID
        assert t.user is None
        assert t.attribution_confidence < 0.5
