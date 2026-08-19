"""统一 Watermarker Facade：一键嵌入 + 一键溯源。

把 GreenlistCodec（信道B 溯源）+ DocumentBinder（信道A 防篡改）+ UIDRegistry
组合成简洁的三件套 API：

    watermarker = Watermarker.from_config("config.json")
    result = watermarker.embed(text, user_id=42)
    # 发布 result.watermarked_text

    trace = watermarker.trace(suspect_text)
    if trace.watermarked:
        print(trace.user, trace.confidence)

这是插件层的核心——所有框架适配器都通过这个 Facade 调用算法层。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from ..binding import BindingSeal, BindingVerdict, DocumentBinder
from ..greenlist import BandReport, BandReport as BandStat, GreenlistCodec
from ..keys import generate_master_key, generate_session_salt
from .keystore import KeyStore
from .registry import UIDRegistry
from .context import _detect_language


# ----------------------------------------------------------------------
# 结果类型
# ----------------------------------------------------------------------

@dataclass
class EmbedResult:
    """嵌入结果。

    Attributes:
        watermarked_text: 水印后的文本（发布这个）
        session_salt: 会话盐（检测方需要，可公开，需存档）
        user_id: 实际嵌入的 UID（int）
        user_alias: 用户别名（如有注册库）
        seal: 信道 A 签名（如启用）；None=未签名
        language: 使用的语言
        n_dict_words: 词典命中词数
        existence_score: 存在性得分（嵌入后自检）
    """
    watermarked_text: str
    session_salt: bytes
    user_id: int
    user_alias: Optional[str] = None
    seal: Optional[BindingSeal] = None
    language: str = "en"
    n_dict_words: int = 0
    existence_score: float = 0.0


@dataclass
class TraceResult:
    """溯源结果。

    Attributes:
        watermarked: 是否检出水印（存在性判定）
        uid: 解码出的 UID（int），未检出/低置信时为 None
        user: 注册库匹配到的用户别名；None=无匹配或无注册库
        hamming_dist: 与最近邻 UID 的汉明距（-1=无匹配）
        confidence: 置信度 [0,1]，基于 existence_score 归一化
        tampered: 信道 A 篡改判定。True=被篡改；False=未篡改；None=无 seal 无法判定
        tampered_paragraphs: 被改段落索引
        band_report: 逐带明细
        existence_score: 原始存在性得分 Σ|z|
        n_dict_words: 词典命中词数
    """
    watermarked: bool
    uid: Optional[int]
    user: Optional[str]
    hamming_dist: int
    confidence: float
    tampered: Optional[bool]
    tampered_paragraphs: List[int] = field(default_factory=list)
    band_report: Optional[BandReport] = None
    existence_score: float = 0.0
    n_dict_words: int = 0


# ----------------------------------------------------------------------
# 存在性检测阈值（可调）
# ----------------------------------------------------------------------

@dataclass
class DetectionThresholds:
    """检测阈值配置。

    存在性判定策略：自适应阈值（基于词典命中数 n_dict_words）。
    - null 分布的 Σ|z| 期望 ≈ √(n_bands) × √(n/4)（每带 n/bands 个词的随机游走）
    - 水印文本的 Σ|z| 显著高于此
    - 阈值 = max(fixed_floor, adaptive_factor × √(n_dict_words))
    """
    # 自适应系数：阈值 = adaptive_factor × √(n_dict_words)
    # null 经验值系数 ~1.0-1.5，水印 ~2.5-4.0，取 2.0 做保守分界
    adaptive_factor: float = 2.0
    # 固定下限：极短文本的兜底阈值
    existence_floor: float = 8.0
    # 置信度归一化分母（existence_score / 此值 → [0,1]）
    confidence_scale: float = 40.0
    # 注册库最近邻匹配最大汉明距
    max_hamming: int = 3


# ----------------------------------------------------------------------
# Facade
# ----------------------------------------------------------------------

class Watermarker:
    """统一水印 Facade：一键嵌入 + 一键溯源。

    组合 GreenlistCodec（信道B）+ DocumentBinder（信道A）+ UIDRegistry。

    用法::

        # 最简：纯内存
        wm = Watermarker()
        result = wm.embed(text, user_id=42)

        # 带注册库 + 持久化密钥
        wm = Watermarker(
            keystore=KeyStore.from_file("key.json", create=True),
            registry=UIDRegistry(backend="file", path="registry.json"),
        )
        result = wm.embed(text, user_id="agent-cuiyin")  # 别名自动注册
    """

    def __init__(
        self,
        master_key: Optional[Union[bytes, str]] = None,
        *,
        keystore: Optional[KeyStore] = None,
        registry: Optional[UIDRegistry] = None,
        language: str = "auto",
        thresholds: Optional[DetectionThresholds] = None,
    ) -> None:
        # 密钥
        if keystore is not None:
            self._keystore = keystore
        elif master_key is not None:
            self._keystore = KeyStore(master_key if isinstance(master_key, bytes)
                                       else bytes.fromhex(master_key))
        else:
            self._keystore = KeyStore()  # 随机生成
        self._master_key = self._keystore.get()

        # 注册库（可选）
        self._registry = registry

        # 默认语言
        self._default_language = language

        # 阈值
        self._thresholds = thresholds or DetectionThresholds()

        # p0 标定缓存：{language_tag: bool}
        self._p0_calibrated: Dict[bytes, bool] = {}

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        key_file: Optional[str] = None,
        registry_file: Optional[str] = None,
        language: str = "auto",
    ) -> "Watermarker":
        """从配置文件创建（便捷方法）。

        Args:
            key_file: 密钥文件路径（不存在则自动创建）
            registry_file: 注册库文件路径（None=纯内存）
            language: 默认语言 "en"/"zh"/"auto"
        """
        ks = KeyStore.from_file(key_file, create=True) if key_file else KeyStore()
        reg = UIDRegistry(backend="file", path=registry_file) if registry_file else UIDRegistry()
        return cls(keystore=ks, registry=reg, language=language)

    # ------------------------------------------------------------------
    # 嵌入
    # ------------------------------------------------------------------

    def embed(
        self,
        text: str,
        user_id: Union[int, str],
        *,
        session_salt: Optional[bytes] = None,
        sign: bool = True,
        language: Optional[str] = None,
        bias: float = 1.0,
        rng_seed: Optional[int] = None,
    ) -> EmbedResult:
        """嵌入水印。

        Args:
            text: 原始文本
            user_id: int=UID直传；str=别名经注册库映射
            session_salt: 会话盐（None→自动生成）
            sign: 是否同时签信道 A（Merkle 防篡改）
            language: 语言覆盖（None→用实例默认 / auto 检测）
            bias: 嵌入强度（1.0=全词参与，<1.0=随机跳过换低偏移）
            rng_seed: 随机种子（None=不固定；指定后同 text+salt+uid+seed 确定性嵌入）

        Returns:
            EmbedResult
        """
        # 1. 解析 user_id
        uid, alias = self._resolve_user_id(user_id)

        # 2. 语言
        lang = self._resolve_language(text, language)
        lang_tag = b"zh" if lang == "zh" else b"en"

        # 3. 盐
        if session_salt is None:
            session_salt = generate_session_salt()

        # 4. 信道 B 嵌入
        codec = GreenlistCodec(self._master_key, session_salt, language_tag=lang_tag)
        rng = None
        if rng_seed is not None:
            import random
            rng = random.Random(rng_seed)
        marked = codec.embed(text, uid, bias=bias, rng=rng)

        # 5. 自检
        report = codec.detect(marked)

        # 6. 信道 A 签名（可选）
        seal = None
        if sign:
            binder = DocumentBinder(self._master_key, session_salt)
            seal = binder.sign(marked, aad=uid.to_bytes(2, "big"))

        return EmbedResult(
            watermarked_text=marked,
            session_salt=session_salt,
            user_id=uid,
            user_alias=alias,
            seal=seal,
            language=lang,
            n_dict_words=report.n_dict_words,
            existence_score=report.existence_score,
        )

    # ------------------------------------------------------------------
    # 溯源
    # ------------------------------------------------------------------

    def trace(
        self,
        text: str,
        *,
        session_salt: Optional[bytes] = None,
        seal: Optional[BindingSeal] = None,
        language: Optional[str] = None,
    ) -> TraceResult:
        """溯源：存在性检测 + UID 解码 + 注册库最近邻匹配 + 篡改判定。

        Args:
            text: 嫌疑文本
            session_salt: 会话盐（有则做信道A验证 + 用原盐解码）
            seal: 信道 A 签名（有则验证篡改）
            language: 语言覆盖

        Returns:
            TraceResult
        """
        lang = self._resolve_language(text, language)
        lang_tag = b"zh" if lang == "zh" else b"en"

        # 信道 B 检测
        codec = GreenlistCodec(self._master_key, session_salt or generate_session_salt(),
                                language_tag=lang_tag)
        report = codec.detect(text)

        # 存在性判定：自适应阈值
        threshold = self._compute_threshold(report.n_dict_words)
        watermarked = report.existence_score >= threshold

        # UID 解码 + 注册库匹配
        uid = report.uid if watermarked else None
        user = None
        hamming_dist = -1
        if watermarked and uid is not None and self._registry is not None:
            match = self._registry.nearest_match(uid, max_hamming=self._thresholds.max_hamming)
            if match is not None:
                _, user, hamming_dist = match
            else:
                hamming_dist = min(
                    (bin(uid ^ u).count("1") for u in self._registry.list_all()),
                    default=-1,
                )

        # 置信度
        confidence = min(1.0, report.existence_score / self._thresholds.confidence_scale)

        # 信道 A 篡改判定
        tampered = None
        tampered_paras: List[int] = []
        if seal is not None and session_salt is not None:
            binder = DocumentBinder(self._master_key, session_salt)
            verdict = binder.verify(text, seal)
            tampered = not verdict.ok
            tampered_paras = list(verdict.mismatched_indices)

        return TraceResult(
            watermarked=watermarked,
            uid=uid,
            user=user,
            hamming_dist=hamming_dist,
            confidence=confidence,
            tampered=tampered,
            tampered_paragraphs=tampered_paras,
            band_report=report,
            existence_score=report.existence_score,
            n_dict_words=report.n_dict_words,
        )

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def detect_only(
        self,
        text: str,
        *,
        session_salt: Optional[bytes] = None,
        language: Optional[str] = None,
    ) -> bool:
        """只做存在性检测。

        注意：存在性得分依赖 session_salt（绿名单派生自 salt）。
        - 有 salt：用原 salt 检测，精度高
        - 无 salt：用随机盐，精度有限（适合 p0 已标定的场景做初筛）

        Args:
            text: 嫌疑文本
            session_salt: 会话盐（推荐传入）
            language: 语言覆盖
        """
        lang = self._resolve_language(text, language)
        lang_tag = b"zh" if lang == "zh" else b"en"
        salt = session_salt or generate_session_salt()
        codec = GreenlistCodec(self._master_key, salt, language_tag=lang_tag)
        report = codec.detect(text)
        threshold = self._compute_threshold(report.n_dict_words)
        return report.existence_score >= threshold

    def calibrate_p0(self, corpus: List[str], language: str = "en") -> None:
        """在无水印参考语料上标定 p0（提升检测精度）。

        部署时用一批真实无水印文本调一次即可。
        """
        lang_tag = b"zh" if language == "zh" else b"en"
        codec = GreenlistCodec(self._master_key, generate_session_salt(), language_tag=lang_tag)
        codec.calibrate_p0(corpus)
        self._p0_calibrated[lang_tag] = True

    @property
    def registry(self) -> Optional[UIDRegistry]:
        return self._registry

    @property
    def keystore(self) -> KeyStore:
        return self._keystore

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _resolve_user_id(self, user_id: Union[int, str]) -> Tuple[int, Optional[str]]:
        """把 user_id（int 或 str 别名）解析为 (uid, alias)。"""
        if isinstance(user_id, int):
            return user_id, None
        # str：经注册库映射
        if self._registry is None:
            # 无注册库时，把字符串哈希为 UID
            h = int.from_bytes(hashlib.sha256(user_id.encode()).digest()[:2], "big")
            return h, user_id
        uid = self._registry.resolve_alias(user_id)
        return uid, user_id

    def _resolve_language(self, text: str, override: Optional[str]) -> str:
        if override is not None:
            return override
        if self._default_language != "auto":
            return self._default_language
        return _detect_language(text)

    def _compute_threshold(self, n_dict_words: int) -> float:
        """根据词典命中数计算自适应存在性阈值。

        null 分布的 Σ|z| 随 n_dict_words 增长（近似 √n）。
        阈值 = max(floor, factor × √(n_dict_words))。
        """
        import math
        adaptive = self._thresholds.adaptive_factor * math.sqrt(max(n_dict_words, 1))
        return max(self._thresholds.existence_floor, adaptive)
