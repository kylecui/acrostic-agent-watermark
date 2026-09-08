"""aawm-enterprise 测试公共 fixture。

测试纪律（项目约定）：固定 key+盐（rng_seed/固定 uid）、codec_mode 显式、
标定语料 = 核心包内置 demo_corpus（与 wheel 分发一致，独立于仓库 docs/）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让测试直接从包内 src 运行（无需 pip install）；核心 aawm 同理
_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (_ROOT / "src", Path(__file__).resolve().parent.parent / "src"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from aawm import Watermarker  # noqa: E402
from aawm_enterprise import AggregateWatermarker, FlushPolicy  # noqa: E402


def demo_corpus() -> list[str]:
    """核心包内置 demo_corpus（pyproject package-data 随 wheel 分发）。"""
    import aawm

    base = Path(aawm.__file__).parent / "data" / "demo_corpus"
    return [p.read_text(encoding="utf-8") for p in sorted(base.glob("*.md"))]


def long_zh_doc_raw() -> str:
    """全部 demo_corpus 拼接（≈24k 字）——聚合素材。

    注：核心 embed 换盐重试的随机盐使单篇文本容量 k 波动（1863-6233 字
    时 k=7~13 不稳定）；全量拼接后 k 稳定 >10，r=3 决策与 high 档判定
    才能跨运行稳定（测试禁随机性原则：盐随机，素材必须留足余量）。
    """
    import aawm

    base = Path(aawm.__file__).parent / "data" / "demo_corpus"
    return "\n\n".join(
        p.read_text(encoding="utf-8") for p in sorted(base.glob("*.md")))


@pytest.fixture(scope="session")
def long_zh_doc() -> str:
    """约 1800 字中文文档（performance.md），切成段做聚合素材。"""
    return long_zh_doc_raw()


@pytest.fixture(scope="session")
def wm() -> Watermarker:
    """中文 zero_cost Watermarker（demo_corpus 标定，固定语义）。"""
    return Watermarker(language="zh", codec_mode="zero_cost",
                       calibrate_corpus=demo_corpus())


@pytest.fixture()
def agg(wm: Watermarker) -> AggregateWatermarker:
    """默认策略聚合器（uid_redundancy=3）。"""
    return AggregateWatermarker(wm)


@pytest.fixture()
def strict_policy() -> FlushPolicy:
    return FlushPolicy(target_capacity=10, min_tier="medium",
                       idle_timeout_s=300.0, max_buffer_chars=200_000)
