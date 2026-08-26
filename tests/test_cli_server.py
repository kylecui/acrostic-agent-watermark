"""CLI + Server 测试。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


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
    "reviewer can always find the root cause of a hard problem."
)


# ======================================================================
# CLI 测试（subprocess 调用）
# ======================================================================

class TestCLI:
    def _run_cli(self, *args, input_text=None):
        """运行 aawm CLI。"""
        cmd = [sys.executable, "-m", "aawm.cli"] + list(args)
        src_path = str(Path(__file__).resolve().parents[1] / "src")
        env = {**os.environ, "PYTHONPATH": src_path}
        result = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            env=env,
        )
        return result

    def test_keygen_hex(self, tmp_path):
        result = self._run_cli("keygen")
        assert result.returncode == 0
        # hex 输出
        key_hex = result.stdout.strip()
        assert len(bytes.fromhex(key_hex)) == 32

    def test_keygen_file(self, tmp_path):
        key_file = tmp_path / "key.json"
        result = self._run_cli("keygen", "--output", str(key_file))
        assert result.returncode == 0
        assert key_file.exists()
        data = json.loads(key_file.read_text())
        assert "master_key" in data

    def test_registry_add_list_find(self, tmp_path):
        reg_file = tmp_path / "registry.json"
        # add
        result = self._run_cli("registry", "add", "alice", "--registry", str(reg_file))
        assert result.returncode == 0
        assert "alice" in result.stdout
        # list
        result = self._run_cli("registry", "list", "--registry", str(reg_file))
        assert result.returncode == 0
        assert "alice" in result.stdout
        # find
        result = self._run_cli("registry", "find", "1", "--registry", str(reg_file))
        assert result.returncode == 0
        assert "alice" in result.stdout

    def test_embed_trace_roundtrip(self, tmp_path):
        key_file = tmp_path / "key.json"
        self._run_cli("keygen", "--output", str(key_file))
        reg_file = tmp_path / "registry.json"
        self._run_cli("registry", "add", "agent-cuiyin", "--uid", "4660",
                       "--registry", str(reg_file))
        input_file = tmp_path / "input.txt"
        input_file.write_text(LONG_TEXT, encoding="utf-8")

        # 嵌入+溯源可能因统计检测概率性失败，重试至多 5 次
        for attempt in range(5):
            output_file = tmp_path / f"marked_{attempt}.txt"
            result = self._run_cli(
                "embed",
                str(input_file),
                "--key", str(key_file),
                "--user", "agent-cuiyin",
                "--registry", str(reg_file),
                "-o", str(output_file),
            )
            assert result.returncode == 0
            assert output_file.exists()
            meta_file = output_file.with_suffix(".meta.json")
            assert meta_file.exists()
            # trace
            result = self._run_cli(
                "trace",
                str(output_file),
                "--key", str(key_file),
                "--registry", str(reg_file),
                "--meta", str(meta_file),
            )
            if result.returncode == 0 and "是" in result.stdout and "agent-cuiyin" in result.stdout:
                return
        assert False, f"embed+trace roundtrip failed after 5 attempts\n{result.stdout}\n{result.stderr}"

    def test_embed_trace_adaptive_roundtrip(self, tmp_path):
        """zero_cost/hybrid 模式 CLI 端到端（--codec-mode + --calibrate-corpus）。"""
        from tests.test_e2e_integration import _long_zh_text

        key_file = tmp_path / "key.json"
        self._run_cli("keygen", "--output", str(key_file))
        input_file = tmp_path / "input.txt"
        input_file.write_text(_long_zh_text(), encoding="utf-8")
        supp_file = tmp_path / "supp.json"
        supp_file.write_text(json.dumps(
            {"这个": ["此个", "这一"], "因为": ["由于"], "所以": ["因此"]},
            ensure_ascii=False), encoding="utf-8")

        for mode, extra in (("zero_cost", []),
                            ("hybrid", ["--supplementary-dict", str(supp_file)])):
            output_file = tmp_path / f"marked_{mode}.txt"
            result = self._run_cli(
                "embed", str(input_file), "--key", str(key_file),
                "--user", "42", "--codec-mode", mode, *extra,
                "-o", str(output_file))
            assert result.returncode == 0, result.stderr
            meta_file = output_file.with_suffix(".meta.json")
            assert meta_file.exists()
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            assert meta["codec_mode"] == mode
            assert meta["bands"], f"{mode} 应返回 bands"

            result = self._run_cli(
                "trace", str(output_file), "--key", str(key_file),
                "--meta", str(meta_file), "--codec-mode", mode, *extra)
            assert result.returncode == 0, result.stderr
            assert "检出水印: 是" in result.stdout

    def test_embed_trace_stdin(self, tmp_path):
        key_file = tmp_path / "key.json"
        self._run_cli("keygen", "--output", str(key_file))
        # embed via stdin
        result = self._run_cli(
            "embed", "-",
            "--key", str(key_file),
            "--user", "42",
            input_text=LONG_TEXT,        )
        assert result.returncode == 0
        marked_text = result.stdout
        # trace via stdin（需要 salt，先从 stderr 拿不到，直接传文本）
        # 这里只验证嵌入成功
        assert marked_text != LONG_TEXT


# ======================================================================
# Server 测试（httpx AsyncClient）
# ======================================================================

class TestServer:
    @pytest.fixture
    def app_client(self):
        """创建测试用 app + client（每个测试独立设置 watermarker）。"""
        from aawm.plugins import Watermarker
        from aawm.server.api import create_app, set_watermarker

        set_watermarker(Watermarker())
        app = create_app()
        from httpx import ASGITransport, AsyncClient
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")
        yield app, client

    @pytest.mark.asyncio
    async def test_health(self, app_client):
        _, client = app_client
        try:
            resp = await client.get("/v1/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["watermarker_initialized"] is True
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_trace_endpoint(self, app_client):
        from aawm.plugins import Watermarker
        from aawm.server.api import set_watermarker

        wm = Watermarker()
        result = wm.embed(LONG_TEXT, user_id=42)
        set_watermarker(wm)

        _, client = app_client
        try:
            resp = await client.post("/v1/trace", json={
                "text": result.watermarked_text,
                "session_salt": result.session_salt.hex(),
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["watermarked"] is True
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_trace_null_text(self, app_client):
        from aawm.plugins import Watermarker
        from aawm.server.api import set_watermarker
        set_watermarker(Watermarker())
        _, client = app_client
        try:
            false_count = 0
            for _ in range(5):
                resp = await client.post("/v1/trace", json={"text": LONG_TEXT})
                assert resp.status_code == 200
                if not resp.json()["watermarked"]:
                    false_count += 1
            assert false_count >= 3
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_embed_endpoint(self, app_client):
        _, client = app_client
        try:
            resp = await client.post("/v1/embed", json={
                "text": LONG_TEXT,
                "user_id": 42,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "watermarked_text" in data
            assert "session_salt" in data
            assert data["user_id"] == 42
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_embed_trace_adaptive_roundtrip(self, app_client):
        """zero_cost 模式经 server API 的端到端往返（bands/n_bits 回传）。"""
        from aawm.plugins import Watermarker
        from aawm.server.api import set_watermarker
        from tests.test_e2e_integration import _long_zh_text

        wm = Watermarker(codec_mode="zero_cost")
        set_watermarker(wm)

        _, client = app_client
        try:
            resp = await client.post("/v1/embed", json={
                "text": _long_zh_text(),
                "user_id": 42,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["codec_mode"] == "zero_cost"
            assert data["bands"], "自适应模式应返回 bands"
            assert data["n_bits"] >= 1

            resp2 = await client.post("/v1/trace", json={
                "text": data["watermarked_text"],
                "session_salt": data["session_salt"],
                "bands": data["bands"],
                "n_bits": data["n_bits"],
            })
            assert resp2.status_code == 200
            t = resp2.json()
            assert t["watermarked"] is True
            assert t["codec_mode"] == "zero_cost"
            assert t["active_bands"] >= 1
            assert t["n_bits"] == data["n_bits"]
            assert t["uid"] == 42 & ((1 << data["n_bits"]) - 1)
        finally:
            await client.aclose()
