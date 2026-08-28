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
                "--codec-mode", "default",  # LONG_TEXT 通用英文→default 兼容路径
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

    def test_find_meta(self, tmp_path):
        """find-meta：段哈希锁定正确 meta + 信道 B 验证（含干扰 meta）。"""
        from tests.test_e2e_integration import _long_zh_text

        key_file = tmp_path / "key.json"
        self._run_cli("keygen", "--output", str(key_file))

        target = tmp_path / "target.txt"
        target.write_text(_long_zh_text(), encoding="utf-8")
        marked = tmp_path / "marked.txt"
        result = self._run_cli(
            "embed", str(target), "--key", str(key_file),
            "--user", "7", "--codec-mode", "zero_cost", "-o", str(marked))
        assert result.returncode == 0, result.stderr
        target_meta = marked.with_suffix(".meta.json")
        assert target_meta.exists()

        # 干扰 meta：另一段无关文本
        other = tmp_path / "other.txt"
        other.write_text(LONG_TEXT, encoding="utf-8")
        other_marked = tmp_path / "other_marked.txt"
        self._run_cli("embed", str(other), "--key", str(key_file),
                      "--user", "8", "-o", str(other_marked))
        other_meta = other_marked.with_suffix(".meta.json")

        metas_dir = tmp_path / "metas"
        metas_dir.mkdir()
        for m in (target_meta, other_meta):
            (metas_dir / m.name).write_text(
                m.read_text(encoding="utf-8"), encoding="utf-8")

        result = self._run_cli(
            "find-meta", str(marked), str(metas_dir),
            "--key", str(key_file), "--codec-mode", "zero_cost")
        assert result.returncode == 0, result.stdout + result.stderr
        # 段哈希命中正确 meta（排名首位）
        assert target_meta.name in result.stdout
        assert "命中" in result.stdout
        # 信道 B 验证解出 UID
        assert "UID=0x0007" in result.stdout

    def test_find_meta_tampered(self, tmp_path):
        """find-meta：文本被改写后仍能锁定 meta 并判定篡改段落。"""
        from tests.test_e2e_integration import _long_zh_text

        key_file = tmp_path / "key.json"
        self._run_cli("keygen", "--output", str(key_file))

        target = tmp_path / "target.txt"
        target.write_text(_long_zh_text(), encoding="utf-8")
        marked = tmp_path / "marked.txt"
        self._run_cli("embed", str(target), "--key", str(key_file),
                      "--user", "7", "--codec-mode", "zero_cost",
                      "-o", str(marked))
        meta_file = marked.with_suffix(".meta.json")

        # 改写第一段模拟泄露后被编辑
        paras = marked.read_text(encoding="utf-8").split("\n\n")
        paras[0] = paras[0] + "（后被补写的段落）"
        suspect = tmp_path / "suspect.txt"
        suspect.write_text("\n\n".join(paras), encoding="utf-8")

        result = self._run_cli(
            "find-meta", str(suspect), str(meta_file),
            "--key", str(key_file), "--codec-mode", "zero_cost")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "篡改判定: 是" in result.stdout
        assert "被改段落: [0]" in result.stdout

    def test_find_meta_jsonl_archive(self, tmp_path):
        """find-meta：proxy salt-archive JSONL（每行一条）也能反查。"""
        from tests.test_e2e_integration import _long_zh_text

        key_file = tmp_path / "key.json"
        self._run_cli("keygen", "--output", str(key_file))

        target = tmp_path / "target.txt"
        target.write_text(_long_zh_text(), encoding="utf-8")
        marked = tmp_path / "marked.txt"
        self._run_cli("embed", str(target), "--key", str(key_file),
                      "--user", "7", "--codec-mode", "zero_cost",
                      "-o", str(marked))
        target_meta = json.loads(
            marked.with_suffix(".meta.json").read_text(encoding="utf-8"))

        # 干扰行：另一段无关文本的 meta
        other = tmp_path / "other.txt"
        other.write_text(LONG_TEXT, encoding="utf-8")
        other_marked = tmp_path / "other_marked.txt"
        self._run_cli("embed", str(other), "--key", str(key_file),
                      "--user", "8", "-o", str(other_marked))
        other_meta = json.loads(
            other_marked.with_suffix(".meta.json").read_text(encoding="utf-8"))

        # 模拟 proxy salt-archive：JSONL 每行一条（含 uid 字段名差异）
        def to_archive_rec(meta):
            rec = dict(meta)
            rec["uid"] = rec.pop("user_id")
            rec["ts"] = 0
            return rec

        archive = tmp_path / "salts.jsonl"
        archive.write_text("\n".join(
            json.dumps(to_archive_rec(m), ensure_ascii=False)
            for m in (other_meta, target_meta)) + "\n", encoding="utf-8")

        result = self._run_cli(
            "find-meta", str(marked), str(archive),
            "--key", str(key_file), "--codec-mode", "zero_cost")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "命中" in result.stdout
        assert "UID=0x0007" in result.stdout
        # JSONL 记录标签带行号，正确记录是第 2 行
        assert "salts.jsonl:2" in result.stdout

    # ------------------------------------------------------------------
    # find-meta 最终裁决（_adjudicate_find_meta）单元测试：
    # 攻击下"存在性盐无关"——错误盐也会检出；裁决必须用段哈希内容
    # 证据 + 存档 UID 交叉校验，不确定即 abstain（VERIFICATION_REPORT
    # §8.2：+20% 同义替换曾输出 "匹配 meta=doc_00, UID=0x0000" 错误结论）。
    # ------------------------------------------------------------------

    def _mk_trace(self, *, watermarked=True, uid=7, n_bits=8, ac=0.8,
                  abstain=False):
        from aawm.plugins.facade import TraceResult
        return TraceResult(
            watermarked=watermarked,
            uid=None if (not watermarked or abstain) else uid,
            user=None,
            hamming_dist=-1,
            confidence=0.8,
            existence_score=30.0,
            tampered=None,
            n_bits=n_bits if watermarked else 0,
            attribution_confidence=ac,
            attribution_abstain=abstain,
        )

    def test_adjudicate_clean_match(self):
        """干净文本：段哈希命中 + 解码 UID 与存档一致 → match。"""
        from aawm.cli import _adjudicate_find_meta
        ranked = [(3, 5, "doc_00", {})]
        detections = [(3, "doc_00", self._mk_trace(uid=7), 7)]
        kind, label, t, reason = _adjudicate_find_meta(ranked, detections)
        assert kind == "match" and label == "doc_00"

    def test_adjudicate_uid_distortion_abstains(self):
        """正确 meta 解码 UID 失真（解码 0 ≠ 存档 17）→ abstain，
        绝不输出可能错误的 UID（报告 §8.2 的 UID=0x0000 形态）。"""
        from aawm.cli import _adjudicate_find_meta
        ranked = [(3, 5, "doc_00", {})]
        detections = [(3, "doc_00", self._mk_trace(uid=0), 17)]
        kind, label, t, reason = _adjudicate_find_meta(ranked, detections)
        assert kind == "abstain" and label == "doc_00"
        assert "失真" in reason

    def test_adjudicate_uid_mask_alias_ok(self):
        """自适应 k-bit 掩码对齐视为一致（解码低 n_bits 位等于存档截断值）。"""
        from aawm.cli import _adjudicate_find_meta
        # 存档 0x1234，n_bits=8，解码 0x34 → 掩码对齐
        ranked = [(3, 5, "doc_00", {})]
        detections = [(3, "doc_00", self._mk_trace(uid=0x34, n_bits=8), 0x1234)]
        kind, label, t, reason = _adjudicate_find_meta(ranked, detections)
        assert kind == "match"

    def test_adjudicate_hash_priority_over_false_detect(self):
        """内容证据优先：无内容证据的错误 meta 检出，不改判给它——
        宁可 abstain 在段哈希锁定的 meta 上。"""
        from aawm.cli import _adjudicate_find_meta
        ranked = [
            (4, 5, "doc_00", {}),   # 内容命中，但 trace 未检出（重度改写）
            (0, 5, "doc_01", {}),   # 无内容证据，却"检出"且 UID 与存档一致
        ]
        detections = [(0, "doc_01", self._mk_trace(uid=0x22), 0x22)]
        kind, label, t, reason = _adjudicate_find_meta(ranked, detections)
        assert kind == "abstain" and label == "doc_00"
        assert "水印未检出" in reason

    def test_adjudicate_no_hash_multi_detect_conflict(self):
        """无段哈希 + 多候选检出且 UID 均失真 → abstain（错误盐巧合风险）。"""
        from aawm.cli import _adjudicate_find_meta
        ranked = [(0, 5, "doc_00", {}), (0, 5, "doc_01", {})]
        detections = [
            (0, "doc_00", self._mk_trace(uid=0x11), 0x22),
            (0, "doc_01", self._mk_trace(uid=0x33), 0x44),
        ]
        kind, label, t, reason = _adjudicate_find_meta(ranked, detections)
        assert kind == "abstain" and label is None
        assert "无法区分真伪" in reason

    def test_adjudicate_no_hash_single_match(self):
        """无 seal（--no-sign）meta：唯一检出且解码与存档一致 → match。"""
        from aawm.cli import _adjudicate_find_meta
        ranked = [(0, 5, "doc_00", {})]
        detections = [(0, "doc_00", self._mk_trace(uid=7), 7)]
        kind, label, t, reason = _adjudicate_find_meta(ranked, detections)
        assert kind == "match" and label == "doc_00"

    def test_find_meta_syn20_scene_no_wrong_uid(self):
        """场景回归护栏：多候选攻击场景下，CLI find-meta 绝不得输出
        与存档 UID 不一致的错误 UID（VERIFICATION_REPORT §8.2 的
        "匹配 meta=doc_00, UID=0x0000" 形态）——要么正确、要么不可判定。"""
        from aawm.cli import _cmd_find_meta
        from aawm.plugins.facade import TraceResult

        # 构造 3 份 meta 存档：doc_00 为正确来源（存档 UID=7），
        # 其余为干扰。嫌疑文本来自 doc_00 但被重写（无段哈希命中）。
        # 正确 meta trace 解码失真（uid=0≠7，存在性仍存活——攻击典型形态），
        # 干扰 meta 检出且 UID 与各自存档"碰巧一致"（错误盐巧合）。
        # 裁决必须 abstain 而非取第一个检出。
        detections = [
            (0, "doc_00", TraceResult(
                watermarked=True, uid=0, user=None, hamming_dist=-1,
                confidence=0.8, existence_score=30.0, tampered=None,
                n_bits=8, attribution_confidence=0.8,
                attribution_abstain=False), 7),
            (0, "doc_01", TraceResult(
                watermarked=True, uid=0x22, user=None, hamming_dist=-1,
                confidence=0.6, existence_score=20.0, tampered=None,
                n_bits=8, attribution_confidence=0.6,
                attribution_abstain=False), 0x22),
        ]
        ranked = [
            (0, 5, "doc_00", {}),
            (0, 5, "doc_01", {}),
            (0, 5, "doc_02", {}),
        ]
        from aawm.cli import _adjudicate_find_meta
        kind, label, t, reason = _adjudicate_find_meta(ranked, detections)
        assert kind == "abstain"
        # 关键：绝不落到"检出且存档一致"的错误 meta 上
        assert label is None or label != "doc_01"

    def test_trace_meta_uid_mismatch_abstains(self, tmp_path):
        """trace --meta 盐外证据校验：解码 UID 与存档 UID 不一致 → 输出
        "不可判定" exit 3，绝不输出可能错误的 UID
        （VERIFICATION_REPORT §8.2：干净泄露仍 UID 误解码 0x14≠117）。"""
        from tests.test_e2e_integration import _long_zh_text

        key_file = tmp_path / "key.json"
        self._run_cli("keygen", "--output", str(key_file))

        target = tmp_path / "target.txt"
        target.write_text(_long_zh_text(), encoding="utf-8")
        marked = tmp_path / "marked.txt"
        self._run_cli("embed", str(target), "--key", str(key_file),
                      "--user", "7", "--codec-mode", "zero_cost",
                      "-o", str(marked))
        meta_file = marked.with_suffix(".meta.json")

        # 篡改存档 UID（8 ≠ 真实 7）→ 解码交叉校验必须 abstain
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["user_id"] = 8
        bad_meta = tmp_path / "bad.meta.json"
        bad_meta.write_text(json.dumps(meta), encoding="utf-8")

        r = self._run_cli("trace", str(marked), "--key", str(key_file),
                          "--meta", str(bad_meta))
        assert r.returncode == 3, r.stdout + r.stderr
        assert "不可判定" in r.stdout
        # 关键：绝不输出错误 UID
        assert "解码 UID" not in r.stdout

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

        wm = Watermarker(codec_mode="default")  # LONG_TEXT 通用英文→default 兼容路径
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
    async def test_trace_archived_uid_mismatch_abstains(self, app_client):
        """/v1/trace 盐外证据校验：archived_uid 与解码 UID 不一致 → abstain
        （绝不输出可能错误的 UID；修复报告 §8.2 干净泄露 UID 误解码）。"""
        from aawm.plugins import Watermarker
        from aawm.server.api import set_watermarker
        from tests.test_e2e_integration import _long_zh_text

        wm = Watermarker(codec_mode="zero_cost")
        set_watermarker(wm)
        r = wm.embed(_long_zh_text(), user_id=7)

        _, client = app_client
        try:
            # archived_uid=8 与真实 7 不一致（任意掩码下 7&mask ≠ 8&mask）
            resp = await client.post("/v1/trace", json={
                "text": r.watermarked_text,
                "session_salt": r.session_salt.hex(),
                "bands": r.bands,
                "n_bits": r.n_bits,
                "archived_uid": 8,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["attribution_abstain"] is True
            assert data["uid"] is None
            assert data["user"] is None
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_trace_archived_uid_match_ok(self, app_client):
        """/v1/trace 盐外证据校验：archived_uid 与解码一致 → 正常归因。"""
        from aawm.plugins import Watermarker
        from aawm.server.api import set_watermarker
        from tests.test_e2e_integration import _long_zh_text

        wm = Watermarker(codec_mode="zero_cost")
        set_watermarker(wm)
        r = wm.embed(_long_zh_text(), user_id=7)

        _, client = app_client
        try:
            resp = await client.post("/v1/trace", json={
                "text": r.watermarked_text,
                "session_salt": r.session_salt.hex(),
                "bands": r.bands,
                "n_bits": r.n_bits,
                "archived_uid": 7,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["watermarked"] is True
            assert data["attribution_abstain"] is False
            assert data["uid"] == 7 & ((1 << r.n_bits) - 1)
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

    @pytest.mark.asyncio
    async def test_find_meta_endpoint(self, app_client):
        """find-meta：多候选中锁定正确存档（段哈希 + 信道 B）。"""
        from aawm.plugins import Watermarker
        from aawm.server.api import set_watermarker
        from tests.test_e2e_integration import _long_zh_text

        wm = Watermarker(codec_mode="zero_cost")
        set_watermarker(wm)

        # 目标存档：zh 文本嵌入（含 seal）
        r_target = wm.embed(_long_zh_text(), user_id=7)
        # 干扰存档：英文文本
        wm_en = Watermarker()
        r_other = wm_en.embed(LONG_TEXT, user_id=8)

        def cand(result, label):
            from aawm.binding import split_paragraphs
            import hashlib as _hl
            return {
                "session_salt": result.session_salt.hex(),
                "bands": result.bands,
                "n_bits": result.n_bits,
                "codec_mode": result.codec_mode,
                "seal": None if result.seal is None else {
                    "merkle_root": result.seal.merkle_root.hex(),
                    "para_hashes": [h.hex() for h in result.seal.para_hashes],
                    "aad": result.seal.aad.hex(),
                    "version": result.seal.version,
                },
                "label": label,
            }

        _, client = app_client
        try:
            resp = await client.post("/v1/find-meta", json={
                "text": r_target.watermarked_text,
                "candidates": [
                    cand(r_other, "other"),
                    cand(r_target, "target"),
                ],
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["watermarked"] is True
            assert data["matched_label"] == "target"
            assert data["para_overlap"] >= 1
            assert data["uid"] == 7 & ((1 << r_target.n_bits) - 1)
        finally:
            await client.aclose()
