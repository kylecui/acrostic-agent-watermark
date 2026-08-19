"""UID 注册库：UID ↔ 用户别名映射 + 最近邻匹配。

支持两种后端：
    - memory：纯内存 dict（默认，进程结束即丢失）
    - file：JSON 文件持久化

设计要点：
    - UID 是 16-bit 无符号整数（0 ~ 65535），与 GreenlistCodec 的 n_bands 对齐
    - 别名是业务侧的可读字符串（如 "agent-cuiyin"）
    - nearest_match 基于 masked_hamming 逻辑：只比较有信号的频带
    - 自动分配 UID：从 1 开始递增（0 保留给"未注册/未知"）
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

# UID 位宽（与 GreenlistCodec 默认 n_bands=16 对齐）
_UID_BITS = 16
_MAX_UID = (1 << _UID_BITS) - 1  # 65535
_RESERVED_UID = 0  # 保留给"未注册/未知"


class UIDRegistry:
    """UID ↔ 用户别名映射 + 最近邻匹配。

    用法::

        # 内存模式
        reg = UIDRegistry()
        uid = reg.register("agent-cuiyin")  # 自动分配
        alias = reg.lookup(uid)             # "agent-cuiyin"

        # 文件持久化
        reg = UIDRegistry(backend="file", path="registry.json")
        uid = reg.register("agent-cuiyin")

        # 最近邻匹配（溯源时用）
        match = reg.nearest_match(0x1234, max_hamming=3)
        # -> (uid, "agent-cuiyin", hamming_dist) or None
    """

    def __init__(
        self,
        backend: str = "memory",
        path: Optional[Union[str, Path]] = None,
        *,
        uid_bits: int = _UID_BITS,
    ) -> None:
        if backend not in ("memory", "file"):
            raise ValueError(f"unsupported backend: {backend}")
        if backend == "file" and path is None:
            raise ValueError("file backend requires path")
        self._backend = backend
        self._path = Path(path) if path else None
        self._uid_bits = uid_bits
        self._max_uid = (1 << uid_bits) - 1
        self._lock = threading.Lock()
        # 两个方向的映射
        self._uid2alias: Dict[int, str] = {}
        self._alias2uid: Dict[str, int] = {}
        self._next_uid = 1  # 从 1 开始，0 保留
        if backend == "file":
            self._load()

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(self, user_alias: str, uid: Optional[int] = None) -> int:
        """注册用户，返回 UID。

        Args:
            user_alias: 用户别名（如 "agent-cuiyin"）
            uid: 指定 UID；None 时自动分配下一个可用 UID

        Returns:
            分配的 UID

        Raises:
            ValueError: 别名已存在 / UID 已被占用 / UID 超范围
        """
        with self._lock:
            if user_alias in self._alias2uid:
                # 幂等：已注册则返回原 UID
                return self._alias2uid[user_alias]
            if uid is not None:
                if uid < 0 or uid > self._max_uid:
                    raise ValueError(f"uid out of range [0, {self._max_uid}]")
                if uid in self._uid2alias:
                    raise ValueError(f"uid {uid} already in use by '{self._uid2alias[uid]}'")
            else:
                uid = self._alloc_uid()
            self._uid2alias[uid] = user_alias
            self._alias2uid[user_alias] = uid
            self._persist()
            return uid

    def resolve_alias(self, user_alias: str) -> int:
        """别名 → UID。不存在则自动注册。"""
        with self._lock:
            if user_alias in self._alias2uid:
                return self._alias2uid[user_alias]
        return self.register(user_alias)

    def lookup(self, uid: int) -> Optional[str]:
        """UID → 别名。不存在返回 None。"""
        with self._lock:
            return self._uid2alias.get(uid)

    # ------------------------------------------------------------------
    # 最近邻匹配
    # ------------------------------------------------------------------

    def nearest_match(
        self,
        uid: int,
        max_hamming: int = 3,
    ) -> Optional[Tuple[int, str, int]]:
        """在注册库中找与给定 UID 汉明距最近的条目。

        Args:
            uid: 待匹配的 UID（检测端解码出来的）
            max_hamming: 最大允许汉明距（超过则判为无匹配）

        Returns:
            (uid, alias, hamming_dist) 或 None
        """
        best: Optional[Tuple[int, str, int]] = None
        with self._lock:
            for candidate_uid, alias in self._uid2alias.items():
                dist = bin(uid ^ candidate_uid).count("1")
                if best is None or dist < best[2]:
                    best = (candidate_uid, alias, dist)
        if best is not None and best[2] <= max_hamming:
            return best
        return None

    def masked_nearest_match(
        self,
        uid: int,
        active_mask: int = 0xFFFF,
        max_hamming: int = 3,
    ) -> Optional[Tuple[int, str, int]]:
        """带掩码最近邻匹配：只比较 active_mask 标记的位（有信号带）。

        Args:
            uid: 待匹配的 UID
            active_mask: 掩码，bit=1 表示该位参与比较（n<2 的带应置 0）
            max_hamming: 最大允许汉明距（按参与比较的位数计）

        Returns:
            (uid, alias, hamming_dist) 或 None
        """
        best: Optional[Tuple[int, str, int]] = None
        with self._lock:
            for candidate_uid, alias in self._uid2alias.items():
                diff = (uid ^ candidate_uid) & active_mask
                dist = bin(diff).count("1")
                if best is None or dist < best[2]:
                    best = (candidate_uid, alias, dist)
        if best is not None and best[2] <= max_hamming:
            return best
        return None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_all(self) -> Dict[int, str]:
        """返回所有 UID → 别名映射的副本。"""
        with self._lock:
            return dict(self._uid2alias)

    def __len__(self) -> int:
        with self._lock:
            return len(self._uid2alias)

    def __contains__(self, item: Union[int, str]) -> bool:
        with self._lock:
            if isinstance(item, int):
                return item in self._uid2alias
            return item in self._alias2uid

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _alloc_uid(self) -> int:
        """分配下一个可用 UID（线性探测）。"""
        uid = self._next_uid
        while uid in self._uid2alias and uid <= self._max_uid:
            uid += 1
        if uid > self._max_uid:
            # 回绕到 1 重试
            uid = 1
            while uid in self._uid2alias:
                uid += 1
                if uid >= self._next_uid:
                    raise ValueError("UID space exhausted")
        self._next_uid = uid + 1
        return uid

    def _persist(self) -> None:
        if self._backend != "file" or self._path is None:
            return
        data = {
            "uid_bits": self._uid_bits,
            "entries": [
                {"uid": uid, "alias": alias}
                for uid, alias in sorted(self._uid2alias.items())
            ],
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        for entry in data.get("entries", []):
            uid = entry["uid"]
            alias = entry["alias"]
            self._uid2alias[uid] = alias
            self._alias2uid[alias] = uid
        if self._uid2alias:
            self._next_uid = max(self._uid2alias) + 1
