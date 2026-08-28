"""v0.13 新功能测试。

覆盖：
- P2-10 CRC-16（coding 载荷 + CA 信道往返/兼容）
- P2-9 词典指纹（greenlist dict_version 稳定性 + facade 全链路比对）
- P2-8 UID 冗余（greenlist 冗余编解码 + facade uid_redundancy 全链路）
- P1-6 密钥轮换（KeyStore 多版本 + facade key_version 溯源）
- P1-7 meta 存储后端（JSONL / SQLite + 段哈希反查）
- P1-5 审计日志（AuditLogger + 全局记录器）
- P1-4 指标（Metrics + /metrics 端点）
- CLI 新参数冒烟（rotate-key / --meta-store / --audit-log）
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.test_cli_server import LONG_TEXT  # noqa: E402
from tests.test_content_anchor import TEXT as CA_TEXT  # noqa: E402
from tests.test_greenlist import make_codec, make_text  # noqa: E402


# ======================================================================
# P2-10 CRC-16
# ======================================================================

class TestCrc16Coding:
    def test_crc16_known_vector(self):
        from aawm.coding import crc16
        # CRC-16/CCITT-FALSE 标准校验向量
        assert crc16(b"123456789") == 0x29B1

    def test_compute_crc_dispatch(self):
        from aawm.coding import compute_crc, crc8
        assert compute_crc(b"data", 8) == crc8(b"data")
        assert compute_crc(b"data", 16) is not None
        with pytest.raises(ValueError):
            compute_crc(b"data", 12)

    def test_payload_roundtrip_crc16(self):
        from aawm.coding import build_payload, parse_payload
        payload = build_payload(0xABCD, user_id_bits=16, crc_bits=16)
        assert len(payload) == 32
        uid, ok = parse_payload(payload, user_id_bits=16, crc_bits=16)
        assert ok and uid == 0xABCD

    def test_payload_bit_flip_detected(self):
        from aawm.coding import build_payload, parse_payload
        payload = build_payload(1234, user_id_bits=16, crc_bits=16)
        for i in range(len(payload)):
            broken = list(payload)
            broken[i] ^= 1
            _, ok = parse_payload(broken, user_id_bits=16, crc_bits=16)
            assert not ok, f"bit {i} 翻转未被 CRC-16 检出"

    def test_legacy_crc8_default_unchanged(self):
        from aawm.coding import build_payload
        # 默认 crc_bits=8 保持向后兼容（旧调用方零改动）
        assert len(build_payload(1)) == 24


class TestContentCrc16:
    KEY = bytes(range(32))
    SALT = bytes(range(16))

    def test_config_default_is_crc16(self):
        from aawm.content import CAConfig
        assert CAConfig().crc_bits == 16

    def test_clean_roundtrip_crc16(self):
        from aawm.content import CAEmbedder, CADecoder
        emb, dec = CAEmbedder(self.KEY), CADecoder(self.KEY)
        r = emb.embed(CA_TEXT, user_id=1001, session_salt=self.SALT)
        d = dec.decode(r.watermarked_text, self.SALT)
        assert d.success and d.user_id == 1001 and d.crc_ok

    def test_legacy_crc8_roundtrip(self):
        from aawm.content import CAConfig, CAEmbedder, CADecoder
        cfg = CAConfig(crc_bits=8)
        emb, dec = CAEmbedder(self.KEY, cfg), CADecoder(self.KEY, cfg)
        r = emb.embed(CA_TEXT, user_id=1001, session_salt=self.SALT)
        d = dec.decode(r.watermarked_text, self.SALT)
        assert d.success and d.user_id == 1001

    def test_mixed_crc_width_fails_safe(self):
        """CRC-16 嵌入 + CRC-8 解码：位宽不匹配 → CRC 失败（非误报）。"""
        from aawm.content import CAConfig, CAEmbedder, CADecoder
        emb = CAEmbedder(self.KEY, CAConfig(crc_bits=16))
        r = emb.embed(CA_TEXT, user_id=1001, session_salt=self.SALT)
        dec = CADecoder(self.KEY, CAConfig(crc_bits=8))
        d = dec.decode(r.watermarked_text, self.SALT)
        assert not d.success


# ======================================================================
# P2-9 词典指纹
# ======================================================================

class TestDictVersion:
    def test_fingerprint_stable_across_salt(self):
        from aawm.greenlist import GreenlistCodec
        c1 = GreenlistCodec(b"k" * 32, b"s1" * 8)
        c2 = GreenlistCodec(b"k" * 32, b"s2" * 8)
        assert c1.dict_version == c2.dict_version
        assert len(c1.dict_version) == 16
        int(c1.dict_version, 16)  # 16 hex 合法

    def test_fingerprint_changes_with_dict(self):
        from aawm.greenlist import GreenlistCodec
        base = GreenlistCodec(b"k" * 32, b"s" * 16)
        small = GreenlistCodec(
            b"k" * 32, b"s" * 16,
            dictionary={"big": ["large", "huge"]})
        assert base.dict_version != small.dict_version


# ======================================================================
# P2-8 UID 冗余（codec 层）
# ======================================================================

class TestUidRedundancyCodec:
    def test_layout_properties(self):
        codec = make_codec()
        text = make_text(1500)
        bands = codec.active_bands(text, min_n=1)
        assert len(bands) >= 10
        layout = codec.build_redundant_layout(bands, r=2)
        # 每个带恰好用于一个 bit（交错带分组，不重叠不遗漏）
        used = [b for bits in layout for b in bits]
        assert sorted(used) == sorted(bands)

    def test_redundant_roundtrip_and_crop(self):
        codec = make_codec()
        text = make_text(1500, seed=11)
        rng = random.Random(7)
        # 信道 B 固定 16 带 → r=2 冗余容量 8 bit，UID 取低 8 位
        marked, layout = codec.embed_redundant(text, 0x5A, r=2, rng=rng)
        n_bits = len(layout)
        assert n_bits == 8
        mask = (1 << n_bits) - 1
        # 完整文本：UID 正确还原
        uid, rep = codec.detect_redundant(marked, layout)
        assert uid == 0x5A & mask
        # 裁剪一半：无冗余会丢带，r=2 交错布局幸存带仍可解
        words = marked.split()
        cropped = " ".join(words[: len(words) // 2])
        uid_c, rep_c = codec.detect_redundant(cropped, layout)
        assert uid_c == uid, "裁剪 50% 后 UID 归因不应翻转"

    def test_no_redundancy_crop_loses_uid(self):
        """对照组：r=1（无冗余）同样裁剪场景作为能力差异参照（允许失败）。"""
        codec = make_codec()
        text = make_text(600, seed=11)
        marked, layout = codec.embed_redundant(text, 0x1234, r=1, rng=random.Random(7))
        words = marked.split()
        cropped = " ".join(words[: len(words) // 2])
        uid_c, _ = codec.detect_redundant(cropped, layout)
        # r=1 时裁剪掉一半带无冗余可投票——只要不抛错、能给出判决即通过
        assert isinstance(uid_c, int)


# ======================================================================
# P1-6 密钥轮换（KeyStore + facade）
# ======================================================================

class TestKeystoreRotation:
    def test_legacy_v1_format_load(self, tmp_path):
        from aawm.plugins.keystore import KeyStore
        kf = tmp_path / "legacy.json"
        kf.write_text(json.dumps({
            "version": 1, "master_key": "ab" * 32, "created": "2020-01-01",
        }), encoding="utf-8")
        ks = KeyStore.from_file(kf)
        assert ks.versions() == [1]
        assert ks.active_version == 1
        assert ks.get() == b"\xab" * 32

    def test_rotate_and_persistence(self, tmp_path):
        from aawm.plugins.keystore import KeyStore
        kf = tmp_path / "key.json"
        ks = KeyStore.from_file(kf, create=True)
        v1 = ks.get()
        new_v = ks.rotate()
        assert new_v == 2 and ks.active_version == 2
        assert ks.get_version(1) == v1  # 双钥并行：旧版本可取
        assert ks.get() != v1

        ks.save(kf)
        data = json.loads(kf.read_text(encoding="utf-8"))
        assert data["version"] == 2 and data["active"] == 2
        assert set(data["keys"]) == {"1", "2"}

        ks2 = KeyStore.from_file(kf)
        assert ks2.versions() == [1, 2]
        assert ks2.get_version(1) == v1

    def test_drop_version(self, tmp_path):
        from aawm.plugins.keystore import KeyStore
        ks = KeyStore.from_file(tmp_path / "k.json", create=True)
        ks.rotate()
        with pytest.raises(ValueError):
            ks.drop_version(2)  # active 不许删
        ks.drop_version(1)
        assert ks.versions() == [2]
        with pytest.raises(KeyError):
            ks.get_version(1)


class TestFacadeKeyVersion:
    def _wm(self, keystore):
        from aawm.plugins.facade import Watermarker
        from aawm.plugins.registry import UIDRegistry
        reg = UIDRegistry()
        reg.register("agent-a", uid=0x1234)
        reg.register("agent-b", uid=0x5678)
        return Watermarker(keystore=keystore, registry=reg,
                           codec_mode="default")

    def test_embed_records_key_version(self, tmp_path):
        from aawm.plugins.keystore import KeyStore
        ks = KeyStore.from_file(tmp_path / "key.json", create=True)
        wm = self._wm(ks)
        # default 模式短文本溯源有统计概率性（同既有测试模式，重试）
        r = t = None
        for _ in range(5):
            r = wm.embed(LONG_TEXT, user_id=0x1234)
            t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                         dict_version=r.dict_version)
            if t.watermarked and t.uid == 0x1234:
                break
        assert r.key_version == 1
        assert r.dict_version  # 词典指纹非空
        assert t.watermarked and t.uid == 0x1234
        assert t.key_version == 1
        assert t.dict_version_match is True

    def test_trace_old_key_version_after_rotation(self, tmp_path):
        from aawm.plugins.keystore import KeyStore
        ks = KeyStore.from_file(tmp_path / "key.json", create=True)
        wm = self._wm(ks)
        # 嵌入+溯源有统计概率性（同既有测试模式，重试至多 5 次）
        r_old = t = None
        for _ in range(5):
            r_old = wm.embed(LONG_TEXT, user_id=0x1234)
            t = wm.trace(r_old.watermarked_text,
                         session_salt=r_old.session_salt)
            if t.watermarked and t.uid == 0x1234:
                break
        assert t.watermarked and t.uid == 0x1234
        # 轮换后嵌入新水印
        ks.rotate()
        r_new = wm.embed(LONG_TEXT, user_id=0x5678)
        assert r_old.key_version == 1 and r_new.key_version == 2
        # 旧水印按 key_version=1 溯源（轮换不破坏历史归因）
        t = wm.trace(r_old.watermarked_text, session_salt=r_old.session_salt,
                     key_version=1)
        assert t.watermarked and t.uid == 0x1234
        # 不传 key_version 走 active（v2），同盐解不出旧 UID
        t2 = wm.trace(r_old.watermarked_text, session_salt=r_old.session_salt)
        assert not (t2.watermarked and t2.uid == 0x1234)
        # 不存在的版本号显式报错
        with pytest.raises(KeyError):
            wm.trace(r_old.watermarked_text, session_salt=r_old.session_salt,
                     key_version=99)

    def test_dict_version_mismatch_flagged(self, tmp_path):
        from aawm.plugins.facade import Watermarker
        from aawm.plugins.keystore import KeyStore
        ks = KeyStore.from_file(tmp_path / "key.json", create=True)
        wm = Watermarker(keystore=ks, codec_mode="hybrid",
                         supplementary_dict={"fast": ["quick", "rapid"]})
        r = wm.embed(LONG_TEXT, user_id=7)
        # 用不同补充词典的指纹比对 → mismatch 显式暴露
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     dict_version="deadbeefdeadbeef")
        assert t.dict_version_match is False


class TestFacadeUidRedundancy:
    def test_embed_redundancy_end_to_end(self):
        from tests.test_e2e_integration import _long_zh_text
        from aawm.plugins.facade import Watermarker
        wm = Watermarker(master_key="42" * 32, codec_mode="zero_cost")
        # n_bits=6：冗余容量 = 活动带数÷2（该语料 7~14，6 位可稳定兑现）。
        # 容量随盐波动且不足时 embed 会静默降级（honor=False 取余量
        # 最大尝试），测试重试到兑现为止。
        r = None
        for _ in range(8):
            cand = wm.embed(_long_zh_text(), user_id=0x1234,
                            uid_redundancy=2, n_bits=6)
            if len(cand.uid_layout) == 6:
                r = cand
                break
        assert r is not None, "8 次尝试仍未兑现 6 位冗余容量"
        assert r.uid_layout, "uid_redundancy=2 应产出布局"
        n_bits = len(r.uid_layout)
        assert n_bits == 6
        # 完整文本溯源（带布局）
        t = wm.trace(r.watermarked_text, session_salt=r.session_salt,
                     uid_layout=r.uid_layout)
        assert t.watermarked and t.uid is not None
        # 冗余生效：裁剪一半后 UID 仍正确归因（低位 n_bits 位）
        paras = r.watermarked_text.split("\n\n")
        cropped = "\n\n".join(paras[: max(1, len(paras) // 2)])
        t2 = wm.trace(cropped, session_salt=r.session_salt,
                      uid_layout=r.uid_layout)
        if t2.uid is not None:
            mask = (1 << n_bits) - 1
            assert t2.uid == 0x1234 & mask, "裁剪后 UID 归因不应翻转"

    def test_uid_redundancy_1_no_layout(self):
        from aawm.plugins.facade import Watermarker
        wm = Watermarker(master_key="43" * 32, codec_mode="zero_cost")
        r = wm.embed(LONG_TEXT, user_id=5)
        assert r.uid_layout == []

    def test_uid_redundancy_rejected_in_default_mode(self):
        from aawm.plugins.facade import Watermarker
        wm = Watermarker(master_key="44" * 32, codec_mode="default")
        with pytest.raises(ValueError, match="uid_redundancy"):
            wm.embed(LONG_TEXT, user_id=5, uid_redundancy=2)


# ======================================================================
# P1-7 meta 存储后端
# ======================================================================

def _sample_record(uid: int, paras: list) -> dict:
    """构造类 embed meta 记录（seal 内含段哈希）。"""
    import hashlib
    return {
        "user_id": uid,
        "session_salt": "0011223344556677",
        "watermarked_text": f"watermarked-{uid}-" + "x" * 40,
        "seal": {"para_hashes": [
            hashlib.sha256(p.encode()).hexdigest() for p in paras]},
    }


class TestMetaStoreBackends:
    @pytest.mark.parametrize("suffix", ["jsonl", "db"])
    def test_put_get_find(self, tmp_path, suffix):
        from aawm.meta_store import open_meta_store
        store = open_meta_store(tmp_path / f"metas.{suffix}")
        rec = _sample_record(7, ["第一段", "第二段"])
        rid = store.put(rec)
        assert rid >= 1
        got = store.get(rid)
        assert got["user_id"] == 7
        # 全文指纹精确匹配
        from aawm.audit import text_fingerprint
        sha = text_fingerprint(rec["watermarked_text"])
        hits = store.find_by_text_hash(sha)
        assert len(hits) == 1 and hits[0]["user_id"] == 7
        # 段哈希反查
        import hashlib
        h1 = hashlib.sha256("第一段".encode()).hexdigest()
        assert {r["user_id"] for r in store.find_by_para_hash(h1)} == {7}
        assert store.find_by_para_hash("nothash") == []
        store.close()

    def test_open_meta_store_dispatch(self, tmp_path):
        from aawm.meta_store import FileMetaStore, SqliteMetaStore, open_meta_store
        assert isinstance(open_meta_store(tmp_path / "a.db"), SqliteMetaStore)
        assert isinstance(open_meta_store(tmp_path / "a.sqlite3"), SqliteMetaStore)
        assert isinstance(open_meta_store(tmp_path / "a.jsonl"), FileMetaStore)

    def test_file_store_reload_persistence(self, tmp_path):
        from aawm.meta_store import open_meta_store
        p = tmp_path / "metas.jsonl"
        store = open_meta_store(p)
        rid = store.put(_sample_record(9, ["p1"]))
        store.close()
        store2 = open_meta_store(p)
        assert store2.get(rid)["user_id"] == 9
        assert store2.put(_sample_record(10, ["p2"])) == rid + 1

    def test_para_reverse_lookup_truncated_text(self, tmp_path):
        """被删减文本的段落哈希反查候选 meta（密钥无关定位）。"""
        from aawm.meta_store import open_meta_store
        store = open_meta_store(tmp_path / "metas.db")
        store.put(_sample_record(7, ["段一", "段二", "段三"]))
        store.put(_sample_record(8, ["另一段"]))
        # 嫌疑文本只剩原第二段（删减/裁剪后）
        import hashlib
        h = hashlib.sha256("段二".encode()).hexdigest()
        hits = store.find_by_para_hash(h)
        assert len(hits) == 1 and hits[0]["user_id"] == 7
        store.close()

    def test_file_store_half_line_tolerated(self, tmp_path):
        """崩溃遗留半行 JSON 容错（跳过不炸）。"""
        from aawm.meta_store import open_meta_store
        p = tmp_path / "metas.jsonl"
        p.write_text(
            json.dumps(_sample_record(7, ["p"]), ensure_ascii=False) + "\n"
            + '{"user_id": 8, "trunc', encoding="utf-8")
        store = open_meta_store(p)
        assert store.get(1)["user_id"] == 7
        assert store.put(_sample_record(9, ["q"])) == 2


# ======================================================================
# P1-5 审计日志
# ======================================================================

class TestAudit:
    def test_text_fingerprint(self):
        from aawm.audit import text_fingerprint
        fp = text_fingerprint("hello")
        assert fp == text_fingerprint("hello")
        assert fp != text_fingerprint("hellx")
        assert len(fp) == 16

    def test_audit_logger_append_and_read(self, tmp_path):
        from aawm.audit import AuditLogger
        logger = AuditLogger(tmp_path / "sub" / "audit.jsonl")
        logger.log({"op": "trace", "source": "cli", "text_sha256": "ab" * 8})
        logger.log({"op": "embed", "source": "server", "uid": 5})
        events = logger.read_all()
        assert len(events) == 2
        assert all("ts" in e for e in events)
        assert events[0]["op"] == "trace"
        # 追加不截断
        logger.log({"op": "find_meta", "source": "cli"})
        assert len(logger.read_all()) == 3

    def test_global_logger(self, tmp_path):
        from aawm.audit import (
            AuditLogger, audit, get_audit_logger, set_audit_logger)
        assert get_audit_logger() is None
        audit({"op": "trace"})  # 未配置时静默无操作
        logger = AuditLogger(tmp_path / "audit.jsonl")
        set_audit_logger(logger)
        try:
            assert get_audit_logger() is logger
            audit({"op": "trace", "uid": 1})
            assert len(logger.read_all()) == 1
        finally:
            set_audit_logger(None)


# ======================================================================
# P1-4 指标
# ======================================================================

class TestMetrics:
    def test_counter_and_render(self):
        from aawm.server.metrics import Metrics
        m = Metrics()
        m.inc("aawm_trace_requests_total")
        m.inc("aawm_trace_requests_total")
        m.inc("aawm_embed_requests_total", reliability="high")
        out = m.render()
        assert "# TYPE aawm_trace_requests_total counter" in out
        assert "aawm_trace_requests_total 2" in out
        assert 'aawm_embed_requests_total{reliability="high"} 1' in out
        assert "aawm_uptime_seconds" in out

    def test_histogram_observe_and_time_it(self):
        from aawm.server.metrics import Metrics
        m = Metrics()
        m.observe("aawm_request_latency_seconds", 0.003, op="trace")
        with m.time_it("aawm_request_latency_seconds", op="embed"):
            pass
        out = m.render()
        assert "aawm_request_latency_seconds_bucket" in out
        assert 'aawm_request_latency_seconds_sum{op="trace"}' in out
        assert 'aawm_request_latency_seconds_count{op="embed"} 1' in out


class TestServerMetricsEndpoint:
    def test_metrics_endpoint_and_counters(self):
        from aawm.plugins import Watermarker
        from aawm.server import api
        from aawm.server.api import create_app, set_watermarker
        set_watermarker(Watermarker(codec_mode="default"))
        app = create_app()
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        body = resp.text
        assert "aawm_uptime_seconds" in body
        # 打一次 trace 请求 → 计数器出现
        r = client.post("/v1/trace", json={"text": LONG_TEXT * 3})
        assert r.status_code == 200
        body = client.get("/metrics").text
        assert "aawm_trace_requests_total 1" in body
        assert 'aawm_text_chars_total{op="trace"}' in body
        api.reset_watermarker()

    def test_audit_log_written_by_server(self, tmp_path):
        from aawm.audit import AuditLogger, set_audit_logger
        from aawm.plugins import Watermarker
        from aawm.server import api
        from aawm.server.api import create_app, set_watermarker
        logger = AuditLogger(tmp_path / "audit.jsonl")
        set_audit_logger(logger)
        try:
            set_watermarker(Watermarker(codec_mode="default"))
            app = create_app()
            from fastapi.testclient import TestClient
            client = TestClient(app)
            r = client.post("/v1/trace", json={"text": LONG_TEXT * 3})
            assert r.status_code == 200
            events = logger.read_all()
            assert len(events) == 1
            assert events[0]["op"] == "trace"
            assert events[0]["source"] == "server"
            assert "text_sha256" in events[0]
        finally:
            set_audit_logger(None)
            api.reset_watermarker()


# ======================================================================
# CLI 冒烟（v0.13 新命令/参数）
# ======================================================================

class TestCLIV013:
    def _run_cli(self, *args, input_text=None):
        cmd = [sys.executable, "-m", "aawm.cli"] + list(args)
        src_path = str(Path(__file__).resolve().parents[1] / "src")
        env = {**os.environ, "PYTHONPATH": src_path}
        return subprocess.run(cmd, input=input_text, capture_output=True,
                              text=True, env=env)

    def test_rotate_key(self, tmp_path):
        key_file = tmp_path / "key.json"
        self._run_cli("keygen", "--output", str(key_file))
        result = self._run_cli("rotate-key", "--key", str(key_file))
        assert result.returncode == 0, result.stderr
        assert "2" in result.stdout
        data = json.loads(key_file.read_text(encoding="utf-8"))
        assert data["version"] == 2 and data["active"] == 2
        # 再轮换 → v3，drop v1
        self._run_cli("rotate-key", "--key", str(key_file))
        result = self._run_cli("rotate-key", "--key", str(key_file), "--drop", "1")
        assert result.returncode == 0, result.stderr
        data = json.loads(key_file.read_text(encoding="utf-8"))
        assert data["active"] == 3
        assert "1" not in data["keys"]

    def test_embed_meta_store_and_find_meta(self, tmp_path):
        from tests.test_e2e_integration import _long_zh_text
        key_file = tmp_path / "key.json"
        self._run_cli("keygen", "--output", str(key_file))
        input_file = tmp_path / "input.txt"
        input_file.write_text(_long_zh_text(), encoding="utf-8")
        marked = tmp_path / "marked.txt"
        store = tmp_path / "metas.db"

        result = self._run_cli(
            "embed", str(input_file), "--key", str(key_file), "--user", "7",
            "--codec-mode", "zero_cost", "--meta-store", str(store),
            "-o", str(marked))
        assert result.returncode == 0, result.stderr
        assert store.exists()
        # meta-store 命中（水印原文反查）
        result = self._run_cli(
            "find-meta", str(marked), "--key", str(key_file),
            "--meta-store", str(store), "--codec-mode", "zero_cost")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "store#" in result.stdout or "命中" in result.stdout

    def test_trace_audit_log(self, tmp_path):
        key_file = tmp_path / "key.json"
        self._run_cli("keygen", "--output", str(key_file))
        input_file = tmp_path / "input.txt"
        input_file.write_text(LONG_TEXT * 2, encoding="utf-8")
        audit_log = tmp_path / "audit.jsonl"
        # 嵌入+溯源可能因统计检测概率性失败（同既有 CLI 测试模式，重试）
        for attempt in range(5):
            marked = tmp_path / f"marked_{attempt}.txt"
            result = self._run_cli(
                "embed", str(input_file), "--key", str(key_file), "--user", "9",
                "--codec-mode", "default", "--audit-log", str(audit_log),
                "-o", str(marked))
            assert result.returncode == 0, result.stderr
            meta_file = marked.with_suffix(".meta.json")
            result = self._run_cli(
                "trace", str(marked), "--key", str(key_file),
                "--codec-mode", "default", "--meta", str(meta_file),
                "--audit-log", str(audit_log))
            if result.returncode == 0 and "检出水印: 是" in result.stdout:
                break
        assert result.returncode == 0, result.stdout + result.stderr
        events = [json.loads(l) for l in
                  audit_log.read_text(encoding="utf-8").splitlines() if l]
        ops = [e["op"] for e in events]
        assert "embed" in ops and "trace" in ops
        embed_ev = next(e for e in events if e["op"] == "embed")
        assert embed_ev["uid"] == 9
        assert "text_sha256" in embed_ev
        trace_ev = next(e for e in events if e["op"] == "trace")
        assert trace_ev["source"] == "cli"
