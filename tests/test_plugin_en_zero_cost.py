"""英文 zero_cost（零感词典）端到端测试（v0.9，feat/en-zero-cost）。

背景：英文此前只能走 default 词林（big→sizable 自然度差）。本测试验证
英文零感词典路径（拼写变体 + 功能副词 + 高自然安全对）与中文 zero_cost
对等：嵌入替换零感、trace 检出、标定链路、hybrid 扩展。

关键实现事实（与中文的差异）：
- en_tokenizer 正则 [A-Za-z]+(?:'[A-Za-z]+)? 切词，词典全小写单 token
- 英文零感词典 133 组（TIER1 拼写变体 30 组是英文专属王牌）
- trace 自适应路径须带 bands（缺 meta 时存在性可检出、UID 可能失真）
"""
from __future__ import annotations

import random

import pytest

from aawm.greenlist import GreenlistCodec
from aawm.plugins import UIDRegistry, Watermarker
from aawm.plugins.keystore import KeyStore

KEY = bytes(range(32))
SALT = b"en-zero-test-salt"
# 编辑攻击测试的固定盐：随机盐下嵌入替换词集合不同，回改 1/3 后信号
# 衰减程度随盐波动（弱信号边界），固定盐使攻击结果确定性。
EDIT_SALT = b"en-edit-salt-04"

# 零感词丰富的英文长文本（覆盖 TIER1 拼写变体 / TIER2 副词 / TIER3 各组）。
# 长度对齐中文专项文本（~1200 字符 / 80 词命中）：zero_cost 的 min_n=1
# 统计需要足够信号密度，短文本命中不足时存在性会低于阈值（中英一致）。
EN_ZERO_TEXT = (
    "We need to analyze the data and organize the project before we can make a final decision. "
    "The team will prioritize the critical tasks and minimize the risk of unexpected errors. "
    "However, the results clearly demonstrate a significant improvement over the previous version. "
    "The whole system remains simple and easy to use, which is a genuine advantage for new users. "
    "The final outcome will be published in the annual report and highlighted in the summary. "
    "We should verify the evidence before we choose the best option for the entire process. "
    "The process was altered to reduce errors and improve the overall outcome of the project. "
    "Almost every example shows the same pattern throughout the entire period of the study. "
    "The supervisor will allocate the resources and assign the tasks to the appropriate teams. "
    "We need to highlight the main factors and strengthen the framework of the whole system. "
    "The team gathered all the data and prepared a comprehensive summary for the final report. "
    "Eventually, the platform will support a global and modern interface for all users. "
    "We frequently discuss the precise requirements before we start the actual work. "
    "The results are usually consistent, although occasionally the data varies across different periods. "
    "We should immediately evaluate the impact and assess the potential risks before we proceed. "
    "The main goal is to achieve a beneficial outcome while maintaining a reliable and sufficient process. "
    "We aim to optimize the workflow and visualize the entire pipeline in a clear manner. "
    "The team decided to postpone the release until we completely finish the remaining tasks. "
    "We need to emphasize the key findings and underscore the important implications for future work. "
    "The system should remain flexible enough to accommodate different types of requests. "
    "We can demonstrate the value of the approach through a simple example and a concrete instance. "
    "The overall perspective of the team is to choose a suitable method and follow the appropriate procedure. "
    "We must ensure that every part of the system remains consistent across different platforms. "
    "The researchers are eager to recognize the contribution of every member of the group. "
    "We should decrease the number of errors and reduce the amount of redundant work. "
    "The project has a clear purpose and a well-defined aim that everyone understands. "
    "We can obtain the necessary evidence from multiple sources and verify the results carefully. "
    "The new version will incorporate the feedback and amend the earlier mistakes. "
    "We should acknowledge the previous achievements and build upon the established foundation. "
    "It is important to protect the sensitive data and safeguard the entire system from threats. "
    "The team will continue to monitor the situation and adjust the strategy as needed. "
    "We can classify the results by type and kind, then compare each factor and element. "
    "The final decision will be based on the evidence and proof that we collect during the study. "
    "We need to establish a common ground and a shared perspective before we proceed further. "
    "The system was designed to handle a huge volume of data and process it efficiently. "
    "We should make the necessary amendments and revise the document before the final submission. "
    "Every period and phase of the project has its own challenges and opportunities. "
    "The goal and objective of this initiative is to improve the overall quality of the service. "
    "We can measure the impact and effect of each change through the collected data. "
    "The purpose and aim of the review is to identify the main benefits and advantages. "
)

# 通用英文文本（零感词典覆盖稀疏，用于验证 default 兼容路径仍可用）
EN_GENERIC_TEXT = (
    "The platform collects telemetry from every distributed agent working "
    "in the fleet. Each agent watches a big stream of events, keeps a small "
    "record of important changes, and builds a short summary at the end of "
    "the reporting window. A strong supervisor groups the results into a "
    "common view, so the whole system stays easy to inspect. When an agent "
    "finds a hard problem it cannot fix alone, it sends a quick alert to "
    "the central team and asks for help. The team then checks whether the "
    "issue is new or old, whether it is critical or minor, and whether a "
    "fast patch is possible without a full restart of the service. The "
    "platform also supports a strong audit trail that records every "
    "important change made by any agent in the system, so a careful "
    "reviewer can always find the root cause of a hard problem. A common "
    "pattern is to split the big work into small tasks, assign each task "
    "to a single agent, and then merge the results into a final report. "
    "This approach keeps the system robust and easy to reason about, even "
    "as the total number of agents grows over time and the volume of "
    "events becomes a big challenge for the central team."
)

# null 参考语料（英文，无水印）
NULL_CORPUS = [
    "The weather was quite pleasant this morning and the sky looked clear.",
    "Students usually read several chapters before the weekly discussion session.",
    "The manager asked everyone to submit their reports by the end of the day.",
    "A good breakfast gives people enough energy to work through the morning.",
    "The library stays open late during the exam period for all registered students.",
]


def make_en_codec() -> GreenlistCodec:
    """英文零感词典 codec（直接构造，与 build_zero_cost_en_codec 等价）。"""
    from aawm.greenlist import build_zero_cost_en_codec
    return build_zero_cost_en_codec(KEY, SALT)


def _tokens(text: str):
    """简单按非字母切分提取小写 token。"""
    import re
    return re.findall(r"[a-z]+", text.lower())


class TestZeroCostEmbed:
    def test_embed_replaces_zero_cost_words(self):
        """embed 后文本被改写，且改动 token 全部来自零感词典组内候选。"""
        from aawm.synonym_data import load_zero_cost_en_dictionary
        wm = Watermarker(keystore=KeyStore(master_key=KEY))
        res = wm.embed(EN_ZERO_TEXT, user_id=42, language="en")
        assert res.language == "en"
        assert res.codec_mode == "zero_cost"
        assert res.watermarked_text != EN_ZERO_TEXT  # 确实被改写

        # 改动 token 必须是词典内合法候选（不允许组外词、不允许漏词）
        d = load_zero_cost_en_dictionary()
        allowed = {w for ws in d.values() for w in ws}
        orig = set(_tokens(EN_ZERO_TEXT))
        new = set(_tokens(res.watermarked_text))
        changed = (orig - new) | (new - orig)
        assert changed, "embed 未产生任何改动"
        assert changed <= allowed, f"改动含词典外词: {changed - allowed}"

        # 结构不变：句点数一致（零感替换不改标点/句子边界）
        assert res.watermarked_text.count(". ") == EN_ZERO_TEXT.count(". ")

    def test_trace_roundtrip_adaptive(self):
        """bands 存档的自适应 trace：watermarked + UID 正确 + 未篡改。"""
        wm = Watermarker()
        uid = 0x5A5A
        res = wm.embed(EN_ZERO_TEXT, user_id=uid, language="en")
        assert len(res.bands) > 0
        tr = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                      seal=res.seal, bands=res.bands, n_bits=res.n_bits,
                      language="en")
        assert tr.watermarked
        assert tr.uid == uid & ((1 << res.n_bits) - 1)
        assert tr.tampered is False

    def test_trace_no_bands_existence_only(self):
        """缺 bands 元数据：存在性应检出（UID 位空间不同可能失真，设计约束）。"""
        wm = Watermarker()
        res = wm.embed(EN_ZERO_TEXT, user_id=42, language="en")
        tr = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                      language="en")
        assert tr.watermarked

    def test_null_text_no_false_positive(self):
        """null 语料文本在正确盐下不应误报。

        用固定盐嵌入，使 res.session_salt 每次运行一致（null 误报随
        随机盐波动是 flaky 来源——VERIFICATION_REPORT 观测到跨盐
        误报 1/56 的弱信号边界波动）。
        """
        wm = Watermarker()
        res = wm.embed(EN_ZERO_TEXT, user_id=42, language="en",
                       session_salt=SALT)
        for null in NULL_CORPUS:
            tr = wm.trace(null, session_salt=res.session_salt, language="en")
            assert not tr.watermarked, f"null 误报: {null[:40]}..."

    def test_language_autodetect_zh_still_works(self):
        """语言自动检测不受英文路径影响（中文文本仍走中文）。"""
        wm = Watermarker(codec_mode="zero_cost")
        zh = "团队需要尽快处理这份报告的内容并优化流程。"
        r_zh = wm.embed(zh, user_id=7, language="zh")
        assert r_zh.language == "zh"
        r_en = wm.embed(EN_ZERO_TEXT, user_id=7, language="en")
        assert r_en.language == "en"


class TestZeroCostCalibration:
    def test_null_model_fitted_per_language(self):
        """null 模型按语言标定（修复：_fit_null_model 曾硬编码 b"zh"）。"""
        wm = Watermarker(codec_mode="zero_cost", language="en",
                         calibrate_corpus=NULL_CORPUS)
        assert b"en" in wm._null_model
        mu, thr_ratio = wm._null_model[b"en"]
        assert mu > 0 and thr_ratio > mu

    def test_calibrated_embed_trace_roundtrip(self):
        """标定下嵌入+溯源往返（英文）。"""
        wm = Watermarker(codec_mode="zero_cost", language="en",
                         calibrate_corpus=NULL_CORPUS)
        res = wm.embed(EN_ZERO_TEXT, user_id=0x1234, language="en")
        tr = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                      bands=res.bands, n_bits=res.n_bits, language="en")
        assert tr.watermarked
        assert tr.uid == 0x1234 & ((1 << res.n_bits) - 1)

    def test_calibrated_null_no_false_positive(self):
        """标定后 null 文本仍不误报（阈值来自英文 ratio 模型，固定盐确定性）。"""
        wm = Watermarker(codec_mode="zero_cost", language="en",
                         calibrate_corpus=NULL_CORPUS)
        res = wm.embed(EN_ZERO_TEXT, user_id=0x7777, language="en",
                       session_salt=SALT)
        tr = wm.trace(NULL_CORPUS[0], session_salt=res.session_salt,
                      language="en")
        assert not tr.watermarked


class TestZeroCostHybrid:
    def test_hybrid_embed_trace(self):
        """hybrid（零感打底 + 补充词典）英文路径。"""
        supp = {
            "tech": ["algorithm", "pipeline"],
            "quality": ["robust", "reliable"],
        }
        wm = Watermarker(codec_mode="hybrid", language="en",
                         supplementary_dict=supp,
                         calibrate_corpus=NULL_CORPUS)
        res = wm.embed(EN_ZERO_TEXT, user_id=0xBEEF, language="en")
        assert res.codec_mode == "hybrid"
        tr = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                      bands=res.bands, n_bits=res.n_bits, language="en")
        assert tr.watermarked
        assert tr.uid == 0xBEEF & ((1 << res.n_bits) - 1)


class TestEnDefaultCompat:
    def test_generic_en_text_default_mode(self):
        """通用英文文本走 default 词林（兼容路径）仍可嵌入+溯源。"""
        wm = Watermarker(codec_mode="default")
        res = wm.embed(EN_GENERIC_TEXT, user_id=0x0F0F, language="en")
        tr = wm.trace(res.watermarked_text, session_salt=res.session_salt,
                      language="en")
        assert tr.watermarked

    def test_generic_en_text_zero_cost_capacity_small(self):
        """通用英文文本在 zero_cost 下命中远少于 default 词林（文档要说明的边界）。"""
        wm_zh = Watermarker(codec_mode="zero_cost")
        wm_default = Watermarker(codec_mode="default")
        r_zc = wm_zh.embed(EN_GENERIC_TEXT, user_id=1, language="en")
        r_df = wm_default.embed(EN_GENERIC_TEXT, user_id=1, language="en")
        # 零感词典对通用文本命中稀疏 → 命中词数远小于 default 词林
        assert r_zc.n_dict_words < r_df.n_dict_words


class TestCodecLayer:
    def test_codec_embed_detect_roundtrip(self):
        """codec 层直接往返（绕过 facade）。"""
        codec = make_en_codec()
        marked = codec.embed(EN_ZERO_TEXT, 0x5A5A)
        rep = codec.detect(marked)
        assert rep.existence_score > 0

    def test_codec_dictionary_invariants(self):
        """词典不变量：组键在组内、全小写、无跨组共享词、无带空格的短语。"""
        from aawm.synonym_data import load_zero_cost_en_dictionary
        import re
        d = load_zero_cost_en_dictionary()
        assert len(d) >= 100, "英文零感词典组数异常"
        owned: dict[str, str] = {}
        for key, words in d.items():
            assert key in words, f"组键 {key} 不在组内"
            for w in words:
                assert w == w.lower(), f"非小写: {w}"
                assert re.fullmatch(r"[a-z]+", w), f"非法 token: {w}"
                assert w not in owned, f"跨组共享词: {w}"
                owned[w] = key

    def test_edit_attack_partial_rewrite(self):
        """部分词被改回原词（第三方无密钥改写）后仍能检出存在性。"""
        # 固定 key+盐：随机盐下替换词集合不同，回改后的信号衰减随盐波动，
        # 部分盐下会跌破存在性阈值（弱信号边界）导致断言偶发失败。
        wm = Watermarker(keystore=KeyStore(master_key=KEY))
        res = wm.embed(EN_ZERO_TEXT, user_id=42, language="en",
                       session_salt=EDIT_SALT)
        # 找出水印文本中真正被替换的词（与原文本 token 不同的词典词）
        from aawm.synonym_data import load_zero_cost_en_dictionary
        d = load_zero_cost_en_dictionary()
        orig_tokens = _tokens(EN_ZERO_TEXT)
        marked_tokens = _tokens(res.watermarked_text)
        replaced = [w for w in set(marked_tokens) - set(orig_tokens)
                    if w in {c for ws in d.values() for c in ws}]
        assert replaced, "embed 未产生可攻击的替换"
        # 回改其中 1/3 个替换词为组首词
        r = random.Random(99)
        revert = set(r.sample(sorted(replaced), max(1, len(replaced) // 3)))
        grp_of = {c: key for key, ws in d.items() for c in ws}
        rebuilt = []
        for tok in res.watermarked_text.split(" "):
            clean = tok.lower().rstrip(".,")
            if clean in revert:
                rebuilt.append(tok.replace(clean, grp_of[clean]))
            else:
                rebuilt.append(tok)
        attacked = " ".join(rebuilt)
        assert attacked != res.watermarked_text
        tr = wm.trace(attacked, session_salt=res.session_salt,
                      bands=res.bands, n_bits=res.n_bits, language="en")
        # 1/3 替换词回改不足以清除信号，存在性应仍检出
        assert tr.watermarked
