"""proxy streamer_factory 注入点测试（v0.14 通用扩展点）。

覆盖：
- 默认（不注入）→ 句子级整流，行为与历史版本一致
- 注入 factory → 流式全文缓冲（feed 返回空、[DONE] flush 整篇嵌入）
- X-AAWM-Watermark-Mode: sentence → 强制整流（opt-out，factory 不被调用）

注意：本测试用本地 FakeFullStreamer 模拟 enterprise 的
FullTextWatermarker 接口——核心测试绝不 import aawm_enterprise
（依赖方向铁律，见 aawm-enterprise/tests/test_dependency_direction.py）。
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
from aawm.plugins.context import Context
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
    reg = UIDRegistry()
    reg.register("alice", uid=ALICE_UID)
    return Watermarker(keystore=KeyStore(master_key=MASTER_KEY),
                       registry=reg, codec_mode="default")


class FakeFullStreamer:
    """与 enterprise FullTextWatermarker 同接口的最小替身。

    feed 恒返空（缓冲不泄露）；flush 整篇嵌入（fail-open 原文）。
    """

    def __init__(self, middleware, calls):
        self._mw = middleware
        self._calls = calls
        self._buf = ""
        self._ctx = None
        calls.append("construct")

    def feed(self, delta, ctx=None):
        if ctx is not None:
            self._ctx = ctx
        self._buf += delta
        return ""

    def flush(self):
        text, self._buf = self._buf, ""
        if not text.strip():
            return ""
        marked, _ = self._mw.transform(text, self._ctx)
        return marked


def _sse_client(handler, streamer_factory=None, calls=None) -> TestClient:
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    app = create_proxy_app(
        _make_wm(),
        ProxyConfig(
            upstream_openai="http://fake-openai.test",
            upstream_anthropic="http://fake-anthropic.test",
            key_map={ALICE_KEY: ALICE_UID},
            streamer_factory=(streamer_factory
                              if streamer_factory is not None
                              else (lambda mw, _c=calls: FakeFullStreamer(mw, _c))
                              if calls is not None else None),
        ),
        http_client=async_client,
    )
    return TestClient(app)


def _openai_sse_handler() -> httpx.Response:
    words = LONG_TEXT.split(" ")
    chunks = []
    for i in range(0, len(words), 5):
        chunks.append({"id": "c1", "object": "chat.completion.chunk",
                       "choices": [{"index": 0,
                                    "delta": {"content": " ".join(words[i:i + 5]) + " "},
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
    return handler


def _collect_stream(client: TestClient, extra_headers: dict = None) -> str:
    headers = {"authorization": f"Bearer {ALICE_KEY}"}
    headers.update(extra_headers or {})
    with client.stream("POST", "/v1/chat/completions", json={
        "model": "gpt-4", "messages": [], "stream": True,
    }, headers=headers) as r:
        assert r.status_code == 200
        raw = b"".join(r.iter_raw())
    text = ""
    for line in raw.decode("utf-8").splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            ch = json.loads(line[6:])
            text += (ch["choices"][0].get("delta") or {}).get("content", "")
    return text


class TestStreamerFactory:
    def test_no_factory_sentence_streaming_unchanged(self):
        """默认（无 factory）：句子级整流，流中即时输出（历史行为）。"""
        c = _sse_client(_openai_sse_handler())
        text = _collect_stream(c)
        assert text.strip() == LONG_TEXT.strip() or len(text) > 500

    def test_factory_used_and_buffers_full_text(self):
        """注入 factory：feed 缓冲不泄露、flush 整篇嵌入。"""
        calls = []
        c = _sse_client(_openai_sse_handler(), calls=calls)
        text = _collect_stream(c)
        assert calls.count("construct") == 1
        assert text.strip()          # flush 尾部整段交付，不丢内容

    def test_sentence_header_opt_out(self):
        """sentence 头强制整流：factory 不被调用。"""
        calls = []
        factory = lambda mw: FakeFullStreamer(mw, calls)  # noqa: E731
        c = _sse_client(_openai_sse_handler(), streamer_factory=factory)
        text = _collect_stream(c, {"X-AAWM-Watermark-Mode": "sentence"})
        assert calls == []           # factory 未被实例化
        assert text.strip()

    def test_non_streaming_unaffected(self):
        """非流式路径不受 factory 影响（始终整段嵌入）。"""
        def handler(request):
            return httpx.Response(200, json={
                "id": "c1", "choices": [{"index": 0,
                                         "message": {"content": LONG_TEXT},
                                         "finish_reason": "stop"}]})

        c = _sse_client(handler)
        r = c.post("/v1/chat/completions", json={"messages": []},
                   headers={"authorization": f"Bearer {ALICE_KEY}"})
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"]
