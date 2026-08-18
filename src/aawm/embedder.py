"""嵌入器：把携带用户 ID 的水印嵌入文本（decode 模式，v0.2 主线）。

流程：
    user_id --CRC-8--> payload(24 bits) --ECC--> codeword(24*r bits)
    codeword 的第 i 位 → 第 i 个锚点：用该位置的 KeyedLetterMap
    选择首字母映射为目标 bit 的同义词。

嵌入时的三种锚点结局：
    natural  — 原词映射恰好等于目标 bit，无需改动（零失真）
    replaced — 找到映射为目标 bit 的同义词，替换
    skipped  — 无可用候选，保留原词 → 该位成为信道错误，由纠错码吸收

注意：同义替换不改变 token 数量，锚点位置在嵌入/解码间保持一致。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .anchor import AnchorConfig, select_anchors
from .coding import build_payload, get_code
from .keys import KeyContext, derive_key, generate_session_salt
from .synonym_data import EN_SYNONYMS_EXTRA, EN_SYNONYMS_RAW, ZH_SYNONYMS_RAW
from .transforms import KeyedLetterMap


# 原始词典（v0.2 词典 + v0.4 扩充，稳定化前的并集）
_SYNONYMS_RAW: Dict[str, List[str]] = {**EN_SYNONYMS_RAW, **EN_SYNONYMS_EXTRA}


def _build_stable_synonyms(
    groups: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """把"主词条 → 候选"词典改造成锚点稳定的词典。

    稳定性条件（锚点可重建的关键）：
        锚点池 = 词典内词的位置。替换只发生在词典词之间
        （候选必为词典词），所以"在词典中"属性嵌入前后不变。

    做法：
        1. 跨组共享词只归属首个组（从后续组的候选中剔除），
           保证每个词的候选组唯一
        2. 为组内每个词生成词条，候选 = 整组

    语义注：候选列表按设计应跨 A-M / N-Z 两个首字母半区，
    否则该组只能表达单一 bit（天然命中时零开销，否则 skip）。
    """
    assigned: Dict[str, int] = {}
    group_words: List[List[str]] = []

    for gi, (key, cands) in enumerate(groups.items()):
        words = list(dict.fromkeys([key] + [c for c in cands if c]))
        kept = []
        for w in words:
            if w not in assigned:
                assigned[w] = gi
            if assigned[w] == gi:
                kept.append(w)
        group_words.append(kept)

    out: Dict[str, List[str]] = {}
    for words in group_words:
        if len(words) < 2:
            continue  # 单词组无替换能力
        for w in words:
            out[w] = list(words)
    return out


# 单半区组的人工补充词（组代表词 → 跨半区补充词）。
# 稳定化后若某组首字母只落在一个半区，该组无法表达另一半 bit，
# 是嵌入 skip 的主要来源；此处注入语义相近的跨半区词修补。
_GROUP_PATCHES: Dict[str, str] = {
    "add": "toss",
    "assessment": "rating",
    "bug": "snag",
    "affordable": "reasonable",
    "cold": "polar",
    "assemble": "shape",
    "details": "particulars",
    "base": "vicinity",
    "establish": "spawn",
    "faint": "soft",
    "gadget": "rig",
    "concern": "worry",
    "peruse": "view",
    "seal": "lock",
    "speed": "haste",
    "unlock": "disclose",
}


def _is_both_halves(words: List[str]) -> bool:
    halves = set()
    for w in words:
        ch = w[:1].upper()
        if ch.isalpha():
            halves.add(0 if "A" <= ch <= "M" else 1)
    return len(halves) == 2


def _apply_group_patches(syn: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """给单半区稳定组注入跨半区补充词（幂等，构建期执行一次）。"""
    uniq: Dict[tuple, List[str]] = {}
    for cands in syn.values():
        uniq.setdefault(tuple(cands), list(cands))

    used = set(syn.keys())
    patched: List[List[str]] = []
    for cands in uniq.values():
        if _is_both_halves(cands):
            patched.append(cands)
            continue
        words = set(cands)
        merged = list(cands)
        for rep, extra in _GROUP_PATCHES.items():
            if rep in words and extra not in used:
                merged = merged + [extra]
                used.add(extra)
                break
        patched.append(merged)

    out: Dict[str, List[str]] = {}
    for cands in patched:
        for w in cands:
            out[w] = list(cands)
    return out


# 稳定化 + 修补后的词典（模块加载时构建一次）
_SYNONYMS = _apply_group_patches(_build_stable_synonyms(_SYNONYMS_RAW))

# 中文稳定化词典（v0.4）：仅做跨组共享词归首组，不做半区修补
# （中文用声母谓词，半区概念不适用；动态可锚定率由 KeyedLetterMap 判定）
_ZH_SYNONYMS = _build_stable_synonyms(ZH_SYNONYMS_RAW)


def diagnose_synonym_groups(synonyms: Dict[str, List[str]]) -> Dict[str, int]:
    """诊断词典质量：统计候选组首字母半区覆盖情况（测试/调优用）。"""
    groups = {tuple(v) for v in synonyms.values()}
    both = first_half_only = second_half_only = 0
    for g in groups:
        bits = {0 if ch.upper() <= "M" else 1 for ch in g if ch[:1].isalpha()}
        if bits == {0, 1}:
            both += 1
        elif bits == {0}:
            first_half_only += 1
        else:
            second_half_only += 1
    return {
        "groups": len(groups),
        "covers_both_halves": both,
        "single_half_only": first_half_only + second_half_only,
    }


@dataclass
class EmbedConfig:
    """嵌入配置（decode 模式）。

    Attributes:
        user_id_bits: 用户 ID 位宽（默认 16，即 65536 个用户）
        code_name: 信道编码名（spread3 默认 / spread5 / repeat3 / hamming74）
        synonyms: 自定义同义词典（覆盖默认）
        anchor_config: 锚点配置（嵌入时会被 codeword 长度动态覆盖）
    """
    user_id_bits: int = 16
    code_name: str = "spread3"
    synonyms: Optional[Dict[str, List[str]]] = None
    anchor_config: AnchorConfig = field(default_factory=AnchorConfig)


@dataclass
class EmbedResult:
    """嵌入结果。

    Attributes:
        watermarked_text: 水印后文本
        session_salt: 会话盐（解码者需要，可公开）
        user_id: 嵌入的用户 ID
        anchors: 锚点位置列表（按 codeword 位序）
        n_anchors: 锚点总数（= codeword 长度）
        n_natural: 天然满足目标 bit 的锚点数（零失真）
        n_replaced: 实际替换的锚点数
        n_skipped: 无法满足的锚点数（信道错误，由纠错码吸收）
        code_name: 使用的信道编码
        codeword_bits: 码字长度
    """
    watermarked_text: str
    session_salt: bytes
    user_id: int
    anchors: List[int]
    n_anchors: int
    n_natural: int
    n_replaced: int
    n_skipped: int
    code_name: str
    codeword_bits: int


class Embedder:
    """水印嵌入器（decode 模式）。

    使用方式：
        embedder = Embedder(master_key)
        result = embedder.embed(text, user_id=42)
        # 发布 result.watermarked_text，把 session_salt 存档
    """

    def __init__(
        self,
        master_key: bytes,
        config: EmbedConfig = EmbedConfig(),
    ) -> None:
        if len(master_key) < 16:
            raise ValueError("master_key too short (>= 16 bytes)")
        self.master_key = master_key
        self.config = config
        self.synonyms = config.synonyms or _SYNONYMS

    # ------------------------------------------------------------------
    # 分词与候选
    # ------------------------------------------------------------------

    def _tokenize(self, text: str) -> List[str]:
        """简单空白分词（MVP）。v0.3 应接入真实 tokenizer。"""
        import re
        return re.findall(r"\S+|\s+", text)

    def _is_word_token(self, token: str) -> bool:
        return bool(token.strip()) and any(c.isalpha() for c in token)

    def _get_candidates(self, token: str) -> List[str]:
        """获取 token 的同义候选（保持原词大小写风格）。"""
        key = token.lower().strip(".,!?;:\"'")
        cands = self.synonyms.get(key, [])
        if not cands:
            return []
        result = []
        for c in cands:
            c = c.strip()
            if not c:
                continue
            if c == key:
                result.append(token)
            elif token and token[0].isupper():
                result.append(c[0].upper() + c[1:])
            else:
                result.append(c)
        seen = set()
        unique = []
        for c in result:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique

    def _normalize(self, token: str) -> str:
        """token → 词典查询键（小写、去标点）。"""
        return token.lower().strip(".,!?;:\"'")

    def _find_anchorable_positions(
        self, tokens: List[str], session_salt: bytes
    ) -> List[int]:
        """可表达词位（该位置密钥映射下，其同义组能表达双 bit 的词）。

        判定与实际 KeyedLetterMap 一致（而非固定 A-M/N-Z 半区）：
        词 w 在位置 i 可锚定 ⇔ 组 G(w) 在 map(key, salt, i) 下 {0,1} 都有词。
        覆盖性是 (组, 位置) 的函数——map 按位置独立派生，不做跨位置缓存。

        稳定性论证（关键）：
        1. 替换只发生在组内（w → w' ∈ G(w)）→ 组身份不变
        2. 组对 map(i) 的双 bit 覆盖性只依赖组内容 → 覆盖性不变
        3. 词典外词永不被改
        因此池在嵌入/解码间保持一致，且锚点 skip 严格为零。
        """
        out = []
        for i, tok in enumerate(tokens):
            key = self._normalize(tok)
            group = self.synonyms.get(key)
            if not group:
                continue
            letter_map = self._letter_map_for(session_salt, i)
            bits = {letter_map.token_to_bit(c) for c in group}
            if bits == {0, 1}:
                out.append(i)
        return out

    def _letter_map_for(self, session_salt: bytes, position: int) -> KeyedLetterMap:
        """派生指定锚点位置的密钥字母映射。"""
        ctx = KeyContext(session_salt=session_salt, position=position, info=b"aawm:map")
        seed = derive_key(self.master_key, ctx)
        return KeyedLetterMap(seed)

    def required_anchors(self) -> int:
        """当前配置所需的锚点数（= codeword 长度）。"""
        payload_len = self.config.user_id_bits + 8  # + CRC-8
        code = get_code(self.config.code_name, payload_len)
        return code.codeword_bits

    # ------------------------------------------------------------------
    # 嵌入
    # ------------------------------------------------------------------

    def embed(
        self,
        text: str,
        user_id: int,
        session_salt: Optional[bytes] = None,
    ) -> EmbedResult:
        """嵌入携带用户 ID 的水印。

        Args:
            text: 原始文本
            user_id: 用户 ID（0 <= user_id < 2^user_id_bits）
            session_salt: 会话盐（默认随机生成；同一文本+盐+ID 的嵌入是确定性的）

        Returns:
            EmbedResult

        Raises:
            ValueError: user_id 超范围，或文本容量不足（词数 < codeword 长度）
        """
        if user_id < 0 or user_id >= (1 << self.config.user_id_bits):
            raise ValueError(
                f"user_id must be in [0, {1 << self.config.user_id_bits})"
            )
        if session_salt is None:
            session_salt = generate_session_salt()

        # 编码：user_id → payload → codeword
        payload = build_payload(user_id, self.config.user_id_bits)
        code = get_code(self.config.code_name, len(payload))
        code_bits = code.encode(payload)
        n_needed = len(code_bits)

        # 锚点：固定数量 = codeword 长度，池 = 可表达词位
        tokens = self._tokenize(text)
        word_positions = self._find_anchorable_positions(tokens, session_salt)
        if len(word_positions) < n_needed:
            raise ValueError(
                f"容量不足：当前配置需要 {n_needed} 个可表达词位（跨半区同义组），"
                f"文本只有 {len(word_positions)} 个。"
                f"请加长文本、改用低开销编码（hamming74），或扩充同义词典"
            )

        anchor_ctx = KeyContext(session_salt=session_salt, info=b"aawm:anchor")
        anchor_seed = derive_key(self.master_key, anchor_ctx)
        anchor_cfg = AnchorConfig(
            alpha=1.0, min_anchors=n_needed, max_anchors=n_needed
        )
        anchors = select_anchors(word_positions, anchor_seed, anchor_cfg)

        # 逐锚点嵌入
        watermarked = list(tokens)
        n_natural = n_replaced = n_skipped = 0

        for idx, pos in enumerate(anchors):
            target_bit = code_bits[idx]
            letter_map = self._letter_map_for(session_salt, pos)
            token = tokens[pos]

            if letter_map.token_to_bit(token) == target_bit:
                # 原词天然携带目标 bit，零失真
                n_natural += 1
                continue

            # 需要替换：找首字母映射为目标 bit 的同义词
            chosen = None
            for c in self._get_candidates(token):
                if c != token and letter_map.token_to_bit(c) == target_bit:
                    chosen = c
                    break

            if chosen is None:
                # 无可用候选 → 信道错误，交给纠错码
                n_skipped += 1
            else:
                watermarked[pos] = chosen
                n_replaced += 1

        return EmbedResult(
            watermarked_text="".join(watermarked),
            session_salt=session_salt,
            user_id=user_id,
            anchors=anchors,
            n_anchors=len(anchors),
            n_natural=n_natural,
            n_replaced=n_replaced,
            n_skipped=n_skipped,
            code_name=code.name,
            codeword_bits=n_needed,
        )
