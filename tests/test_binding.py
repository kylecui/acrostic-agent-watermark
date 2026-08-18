"""信道 A（段落 Merkle-HMAC 绑定）单元测试：v0.5。"""
import random

import pytest

from aawm.binding import (
    BindingSeal,
    DocumentBinder,
    VerdictKind,
    normalize_paragraph,
    split_paragraphs,
)
from aawm.keys import generate_master_key, generate_session_salt

pytestmark = pytest.mark.order(5)

DOC = (
    "The system collects telemetry from distributed agents.\n\n"
    "Each agent embeds a per-user watermark into its output tokens.\n\n"
    "The verifier recomputes anchors with the shared key and decodes the user id.\n\n"
    "Tampering with any paragraph breaks the corresponding hash chain."
)


def make_binder(seed: int = 42) -> DocumentBinder:
    rng = random.Random(seed)
    return DocumentBinder(
        bytes(rng.randrange(256) for _ in range(32)),
        bytes(rng.randrange(256) for _ in range(16)),
    )


class TestNormalize:
    def test_whitespace_folding(self):
        assert normalize_paragraph("a  b\t c\n d ") == "a b c d"

    def test_nfc(self):
        # 组合字符 e + combining acute -> NFC 单字符
        assert normalize_paragraph("cafe\u0301") == "café"

    def test_split_paragraphs_blank_lines(self):
        paras = split_paragraphs(DOC)
        assert len(paras) == 4
        assert all(p for p in paras)

    def test_split_single_newlines(self):
        paras = split_paragraphs("line one\nline two\nline three")
        assert len(paras) == 3


class TestSignVerify:
    def test_intact(self):
        binder = make_binder()
        seal = binder.sign(DOC, aad=(0x1234).to_bytes(2, "big"))
        v = binder.verify(DOC, seal)
        assert v.ok and v.kind is VerdictKind.INTACT
        assert v.root_match

    def test_normalization_tolerated(self):
        """空白排版差异不算篡改。"""
        binder = make_binder()
        seal = binder.sign(DOC)
        noisy = DOC.replace("collects", "collects   ").replace("\n\n", "\n\n\n")
        v = binder.verify(noisy, seal)
        assert v.ok

    def test_single_paragraph_tampered_and_localized(self):
        binder = make_binder()
        seal = binder.sign(DOC)
        tampered = DOC.replace(
            "embeds a per-user watermark", "removes all watermarks"
        )
        v = binder.verify(tampered, seal)
        assert not v.root_match
        assert v.kind is VerdictKind.TAMPERED
        assert v.mismatched_indices == [1]  # 第 2 段被改
        assert 0 in v.matched_indices and 2 in v.matched_indices

    def test_reorder_detected(self):
        binder = make_binder()
        seal = binder.sign(DOC)
        p = DOC.split("\n\n")
        reordered = "\n\n".join([p[1], p[0], p[2], p[3]])
        v = binder.verify(reordered, seal)
        assert v.kind is VerdictKind.REORDERED
        assert not v.root_match
        assert v.mismatched_indices == []  # 内容都匹配，只是顺序变了

    def test_extension_detected(self):
        binder = make_binder()
        seal = binder.sign(DOC)
        extended = DOC + "\n\nAn attacker appended this paragraph."
        v = binder.verify(extended, seal)
        assert v.kind is VerdictKind.RESIZED
        assert v.mismatched_indices == [4]  # 新增段不在 seal 中

    def test_deletion_detected(self):
        binder = make_binder()
        seal = binder.sign(DOC)
        p = DOC.split("\n\n")
        deleted = "\n\n".join([p[0], p[2], p[3]])
        v = binder.verify(deleted, seal)
        assert v.kind is VerdictKind.RESIZED

    def test_wrong_key_fails(self):
        b1, b2 = make_binder(1), make_binder(2)
        seal = b1.sign(DOC)
        v = b2.verify(DOC, seal)
        assert not v.root_match
        assert v.kind is VerdictKind.TAMPERED  # 全部段 hash 不匹配

    def test_aad_bound(self):
        """AAD（UID 声明）参与 root：换 AAD 验证失败。"""
        binder = make_binder()
        seal = binder.sign(DOC, aad=(0x1234).to_bytes(2, "big"))
        v = binder.verify(DOC, BindingSeal(
            merkle_root=seal.merkle_root,
            para_hashes=seal.para_hashes,
            aad=(0x4321).to_bytes(2, "big"),
        ))
        assert not v.root_match

    def test_empty_text(self):
        binder = make_binder()
        seal = binder.sign(DOC)
        v = binder.verify("   \n\n  ", seal)
        assert v.kind is VerdictKind.EMPTY


class TestMerkleInternals:
    def test_leaf_order_binding(self):
        """叶子绑段序：交换两段会改变 root。"""
        from aawm.binding import _Merkle
        key = b"k" * 32
        leaves = [_Merkle.leaf_hash(key, i, bytes([i])) for i in range(3)]
        r1 = _Merkle.root(key, leaves)
        r2 = _Merkle.root(key, [leaves[1], leaves[0], leaves[2]])
        assert r1 != r2

    def test_empty_root_stable(self):
        from aawm.binding import _Merkle
        key = b"k" * 32
        assert _Merkle.root(key, []) == _Merkle.root(key, [])
