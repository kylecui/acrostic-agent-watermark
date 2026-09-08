"""依赖方向 CI（设计 §3.1 铁律）：aawm_enterprise → aawm，禁止反向。

在仓库根运行时生效；pip 安装态（无核心源码树）自动 skip。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent   # aawm-enterprise/ 的上级
CORE_SRC = ROOT / "src" / "aawm"
ENT_SRC = Path(__file__).resolve().parent.parent / "src" / "aawm_enterprise"


@pytest.mark.skipif(not CORE_SRC.is_dir(), reason="not in repo checkout")
def test_core_never_imports_enterprise():
    """MIT 核心源码树不得引用 aawm_enterprise（文件级隔离）。"""
    offenders = []
    for py in CORE_SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "aawm_enterprise" in text or "aawm-enterprise" in text:
            offenders.append(str(py))
    assert not offenders, f"MIT core references enterprise package: {offenders}"


@pytest.mark.skipif(not ENT_SRC.is_dir(), reason="not in repo checkout")
def test_enterprise_imports_only_core_and_stdlib():
    """enterprise 顶层 import 只允许 aawm* 与标准库/本地相对导入。"""
    import ast

    allowed_prefixes = ("aawm",)   # 核心包（含 aawm.plugins.*）
    optional_third_party = {"fastapi", "pydantic"}   # [proxy] extra
    for py in ENT_SRC.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root in allowed_prefixes or root in optional_third_party \
                        or root in sys.stdlib_module_names, \
                        f"{py.name}: unexpected import {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                root = (node.module or "").split(".")[0]
                assert root in allowed_prefixes or root in optional_third_party \
                    or root in sys.stdlib_module_names, \
                    f"{py.name}: unexpected import from {node.module}"
