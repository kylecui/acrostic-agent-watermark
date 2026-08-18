"""信道 A：段落级 Merkle-HMAC 篡改绑定（v0.5）。

防篡改信道：脆而严。机制见 docs/design.md §13.1：
1. 段落规范化（NFC + 空白折叠）后逐段 HMAC，叶子绑定段序 idx
2. 叶子序列构建 Merkle root，连同可选 AAD（如 UID 声明）一并注册
3. 验证时：
   - root 匹配 → 文本未篡改（intact）
   - root 不匹配 + 段集合比对 → 定位被改/增/删的段落，
     段序 Merkle 另可检测"语义不变的重排"

与信道 B 的协同（§13.6）：
    A root 不匹配 + B 出 UID → 篡改确认 + 溯源归属双证据
"""
from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence

from .keys import KeyContext, derive_key

BINDING_VERSION = 1


def normalize_paragraph(text: str) -> str:
    """段落规范化：NFC + 连续空白折叠为单空格 + 去首尾空白。

    刻意保守：只消除无语义影响的排版差异，不改词形（词形由信道 B 负责）。
    """
    nfc = unicodedata.normalize("NFC", text)
    folded = " ".join(nfc.split())
    return folded


def split_paragraphs(text: str) -> List[str]:
    """按空行（连续 ≥2 个换行）分段；无空行时按单换行分段。"""
    if "\n\n" in text or "\r\n\r\n" in text:
        raw = text.replace("\r\n", "\n")
        parts = _split_blank_lines(raw)
    else:
        raw = text.replace("\r\n", "\n")
        parts = [p for p in raw.split("\n") if p.strip()]
    # 丢弃纯空白段（规范化为空串后无意义）
    return [normalize_paragraph(p) for p in parts if normalize_paragraph(p)]


def _split_blank_lines(text: str) -> List[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


class _Merkle:
    """叶子级 Merkle 树（奇数个叶子时复制最后一个）。"""

    @staticmethod
    def leaf_hash(key: bytes, idx: int, para_hash: bytes) -> bytes:
        mac = hmac.new(key, b"leaf:" + idx.to_bytes(4, "big") + para_hash, hashlib.sha256)
        return mac.digest()

    @staticmethod
    def combine(key: bytes, left: bytes, right: bytes) -> bytes:
        mac = hmac.new(key, b"node:" + left + right, hashlib.sha256)
        return mac.digest()

    @classmethod
    def root(cls, key: bytes, leaves: Sequence[bytes]) -> bytes:
        if not leaves:
            return hmac.new(key, b"empty", hashlib.sha256).digest()
        level = list(leaves)
        while len(level) > 1:
            if len(level) % 2 == 1:
                level.append(level[-1])
            level = [cls.combine(key, level[i], level[i + 1]) for i in range(0, len(level), 2)]
        return level[0]


@dataclass(frozen=True)
class BindingSeal:
    """文档绑定封签。

    Attributes:
        merkle_root: 段序 Merkle root（HMAC 域分隔）
        para_hashes: 规范化段落哈希序列（SHA-256，用于逐段比对定位）
        aad: 附加认证数据（如 UID 声明字节串；参与 root 计算）
        version: 机制版本号
    """

    merkle_root: bytes
    para_hashes: List[bytes] = field(default_factory=list)
    aad: bytes = b""
    version: int = BINDING_VERSION


class VerdictKind(Enum):
    INTACT = "intact"                # 未篡改
    TAMPERED = "tampered"            # 存在段内容被改
    REORDERED = "reordered"          # 段集合不变但顺序变了
    RESIZED = "resized"              # 段数不同（增/删段）
    EMPTY = "empty"                  # 待验文本无有效段


@dataclass(frozen=True)
class BindingVerdict:
    """验证判决。"""

    kind: VerdictKind
    root_match: bool
    matched_indices: List[int] = field(default_factory=list)   # 文本段 i -> seal 段 j（内容一致）
    mismatched_indices: List[int] = field(default_factory=list)  # 内容被改的文本段索引
    seal: Optional[BindingSeal] = None

    @property
    def ok(self) -> bool:
        return self.kind is VerdictKind.INTACT


class DocumentBinder:
    """信道 A 签名/验证器。

    用法::

        binder = DocumentBinder(master_key, session_salt)
        seal = binder.sign(text, aad=uid.to_bytes(2, "big"))
        verdict = binder.verify(text_later, seal)
    """

    def __init__(self, master_key: bytes, session_salt: bytes) -> None:
        self._k_para = derive_key(
            master_key,
            KeyContext(session_salt=session_salt, info=b"binding:para"),
        )
        self._k_merkle = derive_key(
            master_key,
            KeyContext(session_salt=session_salt, info=b"binding:merkle"),
        )

    # ------------------------------------------------------------------
    def _para_hash(self, norm_para: str) -> bytes:
        return hashlib.sha256(norm_para.encode("utf-8")).digest()

    def sign(self, text: str, *, aad: bytes = b"") -> BindingSeal:
        """对文本段落签名。aad 参与 root（绑定 UID 声明等元数据）。"""
        paras = split_paragraphs(text)
        para_hashes = [self._para_hash(p) for p in paras]
        leaves = [
            _Merkle.leaf_hash(self._k_merkle, i, h) for i, h in enumerate(para_hashes)
        ]
        root = _Merkle.root(self._k_merkle, leaves)
        if aad:
            root = hmac.new(self._k_merkle, b"aad:" + root + aad, hashlib.sha256).digest()
        return BindingSeal(
            merkle_root=root, para_hashes=para_hashes, aad=aad, version=BINDING_VERSION
        )

    # ------------------------------------------------------------------
    def verify(self, text: str, seal: BindingSeal) -> BindingVerdict:
        """验证文本与封签的一致性，并尽可能定位差异。"""
        paras = split_paragraphs(text)
        if not paras:
            return BindingVerdict(VerdictKind.EMPTY, root_match=False, seal=seal)

        para_hashes = [self._para_hash(p) for p in paras]
        leaves = [
            _Merkle.leaf_hash(self._k_merkle, i, h) for i, h in enumerate(para_hashes)
        ]
        root = _Merkle.root(self._k_merkle, leaves)
        if seal.aad:
            root = hmac.new(self._k_merkle, b"aad:" + root + seal.aad, hashlib.sha256).digest()
        root_match = hmac.compare_digest(root, seal.merkle_root)

        if root_match:
            return BindingVerdict(
                VerdictKind.INTACT, root_match=True,
                matched_indices=list(range(len(paras))), seal=seal,
            )

        # root 不匹配：逐段比对定位（文本段 -> seal 段 hash 匹配）
        seal_lookup = {}
        for j, h in enumerate(seal.para_hashes):
            seal_lookup.setdefault(h, []).append(j)
        matched: List[int] = []
        mismatched: List[int] = []
        matched_map: List[int] = []  # 文本段 i -> seal 段 j（内容一致时的位置映射）
        for i, h in enumerate(para_hashes):
            js = seal_lookup.get(h)
            if js:
                matched.append(i)
                matched_map.append(js.pop(0))
            else:
                mismatched.append(i)

        if len(paras) != len(seal.para_hashes):
            kind = VerdictKind.RESIZED
        elif mismatched:
            kind = VerdictKind.TAMPERED
        elif matched_map == list(range(len(paras))):
            # 内容与顺序都与 seal 一致，root 却不匹配：
            # 只可能是密钥不符或 AAD 被换 —— 保守判篡改（不可信）。
            kind = VerdictKind.TAMPERED
        else:
            kind = VerdictKind.REORDERED  # 段集合同、顺序变
        return BindingVerdict(
            kind=kind,
            root_match=False,
            matched_indices=matched,
            mismatched_indices=mismatched,
            seal=seal,
        )
