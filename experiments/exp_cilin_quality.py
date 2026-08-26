"""exp_cilin_quality.py: 词林变换减病句 —— 语料上下文 + 语素共享过滤。

根因：词林 '='（严格同义）组中很多词虽词典义相同但搭配域不同
（领域↔版图、内容↔要义），替换后产生病句。

两道互补信号：
  1. 字符集上下文兼容率：对每对词，从全部可用语料收集左邻/右邻字符集，
     计算双向重叠率。大语料（~30K 字 docs + PAWS 句子）下覆盖率显著提升。
  2. 语素共享 bonus：同字（共享汉字）的词对更可能安全互换
     （标志/标记→共享"标"+"记"，近邻/邻居→共享"邻"）。

综合分 = max(上下文兼容率, 0.3 if 语素共享 else 0)。
threshold 以下剔除。
"""
from __future__ import annotations

import glob
import random
import re
import sys
from collections import defaultdict

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

import pyarrow.parquet as pq

from aawm.greenlist import GreenlistCodec, build_zero_cost_zh_codec, make_zh_tokenizer
from aawm.synonym_data import (
    load_zero_cost_zh_dictionary, load_zero_cost_zh_block_words,
)
from dict_build import build_cilin_dict
from exp_real_corpus import filter_dict_by_corpus
from exp_zero_cost_robust import (
    KEY, SALT,
    make_docs_written, make_docs_paws,
    build_hybrid_codec,
    synonym_attack,
)

DOC_GLOB = "docs/*.md"
PAWS_GLOB = "corpus/paraphrase/train-00000-of-00001-*.parquet"


# ------------------------------------------------------------------ 大语料
def load_all_text() -> list[str]:
    """加载全部可用中文语料作为上下文来源。"""
    texts = []
    # docs/*.md 全文
    for p in sorted(glob.glob(DOC_GLOB)):
        try:
            t = open(p, encoding="utf-8").read()
        except Exception:
            continue
        t = re.sub(r"```.*?```", "", t, flags=re.S)
        zh = "".join(re.findall(
            r"[\u4e00-\u9fff，。；：、！？——（）「」“”‘’《》]", t))
        if zh:
            texts.append(zh)
    # PAWS 句子
    try:
        t = pq.read_table(glob.glob(PAWS_GLOB)[0])
        for s in t.column("sentence1").to_pylist()[:5000]:
            texts.append(s)
    except Exception:
        pass
    return texts


# ------------------------------------------------------------------ 上下文索引
def build_char_context(texts: list[str], words: set[str]) -> tuple[dict, dict]:
    """对每个词，分别收集左邻字符集和右邻字符集。"""
    left: dict[str, set] = defaultdict(set)
    right: dict[str, set] = defaultdict(set)
    for text in texts:
        for w in words:
            start = 0
            while True:
                idx = text.find(w, start)
                if idx == -1:
                    break
                if idx > 0:
                    left[w].add(text[idx - 1])
                end = idx + len(w)
                if end < len(text):
                    right[w].add(text[end])
                start = idx + 1
    return dict(left), dict(right)


def pair_compat(left: dict, right: dict, a: str, b: str) -> float:
    """双向字符集兼容率：最保守方向的最弱重叠。"""
    la, lb = left.get(a, set()), left.get(b, set())
    ra, rb = right.get(a, set()), right.get(b, set())
    scores = []
    for src, dst in [(la, lb), (lb, la), (ra, rb), (rb, ra)]:
        if not src:
            scores.append(0.0)
        else:
            scores.append(len(src & dst) / len(src))
    return min(scores)


def has_shared_morpheme(a: str, b: str) -> bool:
    """两词是否共享至少一个汉字（语素）。"""
    return bool(set(a) & set(b))


def group_score(group: list[str], left: dict, right: dict) -> float:
    """综合兼容分 = max(上下文兼容率, 0.3 if 语素共享 else 0)。

    取组内最差词对的分数。
    """
    if len(group) < 2:
        return 1.0
    worst = 1.0
    for i in range(len(group)):
        for j in range(i + 1, len(group)):
            ctx_ov = pair_compat(left, right, group[i], group[j])
            morph = 0.3 if has_shared_morpheme(group[i], group[j]) else 0.0
            score = max(ctx_ov, morph)
            if score < worst:
                worst = score
    return worst


def filter_groups(
    cilin_groups: dict[str, list[str]],
    left: dict, right: dict,
    threshold: float = 0.15,
) -> tuple[dict, dict, dict]:
    """返回 (存活组, 存活组分数, 剔除组分数)。"""
    survived, surv_sc, drop_sc = {}, {}, {}
    for key, words in cilin_groups.items():
        sc = group_score(words, left, right)
        if sc >= threshold:
            survived[key] = words
            surv_sc[key] = sc
        else:
            drop_sc[key] = sc
    return survived, surv_sc, drop_sc


# ------------------------------------------------------------------ 混合 codec（带过滤）
def build_hybrid_filtered(
    docs: list[str],
    all_texts: list[str],
    threshold: float = 0.15,
) -> GreenlistCodec:
    """混合 codec + 上下文兼容性过滤。"""
    zero_dict = load_zero_cost_zh_dictionary()
    block = load_zero_cost_zh_block_words()
    zero_words = {w for ws in zero_dict.values() for w in ws}

    # 词林组语料过滤
    base = GreenlistCodec(KEY, SALT, language_tag=b"zh")
    raw_cilin = build_cilin_dict("corpus/dict/cilin_extended.txt")
    cilin_groups = filter_dict_by_corpus(
        raw_cilin, docs, base._tokenizer, max_group=20, zh_mode=True)

    # 大语料上下文兼容性过滤
    cilin_words = {w for ws in cilin_groups.values() for w in ws}
    left, right = build_char_context(all_texts, cilin_words)
    cilin_filtered, surv_sc, drop_sc = filter_groups(
        cilin_groups, left, right, threshold=threshold)

    # 合并
    merged = dict(zero_dict)
    owned = set(zero_words)
    n_added = 0
    for key, words in cilin_filtered.items():
        if any(w in owned for w in words):
            continue
        merged[key] = words
        owned.update(words)
        n_added += 1

    all_words = owned | block
    tokenizer = make_zh_tokenizer(dict_words=all_words)
    codec = GreenlistCodec(
        KEY, SALT, n_bands=16,
        dictionary=merged, language_tag=b"zh", tokenizer=tokenizer,
    )
    codec.calibrate_p0(docs[len(docs) // 2:])
    n_dropped = len(cilin_groups) - len(cilin_filtered)
    print(f"  [hybrid-f{threshold}] 零感 {len(zero_dict)} + 词林 {n_added}"
          f" (过滤掉 {n_dropped} 组) = {len(merged)} 组, "
          f"编码组 {len(codec._groups)}")
    return codec


# ------------------------------------------------------------------ 测量
def _cands(k, uid, n_reg, seed):
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


def measure(codec, docs, label, n_reg=4):
    """测 k / rt gap / s30 错报。"""
    ks = []
    rt_gaps = []
    s30_ok = s30_err = 0
    for i, doc in enumerate(docs):
        k = codec.capacity(doc)
        ks.append(k)
        if k < 2:
            continue
        for s in range(5):
            uid = random.Random(1000 + i * 11 + s).randrange(1 << k)
            mm, used = codec.embed_adaptive(
                doc, uid, n_bits=k, rng=random.Random(4000 + i * 11 + s))
            cands = _cands(k, uid, n_reg, 8000 + i * 13 + s)
            best, sc, gap = codec.soft_match_adaptive(mm, cands, used)
            if best == uid:
                rt_gaps.append(gap)
            t, _ = synonym_attack(codec, mm, 0.30, 6000 + i * 10 + s)
            best2, sc2, gap2 = codec.soft_match_adaptive(t, cands, used)
            if best2 == uid:
                s30_ok += 1
            else:
                s30_err += 1
    ks.sort()
    rt_gaps.sort()
    rt_med = rt_gaps[len(rt_gaps) // 2] if rt_gaps else 0
    s30_total = s30_ok + s30_err
    print(f"  [{label}] k mean={sum(ks)/len(ks):.1f} med={ks[len(ks)//2]}  "
          f"rt gap med={rt_med:.2f}  "
          f"s30: {s30_ok}/{s30_total} ({100*s30_ok/max(s30_total,1):.0f}%)")


def main():
    print("=" * 70)
    print("词林变换减病句 —— 语料上下文 + 语素共享过滤")
    print("=" * 70)

    # 加载全部可用语料
    all_texts = load_all_text()
    print(f"上下文语料: {len(all_texts)} 段, "
          f"总字符 {sum(len(t) for t in all_texts)}")

    for corpus_name, make_docs in [("书面", make_docs_written), ("口语", make_docs_paws)]:
        docs = make_docs(10)
        if not docs:
            continue
        print(f"\n{'='*60}")
        print(f"语料: {corpus_name} ({len(docs)} 篇)")

        # 1. 分析词林组的兼容分数分布
        base = GreenlistCodec(KEY, SALT, language_tag=b"zh")
        raw_cilin = build_cilin_dict("corpus/dict/cilin_extended.txt")
        cilin_groups = filter_dict_by_corpus(
            raw_cilin, docs, base._tokenizer, max_group=20, zh_mode=True)
        cilin_words = {w for ws in cilin_groups.values() for w in ws}
        left, right = build_char_context(all_texts, cilin_words)

        scores = []
        for key, words in cilin_groups.items():
            sc = group_score(words, left, right)
            scores.append((sc, key, words))
        scores.sort()

        print(f"\n词林组数: {len(cilin_groups)}")
        print(f"综合分分布: min={scores[0][0]:.3f} "
              f"p25={scores[len(scores)//4][0]:.3f} "
              f"med={scores[len(scores)//2][0]:.3f} "
              f"p75={scores[3*len(scores)//4][0]:.3f} "
              f"max={scores[-1][0]:.3f}")

        # 抽检：最低分（最可能病句）的组
        print("\n--- 最低分组（最可能病句） ---")
        for sc, key, words in scores[:8]:
            shared = "共享" if any(
                has_shared_morpheme(words[i], words[j])
                for i in range(len(words))
                for j in range(i+1, len(words))) else "无共享"
            print(f"  {sc:.3f}  {words}  [{shared}]")

        # 抽检：最高分（最安全）的组
        print("\n--- 最高分组（最安全） ---")
        for sc, key, words in scores[-5:]:
            shared = "共享" if any(
                has_shared_morpheme(words[i], words[j])
                for i in range(len(words))
                for j in range(i+1, len(words))) else "无共享"
            print(f"  {sc:.3f}  {words}  [{shared}]")

        # 2. 不同阈值下过滤效果
        print(f"\n--- 过滤效果 ---")
        for thresh in [0.05, 0.10, 0.15, 0.20, 0.30, 0.40]:
            n_surv = sum(1 for sc, _, _ in scores if sc >= thresh)
            n_drop = len(scores) - n_surv
            print(f"  thresh={thresh:.2f}: 存活 {n_surv}/{len(cilin_groups)} "
                  f"(过滤 {n_drop}, {100*n_drop/len(scores):.0f}%)")

        # 3. 对比测试：无过滤 vs 过滤
        print(f"\n--- 性能对比 (n_reg=4) ---")
        print("  [raw]", end="")
        measure(build_hybrid_codec(docs), docs, f"{corpus_name}/raw")
        for thresh in [0.05, 0.10, 0.15, 0.20]:
            print(f"  [f{thresh}]", end="")
            measure(build_hybrid_filtered(docs, all_texts, threshold=thresh),
                   docs, f"{corpus_name}/f{thresh}")


if __name__ == "__main__":
    main()
