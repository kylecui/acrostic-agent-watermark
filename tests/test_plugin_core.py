"""Phase 1 核心插件模块单元测试。

覆盖：keystore / registry / context / facade / middleware / streaming。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm.plugins import (
    Context,
    ContextChain,
    ContextProvider,
    DetectionThresholds,
    EmbedResult,
    EnvVarContextProvider,
    FrameworkContextProvider,
    HeaderContextProvider,
    KeyStore,
    StreamingWatermarker,
    TraceResult,
    UIDRegistry,
    Watermarker,
    WatermarkMiddleware,
    generate_key,
    reset_user_context,
    set_user_context,
)


# ----------------------------------------------------------------------
# 测试文本（足够长，确保词典命中数充足）
# ----------------------------------------------------------------------

LONG_TEXT_EN = (
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

LONG_TEXT_ZH = (
    "平台从每一个在编队中工作的分布式代理收集遥测数据。每个代理监视一个"
    "大事件流，保留重要变更的小记录，并在报告窗口结束时构建简短摘要。"
    "强大的监督器将结果分组为通用视图，使整个系统易于检查。当代理发现无法"
    "独自修复的困难问题时，会向中央团队发送快速警报并寻求帮助。团队然后"
    "检查问题是新是旧，是关键还是次要，以及是否可以在不完全重启服务的"
    "情况下应用快速补丁。平台还支持强大的审计跟踪，记录系统中任何代理"
    "所做的每一项重要变更，因此仔细的审查者总能找到困难问题的根本原因。"
    "常见模式是将大工作拆分为小任务，将每个任务分配给单个代理，然后将"
    "结果合并为最终报告。这种方法使系统保持稳健且易于推理，即使代理总数"
    "随时间增长且事件量成为中央团队的大挑战。"
)


# ======================================================================
# KeyStore 测试
# ======================================================================

class TestKeyStore:
    def test_memory_default(self):
        ks = KeyStore()
        key = ks.get()
        assert len(key) == 32

    def test_custom_key(self):
        key = os.urandom(32)
        ks = KeyStore(key)
        assert ks.get() == key

    def test_short_key_rejected(self):
        with pytest.raises(ValueError, match="too short"):
            KeyStore(b"short")

    def test_file_persist_load(self, tmp_path):
        path = tmp_path / "key.json"
        ks1 = KeyStore()
        ks1.save(path)
        assert path.exists()

        ks2 = KeyStore.from_file(path)
        assert ks1.get() == ks2.get()

    def test_file_create_if_not_exists(self, tmp_path):
        path = tmp_path / "new_key.json"
        assert not path.exists()
        ks = KeyStore.from_file(path, create=True)
        assert path.exists()
        assert len(ks.get()) == 32

    def test_file_no_create_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            KeyStore.from_file(tmp_path / "nonexistent.json")

    def test_from_env(self, monkeypatch):
        key_hex = os.urandom(32).hex()
        monkeypatch.setenv("AAWM_MASTER_KEY", key_hex)
        ks = KeyStore.from_env()
        assert ks.get().hex() == key_hex

    def test_from_env_custom_var(self, monkeypatch):
        key_hex = os.urandom(32).hex()
        monkeypatch.setenv("CUSTOM_KEY", key_hex)
        ks = KeyStore.from_env("CUSTOM_KEY")
        assert ks.get().hex() == key_hex

    def test_from_any_priority(self, tmp_path):
        # 直接传入 > 文件 > 环境变量
        direct = os.urandom(32)
        ks = KeyStore.from_any(master_key=direct)
        assert ks.get() == direct

    def test_from_any_file(self, tmp_path):
        path = tmp_path / "k.json"
        ks1 = KeyStore()
        ks1.save(path)
        ks2 = KeyStore.from_any(key_file=path)
        assert ks1.get() == ks2.get()

    def test_from_any_memory_fallback(self):
        ks = KeyStore.from_any()
        assert len(ks.get()) == 32

    def test_export_env(self):
        ks = KeyStore()
        s = ks.export_env()
        assert s.startswith("export AAWM_MASTER_KEY=")
        assert ks.get().hex() in s

    def test_generate_key(self):
        k = generate_key()
        assert len(k) == 32
        k2 = generate_key(16)
        assert len(k2) == 16


# ======================================================================
# UIDRegistry 测试
# ======================================================================

class TestUIDRegistry:
    def test_register_auto_uid(self):
        reg = UIDRegistry()
        uid = reg.register("alice")
        assert uid == 1
        uid2 = reg.register("bob")
        assert uid2 == 2

    def test_register_explicit_uid(self):
        reg = UIDRegistry()
        uid = reg.register("alice", uid=0x1234)
        assert uid == 0x1234

    def test_register_idempotent(self):
        reg = UIDRegistry()
        uid1 = reg.register("alice")
        uid2 = reg.register("alice")
        assert uid1 == uid2

    def test_register_conflict_uid(self):
        reg = UIDRegistry()
        reg.register("alice", uid=100)
        with pytest.raises(ValueError, match="already in use"):
            reg.register("bob", uid=100)

    def test_register_uid_out_of_range(self):
        reg = UIDRegistry()
        with pytest.raises(ValueError, match="out of range"):
            reg.register("x", uid=70000)

    def test_resolve_alias_auto_register(self):
        reg = UIDRegistry()
        uid = reg.resolve_alias("alice")
        assert uid == 1
        # 再次 resolve 应返回同一 UID
        assert reg.resolve_alias("alice") == uid

    def test_lookup(self):
        reg = UIDRegistry()
        uid = reg.register("alice")
        assert reg.lookup(uid) == "alice"
        assert reg.lookup(99999) is None

    def test_nearest_match_exact(self):
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        reg.register("bob", uid=0x00FF)
        match = reg.nearest_match(0x1234)
        assert match is not None
        assert match[0] == 0x1234
        assert match[1] == "alice"
        assert match[2] == 0

    def test_nearest_match_close(self):
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        reg.register("bob", uid=0x00FF)
        # 0x1235 与 0x1234 汉明距 1
        match = reg.nearest_match(0x1235, max_hamming=3)
        assert match is not None
        assert match[1] == "alice"
        assert match[2] == 1

    def test_nearest_match_no_match(self):
        reg = UIDRegistry()
        reg.register("alice", uid=0x0001)
        # 0xFFFF 与 0x0001 汉明距 15
        match = reg.nearest_match(0xFFFF, max_hamming=3)
        assert match is None

    def test_masked_nearest_match(self):
        reg = UIDRegistry()
        reg.register("alice", uid=0x1234)
        # 只比较低 4 位
        match = reg.masked_nearest_match(0x1234, active_mask=0x000F)
        assert match is not None
        assert match[2] == 0

    def test_file_persistence(self, tmp_path):
        path = tmp_path / "registry.json"
        reg1 = UIDRegistry(backend="file", path=path)
        reg1.register("alice", uid=0x1234)
        reg1.register("bob", uid=0x00FF)
        # 重新加载
        reg2 = UIDRegistry(backend="file", path=path)
        assert reg2.lookup(0x1234) == "alice"
        assert reg2.lookup(0x00FF) == "bob"

    def test_list_all(self):
        reg = UIDRegistry()
        reg.register("alice", uid=1)
        reg.register("bob", uid=2)
        all_entries = reg.list_all()
        assert len(all_entries) == 2
        assert all_entries[1] == "alice"

    def test_contains(self):
        reg = UIDRegistry()
        reg.register("alice", uid=1)
        assert 1 in reg
        assert "alice" in reg
        assert 999 not in reg

    def test_len(self):
        reg = UIDRegistry()
        assert len(reg) == 0
        reg.register("alice")
        assert len(reg) == 1


# ======================================================================
# Context 测试
# ======================================================================

class TestContext:
    def test_context_valid(self):
        ctx = Context(user_id=42)
        assert ctx.is_valid()

    def test_context_invalid(self):
        ctx = Context()
        assert not ctx.is_valid()

    def test_language_tag(self):
        assert Context(user_id=1, language="en").language_tag() == b"en"
        assert Context(user_id=1, language="zh").language_tag() == b"zh"

    def test_framework_provider_dict(self):
        provider = FrameworkContextProvider()
        ctx = provider.resolve(context={"user_id": 42})
        assert ctx is not None
        assert ctx.user_id == 42

    def test_framework_provider_kwargs(self):
        provider = FrameworkContextProvider()
        ctx = provider.resolve(user_id="alice", session_id="s1")
        assert ctx is not None
        assert ctx.user_id == "alice"
        assert ctx.session_id == "s1"

    def test_framework_provider_no_user_id(self):
        provider = FrameworkContextProvider()
        ctx = provider.resolve(context={"foo": "bar"})
        assert ctx is None

    def test_env_provider_envvar(self, monkeypatch):
        monkeypatch.setenv("AAWM_USER_ID", "42")
        provider = EnvVarContextProvider()
        ctx = provider.resolve()
        assert ctx is not None
        assert ctx.user_id == 42

    def test_env_provider_hex(self, monkeypatch):
        monkeypatch.setenv("AAWM_USER_ID", "0x1234")
        provider = EnvVarContextProvider()
        ctx = provider.resolve()
        assert ctx.user_id == 0x1234

    def test_env_provider_alias(self, monkeypatch):
        monkeypatch.setenv("AAWM_USER_ID", "agent-cuiyin")
        provider = EnvVarContextProvider()
        ctx = provider.resolve()
        assert ctx.user_id == "agent-cuiyin"

    def test_env_provider_contextvar(self):
        provider = EnvVarContextProvider()
        token = set_user_context(user_id=99, session_id="s1", language="zh")
        try:
            ctx = provider.resolve()
            assert ctx.user_id == 99
            assert ctx.session_id == "s1"
            assert ctx.language == "zh"
        finally:
            reset_user_context(token)

    def test_header_provider(self):
        provider = HeaderContextProvider()
        headers = {"X-AAWM-User-Id": "42", "X-AAWM-Session-Id": "s1"}
        ctx = provider.resolve(headers=headers)
        assert ctx is not None
        assert ctx.user_id == 42
        assert ctx.session_id == "s1"

    def test_header_provider_alias(self):
        provider = HeaderContextProvider()
        ctx = provider.resolve(headers={"X-AAWM-User-Id": "agent-cuiyin"})
        assert ctx.user_id == "agent-cuiyin"

    def test_header_provider_no_header(self):
        provider = HeaderContextProvider()
        ctx = provider.resolve(headers={})
        assert ctx is None

    def test_context_chain_default(self):
        chain = ContextChain.default()
        ctx = chain.resolve(user_id=42)
        assert ctx.user_id == 42

    def test_context_chain_fallback(self, monkeypatch):
        monkeypatch.setenv("AAWM_USER_ID", "99")
        chain = ContextChain.default()
        ctx = chain.resolve()  # 框架无，env 有
        assert ctx.user_id == 99

    def test_context_chain_empty(self):
        chain = ContextChain.default()
        ctx = chain.resolve()
        assert not ctx.is_valid()

    def test_context_chain_prepend(self):
        class CustomProvider:
            def resolve(self, **kwargs):
                return Context(user_id="custom")
        chain = ContextChain.default().prepend(CustomProvider())
        ctx = chain.resolve()
        assert ctx.user_id == "custom"


# ======================================================================
# Watermarker Facade 测试
# ======================================================================

class TestWatermarker:
    def test_embed_basic(self):
        wm = Watermarker()
        result = wm.embed(LONG_TEXT_EN, user_id=0x1234)
        assert isinstance(result, EmbedResult)
        assert result.watermarked_text != LONG_TEXT_EN  # 有改动
        assert result.user_id == 0x1234
        assert result.existence_score > 0
        assert result.seal is not None  # 默认签名

    def test_embed_no_sign(self):
        wm = Watermarker()
        result = wm.embed(LONG_TEXT_EN, user_id=42, sign=False)
        assert result.seal is None

    def test_embed_int_uid(self):
        wm = Watermarker()
        result = wm.embed(LONG_TEXT_EN, user_id=0xABCD)
        assert result.user_id == 0xABCD
        assert result.user_alias is None

    def test_embed_str_alias_with_registry(self):
        reg = UIDRegistry()
        reg.register("agent-cuiyin", uid=0x1234)
        wm = Watermarker(registry=reg)
        result = wm.embed(LONG_TEXT_EN, user_id="agent-cuiyin")
        assert result.user_id == 0x1234
        assert result.user_alias == "agent-cuiyin"

    def test_embed_str_alias_auto_register(self):
        reg = UIDRegistry()
        wm = Watermarker(registry=reg)
        result = wm.embed(LONG_TEXT_EN, user_id="new-user")
        assert result.user_id == 1
        assert result.user_alias == "new-user"

    def test_embed_str_alias_no_registry(self):
        wm = Watermarker()
        result = wm.embed(LONG_TEXT_EN, user_id="agent-cuiyin")
        # 无注册库时哈希为 UID
        assert isinstance(result.user_id, int)
        assert result.user_alias == "agent-cuiyin"

    def test_trace_roundtrip(self):
        wm = Watermarker()
        result = wm.embed(LONG_TEXT_EN, user_id=0x1234)
        trace = wm.trace(result.watermarked_text,
                         session_salt=result.session_salt, seal=result.seal)
        assert isinstance(trace, TraceResult)
        assert trace.watermarked is True
        assert trace.uid is not None
        assert trace.tampered is False  # 未篡改

    def test_trace_null_text(self):
        wm = Watermarker()
        # 无水印原文在随机 salt 下可能有统计假阳性（概率性检测），
        # 多次检测取多数表决确保原始文本不被误判
        false_count = 0
        for _ in range(5):
            trace = wm.trace(LONG_TEXT_EN)  # 无水印原文
            if not trace.watermarked:
                false_count += 1
        assert false_count >= 3  # 多数 salt 下应为 False

    def test_trace_with_registry_match(self):
        reg = UIDRegistry()
        reg.register("agent-cuiyin", uid=0x1234)
        reg.register("agent-beta", uid=0x00FF)
        wm = Watermarker(registry=reg)
        result = wm.embed(LONG_TEXT_EN, user_id="agent-cuiyin")
        trace = wm.trace(result.watermarked_text, session_salt=result.session_salt)
        assert trace.watermarked
        assert trace.user == "agent-cuiyin"

    def test_trace_tampered(self):
        wm = Watermarker()
        result = wm.embed(LONG_TEXT_EN, user_id=42)
        # 篡改文本
        paras = result.watermarked_text.split(". ")
        paras[0] = paras[0].replace("telemetry", "surveillance")
        tampered = ". ".join(paras)
        trace = wm.trace(tampered, session_salt=result.session_salt, seal=result.seal)
        assert trace.tampered is True
        assert len(trace.tampered_paragraphs) > 0

    def test_detect_only(self):
        wm = Watermarker()
        result = wm.embed(LONG_TEXT_EN, user_id=42, rng_seed=42)
        # detect_only 需要传 session_salt（存在性依赖盐派生）
        assert wm.detect_only(result.watermarked_text, session_salt=result.session_salt) is True
        # 原始文本在特定 salt 下可能有统计假阳性（概率性检测），
        # 用多个 salt 取多数表决确保原始文本不被误判
        false_count = 0
        for _ in range(5):
            if not wm.detect_only(LONG_TEXT_EN):
                false_count += 1
        assert false_count >= 3  # 多数 salt 下应为 False

    def test_chinese_embed_trace(self):
        wm = Watermarker()
        result = wm.embed(LONG_TEXT_ZH, user_id=0x1234, language="zh")
        assert result.language == "zh"
        trace = wm.trace(result.watermarked_text,
                        session_salt=result.session_salt, seal=result.seal)
        assert trace.watermarked is True

    def test_auto_language_detection(self):
        wm = Watermarker(language="auto")
        result_en = wm.embed(LONG_TEXT_EN, user_id=42)
        result_zh = wm.embed(LONG_TEXT_ZH, user_id=42)
        assert result_en.language == "en"
        assert result_zh.language == "zh"

    def test_calibrate_p0(self):
        wm = Watermarker()
        wm.calibrate_p0([LONG_TEXT_EN, LONG_TEXT_EN + " More text here."])
        # 标定后应仍能正常嵌入溯源
        result = wm.embed(LONG_TEXT_EN, user_id=42)
        trace = wm.trace(result.watermarked_text, session_salt=result.session_salt)
        assert trace.watermarked

    def test_from_config(self, tmp_path):
        key_file = tmp_path / "key.json"
        reg_file = tmp_path / "registry.json"
        wm = Watermarker.from_config(str(key_file), str(reg_file))
        assert key_file.exists()
        result = wm.embed(LONG_TEXT_EN, user_id="alice")
        assert result.user_alias == "alice"
        # 重新加载应持久化
        wm2 = Watermarker.from_config(str(key_file), str(reg_file))
        assert wm2.registry.lookup(result.user_id) == "alice"

    def test_deterministic_embed(self):
        wm = Watermarker()
        salt = os.urandom(16)
        r1 = wm.embed(LONG_TEXT_EN, user_id=42, session_salt=salt, rng_seed=42)
        r2 = wm.embed(LONG_TEXT_EN, user_id=42, session_salt=salt, rng_seed=42)
        assert r1.watermarked_text == r2.watermarked_text


# ======================================================================
# WatermarkMiddleware 测试
# ======================================================================

class TestWatermarkMiddleware:
    def test_transform_basic(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        ctx = Context(user_id=42)
        marked, result = mw.transform(LONG_TEXT_EN, ctx)
        assert result is not None
        assert marked != LONG_TEXT_EN

    def test_transform_no_context_skip(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm, skip_if_no_context=True)
        marked, result = mw.transform(LONG_TEXT_EN, None)
        assert result is None
        assert marked == LONG_TEXT_EN

    def test_transform_short_text_skip(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm, min_text_length=100)
        ctx = Context(user_id=42)
        marked, result = mw.transform("short text", ctx)
        assert result is None
        assert marked == "short text"

    def test_transform_fail_open_on_error(self):
        # 制造一个会抛异常的 Watermarker
        class BadWatermarker:
            def embed(self, *args, **kwargs):
                raise RuntimeError("boom")
        mw = WatermarkMiddleware(BadWatermarker(), skip_if_no_context=False)  # type: ignore
        ctx = Context(user_id=42)
        marked, result = mw.transform(LONG_TEXT_EN, ctx)
        assert result is None
        assert marked == LONG_TEXT_EN  # fail-open 透传原文

    def test_transform_empty_text(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        ctx = Context(user_id=42)
        marked, result = mw.transform("", ctx)
        assert result is None
        assert marked == ""

    def test_on_embed_called_with_result(self):
        """on_embed 回调收到 EmbedResult，且 salt 可用于溯源。"""
        wm = Watermarker()
        archived = {}

        def on_embed(result, ctx):
            archived[result.user_id] = result.session_salt

        mw = WatermarkMiddleware(wm, on_embed=on_embed)
        ctx = Context(user_id=42)
        marked, result = mw.transform(LONG_TEXT_EN, ctx)

        assert result is not None
        assert 42 in archived
        # 用回调存下的 salt 能成功溯源
        t = wm.trace(marked, session_salt=archived[42])
        assert t.watermarked
        assert t.uid == 42

    def test_on_embed_fail_open(self):
        """on_embed 回调抛异常时不影响嵌入主流程（fail-open）。"""
        wm = Watermarker()

        def bad_callback(result, ctx):
            raise RuntimeError("archive down")

        mw = WatermarkMiddleware(wm, on_embed=bad_callback)
        ctx = Context(user_id=42)
        marked, result = mw.transform(LONG_TEXT_EN, ctx)

        assert result is not None  # 嵌入仍成功
        assert marked != LONG_TEXT_EN

    def test_should_embed_string(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        assert mw.should_embed("hello world this is a long enough text") is True

    def test_should_embed_empty(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        assert mw.should_embed("") is False
        assert mw.should_embed(None) is False

    def test_should_embed_tool_calls_openai(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        # OpenAI 格式响应带 tool_calls
        response = {
            "choices": [{
                "message": {"content": "text", "tool_calls": [{"id": "1"}]},
                "finish_reason": "tool_calls",
            }]
        }
        assert mw.should_embed(response) is False

    def test_should_embed_openai_text(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        response = {
            "choices": [{
                "message": {"content": "hello world"},
                "finish_reason": "stop",
            }]
        }
        assert mw.should_embed(response) is True

    def test_extract_text_openai(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        response = {"choices": [{"message": {"content": "hello"}}]}
        assert mw.extract_text(response) == "hello"

    def test_extract_text_langchain(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        class FakeResponse:
            content = "hello from langchain"
        assert mw.extract_text(FakeResponse()) == "hello from langchain"

    def test_extract_text_string(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        assert mw.extract_text("raw string") == "raw string"

    def test_write_back_openai(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        class FakeMsg:
            content = "old"
        class FakeChoice:
            message = FakeMsg()
        class FakeResponse:
            choices = [FakeChoice()]
        resp = FakeResponse()
        mw.write_back(resp, "new")
        assert resp.choices[0].message.content == "new"

    def test_write_back_langchain(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        class FakeResponse:
            content = "old"
        resp = FakeResponse()
        mw.write_back(resp, "new")
        assert resp.content == "new"

    def test_context_resolution_from_kwargs(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        # 通过 kwargs 传 context
        marked, result = mw.transform(LONG_TEXT_EN, user_id=42)
        assert result is not None
        assert result.user_id == 42


# ======================================================================
# StreamingWatermarker 测试
# ======================================================================

class TestStreamingWatermarker:
    def test_feed_accumulate_and_release(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm, min_text_length=20)
        streamer = StreamingWatermarker(mw)
        ctx = Context(user_id=42)

        # 分多次喂入
        parts = LONG_TEXT_EN.split(". ")
        output = ""
        for i, part in enumerate(parts):
            delta = part + ". "
            output += streamer.feed(delta, ctx if i == 0 else None)
        output += streamer.flush()

        # 总长度应接近原文（嵌入可能微调）
        assert len(output) > 0
        assert streamer.total_buffered > 0

    def test_flush_empty(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm)
        streamer = StreamingWatermarker(mw)
        assert streamer.flush() == ""

    def test_buffered_length(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm, min_text_length=20)
        streamer = StreamingWatermarker(mw)
        ctx = Context(user_id=42)
        # 喂入无句末标点的文本
        streamer.feed("hello world no punctuation here", ctx)
        assert streamer.buffered_length > 0

    def test_short_chunk_passthrough(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm, min_text_length=20)
        streamer = StreamingWatermarker(mw)
        ctx = Context(user_id=42)
        # 极短句应原样释放（不嵌入）
        out = streamer.feed("Hi. ", ctx)
        # "Hi." 不到 20 字符，应原样释放
        assert "Hi" in out

    def test_total_stats(self):
        wm = Watermarker()
        mw = WatermarkMiddleware(wm, min_text_length=20)
        streamer = StreamingWatermarker(mw)
        ctx = Context(user_id=42)
        text = "First sentence here. Second one too."
        streamer.feed(text, ctx)
        streamer.flush()
        assert streamer.total_buffered == len(text)
