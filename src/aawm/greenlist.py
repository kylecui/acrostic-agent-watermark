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

中文路线（§13.5）：本模块的 tokenizer/词典均可注入，
语言相关逻辑隔离在 (language_tag, dictionary, tokenizer) 三元组，
统计管线 100% 共享（§13.8 结论的代码化）。
"""
from __future__ import annotations

import hashlib
import hmac
import random
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .keys import KeyContext, derive_key
from .synonym_data import EN_SYNONYMS_EXTRA, EN_SYNONYMS_RAW

DEFAULT_N_BANDS = 16
_UID_BITS = 16  # v0.5 固定 16-bit UID（与 N_BANDS 对齐）

_EN_TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _split_keep_seps(text: str) -> List[str]:
    """把文本切成 [词 | 非词] 交替片段，保留原始分隔符以无损重组。"""
    return re.split(r"([A-Za-z]+(?:'[A-Za-z]+)?)", text)


# ---------------------------------------------------------------------------
# 语言接缝（§13.8）：tokenizer 协议
#   tokenize(text) -> List[(raw片段, norm词 or None)]
#   join(raw) 必须无损还原原文；norm 是词典查询键（英文小写、中文原样）
# 统计管线（embed/detect/calibrate）对语言一无所知。
# ---------------------------------------------------------------------------
Tokenizer = Callable[[str], List[Tuple[str, Optional[str]]]]


def en_tokenizer(text: str) -> List[Tuple[str, Optional[str]]]:
    """英文默认分词：正则切词保留分隔符，词片段给出小写 norm。"""
    return [
        (p, p.lower()) if p and _EN_TOKEN_RE.fullmatch(p) else (p, None)
        for p in _split_keep_seps(text)
    ]


def make_zh_tokenizer(dict_words: Optional[set] = None) -> Tokenizer:
    """中文分词：前向最大匹配（双字词优先），复用 ZhAdapter。

    dict_words=None 时词典 = ZH_SYNONYMS_RAW 全部词条；传入自定义词典时
    必须覆盖全部候选词（替换后的词也要能被重新切出，否则 token 数变化、
    detect 命中崩塌 —— exp_real_corpus 实测教训）。命中词典的双字 token
    给出 norm=自身，其余（单字、标点、ASCII 连续段）norm=None 原样保留。
    """
    from .zh import ZhAdapter

    adapter = ZhAdapter(dict_words=dict_words)
    words = adapter._dict_words

    def _tokenize(text: str) -> List[Tuple[str, Optional[str]]]:
        return [
            (tok, tok if tok in words else None)
            for tok in adapter.tokenize(text)
        ]

    return _tokenize


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
        tokenizer: Optional[Tokenizer] = None,
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

        # --- 语言接缝装配（§13.8）：语言差异 = (词典, 分词器) 二元组 ---
        if language_tag == b"zh":
            if dictionary is None:
                from .synonym_data import ZH_SYNONYMS_RAW

                dictionary = ZH_SYNONYMS_RAW
            if tokenizer is None:
                # 分词词典必须与嵌入词典同步（含全部候选词），
                # 否则替换后的新词切不出来、detect 命中崩塌
                all_words = {w for ws in dictionary.values() for w in ws}
                tokenizer = make_zh_tokenizer(dict_words=all_words)
        else:
            if dictionary is None:
                dictionary = {**EN_SYNONYMS_RAW, **EN_SYNONYMS_EXTRA}
            if tokenizer is None:
                tokenizer = en_tokenizer
        self._tokenizer = tokenizer

        # 全部词条集合（含被过滤组的词），供边界稳定性检查用
        self._all_words: set = set()
        for members in dictionary.values():
            self._all_words.update(members)

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
            "min_group_size": min((len(m) for m in self._groups.values()), default=0),
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
            for _raw, norm in self._tokenizer(text):
                if norm is None:
                    continue
                b = self._w2band.get(norm)
                if b is not None:
                    n, g = counts[b]
                    counts[b] = (n + 1, g + self.green(norm))
        for b, (n, g) in counts.items():
            if n > 0:
                self._p0[b] = (g + pseudocount) / (n + 2.0 * pseudocount)

    def _p0_of(self, band: int) -> float:
        return self._p0.get(band, 0.5)

    # ------------------------------------------------------------------
    # 嵌入（post-hoc 同义替换）
    # ------------------------------------------------------------------
    def _boundary_safe(self, prev_char: str, choice: str, next_char: str) -> bool:
        """边界稳定性检查（中文连续书写的特有坑，英文天然安全）。

        双字词替换可能引发下游分词边界漂移：如 "项|指标" 替换 "指标"->"目的"
        后，重新分词把 "项目" 切成词典词，漂移产物落错频带带错颜色，
        污染逐带统计（实测 53 个漂移词可把 z 打成 -1.69）。

        预防：替换词与左右邻字符的拼接不得构成词典词。
        英文词有天然分隔符，邻字符为空格/标点，拼接恒非词典词，自动通过。
        """
        if prev_char and (prev_char + choice[0]) in self._all_words:
            return False
        if next_char and (choice[-1] + next_char) in self._all_words:
            return False
        return True

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

        parts = self._tokenizer(text)
        out: List[str] = []
        last_char = ""  # 已输出文本的最后一个字符（边界检查用）
        for i, (raw, norm) in enumerate(parts):
            b = self._w2band.get(norm) if norm is not None else None
            next_char = parts[i + 1][0][:1] if i + 1 < len(parts) else ""
            if b is None or rng.random() >= bias:
                out.append(raw)
                if raw:
                    last_char = raw[-1]
                continue
            want = bool(bits[b])
            pool = [x for x in self._w2group[norm] if bool(self.green(x)) == want]
            choice = None
            for cand in rng.sample(pool, len(pool)):  # 随机序 + 边界稳定
                if self._boundary_safe(last_char, cand, next_char):
                    choice = cand
                    break
            if choice is None:
                out.append(raw)  # 全部候选边界不稳：保守跳过
                if raw:
                    last_char = raw[-1]
                continue
            # 保留原文的大小写风格（首字母大写 -> 首字母大写；中文恒 False 无影响）
            if raw[:1].isupper():
                choice = choice.capitalize()
            out.append(choice)
            last_char = choice[-1]
        return "".join(out)

    # ------------------------------------------------------------------
    # 检测（逐带 z 检验 + UID 解码 + 存在性得分）
    # ------------------------------------------------------------------
    def detect(self, text: str, *, min_n: int = 2) -> "BandReport":
        """检测水印并解码 UID。

        Args:
            text: 嫌疑文本
            min_n: 参与检测的最小带内词数（默认 2，过滤单词噪声带）。
                设为 1 时弱证据带（n=1）也参与：其 z 符号正确率实测
                79%~100%，净贡献为正，适合注册库软判决匹配
                （见 soft_match）。

        Returns:
            BandReport
        """
        n_per_band = [0] * self.n_bands
        g_per_band = [0] * self.n_bands
        for _raw, norm in self._tokenizer(text):
            if norm is None:
                continue
            b = self._w2band.get(norm)
            if b is not None:
                n_per_band[b] += 1
                g_per_band[b] += self.green(norm)

        z_per_band: List[float] = [0.0] * self.n_bands
        uid = 0
        existence = 0.0
        band_stats: List[BandStat] = []
        for b in range(self.n_bands):
            n, g = n_per_band[b], g_per_band[b]
            if n < min_n:
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

    def masked_hamming(self, text: str, uid: int) -> Tuple[int, int]:
        """带掩码汉明距：零覆盖带（n<2）不参与比对。

        返回 (汉明距, 参与比对的带数)。真实语料每带样本稀疏时，
        零覆盖带默认解 bit=0 会造成系统性翻位（exp_real_corpus 实测：
        600 词自然文本词典词 38-99，每带 2-6 词，偶发整带零覆盖），
        注册库匹配应只比较有信号带。
        """
        rep = self.detect(text)
        dist = 0
        n_active = 0
        for st in rep.bands:
            if st.has_signal:
                n_active += 1
                if ((rep.uid >> st.band) & 1) != ((uid >> st.band) & 1):
                    dist += 1
        return dist, n_active

    def soft_match(
        self,
        text: str,
        candidates: Sequence[int],
        *,
        min_n: int = 1,
        margin: float = 0.0,
    ) -> Tuple[Optional[int], float, float]:
        """软判决注册库匹配（鲁棒性增强）。

        对候选 UID 逐带 z 打点积分 s(c) = Σ_b z_b·(2·bit_b(c) − 1)，
        返回得分最高的候选。零覆盖带（n < min_n）不参与。

        与 masked_hamming 的硬判决不同，soft_match 直接利用逐带 z 的
        幅度信息。弱证据带（n=1）的 z 符号虽有噪声（攻击下实测正确率
        79%~100%），但净贡献为正——30% 同组改写攻击下 min_n=1 比 2
        的匹配率提升 20→27/30（exp_soft_match 实测）。

        Args:
            text: 嫌疑文本
            candidates: 候选 UID 列表（如注册库全部 UID）
            min_n: 参与检测的最小带内词数（默认 1，利用弱证据带）
            margin: 置信阈值。最优与次优得分差 < margin 时 abstain
                （返回 best_uid=None）。实测 margin=2.0 可把错误匹配
                全部转为 abstain（precision→100%），代价是略降召回。

        Returns:
            (best_uid, best_score, gap)：
                best_uid: 得分最高的候选；gap < margin 时为 None（不可靠）
                best_score: 最优候选的 soft 得分
                gap: 最优与次优候选的得分差
        """
        cands = list(dict.fromkeys(candidates))  # 去重保序
        if not cands:
            return None, 0.0, 0.0
        rep = self.detect(text, min_n=min_n)
        z_by_band = {st.band: st.z for st in rep.bands if st.has_signal}

        def _score(c: int) -> float:
            s = 0.0
            for b, z in z_by_band.items():
                s += z * (1 if ((c >> b) & 1) else -1)
            return s

        scored = sorted(((_score(c), c) for c in cands), key=lambda x: x[0], reverse=True)
        best_score, best_uid = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else best_score - margin - 1.0
        gap = best_score - second_score
        if gap < margin:
            return None, best_score, gap
        return best_uid, best_score, gap


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
