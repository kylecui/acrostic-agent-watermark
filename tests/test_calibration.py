"""v0.12 标定能力测试。

覆盖：
  1. `aawm calibrate` CLI 端到端（--demo → calibration.json → embed/trace --calibration）
  2. 标定文件与 corpus 现场标定的等价性（null 模型 + embed/trace 往返）
  3. 标定文件跨密钥复用（null 模型密钥无关，p0 词频表按新密钥重算）
  4. reliability_tier 分级规则 + EmbedResult.reliability 字段
  5. estimate_capacity 容量预检
  6. Watermarker/from_config 接受标定文件路径
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm.plugins import Watermarker  # noqa: E402


# ------------------------------------------------------------------ 固件

DEMO_CORPUS_DIR = Path(__file__).resolve().parents[1] / "src" / "aawm" / "data" / "demo_corpus"

LONG_ZH_TEXT = "\n\n".join(
    p.read_text(encoding="utf-8") for p in sorted(DEMO_CORPUS_DIR.glob("*.md"))
)[:12000]

SHORT_ZH_TEXT = "这是一个测试文本，用于验证容量分级的行为。数据量很小。"


def _corpus():
    """从包内置示例语料取标定语料（保证有 zh 词典词命中）。"""
    return [p.read_text(encoding="utf-8")
            for p in sorted(DEMO_CORPUS_DIR.glob("*.md"))]


# ------------------------------------------------------------------
# 标定文件与 corpus 标定的等价性
# ------------------------------------------------------------------

class TestCalibrationEquivalence:
    def test_null_model_and_roundtrip(self):
        corpus = _corpus()
        wm_a = Watermarker(master_key="aa" * 32)
        wm_a.calibrate_null_model(corpus)  # 与 CLI aawm calibrate 同管线
        calib = wm_a.export_calibration()

        # 标定文件结构
        assert calib["version"] == 1
        assert calib["codec_mode"] == "zero_cost"
        assert set(calib["null_model"]) >= {"zh", "en"}
        assert calib["p0_vocab"]["zh"]  # 非空词频表

        # 文件路径与 corpus 现场标定：null 模型一致
        wm_b = Watermarker(master_key="aa" * 32, calibration=calib)
        for lang in (b"zh", b"en"):
            mu_a, ratio_a = wm_a._null_model[lang]
            mu_b, ratio_b = wm_b._null_model[lang]
            assert mu_b == pytest.approx(mu_a)
            assert ratio_b == pytest.approx(ratio_a)

        # 文件路径 embed → trace 往返成功
        r = wm_b.embed(LONG_ZH_TEXT, user_id="alice")
        t = wm_b.trace(r.watermarked_text, session_salt=r.session_salt,
                       bands=r.bands, n_bits=r.n_bits)
        assert t.watermarked
        assert t.uid is not None

    def test_calibration_accepts_path_string(self, tmp_path):
        corpus = _corpus()
        wm_a = Watermarker(master_key="bb" * 32)
        wm_a.calibrate_null_model(corpus)
        calib_file = tmp_path / "calibration.json"
        calib_file.write_text(
            json.dumps(wm_a.export_calibration(), ensure_ascii=False),
            encoding="utf-8")

        # __init__ 与 from_config 都接受路径字符串
        wm_b = Watermarker(master_key="bb" * 32, calibration=str(calib_file))
        assert wm_b._null_model
        wm_c = Watermarker.from_config(None, None, calibration=str(calib_file))
        assert wm_c._null_model

    def test_cross_key_reuse(self):
        """null 模型密钥无关；标定文件跨密钥复用后 embed/trace 仍成功。"""
        corpus = _corpus()
        wm_a = Watermarker(master_key="cc" * 32)
        wm_a.calibrate_null_model(corpus)
        calib = wm_a.export_calibration()

        wm_b = Watermarker(master_key="dd" * 32, calibration=calib)
        # null 模型完全相同（密钥无关）
        assert wm_b._null_model[b"zh"] == wm_a._null_model[b"zh"]
        # p0 词频表按新密钥重算后仍可检出
        r = wm_b.embed(LONG_ZH_TEXT, user_id=42)
        t = wm_b.trace(r.watermarked_text, session_salt=r.session_salt,
                       bands=r.bands, n_bits=r.n_bits)
        assert t.watermarked


# ------------------------------------------------------------------
# 可靠性分级
# ------------------------------------------------------------------

class TestReliability:
    def test_reliability_tier_rules(self):
        rt = Watermarker.reliability_tier
        # 容量分级
        assert rt(10, False) == "high"
        assert rt(100, False) == "high"
        assert rt(9, False) == "medium"
        assert rt(6, False) == "medium"
        assert rt(5, False) == "low"
        assert rt(0, False) == "low"
        # weak_embed 一票降级
        assert rt(50, True) == "low"

    def test_embed_reliability_field(self):
        corpus = _corpus()
        wm = Watermarker(master_key="ee" * 32, calibrate_corpus=corpus)
        # 长文本：high（demo 语料拼接容量 ≥10）
        r_long = wm.embed(LONG_ZH_TEXT, user_id=1)
        assert r_long.capacity >= 10
        assert r_long.reliability == "high"
        # 短文本：不拒嵌，降级为 low/medium
        r_short = wm.embed(SHORT_ZH_TEXT, user_id=1)
        assert r_short.watermarked_text  # 仍然嵌入
        assert r_short.reliability in ("low", "medium")

    def test_estimate_capacity(self):
        corpus = _corpus()
        wm = Watermarker(master_key="ff" * 32, calibrate_corpus=corpus)
        k_long = wm.estimate_capacity(LONG_ZH_TEXT)
        k_short = wm.estimate_capacity(SHORT_ZH_TEXT)
        assert k_long >= 10
        assert 0 <= k_short < k_long
        # 随机盐估计：多次调用都在合理区间（长文本稳定 ≥10）
        for _ in range(3):
            assert wm.estimate_capacity(LONG_ZH_TEXT) >= 10


# ------------------------------------------------------------------
# CLI 端到端：aawm calibrate --demo → embed/trace --calibration
# ------------------------------------------------------------------

class TestCalibrateCLI:
    def _run_cli(self, *args):
        cmd = [sys.executable, "-m", "aawm.cli"] + list(args)
        src_path = str(Path(__file__).resolve().parents[1] / "src")
        env = {**os.environ, "PYTHONPATH": src_path}
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def test_calibrate_demo_full_roundtrip(self, tmp_path):
        # 1) keygen + registry
        key_file = tmp_path / "key.json"
        reg_file = tmp_path / "reg.json"
        assert self._run_cli("keygen", "-o", str(key_file)).returncode == 0
        assert self._run_cli(
            "registry", "add", "alice", "--registry", str(reg_file)).returncode == 0

        # 2) calibrate --demo
        calib_file = tmp_path / "calibration.json"
        result = self._run_cli(
            "calibrate", "--demo", "-o", str(calib_file))
        assert result.returncode == 0
        assert "标定完成" in result.stdout
        calib = json.loads(calib_file.read_text(encoding="utf-8"))
        assert calib["version"] == 1
        assert "zh" in calib["null_model"]
        assert calib["p0_vocab"]["zh"]

        # 3) embed（包内置示例长文 + 标定文件）
        demo_input = DEMO_CORPUS_DIR / "agent_embedding_guide.md"
        marked = tmp_path / "marked.txt"
        result = self._run_cli(
            "embed", str(demo_input),
            "--key", str(key_file), "--user", "alice",
            "--registry", str(reg_file),
            "--calibration", str(calib_file),
            "-o", str(marked))
        assert result.returncode == 0
        assert "可靠性" in result.stderr
        # meta 文件名 = 输出文件名 .txt 后缀替换为 .meta.json
        meta = json.loads(
            (tmp_path / "marked.meta.json").read_text(encoding="utf-8"))
        assert meta["reliability"] in ("high", "medium", "low")

        # 4) trace（同一份标定文件）
        result = self._run_cli(
            "trace", str(marked),
            "--key", str(key_file), "--registry", str(reg_file),
            "--calibration", str(calib_file),
            "--meta", str(tmp_path / "marked.meta.json"))
        assert result.returncode == 0
        assert "检出水印: 是" in result.stdout
        assert "alice" in result.stdout

    def test_calibrate_missing_corpus_errors(self, tmp_path):
        result = self._run_cli("calibrate", str(tmp_path / "nonexistent"))
        assert result.returncode != 0

    def test_short_text_embeds_with_low_reliability(self, tmp_path):
        """短文本不被拒嵌：照常嵌入并输出可靠性降级说明。"""
        key_file = tmp_path / "key.json"
        calib_file = tmp_path / "calibration.json"
        assert self._run_cli("keygen", "-o", str(key_file)).returncode == 0
        assert self._run_cli(
            "calibrate", "--demo", "-o", str(calib_file)).returncode == 0

        short = tmp_path / "short.txt"
        short.write_text(SHORT_ZH_TEXT, encoding="utf-8")
        marked = tmp_path / "short_marked.txt"
        result = self._run_cli(
            "embed", str(short),
            "--key", str(key_file), "--user", "alice",
            "--calibration", str(calib_file),
            "-o", str(marked))
        assert result.returncode == 0  # 不因短文本失败
        assert "可靠性" in result.stderr
