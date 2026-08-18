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
