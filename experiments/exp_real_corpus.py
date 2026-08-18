#!/usr/bin/env python3
"""exp_real_corpus.py: 真实语料全链路实验（P0 销项：Zipf p0 标定 + 真实文本往返）。

英文: Gutenberg 3 本名著多窗口 × WordNet(synset, 语料词频过滤)
中文A: 人民日报 1998（词林自带文本还原）× 词林 —— 同域（词典与文本同时代）
中文B: 红楼梦 × 词林 —— 跨域对照（古白话 vs 现代词典，预期覆盖崩塌）

修复要点（相对 v1）:
  - 每书切多个互不重叠窗口文档，null/test 互斥
  - 真实语料 calibrate_p0（v1 漏标定导致 z 系统偏差）
  - 零覆盖带用 masked_hamming 排除（真实语料词典词稀疏的必然产物）
"""
from __future__ import annotations

import glob
import json
import random
import re
import sys
from collections import Counter

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from dict_build import build_cilin_dict, build_wordnet_dict

KEY = bytes(range(32))
SALT = b"real-corpus-2026"


# ---------------------------------------------------------------- 语料加载
def load_gutenberg() -> list[str]:
    docs = []
    for f in sorted(glob.glob("corpus/en/*.txt")):
        raw = open(f, encoding="utf-8", errors="ignore").read()
        m = re.search(r"\*\*\* ?START OF.*?\*\*\*(.*?)\*\*\* ?END OF", raw, re.S)
        body = m.group(1) if m else raw
        docs.append(re.sub(r"\s+", " ", body).strip())
    return docs


def load_people_daily() -> list[str]:
    """从词林文件还原人民日报 1998 纯文本（去掉 /词性/编码 标注）。"""
    texts = []
    buf = []
    for line in open("corpus/dict/cilin_utf8.txt", encoding="utf-8"):
        words = []
        for item in line.split():
            parts = item.split("/")
            if parts and parts[0]:
                words.append(parts[0])
        buf.append("".join(words))
        if len(buf) >= 40:  # 40 行 ≈ 一篇新闻
            texts.append("".join(buf))
            buf = []
    if buf:
        texts.append("".join(buf))
    return texts


def load_hlm() -> list[str]:
    docs = []
    for f in sorted(glob.glob("corpus/zh/*.txt")):
        docs.append(re.sub(r"\s+", "", open(f, encoding="utf-8", errors="ignore").read()))
    return docs


# ---------------------------------------------------------------- 词典过滤
def zh_bigram_freq(texts: list[str]) -> Counter:
    """中文双字滑窗词频（与词典无关，避免"词典驱动分词"的鸡生蛋问题）。"""
    freq = Counter()
    for t in texts:
        n = len(t)
        for i in range(n - 1):
            w = t[i:i + 2]
            if "\u4e00" <= w[0] <= "\u9fff" and "\u4e00" <= w[1] <= "\u9fff":
                freq[w] += 1
    return freq


def filter_dict_by_corpus(raw_dict: dict, corpus_texts: list[str], tokenizer,
                          min_group: int = 2, max_group: int = 8,
                          max_vocab: int | None = 15000,
                          zh_mode: bool = False):
    """按语料词频过滤词典：组内 ≥min_group 个词形出现在语料高频词表中。

    zh_mode=True 时用双字滑窗统计（中文 tokenizer 是词典驱动分词，
    若用它统计新词典词频会漏掉所有新词）。
    """
    if zh_mode:
        freq = zh_bigram_freq(corpus_texts)
    else:
        freq = Counter()
        for t in corpus_texts:
            for _, norm in tokenizer(t):
                if norm:
                    freq[norm] += 1
    vocab = {w for w, _ in freq.most_common(max_vocab)} if max_vocab else set(freq)
    groups = {}
    for key, words in raw_dict.items():
        words = [w for w in words if " " not in w]  # tokenizer 切不出多词短语
        if not (min_group <= len(words) <= max_group):
            continue
        hit = [w for w in words if w in vocab]
        if len(hit) >= min_group:
            groups[key] = hit
    return groups


# ---------------------------------------------------------------- 窗口切分
def windows_en(text: str, n_win: int = 12, words_per: int = 600, skip: int = 2000):
    """每本书跳过开头(序言)后切 n_win 个 600 词窗口。"""
    words = text.split()[skip:]
    return ["\n\n".join(" ".join(words[i * words_per + j * 100:(i + 1) * words_per if False else i * words_per + (j + 1) * 100])
                        for j in range(6)) for i in range(n_win)]


def windows_zh(text: str, n_win: int = 6, chars_per: int = 1200, skip: int = 200):
    out = []
    for i in range(n_win):
        seg = text[skip + i * chars_per: skip + (i + 1) * chars_per]
        if len(seg) < chars_per // 2:
            break
        out.append("\n\n".join(seg[j * 200:(j + 1) * 200] for j in range(6)))
    return out


# ---------------------------------------------------------------- 攻击
def synonym_attack(codec: GreenlistCodec, text: str, frac: float, seed: int):
    r = random.Random(seed)
    toks = codec._tokenizer(text)
    n_dict = sum(1 for _, n in toks if n and n in codec._w2group)
    n_target = int(n_dict * frac)
    out, changed = [], 0
    for raw, norm in toks:
        grp = codec._w2group.get(norm) if norm else None
        if grp and changed < n_target and r.random() < frac + 0.35:
            alts = [x for x in grp if x != norm]
            if alts:
                out.append(r.choice(alts))
                changed += 1
            else:
                out.append(raw)
        else:
            out.append(raw)
    return "".join(out), changed


# ---------------------------------------------------------------- 主流程
def run_lang(tag: str, lang_bytes: bytes, test_docs: list[str], null_docs: list[str],
             raw_dict: dict, zh_mode: bool = False):
    base = GreenlistCodec(KEY, SALT, language_tag=lang_bytes)
    groups = filter_dict_by_corpus(raw_dict, null_docs + test_docs, base._tokenizer, zh_mode=zh_mode)
    codec = GreenlistCodec(KEY, SALT, dictionary=groups, language_tag=lang_bytes)
    n_words = len({w for ws in groups.values() for w in ws})
    print(f"\n===== {tag} =====")
    print(f"词典: {len(groups)} 组 / {n_words} 词；管线后: {codec.stats}")
    codec.calibrate_p0(null_docs)
    print(f"null 标定: {len(null_docs)} 文档；测试: {len(test_docs)} 文档")

    # 逐文档往返 + 改写（masked 汉明距，零覆盖带不投票）
    rt, mh30, mh50, mh70 = [], [], [], []
    sumz_m, sumz_n, dict_counts = [], [], []
    for i, doc in enumerate(test_docs):
        uid = (0x1000 + i * 0x0111) & 0xFFFF
        marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
        rep = codec.detect(marked)
        dist, active = codec.masked_hamming(marked, uid)
        rt.append((dist, active))
        dict_counts.append(rep.n_dict_words)
        sumz_m.append(rep.existence_score)
        sumz_n.append(codec.detect(doc).existence_score)
        for frac, bucket in ((0.30, mh30), (0.50, mh50), (0.70, mh70)):
            rw, _ = synonym_attack(codec, marked, frac, 100 + i)
            d, a = codec.masked_hamming(rw, uid)
            bucket.append((d, a))

    n_exact = sum(1 for d, _ in rt if d == 0)
    print(f"[往返 masked 汉明=0] {n_exact}/{len(rt)} = {n_exact/len(rt)*100:.0f}%")
    print(f"[词典词/600词文档] 均值={sum(dict_counts)/len(dict_counts):.0f} "
          f"min={min(dict_counts)} max={max(dict_counts)}")
    print(f"[Σ|z|] 嵌入后={sum(sumz_m)/len(sumz_m):.1f} vs null={sum(sumz_n)/len(sumz_n):.1f}")
    for name, bucket in (("30%", mh30), ("50%", mh50), ("70%", mh70)):
        d_mean = sum(d for d, _ in bucket) / len(bucket)
        le1 = sum(1 for d, _ in bucket if d <= 1) / len(bucket)
        print(f"[改写{name}] masked 汉明均值={d_mean:.2f}  ≤1占比={le1*100:.0f}%")

    # 双语示例
    doc = test_docs[0]
    uid = 0x1000
    marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(0))
    diffs = []
    for (r1, n1), (r2, n2) in zip(codec._tokenizer(doc), codec._tokenizer(marked)):
        if n1 and n1 != n2:
            diffs.append(f"{r1} → {r2}")
        if len(diffs) >= 8:
            break
    print(f"[示例替换 uid=0x{uid:04X}]: {diffs}")

    return {
        "tag": tag, "n_groups": len(groups), "n_words": n_words,
        "roundtrip": f"{n_exact}/{len(rt)}",
        "dict_words_mean": round(sum(dict_counts) / len(dict_counts), 1),
        "sumz_marked": round(sum(sumz_m) / len(sumz_m), 1),
        "sumz_null": round(sum(sumz_n) / len(sumz_n), 1),
        "ham30_mean": round(sum(d for d, _ in mh30) / len(mh30), 2),
        "ham30_le1": f"{sum(1 for d, _ in mh30 if d <= 1)/len(mh30)*100:.0f}%",
        "ham50_mean": round(sum(d for d, _ in mh50) / len(mh50), 2),
        "ham70_mean": round(sum(d for d, _ in mh70) / len(mh70), 2),
        "sample_diffs": diffs,
    }


def main():
    out = {}
    rng = random.Random(7)

    # ---- EN: Gutenberg × WordNet ----
    books = load_gutenberg()
    en_all = []
    for b in books:
        en_all.extend(windows_en(b, n_win=8))
    rng.shuffle(en_all)
    en_test, en_null = en_all[:18], en_all[18:40]
    out["en"] = run_lang("EN: Gutenberg × WordNet", b"en", en_test, en_null,
                         build_wordnet_dict())

    # ---- ZH-A: 人民日报 × 词林（同域）----
    pd = load_people_daily()
    rng.shuffle(pd)
    pd_test, pd_null = pd[:18], pd[18:60]
    out["zh_same_domain"] = run_lang("ZH: 人民日报 × 词林（同域）", b"zh",
                                     pd_test, pd_null, build_cilin_dict(), zh_mode=True)

    # ---- ZH-B: 红楼梦 × 词林（跨域对照）----
    hlm = load_hlm()
    zh_windows = []
    for d in hlm[:40]:
        zh_windows.extend(windows_zh(d, n_win=3))
    rng.shuffle(zh_windows)
    out["zh_cross_domain"] = run_lang("ZH: 红楼梦 × 词林（跨域对照）", b"zh",
                                      zh_windows[:18], zh_windows[18:50],
                                      build_cilin_dict(), zh_mode=True)

    with open("/tmp/real_corpus_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n结果已写入 /tmp/real_corpus_result.json")


if __name__ == "__main__":
    main()
