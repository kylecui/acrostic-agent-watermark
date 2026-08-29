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
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
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
        margin_ratio: 自检存在性余量 = existence_score / 阈值（自适应模式；
            default 模式=0）。<1.5 表示信号贴近阈值，检出可靠性低。
        weak_embed: 弱嵌入标志（自适应模式）。自检未达 1.5×阈值标准时置
            True——embed 已尽力（换盐/换 rng 4 次），但文本信号不足，
            事后 trace 可能漏检或归因 abstain，调用方应据此警告用户。
        reliability: 溯源可靠性分级（v0.12）。综合容量与自检余量给出
            "high"/"medium"/"low"：
            - high   容量 k>=10 且余量达标——窗口内检出与归因均可靠
            - medium 6<=k<10——存在性检出通常存活，但 UID 归因可能失败
            - low    k<6 或 weak_embed——文本过短/信号贴近阈值，事后
                     trace 可能漏检或归因 abstain；建议加长文本或聚合
                     多条输出后再嵌入
            default 模式（容量不足会硬报错）嵌入成功即 "high"。
        key_version: 嵌入所用密钥版本（v0.13 P1-6）。meta 存档；
            trace 时传入以用对应版本密钥解码——密钥轮换不破坏历史溯源。
        dict_version: 词典指纹（v0.13 P2-9）。trace 重建 codec 后比对，
            mismatch 说明词典已变更（带映射失效，会漏检）。
        uid_layout: UID 冗余布局（v0.13 P2-8，uid_redundancy>1 时非空）：
            layout[bit] = 编码该位的带列表。trace 时传入走冗余解码。
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
    margin_ratio: float = 0.0
    weak_embed: bool = False
    reliability: str = "high"
    key_version: int = 1
    dict_version: str = ""
    uid_layout: List[List[int]] = field(default_factory=list)


@dataclass
class TraceResult:
    """溯源结果。

    Attributes:
        watermarked: 是否检出水印（存在性判定）
        uid: 解码出的 UID（int），未检出/低置信时为 None
        user: 注册库匹配到的用户别名；None=无匹配或无注册库
        hamming_dist: 与最近邻 UID 的汉明距（-1=无匹配）
        confidence: 存在性置信度 [0,1]，基于 existence_score 归一化。
            只反映"信号多强"，不反映"UID 解对没有"（对抗场景可能
            存在性存活但归因错误）——归因可靠性看 attribution_confidence。
        attribution_confidence: 归因置信度 [0,1]（v0.10）。综合判别力
            （soft 路径 gap/√n_dict、硬路径汉明距）与容量充分性
            （自适应 k-bit 空间相对候选数）。低于 attribution_floor
            （默认 0.5）时 attribution_abstain=True 且 uid/user 置 None。
        attribution_abstain: 检出水印但归因置信不足（True=uid/user 被
            置 None，输出"不可判定"而非可能的错误用户）。
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
        key_version: 实际使用的密钥版本（v0.13 P1-6；trace(key_version=...)
            或 active 版本）
        dict_version: 本次 trace 重建 codec 的词典指纹（v0.13 P2-9）
        dict_version_match: 与传入 dict_version（meta 存档）的比对结果。
            None=未传存档指纹无从比对；False=词典已变更，带映射失效
            （本次结果可能漏检/失真，应排查词典版本）
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
    attribution_confidence: float = 0.0
    attribution_abstain: bool = False
    key_version: int = 1
    dict_version: str = ""
    dict_version_match: Optional[bool] = None


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
    # 归因置信度（v0.10）判定参数。对抗场景最危险失败模式是"高置信度
    # 错误归因"——存在性存活但 UID 解错，仍输出错误用户结论。因此
    # attribution_confidence 独立于存在性 confidence，专门编码
    # "归因结果有多大可能对"，不足时 abstain（uid=None）。
    # 判别力标定锚点来自 exp_margin_scale/exp_margin_ratio 跨语料实测：
    #   错误匹配 gap/√n_dict 上界 ≈ 0.22，正确匹配均值 0.5~0.7
    #   （短文本/稀疏命中的干净匹配可低至 ~0.35，故 ok_lo 取 0.4——
    #   保证 margin 门限(0.3)之上仍有归因余量）
    attribution_floor: float = 0.5    # AC < 此值 → abstain（uid=None）
    gap_error_hi: float = 0.22        # gap/√n_dict ≤ 此值视为错误区间
    gap_ok_lo: float = 0.4            # gap/√n_dict ≥ 此值视为可靠区间
    capacity_full_width: int = 16     # 自适应 k-bit 空间相对全宽 UID 的参考宽度
    hard_no_cands_cap: float = 0.5    # 无候选对比（无注册库）时的判别力上限


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
        codec_mode: str = "zero_cost",
        supplementary_dict: Optional[Dict[str, List[str]]] = None,
        calibrate_corpus: Optional[List[str]] = None,
        calibration: Optional[Dict[str, Any]] = None,
    ) -> "Watermarker":
        """Args:
            codec_mode: codec 模式（中英双语生效；v0.9 起英文也有零感词典）。
                "zero_cost"— 零感词典（中文 136 组 / 英文拼写变体+副词+安全对，
                              默认，推荐）
                "default"  — 全词林 GreenlistCodec（旧行为，病句率高，
                              不推荐；显式传入以兼容旧部署）
                "hybrid"   — 零感打底 + supplementary_dict 补带
            supplementary_dict: hybrid 模式的补充词典 {组名: [词列表]}
            calibrate_corpus: 无水印参考语料，构建 codec 时标定 p0
                并现场拟合 null 存在性阈值模型（每次构造都要重新拟合，
                大语料时慢——改用 calibration 文件一次性拟合复用）
            calibration: 标定文件（`aawm calibrate` 产出的 JSON，
                或 `export_calibration()` 的返回值）。传 dict 或 JSON
                文件路径均可。装载已拟合的 null 阈值模型，免去运行时
                拟合。与 calibrate_corpus 同时给出时 corpus 现场拟合
                优先（p0 也会被标定，更准）
        """
        if isinstance(calibration, (str, os.PathLike)):
            # 便捷：直接传标定文件路径（与 CLI --calibration 一致）
            calibration = json.loads(
                Path(calibration).read_text(encoding="utf-8"))
        # 密钥
        if keystore is not None:
            self._keystore = keystore
        elif master_key is not None:
            self._keystore = KeyStore(master_key if isinstance(master_key, bytes)
                                       else bytes.fromhex(master_key))
        else:
            self._keystore = KeyStore()  # 随机生成

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
        # {lang_tag: (每带均值 ratio, 阈值 ratio)} — Σ|z|/m ≈ ratio·N(μ, σ)
        # 阈值 = m × (μ + 3σ)。m = 活动带数。
        # 仅当提供 calibrate_corpus 且非 default 模式时按语言分别计算
        self._null_model: Dict[bytes, Tuple[float, float]] = {}
        # 标定文件的词典词频表（p0 跨盐重算用）：{lang_tag: {词: 次数}}
        # 须在 _fit_null_model 之前初始化（_build_codec 会读它）
        self._p0_vocab: Dict[bytes, Dict[str, int]] = {}
        if calibrate_corpus and codec_mode != "default":
            self._fit_null_model(calibrate_corpus, b"zh")
            self._fit_null_model(calibrate_corpus, b"en")
        if calibration and not calibrate_corpus:
            self._load_calibration(calibration)

    @property
    def _master_key(self) -> bytes:
        """当前 active 密钥（v0.13：实时从 keystore 取——rotate 后不留
        陈旧引用；trace 的旧版本密钥走 _build_codec/master_key 覆盖）。"""
        return self._keystore.get()

    def _fit_null_model(self, corpus: List[str], lang_tag: bytes) -> None:
        """在 null 语料上拟合存在性阈值模型（自适应路径专用，按语言）。

        每带归一化 ratio 模型：对每篇 null 文本用多个不同 salt 的
        codec 检测（min_n=1），收集每带平均得分 r = Σ|z|/m（m=活动带数），
        阈值 ratio = μ_r + 2.5·σ_r，判定阈值 = m × 阈值 ratio。

        两个关键点：
        - 多 salt 采样：绿名单颜色随 salt 重排，null 得分的 salt 间
          方差远大于语料间残差——单 salt 拟合严重低估 σ。
        - ratio 归一化：每带均值跨 m 稳定，避免线性回归在标定语料
          m 散布窄时外推失稳（实测线性模型 FP 1/30~13/30，
          ratio 模型 0/30 且 marked/null ratio 分离 4 倍以上）。
        - 语言独立：中英文 codec 词典不同，null 得分分布不同，须按
          语言_tag 分别拟合（语料只有单一语言时另一语言条目保持空）。
        """
        ratios: List[float] = []
        # 盐采样自适应扩展：稀疏语料（词典命中少）单轮 5 盐可能凑不满
        # 3 个有效 ratio（每盐期望命中 <1.4 时 P(<3)≈3%，CI/生产偶发
        # 标定静默失败）。扩展到最多 25 盐，凑满 10 个 ratio 即停。
        max_salts = 25
        for _ in range(max_salts):  # 5 个盐起步：σ 覆盖 salt 间方差
            codec = self._build_codec(generate_session_salt(), lang_tag)
            for t in corpus:
                rep = codec.detect(t, min_n=1)
                m = sum(1 for st in rep.bands if st.has_signal)
                if m > 0:
                    ratios.append(rep.existence_score / m)
            if len(ratios) >= 10 and _ >= 4:
                break  # 常规语料 5 盐即够，避免多余开销
        if len(ratios) < 3:
            return
        n = len(ratios)
        mu = sum(ratios) / n
        sd = (sum((r - mu) ** 2 for r in ratios) / n) ** 0.5
        if sd < 1e-9:
            sd = 0.1  # 完全同质语料：给保守余量
        # 3σ（非 2.5σ）：标定语料有限时 σ 本身仍是低估的，
        # 且 marked/null ratio 分离 4 倍以上，宽阈值无漏检代价
        self._null_model[lang_tag] = (mu, mu + 3.0 * sd)

    def calibrate_null_model(self, corpus: List[str]) -> None:
        """在 null 语料上拟合存在性阈值模型 + 词典词频表，供导出复用。

        产出两部分（export_calibration 导出）：
        1. null 阈值模型（μ/σ ratio，盐平均后盐无关）
        2. 词典词频表 p0_vocab——p0 逐盐重算的关键：green(词) 随盐变，
           词频不变，标定文件消费方在任意盐下重算即得与语料原文
           数学等价的 p0（实测 docs 语料仅 ~900 字节）

        拟合管线与"只持标定文件"的运行时完全一致（codec 先用词频表
        标定 p0，再检测 null 语料），消费方无一致性坑。

        语料要求：几十篇同领域无水印文本（≥3 篇即可拟合，但样本少
        时阈值保守度不足）。
        """
        if self._codec_mode == "default":
            raise ValueError("default 模式无自适应路径，无需 null 标定")
        # 1. 词频表（先建，拟合管线即运行时管线；清空防陈旧条目）
        self._p0_vocab = {}
        for tag in (b"zh", b"en"):
            codec = self._build_codec(generate_session_salt(), tag)
            self._p0_vocab[tag] = codec.dict_word_counts(corpus)
        # 2. null 阈值模型（_build_codec 此时已带词频表 p0）
        self._fit_null_model(corpus, b"zh")
        self._fit_null_model(corpus, b"en")

    def export_calibration(self) -> Dict[str, Any]:
        """导出当前标定为可 JSON 序列化的标定文件结构。

        配合 calibrate_null_model 使用：
            wm.calibrate_null_model(corpus)
            json.dump(wm.export_calibration(), open("calibration.json", "w"))
        embed/trace 侧用 Watermarker(calibration=...) 装载，或 CLI
        --calibration calibration.json。大语料一次拟合、处处复用。
        """
        null_model = {}
        for tag in (b"zh", b"en"):
            nm = self._null_model.get(tag)
            if nm:
                null_model[tag.decode()] = {
                    "mu": nm[0],
                    "threshold_ratio": nm[1],
                }
        p0_vocab = {
            tag.decode(): counts
            for tag, counts in self._p0_vocab.items() if counts
        }
        return {
            "version": 1,
            "codec_mode": self._codec_mode,
            "null_model": null_model,
            "p0_vocab": p0_vocab,
        }

    def _load_calibration(self, calibration: Dict[str, Any]) -> None:
        """装载 aawm calibrate / export_calibration 产出的标定文件。"""
        if not isinstance(calibration, dict) or "null_model" not in calibration:
            raise ValueError(
                "calibration 结构不合法：应为 aawm calibrate 产出的 JSON "
                "（含 null_model 字段）")
        file_mode = calibration.get("codec_mode")
        if file_mode and file_mode != self._codec_mode:
            import warnings
            warnings.warn(
                f"标定文件的 codec_mode={file_mode!r} 与当前 "
                f"{self._codec_mode!r} 不一致，阈值模型可能失准")
        for lang, entry in calibration["null_model"].items():
            try:
                self._null_model[lang.encode()] = (
                    float(entry["mu"]), float(entry["threshold_ratio"]))
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(f"calibration 语言条目 {lang!r} 不合法: {e}")
        vocab = calibration.get("p0_vocab") or {}
        for lang, counts in vocab.items():
            if not isinstance(counts, dict):
                raise ValueError(f"calibration p0_vocab[{lang!r}] 应为词频表 dict")
            self._p0_vocab[lang.encode()] = {
                str(w): int(c) for w, c in counts.items()}

    # ------------------------------------------------------------------
    # codec 构建
    # ------------------------------------------------------------------

    def _build_codec(
        self,
        session_salt: bytes,
        lang_tag: bytes,
        master_key: Optional[bytes] = None,
    ) -> GreenlistCodec:
        """按 codec_mode 与语言构建 codec。

        default 模式 → 默认词典词林（中/英各自，向后兼容）；
        zero_cost + 中文 → 中文零感词典；zero_cost + 英文 → 英文零感
        词典（拼写变体 + 功能副词）；hybrid → 零感打底 + 补充词典。

        master_key：密钥覆盖（v0.13 P1-6）——trace 旧水印时传
        keystore.get_version(key_version)，缺省用 active 密钥。
        """
        key = master_key if master_key is not None else self._master_key
        if self._codec_mode == "default":
            return GreenlistCodec(key, session_salt,
                                  language_tag=lang_tag)
        if lang_tag == b"zh":
            if self._codec_mode == "zero_cost":
                from ..greenlist import build_zero_cost_zh_codec
                codec = build_zero_cost_zh_codec(
                    key, session_salt,
                    calibrate_corpus=self._calibrate_corpus)
                return self._apply_p0_vocab(codec, lang_tag)
            # hybrid
            if self._supplementary_dict is None:
                raise ValueError("hybrid 模式需要 supplementary_dict")
            from ..greenlist import build_hybrid_zh_codec
            codec = build_hybrid_zh_codec(
                key, session_salt,
                supplementary_dict=self._supplementary_dict,
                calibrate_corpus=self._calibrate_corpus)
            return self._apply_p0_vocab(codec, lang_tag)
        # 英文 + zero_cost/hybrid → 英文零感词典路径
        if self._codec_mode == "hybrid":
            if self._supplementary_dict is None:
                raise ValueError("hybrid 模式需要 supplementary_dict")
            from ..greenlist import build_hybrid_en_codec
            codec = build_hybrid_en_codec(
                key, session_salt,
                supplementary_dict=self._supplementary_dict,
                calibrate_corpus=self._calibrate_corpus)
            return self._apply_p0_vocab(codec, lang_tag)
        from ..greenlist import build_zero_cost_en_codec
        codec = build_zero_cost_en_codec(
            key, session_salt,
            calibrate_corpus=self._calibrate_corpus)
        return self._apply_p0_vocab(codec, lang_tag)

    def _apply_p0_vocab(self, codec: GreenlistCodec, lang_tag: bytes) -> GreenlistCodec:
        """标定文件的词频表 → 当前盐下的精确 p0（calibrate_corpus 缺席时）。

        corpus 在场时 builder 已做 p0 标定（更准，语料原文全信息），
        词频表仅在"只持标定文件"的消费方生效——两者数学等价。
        """
        vocab = self._p0_vocab.get(lang_tag)
        if vocab and not self._calibrate_corpus:
            codec.calibrate_p0_from_counts(vocab)
        return codec

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        key_file: Optional[str] = None,
        registry_file: Optional[str] = None,
        language: str = "auto",
        codec_mode: str = "zero_cost",
        supplementary_dict: Optional[Dict[str, List[str]]] = None,
        calibrate_corpus: Optional[List[str]] = None,
        calibration: Optional[Dict[str, Any]] = None,
    ) -> "Watermarker":
        """从配置文件创建（便捷方法）。

        Args:
            key_file: 密钥文件路径（不存在则自动创建）
            registry_file: 注册库文件路径（None=纯内存）
            language: 默认语言 "en"/"zh"/"auto"
            codec_mode: codec 模式（default/zero_cost/hybrid，默认 zero_cost，
                与 __init__ 默认一致——此前 from_config 默认 default 会静默
                走词林，用户以为拿到零感）
            supplementary_dict: hybrid 模式补充词典
            calibrate_corpus: p0 标定语料
            calibration: null 阈值标定（aawm calibrate 产出的 JSON；
                传 dict 或 JSON 文件路径均可——大语料用它免去每次构造
                重新拟合）
        """
        ks = KeyStore.from_file(key_file, create=True) if key_file else KeyStore()
        reg = UIDRegistry(backend="file", path=registry_file) if registry_file else UIDRegistry()
        return cls(keystore=ks, registry=reg, language=language,
                   codec_mode=codec_mode, supplementary_dict=supplementary_dict,
                   calibrate_corpus=calibrate_corpus, calibration=calibration)

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
        uid_redundancy: int = 1,
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
            uid_redundancy: UID 冗余份数 r（v0.13 P2-8，默认 1=无冗余）。
                r>1 时同一 UID 位由 r 个交错带共同编码（layout 见
                EmbedResult.uid_layout），段落删除/裁剪攻击下 UID 归因
                存活率显著提升（crop50 实测 1-3/5 → 5/5）。代价：UID
                位空间缩小 r 倍（容量 k → floor(k/r) 位）。与 n_bits
                同时给出时 n_bits 是冗余后的 UID 位宽（需 k >= n_bits*r）。

        自适应模式（zero_cost/hybrid，中英文一致）注意：
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

        # 4. 信道 B 嵌入（zero_cost/hybrid → 自适应路径，中英文一致）
        codec = self._build_codec(session_salt, lang_tag)
        adaptive = self._codec_mode != "default"

        # v0.13 P2-8：UID 冗余只在自适应路径实现；default 模式静默
        # 忽略会让调用方以为有冗余保护——显式拒绝（fail-fast）。
        if uid_redundancy > 1 and not adaptive:
            raise ValueError(
                "uid_redundancy 仅支持 zero_cost/hybrid 模式"
                "（default 全词林路径无自适应容量，未实现冗余）")

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
            # (honor, uid_ok, margin, marked, bands, report, salt, codec, eff_bits, k, layout)
            # honor = 满足请求的 n_bits（显式请求时要求 k >= n_bits）——
            # 换盐重试会让容量缩水（如 15→11），若直接按满容量钳位会
            # 悄悄吞掉用户要的冗余带。显式 n_bits 下优先选能兑现的盐。
            # 冗余模式（uid_redundancy>1）下 k_uid = k // r 是 UID 位宽，
            # n_bits 语义相应变为冗余后的位宽。
            redundant = uid_redundancy > 1
            best = None
            for attempt in range(max_attempts):
                k = codec.capacity(text)
                if redundant:
                    k_uid = k // uid_redundancy
                    honor = n_bits is None or k_uid >= n_bits
                    eff_bits = n_bits if (honor and n_bits is not None) else k_uid
                    uid_eff = uid & ((1 << eff_bits) - 1) if eff_bits < 16 else uid
                    marked, layout = codec.embed_redundant(
                        text, uid_eff, r=uid_redundancy, n_bits=eff_bits,
                        bias=bias, rng=rng)
                    uid_chk, report = codec.detect_redundant(
                        marked, layout, min_n=1)
                    bands = [b for bl in layout for b in bl]
                else:
                    honor = n_bits is None or k >= n_bits
                    eff_bits = n_bits if (honor and n_bits is not None) else k
                    uid_eff = uid & ((1 << eff_bits) - 1) if eff_bits < 16 else uid
                    marked, bands = codec.embed_adaptive(
                        text, uid_eff, n_bits=eff_bits, bias=bias, rng=rng)
                    uid_chk, _, report = codec.detect_adaptive(marked, bands, min_n=1)
                    layout = []
                threshold = self._compute_threshold_adaptive(report, lang_tag)
                margin = report.existence_score / threshold if threshold > 0 else float("inf")
                uid_ok = uid_chk == uid_eff
                cand = (honor, uid_ok, margin, marked, bands, report,
                        session_salt, codec, eff_bits, k, layout)
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
            (_, _, best_margin, marked, bands, report, session_salt,
             best_codec, eff_bits, k, layout) = best
        else:
            marked = codec.embed(text, uid, bias=bias, rng=rng)
            report = codec.detect(marked)
            bands, eff_bits, k, layout = [], 0, 0, []
            best_margin = float("inf")  # default 模式容量不足会硬报错，无弱嵌入概念

        # 6. 弱嵌入判定：自适应自检标准是 margin >= 1.5（见上）；换盐/换 rng
        #    max_attempts 次仍不达标时静默返回余量最大的一次——这里显式暴露。
        weak_embed = adaptive and best_margin < 1.5

        # 6.5 溯源可靠性分级（v0.12）：容量 + 余量 → high/medium/low。
        #     略短文本不拒绝嵌入（存在性检测仍有价值），但明确标注
        #     可靠性降低，调用方据此决定加长文本或聚合后再嵌。
        reliability = ("high" if not adaptive
                       else self.reliability_tier(k, weak_embed))

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
            margin_ratio=best_margin if adaptive else 0.0,
            weak_embed=weak_embed,
            reliability=reliability,
            key_version=self._keystore.active_version,
            dict_version=codec.dict_version,
            uid_layout=[list(bl) for bl in layout] if layout else [],
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
        soft_match: bool = True,
        match_margin: float = 2.0,
        match_margin_ratio: Optional[float] = 0.3,
        bands: Optional[List[int]] = None,
        n_bits: Optional[int] = None,
        archived_uid: Optional[int] = None,
        key_version: Optional[int] = None,
        uid_layout: Optional[List[List[int]]] = None,
        dict_version: Optional[str] = None,
    ) -> TraceResult:
        """溯源：存在性检测 + UID 解码 + 注册库匹配 + 篡改判定。

        Args:
            text: 嫌疑文本
            session_salt: 会话盐（有则做信道A验证 + 用原盐解码）
            seal: 信道 A 签名（有则验证篡改）
            language: 语言覆盖
            soft_match: 启用软判决注册库匹配（v0.10 起默认 True）。
                True 时用逐带 z 打点积分对注册库候选直接打分（min_n=1，
                弱证据带参与），替代"解码 UID + 汉明最近邻"路径。
                需注册库非空；否则回退硬判决路径。软匹配结果只在水印
                存在性判定通过（watermarked）后采纳——soft_match 是候选
                区分器，不回答"是否嵌了水印"（null 文本也可能与某候选
                方向对齐）。
            match_margin: 软判决绝对置信阈值。最优与次优得分差 < margin
                时视为不可靠（soft_uid=None）。在已嵌入（含受损）文本上
                实测 margin=2.0 可把温和攻击下的错误匹配全部转为 abstain。
            match_margin_ratio: 软判决自适应置信系数（v0.8；v0.10 起默认
                0.3）。gap 尺度随 √n_dict 增长，固定绝对 margin 对长文本
                偏松（50% 改写下错误 gap 仍超 2.0，"自信地错"）。给出时
                生效阈值 max(match_margin, ratio·√n_dict)——短文本由绝对项
                主导、长文本由比例项主导。实测错误匹配 gap/√n_dict 上界
                跨语料稳定 ≈0.22，正确匹配均值 0.5~0.7，但重度攻击下分布
                重叠：ratio 是"宁可 abstain 也不错"的权衡旋钮。默认 0.3
                高于错误上界（压错误）、低于正确均值（保召回）。None 时
                纯绝对阈值（v0.7 兼容）。
            bands: 嵌入时保存的带集元数据（自适应路径）。传入时走
                detect_adaptive/soft_match_adaptive（k-bit 空间）。
            n_bits: 嵌入时的编码位数（含冗余）。None 时用 len(bands)。
            archived_uid: 盐外证据（v0.10+）：嵌入时存档的 UID（meta 的
                user_id/uid 字段，嵌入时真值）。多盐扫描/档案扫描场景
                下调用方持 meta 时应传入：解码 UID 与存档 UID 不一致即
                视为失真 → abstain（uid=None，绝不输出可能错误的 UID）。
                消除防御的路径依赖——此前只有 CLI/find-meta（持 meta
                路径）做此校验，直接调本 API 的消费者无保护。
            key_version: 嵌入时的密钥版本（v0.13 P1-6，meta 的
                key_version 字段）。密钥轮换后旧水印按版本取对应密钥
                解码——不传则用 active 版本（轮换前嵌入的水印会漏检）。
            uid_layout: 嵌入时的冗余布局（v0.13 P2-8，meta 的
                uid_layout 字段）。传入时走冗余解码/软匹配（同一位
                多带投票）；不传则按 bands 普通自适应路径。
            dict_version: 嵌入时的词典指纹（v0.13 P2-9，meta 的
                dict_version 字段）。传入时与本次重建 codec 的指纹
                比对，结果记录在 TraceResult.dict_version_match
                （False=词典已变更，带映射失效，本次结果可能漏检）。

        Returns:
            TraceResult
        """
        lang = self._resolve_language(text, language)
        lang_tag = b"zh" if lang == "zh" else b"en"

        salt = session_salt or generate_session_salt()
        # v0.13 P1-6：按 key_version 取密钥（轮换后旧水印仍可溯源）
        if key_version is not None:
            if key_version not in self._keystore.versions():
                raise KeyError(
                    f"密钥版本 {key_version} 不在 keystore "
                    f"（现有版本：{self._keystore.versions()}）")
            eff_key = self._keystore.get_version(key_version)
        else:
            eff_key = self._master_key
        used_key_version = (
            key_version if key_version is not None
            else self._keystore.active_version)
        codec = self._build_codec(salt, lang_tag, master_key=eff_key)
        # v0.13 P2-9：词典指纹比对（meta 存档 vs 本次重建 codec）
        dict_version_match = (
            codec.dict_version == dict_version
            if dict_version is not None else None)
        adaptive = bands is not None or uid_layout is not None

        # 信道 B 检测
        if uid_layout is not None:
            # 冗余路径（v0.13 P2-8）：按 layout 聚合多带投票
            uid_dec, report = codec.detect_redundant(text, uid_layout)
            active_set = {st.band for st in report.bands if st.has_signal}
            layout_flat = list(dict.fromkeys(
                b for bl in uid_layout for b in bl))
            active = [b for b in layout_flat if b in active_set]
            capacity = len(bands) if bands is not None else len(layout_flat)
            eff_bits = n_bits if n_bits is not None else len(uid_layout)
        elif adaptive:
            uid_dec, active, report = codec.detect_adaptive(text, bands)
            capacity = len(bands)
            eff_bits = n_bits if n_bits is not None else capacity
        else:
            report = codec.detect(text)
            uid_dec = report.uid
            active, capacity, eff_bits = [], 0, 0
            # 兜底：有正确盐但缺 bands 元数据（adaptive 嵌入、meta 散失）
            # 时非自适应路径会漏检——用文本自身活动带重试自适应检测。
            # 注：嵌入若留了冗余带（n_bits<容量），UID 位空间不同，
            # 解码可能失真——始终优先用存档 meta。
            if (session_salt is not None
                    and report.existence_score
                    < self._compute_threshold(report.n_dict_words)):
                uid_ad, active_ad, rep_ad = codec.detect_adaptive(text, None)
                # active_ad 非空才采纳兜底：null 文本可整句零词典词，
                # detect_adaptive 返回的空带报告 existence=0、m=0，
                # 阈值退化 0 会误报（见 _compute_threshold_adaptive 的 m=0 守卫）。
                if active_ad and rep_ad.existence_score >= \
                        self._compute_threshold_adaptive(rep_ad, lang_tag):
                    adaptive = True
                    uid_dec, active, report = uid_ad, active_ad, rep_ad
                    capacity = len(active_ad)
                    eff_bits = n_bits if n_bits is not None else capacity

        # 存在性判定：自适应阈值（自适应路径用带数线性模型）
        if adaptive:
            threshold = self._compute_threshold_adaptive(report, lang_tag)
        else:
            threshold = self._compute_threshold(report.n_dict_words)
        watermarked = report.existence_score >= threshold

        # UID 解码 + 注册库匹配
        uid = uid_dec if watermarked else None
        user = None
        hamming_dist = -1
        soft_uid: Optional[int] = None
        soft_gap = -1.0
        reg_uids: List[int] = (
            list(self._registry.list_all())
            if self._registry is not None else [])
        soft_engaged = bool(reg_uids) and soft_match

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
                    if uid_layout is not None:
                        # 冗余路径（v0.13 P2-8）：按 layout 多带投票打分
                        soft_uid, best_score, soft_gap = codec.soft_match_redundant(
                            text, k_cands, uid_layout,
                            min_n=1, margin=match_margin,
                            margin_ratio=match_margin_ratio)
                    else:
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
                # 软判决 margin 拒绝（soft_uid=None）：不再回退硬解码 uid。
                # 攻击下存在性常存活但解码不可靠，硬解码恰恰是"自信地错"
                # 的来源（VERIFICATION_REPORT 4.2/4.4）——宁可不判定。
                if watermarked and soft_uid is None:
                    uid = None
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

        # 存在性置信度（只反映信号强度，不反映归因可靠性）
        confidence = min(1.0, report.existence_score / self._thresholds.confidence_scale)

        # 归因置信度（v0.10）：判别力 × 容量充分性。低于门槛 → abstain。
        # 对抗场景"高置信度错误归因"的根治：存在性存活但归因不可靠时，
        # 输出"不可判定"而非一个可能错误的具体用户。
        attribution_confidence = self._compute_attribution_confidence(
            watermarked=watermarked,
            soft_engaged=soft_engaged,
            soft_uid=soft_uid,
            soft_gap=soft_gap,
            n_dict_words=report.n_dict_words,
            hamming_dist=hamming_dist,
            eff_bits=eff_bits,
            adaptive=adaptive,
            reg_uids=reg_uids,
        )
        attribution_abstain = False
        if watermarked and attribution_confidence < self._thresholds.attribution_floor:
            attribution_abstain = True
            uid = None
            user = None
            hamming_dist = -1

        # 盐外证据（v0.10+）：meta 存档 UID 与解码 UID 交叉校验。
        # 攻击下存在性常存活但 UID 解码失真（"自信地错"），存档 UID 是
        # 嵌入时的真值——不一致即视为失真，宁可 abstain 也不输出可能
        # 错误的 UID。裸 API 消费者持 meta 时必须显式传 archived_uid，
        # 否则与 CLI/find-meta 的防御存在路径依赖差（存档 UID 校验
        # 只发生在"持有 meta"的路径上）。
        if (watermarked and archived_uid is not None and uid is not None
                and not self._uid_alias_match(uid, archived_uid, eff_bits)):
            attribution_abstain = True
            uid = None
            user = None
            hamming_dist = -1
            attribution_confidence = 0.0

        # 信道 A 篡改判定
        tampered = None
        tampered_paras: List[int] = []
        if seal is not None and session_salt is not None:
            binder = DocumentBinder(eff_key, session_salt)
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
            attribution_confidence=attribution_confidence,
            attribution_abstain=attribution_abstain,
            key_version=used_key_version,
            dict_version=codec.dict_version,
            dict_version_match=dict_version_match,
        )

    def _compute_attribution_confidence(
        self,
        *,
        watermarked: bool,
        soft_engaged: bool,
        soft_uid: Optional[int],
        soft_gap: float,
        n_dict_words: int,
        hamming_dist: int,
        eff_bits: int,
        adaptive: bool,
        reg_uids: List[int],
    ) -> float:
        """归因置信度 [0,1]（v0.10）。

        对抗场景的失败模式是存在性存活但 UID 解错（"自信地错"），
        故本分数与存在性 confidence 独立，只回答"归因有多大可能对"：

        1. 判别力项 disc [0,1]：
           - 软判决路径（默认）：margin 门限已拒绝（soft_uid=None）时
             归因判定为不可靠，disc=0（门限是权威信号，不因 gap_ratio
             大而翻盘——match_margin_ratio 拉满时 gap 再大也不可信）。
             门限放行时用 gap/√n_dict 线性映射。标定锚点来自
             exp_margin_scale/exp_margin_ratio 跨语料实测——错误匹配
             gap/√n_dict 上界 ≈0.22，正确匹配均值 0.5~0.7（稀疏命中
             的干净匹配可低至 ~0.35，故 ok_lo=0.4，与 margin 门限
             0.3 之间保留归因余量）。gap_ratio ≤ error_hi 得 0，
             ≥ ok_lo 得 1。
           - 硬判决路径（显式 soft_match=False + 注册库）：按汉明距
             线性映射（dist=0→1，dist≥max_hamming→0）。
           - 无候选对比（无注册库）：无从分辨单次解码的对错，给诚实
             上限 hard_no_cands_cap（默认 0.5，恰好踩在 abstain 门槛
             下缘——存在性强时保留 uid，弱时 abstain）。
        2. 容量项 cap {0,1}：仅自适应 k-bit 空间有意义。注册库 UID
           mask 到低 eff_bits 位后若有碰撞（两用户低 k 位相同），二者
           产生的水印在 k-bit 空间不可区分，归因数学上不可能对——
           cap=0 直接 abstain（如 n_bits=2 下 UID 1 与 5 都 mask 成 1，
           解出 1≠5 的"自信地错"正是此因）。无碰撞取 1。
           非自适应路径无 k-bit 截断，恒为 1（中性）。

        总分 = disc × cap。低于 attribution_floor（默认 0.5）时
        trace 置 attribution_abstain=True 且 uid/user=None。
        """
        t = self._thresholds
        if not watermarked:
            return 0.0

        # 1. 判别力项
        if soft_engaged:
            if soft_uid is None:
                disc = 0.0  # margin 门限拒绝 → 归因不可靠
            else:
                gap_ratio = soft_gap / max(1.0, math.sqrt(max(1, n_dict_words)))
                disc = min(1.0, max(
                    0.0,
                    (gap_ratio - t.gap_error_hi) / (t.gap_ok_lo - t.gap_error_hi)))
        elif hamming_dist >= 0:
            disc = min(1.0, max(0.0, 1.0 - hamming_dist / max(1, t.max_hamming)))
        else:
            disc = t.hard_no_cands_cap

        # 2. 容量充分性（仅自适应 k-bit 空间）
        cap = 1.0
        if adaptive and eff_bits > 0 and reg_uids:
            mask = (1 << eff_bits) - 1
            if len({u & mask for u in reg_uids}) < len(reg_uids):
                cap = 0.0  # 低 k 位碰撞 → k-bit 空间内用户不可区分

        return disc * cap

    def _lookup_masked(self, k_uid: int, reg_uids: List[int], mask: int) -> Optional[str]:
        """k-bit UID → 注册库用户（低 n_bits 位匹配；多位命中取最小 UID）。"""
        hits = [u for u in reg_uids if (u & mask) == k_uid]
        if not hits:
            return None
        return self._registry.lookup(min(hits))

    @staticmethod
    def _uid_alias_match(uid: Optional[int], archived_uid: Optional[Any],
                         n_bits: int) -> bool:
        """解码 UID 与 meta 存档 UID 是否一致（盐外证据，含自适应 k-bit 掩码对齐）。

        语义与 cli._uid_alias_match 保持一致：相等，或按 n_bits 掩码
        ``uid == (archived_uid & mask)`` 视为一致（自适应 k-bit 空间下
        UID 实际编码在低 n_bits 位，解码值是该位空间内的值）。

        Args:
            uid: 解码出的 UID（None=未归因，无法比对）
            archived_uid: 嵌入时存档的 UID（meta 真值；可为 int 或数字串）
            n_bits: 编码位数（非自适应路径传 0 → 仅精确相等算一致）
        """
        if uid is None or archived_uid is None:
            return False
        try:
            auid = int(archived_uid)
        except (TypeError, ValueError):
            return False
        if uid == auid:
            return True
        mask = (1 << n_bits) - 1 if n_bits else None
        return bool(mask and uid == (auid & mask))

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def estimate_capacity(
        self,
        text: str,
        *,
        language: Optional[str] = None,
        session_salt: Optional[bytes] = None,
    ) -> int:
        """嵌入前容量预检：估算文本的有效容量 k（活动带数）。

        不做嵌入，构建 codec 估算。容量随盐有小幅波动（活动带集
        不同）：默认随机盐返回单次采样值；传 session_salt 可复现
        embed(text, session_salt=...) 的确切容量（同盐同文本确定性）。
        k>=10 通常可支撑高可靠溯源，k<6 的事后归因大概率失败
        （见 EmbedResult.reliability）。

        Args:
            text: 待嵌入文本
            language: 语言覆盖（默认自动检测）
            session_salt: 指定盐（默认随机采样一次）
        """
        lang = self._resolve_language(text, language)
        lang_tag = b"zh" if lang == "zh" else b"en"
        codec = self._build_codec(
            session_salt or generate_session_salt(), lang_tag)
        return codec.capacity(text)

    @staticmethod
    def reliability_tier(capacity: int, weak_embed: bool) -> str:
        """容量 + 自检余量 → 溯源可靠性分级。

        分级锚点来自五轮外部验证实测（VERIFICATION_REPORT §4.1/§9.2）：
        k>=10（中文标定后约 1200 字）检出与归因双高；6<=k<10 检出
        常存活但归因可能失败（800 字：检出 11/12、uid 9-10/12）；
        k<6 检出与归因都不可靠（400 字：uid 5-7/12）。
        weak_embed（余量<1.5）无论容量如何都降为 low。
        """
        if weak_embed:
            return "low"
        if capacity >= 10:
            return "high"
        if capacity >= 6:
            return "medium"
        return "low"

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

    def _compute_threshold_adaptive(self, report: BandReport,
                                    lang_tag: bytes) -> float:
        """自适应路径（zero_cost/hybrid，min_n=1 统计）的存在性阈值。

        优先用 null 语料标定的每带 ratio 模型（阈值 = m × 阈值 ratio，
        m = 活动带数），未标定时用 DetectionThresholds 的默认线性常数。
        中英文分别用各自标定模型（词典不同、null 分布不同）。
        """
        m = sum(1 for st in report.bands if st.has_signal)
        nm = self._null_model.get(lang_tag)
        if nm is not None:
            _, thr_ratio = nm
            if m == 0:
                # 零活动带（词典词为零）：空证据不能构成检出。标定 ratio
                # 模型 0×ratio=0 会退化为 "0>=0 恒真" 误报——英文 null 文本
                # 可整句无词典词（零感词典覆盖稀疏），实测触发。
                return self._thresholds.existence_floor
            return m * thr_ratio
        return (self._thresholds.adaptive_intercept
                + self._thresholds.adaptive_slope * m)
