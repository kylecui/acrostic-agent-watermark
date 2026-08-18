"""Acrostic Agent Watermark (AAWM).

Agent-level digital watermarking via acrostic-style token transforms.
v0.4: 句子边界感知指纹 + 词典扩充 + 中文声母支持。
"""

__version__ = "0.4.0"

from .keys import derive_key, KeyContext, generate_master_key, generate_session_salt
from .stats import z_test, DetectionResult
from .anchor import select_anchors, AnchorConfig
from .transforms import (
    Predicate,
    FirstLetterPredicate,
    register_predicate,
    KeyedLetterMap,
)
from .coding import (
    build_payload,
    parse_payload,
    crc8,
    get_code,
    available_codes,
    RepetitionCode,
    Hamming74Code,
)
from .embedder import Embedder, EmbedConfig, EmbedResult
from .decoder import Decoder, DecodeResult
from .verifier import Verifier
from .content import (
    CAEmbedder,
    CADecoder,
    CAConfig,
    CAEmbedResult,
    CADecodeResult,
)
from .zh import (
    LanguageAdapter,
    EnAdapter,
    ZhAdapter,
    get_adapter,
)

__all__ = [
    # 密钥
    "derive_key",
    "KeyContext",
    "generate_master_key",
    "generate_session_salt",
    # 统计（detect 模式）
    "z_test",
    "DetectionResult",
    # 锚点
    "select_anchors",
    "AnchorConfig",
    # 谓词与映射
    "Predicate",
    "FirstLetterPredicate",
    "register_predicate",
    "KeyedLetterMap",
    # 信道编码
    "build_payload",
    "parse_payload",
    "crc8",
    "get_code",
    "available_codes",
    "RepetitionCode",
    "Hamming74Code",
    # 嵌入与解码（v0.2 主线）
    "Embedder",
    "EmbedConfig",
    "EmbedResult",
    "Decoder",
    "DecodeResult",
    # 内容寻址锚点（v0.3 主线 + v0.4 句子感知/中文）
    "CAEmbedder",
    "CADecoder",
    "CAConfig",
    "CAEmbedResult",
    "CADecodeResult",
    # 语言适配器（v0.4）
    "LanguageAdapter",
    "EnAdapter",
    "ZhAdapter",
    "get_adapter",
    # detect 模式（遗留）
    "Verifier",
]
