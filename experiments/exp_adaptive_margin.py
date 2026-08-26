"""exp_adaptive_margin.py: margin abstain × 候选库规模 的联合权衡。

上一版结论：满容量候选（2^k）下 margin 无区分力。
机制（本轮钉死）：
    soft 得分 s(c) = Σ_b z_b·(2·bit_b(c)−1)，满容量候选库里"单带翻转"
    近邻必然存在 → 无论真值正确还是被替换污染翻转带，gap 都 ≈ 2·min|z_b|
    （实验值 ~0.71）。gap 与正确性解耦 → 绝对阈值原理上失效。
    这解释了 "margin 越大越糟"：m≥2 把样本全 abstain，或只放行
    错误候选（"自信地错"）。

本版新增维度：候选库规模 N_reg（真实部署形态 = 稀疏注册库）。
推理：
    · 稀疏库次优是"多带翻转"远邻 → 真值正确时 gap 大；
    · 攻击翻转带 t 且翻转候选 c* ∈ 注册库 → 真值掉到次优，
      最优 c* 与次优（其他注册候选）gap 回到近邻尺度 → 小；
    · 若翻转候选 ∉ 注册库 → 真值仍是最高，只是得分降低。
    ⇒ 稀疏库下 margin 应恢复区分力：错报可清零，代价是 abstain。

扫描：候选规模 × margin，输出 错报% / abstain% / 命中%（端到端可用率）。

运行：python experiments/exp_adaptive_margin.py
"""
from __future__ import annotations

import math
import random
import statistics
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from exp_real_corpus import synonym_attack
from exp_zero_cost_robust import (
    build_cilin_codec,
    build_zero_codec,
    make_docs_paws,
    make_docs_written,
    paragraph_delete,
)

N_DOCS = 8
N_SEED = 5
REG_SIZES = ["full", 64, 16, 4]
MARGINS = [0.0, 1.0, 2.0, 3.0, 4.0]


def _cands(k: int, uid: int, n_reg, seed: int) -> list[int]:
    if n_reg == "full":
        return list(range(1 << k))
    cap = min(n_reg, 1 << k)  # n_reg 超过 2^k 时退化为满容量
    pool = [uid]
    r = random.Random(seed)
    while len(pool) < cap:
        c = r.randrange(1 << k)
        if c not in pool:
            pool.append(c)
    return pool


def sweep(codec, docs, tag: str, codec_name: str) -> None:
    print(f"\n===== {codec_name} {tag} · 每候选规模 {N_DOCS}篇×{N_SEED}seed =====")
    print(f"{'候选':<6}{'margin':>7}{'abstain%':>10}{'错报%':>8}{'命中%':>8}{'返回n':>7}"
          f"   rt误abstain")
    for n_reg in REG_SIZES:
        rt_ok_gaps: list[float] = []
        att_pool: list[tuple[bool, float, str]] = []
        sys.stdout.write(f"  [{n_reg}]"); sys.stdout.flush()
        for i in range(N_DOCS):
            doc = docs[i]
            k = codec.capacity(doc)
            if k < 2:
                continue
            uid = random.Random(1000 + i).randrange(1 << k)
            for s in range(N_SEED):
                mm, used = codec.embed_adaptive(
                    doc, uid, n_bits=k, rng=random.Random(4000 + i * 11 + s))
                cands = _cands(k, uid, n_reg, 8000 + i * 13 + s)
                b, sc, g = codec.soft_match_adaptive(mm, cands, used)
                rt_ok_gaps.append(g if b == uid else float("nan"))
                for name, rate in (("s30", 0.30), ("s50", 0.50), ("del.5", 0.50)):
                    if name == "del.5":
                        t = paragraph_delete(mm, rate, 5000 + i * 10 + s)
                    else:
                        t, _ = synonym_attack(codec, mm, rate, 6000 + i * 10 + s)
                    b, sc, g = codec.soft_match_adaptive(t, cands, used)
                    att_pool.append((b == uid, g, name))
                sys.stdout.write("."); sys.stdout.flush()
        sys.stdout.write("\n"); sys.stdout.flush()
        label = "full" if n_reg == "full" else f"2^{int(math.log2(n_reg))}"
        for m in MARGINS:
            ret = n_ok = n_bad = 0
            for ok, g, a in att_pool:
                if g >= m:
                    ret += 1
                    if ok:
                        n_ok += 1
                    else:
                        n_bad += 1
            tot = len(att_pool)
            abstain = 100 * (1 - ret / tot)
            bad = 100 * n_bad / ret if ret else 0.0
            hit = 100 * n_ok / tot
            rt_ok = [g for g in rt_ok_gaps if not math.isnan(g)]
            rt_abs = 100 * sum(1 for g in rt_ok if g < m) / len(rt_ok) if rt_ok else 0.0
            print(f"  {label:<5}{m:>6.1f}{abstain:>9.1f}%{bad:>7.1f}%{hit:>7.1f}%{ret:>7d}"
                  f"   {rt_abs:.0f}%")
    print()


def main() -> None:
    docs_w = make_docs_written()
    docs_p = make_docs_paws()
    print(f"书面语 {len(docs_w)} 篇 / 口语 {len(docs_p)} 篇\n")
    cw = build_cilin_codec(docs_w)
    zw = build_zero_codec(docs_w)
    sweep(cw, docs_w, "书面语", "词林")
    sweep(zw, docs_w, "书面语", "零感")
    sweep(zw, docs_p, "口语", "零感")


if __name__ == "__main__":
    main()
