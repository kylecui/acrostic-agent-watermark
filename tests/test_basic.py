"""AAWM v0.2 测试套件：用户 ID 编码水印。"""
import random

import pytest

from aawm import (
    Decoder,
    EmbedConfig,
    Embedder,
    KeyedLetterMap,
    Verifier,
    generate_master_key,
    generate_session_salt,
)
from aawm.coding import (
    Hamming74Code,
    RepetitionCode,
    SpreadRepetitionCode,
    bits_to_int,
    build_payload,
    crc8,
    get_code,
    hamming_distance,
    int_to_bits,
    parse_payload,
)
from aawm.embedder import _SYNONYMS, diagnose_synonym_groups

SAMPLE_TEXT = (
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


# ---------------------------------------------------------------------------
# 信道编码
# ---------------------------------------------------------------------------


class TestCoding:
    def test_int_bits_roundtrip(self):
        for v in [0, 1, 42, 255, 65535, 2**31 - 1]:
            assert bits_to_int(int_to_bits(v, 32)) == v

    def test_int_to_bits_range(self):
        with pytest.raises(ValueError):
            int_to_bits(256, 8)

    def test_crc8_known_value(self):
        # CRC-8/ATM 标准测试向量: "123456789" -> 0xF4
        assert crc8(b"123456789") == 0xF4

    def test_payload_roundtrip(self):
        for uid in [0, 1, 42, 1001, 65535]:
            payload = build_payload(uid, 16)
            out, crc_ok = parse_payload(payload, 16)
            assert out == uid and crc_ok

    def test_payload_detects_corruption(self):
        payload = build_payload(1001, 16)
        payload[3] ^= 1  # 翻转一位
        _, crc_ok = parse_payload(payload, 16)
        assert not crc_ok

    def test_repetition_roundtrip(self):
        code = RepetitionCode(24, 3)
        payload = build_payload(1001, 16)
        cw = code.encode(payload)
        out, n_corr = code.decode(cw)
        assert out == payload and n_corr == 0

    def test_repetition_corrects_1_of_3(self):
        code = RepetitionCode(24, 3)
        payload = build_payload(1001, 16)
        cw = list(code.encode(payload))
        cw[0] ^= 1  # bit 0 三票中错一票
        out, n_corr = code.decode(cw)
        assert out == payload and n_corr == 1

    def test_spread_roundtrip(self):
        code = SpreadRepetitionCode(24, 3)
        payload = build_payload(2002, 16)
        out, n_corr = code.decode(code.encode(payload))
        assert out == payload and n_corr == 0

    def test_spread_interleaves(self):
        """交织验证：码字 = payload 重复三轮（spread 映射 j%24）。"""
        code = SpreadRepetitionCode(24, 3)
        payload = build_payload(42, 16)
        cw = code.encode(payload)
        assert cw[:24] == payload and cw[24:48] == payload and cw[48:] == payload

    def test_hamming74_corrects_single_error(self):
        code = Hamming74Code(24)
        payload = build_payload(3003, 16)
        cw = list(code.encode(payload))
        # 在每组各翻转一位（共 6 位错，每组可纠 1 位）
        for g in range(6):
            cw[g * 7 + 2] ^= 1
        out, n_corr = code.decode(cw)
        assert out == payload and n_corr == 6

    def test_get_code_registry(self):
        for name in ["spread3", "spread5", "repeat3", "repeat1", "hamming74"]:
            c = get_code(name, 24)
            assert c.payload_bits == 24


# ---------------------------------------------------------------------------
# KeyedLetterMap
# ---------------------------------------------------------------------------


class TestKeyedLetterMap:
    def test_deterministic(self):
        seed = bytes(range(32))
        assert KeyedLetterMap(seed).mapping == KeyedLetterMap(seed).mapping

    def test_balanced_partition(self):
        m = KeyedLetterMap(bytes(range(32)))
        zeros = sum(1 for v in m.mapping.values() if v == 0)
        assert zeros == 13  # 13/13 均分

    def test_position_dependence(self):
        """不同位置种子 → 独立映射。"""
        seeds = [bytes([i]) * 32 for i in range(4)]
        maps = [KeyedLetterMap(s).mapping for s in seeds]
        for i in range(4):
            for j in range(i + 1, 4):
                assert maps[i] != maps[j]

    def test_token_case_insensitive(self):
        m = KeyedLetterMap(bytes(range(32)))
        assert m.token_to_bit("System") == m.token_to_bit("system")
        assert m.token_to_bit("!system") == m.token_to_bit("system")


# ---------------------------------------------------------------------------
# 嵌入与解码
# ---------------------------------------------------------------------------


class TestEmbedDecode:
    def test_roundtrip_multiple_users(self):
        key = generate_master_key()
        emb, dec = Embedder(key), Decoder(key)
        for uid in [0, 1, 42, 1001, 65535]:
            r = emb.embed(SAMPLE_TEXT, user_id=uid)
            d = dec.decode(r.watermarked_text, r.session_salt)
            assert d.success and d.user_id == uid, f"uid={uid} 往返失败"

    def test_zero_skipped_anchors(self):
        """可表达锚点池保证 skip 严格为零（多密钥）。"""
        for _ in range(3):
            key = generate_master_key()
            r = Embedder(key).embed(SAMPLE_TEXT, user_id=7)
            assert r.n_skipped == 0

    def test_deterministic_embedding(self):
        key = generate_master_key()
        salt = generate_session_salt()
        emb = Embedder(key)
        r1 = emb.embed(SAMPLE_TEXT, user_id=42, session_salt=salt)
        r2 = emb.embed(SAMPLE_TEXT, user_id=42, session_salt=salt)
        assert r1.watermarked_text == r2.watermarked_text

    def test_per_user_texts_differ(self):
        key = generate_master_key()
        emb = Embedder(key)
        texts = {
            uid: emb.embed(SAMPLE_TEXT, user_id=uid).watermarked_text
            for uid in [1001, 2002, 3003]
        }
        assert len(set(texts.values())) == 3

    def test_wrong_key_fails(self):
        key = generate_master_key()
        r = Embedder(key).embed(SAMPLE_TEXT, user_id=42)
        d = Decoder(generate_master_key()).decode(r.watermarked_text, r.session_salt)
        assert not d.success

    def test_unwatermarked_text_fails(self):
        """无水印文本：CRC 通过概率仅 2^-8，多次断言统计压倒性失败。"""
        key = generate_master_key()
        dec = Decoder(key)
        fails = 0
        for t in range(8):
            d = dec.decode(SAMPLE_TEXT, generate_session_salt())
            if not d.success:
                fails += 1
        assert fails >= 7  # 全失败概率 (255/256)^8 ≈ 96.9%

    def test_cross_salt_fails(self):
        key = generate_master_key()
        emb = Embedder(key)
        r = emb.embed(SAMPLE_TEXT, user_id=42)
        # 随机盐下 CRC 偶然通过的概率 1/256，但解出原 UID 还需再
        # 1/65536 → 断言"不能解出原 UID"使 flaky 率降至 ~6e-8
        d = Decoder(key).decode(r.watermarked_text, generate_session_salt())
        assert not (d.success and d.user_id == 42)

    def test_capacity_error_on_short_text(self):
        key = generate_master_key()
        with pytest.raises(ValueError, match="容量不足"):
            Embedder(key).embed("Too short text.", user_id=1)

    def test_invalid_user_id(self):
        key = generate_master_key()
        with pytest.raises(ValueError):
            Embedder(key).embed(SAMPLE_TEXT, user_id=70000)  # 16-bit 上界 65535

    def test_identify(self):
        key = generate_master_key()
        emb, dec = Embedder(key), Decoder(key)
        r = emb.embed(SAMPLE_TEXT, user_id=2002)
        assert dec.identify(
            r.watermarked_text, r.session_salt, candidate_ids=[1001, 2002, 3003]
        ) == 2002

    def test_verifier_detect(self):
        key = generate_master_key()
        emb = Embedder(key)
        r = emb.embed(SAMPLE_TEXT, user_id=42)
        v = Verifier(key)
        det = v.detect(r.watermarked_text, r.session_salt)
        assert det.detected and det.payload.user_id == 42
        det2 = v.detect(SAMPLE_TEXT, r.session_salt)
        assert not det2.detected

    def test_hamming74_config(self):
        """低开销编码：42 锚点即可嵌入（更短文本可用）。"""
        key = generate_master_key()
        cfg = EmbedConfig(code_name="hamming74")
        emb, dec = Embedder(key, cfg), Decoder(key, cfg)
        r = emb.embed(SAMPLE_TEXT, user_id=42)
        assert r.n_anchors == 42
        d = dec.decode(r.watermarked_text, r.session_salt)
        assert d.success and d.user_id == 42


# ---------------------------------------------------------------------------
# 攻击鲁棒性
# ---------------------------------------------------------------------------


def _flip_attack(text: str, n_flips: int, rng: random.Random) -> str:
    """攻击者模型：知词典，不知锚点与映射，随机同义翻转。"""
    words = text.split()
    cands = [
        i for i, w in enumerate(words)
        if w.lower().strip(".,!?;:\"'") in _SYNONYMS
    ]
    rng.shuffle(cands)
    done = 0
    for i in cands:
        if done >= n_flips:
            break
        key = words[i].lower().strip(".,!?;:\"'")
        grp = [c for c in _SYNONYMS[key] if c != key]
        if not grp:
            continue
        nw = rng.choice(grp)
        if words[i][:1].isupper():
            nw = nw[:1].upper() + nw[1:]
        words[i] = nw
        done += 1
    return " ".join(words)


class TestRobustness:
    def test_mild_attack_survival(self):
        """轻攻击（5 词翻转）下成功率应 ≥ 60%。"""
        key = generate_master_key()
        emb, dec = Embedder(key), Decoder(key)
        r = emb.embed(SAMPLE_TEXT, user_id=1001)
        rng = random.Random(2026)
        ok = 0
        for _ in range(20):
            attacked = _flip_attack(r.watermarked_text, 5, rng)
            d = dec.decode(attacked, r.session_salt)
            if d.success and d.user_id == 1001:
                ok += 1
        assert ok >= 12, f"轻攻击存活率过低: {ok}/20"

    def test_heavy_attack_no_false_id(self):
        """重攻击下可解码失败，但不得稳定解出错误 ID。"""
        key = generate_master_key()
        emb, dec = Embedder(key), Decoder(key)
        r = emb.embed(SAMPLE_TEXT, user_id=1001)
        rng = random.Random(99)
        wrong_id_count = 0
        for _ in range(10):
            attacked = _flip_attack(r.watermarked_text, 30, rng)
            d = dec.decode(attacked, r.session_salt)
            if d.success and d.user_id != 1001:
                wrong_id_count += 1
        assert wrong_id_count <= 1  # 解错他人 ID 的概率应极低


# ---------------------------------------------------------------------------
# 词典质量
# ---------------------------------------------------------------------------


class TestSynonyms:
    def test_dictionary_size(self):
        assert len(_SYNONYMS) >= 2000  # v0.4 扩充后 ~2300 词条

    def test_average_dynamic_anchorability(self):
        """v0.4：静态半区覆盖已不是准确度量（v0.2 起可锚定由动态
        KeyedLetterMap 决定）。度量改为：组在随机映射下能表达双 bit 的
        平均概率（动态可锚定率），阈值 0.75（实测 ~0.77）。"""
        from aawm.keys import derive_key, generate_master_key, KeyContext
        from aawm.transforms import KeyedLetterMap

        groups = {tuple(v) for v in _SYNONYMS.values()}
        maps = [
            KeyedLetterMap(
                derive_key(
                    generate_master_key(),
                    KeyContext(session_salt=b"bench", info=b"aawm:map"),
                )
            )
            for _ in range(20)
        ]
        total = sum(
            sum(
                1 for m in maps
                if {m.token_to_bit(w) for w in g} == {0, 1}
            ) / len(maps)
            for g in groups
        )
        avg = total / len(groups)
        assert avg >= 0.75, f"平均动态可锚定率过低: {avg:.3f}"

    def test_no_multiword_candidates(self):
        """候选必须是单词（多词短语会破坏 token 数稳定）。"""
        for cands in _SYNONYMS.values():
            for c in cands:
                assert " " not in c, f"多词候选: {c!r}"
