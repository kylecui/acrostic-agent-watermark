"""exp_hybrid_codec.py: 混合词典（零感打底+词林补带）效果验证。

核心假设：零感书面 k≈12、口语 k≈2 → gap≈0.71 无区分力。
混合后词林内容词补齐空带 → k↑ → gap 尺度↑ → margin 恢复区分力。

测量三 codec × 两语料：
  - capacity k 分布
  - rt gap 分布（正确匹配的 gap，越大越好）
  - 攻击后 gap 分布 + 错报率
  - margin 有效阈值（错报降到 0 的最小 margin）

三 codec：zero（零感 149 组）、cilin（词林语料过滤）、hybrid（混合）
"""
from __future__ import annotations

import math
import random
import sys
from collections import defaultdict

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import build_zero_cost_zh_codec
from exp_zero_cost_robust import (
    KEY, SALT,
    make_docs_written, make_docs_paws,
    build_cilin_codec, build_hybrid_codec, build_zero_codec,
    synonym_attack, paragraph_delete,
)

N_DOCS = 8
N_SEED = 5
MARGINS = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]


def _cands(k: int, uid: int, n_reg, seed: int) -> list[int]:
    """生成候选库：含真值 uid + 随机填充到 n_reg 个（上限 2^k）。"""
    cap = 1 << k
    if n_reg == "full" or n_reg >= cap:
        return list(range(cap))
    pool = [uid]
    r = random.Random(seed)
    while len(pool) < n_reg:
        c = r.randrange(cap)
        if c not in pool:
            pool.append(c)
    return pool


def measure_capacity(codec, docs, label):
    """Part 1: 容量 k 分布。"""
    ks = []
    for doc in docs:
        k = codec.capacity(doc)
        ks.append(k)
    ks.sort()
    med = ks[len(ks) // 2]
    mean = sum(ks) / len(ks)
    print(f"  [{label}] k: mean={mean:.1f} median={med} "
          f"min={ks[0]} max={ks[-1]} dist={ks}")
    return ks


def measure_gap(codec, docs, label, n_reg=4):
    """Part 2: rt gap 分布（正确匹配的 gap）。"""
    gaps = []
    for i, doc in enumerate(docs):
        k = codec.capacity(doc)
        if k < 2:
            continue
        for s in range(N_SEED):
            uid = random.Random(1000 + i * 11 + s).randrange(1 << k)
            mm, used = codec.embed_adaptive(
                doc, uid, n_bits=k, rng=random.Random(4000 + i * 11 + s))
            cands = _cands(k, uid, n_reg, 8000 + i * 13 + s)
            best, sc, gap = codec.soft_match_adaptive(mm, cands, used)
            if best == uid:
                gaps.append(gap)
    if gaps:
        gaps.sort()
        med = gaps[len(gaps) // 2]
        mean = sum(gaps) / len(gaps)
        print(f"  [{label}] rt gap: n={len(gaps)} mean={mean:.2f} "
              f"med={med:.2f} min={gaps[0]:.2f} max={gaps[-1]:.2f}")
    else:
        print(f"  [{label}] rt gap: (no valid samples)")
    return gaps


def measure_attack_gap(codec, docs, label, n_reg=4):
    """Part 3: s30 攻击后 gap 分布 + 错报率。"""
    ok_gaps = []
    bad_gaps = []
    for i, doc in enumerate(docs):
        k = codec.capacity(doc)
        if k < 2:
            continue
        for s in range(N_SEED):
            uid = random.Random(1000 + i * 11 + s).randrange(1 << k)
            mm, used = codec.embed_adaptive(
                doc, uid, n_bits=k, rng=random.Random(4000 + i * 11 + s))
            cands = _cands(k, uid, n_reg, 8000 + i * 13 + s)
            # s30 攻击
            t, _ = synonym_attack(codec, mm, 0.30, 6000 + i * 10 + s)
            best, sc, gap = codec.soft_match_adaptive(t, cands, used)
            if best == uid:
                ok_gaps.append(gap)
            else:
                bad_gaps.append(gap)
    total = len(ok_gaps) + len(bad_gaps)
    err_rate = len(bad_gaps) / total * 100 if total else 0
    if ok_gaps:
        ok_gaps.sort()
        ok_med = ok_gaps[len(ok_gaps) // 2]
        ok_mean = sum(ok_gaps) / len(ok_gaps)
    else:
        ok_med = ok_mean = 0
    if bad_gaps:
        bad_gaps.sort()
        bad_med = bad_gaps[len(bad_gaps) // 2]
        bad_mean = sum(bad_gaps) / len(bad_gaps)
    else:
        bad_med = bad_mean = 0
    print(f"  [{label}] s30: ok={len(ok_gaps)}/{total} ({100-err_rate:.0f}%)  "
          f"err={len(bad_gaps)}/{total} ({err_rate:.0f}%)")
    print(f"    ok gap: mean={ok_mean:.2f} med={ok_med:.2f}  |  "
          f"bad gap: mean={bad_mean:.2f} med={bad_med:.2f}")
    return ok_gaps, bad_gaps


def measure_margin(codec, docs, label, n_reg=4):
    """Part 4: margin 阈值 vs 错报率/误abstain率。"""
    # 收集 rt 和 s30 的 (correct, gap) 对
    samples = []  # (is_correct, gap)
    for i, doc in enumerate(docs):
        k = codec.capacity(doc)
        if k < 2:
            continue
        for s in range(N_SEED):
            uid = random.Random(1000 + i * 11 + s).randrange(1 << k)
            mm, used = codec.embed_adaptive(
                doc, uid, n_bits=k, rng=random.Random(4000 + i * 11 + s))
            cands = _cands(k, uid, n_reg, 8000 + i * 13 + s)
            # rt
            best, sc, gap = codec.soft_match_adaptive(mm, cands, used)
            samples.append(("rt", best == uid, gap))
            # s30
            t, _ = synonym_attack(codec, mm, 0.30, 6000 + i * 10 + s)
            best, sc, gap = codec.soft_match_adaptive(t, cands, used)
            samples.append(("s30", best == uid, gap))

    rt_n = sum(1 for a, _, _ in samples if a == "rt")
    s30_n = sum(1 for a, _, _ in samples if a == "s30")
    line = f"  [{label}] margin sweep (n_reg={n_reg}):"
    line += f"\n    {'margin':>6}  {'s30_err':>7}  {'s30_abst':>8}  {'rt_abst':>7}"
    for m in MARGINS:
        s30_err = s30_abst = rt_abst = 0
        for atk, correct, gap in samples:
            if atk == "s30":
                if gap < m:
                    s30_abst += 1
                elif not correct:
                    s30_err += 1
            else:  # rt
                if gap < m:
                    rt_abst += 1
        line += (f"\n    {m:6.1f}  {s30_err:7d}  {s30_abst:8d}  {rt_abst:7d}")
    print(line)


def main():
    print("=" * 70)
    print("混合词典效果验证")
    print("=" * 70)

    for corpus_name, make_docs in [("书面", make_docs_written), ("口语", make_docs_paws)]:
        docs = make_docs(N_DOCS)
        if not docs:
            print(f"\n[{corpus_name}] 无可用文档，跳过")
            continue
        print(f"\n{'='*60}")
        print(f"语料: {corpus_name} ({len(docs)} 篇)")
        print(f"{'='*60}")

        codecs = {}
        print("\n--- 构建 codecs ---")
        print("  [zero]", end="")
        codecs["zero"] = build_zero_codec(docs)
        print(f"  组数={len(codecs['zero']._groups)}")
        print("  [cilin]", end="")
        codecs["cilin"] = build_cilin_codec(docs)
        print(f"  组数={len(codecs['cilin']._groups)}")
        codecs["hybrid"] = build_hybrid_codec(docs)
        print(f"  组数={len(codecs['hybrid']._groups)}")

        print("\n--- Part 1: 容量 k 分布 ---")
        for name, c in codecs.items():
            measure_capacity(c, docs, f"{corpus_name}/{name}")

        print("\n--- Part 2: rt gap 分布 (n_reg=4) ---")
        for name, c in codecs.items():
            measure_gap(c, docs, f"{corpus_name}/{name}", n_reg=4)

        print("\n--- Part 3: s30 攻击后 gap + 错报 (n_reg=4) ---")
        for name, c in codecs.items():
            measure_attack_gap(c, docs, f"{corpus_name}/{name}", n_reg=4)

        print("\n--- Part 4: margin 阈值扫描 (n_reg=4) ---")
        for name, c in codecs.items():
            measure_margin(c, docs, f"{corpus_name}/{name}", n_reg=4)


if __name__ == "__main__":
    main()
