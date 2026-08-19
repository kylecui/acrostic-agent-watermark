"""密钥管理：master_key 的生成 / 加载 / 持久化。

支持三种后端：
    - memory：纯内存（默认，不持久化）
    - file：JSON 文件（hex 编码 master_key + 元数据）
    - env：环境变量（AAWM_MASTER_KEY，hex 编码）

密钥本身只是 32 字节随机数；派生逻辑（HKDF）仍在 aawm.keys。
本模块只负责"密钥从哪里来、存到哪里去"。
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Optional, Union

# 默认 master_key 长度（字节）
_DEFAULT_KEY_LEN = 32
# 环境变量名
_ENV_VAR = "AAWM_MASTER_KEY"


class KeyStore:
    """master_key 的存储抽象。

    用法::

        # 生成并持久化到文件
        ks = KeyStore.from_file("key.json", create=True)
        key = ks.get()

        # 从环境变量加载
        ks = KeyStore.from_env()
        key = ks.get()

        # 纯内存（每次随机）
        ks = KeyStore()
        key = ks.get()
    """

    def __init__(self, master_key: Optional[bytes] = None) -> None:
        """纯内存 KeyStore。master_key=None 时随机生成。"""
        self._key = master_key if master_key is not None else secrets.token_bytes(_DEFAULT_KEY_LEN)
        if len(self._key) < 16:
            raise ValueError("master_key too short (>= 16 bytes required)")

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: Union[str, Path], *, create: bool = False) -> "KeyStore":
        """从 JSON 文件加载 master_key。

        文件格式::

            {"master_key": "<hex>", "version": 1, "created": "2026-08-19"}

        Args:
            path: 文件路径
            create: 文件不存在时是否创建新密钥并写入
        """
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            key = bytes.fromhex(data["master_key"])
            return cls(key)
        if not create:
            raise FileNotFoundError(f"key file not found: {p}")
        # 创建新密钥
        ks = cls()
        ks._save_to_file(p)
        return ks

    @classmethod
    def from_env(cls, var: str = _ENV_VAR) -> "KeyStore":
        """从环境变量加载（hex 编码）。"""
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
        """获取 master_key。"""
        return self._key

    def save(self, path: Union[str, Path]) -> None:
        """持久化到文件。"""
        self._save_to_file(Path(path))

    def export_env(self, var: str = _ENV_VAR) -> str:
        """返回 ``export AAWM_MASTER_KEY=<hex>`` 形式字符串。"""
        return f"export {var}={self._key.hex()}"

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _save_to_file(self, p: Path) -> None:
        from datetime import datetime, timezone

        data = {
            "master_key": self._key.hex(),
            "version": 1,
            "created": datetime.now(timezone.utc).isoformat(),
            "length": len(self._key),
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
