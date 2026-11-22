#!/usr/bin/env python3
"""paper_common.py: 论文实验公共库（PLAN I-1）。

所有论文实验（paper_e*.py）复用本模块，禁止复制粘贴。
纪律（PLAN §3）：一切实验 fixed seed；一切断言可溯源到本模块的
save_result() 产物。

实验元数据约定：每个结果 JSON 含
  {experiment, seed, aawm_version, commit, config, results, generated_at}
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DEMO_DIR = "src/aawm/data/demo_corpus"
DEMO_GLOB = DEMO_DIR + "/*.md"
EN_FILES = ["corpus/en/pg84.txt", "corpus/en/pg1342.txt", "corpus/en/pg2701.txt"]
ZH_GLOB = "corpus/zh/*.txt"

DEFAULT_SEED = 20260911


# ---------------------------------------------------------------------------
# 环境与溯源
# ---------------------------------------------------------------------------

def aawm_version() -> str:
    from aawm import __version__
    return __version__


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def save_result(experiment: str, seed: int, config: dict, results: dict,
                path: Optional[str] = None) -> str:
    """结果落盘（papers/paperB/data/ 或调用方指定），返回路径。"""
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "..",
                            "papers", "paperB", "data", f"{experiment}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "experiment": experiment,
        "seed": seed,
        "aawm_version": aawm_version(),
        "commit": git_commit(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config": config,
        "results": results,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------------------
# 语料（全部无水印 null 文本）
# ---------------------------------------------------------------------------

def read_all(paths, encoding="utf-8") -> str:
    return "\n".join(open(p, encoding=encoding, errors="ignore").read()
                     for p in paths)


def demo_corpus_text() -> str:
    return read_all(sorted(glob.glob(DEMO_GLOB)))


def load_chunks(text: str, size: int, n: int, seed: int,
                dedup: bool = True) -> List[str]:
    """从大文本切 size 字符块（fixed seed 可复现）。dedup=True 时
    按内容哈希去重（红楼梦章回切分易重叠）。"""
    rng = random.Random(seed)
    flat = " ".join(text.split())
    step = max(1, size // 2)
    population = max(1, (max(1, len(flat) - size)) // step)
    k = min(n * 3, population)
    starts = sorted(s * step for s in rng.sample(range(population), k))
    out, seen = [], set()
    for s in starts:
        chunk = flat[s:s + size]
        if len(chunk) < size * 0.9:
            continue
        if dedup:
            h = hashlib.sha256(chunk.encode()).hexdigest()[:16]
            if h in seen:
                continue
            seen.add(h)
        out.append(chunk)
        if len(out) >= n:
            break
    return out


def make_domain_corpus(name: str, size: int, n: int,
                       seed: int = DEFAULT_SEED) -> List[str]:
    """论文统一语料入口。域定义与 exp_cross_domain_fpr 对齐并扩展。

    支持域：demo_in_domain / en_literature / zh_literature /
    code_comment / short_fragments；repetitive_template 单独构造。
    """
    if name == "demo_in_domain":
        return load_chunks(demo_corpus_text(), size, n, seed)
    if name == "en_literature":
        return load_chunks(read_all(EN_FILES), size, n, seed + 1)
    if name == "zh_literature":
        return load_chunks(read_all(sorted(glob.glob(ZH_GLOB))),
                           size, n, seed + 2)
    if name == "code_comment":
        # 代码注释风格：从项目自身源码的注释与 docstring 抽取（真实、
        # 同机可造、与文学/技术散文均不同域）
        return _code_comment_chunks(size, n, seed + 4)
    if name == "short_fragments":
        return load_chunks(demo_corpus_text(), size, n, seed + 3)
    raise ValueError(f"未知域: {name}")


def _code_comment_chunks(size: int, n: int, seed: int) -> List[str]:
    import re
    comments = []
    for f in glob.glob("src/aawm/**/*.py", recursive=True):
        src = open(f, encoding="utf-8").read()
        comments += re.findall(r'"""([^"]{40,})"""', src)
        comments += re.findall(r"#\s(.{30,})", src)
        comments += re.findall(r"'''\s*([^']{40,})", src)
    rng = random.Random(seed)
    rng.shuffle(comments)
    out, cur = [], ""
    for c in comments:
        c = " ".join(c.split())
        if not c:
            continue
        cur = (cur + " " + c).strip()
        if len(cur) >= size:
            out.append(cur[:size])
            cur = ""
            if len(out) >= n:
                break
    return out


def repetitive_template_texts(n: int, seed: int = DEFAULT_SEED) -> List[str]:
    """病态模板域（PLAN F3）：短句重复、零感词饱和。"""
    tmpl = (
        "项目管理不仅需要规划，而且需要持续跟踪。团队成员应当明确目标，"
        "并且在执行过程中及时调整。质量标准必须统一，因此沟通非常重要。"
    )
    return [tmpl * (6 + i % 5) for i in range(n)]


# ---------------------------------------------------------------------------
# 水印侧公共件
# ---------------------------------------------------------------------------

def make_registry(n_users: int, base_uid: int = 1000):
    from aawm.plugins.registry import UIDRegistry
    reg = UIDRegistry()
    for i in range(n_users):
        reg.register(f"user-{i}", uid=base_uid + i)
    return reg


def existence_metrics(wm, texts: Sequence[str], lang: str,
                      registry_attribution: bool = True) -> Dict:
    """显式盐路径的存在性 FPR / 归因 FPR（null 文本用）。

    每文本一个新鲜随机盐——模拟"攻击者重排后验证者持盐检测"。
    """
    from aawm.keys import generate_session_salt
    fp_exist = fp_attr = 0
    n = 0
    for t in texts:
        r = wm.trace(t, session_salt=generate_session_salt(), language=lang)
        n += 1
        if r.watermarked:
            fp_exist += 1
        if r.user is not None:
            fp_attr += 1
    return {"fpr_existence": fp_exist / n if n else 0.0,
            "fpr_attribution": fp_attr / n if n else 0.0,
            "n": n}


def detection_rates(wm, texts: Sequence[str], lang: str,
                    uid: int = 1000) -> Dict:
    """TPR 对照：同一批文本嵌入后（显式盐+meta 路径）的检出率。"""
    tp = n = 0
    for t in texts:
        emb = wm.embed(t, user_id=uid, language=lang)
        r = wm.trace(emb.watermarked_text, session_salt=emb.session_salt,
                     bands=emb.bands, language=lang)
        n += 1
        if r.watermarked:
            tp += 1
    return {"tpr": tp / n if n else 0.0, "n": n}


def fit_calibration(wm, corpus: Sequence[str]) -> None:
    """域内标定（多篇语料，PLAN §3 口径）。语料须 ≥10 篇。"""
    assert len(corpus) >= 10, "标定语料 <10 篇，σ 估计不可靠（PLAN §3）"
    wm.calibrate_null_model(list(corpus))
