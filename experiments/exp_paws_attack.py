#!/usr/bin/env python3
"""exp_paws_attack.py: 真实改写对（PAWS-X zh / PKU-Paraphrase-Bank）攻击谱。

补全攻击谱的两端。两个中文真实改写对数据集标定词典词级转移矩阵，
把转移概率作用在带水印文本上，模拟"攻击者对 marked 做 X 级改写"：

  - PAWS-X zh（train 21829 正样本）：人工构造的高词汇重叠改写（温和端）
      keep 83.7% / 组内同义换 2.14% / 删除·组外 14.1%
  - PKU-Paraphrase-Bank（509832 对）：文学翻译的自由改写（重度端）
      keep 46.6% / 组内同义换 2.84% / 删除·组外 50.5%

方法论注意（曾踩坑）：不能直接把 sentence2 拼成文档去检测——
sentence2 是未嵌入的独立自然文本，检测它等于检测 null 文档，
测得汉明≈null 噪声是"信号从不存在"的必然，不构成攻击结果。
真实改写对的价值在标定"词典词命运"的转移参数，而非提供已嵌入的改写文本。

对照攻击谱（30 篇拼接文档，每篇 20 句 ≈ 900 字，词典词均值 51）：
  嵌入往返基线  汉明 0.00  ≤1=100%
  PAWS 温和改写  汉明 0.70  ≤1=90%   Σ|z| 24.6→18.0
  PKU  重度改写  汉明 ~5    （≈ null 噪声，信号丢失）
  同组 30% 狠攻  汉明 1.20  ≤1=67%   Σ|z| →15.5
  同组 50% 狠攻  汉明 3.40  ≤1=3%    Σ|z| →11.2

结论：温和改写（PAWS 级）水印存活良好；重度改写（PKU 级，过半词典词
被删）的存活率**取决于"删除"的实现物理**——见 `paraphrase_style_attack`
的 del_mode 参数：
  - flip（反色同组替换，历史口径）：需知密钥颜色，属上帝视角上限攻击；
    带内绿率 1.0→0.5，z 均值归零，UID≈随机（0/30）。
  - 真实物理（del 词消失或落回词典、颜色随机）：z 符号保持，仅样本
    减半+少量污染；生产词典（D3r）下 soft 匹配 30/30（exp_dict_expansion
    实测）。"PKU 删除攻击是物理边界"的旧结论源于 flip 口径，已修正
    （见 exp_pku_real_physics.py / design §13.13）。
真实威胁仍是同义替换攻击（s30/s50，synonym_attack 口径）。
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
PKU_TSV = "corpus/paraphrase/pku_paraphrase.tsv"


def load_paws_positive() -> list[tuple[str, str]]:
    """加载 PAWS-X zh train 正样本对 (sentence1, sentence2)。"""
    df = pd.read_parquet(glob.glob(PAWS_GLOB)[0])
    pos = df[df["score"] == 1][["sentence1", "sentence2"]].reset_index(drop=True)
    return [(s1, s2) for s1, s2 in pos.itertuples(index=False)]


def load_pku_pairs(limit: int = 6000) -> list[tuple[str, str]]:
    """加载 PKU-Paraphrase-Bank 改写对 (sentence1, sentence2)。"""
    out: list[tuple[str, str]] = []
    with open(PKU_TSV, encoding="utf-8") as f:
        for line in f:
            s1, s2 = line.rstrip("\n").split("\t")
            out.append((s1, s2))
            if len(out) >= limit:
                break
    return out


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


def paraphrase_style_attack(codec: GreenlistCodec, text: str, seed: int,
                            p_del: float, p_grp: float, *,
                            del_mode: str = "flip", alpha: float = 0.5) -> str:
    """参数化温和攻击：词典词按实测转移概率处理（真实改写对标定）。

    del_mode 控制"删除/组外"(p_del) 的物理实现——这是决定攻击破坏力的
    关键假设（exp_pku_real_physics 实测差异巨大）：
      flip      —— del → 反色同组替换（历史口径）。需知密钥颜色才能选
                     反色词，属"上帝视角"上限攻击；带内 n 不变、绿率
                     1.0→0.5，z 均值归零 → UID 随机（最悲观，0/30）。
      remove    —— del → token 消失（真实"删除"物理）。带内 n 减半、
                     绿率保持 ~1.0，z 符号保持 → UID 大多可解（30/30）。
      rand_dict —— del → 换成词典内随机组词（颜色随机污染，α=1.0 上限）。
      mix       —— del 词以概率 alpha 变词典随机词、否则消失。真实 PKU
                     改写对实测 alpha≈0.36~0.72（del 词落回词典的比例），
                     代表值 0.5。
    默认 flip 保持历史口径兼容；真实物理用 mix/remove。
    """
    r = random.Random(seed)
    out = []
    for raw, norm in codec._tokenizer(text):
        grp = codec._w2group.get(norm) if norm else None
        if grp:
            x = r.random()
            if x < p_del:  # 删除/组外改写
                if del_mode == "flip":
                    alts = [c for c in grp if c != norm and codec.green(c) != codec.green(norm)]
                    out.append(r.choice(alts) if alts else raw)
                elif del_mode == "remove":
                    continue  # token 消失，不计入统计
                elif del_mode == "rand_dict":
                    head = r.choice(list(codec._groups))
                    out.append(r.choice(codec._groups[head]))
                else:  # mix
                    if r.random() < alpha:
                        head = r.choice(list(codec._groups))
                        out.append(r.choice(codec._groups[head]))
                    else:
                        continue
            elif x < p_del + p_grp:  # 组内同义改写 → 同组随机（颜色半翻转）
                alts = [c for c in grp if c != norm]
                out.append(r.choice(alts) if alts else raw)
            else:  # 保留（信号不动）
                out.append(raw)
        else:
            out.append(raw)
    return "".join(out)


def main() -> None:
    paws = load_paws_positive()
    pku = load_pku_pairs(20000)
    base = GreenlistCodec(KEY, SALT, language_tag=b"zh")
    raw = build_cilin_dict("corpus/dict/cilin_extended.txt")

    # 保留词典词 ≥2 的句子，随机打乱后拼接成文档（用 PAWS 句子做文档基准）
    def n_dict(s: str) -> int:
        return sum(1 for _, n in base._tokenizer(s) if n and n in base._w2group)

    kept = [p for p in paws if n_dict(p[0]) >= 2]
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

    # 两个数据集的词典词级转移矩阵（水印词典视角）
    tm_paws = transfer_matrix(codec, paws)
    tm_pku = transfer_matrix(codec, pku, limit=20000)
    print(f"\n[PAWS 转移矩阵] {tm_paws['pairs']} 对 / {tm_paws['n1']} 词典词:")
    print(f"  keep={tm_paws['keep_p']*100:.1f}%  组内同义换={tm_paws['grp_sub_p']*100:.2f}%  "
          f"删除/组外={tm_paws['del_p']*100:.1f}%  sentence2 词典词净增={tm_paws['new']}")
    print(f"[PKU  转移矩阵] {tm_pku['pairs']} 对 / {tm_pku['n1']} 词典词:")
    print(f"  keep={tm_pku['keep_p']*100:.1f}%  组内同义换={tm_pku['grp_sub_p']*100:.2f}%  "
          f"删除/组外={tm_pku['del_p']*100:.1f}%  sentence2 词典词净增={tm_pku['new']}")

    # 攻击谱：rt / paws / pku / s30 / s50
    rt, paws_b, pku_b, s30, s50 = [], [], [], [], []
    sumz_rt, sumz_paws, sumz_pku, sumz_s30, sumz_s50 = [], [], [], [], []
    dict_counts = []
    for i, doc in enumerate(test_docs):
        uid = (0x1000 + i * 0x0111) & 0xFFFF
        marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
        rep = codec.detect(marked)
        dict_counts.append(rep.n_dict_words)
        sumz_rt.append(rep.existence_score)
        rt.append(codec.masked_hamming(marked, uid))
        for tag, rw, bucket, zl in (
            ("paws", paraphrase_style_attack(codec, marked, 200 + i,
                                             tm_paws["del_p"], tm_paws["grp_sub_p"]),
             paws_b, sumz_paws),
            ("pku", paraphrase_style_attack(codec, marked, 300 + i,
                                            tm_pku["del_p"], tm_pku["grp_sub_p"],
                                            del_mode="mix", alpha=0.5),
             pku_b, sumz_pku),
        ):
            d, a = codec.masked_hamming(rw, uid)
            bucket.append((d, a))
            zl.append(codec.detect(rw).existence_score)
        for frac, bucket, zl in ((0.30, s30, sumz_s30), (0.50, s50, sumz_s50)):
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
    report("PAWS温和改写(实测参数)", paws_b)
    report("PKU重度改写(实测参数, del=mix真实物理)", pku_b)
    report("同组30%狠攻", s30)
    report("同组50%狠攻", s50)
    print(f"[Σ|z|] 嵌入后={sum(sumz_rt) / len(sumz_rt):.1f}  "
          f"PAWS={sum(sumz_paws) / len(sumz_paws):.1f}  "
          f"PKU={sum(sumz_pku) / len(sumz_pku):.1f}  "
          f"同组30%={sum(sumz_s30) / len(sumz_s30):.1f}  "
          f"同组50%={sum(sumz_s50) / len(sumz_s50):.1f}")

    out = {
        "n_groups": len(codec._groups),
        "dict_words_per_doc": round(sum(dict_counts) / len(dict_counts), 1),
        "transfer_paws": tm_paws,
        "transfer_pku": tm_pku,
        "rt_ham_mean": round(sum(x for x, _ in rt) / len(rt), 2),
        "paws_ham_mean": round(sum(x for x, _ in paws_b) / len(paws_b), 2),
        "paws_le1": f"{sum(1 for x, _ in paws_b if x <= 1) / len(paws_b) * 100:.0f}%",
        "paws_le2": f"{sum(1 for x, _ in paws_b if x <= 2) / len(paws_b) * 100:.0f}%",
        "paws_sumz": round(sum(sumz_paws) / len(sumz_paws), 1),
        "pku_ham_mean": round(sum(x for x, _ in pku_b) / len(pku_b), 2),
        "pku_le1": f"{sum(1 for x, _ in pku_b if x <= 1) / len(pku_b) * 100:.0f}%",
        "pku_sumz": round(sum(sumz_pku) / len(sumz_pku), 1),
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
