"""信道 B：密钥派生绿名单 × 16 频带统计（v0.5）。

溯源信道：鲁棒而松。机制见 docs/design.md §13.2：
1. 词典预处理三必修课（§13.4 踩坑记录的产物）：
   a. 不相交划分 —— 同义词组重叠率 23%，按"先到先得"给每个词唯一组归属
   b. 可翻转组过滤 —— 组内全同色（单色组）无法编码且污染 null，剔除
   c. 逐带 p0 标定 —— p0 ≠ 0.5（实测带间 0.411~0.535），
      跳过这步 z 检验有系统性偏移
2. 密钥派生：green(w) = HMAC(K_green, w)[0] >> 7
              band(g) = HMAC(K_band, g)[0] % N
3. UID 16 bit ↔ 16 频带方向：bits[b] = (uid >> b) & 1
4. 检测：逐带 z 检验（逐带 p0），uid = Σ (z_b > 0) << b，
   存在性得分 = Σ|z_b|；点积形式 ⟨v, τ_uid⟩ = Σ z_b·(2·bit_b − 1)

中文路线（§13.5）：待办。本模块的 tokenizer/词典均可注入，
语言相关逻辑隔离在构造参数，统计管线 100% 共享（§13.8 结论）。
"""
from __future__ import annotations

import hashlib
import hmac
import random
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .keys import KeyContext, derive_key
from .synonym_data import EN_SYNONYMS_EXTRA, EN_SYNONYMS_RAW

DEFAULT_N_BANDS = 16
_UID_BITS = 16  # v0.5 固定 16-bit UID（与 N_BANDS 对齐）

_EN_TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _split_keep_seps(text: str) -> List[str]:
    """把文本切成 [词 | 非词] 交替片段，保留原始分隔符以无损重组。"""
    return re.split(r"([A-Za-z]+(?:'[A-Za-z]+)?)", text)


class GreenlistCodec:
    """信道 B 编解码器。

    用法::

        codec = GreenlistCodec(master_key, session_salt)
        marked = codec.embed(text, uid=0x1234, bias=1.0)
        report = codec.detect(marked)
        report.uid  # -> 0x1234
    """

    def __init__(
        self,
        master_key: bytes,
        session_salt: bytes,
        *,
        n_bands: int = DEFAULT_N_BANDS,
        dictionary: Optional[Dict[str, List[str]]] = None,
        language_tag: bytes = b"en",
    ) -> None:
        if n_bands < 1:
            raise ValueError("n_bands must be >= 1")
        self.n_bands = n_bands
        self.language_tag = language_tag

        # 密钥派生：绿/带各自独立子密钥，语言标签隔离中英文命名空间
        self._k_green = derive_key(
            master_key,
            KeyContext(session_salt=session_salt, info=b"greenlist:green", tag=language_tag),
        )
        self._k_band = derive_key(
            master_key,
            KeyContext(session_salt=session_salt, info=b"greenlist:band", tag=language_tag),
        )

        if dictionary is None:
            dictionary = {**EN_SYNONYMS_RAW, **EN_SYNONYMS_EXTRA}

        # --- 必修课 1：不相交划分（先到先得）---
        word_owner: Dict[str, str] = {}
        for head, candidates in dictionary.items():
            for w in candidates:
                word_owner.setdefault(w, head)
        disjoint: Dict[str, List[str]] = {}
        for w, head in word_owner.items():
            disjoint.setdefault(head, []).append(w)

        # --- 必修课 2：可翻转组过滤（剔除单色组）---
        self._groups: Dict[str, List[str]] = {
            head: sorted(members)
            for head, members in disjoint.items()
            if len(set(self.green(w) for w in members)) > 1
        }

        # 词级索引：w -> (band, group_members)
        self._w2band: Dict[str, int] = {}
        self._w2group: Dict[str, List[str]] = {}
        for head, members in self._groups.items():
            b = self.band_of_group(head)
            for w in members:
                self._w2band[w] = b
                self._w2group[w] = members

        # --- 必修课 3：逐带 p0（默认 0.5，需 calibrate_p0 用真实语料标定）---
        self._p0: Dict[int, float] = {}

    # ------------------------------------------------------------------
    # 密钥化谓词
    # ------------------------------------------------------------------
    def green(self, word: str) -> int:
        """词颜色：HMAC(K_green, word) 首位。"""
        mac = hmac.new(self._k_green, word.encode("utf-8"), hashlib.sha256)
        return (mac.digest()[0] >> 7) & 1

    def band_of_group(self, group_head: str) -> int:
        """组到频带的密钥派生映射。"""
        mac = hmac.new(
            self._k_band, group_head.encode("utf-8"), hashlib.sha256
        )
        return mac.digest()[0] % self.n_bands

    # ------------------------------------------------------------------
    # 词典结构自检（测试用）
    # ------------------------------------------------------------------
    @property
    def stats(self) -> Dict[str, int]:
        n_words = sum(len(m) for m in self._groups.values())
        return {
            "n_groups": len(self._groups),
            "n_words": n_words,
            "n_bands": self.n_bands,
            "min_group_size": min(len(m) for m in self._groups.values()),
        }

    # ------------------------------------------------------------------
    # p0 标定（必修课 3）
    # ------------------------------------------------------------------
    def calibrate_p0(self, corpus: Sequence[str], *, pseudocount: float = 1.0) -> None:
        """在无水印参考语料上估计逐带绿率 p0(b)。

        对每个频带 b：统计语料中落在带 b 的词典词数 n_b 与绿词数 g_b，
        p0(b) = (g_b + pseudocount) / (n_b + 2·pseudocount)
        （拉普拉斯平滑，避免小样本 0/1 极端值）。
        """
        counts: Dict[int, Tuple[int, int]] = {b: (0, 0) for b in range(self.n_bands)}
        for text in corpus:
            for tok in _EN_TOKEN_RE.findall(text):
                tok = tok.lower()
                b = self._w2band.get(tok)
                if b is not None:
                    n, g = counts[b]
                    counts[b] = (n + 1, g + self.green(tok))
        for b, (n, g) in counts.items():
            if n > 0:
                self._p0[b] = (g + pseudocount) / (n + 2.0 * pseudocount)

    def _p0_of(self, band: int) -> float:
        return self._p0.get(band, 0.5)

    # ------------------------------------------------------------------
    # 嵌入（post-hoc 同义替换）
    # ------------------------------------------------------------------
    def embed(
        self,
        text: str,
        uid: int,
        *,
        bias: float = 1.0,
        rng: Optional[random.Random] = None,
    ) -> str:
        """把 uid 写入文本：词典词以概率 bias 替换为组内颜色匹配候选。

        bias=1.0 时所有词典词都参与编码（实测 600 词 100% 往返）；
        bias<1.0 引入随机跳过，换取更低的分布偏移。
        """
        if not (0.0 <= bias <= 1.0):
            raise ValueError("bias must be in [0, 1]")
        if rng is None:
            rng = random.Random()
        if uid < 0 or uid >= (1 << self.n_bands):
            raise ValueError(f"uid must fit in {self.n_bands} bits")
        bits = [(uid >> b) & 1 for b in range(self.n_bands)]

        parts = _split_keep_seps(text)
        out: List[str] = []
        for part in parts:
            low = part.lower()
            b = self._w2band.get(low)
            if b is None or rng.random() >= bias:
                out.append(part)
                continue
            want = bool(bits[b])
            pool = [x for x in self._w2group[low] if bool(self.green(x)) == want]
            if not pool:
                out.append(part)  # 理论上已过滤，兜底
            else:
                choice = rng.choice(pool)
                # 保留原文的大小写风格（首字母大写 -> 首字母大写）
                if part[:1].isupper():
                    choice = choice.capitalize()
                out.append(choice)
        return "".join(out)

    # ------------------------------------------------------------------
    # 检测（逐带 z 检验 + UID 解码 + 存在性得分）
    # ------------------------------------------------------------------
    def detect(self, text: str) -> "BandReport":
        n_per_band = [0] * self.n_bands
        g_per_band = [0] * self.n_bands
        for tok in _EN_TOKEN_RE.findall(text):
            tok = tok.lower()
            b = self._w2band.get(tok)
            if b is not None:
                n_per_band[b] += 1
                g_per_band[b] += self.green(tok)

        z_per_band: List[float] = [0.0] * self.n_bands
        uid = 0
        existence = 0.0
        band_stats: List[BandStat] = []
        for b in range(self.n_bands):
            n, g = n_per_band[b], g_per_band[b]
            if n < 2:
                band_stats.append(BandStat(band=b, n=n, green=g, p0=self._p0_of(b), z=0.0, has_signal=False))
                continue
            p0 = self._p0_of(b)
            var = p0 * (1.0 - p0) * n
            z = (g - p0 * n) / (var ** 0.5) if var > 0 else 0.0
            z_per_band[b] = z
            existence += abs(z)
            if z > 0:
                uid |= 1 << b
            band_stats.append(BandStat(band=b, n=n, green=g, p0=p0, z=z, has_signal=True))

        return BandReport(
            uid=uid,
            existence_score=existence,
            bands=band_stats,
            n_dict_words=sum(n_per_band),
        )

    def dot_score(self, text: str, uid: int) -> float:
        """⟨v(text), τ_uid⟩：v = 逐带标准化绿计向量，τ = ±1 方向向量。

        统一内积框架（SOTA 调研中 Collab Threshold Watermarking 的形式）。
        对嵌入过 uid 的文本取正值，且值随证据量增大。
        """
        report = self.detect(text)
        s = 0.0
        for st in report.bands:
            if st.has_signal:
                s += st.z * (1 if ((uid >> st.band) & 1) else -1)
        return s

    def uid_hamming(self, text: str, uid: int) -> int:
        """检测 UID 与给定 UID 的汉明距。"""
        return bin(self.detect(text).uid ^ uid).count("1")


@dataclass(frozen=True)
class BandStat:
    """单频带统计。"""

    band: int
    n: int
    green: int
    p0: float
    z: float
    has_signal: bool


@dataclass(frozen=True)
class BandReport:
    """信道 B 检测报告。

    Attributes:
        uid: 解码出的 16-bit UID（n<2 的带贡献 0，即 bit=0）
        existence_score: Σ|z_b| 存在性得分（对比无水印 null 分布，
            部署上推荐 UID 注册库匹配而非盲检，见 §13.3）
        bands: 逐带明细
        n_dict_words: 文本命中的词典词总数
    """

    uid: int
    existence_score: float
    bands: List[BandStat] = field(default_factory=list)
    n_dict_words: int = 0
