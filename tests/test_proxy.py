"""代理网关测试：fake 上游（httpx MockTransport）双协议往返。

覆盖：OpenAI/Anthropic 非流式嵌入、SSE 流式句子级嵌入、key→UID 映射、
未映射 key 透传、tool_calls 不改、salt 归档、上游错误透传、兜底路由。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm.plugins import UIDRegistry, Watermarker
from aawm.plugins.keystore import KeyStore
from aawm.proxy import ProxyConfig, create_proxy_app

MASTER_KEY = bytes(range(32))
ALICE_KEY = "sk-aawm-alice"
ALICE_UID = 42
LONG_TEXT = (
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


def _make_wm() -> Watermarker:
    """带注册库（alice→42）的 Watermarker，trace 时可容错匹配用户。"""
    reg = UIDRegistry()
    reg.register("alice", uid=ALICE_UID)
    return Watermarker(keystore=KeyStore(master_key=MASTER_KEY), registry=reg)


def _make_client(handler, archive=None) -> TestClient:
    """MockTransport 上游 + TestClient。archive 可选 salt 归档路径。"""
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    app = create_proxy_app(
        _make_wm(),
        ProxyConfig(
            upstream_openai="http://fake-openai.test",
            upstream_anthropic="http://fake-anthropic.test",
            key_map={ALICE_KEY: ALICE_UID},
            salt_archive=archive,
        ),
        http_client=async_client,
    )
    return TestClient(app)


def _last_salt(archive) -> bytes:
    """从归档 JSONL 取最后一个 session_salt。"""
    lines = archive.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "salt 归档为空——嵌入未发生"
    return bytes.fromhex(json.loads(lines[-1])["session_salt"])


def _salts(archive) -> list:
    """归档全部 session_salt（按写入顺序）。"""
    lines = archive.read_text(encoding="utf-8").strip().splitlines()
    return [bytes.fromhex(json.loads(l)["session_salt"]) for l in lines]


def _openai_json_handler(body: str = LONG_TEXT):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": body}}],
        })
    return handler


def _anthropic_json_handler(body: str = LONG_TEXT):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "msg_1", "role": "assistant", "type": "message",
            "content": [{"type": "text", "text": body}],
        })
    return handler


def _sse_lines(events: list) -> bytes:
    out = []
    for ev in events:
        out.append(f"data: {json.dumps(ev)}\n\n")
    return "".join(out).encode("utf-8")


# ======================================================================
# OpenAI 协议
# ======================================================================

class TestOpenAIProtocol:

    def test_nonstream_embeds_and_key_maps_uid(self, tmp_path: Path):
        archive = tmp_path / "salts.jsonl"
        c = _make_client(_openai_json_handler(), archive)
        r = c.post("/v1/chat/completions", json={
            "model": "gpt-4", "messages": [{"role": "user", "content": "hi"}],
        }, headers={"authorization": f"Bearer {ALICE_KEY}"})
        assert r.status_code == 200
        body = r.json()
        text = body["choices"][0]["message"]["content"]
        assert text != LONG_TEXT

        # 用归档 salt 溯源 → 命中 alice 的 UID
        trace = _make_wm().trace(text, session_salt=_last_salt(archive))
        assert trace.watermarked and trace.user == "alice"

    def test_unmapped_key_passes_through(self):
        c = _make_client(_openai_json_handler())
        r = c.post("/v1/chat/completions", json={
            "model": "gpt-4", "messages": [{"role": "user", "content": "hi"}],
        }, headers={"authorization": "Bearer sk-stranger"})
        text = r.json()["choices"][0]["message"]["content"]
        assert text == LONG_TEXT  # 无 UID → 不嵌入

    def test_tool_calls_response_untouched(self):
        def handler(request):
            return httpx.Response(200, json={
                "choices": [{"index": 0, "finish_reason": "tool_calls",
                             "message": {"role": "assistant", "content": None,
                                         "tool_calls": [{"id": "t1", "function":
                                         {"name": "run", "arguments": "{}"}}]}}],
            })
        c = _make_client(handler)
        r = c.post("/v1/chat/completions", json={
            "model": "gpt-4", "messages": [],
        }, headers={"authorization": f"Bearer {ALICE_KEY}"})
        assert r.json()["choices"][0]["message"]["tool_calls"][0][
            "function"]["arguments"] == "{}"

    def test_stream_sse_embeds_sentences(self, tmp_path: Path):
        archive = tmp_path / "salts.jsonl"
        words = LONG_TEXT.split(" ")
        chunks = []
        for i in range(0, len(words), 5):
            piece = " ".join(words[i:i + 5]) + " "
            chunks.append({"id": "c1", "object": "chat.completion.chunk",
                           "choices": [{"index": 0,
                                        "delta": {"content": piece},
                                        "finish_reason": None}]})
        chunks.append({"id": "c1", "object": "chat.completion.chunk",
                       "choices": [{"index": 0, "delta": {},
                                    "finish_reason": "stop"}]})

        def handler(request):
            body = b""
            for ch in chunks:
                body += f"data: {json.dumps(ch)}\n\n".encode()
            body += b"data: [DONE]\n\n"
            return httpx.Response(200, content=body,
                                  headers={"content-type": "text/event-stream"})

        c = _make_client(handler, archive)
        with c.stream("POST", "/v1/chat/completions", json={
            "model": "gpt-4", "messages": [], "stream": True,
        }, headers={"authorization": f"Bearer {ALICE_KEY}"}) as r:
            assert r.status_code == 200
            raw = b"".join(r.iter_raw())

        # 解析全部 data 行，拼回复文本
        text = ""
        for line in raw.decode("utf-8").splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                ch = json.loads(line[6:])
                text += (ch["choices"][0].get("delta") or {}).get("content", "")
        assert text.strip()  # 尾句 flush 后不丢内容
        trace = _make_wm().trace(text, session_salt=_last_salt(archive))
        assert trace.watermarked and trace.user == "alice"

    def test_upstream_error_passthrough(self):
        def handler(request):
            return httpx.Response(503, json={"error": "upstream down"})
        c = _make_client(handler)
        r = c.post("/v1/chat/completions", json={"messages": []},
                   headers={"authorization": f"Bearer {ALICE_KEY}"})
        assert r.status_code == 503
        assert r.json()["error"] == "upstream down"


# ======================================================================
# Anthropic 协议
# ======================================================================

class TestAnthropicProtocol:

    def test_nonstream_embeds_with_xapikey(self, tmp_path: Path):
        archive = tmp_path / "salts.jsonl"
        c = _make_client(_anthropic_json_handler(), archive)
        r = c.post("/v1/messages", json={
            "model": "claude-3", "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        }, headers={"x-api-key": ALICE_KEY, "anthropic-version": "2023-06-01"})
        assert r.status_code == 200
        text = r.json()["content"][0]["text"]
        trace = _make_wm().trace(text, session_salt=_last_salt(archive))
        assert trace.watermarked and trace.user == "alice"

    def test_stream_sse_embeds(self, tmp_path: Path):
        archive = tmp_path / "salts.jsonl"
        words = LONG_TEXT.split(" ")
        events = [
            {"type": "message_start", "message": {"role": "assistant"}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
        ]
        for i in range(0, len(words), 5):
            events.append({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta",
                                     "text": " ".join(words[i:i + 5]) + " "}})
        events += [
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]

        def handler(request):
            return httpx.Response(200, content=_sse_lines(events),
                                  headers={"content-type": "text/event-stream"})

        c = _make_client(handler, archive)
        with c.stream("POST", "/v1/messages", json={
            "model": "claude-3", "messages": [], "stream": True,
        }, headers={"x-api-key": ALICE_KEY}) as r:
            raw = b"".join(r.iter_raw())

        text = ""
        for line in raw.decode("utf-8").splitlines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                if ev.get("type") == "content_block_delta":
                    text += ev["delta"].get("text", "")
        trace = _make_wm().trace(text, session_salt=_last_salt(archive))
        assert trace.watermarked and trace.user == "alice"

    def test_tool_use_block_untouched(self, tmp_path: Path):
        archive = tmp_path / "salts.jsonl"

        def handler(request):
            return httpx.Response(200, json={
                "content": [
                    {"type": "text", "text": LONG_TEXT},
                    {"type": "tool_use", "id": "tu1", "name": "run",
                     "input": {"cmd": "ls"}},
                ],
            })
        c = _make_client(handler, archive)
        r = c.post("/v1/messages", json={"messages": []},
                   headers={"x-api-key": ALICE_KEY})
        body = r.json()
        assert body["content"][1]["input"] == {"cmd": "ls"}
        trace = _make_wm().trace(body["content"][0]["text"],
                                 session_salt=_last_salt(archive))
        assert trace.watermarked


# ======================================================================
# OpenAI Responses 协议（Codex / 新版 opencode 走 /v1/responses）
# ======================================================================

class TestResponsesProtocol:

    def test_nonstream_embeds_output_text(self, tmp_path: Path):
        archive = tmp_path / "salts.jsonl"

        def handler(request):
            return httpx.Response(200, json={
                "id": "resp_1", "object": "response",
                "output": [{
                    "type": "message", "id": "msg_1",
                    "content": [{"type": "output_text", "text": LONG_TEXT,
                                 "annotations": []}],
                }],
                "output_text": LONG_TEXT,
            })
        c = _make_client(handler, archive)
        r = c.post("/v1/responses", json={
            "model": "gpt-5", "input": "hi",
        }, headers={"authorization": f"Bearer {ALICE_KEY}"})
        assert r.status_code == 200
        body = r.json()
        out_text = body["output"][0]["content"][0]["text"]
        top_text = body["output_text"]
        assert out_text != LONG_TEXT and top_text != LONG_TEXT

        # 两个字段各自独立嵌入 → 归档两条，各自可溯源
        s = _salts(archive)
        assert len(s) == 2
        assert _make_wm().trace(out_text, session_salt=s[0]).watermarked
        assert _make_wm().trace(top_text, session_salt=s[1]).watermarked

    def test_unmapped_key_passes_through(self):
        def handler(request):
            return httpx.Response(200, json={
                "id": "resp_1", "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": LONG_TEXT}],
                }],
            })
        c = _make_client(handler)
        r = c.post("/v1/responses", json={"input": "hi"},
                   headers={"authorization": "Bearer sk-stranger"})
        body = r.json()
        assert body["output"][0]["content"][0]["text"] == LONG_TEXT

    def test_stream_sse_embeds_and_flushes_before_completed(self,
                                                            tmp_path: Path):
        archive = tmp_path / "salts.jsonl"
        words = LONG_TEXT.split(" ")
        events = [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_item.added",
             "item_id": "msg_1", "output_index": 0},
        ]
        for i in range(0, len(words), 5):
            events.append({
                "type": "response.output_text.delta",
                "item_id": "msg_1", "output_index": 0, "content_index": 0,
                "delta": " ".join(words[i:i + 5]) + " ",
            })
        events += [
            {"type": "response.output_text.done", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "text": LONG_TEXT},
            {"type": "response.output_item.done", "item_id": "msg_1",
             "output_index": 0},
            {"type": "response.completed", "response": {"id": "resp_1"}},
        ]

        def handler(request):
            return httpx.Response(200, content=_sse_lines(events),
                                  headers={"content-type": "text/event-stream"})

        c = _make_client(handler, archive)
        with c.stream("POST", "/v1/responses", json={
            "model": "gpt-5", "input": "hi", "stream": True,
        }, headers={"authorization": f"Bearer {ALICE_KEY}"}) as r:
            assert r.status_code == 200
            raw = b"".join(r.iter_raw())

        text = ""
        for line in raw.decode("utf-8").splitlines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                if ev.get("type") == "response.output_text.delta":
                    text += ev.get("delta", "")
        assert text.strip()  # completed 前 flush 尾句，不丢内容
        trace = _make_wm().trace(text, session_salt=_last_salt(archive))
        assert trace.watermarked and trace.user == "alice"


# ======================================================================
# 其他
# ======================================================================

class TestProxyMisc:

    def test_salt_archive_jsonl(self, monkeypatch, tmp_path: Path):
        archive = tmp_path / "salts.jsonl"
        transport = httpx.MockTransport(_openai_json_handler())
        app = create_proxy_app(
            _make_wm(),
            ProxyConfig(key_map={ALICE_KEY: ALICE_UID},
                        salt_archive=archive),
            http_client=httpx.AsyncClient(transport=transport),
        )
        c = TestClient(app)
        c.post("/v1/chat/completions", json={"messages": []},
               headers={"authorization": f"Bearer {ALICE_KEY}"})
        lines = archive.read_text(encoding="utf-8").strip().splitlines()
        assert lines
        rec = json.loads(lines[0])
        assert rec["uid"] == ALICE_UID
        assert len(rec["session_salt"]) > 0

    def test_passthrough_other_paths(self):
        def handler(request):
            return httpx.Response(200, json={"models": ["gpt-4"]})
        c = _make_client(handler)
        r = c.get("/v1/models", headers={"authorization": f"Bearer {ALICE_KEY}"})
        assert r.status_code == 200
        assert r.json() == {"models": ["gpt-4"]}

    def test_real_upstream_key_substitution(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            seen["xapikey"] = request.headers.get("x-api-key")
            return _openai_json_handler()(request)
        transport = httpx.MockTransport(handler)
        app = create_proxy_app(
            _make_wm(),
            ProxyConfig(key_map={ALICE_KEY: ALICE_UID},
                        upstream_openai_key="sk-real-openai-key"),
            http_client=httpx.AsyncClient(transport=transport),
        )
        c = TestClient(app)
        c.post("/v1/chat/completions", json={"messages": []},
               headers={"authorization": f"Bearer {ALICE_KEY}"})
        assert seen["auth"] == "Bearer sk-real-openai-key"
        assert seen["xapikey"] is None
