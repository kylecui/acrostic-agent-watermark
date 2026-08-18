"""谓词与符号映射模块。

两类机制：
1. Predicate（detect 模式用）：密钥决定谓词参数，检验 token 是否满足 → z-test 检测水印存在性
2. KeyedLetterMap（decode 模式用）：密钥派生"字母→bit"映射，每锚点携带 1 bit 数据 → 可解码出用户 ID
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class Predicate(ABC):
    """谓词基类。"""

    @abstractmethod
    def evaluate(self, token: str, params: Dict[str, Any]) -> int:
        """评估 token 是否满足谓词。

        Args:
            token: 待评估 token
            params: 谓词参数（由密钥派生）

        Returns:
            1 表示满足，0 表示不满足
        """
        ...

    @abstractmethod
    def derive_params(self, predicate_seed: bytes) -> Dict[str, Any]:
        """从密钥派生谓词参数。"""
        ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def bits_per_anchor(self) -> int:
        """每个锚点承载的比特数（默认 1）。"""
        return 1


class FirstLetterPredicate(Predicate):
    """P1: token 首字母落在密钥指定的字母区间。

    高鲁棒性谓词：抗 token 化变化，验证只需首字母。
    """

    NAME = "first_letter"

    @property
    def name(self) -> str:
        return self.NAME

    def derive_params(self, predicate_seed: bytes) -> Dict[str, Any]:
        # 用种子的第一字节决定"前半字母" 还是 "后半字母"
        first_byte = predicate_seed[0] if predicate_seed else 0
        # bit=0: A-M, bit=1: N-Z
        bit = first_byte & 1
        return {"bit": bit}

    def evaluate(self, token: str, params: Dict[str, Any]) -> int:
        bit = params["bit"]
        if not token:
            return 0
        # 取首字母（跳过非字母前缀）
        for ch in token:
            if ch.isalpha():
                upper = ch.upper()
                if bit == 0:
                    return 1 if "A" <= upper <= "M" else 0
                else:
                    return 1 if "N" <= upper <= "Z" else 0
        return 0


# 谓词注册表
_REGISTRY: Dict[str, Predicate] = {}


def register_predicate(predicate: Predicate) -> None:
    """注册谓词实例。"""
    _REGISTRY[predicate.name] = predicate


def get_predicate(name: str) -> Predicate:
    """按名查找谓词。"""
    if name not in _REGISTRY:
        raise KeyError(f"predicate not registered: {name}")
    return _REGISTRY[name]


# 默认注册
_DEFAULT = FirstLetterPredicate()
register_predicate(_DEFAULT)


# ---------------------------------------------------------------------------
# KeyedLetterMap：密钥派生的字母→bit 符号映射（decode 模式的核心）
# ---------------------------------------------------------------------------


_ALPHABET = [chr(ord("A") + i) for i in range(26)]


class KeyedLetterMap:
    """密钥派生的"首字母 → bit"映射。

    构造：用密钥种子对字母表做 Fisher-Yates 伪随机洗牌，
    前半映射 bit=0，后半映射 bit=1。

    安全性质：
    - 无密钥者不知道映射，无法构造携带指定 user_id 的水印（防 framing）
    - 每个锚点位置派生独立映射，跨位置无统计规律可循（防合谋统计）
    - 均匀二分：随机文本的任意位置读出的 bit 服从 Bernoulli(0.5)

    用法：
        m = KeyedLetterMap(seed)                    # 默认 26 字母
        m = KeyedLetterMap(seed, alphabet=声母表)    # 中文声母
        bit = m.token_to_bit("System")        # 读出该词携带的 bit
        letters = m.letters_for_bit(1)        # bit=1 对应的字母集合
    """

    def __init__(
        self,
        seed: bytes,
        alphabet: Optional[List[str]] = None,
        symbol_extractor: Optional["callable"] = None,
    ) -> None:
        if alphabet is None:
            alphabet = _ALPHABET
        self._alphabet = list(alphabet)
        self._symbol_extractor = symbol_extractor  # None 用默认 _extract_symbol
        n = len(self._alphabet)
        if n < 2:
            raise ValueError("alphabet must have >= 2 symbols")
        half = n // 2

        # 从种子生成确定性的伪随机流
        stream = bytearray()
        counter = 0
        needed = 2 * n
        while len(stream) < needed:
            stream += hashlib.sha256(
                b"aawm:lettermap:" + seed + counter.to_bytes(4, "big")
            ).digest()
            counter += 1

        letters = list(self._alphabet)
        # Fisher-Yates 洗牌（从尾往头）
        for i in range(n - 1, 0, -1):
            j = int.from_bytes(bytes(stream[2 * i:2 * i + 2]), "big") % (i + 1)
            letters[i], letters[j] = letters[j], letters[i]

        self._map: Dict[str, int] = {
            ch: (0 if idx < half else 1) for idx, ch in enumerate(letters)
        }

    def token_to_bit(self, token: str) -> Optional[int]:
        """读出 token 首个符号对应的 bit。

        英文：首字母。中文：声母（由注入的 symbol_extractor 提取）。
        """
        if self._symbol_extractor is not None:
            sym = self._symbol_extractor(token)
        else:
            sym = self._extract_symbol(token)
        if sym is None:
            return None
        return self._map.get(sym)

    def _extract_symbol(self, token: str) -> Optional[str]:
        """从 token 提取映射用的符号（默认实现：首个英文字母大写）。

        中文场景由 _scan_slots 注入 adapter.extract_symbol 作为 symbol_extractor，
        覆盖此默认行为。
        """
        for ch in token:
            if ch.isalpha():
                return ch.upper()
        return None

    def letters_for_bit(self, bit: int) -> List[str]:
        """返回映射到指定 bit 的字母集合（嵌入时筛选同义候选用）。"""
        return sorted(ch for ch, b in self._map.items() if b == bit)

    @property
    def mapping(self) -> Dict[str, int]:
        """完整映射（诊断/测试用；生产环境勿打印）。"""
        return dict(self._map)

    @property
    def alphabet(self) -> List[str]:
        """本映射使用的字母表。"""
        return list(self._alphabet)
