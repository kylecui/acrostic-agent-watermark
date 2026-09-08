"""proxy_ext：会话端点 + A/S 模式注册表护栏。"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI                       # noqa: E402
from fastapi.testclient import TestClient         # noqa: E402

from aawm.plugins.context import Context          # noqa: E402
from aawm_enterprise import AggregateWatermarker  # noqa: E402
from aawm_enterprise.proxy_ext import (           # noqa: E402
    ModeRegistry, mount_enterprise,
)


@pytest.fixture()
def client(agg, long_zh_doc):
    app = FastAPI()
    registry = mount_enterprise(app, agg)
    return TestClient(app), registry, agg, long_zh_doc


def test_append_and_close(client):
    tc, registry, agg, doc = client
    segs = [doc[i:i + len(doc) // 4] for i in range(0, len(doc), len(doc) // 4)][:4]
    for seg in segs:
        r = tc.post("/v1/aawm/buffer/task-42",
                    json={"text": seg, "user_id": 7})
        assert r.status_code == 200
        assert r.json()["text"] == seg            # 原文透传
    r = tc.post("/v1/aawm/buffer/task-42/close", json={"user_id": 7})
    assert r.status_code == 200
    body = r.json()
    assert not body["abstained"]
    assert body["reliability"] in ("high", "medium")
    assert body["watermarked_document"] != doc
    assert body["meta"]["session_id"] == "task-42"


def test_close_empty_404(client):
    tc, *_ = client
    r = tc.post("/v1/aawm/buffer/no-such/close", json={"user_id": 7})
    assert r.status_code == 404


def test_missing_fields_422(client):
    tc, *_ = client
    r = tc.post("/v1/aawm/buffer/s1", json={"text": "only text"})
    assert r.status_code == 422


def test_as_guard_sentence_blocks_buffer(client):
    """A/S 护栏：session 注册 sentence 后 buffer append → 409，不产生嵌入。"""
    tc, registry, agg, doc = client
    r = tc.put("/v1/aawm/mode/conv-1", json={"mode": "sentence"})
    assert r.status_code == 200
    r = tc.post("/v1/aawm/buffer/conv-1",
                json={"text": doc[:200], "user_id": 7})
    assert r.status_code == 409
    assert "dual-band" in r.json()["detail"]
    # 未产生任何缓冲
    assert agg._buffers.get(("conv-1", 7)) is None


def test_mode_registry_validation():
    reg = ModeRegistry()
    reg.register("s", "aggregate")
    assert reg.mode_of("s") == "aggregate"
    assert not reg.conflicts("s", "aggregate")
    assert reg.conflicts("s", "sentence")
    with pytest.raises(ValueError):
        reg.register("s", "bogus")
