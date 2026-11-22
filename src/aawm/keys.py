"""密钥派生模块。

用 master_key + session_salt + context 派生锚点种子和谓词参数。
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class KeyContext:
    """密钥派生上下文。

    Attributes:
        session_salt: 每会话随机盐（公开，防跨会话关联）
        position: token 位置索引（用于派生位置局部种子）
        tag: 任意用途标签（如 v0.3 内容寻址锚点的内容指纹）。
            与 info 的区别：info 是静态用途字符串，tag 是动态绑定数据。
        info: 用途标签，如 "anchor" / "pred"
    """
    session_salt: bytes
    position: Optional[int] = None
    tag: Optional[bytes] = None
    info: bytes = b"aawm"


def _hkdf_expand_extract(
    master_key: bytes, salt: bytes, info: bytes, length: int = 32
) -> bytes:
    """简化版 HKDF（RFC 5869）。

    用 hashlib 实现以避免额外依赖。
    """
    if length > 255 * 32:
        raise ValueError("requested length too long for HKDF-SHA256")
    prk = hmac.new(salt, master_key, hashlib.sha256).digest()
    t = b""
    okm = b""
    for i in range(1, (length + 31) // 32 + 1):
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
    return okm[:length]


def derive_key(
    master_key: bytes,
    context: KeyContext,
    length: int = 32,
) -> bytes:
    """从 master_key 派生子密钥。

    Args:
        master_key: agent 实例主密钥（>= 32 bytes 推荐）
        context: 派生上下文
        length: 输出字节长度

    Returns:
        派生密钥字节串
    """
    if len(master_key) < 16:
        raise ValueError("master_key too short (>= 16 bytes required)")
    info = context.info
    if context.position is not None:
        info = info + b":pos:" + str(context.position).encode()
    if context.tag is not None:
        info = info + b":tag:" + context.tag
    return _hkdf_expand_extract(master_key, context.session_salt, info, length)


def generate_master_key() -> bytes:
    """生成一个新的 master_key（32 字节）。"""
    return os.urandom(32)


def generate_session_salt() -> bytes:
    """生成会话盐（16 字节，可公开）。"""
    return os.urandom(16)


def derive_session_salt(
    master_key: bytes,
    text: str,
    *,
    nonce: int = 0,
    head_chars: Optional[int] = None,
) -> bytes:
    """从 master_key + 文本内容确定性派生会话盐（v0.15）。

    v0.15 起 embed 不传 session_salt 时默认使用本函数（nonce=0），
    盐与文本内容绑定：meta 存档散失时，trace 可从嫌疑文本重算盐
    恢复溯源（不再依赖 meta_store 零失误）。重复嵌入同一文本得到
    同一盐，行为可预期、可复现。

    攻击/排版差异容忍：head_chars 给出时只用规范化文本的前 N 字符
    派生——编辑攻击改了中段但开头幸存时仍可能命中（trace 的候选
    盐列表会同时尝试全文派生与 head 派生，取信号最强者）。

    Args:
        master_key: agent 实例主密钥
        text: 待绑定文本（嵌入时=原文，溯源时=嫌疑文本）
        nonce: 重试序号（embed 自检换盐重试时递增 0..3）
        head_chars: 只用前 N 个规范化字符（None=全文）

    Returns:
        16 字节会话盐
    """
    import unicodedata

    norm = unicodedata.normalize("NFC", " ".join(text.split()))
    if head_chars is not None:
        norm = norm[:head_chars]
    digest = hashlib.sha256(norm.encode("utf-8")).digest()
    return hmac.new(
        master_key,
        b"aawm:v1:salt" + nonce.to_bytes(2, "big") + digest,
        hashlib.sha256,
    ).digest()[:16]
