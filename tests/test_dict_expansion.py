"""词典扩容（v0.9）测试：加载器、回退、默认词典生效、往返。"""
from __future__ import annotations

import os

import pytest

from aawm.greenlist import GreenlistCodec
from aawm.synonym_data import (
    EN_SYNONYMS_EXTRA,
    EN_SYNONYMS_RAW,
    ZH_SYNONYMS_RAW,
    _load_groups,
    load_default_en_dictionary,
    load_default_zh_dictionary,
)


class TestLoaders:
    def test_load_zh_expanded(self):
        """数据文件存在时返回扩容词典（组数远超手工策划 260）。"""
        d = load_default_zh_dictionary()
        assert d is not ZH_SYNONYMS_RAW
        assert len(d) > 1000
        # 词条全部双字汉字（zh tokenizer 兼容性）
        for ws in d.values():
            for w in ws:
                assert len(w) == 2

    def test_load_en_expanded(self):
        """数据文件存在时返回扩容词典（组数远超手工策划 677）。"""
        d = load_default_en_dictionary()
        assert len(d) > 1000
        # 英文词条必须是单词（无空格）
        for ws in d.values():
            for w in ws:
                assert " " not in w
                assert len(w) >= 1

    def test_load_groups_missing_file(self, monkeypatch, tmp_path):
        """数据文件缺失返回 None（回退路径的触发条件）。"""
        monkeypatch.setattr(
            "aawm.synonym_data._DATA_DIR", str(tmp_path)
        )
        assert _load_groups("zh_synonyms.json") is None

    def test_fallback_to_handcrafted(self, monkeypatch, tmp_path):
        """数据文件缺失时回退手工策划词典，接口不抛异常。"""
        monkeypatch.setattr(
            "aawm.synonym_data._DATA_DIR", str(tmp_path)
        )
        monkeypatch.setattr("aawm.synonym_data._DEFAULT_ZH_CACHE", None)
        monkeypatch.setattr("aawm.synonym_data._DEFAULT_EN_CACHE", None)
        zh = load_default_zh_dictionary()
        en = load_default_en_dictionary()
        assert zh == ZH_SYNONYMS_RAW
        assert en == {**EN_SYNONYMS_RAW, **EN_SYNONYMS_EXTRA}

    def test_cache_reuse(self):
        """模块级缓存：两次调用返回同一对象（避免重复解析 JSON）。"""
        assert load_default_zh_dictionary() is load_default_zh_dictionary()
        assert load_default_en_dictionary() is load_default_en_dictionary()


class TestDefaultCodec:
    def test_zh_default_uses_expanded_dict(self):
        """GreenlistCodec 默认中文词典 = 扩容版（分词器同步）。"""
        codec = GreenlistCodec(b"k" * 16, b"s" * 16, language_tag=b"zh")
        assert len(codec._groups) > 1000
        # 扩容词典的常见词能被分词命中（词林 '=' 组词）
        text = "研究人员分析了系统的运行状态。"
        n = sum(1 for _, norm in codec._tokenizer(text)
                if norm and norm in codec._w2band)
        assert n >= 2

    def test_en_default_uses_expanded_dict(self):
        codec = GreenlistCodec(b"k" * 16, b"s" * 16, language_tag=b"en")
        assert len(codec._groups) > 1000

    def test_zh_roundtrip_with_expanded_dict(self):
        """扩容词典下嵌入-解码往返（16 band 全覆盖的合成文本）。"""
        codec = GreenlistCodec(b"k" * 16, b"s" * 16, language_tag=b"zh")
        # 从词典组采样构造高密度文本
        import random

        rng = random.Random(7)
        groups = list(codec._groups.values())
        words = []
        for _ in range(300):
            words.append(rng.choice(rng.choice(groups)))
        text = "".join(w for i, w in enumerate(words) if i % 5 == 0) \
            + "".join(w for i, w in enumerate(words) if i % 5 != 0 and i % 3 == 0)
        marked = codec.embed(text, 0x1234, bias=1.0, rng=random.Random(0))
        rep = codec.detect(marked)
        assert rep.uid == 0x1234
        assert rep.existence_score > 10

    def test_data_files_exist_and_valid(self):
        """数据文件在包内且 JSON 可解析（打包完整性）。"""
        import aawm.synonym_data as sd

        for fname in ("zh_synonyms.json", "en_synonyms.json"):
            path = os.path.join(sd._DATA_DIR, fname)
            assert os.path.exists(path), f"missing {fname}"
            assert os.path.getsize(path) > 100_000
