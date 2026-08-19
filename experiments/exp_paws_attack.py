#!/usr/bin/env python3
"""exp_paws_attack.py: PAWS-X zh 真实改写对接入攻击模拟（P2 销项）。

补全攻击谱的"温和改写"端。PAWS-X 正样本对（sentence1 → sentence2，
人工构造的高重叠改写，train 21829 对）用于标定词典词级转移矩阵：
  keep 83.6% / 组内同义换 2.38% / 删除·组外 14.1%（水印词典视角）
然后把转移概率作用在带水印文本上，模拟"攻击者对 marked 做 PAWS 级改写"。

方法论注意（曾踩坑）：不能直接把 sentence2 拼成文档去检测——
sentence2 是未嵌入的独立自然文本，检测它等于检测 null 文档，
测得汉明≈null 噪声是"信号从不存在"的必然，不构成攻击结果。
PAWS 的价值在标定"词典词命运"的转移参数，而非提供已嵌入的改写文本。

对照攻击谱（30 篇拼接文档，每篇 20 句 ≈ 900 字，词典词均值 51）：
  嵌入往返基线  汉明 0.00  ≤1=100%
  PAWS 温和改写  汉明 0.70  ≤1=90%   Σ|z| 24.6→18.0
  同组 30% 狠攻  汉明 1.20  ≤1=67%   Σ|z| →15.5
  同组 50% 狠攻  汉明 3.40  ≤1=3%    Σ|z| →11.2

结论：温和改写（PAWS 级）下水印存活良好，破坏力低于同组 30% 替换；
攻击谱两端（温和/狠攻）的破坏力单调，注册库 Hamming≤3 均能覆盖 PAWS 端。
"""
from __future__ import annotations

import glob
import json
import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

import pandas as pd

from aawm.greenlist import GreenlistCodec
from dict_build import build_cilin_dict
from exp_real_corpus import filter_dict_by_corpus, synonym_attack

KEY = bytes(range(32))
SALT = b"real-corpus-2026"
N_SENT = 20  # 每篇拼接句子对数
N_DOCS = 60  # 30 test + 30 null
PAWS_GLOB = "corpus/paraphrase/train-00000-of-00001-*.parquet"


def load_paws_positive() -> list[tuple[str, str]]:
    """加载 PAWS-X zh train 正样本对 (sentence1, sentence2)。"""
    df = pd.read_parquet(glob.glob(PAWS_GLOB)[0])
    pos = df[df["score"] == 1][["sentence1", "sentence2"]].reset_index(drop=True)
    return [(s1, s2) for s1, s2 in pos.itertuples(index=False)]


def build_codec(base: GreenlistCodec, docs: list[str], raw_dict: dict) -> GreenlistCodec:
    groups = filter_dict_by_corpus(raw_dict, docs, base._tokenizer,
                                   max_group=20, zh_mode=True)
    codec = GreenlistCodec(KEY, SALT, dictionary=groups, language_tag=b"zh")
    codec.calibrate_p0(docs[len(docs) // 2:])
    return codec


def transfer_matrix(codec: GreenlistCodec, pairs: list[tuple[str, str]],
                    limit: int = 6000) -> dict:
    """标定词典词级转移矩阵：keep / 组内同义换 / 删除·组外 / 新增。"""
    w2g = codec._w2group
    cnt = {"keep": 0, "grp_sub": 0, "del": 0, "new": 0, "n1": 0, "n2": 0, "pairs": 0}
    for s1, s2 in pairs[:limit]:
        t1 = [n for _, n in codec._tokenizer(s1) if n]
        t2 = [n for _, n in codec._tokenizer(s2) if n]
        d1 = [w for w in t1 if w in w2g]
        if not d1:
            continue
        cnt["pairs"] += 1
        cnt["n1"] += len(d1)
        cnt["n2"] += sum(1 for w in t2 if w in w2g)
        for w in d1:
            if w in s2:
                cnt["keep"] += 1
            else:
                grp = [x for x in w2g[w] if x != w]
                if any(x in s2 for x in grp):
                    cnt["grp_sub"] += 1
                else:
                    cnt["del"] += 1
    cnt["new"] = cnt["n2"] - cnt["n1"]
    tot = cnt["n1"]
    cnt["keep_p"] = round(cnt["keep"] / tot, 4)
    cnt["grp_sub_p"] = round(cnt["grp_sub"] / tot, 4)
    cnt["del_p"] = round(cnt["del"] / tot, 4)
    return cnt


def paws_style_attack(codec: GreenlistCodec, text: str, seed: int,
                      p_del: float, p_grp: float) -> str:
    """PAWS 参数化温和攻击：词典词按实测转移概率处理。"""
    r = random.Random(seed)
    out = []
    for raw, norm in codec._tokenizer(text):
        grp = codec._w2group.get(norm) if norm else None
        if grp:
            x = r.random()
            if x < p_del:  # 删除/组外改写 → 换颜色相反的同组词（信号全丢）
                alts = [c for c in grp if c != norm and codec.green(c) != codec.green(norm)]
                out.append(r.choice(alts) if alts else raw)
            elif x < p_del + p_grp:  # 组内同义改写 → 同组随机（颜色半翻转）
                alts = [c for c in grp if c != norm]
                out.append(r.choice(alts) if alts else raw)
            else:  # 保留（信号不动）
                out.append(raw)
        else:
            out.append(raw)
    return "".join(out)


def main() -> None:
    pairs = load_paws_positive()
    base = GreenlistCodec(KEY, SALT, language_tag=b"zh")
    raw = build_cilin_dict("corpus/dict/cilin_extended.txt")

    # 保留词典词 ≥2 的句子，随机打乱后拼接成文档
    def n_dict(s: str) -> int:
        return sum(1 for _, n in base._tokenizer(s) if n and n in base._w2group)

    kept = [p for p in pairs if n_dict(p[0]) >= 2]
    rng = random.Random(7)
    rng.shuffle(kept)

    docs = [" ".join(kept[i * N_SENT:(i + 1) * N_SENT][j][0]
                     for j in range(N_SENT)) + " "
            for i in range(N_DOCS)]
    test_docs, null_docs = docs[:N_DOCS // 2], docs[N_DOCS // 2:]

    codec = build_codec(base, docs, raw)
    print(f"词典: {len(codec._groups)} 组 / {codec.stats}")
    print(f"文档: {len(test_docs)} test + {len(null_docs)} null，"
          f"句长均值 {sum(len(d) for d in test_docs) / len(test_docs):.0f} 字")

    # PAWS 转移矩阵标定（水印词典视角）
    tm = transfer_matrix(codec, pairs)
    p_del, p_grp = tm["del_p"], tm["grp_sub_p"]
    print(f"\nPAWS 词典词级转移矩阵（{tm['pairs']} 对，{tm['n1']} 词）:")
    print(f"  keep={tm['keep_p']*100:.1f}%  组内同义换={tm['grp_sub_p']*100:.2f}%  "
          f"删除/组外={tm['del_p']*100:.1f}%  sentence2 词典词净增={tm['new']}")

    # 攻击谱
    rt, paws, s30, s50 = [], [], [], []
    sumz_rt, sumz_paws, sumz_s30, sumz_s50 = [], [], [], []
    dict_counts = []
    for i, doc in enumerate(test_docs):
        uid = (0x1000 + i * 0x0111) & 0xFFFF
        marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
        rep = codec.detect(marked)
        dict_counts.append(rep.n_dict_words)
        sumz_rt.append(rep.existence_score)
        rt.append(codec.masked_hamming(marked, uid))
        for frac, bucket, zl in (
            (None, paws, sumz_paws), (0.30, s30, sumz_s30), (0.50, s50, sumz_s50),
        ):
            if frac is None:
                rw = paws_style_attack(codec, marked, 200 + i, p_del, p_grp)
            else:
                rw, _ = synonym_attack(codec, marked, frac, 100 + i)
            d, a = codec.masked_hamming(rw, uid)
            bucket.append((d, a))
            zl.append(codec.detect(rw).existence_score)

    def report(name: str, bucket) -> None:
        mean = sum(x for x, _ in bucket) / len(bucket)
        le1 = sum(1 for x, _ in bucket if x <= 1) / len(bucket)
        le2 = sum(1 for x, _ in bucket if x <= 2) / len(bucket)
        print(f"[{name}] 汉明均值={mean:.2f}  ≤1={le1*100:.0f}%  ≤2={le2*100:.0f}%")

    print(f"\n词典词/文档: 均值={sum(dict_counts) / len(dict_counts):.0f}")
    report("嵌入往返(基线)", rt)
    report("PAWS温和改写(实测参数)", paws)
    report("同组30%狠攻", s30)
    report("同组50%狠攻", s50)
    print(f"[Σ|z|] 嵌入后={sum(sumz_rt) / len(sumz_rt):.1f}  "
          f"PAWS改写={sum(sumz_paws) / len(sumz_paws):.1f}  "
          f"同组30%={sum(sumz_s30) / len(sumz_s30):.1f}  "
          f"同组50%={sum(sumz_s50) / len(sumz_s50):.1f}")

    out = {
        "n_groups": len(codec._groups),
        "transfer": tm,
        "dict_words_per_doc": round(sum(dict_counts) / len(dict_counts), 1),
        "rt_ham_mean": round(sum(x for x, _ in rt) / len(rt), 2),
        "paws_ham_mean": round(sum(x for x, _ in paws) / len(paws), 2),
        "paws_le1": f"{sum(1 for x, _ in paws if x <= 1) / len(paws) * 100:.0f}%",
        "paws_le2": f"{sum(1 for x, _ in paws if x <= 2) / len(paws) * 100:.0f}%",
        "paws_sumz": round(sum(sumz_paws) / len(sumz_paws), 1),
        "s30_ham_mean": round(sum(x for x, _ in s30) / len(s30), 2),
        "s30_le1": f"{sum(1 for x, _ in s30 if x <= 1) / len(s30) * 100:.0f}%",
        "s30_sumz": round(sum(sumz_s30) / len(sumz_s30), 1),
        "s50_ham_mean": round(sum(x for x, _ in s50) / len(s50), 2),
        "sumz_marked": round(sum(sumz_rt) / len(sumz_rt), 1),
    }
    with open("experiments/paws_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n结果已写入 experiments/paws_result.json")


if __name__ == "__main__":
    main()
