"""密钥管理：master_key 的生成 / 加载 / 持久化 / 轮换。

支持三种后端：
    - memory：纯内存（默认，不持久化）
    - file：JSON 文件（hex 编码 master_key + 元数据）
    - env：环境变量（AAWM_MASTER_KEY，hex 编码）

密钥本身只是 32 字节随机数；派生逻辑（HKDF）仍在 aawm.keys。
本模块只负责"密钥从哪里来、存到哪里去"。

v0.13 密钥轮换（P1-6）：文件格式升级为多版本——

    {"version": 2, "active": 2,
     "keys": {"1": "<hex>", "2": "<hex>"}, "created": "..."}

旧格式（单 master_key）自动读为 version=1；rotate() 追加新版本并
切换 active。旧版本保留（"双钥并行期"）：旧水印的 meta 记录嵌入时
的 key_version，trace 据此用对应版本的密钥解码——轮换不破坏历史
溯源。紧急轮换（怀疑泄漏）后应尽快删除被泄版本：
``aawm rotate-key --key key.json --drop 1``。
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Dict, List, Optional, Union

# 默认 master_key 长度（字节）
_DEFAULT_KEY_LEN = 32
# 环境变量名
_ENV_VAR = "AAWM_MASTER_KEY"


class KeyStore:
    """master_key 的存储抽象（多版本，支持轮换）。

    用法::

        # 生成并持久化到文件
        ks = KeyStore.from_file("key.json", create=True)
        key = ks.get()

        # 轮换（双钥并行：旧 key 仍可按版本取用）
        ks.rotate()
        ks.save("key.json")

        # 从环境变量加载
        ks = KeyStore.from_env()
        key = ks.get()

        # 纯内存（每次随机）
        ks = KeyStore()
        key = ks.get()
    """

    def __init__(self, master_key: Optional[bytes] = None) -> None:
        """纯内存 KeyStore。master_key=None 时随机生成（version 1）。"""
        key = master_key if master_key is not None else secrets.token_bytes(_DEFAULT_KEY_LEN)
        if len(key) < 16:
            raise ValueError("master_key too short (>= 16 bytes required)")
        self._keys: Dict[int, bytes] = {1: key}
        self._active: int = 1

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: Union[str, Path], *, create: bool = False) -> "KeyStore":
        """从 JSON 文件加载 master_key。

        文件格式（v0.13 多版本）::

            {"version": 2, "active": 2,
             "keys": {"1": "<hex>", "2": "<hex>"}, "created": "..."}

        旧格式（v0.13 前单密钥）自动兼容::

            {"master_key": "<hex>", "version": 1, "created": "2026-08-19"}

        Args:
            path: 文件路径
            create: 文件不存在时是否创建新密钥并写入
        """
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            ks = cls.__new__(cls)
            if "keys" in data:
                ks._keys = {int(v): bytes.fromhex(h) for v, h in data["keys"].items()}
                ks._active = int(data.get("active") or max(ks._keys))
            else:
                ks._keys = {1: bytes.fromhex(data["master_key"])}
                ks._active = 1
            if ks._active not in ks._keys:
                raise ValueError(f"key 文件 active 版本 {ks._active} 不在 keys 中")
            for k in ks._keys.values():
                if len(k) < 16:
                    raise ValueError("master_key too short (>= 16 bytes required)")
            return ks
        if not create:
            raise FileNotFoundError(f"key file not found: {p}")
        # 创建新密钥
        ks = cls()
        ks._save_to_file(p)
        return ks

    @classmethod
    def from_env(cls, var: str = _ENV_VAR) -> "KeyStore":
        """从环境变量加载（hex 编码；单密钥，无轮换语义）。"""
        raw = os.environ.get(var)
        if not raw:
            raise ValueError(f"env var {var} not set")
        return cls(bytes.fromhex(raw))

    @classmethod
    def from_any(
        cls,
        master_key: Optional[Union[bytes, str]] = None,
        *,
        key_file: Optional[Union[str, Path]] = None,
        env_var: Optional[str] = None,
    ) -> "KeyStore":
        """统一加载入口，按优先级尝试：master_key 直传 > 文件 > 环境变量 > 内存生成。

        Args:
            master_key: 直接传入的密钥（bytes 或 hex 字符串）
            key_file: 密钥文件路径
            env_var: 环境变量名（None 用默认 AAWM_MASTER_KEY）
        """
        if master_key is not None:
            if isinstance(master_key, str):
                return cls(bytes.fromhex(master_key))
            return cls(master_key)
        if key_file is not None:
            return cls.from_file(key_file, create=True)
        if env_var is not None or os.environ.get(_ENV_VAR):
            return cls.from_env(env_var or _ENV_VAR)
        # 兜底：纯内存
        return cls()

    # ------------------------------------------------------------------
    # 核心
    # ------------------------------------------------------------------

    def get(self) -> bytes:
        """获取当前 active 的 master_key。"""
        return self._keys[self._active]

    @property
    def active_version(self) -> int:
        """当前 active 密钥版本号。"""
        return self._active

    def versions(self) -> List[int]:
        """全部密钥版本号（升序）。"""
        return sorted(self._keys)

    def get_version(self, version: int) -> bytes:
        """按版本号取密钥（旧水印 trace 用；不存在则 KeyError）。"""
        return self._keys[version]

    def rotate(self, new_key: Optional[bytes] = None) -> int:
        """轮换：追加新密钥版本并设为 active。

        旧版本保留（双钥并行期）——旧水印 meta 记录的 key_version
        仍可解码。返回新版本号。
        """
        key = new_key if new_key is not None else secrets.token_bytes(_DEFAULT_KEY_LEN)
        if len(key) < 16:
            raise ValueError("master_key too short (>= 16 bytes required)")
        new_v = max(self._keys) + 1
        self._keys[new_v] = key
        self._active = new_v
        return new_v

    def drop_version(self, version: int) -> None:
        """删除某版本密钥（确认泄漏后的应急清理）。

        不允许删除 active 版本（先 rotate 再 drop 旧的）。
        """
        if version == self._active:
            raise ValueError("不能删除 active 版本（先 rotate 再 drop 旧版本）")
        if version not in self._keys:
            raise KeyError(f"版本 {version} 不存在")
        del self._keys[version]

    def save(self, path: Union[str, Path]) -> None:
        """持久化到文件。"""
        self._save_to_file(Path(path))

    def export_env(self, var: str = _ENV_VAR) -> str:
        """返回 ``export AAWM_MASTER_KEY=<hex>`` 形式字符串（active 密钥）。"""
        return f"export {var}={self.get().hex()}"

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _save_to_file(self, p: Path) -> None:
        from datetime import datetime, timezone

        if len(self._keys) == 1 and self._active == 1:
            # 单密钥：保持旧格式（零行为变更，v0.13 前的工具可读）
            data = {
                "master_key": self._keys[1].hex(),
                "version": 1,
                "created": datetime.now(timezone.utc).isoformat(),
                "length": len(self._keys[1]),
            }
        else:
            data = {
                "version": 2,
                "active": self._active,
                "keys": {str(v): k.hex() for v, k in sorted(self._keys.items())},
                "created": datetime.now(timezone.utc).isoformat(),
                "length": len(self.get()),
            }
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # 限制权限（类 Unix）
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def generate_key(length: int = _DEFAULT_KEY_LEN) -> bytes:
    """生成随机 master_key（便捷函数）。"""
    return secrets.token_bytes(length)
