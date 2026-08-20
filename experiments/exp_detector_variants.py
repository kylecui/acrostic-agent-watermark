#!/usr/bin/env python3
"""exp_detector_variants.py: 检测端判决变体 —— 翻转带抑制。

失守机制：s50 攻击翻转 3-5 个带，其 z 符号反转。soft_match 幅度积分
s(c)=Σ z_b·sgn 中，翻转带贡献 −|z_b| 给正确候选、+|z_b| 给错误候选，
几个大 |z| 翻转带足以翻盘。但翻转带是少数（~3/16），带级投票
（每带 ±1 等权）对翻转带更鲁棒。

变体（均不改变编码结构，API 兼容）：
  soft   —— 现状：s(c) = Σ z_b·sgn_b（幅度积分）
  vote   —— 带级等权投票：s(c) = Σ sign(z_b)·sgn_b
  clip1  —— 幅度裁剪 τ=1：s(c) = Σ clip(z_b,-1,1)·sgn_b
  clip2  —— 幅度裁剪 τ=2
  rank   —— 带级秩和：按 |z_b| 排序取符号贡献

对照：ZH D3r，n_bands=16，s30/s50/s70，多 seed 稳健性。

用法: python experiments/exp_detector_variants.py
"""
from __future__ import annotations

import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from aawm.synonym_data import ZH_SYNONYMS_RAW
from dict_build import build_cilin_dict
from exp_paws_attack import KEY, SALT, N_SENT, N_DOCS, load_paws_positive
from exp_real_corpus import synonym_attack
from exp_bandwidth_d3r import build_d3r_codec

N_TEST = N_DOCS // 2


def score_variants(z_by_band, candidates):
    """返回各判决变体的 best_uid。"""
    out = {}
    for name, weight in (("soft", "z"), ("vote", "sign"),
                         ("clip1", 1.0), ("clip2", 2.0), ("rank", "rank")):
        def _s(c):
            tot = 0.0
            for b, z in z_by_band.items():
                sgn = 1 if ((c >> b) & 1) else -1
                if weight == "z":
                    tot += z * sgn
                elif weight == "sign":
                    tot += (1 if z > 0 else -1) * sgn
                elif weight == "rank":
                    tot += 0.0  # 后面单独处理
                else:
                    tot += max(-weight, min(weight, z)) * sgn
            return tot
        if weight == "rank":
            # 秩加权：|z| 大者权重高（秩/总秩）
            order = sorted(z_by_band.items(), key=lambda kv: -abs(kv[1]))
            n = len(order)
            rank_w = {b: (n - i) for i, (b, _) in enumerate(order)}
            def _s2(c):
                return sum(rank_w[b] * (1 if ((c >> b) & 1) else -1) * (1 if z > 0 else -1)
                           for b, z in z_by_band.items())
            scored = sorted(((_s2(c), c) for c in set(candidates)),
                            key=lambda x: x[0], reverse=True)
            out[name] = scored[0][1]
            continue
        scored = sorted(((_s(c), c) for c in set(candidates)),
                        key=lambda x: x[0], reverse=True)
        out[name] = scored[0][1]
    return out


def main():
    codec, test_docs, null_docs = build_d3r_codec(16)
    true_uids = [(0x1000 + i * 0x0111) & 0xFFFF for i in range(N_TEST)]
    candidates = sorted(set(range(1, 33)) | set(true_uids))

    names = ("soft", "vote", "clip1", "clip2", "rank")
    print(f"{'攻击':5s} | " + " | ".join(f"{n:>6s}" for n in names))
    for frac in (0.30, 0.50, 0.70):
        ok = {n: 0 for n in names}
        for i, doc in enumerate(test_docs):
            uid = true_uids[i]
            marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
            rw, _ = synonym_attack(codec, marked, frac, 100 + i)
            rep = codec.detect(rw, min_n=1)
            z = {st.band: st.z for st in rep.bands if st.has_signal}
            res = score_variants(z, candidates)
            for n in names:
                if res[n] == uid:
                    ok[n] += 1
        print(f"s{int(frac*100):2d} | " + " | ".join(f"{ok[n]:6d}" for n in names))

    # 多 seed 稳健性：s50 下 soft vs vote vs clip1
    print("\ns50 多 seed（攻击 seed 基 = 100/500/900）")
    for base in (500, 900):
        ok = {n: 0 for n in names}
        for i, doc in enumerate(test_docs):
            uid = true_uids[i]
            marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
            rw, _ = synonym_attack(codec, marked, 0.50, base + i)
            rep = codec.detect(rw, min_n=1)
            z = {st.band: st.z for st in rep.bands if st.has_signal}
            res = score_variants(z, candidates)
            for n in names:
                if res[n] == uid:
                    ok[n] += 1
        print(f"seed{base:4d} | " + " | ".join(f"{ok[n]:6d}" for n in names))


if __name__ == "__main__":
    main()
