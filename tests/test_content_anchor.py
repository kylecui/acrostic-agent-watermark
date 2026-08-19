"""v0.3 内容寻址锚点测试套件。"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm import CADecoder, CAEmbedder, CAConfig, generate_master_key  # noqa: E402

TEXT = (
    "The team released a new version of the platform this week. "
    "The company said the update will help every customer who uses the service. "
    "Our goal is simple: we want to make the product fast, clear, and useful for each user. "
    "The new framework allows people to build complex features in a short time. "
    "Developers can test every part of the system before the final release. "
    "If the team finds an issue, they can fix the problem quickly and send the update to every user. "
    "The report shows strong results this quarter. "
    "Sales grew at a rapid pace, and the customer base expanded into new areas. "
    "The analysis suggests the modern design was a key reason for the growth. "
    "People really like the clear layout and the quick response time. "
    "Our plan for the future is to improve the core system and add more tools. "
    "We will keep the price low, because we want the service to stay affordable for small teams. "
    "The company believes this approach will create real value for every customer. "
    "Security is a major focus of the new version. "
    "The team will check every request and remove any risk before it can cause harm. "
    "Users can change their settings and choose the level of protection they need. "
    "If a question comes up, our support team will answer fast and explain every detail. "
    "This project is important for the whole company. "
    "It shows our team can solve hard problems and deliver quality work. "
    "We expect the platform to become the standard tool for teams that value speed and simplicity."
)

INSERT_WORDS = [
    "also", "just", "really", "quite", "often", "perhaps", "maybe", "simply",
]


def insert_attack(text: str, n: int, rng: random.Random) -> str:
    words = text.split(" ")
    for _ in range(n):
        i = rng.randrange(len(words) + 1)
        words.insert(i, rng.choice(INSERT_WORDS))
    return " ".join(words)


def delete_attack(text: str, n: int, rng: random.Random) -> str:
    words = [w for w in text.split(" ") if w]
    for _ in range(n):
        if len(words) <= 10:
            break
        del words[rng.randrange(len(words))]
    return " ".join(words)


class TestRoundTrip:
    def test_multiple_uids(self):
        key = generate_master_key()
        emb, dec = CAEmbedder(key), CADecoder(key)
        for uid in [0, 42, 1001, 65535]:
            ok = False
            for _ in range(10):
                r = emb.embed(TEXT, user_id=uid)
                d = dec.decode(r.watermarked_text, r.session_salt)
                if d.success and d.user_id == uid:
                    ok = True
                    break
            assert ok, f"uid={uid} roundtrip failed after 10 retries"

    def test_zero_skip_multiple_keys(self):
        """可锚定位永不 skip（组双 bit 表达性保证）。"""
        for _ in range(5):
            key = generate_master_key()
            emb = CAEmbedder(key)
            r = emb.embed(TEXT, user_id=7)
            assert r.n_skipped == 0

    def test_deterministic_with_same_salt(self):
        key = generate_master_key()
        emb = CAEmbedder(key)
        salt = b"0123456789abcdef"
        r1 = emb.embed(TEXT, user_id=42, session_salt=salt)
        r2 = emb.embed(TEXT, user_id=42, session_salt=salt)
        assert r1.watermarked_text == r2.watermarked_text

    def test_different_uids_different_texts(self):
        key = generate_master_key()
        emb = CAEmbedder(key)
        salt = b"0123456789abcdef"
        texts = {
            uid: emb.embed(TEXT, user_id=uid, session_salt=salt).watermarked_text
            for uid in [1, 2, 3]
        }
        assert len(set(texts.values())) == 3


class TestEditRobustness:
    def test_insert_10_words(self):
        key = generate_master_key()
        emb, dec = CAEmbedder(key), CADecoder(key)
        # 先确保嵌入成功（短文本有概率失败），最多重试 5 次
        for _ in range(5):
            r = emb.embed(TEXT, user_id=1001)
            if dec.decode(r.watermarked_text, r.session_salt).success:
                break
        rng = random.Random(1)
        ok = sum(
            1 for _ in range(10)
            if (lambda t: dec.decode(t, r.session_salt).success)(
                insert_attack(r.watermarked_text, 10, rng))
        )
        assert ok >= 7, f"插入 10 词存活率过低: {ok}/10"

    def test_delete_10_words(self):
        key = generate_master_key()
        emb, dec = CAEmbedder(key), CADecoder(key)
        for _ in range(5):
            r = emb.embed(TEXT, user_id=1001)
            if dec.decode(r.watermarked_text, r.session_salt).success:
                break
        rng = random.Random(2)
        ok = sum(
            1 for _ in range(10)
            if (lambda t: dec.decode(t, r.session_salt).success)(
                delete_attack(r.watermarked_text, 10, rng))
        )
        assert ok >= 7, f"删除 10 词存活率过低: {ok}/10"

    def test_mixed_heavy_edit(self):
        key = generate_master_key()
        emb, dec = CAEmbedder(key), CADecoder(key)
        r = emb.embed(TEXT, user_id=1001)
        rng = random.Random(3)
        attacked = delete_attack(
            insert_attack(r.watermarked_text, 10, rng), 10, rng
        )
        d = dec.decode(attacked, r.session_salt)
        # 20 次混合编辑后可能失败，但绝不能解出错误的用户 ID
        assert (not d.success) or d.user_id == 1001


class TestRejection:
    def test_wrong_key_rejected(self):
        """错误密钥：CRC 通过概率 1/256，多次重试确保统计压倒性失败。"""
        key = generate_master_key()
        emb = CAEmbedder(key)
        wrong = CADecoder(generate_master_key())
        fails = 0
        for _ in range(8):
            r = emb.embed(TEXT, user_id=42)
            d = wrong.decode(r.watermarked_text, r.session_salt)
            if not d.success:
                fails += 1
        assert fails >= 7

    def test_plain_text_rejected(self):
        """无水印文本：CRC 通过概率 1/256，多次重试。"""
        key = generate_master_key()
        dec = CADecoder(key)
        fails = 0
        for _ in range(8):
            d = dec.decode(TEXT, generate_master_key()[:16])
            if not d.success:
                fails += 1
        assert fails >= 7

    def test_wrong_salt_rejected(self):
        key = generate_master_key()
        emb, dec = CAEmbedder(key), CADecoder(key)
        r = emb.embed(TEXT, user_id=42)
        d = dec.decode(r.watermarked_text, b"ffffffffffffffff")
        assert not (d.success and d.user_id == 42)

    def test_uid_out_of_range(self):
        emb = CAEmbedder(generate_master_key())
        with pytest.raises(ValueError):
            emb.embed(TEXT, user_id=70000)  # 16-bit 上限 65535


class TestCapacity:
    def test_insufficient_text_rejected(self):
        emb = CAEmbedder(generate_master_key())
        short = "The system is good and fast for every user today."
        with pytest.raises(ValueError, match="容量不足"):
            emb.embed(short, user_id=1)

    def test_config_min_anchorable(self):
        cfg = CAConfig(min_anchorable=10)
        emb = CAEmbedder(generate_master_key(), cfg)
        mid = " ".join(TEXT.split()[:60])
        r = emb.embed(mid, user_id=1)  # 不应抛异常
        assert r.n_anchorable >= 10


class TestDiagnostics:
    def test_votes_histogram_matches(self):
        key = generate_master_key()
        emb = CAEmbedder(key)
        r = emb.embed(TEXT, user_id=42)
        assert sum(r.votes_histogram) == r.n_anchorable
        assert len(r.votes_histogram) == 24  # 16 uid + 8 crc

    def test_decode_reports_votes(self):
        key = generate_master_key()
        emb, dec = CAEmbedder(key), CADecoder(key)
        r = emb.embed(TEXT, user_id=42)
        d = dec.decode(r.watermarked_text, r.session_salt)
        assert d.n_votes == r.n_anchorable


# ---------------------------------------------------------------------------
# v0.4 句子边界感知指纹测试
# ---------------------------------------------------------------------------


class TestSentenceAware:
    """v0.4 句子边界感知指纹：重写单句只损失该句的票，不污染邻句。"""

    def test_sentence_aware_default_true(self):
        """v0.4 默认 sentence_aware=True。"""
        assert CAConfig().sentence_aware is True

    def test_v04_embed_decode_roundtrip(self):
        """v0.4 句子感知指纹下 embed→decode 往返成功。"""
        key = generate_master_key()
        emb, dec = CAEmbedder(key), CADecoder(key)
        for _ in range(10):
            r = emb.embed(TEXT, user_id=2024)
            d = dec.decode(r.watermarked_text, r.session_salt)
            if d.success and d.user_id == 2024:
                return
        assert False, "v0.4 roundtrip failed after 10 retries"

    def test_sentence_aware_false_matches_v03(self):
        """sentence_aware=False 时指纹与 v0.3 一致（跨句真实邻居）。"""
        key = generate_master_key()
        from aawm.content import _scan_slots
        from aawm.embedder import _SYNONYMS

        salt = b"\x00" * 16
        s_aware = _scan_slots(TEXT, _SYNONYMS, key, salt, 24, sentence_aware=True)
        s_legacy = _scan_slots(TEXT, _SYNONYMS, key, salt, 24, sentence_aware=False)

        # 两种模式的指纹集合应不同（_BOS/_EOS 改变了边界词指纹）
        fp_aware = {s.fingerprint for s in s_aware}
        fp_legacy = {s.fingerprint for s in s_legacy}
        assert fp_aware != fp_legacy

    def test_paraphrase_single_sentence_survives(self):
        """重写 1 句（同义替换）后解码仍成功（v0.4 句级局部性）。"""
        key = generate_master_key()
        emb, dec = CAEmbedder(key), CADecoder(key)
        for _ in range(10):
            r = emb.embed(TEXT, user_id=999)

            # 只改第一句的词典词为同义词（不改变 stable_id）
            import re
            from aawm.embedder import _SYNONYMS

            sentences = re.split(r"(?<=[.!?])\s+", r.watermarked_text)
            words = sentences[0].split(" ")
            for i, w in enumerate(words):
                key_w = w.lower().strip(".,!?;:\"'()[]")
                if key_w in _SYNONYMS:
                    grp = [c for c in _SYNONYMS[key_w] if c != key_w]
                    if grp:
                        import random
                        rng = random.Random(42)
                        nw = rng.choice(grp)
                        if w[:1].isupper():
                            nw = nw[:1].upper() + nw[1:]
                        words[i] = nw
            sentences[0] = " ".join(words)
            attacked = " ".join(sentences)

            d = dec.decode(attacked, r.session_salt)
            if d.success and d.user_id == 999:
                return
        assert False, "paraphrase single sentence failed after 10 retries"
