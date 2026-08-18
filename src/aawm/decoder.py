"""解码器：从文本中还原嵌入的用户 ID（decode 模式）。

流程（与嵌入器严格对称）：
    重算锚点 → 逐锚点用 KeyedLetterMap 读出 bit → codeword
    → 纠错解码 → payload → CRC-8 校验 → user_id

置信度依据：
    crc_ok        — CRC 通过（随机文本通过概率仅 2^-8 ≈ 0.4%）
    n_errors      — 观测误码数（解码后再编码与读出序列的海明距离）
    error_rate    — 观测误码率（信道质量指标）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .anchor import AnchorConfig, select_anchors
from .coding import (
    build_payload,
    get_code,
    hamming_distance,
    parse_payload,
)
from .embedder import EmbedConfig, Embedder
from .keys import KeyContext, derive_key


@dataclass(frozen=True)
class DecodeResult:
    """解码结果。

    Attributes:
        success: 是否成功还原用户 ID（= crc_ok 且容量足够）
        user_id: 还原的用户 ID（失败时为 None）
        crc_ok: CRC-8 校验是否通过
        n_anchors: 实际使用的锚点数
        n_code_errors: 纠错动作数（解码器翻转的位数）
        n_errors: 观测总误码数（含未纠正的，解码后重编码对比）
        error_rate: 观测误码率 n_errors / codeword_bits
        reason: 失败原因（成功时为空）
    """
    success: bool
    user_id: Optional[int]
    crc_ok: bool
    n_anchors: int
    n_code_errors: int
    n_errors: int
    error_rate: float
    reason: str = ""


class Decoder:
    """水印解码器。

    使用方式：
        decoder = Decoder(master_key)
        result = decoder.decode(suspect_text, session_salt)
        if result.success:
            print(f"水印属于用户 {result.user_id}")
    """

    def __init__(
        self,
        master_key: bytes,
        config: EmbedConfig = EmbedConfig(),
    ) -> None:
        if len(master_key) < 16:
            raise ValueError("master_key too short (>= 16 bytes)")
        # 复用 Embedder 的分词/词典/映射逻辑，保证嵌入-解码对称
        self._embedder = Embedder(master_key, config)
        self.master_key = master_key
        self.config = config

    def decode(
        self,
        suspect_text: str,
        session_salt: bytes,
    ) -> DecodeResult:
        """从嫌疑文本解码用户 ID。

        Args:
            suspect_text: 待检测文本（可能经过编辑/攻击）
            session_salt: 嵌入时的会话盐

        Returns:
            DecodeResult
        """
        emb = self._embedder
        payload_len = self.config.user_id_bits + 8
        code = get_code(self.config.code_name, payload_len)
        n_needed = code.codeword_bits

        tokens = emb._tokenize(suspect_text)
        word_positions = emb._find_anchorable_positions(tokens, session_salt)
        if len(word_positions) < n_needed:
            return DecodeResult(
                success=False, user_id=None, crc_ok=False,
                n_anchors=len(word_positions), n_code_errors=0,
                n_errors=0, error_rate=0.0,
                reason=f"容量不足：需要 {n_needed} 个可表达词位，实际 {len(word_positions)}",
            )

        # 重算锚点（与嵌入者相同的种子与数量）
        anchor_ctx = KeyContext(session_salt=session_salt, info=b"aawm:anchor")
        anchor_seed = derive_key(self.master_key, anchor_ctx)
        anchor_cfg = AnchorConfig(alpha=1.0, min_anchors=n_needed, max_anchors=n_needed)
        anchors = select_anchors(word_positions, anchor_seed, anchor_cfg)

        # 逐锚点读 bit
        read_bits = []
        for idx, pos in enumerate(anchors):
            letter_map = emb._letter_map_for(session_salt, pos)
            token = tokens[pos] if pos < len(tokens) else ""
            bit = letter_map.token_to_bit(token)
            read_bits.append(0 if bit is None else bit)

        # 纠错解码 + CRC
        payload, n_corrected = code.decode(read_bits)
        user_id, crc_ok = parse_payload(payload, self.config.user_id_bits)

        # 观测误码：解码结果重编码 vs 实际读出序列的海明距离
        reencoded = code.encode(payload)
        n_errors = hamming_distance(read_bits, reencoded)
        error_rate = n_errors / len(read_bits) if read_bits else 0.0

        if not crc_ok:
            reason = "CRC 校验失败：无水印、密钥错误或误码超过纠错能力"
        else:
            reason = ""

        return DecodeResult(
            success=crc_ok,
            user_id=user_id if crc_ok else None,
            crc_ok=crc_ok,
            n_anchors=len(anchors),
            n_code_errors=n_corrected,
            n_errors=n_errors,
            error_rate=error_rate,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # 辅助：多用户比对模式
    # ------------------------------------------------------------------

    def identify(
        self,
        suspect_text: str,
        session_salt: bytes,
        candidate_ids: list,
    ) -> Optional[int]:
        """在候选用户名单中比对，返回匹配的用户 ID。

        用于 user_id 不是完整编号而是指纹的场景：
        CRC 恢复出指纹后与注册表比对。CRC 已通过时结果与 decode 一致；
        CRC 失败时逐一尝试候选（低置信度，仅供排查参考）。
        """
        result = self.decode(suspect_text, session_salt)
        if result.success:
            return result.user_id
        # CRC 失败：对每个候选重嵌入比对码字距离，取最近者（若显著近）
        emb = self._embedder
        best_id, best_rate = None, float("inf")
        for uid in candidate_ids:
            try:
                r = emb.embed(suspect_text, uid, session_salt)
            except ValueError:
                continue
            d = self.decode(r.watermarked_text, session_salt)
            if d.error_rate < best_rate:
                best_rate, best_id = d.error_rate, uid
        # 简单阈值：误码率 < 25% 才认为是匹配（repeat3 可纠 33%）
        if best_id is not None and best_rate < 0.25:
            return best_id
        return None
