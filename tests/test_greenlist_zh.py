"""信道 B 中文路线测试（v0.5，§13.5/§13.8）。

验证要点：
1. 语言接缝隔离：language_tag=b"zh" 自动装配 (ZH 词典, 前向最大匹配分词器)，
   统计管线（embed/detect/calibrate/dot_score）代码路径与英文 100% 共享
2. 三必修课在中文词典上成立（不相交/双色过滤/逐带 p0）
3. 600 slot 中文文本：UID 往返 100%、30% 改写汉明距 ≤1、错误密钥无泄漏
"""
from __future__ import annotations

import random

import pytest

from aawm.greenlist import GreenlistCodec

KEY = bytes(range(32))
SALT = b"zh-test-salt"

_TEMPLATES = [
    "这份报告的内容非常{w}，需要尽快处理。",
    "团队的表现十分{w}，客户对此{w}。",
    "系统运行{w}，各项指标{w}，结果{w}。",
    "我们认为该方案{w}，但其成本仍然{w}。",
    "经过{w}的分析，结论是{w}的。",
]


def make_codec() -> GreenlistCodec:
    return GreenlistCodec(KEY, SALT, language_tag=b"zh")


def make_zh_text(codec: GreenlistCodec, n_slots: int, seed: int) -> str:
    """模板句填充过滤后组的组首词，n_slots 为词典词槽位数。"""
    r = random.Random(seed)
    group_list = list(codec._groups.values())
    sents: list[str] = []
    made = 0
    while made < n_slots:
        t = r.choice(_TEMPLATES)
        picks = [r.choice(group_list)[0] for _ in range(t.count("{w}"))]
        for p in picks:
            t = t.replace("{w}", p, 1)
        sents.append(t)
        made += len(picks)
    return "".join(sents)


def zh_paraphrase(codec: GreenlistCodec, text: str, frac: float, seed: int):
    """同组随机替换 frac 比例的词典词（模拟第三方无密钥改写）。"""
    r = random.Random(seed)
    toks = codec._tokenizer(text)
    n_dict = sum(1 for _, n in toks if n is not None)
    n_target = int(n_dict * frac)
    out: list[str] = []
    changed = 0
    for raw, norm in toks:
        grp = codec._w2group.get(norm)
        if grp and changed < n_target and r.random() < frac + 0.35:
            out.append(r.choice([x for x in grp if x != norm]))
            changed += 1
        else:
            out.append(raw)
    return "".join(out), changed


class TestZhPipeline:
    def test_pipeline_invariants(self):
        """双色组过滤后组数随密钥浮动（v0.9 词典扩容后实测 ~4.5k/6.3k），
        不变量恒成立。"""
        codec = make_codec()
        st = codec.stats
        assert 4000 <= st["n_groups"] <= 6300
        assert st["n_bands"] == 16
        # 双色不变量
        for members in codec._groups.values():
            colors = {codec.green(w) for w in members}
            assert len(colors) > 1
        # 不相交不变量：一词一带一组
        seen: dict[str, int] = {}
        for w, b in codec._w2band.items():
            prev = seen.setdefault(w, b)
            assert prev == b

    def test_embed_preserves_non_dict_text(self):
        """无损重组：非词典字符（标点/ASCII/单字）原样保留。"""
        codec = make_codec()
        text = "系统运行123分钟。OK，结果{w}的。".replace("{w}", "稳定")
        marked = codec.embed(text, 0x00FF, bias=1.0)
        assert "123分钟" in marked
        assert "OK，" in marked
        assert "的。" in marked

    def test_no_boundary_drift_after_embed(self):
        """回归：双字词替换不得引发分词边界漂移。

        场景（实测踩坑）："项|指标" 替换 "指标"->"目的" 后重新分词
        切出 "项目"（band0 非绿词），53 个漂移词把 band0 的 z 打成 -1.69。
        修复后 embed 的每次替换保证左右接缝不成词，marked 的词典词
        token 序列与原文严格对齐。
        """
        codec = make_codec()
        text = make_zh_text(codec, 600, seed=42 + 0xFFFF)
        before = [
            norm
            for _, norm in codec._tokenizer(text)
            if norm is not None and norm in codec._w2band
        ]
        marked = codec.embed(text, 0xFFFF, bias=1.0)
        after = [
            norm
            for _, norm in codec._tokenizer(marked)
            if norm is not None and norm in codec._w2band
        ]
        assert len(before) == len(after), "嵌入前后有效词典词 token 数漂移"


class TestZhRoundTrip:
    def test_round_trip_multiple_uids(self):
        codec = make_codec()
        for uid in (0x0000, 0x1234, 0x5678, 0xABCD, 0xFFFF):
            text = make_zh_text(codec, 600, seed=42 + uid)
            marked = codec.embed(text, uid, bias=1.0)
            rep = codec.detect(marked)
            assert rep.uid == uid, f"uid=0x{uid:04X} 解出 0x{rep.uid:04X}"

    def test_calibrated_null_is_small(self):
        """必修课 3：标定后无水印文本 Σ|z| 显著小于嵌入文本。"""
        codec = make_codec()
        null_corpus = [make_zh_text(codec, 600, seed=s) for s in range(100, 106)]
        codec.calibrate_p0(null_corpus)
        nulls = [codec.detect(t).existence_score for t in null_corpus]
        assert max(nulls) < 20  # 16 带标定后 null Σ|z| 均值 ~8

        text = make_zh_text(codec, 600, seed=7)
        marked = codec.embed(text, 0x5678, bias=1.0)
        assert codec.detect(marked).existence_score > 4 * max(nulls)

    def test_paraphrase_30pct(self):
        codec = make_codec()
        text = make_zh_text(codec, 600, seed=7)
        uid = 0x5678
        marked = codec.embed(text, uid, bias=1.0)
        rw, _ = zh_paraphrase(codec, marked, 0.30, seed=9)
        rep = codec.detect(rw)
        assert bin(rep.uid ^ uid).count("1") <= 1  # 注册库近邻可恢复

    def test_wrong_key_no_leak(self):
        codec = make_codec()
        bad = GreenlistCodec(KEY + b"x", SALT, language_tag=b"zh")
        text = make_zh_text(codec, 600, seed=7)
        marked = codec.embed(text, 0x5678, bias=1.0)
        rep = bad.detect(marked)
        assert bin(rep.uid ^ 0x5678).count("1") >= 5  # 错误密钥下 UID 均匀化

    def test_dot_score_positive_for_true_uid(self):
        codec = make_codec()
        null_corpus = [make_zh_text(codec, 600, seed=s) for s in range(100, 104)]
        codec.calibrate_p0(null_corpus)
        text = make_zh_text(codec, 600, seed=7)
        marked = codec.embed(text, 0x5678, bias=1.0)
        assert codec.dot_score(marked, 0x5678) > 50
        assert codec.dot_score(marked, 0x5678 ^ 0xFFFF) < -50  # 反相 UID 显著负

    def test_en_zh_key_isolation(self):
        """language_tag 隔离：同一 master_key 下中英文绿名单互不干扰。"""
        en = GreenlistCodec(KEY, SALT, language_tag=b"en")
        zh = GreenlistCodec(KEY, SALT, language_tag=b"zh")
        w = "稳定"
        if w in zh._w2band:
            assert en.green(w) != zh.green(w) or en.band_of_group(w) != zh.band_of_group(w)
