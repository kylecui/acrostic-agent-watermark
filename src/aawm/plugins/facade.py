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
        codec_mode: 使用的 codec 模式（"zero_cost"/"hybrid"/"default"）
        bands: 自适应编码使用的带列表（自适应检测需要，需存档）
        capacity: 文档有效容量 k（活动带数）
        n_bits: 实际编码位数（含冗余时 < capacity）
    """
    watermarked_text: str
    session_salt: bytes
    user_id: int
    user_alias: Optional[str] = None
    seal: Optional[BindingSeal] = None
    language: str = "en"
    n_dict_words: int = 0
    existence_score: float = 0.0
    codec_mode: str = "default"
    bands: List[int] = field(default_factory=list)
    capacity: int = 0
    n_bits: int = 0


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
        soft_uid: 软判决匹配结果 UID（trace(soft_match=True) 时填充；未匹配=低置信时为 None）
        soft_gap: 软判决最优与次优候选得分差（soft 路径的置信度量；未启用时为 -1.0）
        codec_mode: 使用的 codec 模式
        bands: 自适应检测使用的带列表（None=旧路径）
        capacity: 自适应容量 k（自适应路径）
        active_bands: 攻击后仍存活的活动带数（自适应路径）
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
    soft_uid: Optional[int] = None
    soft_gap: float = -1.0
    codec_mode: str = "default"
    bands: List[int] = field(default_factory=list)
    capacity: int = 0
    n_bits: int = 0
    active_bands: int = 0


# ----------------------------------------------------------------------
# 存在性检测阈值（可调）
# ----------------------------------------------------------------------

@dataclass
class DetectionThresholds:
    """检测阈值配置。

    存在性判定策略（default 模式，min_n=2 统计）：
    - null 分布的 Σ|z| 期望 ≈ √(n_bands) × √(n/4)（每带 n/bands 个词的随机游走）
    - 水印文本的 Σ|z| 显著高于此
    - 阈值 = max(fixed_floor, adaptive_factor × √(n_dict_words))

    自适应路径（zero_cost/hybrid + bands 元数据，min_n=1 统计）：
    - n=1 带的 |z| 恒为 1（null 与 marked 相同），Σ|z| 随活动带数 m
      近似线性增长，√n_dict 公式失效
    - 阈值 = adaptive_intercept + adaptive_slope × m（活动带数）
    - 默认常数来自 docs 语料实测（null 线性拟合 + 2.5σ 余量）；
      生产部署应传 calibrate_corpus 自动标定（更准）
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
    # 自适应路径（min_n=1 统计）线性阈值：intercept + slope × 活动带数
    # 实测依据：p0 标定后 null Σ|z| ≈ 1.0-1.1/带，marked ≈ 2.0+/带
    adaptive_intercept: float = 1.0
    adaptive_slope: float = 1.6


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

        # 中文零感模式（推荐用于中文生产场景）
        wm = Watermarker(keystore=ks, language="zh", codec_mode="zero_cost")
        result = wm.embed(text, user_id=42)
        # 发布 result.watermarked_text，存档 result.bands + session_salt

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
        codec_mode: str = "default",
        supplementary_dict: Optional[Dict[str, List[str]]] = None,
        calibrate_corpus: Optional[List[str]] = None,
    ) -> None:
        """Args:
            codec_mode: 中文 codec 模式。
                "default"  — 全词林 GreenlistCodec（旧行为，向后兼容）
                "zero_cost"— 零感词典（136 组高自然替换，推荐）
                "hybrid"   — 零感打底 + supplementary_dict 补带
            supplementary_dict: hybrid 模式的补充词典 {组名: [词列表]}
            calibrate_corpus: 无水印参考语料，构建 codec 时标定 p0
        """
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

        # codec 模式与配置
        if codec_mode not in ("default", "zero_cost", "hybrid"):
            raise ValueError(f"未知 codec_mode: {codec_mode!r} "
                             "(可选 default/zero_cost/hybrid)")
        self._codec_mode = codec_mode
        self._supplementary_dict = supplementary_dict
        self._calibrate_corpus = calibrate_corpus

        # p0 标定缓存：{language_tag: bool}
        self._p0_calibrated: Dict[bytes, bool] = {}

        # null 存在性模型（自适应路径阈值标定）：
        # (每带均值 ratio, 阈值 ratio) — Σ|z|/m ≈ ratio·N(μ, σ)
        # 阈值 = m × (μ + 2.5σ)。m = 活动带数。
        # 仅当提供 calibrate_corpus 且中文自适应模式时计算
        self._null_model: Optional[Tuple[float, float]] = None
        if calibrate_corpus and codec_mode != "default":
            self._fit_null_model(calibrate_corpus)

    def _fit_null_model(self, corpus: List[str]) -> None:
        """在 null 语料上拟合存在性阈值模型（自适应路径专用）。

        每带归一化 ratio 模型：对每篇 null 文本用多个不同 salt 的
        codec 检测（min_n=1），收集每带平均得分 r = Σ|z|/m（m=活动带数），
        阈值 ratio = μ_r + 2.5·σ_r，判定阈值 = m × 阈值 ratio。

        两个关键点：
        - 多 salt 采样：绿名单颜色随 salt 重排，null 得分的 salt 间
          方差远大于语料间残差——单 salt 拟合严重低估 σ。
        - ratio 归一化：每带均值跨 m 稳定，避免线性回归在标定语料
          m 散布窄时外推失稳（实测线性模型 FP 1/30~13/30，
          ratio 模型 0/30 且 marked/null ratio 分离 4 倍以上）。
        """
        ratios: List[float] = []
        for _ in range(5):  # 5 个 salt：σ 覆盖 salt 间方差
            codec = self._build_codec(generate_session_salt(), b"zh")
            for t in corpus:
                rep = codec.detect(t, min_n=1)
                m = sum(1 for st in rep.bands if st.has_signal)
                if m > 0:
                    ratios.append(rep.existence_score / m)
        if len(ratios) < 3:
            return
        n = len(ratios)
        mu = sum(ratios) / n
        sd = (sum((r - mu) ** 2 for r in ratios) / n) ** 0.5
        if sd < 1e-9:
            sd = 0.1  # 完全同质语料：给保守余量
        # 3σ（非 2.5σ）：标定语料有限时 σ 本身仍是低估的，
        # 且 marked/null ratio 分离 4 倍以上，宽阈值无漏检代价
        self._null_model = (mu, mu + 3.0 * sd)

    # ------------------------------------------------------------------
    # codec 构建
    # ------------------------------------------------------------------

    def _build_codec(self, session_salt: bytes, lang_tag: bytes) -> GreenlistCodec:
        """按 codec_mode 与语言构建 codec。

        default 模式或英文 → 旧 GreenlistCodec 默认构造（向后兼容）；
        zero_cost/hybrid + 中文 → 零感/混合 codec。
        """
        if lang_tag != b"zh" or self._codec_mode == "default":
            return GreenlistCodec(self._master_key, session_salt,
                                  language_tag=lang_tag)
        if self._codec_mode == "zero_cost":
            from ..greenlist import build_zero_cost_zh_codec
            return build_zero_cost_zh_codec(
                self._master_key, session_salt,
                calibrate_corpus=self._calibrate_corpus)
        # hybrid
        if self._supplementary_dict is None:
            raise ValueError("hybrid 模式需要 supplementary_dict")
        from ..greenlist import build_hybrid_zh_codec
        return build_hybrid_zh_codec(
            self._master_key, session_salt,
            supplementary_dict=self._supplementary_dict,
            calibrate_corpus=self._calibrate_corpus)

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        key_file: Optional[str] = None,
        registry_file: Optional[str] = None,
        language: str = "auto",
        codec_mode: str = "default",
        supplementary_dict: Optional[Dict[str, List[str]]] = None,
        calibrate_corpus: Optional[List[str]] = None,
    ) -> "Watermarker":
        """从配置文件创建（便捷方法）。

        Args:
            key_file: 密钥文件路径（不存在则自动创建）
            registry_file: 注册库文件路径（None=纯内存）
            language: 默认语言 "en"/"zh"/"auto"
            codec_mode: 中文 codec 模式（default/zero_cost/hybrid）
            supplementary_dict: hybrid 模式补充词典
            calibrate_corpus: p0 标定语料
        """
        ks = KeyStore.from_file(key_file, create=True) if key_file else KeyStore()
        reg = UIDRegistry(backend="file", path=registry_file) if registry_file else UIDRegistry()
        return cls(keystore=ks, registry=reg, language=language,
                   codec_mode=codec_mode, supplementary_dict=supplementary_dict,
                   calibrate_corpus=calibrate_corpus)

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
        n_bits: Optional[int] = None,
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
            n_bits: 自适应模式编码位数（None=满容量 k；<k 留冗余带抗替换）

        自适应模式（zero_cost/hybrid + 中文）注意：
            UID 实际编码在 n_bits 位空间——user_id 超出时取低 n_bits 位
            （容量自适应的固有约束；检测时注册库候选同样按低 n_bits 位匹配）。
            result.bands 必须存档，trace 时传入。

        Returns:
            EmbedResult
        """
        # 1. 解析 user_id
        uid, alias = self._resolve_user_id(user_id)

        # 2. 语言
        lang = self._resolve_language(text, language)
        lang_tag = b"zh" if lang == "zh" else b"en"

        # 3. 盐
        salt_fixed = session_salt is not None
        if session_salt is None:
            session_salt = generate_session_salt()

        rng = None
        if rng_seed is not None:
            import random
            rng = random.Random(rng_seed)

        # 4. 信道 B 嵌入（中文 + zero_cost/hybrid → 自适应路径）
        codec = self._build_codec(session_salt, lang_tag)
        adaptive = lang_tag == b"zh" and self._codec_mode != "default"

        if adaptive:
            # 自检重试（自动盐时换 salt，组颜色+带映射全变）：
            # 1) UID 回验：某词的唯一异色候选可能 boundary unsafe
            #    （如 "不但"+"是" 拼成 "但是"），颜色无法翻转 → 单带误码
            # 2) 信号余量：稀疏文本（每带 1-2 词）z 饱和，marked 得分
            #    可能贴着存在性阈值 → trace 随机漏检。salt 间信号方差
            #    巨大（实测同文本 8.5 vs 30.9），挑信号强的盐。
            # 通过标准：UID 正确 且 得分 ≥ 1.5×阈值；全部尝试不达标
            # 时返回余量最大的一次（UID 正确优先）。
            # 固定盐时只能换 rng 流（帮助多候选池，单候选无解）。
            # 注意容量随 salt 变化（活动带集不同），每次尝试须重算。
            max_attempts = 4
            # (honor, uid_ok, margin, marked, bands, report, salt, codec, eff_bits, k)
            # honor = 满足请求的 n_bits（显式请求时要求 k >= n_bits）——
            # 换盐重试会让容量缩水（如 15→11），若直接按满容量钳位会
            # 悄悄吞掉用户要的冗余带。显式 n_bits 下优先选能兑现的盐。
            best = None
            for attempt in range(max_attempts):
                k = codec.capacity(text)
                honor = n_bits is None or k >= n_bits
                eff_bits = n_bits if (honor and n_bits is not None) else k
                uid_eff = uid & ((1 << eff_bits) - 1) if eff_bits < 16 else uid
                marked, bands = codec.embed_adaptive(
                    text, uid_eff, n_bits=eff_bits, bias=bias, rng=rng)
                uid_chk, _, report = codec.detect_adaptive(marked, bands, min_n=1)
                threshold = self._compute_threshold_adaptive(report)
                margin = report.existence_score / threshold if threshold > 0 else float("inf")
                uid_ok = uid_chk == uid_eff
                cand = (honor, uid_ok, margin, marked, bands, report,
                        session_salt, codec, eff_bits, k)
                if best is None or (honor, uid_ok, margin) > (best[0], best[1], best[2]):
                    best = cand
                if honor and uid_ok and margin >= 1.5:
                    break
                if attempt < max_attempts - 1:
                    if not salt_fixed:
                        session_salt = generate_session_salt()
                        codec = self._build_codec(session_salt, lang_tag)
                    if rng_seed is not None:
                        import random
                        rng = random.Random(rng_seed + attempt + 1)
                    else:
                        rng = None
            _, _, _, marked, bands, report, session_salt, _, eff_bits, k = best
        else:
            marked = codec.embed(text, uid, bias=bias, rng=rng)
            report = codec.detect(marked)
            bands, eff_bits, k = [], 0, 0

        # 5. 信道 A 签名（可选）
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
            codec_mode=self._codec_mode if adaptive else "default",
            bands=list(bands),
            capacity=k,
            n_bits=eff_bits,
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
        soft_match: bool = False,
        match_margin: float = 2.0,
        match_margin_ratio: Optional[float] = None,
        bands: Optional[List[int]] = None,
        n_bits: Optional[int] = None,
    ) -> TraceResult:
        """溯源：存在性检测 + UID 解码 + 注册库匹配 + 篡改判定。

        Args:
            text: 嫌疑文本
            session_salt: 会话盐（有则做信道A验证 + 用原盐解码）
            seal: 信道 A 签名（有则验证篡改）
            language: 语言覆盖
            soft_match: 启用软判决注册库匹配（v0.7 鲁棒性增强）。
                True 时用逐带 z 打点积分对注册库候选直接打分（min_n=1，
                弱证据带参与），替代"解码 UID + 汉明最近邻"路径。
                需注册库非空；否则回退硬判决路径。软匹配结果只在水印
                存在性判定通过（watermarked）后采纳——soft_match 是候选
                区分器，不回答"是否嵌了水印"（null 文本也可能与某候选
                方向对齐）。
            match_margin: 软判决绝对置信阈值。最优与次优得分差 < margin
                时视为不可靠（uid=None）。在已嵌入（含受损）文本上
                实测 margin=2.0 可把温和攻击下的错误匹配全部转为 abstain。
            match_margin_ratio: 软判决自适应置信系数（v0.8）。gap 尺度
                随 √n_dict 增长，固定绝对 margin 对长文本偏松（50% 改写
                下错误 gap 仍超 2.0，"自信地错"）。给出时生效阈值
                max(match_margin, ratio·√n_dict)——短文本由绝对项主导、
                长文本由比例项主导。实测错误匹配 gap/√n_dict 上界跨语料
                稳定 ≈0.22，正确匹配均值 0.5~0.7，但重度攻击下分布重叠：
                ratio 是"宁可 abstain 也不错"的权衡旋钮（ratio=0.5 时
                s50/pku 错误清零，s30 召回 19→8）。None 时纯绝对阈值。
            bands: 嵌入时保存的带集元数据（自适应路径）。传入时走
                detect_adaptive/soft_match_adaptive（k-bit 空间）。
            n_bits: 嵌入时的编码位数（含冗余）。None 时用 len(bands)。

        Returns:
            TraceResult
        """
        lang = self._resolve_language(text, language)
        lang_tag = b"zh" if lang == "zh" else b"en"

        salt = session_salt or generate_session_salt()
        codec = self._build_codec(salt, lang_tag)
        adaptive = bands is not None

        # 信道 B 检测
        if adaptive:
            uid_dec, active, report = codec.detect_adaptive(text, bands)
            capacity = len(bands)
            eff_bits = n_bits if n_bits is not None else capacity
        else:
            report = codec.detect(text)
            uid_dec = report.uid
            active, capacity, eff_bits = [], 0, 0

        # 存在性判定：自适应阈值（自适应路径用带数线性模型）
        if adaptive:
            threshold = self._compute_threshold_adaptive(report)
        else:
            threshold = self._compute_threshold(report.n_dict_words)
        watermarked = report.existence_score >= threshold

        # UID 解码 + 注册库匹配
        uid = uid_dec if watermarked else None
        user = None
        hamming_dist = -1
        soft_uid: Optional[int] = None
        soft_gap = -1.0

        if self._registry is not None and len(self._registry) > 0:
            reg_uids = list(self._registry.list_all())
            if soft_match:
                # 软判决路径：直接对注册库候选打分（min_n=1，利用弱证据带）。
                # 注意：soft_match 是"候选区分器"，null 文本也可能与某候选
                # 方向对齐（z 随机游走）——存在性必须由 watermarked 门控。
                if adaptive:
                    # k-bit 空间：注册库 UID 按低 eff_bits 位映射
                    mask = (1 << eff_bits) - 1
                    k_cands = sorted({u & mask for u in reg_uids})
                    soft_uid, best_score, soft_gap = codec.soft_match_adaptive(
                        text, k_cands, bands,
                        min_n=1, margin=match_margin,
                        margin_ratio=match_margin_ratio)
                    if watermarked and soft_uid is not None:
                        uid = soft_uid
                        # k-bit → 注册库 16-bit UID（取低 n_bits 位匹配的注册项）
                        user = self._lookup_masked(soft_uid, reg_uids, mask)
                else:
                    soft_uid, best_score, soft_gap = codec.soft_match(
                        text, reg_uids,
                        min_n=1,
                        margin=match_margin,
                        margin_ratio=match_margin_ratio,
                    )
                    if watermarked and soft_uid is not None:
                        uid = soft_uid
                        user = self._registry.lookup(uid)
            elif watermarked and uid is not None:
                match = self._registry.nearest_match(
                    uid, max_hamming=self._thresholds.max_hamming)
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
            soft_uid=soft_uid,
            soft_gap=soft_gap,
            codec_mode=self._codec_mode if adaptive else "default",
            bands=list(bands) if bands else [],
            capacity=capacity,
            n_bits=eff_bits if adaptive else 0,
            active_bands=len(active),
        )

    def _lookup_masked(self, k_uid: int, reg_uids: List[int], mask: int) -> Optional[str]:
        """k-bit UID → 注册库用户（低 n_bits 位匹配；多位命中取最小 UID）。"""
        hits = [u for u in reg_uids if (u & mask) == k_uid]
        if not hits:
            return None
        return self._registry.lookup(min(hits))

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
        codec = self._build_codec(salt, lang_tag)
        report = codec.detect(text)
        threshold = self._compute_threshold(report.n_dict_words)
        return report.existence_score >= threshold

    def calibrate_p0(self, corpus: List[str], language: str = "en") -> None:
        """在无水印参考语料上标定 p0（提升检测精度）。

        部署时用一批真实无水印文本调一次即可。
        注意：zero_cost/hybrid 模式应通过构造参数 calibrate_corpus
        传入语料（构建时逐 codec 标定），本方法仅作用于 default 模式。
        """
        if self._codec_mode != "default" and language == "zh":
            # zero_cost/hybrid 的 p0 在 _build_codec 时标定
            self._calibrate_corpus = corpus
            return
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

    def _compute_threshold_adaptive(self, report: BandReport) -> float:
        """自适应路径（zero_cost/hybrid，min_n=1 统计）的存在性阈值。

        优先用 null 语料标定的每带 ratio 模型（阈值 = m × 阈值 ratio，
        m = 活动带数），未标定时用 DetectionThresholds 的默认线性常数。
        """
        m = sum(1 for st in report.bands if st.has_signal)
        if self._null_model is not None:
            _, thr_ratio = self._null_model
            return m * thr_ratio
        return (self._thresholds.adaptive_intercept
                + self._thresholds.adaptive_slope * m)
