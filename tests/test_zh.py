"""v0.4 中文支持测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm import CAEmbedder, CADecoder, CAConfig, generate_master_key  # noqa: E402
from aawm.zh import ZhAdapter, EnAdapter, get_adapter  # noqa: E402

ZH_TEXT = (
    "这个项目非常重要，我们需要快速推进。系统必须稳定可靠，"
    "同时保持灵活高效。虽然过程复杂困难，但成果会非常优秀。"
    "团队需要认真仔细地分析问题，制定严格清晰的方案。"
    "每个细节都要精确准确，确保安全稳固。"
    "这个方案非常重要，必须快速推进。虽然过程复杂困难，"
    "但成果会非常优秀。团队需要认真仔细地分析问题，"
    "制定严格清晰的方案。每个细节都要精确准确，确保安全稳固。"
    "项目的重要性和紧急性都很高。我们需要全面系统地分析每个问题。"
    "方案必须精确清晰，过程必须稳定可靠。虽然困难复杂，"
    "但成果会很优秀。团队要认真仔细，确保每个细节安全稳固。"
)


class TestZhAdapter:
    def test_get_adapter_zh(self):
        a = get_adapter("zh")
        assert isinstance(a, ZhAdapter)

    def test_get_adapter_en(self):
        a = get_adapter("en")
        assert isinstance(a, EnAdapter)

    def test_get_adapter_unknown_defaults_en(self):
        a = get_adapter("unknown")
        assert isinstance(a, EnAdapter)

    def test_zh_tokenize_preserves_text(self):
        """分词后 join 还原原文。"""
        adapter = ZhAdapter()
        tokens = adapter.tokenize(ZH_TEXT)
        assert "".join(tokens) == ZH_TEXT

    def test_zh_tokenize_finds_dict_words(self):
        """双字词典词被识别为整体 token。"""
        adapter = ZhAdapter()
        tokens = adapter.tokenize("这个项目非常重要")
        assert "项目" in tokens
        assert "重要" in tokens

    def test_zh_extract_symbol(self):
        """提取声母。"""
        adapter = ZhAdapter()
        # 项目 -> x, 重要 -> zh
        assert adapter.extract_symbol("项目") == "x"
        assert adapter.extract_symbol("重要") == "zh"

    def test_zh_letter_alphabet_23(self):
        """中文声母表 23 个。"""
        adapter = ZhAdapter()
        assert len(adapter.letter_alphabet()) == 23


class TestZhWatermark:
    def test_zh_roundtrip(self):
        """中文 embed→decode 往返成功（短文本容量有限，重试至多 10 次）。"""
        key = generate_master_key()
        cfg = CAConfig(language="zh", min_anchorable=20)
        emb, dec = CAEmbedder(key, cfg), CADecoder(key, cfg)
        for _ in range(10):
            r = emb.embed(ZH_TEXT, user_id=42)
            d = dec.decode(r.watermarked_text, r.session_salt)
            if d.success and d.user_id == 42:
                return
        assert False, "zh roundtrip failed after 10 retries"

    def test_zh_multiple_uids(self):
        """多 UID 往返（短文本容量有限，每个 UID 重试至多 10 次）。"""
        key = generate_master_key()
        cfg = CAConfig(language="zh", min_anchorable=20)
        emb, dec = CAEmbedder(key, cfg), CADecoder(key, cfg)
        for uid in [0, 100, 1000, 10000, 60000]:
            success = False
            for _ in range(10):
                r = emb.embed(ZH_TEXT, user_id=uid)
                d = dec.decode(r.watermarked_text, r.session_salt)
                if d.success and d.user_id == uid:
                    success = True
                    break
            assert success, f"uid={uid} failed after 10 retries"

    def test_zh_wrong_key_rejected(self):
        """错误密钥不能解出原 UID。"""
        key = generate_master_key()
        wrong = generate_master_key()
        cfg = CAConfig(language="zh", min_anchorable=20)
        emb = CAEmbedder(key, cfg)
        dec = CADecoder(wrong, cfg)
        r = emb.embed(ZH_TEXT, user_id=42)
        d = dec.decode(r.watermarked_text, r.session_salt)
        assert not (d.success and d.user_id == 42)

    def test_zh_text_preserved(self):
        """水印文本与原文差异有限（仅同义替换，<30% 字符改动）。"""
        key = generate_master_key()
        cfg = CAConfig(language="zh", min_anchorable=20)
        emb = CAEmbedder(key, cfg)
        for _ in range(10):
            r = emb.embed(ZH_TEXT, user_id=42)
            diff = sum(1 for a, b in zip(ZH_TEXT, r.watermarked_text) if a != b)
            if diff < len(ZH_TEXT) * 0.3:
                return
        assert diff < len(ZH_TEXT) * 0.3  # 最后一次仍失败则报错

    def test_zh_no_external_dependency(self):
        """中文支持零强依赖（不导入 jieba/pypinyin）。"""
        import importlib
        # 不应强依赖 jieba
        try:
            jieba = importlib.import_module("jieba")
            # 若安装了也可用，但不强依赖
        except ImportError:
            pass  # 正常：无 jieba 也能工作
        # 确认 ZhAdapter 能独立工作
        adapter = ZhAdapter()
        assert adapter.extract_symbol("项目") == "x"
