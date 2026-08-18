"""验证器（v0.2）：基于解码结果的检测封装。

v0.1 的"谓词 + z-test"检测已被 decode 模式取代：
- 检出判定：CRC-8 校验通过（随机文本误报概率 2^-8 ≈ 0.4%）
- 统计强度：z-test 以"观测误码数 vs H0 误码 Binomial(n, 0.5)"计算，
  反映读出序列与码字的对齐程度

如需 v0.1 谓词检测（不含用户 ID），见 git 历史或 stats.z_test。
"""
from __future__ import annotations

import math

from .decoder import DecodeResult, Decoder
from .embedder import EmbedConfig
from .stats import DetectionResult


class Verifier:
    """水印验证器。

    使用方式：
        verifier = Verifier(master_key)
        result = verifier.detect(text, session_salt)
        if result.detected:
            print(result.payload.user_id)  # DecodeResult
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
        self._decoder = Decoder(master_key, config)

    def detect(
        self,
        suspect_text: str,
        session_salt: bytes,
        alpha: float = 0.01,
    ) -> DetectionResult:
        """检测水印是否存在（并附带完整解码结果）。

        Args:
            suspect_text: 嫌疑文本
            session_salt: 会话盐
            alpha: 显著性水平（参考值；检出主判定以 CRC 为准）

        Returns:
            DetectionResult，payload 字段携带 DecodeResult（含 user_id）
        """
        dec: DecodeResult = self._decoder.decode(suspect_text, session_salt)

        if dec.n_anchors <= 0 or dec.n_errors == 0 and dec.error_rate == 0.0 and not dec.crc_ok:
            # 无有效锚点，z 无意义
            z, p = 0.0, 1.0
        else:
            n = dec.n_anchors
            mean = 0.5 * n
            std = math.sqrt(n * 0.25)
            z = (mean - dec.n_errors) / std if std > 0 else 0.0
            # P(Binomial(n,0.5) <= 观测错误数) 的正态近似
            p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

        return DetectionResult(
            hits=dec.n_anchors - dec.n_errors,  # 一致位数
            total=dec.n_anchors,
            z_score=z,
            p_value=p,
            detected=dec.success,
            payload=dec,
        )
