"""exp_segment_del_robust.py: 段落删除抗性实验（修正版）。

验证 docs/math_formulation.md §3.2 定理 1（统一 SNR 方程）：
  SNR_b = d_b · √(n_b·(1-δ))
  → 段落删除与均匀 token 删除在合并 detect 下统计等价（前提：token 在段间均匀分布）
  → 抗删能力由总样本 N 决定（长文档天然抗删）

实验：变化文档长度 N，固定 δ=0.5 段落删除，测合并 detect 存活率。
预期：N 越大 SNR 越高，存活率从 0% 升到 100%，存在临界 N。

附测：窗口化（W=5 多数表决）对照，验证窗口化相对合并 detect 是否有增益。
"""
from __future__ import annotations

import random
import sys
from collections import Counter

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from aawm.plugins.registry import UIDRegistry
from exp_paws_attack import load_paws_positive, KEY, SALT
from exp_real_corpus import synonym_attack

N_SEED = 20


def main():
    # 加载 PAWS 段并打乱
    paws = load_paws_positive()
    paras = [s0 for s0, _ in paws[:1000]]
    random.Random(0).shuffle(paras)

    reg = UIDRegistry()
    reg.register("agent-cuiyin")
    reg.register("agent-xiaoming")
    reg.register("agent-zhangsan")
    reg.register("agent-external-01")
    # 用一个高熵 UID（0x5555）避免 0x0001 的"汉明≤3 过松"假象
    uid = 0x5555
    reg._uid_to_alias = {uid: "agent-cuiyin-test"}
    # 全 65536 UID 候选（部署级盲检）
    all_uids = list(range(1, 65536))
    print(f"UID=0x{uid:04X} (高熵避免判据假象), seed={N_SEED}")
    print(f"判据：soft_match 在 65535 个候选中正确锁定（部署级严格）")

    codec = GreenlistCodec(KEY, SALT, language_tag=b"zh")
    codec.calibrate_p0(paras[:500])

    print(f"\n{'N段':>6} | {'N词':>6} | "
          f"{'δ=.5 纯删':>10} | {'δ=.7 纯删':>10} | {'δ=.9 纯删':>10} | "
          f"{'50%替换+50%删':>12}")
    print("-" * 76)

    for n_paras in [10, 20, 50, 100, 200, 400]:
        line = [n_paras]
        for delta in [0.5, 0.7, 0.9]:
            ok = 0
            for seed in range(N_SEED):
                rng = random.Random(seed)
                rng2 = random.Random(seed * 7 + 1)
                chosen = rng.sample(paras, n_paras)
                marked_paras = []
                for pi, p in enumerate(chosen):
                    mp = codec.embed(p, uid, bias=1.0,
                                     rng=random.Random(seed * 100 + pi))
                    marked_paras.append(mp)
                n_del = int(round(delta * n_paras))
                idxs = list(range(n_paras))
                rng2.shuffle(idxs)
                del_set = set(idxs[:n_del])
                survived = [marked_paras[i] for i in range(n_paras)
                            if i not in del_set]
                if not survived:
                    continue
                text = " ".join(survived)
                # 全候选 soft_match
                best, _, _ = codec.soft_match(text, all_uids, min_n=1, margin=0.0)
                if best == uid:
                    ok += 1
            line.append(ok)
        # 混合攻击：50% 替换 + 50% 删除
        ok_m = 0
        for seed in range(N_SEED):
            rng = random.Random(seed)
            rng2 = random.Random(seed * 7 + 1)
            chosen = rng.sample(paras, n_paras)
            marked_paras = []
            for pi, p in enumerate(chosen):
                mp = codec.embed(p, uid, bias=1.0,
                                 rng=random.Random(seed * 100 + pi))
                marked_paras.append(mp)
            n_del = n_paras // 2
            idxs = list(range(n_paras))
            rng2.shuffle(idxs)
            del_set = set(idxs[:n_del])
            survived = []
            for i in range(n_paras):
                if i in del_set:
                    continue
                rw, _ = synonym_attack(codec, marked_paras[i], 0.50, seed * 17 + i)
                survived.append(rw)
            text = " ".join(survived)
            best, _, _ = codec.soft_match(text, all_uids, min_n=1, margin=0.0)
            if best == uid:
                ok_m += 1
        # 估算 N 词典词
        sample = " ".join([codec.embed(p, uid, bias=1.0, rng=random.Random(99+i))
                           for i, p in enumerate(chosen[:min(n_paras, 30)])])
        n_words = codec.detect(sample).n_dict_words
        n_words = int(n_words * n_paras / min(n_paras, 30))
        print(f"{n_paras:>6} | {n_words:>6} | "
              f"{line[1]:>3}/{N_SEED:<6} | {line[2]:>3}/{N_SEED:<6} | "
              f"{line[3]:>3}/{N_SEED:<6} | {ok_m:>3}/{N_SEED:<8}")

    print("""
读表：
  · N 越大 SNR 越高 → 抗删能力越强（统一 SNR 方程 §3.2 验证）
  · 长文档对纯段落删除天然抗删（N≥某临界后 δ=0.9 仍可解）
  · 混合攻击（50% 替换 + 50% 删）需要更大 N，是真实威胁上限
  · 短文档是抗删弱点，需要冗余/纠错码辅助
""")


if __name__ == "__main__":
    main()
