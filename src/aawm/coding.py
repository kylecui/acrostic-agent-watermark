"""信道编码模块：用户 ID 的检错与纠错编码。

水印信道是有噪信道：
- 嵌入时锚点可能落在词典未覆盖的词上（该位成为随机错误）
- 发布后文本可能被编辑/改写（同义词被换回、词被替换）

编码流水线：
    user_id (16 bits) --CRC-8--> payload (24 bits) --ECC--> codeword (24*r bits)

解码流水线：
    codeword_bits (从锚点读出) --ECC decode--> payload --CRC 校验--> user_id

CRC 通过（1/256 随机概率）+ 纠错动作数 是解码置信度的依据。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# 位串工具
# ---------------------------------------------------------------------------


def int_to_bits(value: int, width: int) -> List[int]:
    """整数 → 位串（大端，高位在前）。"""
    if value < 0:
        raise ValueError("value must be non-negative")
    if value >= (1 << width):
        raise ValueError(f"value {value} does not fit in {width} bits")
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def bits_to_int(bits: List[int]) -> int:
    """位串 → 整数（大端）。"""
    return sum(b << (len(bits) - 1 - i) for i, b in enumerate(bits))


def bytes_to_bits(data: bytes) -> List[int]:
    return [ (byte >> (7 - i)) & 1 for byte in data for i in range(8) ]


def bits_to_bytes(bits: List[int]) -> bytes:
    if len(bits) % 8 != 0:
        raise ValueError("bits length must be a multiple of 8")
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return bytes(out)


def hamming_distance(a: List[int], b: List[int]) -> int:
    if len(a) != len(b):
        raise ValueError("length mismatch")
    return sum(1 for x, y in zip(a, b) if x != y)


# ---------------------------------------------------------------------------
# CRC-8（多项式 0x07，即 CRC-8/ATM）
# ---------------------------------------------------------------------------


def crc8(data: bytes) -> int:
    poly = 0x07
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


# ---------------------------------------------------------------------------
# 载荷构造 / 解析：user_id_bits || CRC-8
# ---------------------------------------------------------------------------


def build_payload(user_id: int, user_id_bits: int = 16) -> List[int]:
    """构造载荷：user_id 位串 + 其 CRC-8 校验位。

    user_id_bits 必须是 8 的倍数（CRC 按字节计算）。
    """
    if user_id_bits % 8 != 0:
        raise ValueError("user_id_bits must be a multiple of 8")
    uid_bytes = int(user_id).to_bytes(user_id_bits // 8, "big")
    checksum = crc8(uid_bytes)
    return int_to_bits(user_id, user_id_bits) + int_to_bits(checksum, 8)


def parse_payload(payload: List[int], user_id_bits: int = 16) -> Tuple[int, bool]:
    """解析载荷。返回 (user_id, crc_ok)。"""
    if len(payload) != user_id_bits + 8:
        raise ValueError(f"payload length mismatch: {len(payload)} != {user_id_bits + 8}")
    uid = bits_to_int(payload[:user_id_bits])
    crc_bits = payload[user_id_bits:]
    expected = crc8(uid.to_bytes(user_id_bits // 8, "big"))
    crc_ok = bits_to_int(crc_bits) == expected
    return uid, crc_ok


# ---------------------------------------------------------------------------
# 信道编码（ECC）抽象
# ---------------------------------------------------------------------------


class ChannelCode(ABC):
    """信道编码接口。"""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def encode(self, payload_bits: List[int]) -> List[int]:
        """payload (k bits) → codeword (n bits)。"""
        ...

    @abstractmethod
    def decode(self, code_bits: List[int]) -> Tuple[List[int], int]:
        """codeword (n bits) → (payload (k bits), n_corrected)。

        n_corrected 是解码器实际翻转的位数（纠错动作数）。
        注意：超过纠错能力的错误会静默产出错误载荷，由 CRC 兜底。
        """
        ...

    @property
    @abstractmethod
    def payload_bits(self) -> int: ...

    @property
    @abstractmethod
    def codeword_bits(self) -> int: ...


class RepetitionCode(ChannelCode):
    """重复码：每 bit 重复 r 次，多数表决解码。

    r=3 时可容忍每 bit 1 位错误 → 总错误容忍度 ≈ 33%。
    r 必须为奇数（避免平票）。
    """

    def __init__(self, payload_bits: int = 24, r: int = 3) -> None:
        if r < 1 or r % 2 == 0:
            raise ValueError("repetition factor must be odd and >= 1")
        self._payload_bits = payload_bits
        self.r = r

    @property
    def name(self) -> str:
        return f"repeat{self.r}"

    @property
    def payload_bits(self) -> int:
        return self._payload_bits

    @property
    def codeword_bits(self) -> int:
        return self._payload_bits * self.r

    def encode(self, payload_bits: List[int]) -> List[int]:
        if len(payload_bits) != self._payload_bits:
            raise ValueError(f"payload must be {self._payload_bits} bits")
        return [b for b in payload_bits for _ in range(self.r)]

    def decode(self, code_bits: List[int]) -> Tuple[List[int], int]:
        if len(code_bits) != self.codeword_bits:
            raise ValueError(f"codeword must be {self.codeword_bits} bits")
        out: List[int] = []
        n_corrected = 0
        for i in range(0, len(code_bits), self.r):
            chunk = code_bits[i:i + self.r]
            ones = sum(chunk)
            bit = 1 if ones * 2 > self.r else 0
            n_corrected += self.r - max(ones, self.r - ones)
            out.append(bit)
        return out, n_corrected


class Hamming74Code(ChannelCode):
    """汉明 (7,4) 码：4 数据位 + 3 校验位，每组可纠 1 位错。

    码率 4/7 ≈ 0.57（高于重复码的 1/3），总纠错容忍度 ≈ 1/7 ≈ 14%。
    适合词典覆盖率高、信道较干净的场景。
    """

    def __init__(self, payload_bits: int = 24) -> None:
        if payload_bits % 4 != 0:
            raise ValueError("payload_bits must be a multiple of 4 for Hamming(7,4)")
        self._payload_bits = payload_bits

    @property
    def name(self) -> str:
        return "hamming74"

    @property
    def payload_bits(self) -> int:
        return self._payload_bits

    @property
    def codeword_bits(self) -> int:
        return (self._payload_bits // 4) * 7

    def encode(self, payload_bits: List[int]) -> List[int]:
        if len(payload_bits) != self._payload_bits:
            raise ValueError(f"payload must be {self._payload_bits} bits")
        out: List[int] = []
        for i in range(0, self._payload_bits, 4):
            d1, d2, d3, d4 = payload_bits[i:i + 4]
            p1 = d1 ^ d2 ^ d4
            p2 = d1 ^ d3 ^ d4
            p3 = d2 ^ d3 ^ d4
            out += [p1, p2, d1, p3, d2, d3, d4]
        return out

    def decode(self, code_bits: List[int]) -> Tuple[List[int], int]:
        if len(code_bits) != self.codeword_bits:
            raise ValueError(f"codeword must be {self.codeword_bits} bits")
        out: List[int] = []
        n_corrected = 0
        for i in range(0, len(code_bits), 7):
            block = list(code_bits[i:i + 7])
            p1, p2, d1, p3, d2, d3, d4 = block
            s1 = p1 ^ d1 ^ d2 ^ d4
            s2 = p2 ^ d1 ^ d3 ^ d4
            s3 = p3 ^ d2 ^ d3 ^ d4
            syndrome = s1 | (s2 << 1) | (s3 << 2)
            if syndrome != 0:
                if syndrome <= 7:
                    block[syndrome - 1] ^= 1
                    n_corrected += 1
                # syndrome == 0 之外的非法值不存在（3 bits 最大 7）
            out += [block[2], block[4], block[5], block[6]]
        return out, n_corrected


class SpreadRepetitionCode(ChannelCode):
    """交织重复码：bit j 的 r 票分散在全文前/中/后。

    与顺序 RepetitionCode 的区别：后者的 r 票相邻（3i, 3i+1, 3i+2），
    局部突发（如一句话被改写）会同时击毁同一 bit 的多票；
    交织版让突发错误分散到不同 bit，多数表决恢复率显著更高。

    对独立随机攻击两者理论等价；对局部化改写攻击交织版占优。
    """

    def __init__(self, payload_bits: int = 24, r: int = 3) -> None:
        if r < 1 or r % 2 == 0:
            raise ValueError("repetition factor must be odd and >= 1")
        self._payload_bits = payload_bits
        self.r = r

    @property
    def name(self) -> str:
        return f"spread{self.r}"

    @property
    def payload_bits(self) -> int:
        return self._payload_bits

    @property
    def codeword_bits(self) -> int:
        return self._payload_bits * self.r

    def encode(self, payload_bits: List[int]) -> List[int]:
        if len(payload_bits) != self._payload_bits:
            raise ValueError(f"payload must be {self._payload_bits} bits")
        n = self.codeword_bits
        return [payload_bits[j % self._payload_bits] for j in range(n)]

    def decode(self, code_bits: List[int]) -> Tuple[List[int], int]:
        if len(code_bits) != self.codeword_bits:
            raise ValueError(f"codeword must be {self.codeword_bits} bits")
        k = self._payload_bits
        votes: List[List[int]] = [[] for _ in range(k)]
        for j, b in enumerate(code_bits):
            votes[j % k].append(b)
        out: List[int] = []
        n_corrected = 0
        for v in votes:
            ones = sum(v)
            bit = 1 if ones * 2 > len(v) else 0
            n_corrected += len(v) - max(ones, len(v) - ones)
            out.append(bit)
        return out, n_corrected


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_CODE_FACTORIES: Dict[str, callable] = {
    "spread3": lambda pb: SpreadRepetitionCode(pb, 3),
    "spread5": lambda pb: SpreadRepetitionCode(pb, 5),
    "repeat3": lambda pb: RepetitionCode(pb, 3),
    "repeat5": lambda pb: RepetitionCode(pb, 5),
    "repeat1": lambda pb: RepetitionCode(pb, 1),  # 无纠错，纯 CRC 检错
    "hamming74": lambda pb: Hamming74Code(pb),
}


def get_code(name: str, payload_bits: int = 24) -> ChannelCode:
    if name not in _CODE_FACTORIES:
        raise KeyError(f"unknown code: {name!r}, available: {list(_CODE_FACTORIES)}")
    return _CODE_FACTORIES[name](payload_bits)


def available_codes() -> List[str]:
    return list(_CODE_FACTORIES)
