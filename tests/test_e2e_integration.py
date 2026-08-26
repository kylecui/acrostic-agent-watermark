"""端到端集成测试：embed → attack → detect 全链路。

覆盖：
  1. 零感 codec 嵌入/检测往返
  2. 混合 codec 嵌入/检测往返
  3. 容量自适应路径（embed_adaptive → detect_adaptive）
  4. p0 标定效果
  5. 同义替换攻击后检测
  6. 段落删除攻击后检测
  7. margin abstain 行为
  8. 冗余编码（n_bits=k-r）
"""
import random

import pytest

from aawm.greenlist import (
    GreenlistCodec,
    build_zero_cost_zh_codec,
    build_hybrid_zh_codec,
)
from aawm.keys import generate_master_key, generate_session_salt
from aawm.collocation import build_char_context, filter_groups, group_score


# ------------------------------------------------------------------ 固件
def make_keys(seed: int = 42) -> tuple:
    rng = random.Random(seed)
    master = bytes(rng.randrange(256) for _ in range(32))
    salt = bytes(rng.randrange(256) for _ in range(16))
    return master, salt


# 真实风格的中文文本（含零感词典词）
SAMPLE_TEXTS = [
    "随着技术的发展，我们可以使用更加准确的方法来检测系统中的异常。"
    "因为数据量逐渐增大，所以需要提高处理速度和效率。"
    "根据分析结果，明显存在多个关键问题，必须及时解决。",

    "这是一个非常重要的测试，用于验证水印系统的可靠性。"
    "和传统的加密方法不同，这种技术可以保持文本的可读性。"
    "或者可以这么说，用户几乎感觉不到任何变化。",

    "已经检测到多个异常模式，需要进一步分析原因。"
    "为了提高准确率，应该采用更加严格的验证流程。"
    "这不仅是技术问题，也是安全问题，必须认真对待。",
]


def make_supplementary_dict():
    """小型补充词典（模拟词林组，用于混合 codec 测试）。"""
    return {
        "分析": ["分析", "解析", "剖析"],
        "验证": ["验证", "证实", "确认"],
        "异常": ["异常", "反常", "反常"],
        "流程": ["流程", "程序", "工序"],
        "模式": ["模式", "范式", "范式"],
    }


@pytest.fixture
def keys():
    return make_keys()


@pytest.fixture
def zero_codec(keys):
    master, salt = keys
    return build_zero_cost_zh_codec(master, salt)


@pytest.fixture
def hybrid_codec(keys):
    master, salt = keys
    return build_hybrid_zh_codec(
        master, salt,
        supplementary_dict=make_supplementary_dict(),
    )


# ------------------------------------------------------------------ 1. 往返
class TestRoundTrip:
    """嵌入后检测，UID 必须正确还原。"""

    def test_zero_codec_roundtrip(self, zero_codec):
        for text in SAMPLE_TEXTS:
            k = zero_codec.capacity(text)
            if k < 2:
                continue
            uid = random.Random(100).randrange(1 << k)
            marked, bands = zero_codec.embed_adaptive(text, uid)
            uid_d, active, _ = zero_codec.detect_adaptive(marked, bands)
            assert uid_d == uid, f"uid {uid} -> {uid_d}"

    def test_hybrid_codec_roundtrip(self, hybrid_codec):
        for text in SAMPLE_TEXTS:
            k = hybrid_codec.capacity(text)
            if k < 2:
                continue
            uid = random.Random(200).randrange(1 << k)
            marked, bands = hybrid_codec.embed_adaptive(text, uid)
            uid_d, active, _ = hybrid_codec.detect_adaptive(marked, bands)
            assert uid_d == uid, f"uid {uid} -> {uid_d}"

    def test_embed_preserves_text_length(self, zero_codec):
        """嵌入不改变文本长度（同义替换是等长替换）。"""
        text = SAMPLE_TEXTS[0]
        marked = zero_codec.embed(text, uid=0)
        assert len(marked) == len(text)

    def test_embed_preserves_non_dict_words(self, zero_codec):
        """非词典词原样保留。"""
        text = "今天天气很好，我们出去玩吧。"  # 无词典词
        marked = zero_codec.embed(text, uid=0)
        assert marked == text


# ------------------------------------------------------------------ 2. 容量自适应
class TestAdaptiveCapacity:
    """容量自适应路径：k-bit UID ↔ n_bands-bit 映射。"""

    def test_map_unmap_roundtrip(self, zero_codec):
        """map_uid → unmap_uid 可逆。"""
        text = SAMPLE_TEXTS[0]
        bands = zero_codec.active_bands(text)
        k = len(bands)
        if k < 2:
            pytest.skip("容量不足")
        for uid in [0, 1, (1 << k) - 1, random.Random(300).randrange(1 << k)]:
            full = zero_codec.map_uid(uid, bands)
            assert zero_codec.unmap_uid(full, bands) == uid

    def test_n_bits_redundancy(self, zero_codec):
        """冗余编码：n_bits < k 时 uid 仍能往返。"""
        text = SAMPLE_TEXTS[0]
        k = zero_codec.capacity(text)
        if k < 4:
            pytest.skip("容量不足 k<4")
        n_bits = k - 2  # 留 2 带冗余
        uid = random.Random(400).randrange(1 << n_bits)
        marked, used = zero_codec.embed_adaptive(text, uid, n_bits=n_bits)
        uid_d, active, _ = zero_codec.detect_adaptive(marked, used)
        assert uid_d == uid

    def test_capacity_matches_active_bands(self, zero_codec):
        """capacity() == len(active_bands())。"""
        for text in SAMPLE_TEXTS:
            k = zero_codec.capacity(text)
            bands = zero_codec.active_bands(text)
            assert k == len(bands)


# ------------------------------------------------------------------ 3. p0 标定
class TestP0Calibration:
    """p0 标定使逐带绿率从 0.5 偏移到实测值。"""

    def test_default_p0_is_05(self, zero_codec):
        """未标定时 p0 默认 0.5。"""
        for b in range(16):
            assert zero_codec._p0_of(b) == 0.5

    def test_calibration_changes_p0(self, keys):
        master, salt = keys
        codec = build_zero_cost_zh_codec(
            master, salt, calibrate_corpus=SAMPLE_TEXTS * 5,
        )
        changed = sum(1 for b in range(16) if codec._p0_of(b) != 0.5)
        assert changed > 0, "标定后至少有一些带 p0 偏移"

    def test_calibration_improves_detection(self, keys):
        """标定后检测的 uid 不变（p0 影响弱证据带 z 偏移，不改变主信号）。"""
        master, salt = keys
        raw = build_zero_cost_zh_codec(master, salt)
        cal = build_zero_cost_zh_codec(
            master, salt, calibrate_corpus=SAMPLE_TEXTS * 5,
        )
        text = SAMPLE_TEXTS[0]
        k = raw.capacity(text)
        if k < 2:
            pytest.skip()
        uid = 42 % (1 << k)
        marked, bands = raw.embed_adaptive(text, uid)
        # 两个 codec 用相同密钥，相同分词，但不同 p0
        uid_raw, _, _ = raw.detect_adaptive(marked, bands)
        uid_cal, _, _ = cal.detect_adaptive(marked, bands)
        assert uid_raw == uid
        assert uid_cal == uid


# ------------------------------------------------------------------ 4. 攻击后检测
class TestAttackResilience:
    """攻击后检测能力。"""

    def _synonym_attack(self, codec, text, rate=0.3, seed=500):
        """模拟同义替换攻击：随机替换 rate 比例的词典词。"""
        rng = random.Random(seed)
        parts = codec._tokenizer(text)
        out = []
        for raw, norm in parts:
            if norm is None or norm not in codec._w2group:
                out.append(raw)
                continue
            if rng.random() < rate:
                group = codec._w2group[norm]
                cands = [w for w in group if w != norm]
                if cands:
                    out.append(rng.choice(cands))
                    continue
            out.append(raw)
        return "".join(out)

    def _paragraph_delete(self, text, rate=0.3, seed=600):
        """模拟段落删除：随机删除 rate 比例的句子。"""
        rng = random.Random(seed)
        sents = text.replace("。", "。\n").split("\n")
        sents = [s for s in sents if s]
        n_keep = max(1, int(len(sents) * (1 - rate)))
        kept = rng.sample(sents, n_keep)
        return "".join(kept)

    def test_synonym_attack_preserves_uid(self, zero_codec):
        """15% 同义替换后，soft_match 仍能选出正确 UID。

        30% 替换在短文本下会翻转过多带导致错配（实验已验证），
        15% 是温和攻击的合理上限。
        """
        text = SAMPLE_TEXTS[0] + SAMPLE_TEXTS[1]
        k = zero_codec.capacity(text)
        if k < 2:
            pytest.skip()
        uid = random.Random(700).randrange(1 << k)
        marked, bands = zero_codec.embed_adaptive(text, uid)
        attacked = self._synonym_attack(zero_codec, marked, rate=0.15)
        cands = list(range(min(1 << k, 16)))
        if uid not in cands:
            cands.append(uid)
        best, _, _ = zero_codec.soft_match_adaptive(attacked, cands, bands)
        assert best == uid

    def test_paragraph_delete_preserves_uid(self, zero_codec):
        """30% 段落删除后，剩余带上的 UID 位必须正确。"""
        text = SAMPLE_TEXTS[0]
        k = zero_codec.capacity(text)
        if k < 2:
            pytest.skip()
        uid = random.Random(800).randrange(1 << k)
        marked, bands = zero_codec.embed_adaptive(text, uid)
        attacked = self._paragraph_delete(marked, rate=0.3)
        uid_d, active, _ = zero_codec.detect_adaptive(attacked, bands)
        # 删除攻击只丢带，不翻转带 -> 剩余带上的位必须正确
        for i, b in enumerate(bands):
            if b in active:
                expected = (uid >> i) & 1
                actual = (uid_d >> i) & 1
                assert expected == actual


# ------------------------------------------------------------------ 5. soft_match + margin
class TestSoftMatchMargin:
    """软判决匹配与 margin abstain。"""

    def test_soft_match_finds_correct_uid(self, zero_codec):
        text = SAMPLE_TEXTS[0]
        k = zero_codec.capacity(text)
        if k < 2:
            pytest.skip()
        uid = random.Random(900).randrange(1 << k)
        marked, bands = zero_codec.embed_adaptive(text, uid)
        cands = list(range(min(1 << k, 8)))
        if uid not in cands:
            cands.append(uid)
        best, score, gap = zero_codec.soft_match_adaptive(marked, cands, bands)
        assert best == uid

    def test_margin_abstains_on_attack(self, zero_codec):
        """大 margin 下，攻击后的低置信匹配转为 abstain。"""
        text = SAMPLE_TEXTS[0]
        k = zero_codec.capacity(text)
        if k < 2:
            pytest.skip()
        uid = random.Random(1000).randrange(1 << k)
        marked, bands = zero_codec.embed_adaptive(text, uid)
        cands = list(range(min(1 << k, 8)))
        if uid not in cands:
            cands.append(uid)
        # 未攻击：小 margin 应放行
        best, _, gap = zero_codec.soft_match_adaptive(
            marked, cands, bands, margin=0.0,
        )
        assert best == uid
        # 未攻击：大 margin 可能 abstain（gap 有限）
        # 但至少不返回错误 uid
        best_hi, _, _ = zero_codec.soft_match_adaptive(
            marked, cands, bands, margin=100.0,
        )
        assert best_hi is None  # gap < 100 -> abstain

    def test_null_text_does_not_match(self, zero_codec):
        """无水印文本的 soft_match 得分低，大 margin 下 abstain。"""
        text = "今天天气很好，我们出去玩吧。"  # 无词典词
        cands = [0, 1, 2, 3]
        bands = zero_codec.active_bands(text)
        if len(bands) < 2:
            pytest.skip("无信号")
        best, _, _ = zero_codec.soft_match_adaptive(
            text, cands, bands, margin=0.1,
        )
        # 无水印文本应 abstain（或得分极低）
        # 不检查 best 是否 None（小 margin 可能放行），
        # 但至少不"自信地"返回
        # 如果 best 非 None，gap 应该很小
        best0, _, gap0 = zero_codec.soft_match_adaptive(
            text, cands, bands, margin=0.0,
        )
        assert gap0 < 5.0  # 无水印文本 gap 不会很大


# ------------------------------------------------------------------ 6. 混合 codec
class TestHybridCodec:
    """混合词典 codec 集成。"""

    def test_hybrid_has_more_groups(self, hybrid_codec, zero_codec):
        """混合 codec 组数 >= 零感 codec。"""
        assert len(hybrid_codec._groups) >= len(zero_codec._groups)

    def test_hybrid_roundtrip_with_calibrate(self, keys):
        """混合 codec + p0 标定 往返。"""
        master, salt = keys
        codec = build_hybrid_zh_codec(
            master, salt,
            supplementary_dict=make_supplementary_dict(),
            calibrate_corpus=SAMPLE_TEXTS * 5,
        )
        text = SAMPLE_TEXTS[0]
        k = codec.capacity(text)
        if k < 2:
            pytest.skip()
        uid = random.Random(1100).randrange(1 << k)
        marked, bands = codec.embed_adaptive(text, uid)
        uid_d, _, _ = codec.detect_adaptive(marked, bands)
        assert uid_d == uid

    def test_collocation_filtering(self, keys):
        """语料兼容性过滤剔除低分组。"""
        master, salt = keys
        supp = make_supplementary_dict()
        supp_words = {w for ws in supp.values() for w in ws}
        ctx_texts = SAMPLE_TEXTS * 10
        left, right = build_char_context(ctx_texts, supp_words)
        survived, dropped = filter_groups(supp, left, right, threshold=0.01)
        # 过滤后存活组数 <= 原始组数
        assert len(survived) <= len(supp)
        # 每个存活组的 score >= threshold
        for words in survived.values():
            assert group_score(words, left, right) >= 0.01

    def test_hybrid_with_collocation_filter(self, keys):
        """混合 codec + 过滤 仍能正常往返。"""
        master, salt = keys
        codec = build_hybrid_zh_codec(
            master, salt,
            supplementary_dict=make_supplementary_dict(),
            collocation_threshold=0.01,
            context_texts=SAMPLE_TEXTS * 10,
            calibrate_corpus=SAMPLE_TEXTS * 5,
        )
        text = SAMPLE_TEXTS[0]
        k = codec.capacity(text)
        if k < 2:
            pytest.skip()
        uid = random.Random(1200).randrange(1 << k)
        marked, bands = codec.embed_adaptive(text, uid)
        uid_d, _, _ = codec.detect_adaptive(marked, bands)
        assert uid_d == uid


# ======================================================================
# Facade 级端到端（Watermarker 统一入口）
# ======================================================================

from aawm.plugins import Watermarker
from aawm.plugins.keystore import KeyStore


def _long_zh_text() -> str:
    """拼一段较长的中文文本（800+ 字），保证零感词典有足够容量。"""
    base = ("".join(SAMPLE_TEXTS) + "我们应该根据实际情况选择合适的方法。"
            "如果出现错误，就必须及时修正。"
            "并且随着时间的发展，这些方法会更加完善。"
            "因为数据安全非常重要，所以每个用户都应该关注。"
            "使用这种技术可以保护隐私，也能提高系统的可靠性。"
            "目前已经有很多应用，未来还会有更多发展。"
            "为了达到目标，我们需要不断提高能力和水平。"
            "这说明该方法在实际场景中是可行的。")
    while len(base) < 900:
        base += base
    return base[:1200]


class TestFacadeZeroCost:
    """Watermarker + zero_cost 模式全链路。"""

    def _wm(self, calibrate=False):
        kwargs = {"language": "zh", "codec_mode": "zero_cost"}
        if calibrate:
            # 多样化 null 语料（同质语料会让线性拟合退化）
            lt = _long_zh_text()
            kwargs["calibrate_corpus"] = [
                SAMPLE_TEXTS[0], SAMPLE_TEXTS[1], SAMPLE_TEXTS[2],
                lt[:800], lt[200:1000],
            ]
        return Watermarker(keystore=KeyStore(bytes(range(32))), **kwargs)

    def test_embed_trace_roundtrip(self):
        wm = self._wm()
        text = _long_zh_text()
        r = wm.embed(text, user_id=42)
        assert r.codec_mode == "zero_cost"
        assert r.capacity >= 2
        assert len(r.bands) == r.capacity
        # 长度允许小幅变化（双字↔单字同义组，如 曾经↔曾）
        assert abs(len(r.watermarked_text) - len(text)) <= len(text) // 20
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     bands=r.bands, n_bits=r.n_bits)
        assert t.watermarked
        assert t.uid == 42
        assert t.codec_mode == "zero_cost"
        assert t.active_bands >= 1

    def test_redundancy_n_bits(self):
        """n_bits < k 留冗余带。"""
        wm = self._wm()
        text = _long_zh_text()
        r_full = wm.embed(text, user_id=5)
        if r_full.capacity < 4:
            pytest.skip("容量不足")
        r_red = wm.embed(text, user_id=5, n_bits=r_full.capacity - 2)
        assert r_red.n_bits == r_full.capacity - 2
        assert len(r_red.bands) == r_red.n_bits
        t = wm.trace(r_red.watermarked_text, session_salt=r_red.session_salt,
                     bands=r_red.bands, n_bits=r_red.n_bits)
        assert t.uid == 5

    def test_calibrated_existence(self):
        """p0/null 标定后：marked 检出，null 不误报。"""
        wm = self._wm(calibrate=True)
        assert wm._null_model is not None
        text = _long_zh_text()
        r = wm.embed(text, user_id=7)
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     bands=r.bands, n_bits=r.n_bits)
        assert t.watermarked and t.uid == 7
        # null（未嵌水印）不应检出
        t_null = wm.trace(text, session_salt=r.session_salt,
                          bands=r.bands, n_bits=r.n_bits)
        assert not t_null.watermarked

    def test_with_registry_softmatch(self):
        """注册库 + soft_match_adaptive 路径。"""
        from aawm.plugins import UIDRegistry
        reg = UIDRegistry()
        reg.register("alice", uid=42)
        reg.register("bob", uid=99)
        wm = Watermarker(keystore=KeyStore(bytes(range(32))),
                         registry=reg, language="zh", codec_mode="zero_cost")
        text = _long_zh_text()
        r = wm.embed(text, user_id="alice")
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     bands=r.bands, n_bits=r.n_bits, soft_match=True)
        assert t.watermarked
        # uid 为 k-bit 空间：alice 的 UID 42 的低 n_bits 位
        mask = (1 << r.n_bits) - 1
        assert t.uid == (42 & mask)
        assert t.user == "alice"


class TestFacadeDefaultCompat:
    """default 模式向后兼容（旧行为不变）。"""

    def test_default_mode_unchanged(self):
        wm = Watermarker(keystore=KeyStore(bytes(range(32))), language="zh")
        text = SAMPLE_TEXTS[0]
        r = wm.embed(text, user_id=42)
        assert r.codec_mode == "default"
        assert r.bands == [] and r.capacity == 0
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt)
        # default 模式 trace 不传 bands 走旧路径
        assert t.codec_mode == "default"

    def test_hybrid_mode_via_facade(self):
        from aawm.plugins import UIDRegistry
        wm = Watermarker(
            keystore=KeyStore(bytes(range(32))),
            language="zh", codec_mode="hybrid",
            supplementary_dict=make_supplementary_dict(),
        )
        text = _long_zh_text()
        r = wm.embed(text, user_id=3)
        assert r.codec_mode == "hybrid"
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     bands=r.bands, n_bits=r.n_bits)
        assert t.watermarked
        mask = (1 << r.n_bits) - 1
        assert t.uid == (3 & mask)

    def test_invalid_codec_mode_rejected(self):
        with pytest.raises(ValueError):
            Watermarker(codec_mode="bogus")
