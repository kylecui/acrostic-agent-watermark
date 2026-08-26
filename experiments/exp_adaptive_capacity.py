"""exp_adaptive_capacity.py: 容量自适应验证（UID 空间 = 文档命中带数）。

主张：有效 UID 位数 k = 文档命中带数，碰撞概率 = (候选数−1)/2^k。
上一轮 full-16bit 候选 5000 崩，根因是候选在 2^16 空间含 2^(16−k) 个
"信号带全同"的并列候选（得分与真值并列不可区分）。
容量自适应把 UID 声明为 k-bit 空间：候选库 ≤ 2^k 时无并列者，
真值在全部活动带与嵌入方向对齐 → 得分严格最高 → 无攻击下 100% 命中。

Part 1: 容量分布 —— 书面/口语 × 词林/零感的 capacity() 真实分布
Part 2: 碰撞边界 —— 同候选数 N，k-bit 空间 vs full 空间 soft 命中率
        （证明"说得准"：k-bit 声明后满容量候选也全对，full 必崩）
Part 3: 攻击衰减 —— 删除/改写后可读 bit k' 与 soft_match_adaptive 存活

运行：python experiments/exp_adaptive_capacity.py
"""
from __future__ import annotations

import random
import statistics
import sys
from collections import Counter

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import build_zero_cost_zh_codec
from exp_zero_cost_robust import (
    build_cilin_codec,
    make_docs_paws,
    make_docs_written,
    paragraph_delete,
    synonym_attack,
    transfer_matrix,
)


def build_zero_codec(docs: list[str]):
    codec = build_zero_cost_zh_codec(bytes(range(32)), b"real-corpus-2026", n_bands=16)
    codec.calibrate_p0(docs[len(docs) // 2:])
    return codec


def part1(docs_w: list[str], docs_p: list[str]) -> dict:
    """容量分布：UID 有效位数 = 命中带数，跨语料跨词典的诚实声明。"""
    codecs_w = {"词林": build_cilin_codec(docs_w), "零感": build_zero_codec(docs_w)}
    codecs_p = {"词林": build_cilin_codec(docs_p), "零感": build_zero_codec(docs_p)}
    print("========== Part 1: 容量分布（UID 有效位数 = 命中带数） ==========")
    for tag, docs, codecs in (("书面语", docs_w, codecs_w), ("口语", docs_p, codecs_p)):
        for name, c in codecs.items():
            caps = [c.capacity(d) for d in docs]
            dist = dict(sorted(Counter(caps).items()))
            print(f"[{tag} {name}] capacity 均值={statistics.mean(caps):.1f} "
                  f"中位={statistics.median(caps):.0f} "
                  f"范围=[{min(caps)},{max(caps)}] 分布={dist}")
            print(f"        候选上限 2^cap={1 << int(statistics.median(caps)):,}")
    return {"w": codecs_w, "p": codecs_p}


def part2(codecs: dict, docs: list[str], tag: str, n_docs: int = 8) -> None:
    """碰撞边界：k-bit 空间 vs full 空间（核心"说得准"证明）。

    k-bit 空间（容量自适应）：候选 = 全部 2^k 个 k-bit UID，无并列者，
        真值在全部活动带与嵌入方向对齐 → 得分严格最高 → 30/30。
    full 空间（对照组）：与真值"信号带全同"的并列者 2^(16−k) 个同分，
        返回谁取决于候选列表输入顺序（soft_match 稳定排序）→ 不可控。
        演示：(a) 并列者排前 → 返回并列者；(b) 大样本随机序 → 失配率。
    """
    print(f"\n========== Part 2: 碰撞边界（{tag}，{n_docs} 篇 × 30 seed） ==========")
    for name, c in codecs.items():
        test = docs[:n_docs]
        rows = []
        for i, doc in enumerate(test):
            k = c.capacity(doc)
            uid = random.Random(i).randrange(1 << k)
            cands_k = list(range(1 << k))
            ok_k = 0
            mm0, used0 = c.embed_adaptive(doc, uid, n_bits=k,
                                          rng=random.Random(2000 + i * 31))
            full_true = c.map_uid(uid, used0)
            # (a) 确定性演示：并列者排在真值前 → 稳定返回并列者
            vacant = [b for b in range(c.n_bands) if b not in set(used0)]
            tie_demo = full_true ^ (1 << vacant[0]) if vacant else full_true
            best_demo, _, _ = c.soft_match(mm0, [tie_demo, full_true])
            demo_tie_wins = best_demo != full_true
            # (b) 大样本随机序候选集：统计 full 空间失配率
            n_fail_f = 0
            for s in range(30):
                mm, used = c.embed_adaptive(doc, uid, n_bits=k,
                                            rng=random.Random(2000 + i * 31 + s))
                best_k, _, _ = c.soft_match_adaptive(mm, cands_k, used)
                if best_k == uid:
                    ok_k += 1
                r = random.Random(3000 + i * 13 + s * 97)
                cands_f = list({full_true} | {
                    r.randrange(1 << c.n_bands)
                    for _ in range(2047)})
                best_f, _, _ = c.soft_match(mm, cands_f)
                if best_f != full_true:
                    n_fail_f += 1
            n_tie = (1 << (c.n_bands - k)) - 1  # 并列者数量（理论）
            rows.append((k, ok_k, n_fail_f, n_tie, demo_tie_wins))
        for k, ok_k, n_fail_f, n_tie, demo in rows:
            print(f"  {name} k={k:2d}: k-bit满容量候选 soft {ok_k:2d}/30"
                  f"   |   full 随机序候选失配 {n_fail_f}/30"
                  f"（并列者 {n_tie} 个，排前即失配{' ✓' if demo else ''}）")
        agg_k = sum(r[1] for r in rows) / (len(rows) * 30)
        agg_f = sum(r[2] for r in rows) / (len(rows) * 30)
        print(f"  → {name} 汇总：k-bit 空间 {agg_k*100:.0f}% vs full 空间 {100-agg_f*100:.0f}%")


def part4(codecs: dict, docs: list[str], tag: str, n_docs: int = 8) -> None:
    """冗余 + abstain：满容量(n_bits=k) vs 留冗余(n_bits=k−2) vs margin。

    满容量在替换污染下必崩（z 翻转 1 带 → 2^(k−1) 个候选得分反超）；
    留冗余让污染落在冗余带时编码带仍完好；margin abstain 把低置信
    转为"不报"而非错报（precision 优先，代价是召回）。
    """
    print(f"\n========== Part 4: 冗余与 abstain（{tag}，{n_docs} 篇 × 10 seed） ==========")
    from exp_zero_cost_robust import synonym_attack
    for name, c in codecs.items():
        test = docs[:n_docs]
        agg = {m: [0, 0] for m in ("full", "red2", "red4")}
        for i, doc in enumerate(test):
            k = c.capacity(doc)
            for s in range(10):
                # 满容量（n_bits=k，uid 取 k bit 范围）
                uid_k = random.Random(i * 7 + s).randrange(1 << k)
                m_full, used = c.embed_adaptive(doc, uid_k, n_bits=k,
                                                rng=random.Random(8000 + i * 31 + s))
                rw, _ = synonym_attack(c, m_full, 0.30, 9000 + i * 10 + s)
                ok = c.soft_match_adaptive(rw, list(range(1 << k)), used)[0] == uid_k
                agg["full"][0] += ok
                agg["full"][1] += 1
                # 留冗余 k-2 / k-4（uid 按 n_bits 范围取）
                for mname, nb in (("red2", k - 2), ("red4", k - 4)):
                    if nb <= 0:
                        continue
                    uid_nb = random.Random(i * 7 + s).randrange(1 << nb)
                    m_r, used_r = c.embed_adaptive(doc, uid_nb, n_bits=nb,
                                                   rng=random.Random(8000 + i * 31 + s))
                    rw_r, _ = synonym_attack(c, m_r, 0.30, 9000 + i * 10 + s)
                    ok = c.soft_match_adaptive(rw_r, list(range(1 << nb)), used_r)[0] == uid_nb
                    agg[mname][0] += ok
                    agg[mname][1] += 1
        line = "  " + "   ".join(
            f"{m}: {ok}/{n}" for m, (ok, n) in agg.items() if n)
        print(f"{name}  s30 存活（满容量 vs 留冗余 k-2/k-4）：{line}")
        print(f"         → 留冗余把污染带挡在编码带外；若仍崩，说明污染已及编码带")


def part3(codecs: dict, docs: list[str], pku, tag: str, n_docs: int = 10) -> None:
    """攻击衰减：删除/同义替换后可读 bit 数与 soft_match_adaptive 存活率。"""
    print(f"\n========== Part 3: 攻击衰减（{tag}，{n_docs} 篇 × 5 seed） ==========")
    for name, c in codecs.items():
        test = docs[:n_docs]
        tm_pku = transfer_matrix(c, pku, limit=20000)
        print(f"  PKU 转移: keep={tm_pku['keep_p']*100:.0f}% "
              f"grp={tm_pku['grp_sub_p']*100:.1f}% del={tm_pku['del_p']*100:.0f}%")
        rows = []
        for i, doc in enumerate(test):
            k = c.capacity(doc)
            uid = random.Random(i).randrange(1 << k)
            cands = list(range(1 << k))  # 满容量候选（最大库）
            k_read_rt, k_read_del, k_read_s50 = [], [], []
            ok_rt = ok_del3 = ok_del5 = ok_s30 = ok_s50 = 0
            for s in range(5):
                mm, used = c.embed_adaptive(doc, uid, n_bits=k,
                                            rng=random.Random(4000 + i * 11 + s))
                _, act, _ = c.detect_adaptive(mm, used)
                k_read_rt.append(len(act))
                ok_rt += (c.soft_match_adaptive(mm, cands, used)[0] == uid)
                for delta, acc in ((0.3, "del3"), (0.5, "del5")):
                    att = paragraph_delete(mm, delta, 5000 + i * 10 + s)
                    _, act, _ = c.detect_adaptive(att, used)
                    k_read_del.append(len(act))
                    hit = c.soft_match_adaptive(att, cands, used)[0] == uid
                    if acc == "del3":
                        ok_del3 += hit
                    else:
                        ok_del5 += hit
                rw, _ = synonym_attack(c, mm, 0.30, 6000 + i * 10 + s)
                _, act, _ = c.detect_adaptive(rw, used)
                k_read_s50.append(len(act))
                ok_s30 += (c.soft_match_adaptive(rw, cands, used)[0] == uid)
                rw, _ = synonym_attack(c, mm, 0.50, 7000 + i * 10 + s)
                ok_s50 += (c.soft_match_adaptive(rw, cands, used)[0] == uid)
            rows.append((k,
                         statistics.mean(k_read_rt), statistics.mean(k_read_del),
                         statistics.mean(k_read_s50),
                         ok_rt, ok_del3, ok_del5, ok_s30, ok_s50))
        T = n_docs * 5
        for k, kr, kd, ks, rt, d3, d5, s30, s50 in rows:
            print(f"  {name} k={k:2d}: rt可读{kr:.1f} del.3可读{kd:.1f} s50可读{ks:.1f} | "
                  f"rt {rt}/5 del.3 {d3}/5 del.5 {d5}/5 s30 {s30}/5 s50 {s50}/5")
        agg = lambda idx: sum(r[idx] for r in rows)
        print(f"  → {name} 存活汇总: rt {agg(4)}/{T} del.3 {agg(5)}/{T} "
              f"del.5 {agg(6)}/{T} s30 {agg(7)}/{T} s50 {agg(8)}/{T}")
        print(f"    （候选 = 满容量 2^k；可读 bit 反映攻击后的真实区分能力）")


def main() -> None:
    docs_w = make_docs_written()
    docs_p = make_docs_paws()
    pku = []
    with open("corpus/paraphrase/pku_paraphrase.tsv", encoding="utf-8") as f:
        for line in f:
            s1, s2 = line.rstrip("\n").split("\t")
            pku.append((s1, s2))
            if len(pku) >= 20000:
                break
    print(f"书面语 {len(docs_w)} 篇 / 口语 {len(docs_p)} 篇 / PKU {len(pku)} 对")

    codecs = part1(docs_w, docs_p)
    for tag, docs, n_docs in (("书面语", docs_w, 8), ("口语", docs_p, 8)):
        cset = codecs["w" if tag == "书面语" else "p"]
        part2(cset, docs, tag, n_docs)
        part3(cset, docs, pku, tag, n_docs)
        part4(cset, docs, tag, n_docs)
        print()

    print("""
读表：
  · Part 1 是"说得准"的声明：800 字文档 UID 容量就是 capacity()，不是 16。
  · Part 2 证明容量声明的价值：候选压到 2^k 空间后，满容量候选库也 100%
      命中（无并列）；而同样候选数在 full 空间必崩（并列者 2^(16−k) 个）。
  · Part 3 给出攻击后的真实容量衰减：可读 bit k' 与候选 2^k 下的存活率。
      碰撞概率 = (候选数−1)/2^k'，k' 越小越危险——这是攻击的诚实度量。
""")


if __name__ == "__main__":
    main()
