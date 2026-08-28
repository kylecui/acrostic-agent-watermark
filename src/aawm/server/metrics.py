"""Prometheus 指标（手写文本格式，无第三方依赖）。

v0.13（P1-4 可观测性）：server 的 /metrics 端点数据源。

指标清单（产品健康度核心指标，来自产品差距分析 P1-4）：
- aawm_embed_requests_total{reliability}   嵌入量（按可靠性分级）
- aawm_embed_weak_total                    弱嵌入（weak_embed）次数
- aawm_trace_requests_total                溯源请求量
- aawm_trace_watermarked_total             检出（存在性通过）次数
- aawm_trace_abstain_total                 归因弃权次数
- aawm_findmeta_requests_total{result}     存档检索（match/abstain/none）
- aawm_request_latency_seconds{op}         请求延迟直方图
- aawm_text_chars_total{op}                处理字符量（容量规划用）

线程安全：所有更新经一把锁（服务端为多 worker 线程；进程级指标
多进程部署时由 Prometheus 的 sum 聚合，符合其标准模型）。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Tuple

# 延迟直方图桶（秒）：检测 ~1ms、嵌入 ~16ms 量级，覆盖到 5s 兜底
_LATENCY_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


def _canon_labels(labels: Dict[str, str]) -> Tuple[Tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


class Metrics:
    """进程级指标注册表（counter + histogram，Prometheus 文本格式输出）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
        self._histograms: Dict[
            str, Dict[str, object]] = {}  # name -> {labels, buckets, counts, sum, count}
        self._started = time.time()

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        """计数器累加。"""
        key = _canon_labels(labels)
        with self._lock:
            series = self._counters.setdefault(name, {})
            series[key] = series.get(key, 0.0) + value

    def observe(self, name: str, value: float, **labels: str) -> None:
        """直方图观测（延迟等）。"""
        key = _canon_labels(labels)
        with self._lock:
            h = self._histograms.get(name)
            if h is None:
                h = self._histograms[name] = {}
            entry = h.get(key)
            if entry is None:
                entry = h[key] = {
                    "counts": [0] * (len(_LATENCY_BUCKETS) + 1),
                    "sum": 0.0,
                    "count": 0,
                }
            entry["sum"] += value
            entry["count"] += 1
            for i, le in enumerate(_LATENCY_BUCKETS):
                if value <= le:
                    entry["counts"][i] += 1
            entry["counts"][-1] += 1  # +Inf

    def time_it(self, name: str, **labels: str):
        """上下文管理器：观测代码块耗时。"""
        return _Timer(self, name, labels)

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------

    def render(self) -> str:
        """渲染 Prometheus 文本格式（Content-Type: text/plain; version=0.0.4）。"""
        lines: list[str] = []
        with self._lock:
            counters = {k: dict(v) for k, v in self._counters.items()}
            hists = {k: {kk: dict(vv) for kk, vv in v.items()}
                     for k, v in self._histograms.items()}
        for name in sorted(counters):
            series = counters[name]
            lines.append(f"# HELP {name} counter")
            lines.append(f"# TYPE {name} counter")
            for key in sorted(series):
                lines.append(f"{name}{_fmt_labels(key)} {series[key]}")
        for name in sorted(hists):
            lines.append(f"# HELP {name} histogram")
            lines.append(f"# TYPE {name} histogram")
            for key in sorted(hists[name]):
                entry = hists[name][key]
                counts = entry["counts"]
                for le, c in zip(_LATENCY_BUCKETS, counts[:-1]):
                    lab = _fmt_labels(key + (("le", _fmt_float(le)),))
                    lines.append(f"{name}_bucket{lab} {c}")
                lab = _fmt_labels(key + (("le", "+Inf"),))
                lines.append(f"{name}_bucket{lab} {counts[-1]}")
                lines.append(
                    f"{name}_sum{_fmt_labels(key)} {_fmt_float(entry['sum'])}")
                lines.append(
                    f"{name}_count{_fmt_labels(key)} {entry['count']}")
        lines.append("# HELP aawm_uptime_seconds process uptime")
        lines.append("# TYPE aawm_uptime_seconds gauge")
        lines.append(f"aawm_uptime_seconds {time.time() - self._started:.1f}")
        return "\n".join(lines) + "\n"


def _fmt_labels(labels: Tuple[Tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in labels)
    return "{" + inner + "}"


def _fmt_float(v: float) -> str:
    return f"{v:.6g}"


class _Timer:
    def __init__(self, metrics: Metrics, name: str, labels: Dict[str, str]):
        self._m = metrics
        self._name = name
        self._labels = labels

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self._m.observe(self._name, time.perf_counter() - self._t0, **self._labels)
        return False
