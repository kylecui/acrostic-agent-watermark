"""Acrostic Agent Watermark (AAWM).

Agent-level digital watermarking via acrostic-style token transforms.
v0.5: 双信道签名架构 —— 信道 A 段落 Merkle-HMAC 绑定 + 信道 B 绿名单频带统计。
v0.6: 通用 Agent 插件层 —— Facade / 中间件 / 流式 / 框架适配器。
"""

__version__ = "0.7.0"

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
from .greenlist import (
    GreenlistCodec,
    BandReport,
    BandStat,
    build_zero_cost_zh_codec,
    build_hybrid_zh_codec,
)
from .binding import DocumentBinder, BindingSeal, BindingVerdict, VerdictKind

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
    # 信道 B：绿名单 × 频带统计（v0.5）
    "GreenlistCodec",
    "BandReport",
    "BandStat",
    "build_zero_cost_zh_codec",
    "build_hybrid_zh_codec",
    # 信道 A：段落 Merkle-HMAC 绑定（v0.5）
    "DocumentBinder",
    "BindingSeal",
    "BindingVerdict",
    "VerdictKind",
]

# v0.6 插件层便捷导出（懒加载，避免循环导入）
# 完整 API 见 aawm.plugins
def __getattr__(name: str):  # type: ignore
    if name in ("Watermarker", "EmbedResult", "TraceResult", "KeyStore",
                "UIDRegistry", "WatermarkMiddleware", "StreamingWatermarker",
                "Context", "ContextChain", "generate_key"):
        from . import plugins
        return getattr(plugins, name)
    raise AttributeError(f"module 'aawm' has no attribute {name!r}")

