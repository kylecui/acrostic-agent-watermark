"""v0.3 内容寻址锚点（Content-Addressed Anchors）。

v0.2 的锚点 = 绝对位置索引 → 词插入/删除使后续全部锚点偏移，水印尽毁。
v0.3 的锚点身份 = **局部上下文指纹**，与位置解耦：

    fp(w) = SHA256( stable_id(left) || stable_id(w) || stable_id(right) )

stable_id 对词典词 = 同义组 ID（替换前后不变），对词典外词 = 规范化词形
（永不被改）。因此：
- 嵌入者的同义替换不改变任何指纹（替换只发生在组内，组 ID 不变）
- 攻击者的插入/删除只改变局部 ≤3 个词位的指纹 → 只损失局部票，
  不再全局偏移——这是对编辑攻击的根本性改进

**投票桶信道**（替代 v0.2 的"固定 L 锚点 + spread 重复码"）：
每个可锚定词位独立投一个 payload bit 位（桶）：
    bucket = PRF(key, salt, fp) mod L
同一桶的多票构成天然重复码；攻击造成的错票分散在随机桶中，
由桶内多数表决吸收。L = payload 位数（16 ID + 8 CRC），无独立 ECC。

解码置信度：CRC-8 兜底 + 弱桶 chase（翻转表决边际最小的少数桶，
枚举组合重试 CRC），chase 使用会体现在结果里供上层权衡。
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .coding import build_payload, parse_payload
from .embedder import _SYNONYMS, _ZH_SYNONYMS
from .keys import KeyContext, derive_key, generate_session_salt
from .transforms import KeyedLetterMap
from .zh import EnAdapter, LanguageAdapter, get_adapter

_WORD_RE = re.compile(r"\S+|\s+")

# 指纹窗口边界标记
_START = b"<s>"
_END = b"</e>"

# v0.4 句子边界标记：句首词左邻 / 句末词右邻，使指纹跨句解耦
_BOS = b"<bos>"
_EOS = b"<eos>"

# 句子终结符：英文 [.!?] + 中文 。！？；
_SENT_END_RE = re.compile(r"[.!?。！？；]+")


def _segment_sentences(
    word_idx: List[int], tokens: List[str], adapter: Optional[LanguageAdapter] = None
) -> List[Tuple[int, int]]:
    """把 word 位置序列按句子切分，返回每句的 [start_pos, end_pos) 区间（pos 是 word_idx 的索引）。

    切分依据：token 中出现句末标点（.[.!?] / 。！？；）。句末标点落在
    某个 word token 内部时，该 word 归属当前句的末位。
    """
    if not word_idx:
        return []
    pat = adapter.sentence_end_pattern() if adapter else _SENT_END_RE
    sentences: List[Tuple[int, int]] = []
    start = 0
    for pos in range(len(word_idx)):
        wi = word_idx[pos]
        tok = tokens[wi]
        if pat.search(tok):
            sentences.append((start, pos + 1))
            start = pos + 1
    if start < len(word_idx):
        sentences.append((start, len(word_idx)))
    return sentences


def _normalize_word(token: str) -> str:
    """token → 词典查询键（英文小写、去首尾标点）。

    保留供向后兼容；v0.4 路径经 adapter.normalize 处理。
    """
    return token.lower().strip(".,!?;:\"'()[]")


@dataclass(frozen=True)
class _WordSlot:
    """文本中一个 word token 的扫描结果（嵌入/解码共用）。"""

    token_index: int          # 在 tokens 列表中的索引（替换用）
    token: str                # 原始 token
    group: Tuple[str, ...]    # 同义组（词典词）或空元组（词典外词）
    fingerprint: bytes        # 上下文指纹
    bucket: int               # 投票的 payload bit 位
    letter_map: KeyedLetterMap  # 该位的密钥字母映射
    anchorable: bool          # 组在映射下可表达双 bit（可锚定）


def _scan_slots(
    text: str,
    synonyms: Dict[str, List[str]],
    master_key: bytes,
    session_salt: bytes,
    n_buckets: int,
    sentence_aware: bool = True,
    adapter: Optional[LanguageAdapter] = None,
) -> List[_WordSlot]:
    """扫描文本，产出每个 word token 的指纹/桶/映射/可锚定性。

    这是嵌入与解码共享的"信道视图"——两侧执行完全相同的计算，
    一致性由 stable_id 的替换不变性保证。

    sentence_aware=True（v0.4 默认）：句首词左邻用 _BOS、句末词右邻用 _EOS，
    使指纹跨句解耦——重写句子 S 只损失 S 的票，不污染邻句锚点。
    sentence_aware=False：回退 v0.3 行为（跨句真实邻居），用于对比实验。

    adapter：v0.4 语言适配器（分词/规范化/符号提取/字母表）。
    None 时用 EnAdapter（封装 v0.3 英文行为，零行为变更）。
    """
    if adapter is None:
        adapter = EnAdapter()
    tokens = adapter.tokenize(text)
    word_idx = [
        i for i, t in enumerate(tokens) if adapter.is_word_token(t)
    ]

    # 第一遍：稳定 ID（自身）
    stable_ids: List[bytes] = []
    groups: List[Tuple[str, ...]] = []
    for wi in word_idx:
        key = adapter.normalize(tokens[wi])
        group = synonyms.get(key)
        if group is None:
            stable_ids.append(adapter.stable_id_for_raw(tokens[wi]))
            groups.append(())
        else:
            g = tuple(group)
            gid = hashlib.sha256("|".join(sorted(g)).encode("utf-8")).digest()[:10]
            stable_ids.append(b"grp:" + gid)
            groups.append(g)

    # v0.4 句子切分：决定每个 pos 的 left/right 是否用句边界标记
    sent_bounds: Dict[int, str] = {}  # pos → 'bos' | 'eos' | 'both'
    if sentence_aware and len(word_idx) > 0:
        for s_start, s_end in _segment_sentences(word_idx, tokens, adapter):
            if s_start == s_end:
                continue
            # 句首词的 left → _BOS
            cur = sent_bounds.get(s_start, "")
            sent_bounds[s_start] = ("both" if cur == "eos" else "bos") if cur else "bos"
            # 句末词的 right → _EOS
            last = s_end - 1
            cur = sent_bounds.get(last, "")
            sent_bounds[last] = ("both" if cur == "bos" else "eos") if cur else "eos"

    # 锚点种子（一次性派生，指纹级 HMAC 用）
    anchor_seed = derive_key(
        master_key,
        KeyContext(session_salt=session_salt, info=b"aawm:ca:anchor"),
    )

    alphabet = adapter.letter_alphabet()
    sym_extract = adapter.extract_symbol
    slots: List[_WordSlot] = []
    for pos, wi in enumerate(word_idx):
        bound = sent_bounds.get(pos, "")
        if sentence_aware and bound:
            left = _BOS if "bos" in bound else (
                stable_ids[pos - 1] if pos > 0 else _START
            )
            right = _EOS if "eos" in bound else (
                stable_ids[pos + 1] if pos + 1 < len(word_idx) else _END
            )
        else:
            left = stable_ids[pos - 1] if pos > 0 else _START
            right = stable_ids[pos + 1] if pos + 1 < len(word_idx) else _END
        fp = hashlib.sha256(
            left + b"\x00" + stable_ids[pos] + b"\x00" + right
        ).digest()

        group = groups[pos]
        if not group:
            continue  # 词典外词：既不可读也不可写 bit，直接跳过

        # 桶分配：指纹级 PRF
        h = hmac.new(anchor_seed, fp, hashlib.sha256).digest()
        bucket = int.from_bytes(h[:6], "big") % n_buckets

        # 该位的字母映射（指纹派生 → 替换前后不变）
        map_seed = derive_key(
            master_key,
            KeyContext(session_salt=session_salt, tag=fp, info=b"aawm:ca:map"),
        )
        letter_map = KeyedLetterMap(
            map_seed, alphabet=alphabet, symbol_extractor=sym_extract
        )

        # 可锚定：组在映射下能表达双 bit
        bits = {letter_map.token_to_bit(w) for w in group}
        anchorable = bits == {0, 1}

        slots.append(
            _WordSlot(
                token_index=wi,
                token=tokens[wi],
                group=group,
                fingerprint=fp,
                bucket=bucket,
                letter_map=letter_map,
                anchorable=anchorable,
            )
        )
    return slots


# ---------------------------------------------------------------------------
# 配置与结果
# ---------------------------------------------------------------------------


@dataclass
class CAConfig:
    """内容寻址水印配置。

    Attributes:
        user_id_bits: 用户 ID 位宽（默认 16）
        synonyms: 自定义同义词典（默认用内置稳定词典）
        min_anchorable: 最少可锚定位数（不足则拒绝嵌入）
        chase_max_buckets: CRC 失败时最多翻转的弱桶数（2^m 次 CRC 试验）
        sentence_aware: v0.4 句子边界感知指纹开关（默认 True）。
            True 时句首词左邻用 _BOS、句末词右邻用 _EOS，使重写单句只损失
            该句的票不污染邻句锚点；False 回退 v0.3 跨句真实邻居行为。
            注意：fp 值会因此开关而变，v0.3 嵌入文本需用 False 解码。
        language: v0.4 语言选择（"en" 英文 / "zh" 中文）。
            决定分词方式、符号提取（首字母 / 声母）、字母表（26 / 23）。
            "zh" 用 ZhAdapter + 中文稳定词典，零强依赖。
    """
    user_id_bits: int = 16
    synonyms: Optional[Dict[str, List[str]]] = None
    min_anchorable: int = 36
    chase_max_buckets: int = 3
    sentence_aware: bool = True
    language: str = "en"


@dataclass
class CAEmbedResult:
    """嵌入结果。

    Attributes:
        watermarked_text: 水印后文本
        session_salt: 会话盐（解码者需要，可公开）
        user_id: 嵌入的用户 ID
        n_slots: 扫描到的词典词位总数
        n_anchorable: 可锚定位数（实际参与投票）
        n_natural: 天然携带目标 bit（零失真）
        n_replaced: 实际替换数
        n_skipped: 可锚定但无候选满足（应恒为 0）
        votes_histogram: 每桶票数分布（诊断用）
    """
    watermarked_text: str
    session_salt: bytes
    user_id: int
    n_slots: int
    n_anchorable: int
    n_natural: int
    n_replaced: int
    n_skipped: int
    votes_histogram: List[int]


@dataclass
class CADecodeResult:
    """解码结果。

    Attributes:
        success: 是否成功还原用户 ID
        user_id: 还原的用户 ID（失败为 None）
        crc_ok: CRC-8 是否通过
        n_votes: 总票数
        min_bucket_votes: 最少票桶的票数
        weak_buckets: 表决边际 ≤ weak_margin 的桶数
        chase_used: 是否动用了弱桶翻转重试（置信度折减信号）
        reason: 失败原因
    """
    success: bool
    user_id: Optional[int]
    crc_ok: bool
    n_votes: int
    min_bucket_votes: int
    weak_buckets: int
    chase_used: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# 嵌入器 / 解码器
# ---------------------------------------------------------------------------


class CAEmbedder:
    """内容寻址水印嵌入器（v0.3）。

    用法：
        emb = CAEmbedder(master_key)
        r = emb.embed(text, user_id=42)
        # 发布 r.watermarked_text；r.session_salt 存档
    """

    def __init__(self, master_key: bytes, config: CAConfig = CAConfig()) -> None:
        if len(master_key) < 16:
            raise ValueError("master_key too short (>= 16 bytes)")
        self.master_key = master_key
        self.config = config
        self.adapter = get_adapter(config.language)
        if config.synonyms is not None:
            self.synonyms = config.synonyms
        elif config.language == "zh":
            self.synonyms = _ZH_SYNONYMS
        else:
            self.synonyms = _SYNONYMS

    def embed(
        self,
        text: str,
        user_id: int,
        session_salt: Optional[bytes] = None,
    ) -> CAEmbedResult:
        if user_id < 0 or user_id >= (1 << self.config.user_id_bits):
            raise ValueError(
                f"user_id must be in [0, {1 << self.config.user_id_bits})"
            )
        if session_salt is None:
            session_salt = generate_session_salt()

        payload = build_payload(user_id, self.config.user_id_bits)
        n_buckets = len(payload)

        slots = _scan_slots(
            text, self.synonyms, self.master_key, session_salt, n_buckets,
            sentence_aware=self.config.sentence_aware,
            adapter=self.adapter,
        )
        anchorable = [s for s in slots if s.anchorable]
        if len(anchorable) < self.config.min_anchorable:
            raise ValueError(
                f"容量不足：需要 >= {self.config.min_anchorable} 个可锚定位"
                f"（同义组在该位映射下可表达双 bit），实际 {len(anchorable)}。"
                f"请加长文本或扩充同义词典"
            )

        tokens = self.adapter.tokenize(text)
        n_natural = n_replaced = n_skipped = 0
        votes = [0] * n_buckets

        for s in anchorable:
            target = payload[s.bucket]
            votes[s.bucket] += 1

            if s.letter_map.token_to_bit(s.token) == target:
                n_natural += 1
                continue

            chosen = self._pick_candidate(s, target)
            if chosen is None:
                # 理论不可达：anchorable 保证组内有目标 bit 的词。
                # 若触发说明组内该词与 token 大小写形态冲突等边缘情况。
                n_skipped += 1
                continue

            tokens[s.token_index] = chosen
            n_replaced += 1

        return CAEmbedResult(
            watermarked_text="".join(tokens),
            session_salt=session_salt,
            user_id=user_id,
            n_slots=len(slots),
            n_anchorable=len(anchorable),
            n_natural=n_natural,
            n_replaced=n_replaced,
            n_skipped=n_skipped,
            votes_histogram=votes,
        )

    def _pick_candidate(self, slot: _WordSlot, target: int) -> Optional[str]:
        """在组内找符号映射为 target 的候选，保持原词风格。

        英文：保持首字母大小写、尾标点。中文：双字词本身即候选，无需变形。
        """
        norm = self.adapter.normalize(slot.token)
        best: Optional[str] = None
        for w in slot.group:
            if w == norm:
                continue
            if slot.letter_map.token_to_bit(w) != target:
                continue
            if self.config.language == "en":
                # 英文：保持大小写 + 尾标点
                capital = slot.token[:1].isupper()
                cand = (w[:1].upper() + w[1:]) if capital else w
                tail = slot.token[len(slot.token.rstrip(".,!?;:\"'()[]")):]
                if tail and not cand.endswith(tail):
                    cand = cand + tail
            else:
                # 中文：双字词直接用
                cand = w
            if best is None:
                best = cand
        return best


class CADecoder:
    """内容寻址水印解码器（v0.3）。

    用法：
        dec = CADecoder(master_key)
        r = dec.decode(suspect_text, session_salt)
        if r.success:
            print(f"水印属于用户 {r.user_id}")
    """

    def __init__(self, master_key: bytes, config: CAConfig = CAConfig()) -> None:
        if len(master_key) < 16:
            raise ValueError("master_key too short (>= 16 bytes)")
        self.master_key = master_key
        self.config = config
        self.adapter = get_adapter(config.language)
        if config.synonyms is not None:
            self.synonyms = config.synonyms
        elif config.language == "zh":
            self.synonyms = _ZH_SYNONYMS
        else:
            self.synonyms = _SYNONYMS

    def decode(
        self,
        suspect_text: str,
        session_salt: bytes,
    ) -> CADecodeResult:
        synonyms = self.synonyms
        n_buckets = self.config.user_id_bits + 8

        slots = _scan_slots(
            suspect_text, synonyms, self.master_key, session_salt, n_buckets,
            sentence_aware=self.config.sentence_aware,
            adapter=self.adapter,
        )

        # 收票
        ones = [0] * n_buckets
        total = [0] * n_buckets
        for s in slots:
            if not s.anchorable:
                continue
            bit = s.letter_map.token_to_bit(s.token)
            if bit is None:
                continue
            total[s.bucket] += 1
            ones[s.bucket] += bit

        n_votes = sum(total)
        margins = [
            abs(ones[j] - (total[j] - ones[j])) for j in range(n_buckets)
        ]
        min_bucket = min(total) if total else 0
        weak = [j for j in range(n_buckets) if margins[j] <= 1]

        # 第一轮：多数表决 + CRC
        guess = [1 if ones[j] * 2 > total[j] else 0 for j in range(n_buckets)]
        uid, crc_ok = parse_payload(guess, self.config.user_id_bits)
        if crc_ok:
            return CADecodeResult(
                success=True, user_id=uid, crc_ok=True,
                n_votes=n_votes, min_bucket_votes=min_bucket,
                weak_buckets=len(weak), chase_used=False,
            )

        # 第二轮：弱桶 chase —— 翻转表决边际最小的 m 桶，枚举组合重试 CRC
        m = min(self.config.chase_max_buckets, len(weak))
        order = sorted(range(n_buckets), key=lambda j: margins[j])[:m]
        if order:
            from itertools import combinations
            for r in range(1, len(order) + 1):
                for combo in combinations(order, r):
                    trial = list(guess)
                    for j in combo:
                        trial[j] ^= 1
                    uid2, ok2 = parse_payload(trial, self.config.user_id_bits)
                    if ok2:
                        return CADecodeResult(
                            success=True, user_id=uid2, crc_ok=True,
                            n_votes=n_votes, min_bucket_votes=min_bucket,
                            weak_buckets=len(weak), chase_used=True,
                        )

        return CADecodeResult(
            success=False, user_id=None, crc_ok=False,
            n_votes=n_votes, min_bucket_votes=min_bucket,
            weak_buckets=len(weak), chase_used=False,
            reason="CRC 校验失败：无水印、密钥错误或编辑破坏超出纠错能力",
        )
