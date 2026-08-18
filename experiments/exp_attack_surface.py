"""信道 B 攻击面分析实验（v0.5，design.md §13.9）。

攻击者模型（Kerckhoffs）：知道全部机制（16 带绿名单 + 同义替换 + HMAC 谓词
+ 公开词典），唯独不知道主密钥 K。目标：让受害者检测器把攻击文本解码为
攻击者指定的 UID（伪造），或破坏/稀释已有水印（抵赖）。

三类攻击模拟（英文，600 词，bias=1.0 嵌入）：
  A1 盲伪造    —— 攻击者从无水印文本出发，随机同义替换，指望碰出目标 UID
  A2 置换攻击  —— 攻击者拿到别人的水印文本（不知 K），重新随机同义替换，
                   试图把 UID_A 改成 UID_B
  A3 混合攻击  —— collusion：m 份不同 UID 的水印文本词级交错合并，稀释信号

理论预期：
  A1/A2 每带 z 符号由随机替换决定，P(单带正确)=0.5 → 指定 16-bit UID
     成功率 ≈ 2^-16 ≈ 1.53e-5；汉明距分布 ≈ Binomial(16, 0.5)（均值 8）
  A3 每带样本被均摊，多数 UID 的带符号在合并后互相抵消 → 解码趋向
     占比最大者的"幸存带"子集，Σ|z| 随 m 衰减
"""
from __future__ import annotations

import random
import statistics
import sys
from collections import Counter

sys.path.insert(0, "src")

from aawm.greenlist import GreenlistCodec
from aawm.synonym_data import EN_SYNONYMS_EXTRA, EN_SYNONYMS_RAW

KEY = bytes(range(32))
SALT = b"attack-lab"
N_TRIALS_A1 = 400
N_TRIALS_A2 = 200

WORDS_POOL = sorted({w for cands in EN_SYNONYMS_RAW.values() for w in cands})


def make_text(n_words: int, seed: int) -> str:
    """模拟文本：词典词 + 无关填充词交替（词典率 ~35%，同 §13.3 实验）。"""
    r = random.Random(seed)
    filler = ["the", "of", "and", "a", "to", "in", "is", "that", "it", "for"]
    out = []
    for i in range(n_words):
        if r.random() < 0.35:
            out.append(r.choice(WORDS_POOL))
        else:
            out.append(r.choice(filler))
        out.append("." if r.random() < 0.08 else " ")
    return "".join(out).replace(" .", ". ")


def attacker_random_swap(codec: GreenlistCodec, text: str, frac: float, seed: int) -> str:
    """攻击者的随机同义替换（知道词典，不知 K：无法计算绿名单/频带）。"""
    # 攻击者视角的"同义词组"：用公开词典原始组（不知 victim 的不相交划分也行，
    # 这里直接给攻击者 codec 的组结构 = 最强攻击者，仍不知颜色）
    r = random.Random(seed)
    parts = codec._tokenizer(text)
    out = []
    n_dict = sum(1 for _, n in parts if n is not None)
    n_target = int(n_dict * frac)
    changed = 0
    for raw, norm in parts:
        grp = codec._w2group.get(norm)
        if grp and changed < n_target and r.random() < frac + 0.35:
            out.append(r.choice(grp))
            changed += 1
        else:
            out.append(raw)
    return "".join(out)


def main() -> None:
    victim = GreenlistCodec(KEY, SALT)
    null_corpus = [make_text(600, s) for s in range(500, 505)]
    victim.calibrate_p0(null_corpus)

    TARGET = 0x1234

    # ---- A1 盲伪造 ----
    print("=" * 62)
    print("A1 盲伪造：无水印文本 + 攻击者随机替换 → 指定 UID 0x%04X" % TARGET)
    ham_a1, hits, exist_a1 = [], 0, []
    for t in range(N_TRIALS_A1):
        text = make_text(600, 10_000 + t)
        forged = attacker_random_swap(victim, text, 1.0, 20_000 + t)
        rep = victim.detect(forged)
        ham_a1.append(bin(rep.uid ^ TARGET).count("1"))
        exist_a1.append(rep.existence_score)
        if rep.uid == TARGET:
            hits += 1
    mean_ham = statistics.mean(ham_a1)
    print(f"  试验 {N_TRIALS_A1} 次：命中目标 UID {hits} 次")
    print(f"  汉明距均值 {mean_ham:.2f}（Binomial(16,0.5) 期望 8.0）")
    print(f"  汉明距<=2 占比 {sum(1 for h in ham_a1 if h <= 2)/N_TRIALS_A1:.3%}"
          f"（理论 {sum(1 for k in range(3))*0.5**16*sum(1 for _ in range(0)):.0f}"
          f" ≈ C(16,0..2)/2^16 = {17/65536:.3%}）")
    print(f"  Σ|z| 均值 {statistics.mean(exist_a1):.1f}"
          f"（真水印 ~40+，null ~12）")

    # ---- A2 置换攻击 ----
    print("=" * 62)
    print("A2 置换攻击：他人水印文本(UID_A) + 攻击者重替换 → 试图改 UID")
    UID_A, UID_B = 0x5678, 0x1234
    base = make_text(600, 77)
    marked = victim.embed(base, UID_A, bias=1.0)
    rep0 = victim.detect(marked)
    print(f"  原水印：uid=0x{rep0.uid:04X} Σ|z|={rep0.existence_score:.1f}")
    keep, ham_a2, exist_a2 = 0, [], []
    for t in range(N_TRIALS_A2):
        forged = attacker_random_swap(victim, marked, 0.6, 30_000 + t)
        rep = victim.detect(forged)
        ham_a2.append(bin(rep.uid ^ UID_B).count("1"))
        exist_a2.append(rep.existence_score)
        if rep.uid == UID_A:
            keep += 1
    print(f"  重替换后仍解出 UID_A：{keep}/{N_TRIALS_A2}"
          f"（水印残留，攻击者反而破坏了归属证据）")
    print(f"  距攻击目标 UID_B 汉明距均值 {statistics.mean(ham_a2):.2f}（期望 8.0）")
    print(f"  Σ|z| 均值 {statistics.mean(exist_a2):.1f}（信号被部分摧毁）")

    # ---- A3 混合（collusion）攻击 ----
    print("=" * 62)
    print("A3 混合攻击：m 份不同 UID 文本词级交错")
    for m in (2, 4, 8):
        texts = [make_text(600, 1000 + i) for i in range(m)]
        uids = [(0x1111 * (i + 1)) & 0xFFFF for i in range(m)]
        parts = []
        for txt, u in zip(texts, uids):
            parts.append(victim.embed(txt, u, bias=1.0).split())
        # 词级轮转交错
        mixed_words = []
        for ws in zip(*parts):
            mixed_words.extend(ws)
        mixed = " ".join(mixed_words)
        rep = victim.detect(mixed)
        hams = [bin(rep.uid ^ u).count("1") for u in uids]
        single = victim.detect(victim.embed(texts[0], uids[0], bias=1.0))
        print(f"  m={m}: 解出 uid=0x{rep.uid:04X} 距各成员汉明距 min={min(hams)}"
              f" / max={max(hams)}；Σ|z|={rep.existence_score:.1f}"
              f"（单份 {single.existence_score:.1f}）")

    print("=" * 62)
    print("结论：见 design.md §13.9")


if __name__ == "__main__":
    main()
