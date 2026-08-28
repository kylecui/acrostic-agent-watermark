"""审计日志：溯源操作全留痕（JSONL，P1-5）。

溯源结论要支撑内部处分，必须可回答"谁在何时对什么文本得出了
什么结论、置信度多少"。本模块提供 append-only JSONL 记录器，
CLI（aawm trace / find-meta --audit-log）与 server
（aawm serve --audit-log 的 /v1/trace、/v1/embed、/v1/find-meta）
共用同一事件结构。

事件公共字段：
- ts: UTC ISO-8601 时间戳
- op: trace | embed | find_meta
- source: cli | server
- text_sha256: 嫌疑文本 SHA-256 前 16 hex（内容指纹，不落原文，避免
  审计日志本身成为二次泄露源）
- text_chars: 文本长度

trace 事件额外：watermarked / uid / user / attribution_abstain /
attribution_confidence / existence_score / confidence / exit_code。
embed 事件额外：user_id / reliability / weak_embed / capacity / n_bits。
find_meta 事件额外：result(match|abstain|none) / matched_label / uid / user。
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union


def text_fingerprint(text: str) -> str:
    """文本内容指纹（SHA-256 前 16 hex）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class AuditLogger:
    """append-only JSONL 审计日志。

    用法::

        logger = AuditLogger("audit.jsonl")
        logger.log({"op": "trace", "source": "cli",
                    "text_sha256": text_fingerprint(t), ...})

    写入失败（磁盘满/权限）不中断业务操作——审计失败打印到 stderr
    并继续（事件丢失可从 stderr 日志聚合），但构造时路径不可写会
    立即抛错（部署错误应在启动时暴露）。
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 启动即验证可写（fail-fast； append 模式不截断已有日志）
        self._path.touch(exist_ok=True)
        self._lock = threading.Lock()

    def log(self, event: Dict[str, Any]) -> None:
        """追加一条审计事件（自动补 ts）。"""
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        record.update(event)
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            with self._lock:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as e:
            # 审计写入失败不能中断溯源操作本身，但要留下痕迹
            print(f"[审计] 写入失败（{e}）: {line}", file=sys.stderr)

    def read_all(self) -> list:
        """读回全部事件（测试/核对用）。"""
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out


_global_logger: Optional[AuditLogger] = None


def set_audit_logger(logger: Optional[AuditLogger]) -> None:
    """设置全局审计记录器（server 进程用）。"""
    global _global_logger
    _global_logger = logger


def get_audit_logger() -> Optional[AuditLogger]:
    """取全局审计记录器（未配置返回 None）。"""
    return _global_logger


def audit(event: Dict[str, Any]) -> None:
    """便捷入口：全局记录器存在时写一条事件。"""
    if _global_logger is not None:
        _global_logger.log(event)
