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
        wm = Watermarker(registry=reg)
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
        wm = Watermarker(registry=reg)
        # 短 null 文本：existence≈5.8 < 阈值≈11.8，watermarked=False
        null_text = make_text(40, seed=2)
        t = wm.trace(null_text, soft_match=True)
        assert not t.watermarked
        assert t.uid is None
        assert t.user is None

    def test_trace_soft_default_off_preserves_hard_path(self):
        """默认（soft_match=False）仍走硬判决路径。"""
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        wm = Watermarker(registry=reg)
        res = wm.embed(make_text(600, seed=4), user_id="alice")
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt)
        assert t.soft_uid is None          # 未启用软判决
        assert t.soft_gap == -1.0
        assert t.user == "alice"           # 硬判决路径正常

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
        wm = Watermarker(registry=reg)
        res = wm.embed(make_text(600, seed=16), user_id="alice")
        # 巨大 ratio：生效阈值远超任何 gap → soft_uid=None，不误指用户
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                     soft_match=True, match_margin_ratio=100.0)
        assert t.soft_uid is None
        assert t.user is None
        assert t.soft_gap >= 0

    def test_trace_ratio_none_compat(self):
        """match_margin_ratio 默认 None，trace 行为与 v0.7 一致。"""
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        wm = Watermarker(registry=reg)
        res = wm.embed(make_text(600, seed=17), user_id="alice")
        t = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                     soft_match=True)
        assert t.soft_uid == 0x1234
        assert t.user == "alice"
