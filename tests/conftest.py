"""pytest 全局配置：把 src/ 注入 sys.path。

此前依赖"某个先执行的测试文件自己 sys.path.insert"的全局副作用，
单文件/子集运行（pytest tests/test_x.py）会因找不到 aawm 而 collection
失败。这里在 conftest 层统一保证 src 布局可导入。
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
