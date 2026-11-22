#!/usr/bin/env python3
"""paper_eb4_reference_impl.py: E-B4——F1 在独立参考实现上的复现。

目的：证明"确定性词谓词 + 出现次数计票 ⇒ z 随重复线性膨胀"是**方案
类别**的性质，不是 AAWM 的实现 bug。本模块实现一个最小独立的词汇级
z 检验水印（~80 行，与 AAWM 无任何共享代码路径），跑同一攻击构造。

诚实验证：此实现仅作研究复现参考，不代表任何已发表方案的具体实现；
论文中明确标注为 reference implementation（PLAN §4.3 E-B4）。

可复现: python experiments/paper_eb4_reference_impl.py [--seed 20260911]
产出: papers/paperB/data/eb4_reference_impl.json
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import paper_common as pc

WORD_RE = re.compile(r"[a-z]+")


class ReferenceLexicalWM:
    """最小独立词汇级 z 检验水印（研究参考实现）。

    与 AAWM 的设计刻意保持同类别不同实例：
      - 词色 = HMAC(key, "c:"+word) 首位的确定性谓词（同类别机制）
      - 计票 = 出现次数全计（无封顶——复现 F1 的 v0.14 类行为）
      - 得分 = 全文 (g - 0.5n) / sqrt(0.25n)（单桶 z，最简形式）
    """

    def __init__(self, key: bytes):
        self.key = key

    def color(self, word: str) -> int:
        mac = hmac.new(self.key, b"c:" + word.encode(), hashlib.sha256)
        return (mac.digest()[0] >> 7) & 1

    def score(self, text: str) -> tuple:
        n = g = 0
        for w in WORD_RE.findall(text.lower()):
            n += 1
            g += self.color(w)
        if n == 0:
            return 0.0, 0
        return (g - 0.5 * n) / math.sqrt(0.25 * n), n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=pc.DEFAULT_SEED)
    args = ap.parse_args()

    wm = ReferenceLexicalWM(bytes(range(32)))
    # 攻击构造：同一词重复 N 次（该词的色是确定性的）
    word = "system"  # 常见英文词，其确定性颜色由密钥决定
    c = wm.color(word)
    rows = []
    for n_rep in [1, 10, 40, 160, 640]:
        text = " ".join([word] * n_rep)
        z, n = wm.score(text)
        rows.append({"n_repetitions": n_rep, "abs_z": round(abs(z), 3),
                     "n_words": n, "word_color": c,
                     "crosses_z3": abs(z) >= 3.0})
        print(f"N={n_rep:4d} |z|={abs(z):7.2f} (>3={'Y' if abs(z)>=3 else 'N'})")

    results = {"word": word, "word_color": c, "rows": rows,
               "conclusion": "确定性谓词+计票 ⇒ 重复词使 |z| ∝ √N 无界"
               "（本实现与 AAWM 无共享代码）"}
    path = pc.save_result("eb4_reference_impl", args.seed,
                          {"impl": "ReferenceLexicalWM (independent, ~80 lines)",
                           "predicate": "HMAC(key,'c:'+w) top bit",
                           "statistic": "single-bin z over occurrence counts"},
                          results)
    print(f"\n数据: {path}")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(5.2, 3.2))
    ax.plot([r["n_repetitions"] for r in rows],
            [r["abs_z"] for r in rows], "o-")
    ax.axhline(3.0, color="gray", ls=":", lw=0.8, label="$|z|=3$")
    ax.set_xscale("log")
    ax.set_xlabel("repetitions of one word (unwatermarked text)")
    ax.set_ylabel("$|z|$ (single-bin)")
    ax.set_title("Independent reference implementation (E-B4)", fontsize=9)
    ax.legend(fontsize=8)
    fig.tight_layout()
    out = os.path.join("papers", "paperB", "figures",
                       "fig_eb4_reference_impl.pdf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out)
    print(f"FIG: {out}")



if __name__ == "__main__":
    main()
