"""水印元数据存储后端抽象（v0.13 P1-7）。

此前 meta 只有"每文档一个 .meta.json 文件"一种形态，检索 =
全盘扫描 + 逐个试盐解码。生产部署需要：

1. 后端可替换 —— 文件（默认，零依赖）/ SQLite（单文件、并发读、
   索引查询），后续可扩展对象存储（S3/COS）实现同一 Protocol。
2. 段哈希索引 —— 信道 A 的段落哈希是**密钥无关**的
   SHA-256(normalize_paragraph(段落))，被删减/裁剪的嫌疑文本仍
   保留部分原段落——用嫌疑文本的段落哈希反查候选 meta，
   免去"知道盐才能解码、不知道盐就扫全库"的两难。

record 结构即 embed 的 meta dict（session_salt/user_id/seal/bands/
key_version/dict_version/uid_layout/...），另存时补充：
- text_sha256：水印全文指纹（audit.text_fingerprint，精确匹配用）
- para_hashes：段落哈希列表（seal 内提取，段级反查用）

用法::

    from aawm.meta_store import open_meta_store
    store = open_meta_store("metas.db")     # .db/.sqlite → SQLite
    store = open_meta_store("metas.jsonl")  # 其他 → JSONL 文件

    rid = store.put(meta_record)
    hits = store.find_by_para_hash(h.hex())  # 段落反查候选 meta
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .audit import text_fingerprint


@runtime_checkable
class MetaStore(Protocol):
    """meta 存储后端协议。"""

    def put(self, record: Dict[str, Any]) -> int:
        """存入一条 meta 记录，返回记录 ID。"""
        ...

    def get(self, record_id: int) -> Optional[Dict[str, Any]]:
        """按记录 ID 取完整记录（不存在返回 None）。"""
        ...

    def find_by_text_hash(self, text_sha256: str) -> List[Dict[str, Any]]:
        """按全文指纹精确匹配（水印文本原文未改时的快速命中）。"""
        ...

    def find_by_para_hash(self, para_hash: str) -> List[Dict[str, Any]]:
        """按段落哈希反查候选记录（文本被删减/裁剪后仍可命中）。"""
        ...

    def close(self) -> None:
        ...


def _extract_para_hashes(record: Dict[str, Any]) -> List[str]:
    """从 record 的 seal 字段提取段落哈希（hex，密钥无关）。"""
    seal = record.get("seal") or {}
    hashes = seal.get("para_hashes")
    if isinstance(hashes, list):
        return [str(h) for h in hashes]
    return []


def _normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """补全存储侧字段（text_sha256 / created），不改动调用方原始键。"""
    out = dict(record)
    if not out.get("text_sha256"):
        text = out.get("watermarked_text") or ""
        if text:
            out["text_sha256"] = text_fingerprint(text)
    out.setdefault("created", datetime.now(timezone.utc).isoformat())
    return out


class FileMetaStore:
    """JSONL 文件后端（append-only，构造时全量加载建索引）。

    每行一条记录 {"id": n, ...}。适合中小规模（万级）：
    单文件、人可读、git 友好；写入有锁，读走内存索引。
    """

    def __init__(self, path: os.PathLike | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # 索引：text_sha256 / para_hash -> [record_id, ...]
        self._by_text: Dict[str, List[int]] = {}
        self._by_para: Dict[str, List[int]] = {}
        self._records: Dict[int, Dict[str, Any]] = {}
        self._next_id = 1
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 半行（崩溃中断）容错：跳过
                self._index(int(rec.get("id", self._next_id)), rec)

    def _index(self, rid: int, rec: Dict[str, Any]) -> None:
        self._records[rid] = rec
        self._next_id = max(self._next_id, rid + 1)
        sha = rec.get("text_sha256")
        if sha:
            self._by_text.setdefault(str(sha), []).append(rid)
        for h in _extract_para_hashes(rec):
            self._by_para.setdefault(h, []).append(rid)

    def put(self, record: Dict[str, Any]) -> int:
        rec = _normalize_record(record)
        with self._lock:
            rid = self._next_id
            rec["id"] = rid
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._index(rid, rec)
        return rid

    def _find(self, ids: List[int]) -> List[Dict[str, Any]]:
        return [self._records[i] for i in ids if i in self._records]

    def find_by_text_hash(self, text_sha256: str) -> List[Dict[str, Any]]:
        return self._find(self._by_text.get(text_sha256, []))

    def find_by_para_hash(self, para_hash: str) -> List[Dict[str, Any]]:
        return self._find(self._by_para.get(para_hash, []))

    def get(self, record_id: int) -> Optional[Dict[str, Any]]:
        return self._records.get(record_id)

    def close(self) -> None:
        pass  # 无句柄


class SqliteMetaStore:
    """SQLite 后端（单文件 + WAL，段哈希索引表）。

    表结构：
        records(id INTEGER PRIMARY KEY, uid INTEGER, text_sha256 TEXT,
                created TEXT, payload TEXT)   -- payload = 完整 meta JSON
        para_index(hash TEXT, record_id INTEGER REFERENCES records(id))
        CREATE INDEX idx_para_hash / idx_text_sha

    并发：check_same_thread=False + 内部锁（多进程写用 SQLite 自身
    的文件锁；重并发写建议单写进程）。
    """

    def __init__(self, path: os.PathLike | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER,
                text_sha256 TEXT,
                created TEXT,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_records_text_sha
                ON records(text_sha256);
            CREATE TABLE IF NOT EXISTS para_index (
                hash TEXT NOT NULL,
                record_id INTEGER NOT NULL,
                PRIMARY KEY (hash, record_id)
            );
            CREATE INDEX IF NOT EXISTS idx_para_hash
                ON para_index(hash);
            """
        )
        self._conn.commit()

    def put(self, record: Dict[str, Any]) -> int:
        rec = _normalize_record(record)
        payload = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO records (uid, text_sha256, created, payload) "
                "VALUES (?, ?, ?, ?)",
                (rec.get("user_id"), rec.get("text_sha256"),
                 rec.get("created"), payload))
            rid = int(cur.lastrowid)
            self._conn.executemany(
                "INSERT OR IGNORE INTO para_index (hash, record_id) "
                "VALUES (?, ?)",
                [(h, rid) for h in _extract_para_hashes(rec)])
            self._conn.commit()
        return rid

    def _rows_to_records(self, rows) -> List[Dict[str, Any]]:
        out = []
        for (payload,) in rows:
            try:
                out.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
        return out

    def find_by_text_hash(self, text_sha256: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM records WHERE text_sha256 = ?",
            (text_sha256,)).fetchall()
        return self._rows_to_records(rows)

    def find_by_para_hash(self, para_hash: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT r.payload FROM para_index p "
            "JOIN records r ON r.id = p.record_id WHERE p.hash = ?",
            (para_hash,)).fetchall()
        return self._rows_to_records(rows)

    def get(self, record_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT payload FROM records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_meta_store(path: os.PathLike | str) -> MetaStore:
    """按扩展名选择后端：.db/.sqlite/.sqlite3 → SQLite，其余 → JSONL。"""
    p = str(path).lower()
    if p.endswith((".db", ".sqlite", ".sqlite3")):
        return SqliteMetaStore(path)
    return FileMetaStore(path)
