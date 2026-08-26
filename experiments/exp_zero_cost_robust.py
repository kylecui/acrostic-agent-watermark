"""exp_zero_cost_robust.py: 零感词典 vs 词林词典 —— 鲁棒性 + N 组文字对比。

两种语料（文体适配检验）：
- 书面语（主场景，AI 生成文本）：docs/*.md 技术文档切 30 篇 × 800 字窗口
- 口语/叙事（挑战场景）：PAWS-X zh 拼接 30 篇 × 20 句 ≈900 字

两大部分：
1. N 组文字对比：词林 codec（语料过滤） vs 零感 codec（149 组固定，16 带）。
   逐篇统计：词典命中、带覆盖 n>=1/n>=2、直解汉明、存在性。
   —— 检验"定向补带是否跨文体泛化"。零感词典是书面语导向的：
      对技术书面语命中率高（16 带全覆盖），对口语叙事文本几乎不命中。

2. 攻击谱（书面语主场景，两种 codec 各 30 篇 × 各攻击）：
   - rt      嵌入往返基线（masked_hamming）
   - s30/s50 同义替换狠攻（synonym_attack：知道词典、不知密钥）
   - paws    温和改写（PAWS 实测转移矩阵参数）
   - pku     重度改写（PKU 实测参数，del_mode=mix 真实物理）
   - del.3/del.5 段落删除（删 30%/50% 整句后 soft_match 1000 候选）
   指标：汉明均值、≤1 比例、Σ|z|（存在性）、soft 存活率。

方法论注：paws/pku 的转移矩阵对两种 codec 分别标定（各自的
_w2group/_tokenizer），保证参数公平。del_mode=mix 是 exp_pku_real_physics
修正后的"真实物理"——del 词一半落回词典（颜色随机污染）一半消失。

运行：python experiments/exp_zero_cost_robust.py
"""
from __future__ import annotations

import glob
import random
import sys
from collections import Counter
from typing import Optional

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

import pyarrow.parquet as pq

from aawm.greenlist import GreenlistCodec, build_zero_cost_zh_codec
from dict_build import build_cilin_dict
from exp_real_corpus import filter_dict_by_corpus, synonym_attack

KEY = bytes(range(32))
SALT = b"real-corpus-2026"
N_SENT = 20          # 每篇拼接句子数
N_DOCS = 60          # 30 test + 30 null
N_CAND = 1000        # soft_match 候选规模
WIN_CHARS = 800      # 书面语窗口字数
PAWS_GLOB = "corpus/paraphrase/train-00000-of-00001-*.parquet"
PKU_TSV = "corpus/paraphrase/pku_paraphrase.tsv"
DOC_GLOB = "docs/*.md"


# ------------------------------------------------------------------ 数据
def load_paws_positive() -> list[tuple[str, str]]:
    """PAWS-X zh train 正样本对 (sentence1, sentence2)，无 pandas 依赖。"""
    t = pq.read_table(glob.glob(PAWS_GLOB)[0])
    s1 = t.column("sentence1").to_pylist()
    s2 = t.column("sentence2").to_pylist()
    score = t.column("score").to_pylist()
    return [(a, b) for a, b, sc in zip(s1, s2, score) if sc == 1]


def load_pku_pairs(limit: int = 20000) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    with open(PKU_TSV, encoding="utf-8") as f:
        for line in f:
            s1, s2 = line.rstrip("\n").split("\t")
            out.append((s1, s2))
            if len(out) >= limit:
                break
    return out


def make_docs_written(n_docs: int = N_DOCS) -> list[str]:
    """书面语主场景：docs/*.md 全中文拼接，按 WIN_CHARS 窗口切 N_DOCS 篇。"""
    import re
    text = ""
    for p in sorted(glob.glob(DOC_GLOB)):
        try:
            t = open(p, encoding="utf-8").read()
        except Exception:
            continue
        # 只保留中文字符与中文标点，去掉 markdown/代码
        t = re.sub(r"```.*?```", "", t, flags=re.S)
        zh = "".join(re.findall(r"[\u4e00-\u9fff，。；：、！？——（）「」“”‘’《》]", t))
        text += zh
    docs = [text[i * WIN_CHARS:(i + 1) * WIN_CHARS] for i in range(n_docs)]
    return [d for d in docs if len(d) >= WIN_CHARS * 0.6]


def make_docs_paws(n_docs: int = N_DOCS) -> list[str]:
    """口语/叙事挑战场景：PAWS-X 正样本句拼接，每篇 N_SENT 句。"""
    paws = load_paws_positive()
    rng = random.Random(0)
    rng.shuffle(paws)
    paras = [s0 for s0, _ in paws]
    return [" ".join(paras[i * N_SENT:(i + 1) * N_SENT]) for i in range(n_docs)]


# ------------------------------------------------------------------ codec
def build_cilin_codec(docs: list[str]) -> GreenlistCodec:
    """词林 codec：语料过滤 + p0 标定（与 exp_paws_attack 口径一致）。"""
    base = GreenlistCodec(KEY, SALT, language_tag=b"zh")
    raw = build_cilin_dict("corpus/dict/cilin_extended.txt")
    groups = filter_dict_by_corpus(raw, docs, base._tokenizer,
                                   max_group=20, zh_mode=True)
    codec = GreenlistCodec(KEY, SALT, dictionary=groups, language_tag=b"zh")
    codec.calibrate_p0(docs[len(docs) // 2:])
    return codec


def build_zero_codec(docs: list[str]) -> GreenlistCodec:
    """零感 codec：149 组固定 + p0 标定。"""
    codec = build_zero_cost_zh_codec(KEY, SALT, n_bands=16,
                                      calibrate_corpus=docs[len(docs) // 2:])
    return codec


def build_hybrid_codec(docs: list[str]) -> GreenlistCodec:
    """混合 codec：零感词典打底（149 组安全词）+ 词林内容词补带。

    零感组先入 dict 取 word_owner 优先权；词林组仅当不与零感词共享
    时加入（先到先得不吞组）。tokenizer 词典 = 零感词 ∪ 阻断词 ∪ 词林词。
    """
    from aawm.greenlist import make_zh_tokenizer
    from aawm.synonym_data import (
        load_zero_cost_zh_dictionary, load_zero_cost_zh_block_words,
    )
    zero_dict = load_zero_cost_zh_dictionary()
    block = load_zero_cost_zh_block_words()
    zero_words = {w for ws in zero_dict.values() for w in ws}

    # 词林组语料过滤（与 build_cilin_codec 同口径）
    base = GreenlistCodec(KEY, SALT, language_tag=b"zh")
    raw_cilin = build_cilin_dict("corpus/dict/cilin_extended.txt")
    cilin_groups = filter_dict_by_corpus(
        raw_cilin, docs, base._tokenizer, max_group=20, zh_mode=True)

    # 合并：零感先入，词林组跳过任何与零感词共享的组
    merged: dict[str, list[str]] = dict(zero_dict)
    owned = set(zero_words)
    n_cilin_added = 0
    for key, words in cilin_groups.items():
        if any(w in owned for w in words):
            continue
        merged[key] = words
        owned.update(words)
        n_cilin_added += 1

    all_words = owned | block
    tokenizer = make_zh_tokenizer(dict_words=all_words)
    codec = GreenlistCodec(
        KEY, SALT, n_bands=16,
        dictionary=merged, language_tag=b"zh", tokenizer=tokenizer,
    )
    codec.calibrate_p0(docs[len(docs) // 2:])
    print(f"  [hybrid] 零感 {len(zero_dict)} 组 + 词林 {n_cilin_added} 组"
          f" = {len(merged)} 组, 编码组 {len(codec._groups)}")
    return codec


# ------------------------------------------------------------ 改写攻击
def transfer_matrix(codec: GreenlistCodec, pairs: list[tuple[str, str]],
                    limit: int = 6000) -> dict:
    """词典词级转移矩阵：keep / 组内同义换 / 删除·组外（同 exp_paws_attack）。"""
    w2g = codec._w2group
    cnt = {"keep": 0, "grp_sub": 0, "del": 0, "n1": 0}
    for s1, s2 in pairs[:limit]:
        t1 = [n for _, n in codec._tokenizer(s1) if n]
        d1 = [w for w in t1 if w in w2g]
        if not d1:
            continue
        cnt["n1"] += len(d1)
        for w in d1:
            if w in s2:
                cnt["keep"] += 1
            elif any(x in s2 for x in w2g[w] if x != w):
                cnt["grp_sub"] += 1
            else:
                cnt["del"] += 1
    tot = max(cnt["n1"], 1)
    cnt["keep_p"] = cnt["keep"] / tot
    cnt["grp_sub_p"] = cnt["grp_sub"] / tot
    cnt["del_p"] = cnt["del"] / tot
    return cnt


def paraphrase_style_attack(codec: GreenlistCodec, text: str, seed: int,
                            p_del: float, p_grp: float) -> str:
    """del_mode=mix（alpha=0.5 真实物理）：del 词一半落回词典随机词、一半消失。"""
    r = random.Random(seed)
    out = []
    for raw, norm in codec._tokenizer(text):
        grp = codec._w2group.get(norm) if norm else None
        if grp:
            x = r.random()
            if x < p_del:
                if r.random() < 0.5:
                    head = r.choice(list(codec._groups))
                    out.append(r.choice(codec._groups[head]))
                # 否则 token 消失
            elif x < p_del + p_grp:
                alts = [c for c in grp if c != norm]
                out.append(r.choice(alts) if alts else raw)
            else:
                out.append(raw)
        else:
            out.append(raw)
    return "".join(out)


def paragraph_delete(text: str, delta: float, seed: int) -> str:
    """段落删除：句子由空格拼接，按句随机删 delta 比例（整句为段落）。"""
    r = random.Random(seed)
    sents = [s for s in text.split(" ") if s]
    n_del = int(round(delta * len(sents)))
    idxs = list(range(len(sents)))
    r.shuffle(idxs)
    del_set = set(idxs[:n_del])
    return " ".join(s for i, s in enumerate(sents) if i not in del_set)


# ------------------------------------------------------------------ 主流程
def attack_report(name: str, hams: list, sumz: list,
                  soft_hits: Optional[tuple] = None) -> None:
    n = len(hams)
    mean = sum(hams) / n
    le1 = sum(1 for h in hams if h <= 1) / n
    s = f"[{name}] 汉明均值={mean:.2f}  ≤1={le1*100:.0f}%  Σ|z|={sum(sumz)/n:.1f}"
    if soft_hits:
        s += f"  soft={soft_hits[0]}/{soft_hits[1]}"
    print(s)


def cover_compare(tag: str, docs: list[str], codecs: dict,
                  mode: str = "embed") -> None:
    """N 组覆盖对比：embed 后统计命中/覆盖/直解/存在性。"""
    test_docs = docs[:len(docs) // 2]
    print(f"\n[{tag}] {len(test_docs)} 篇 test"
          f"（字/篇 ≈ {sum(len(d) for d in test_docs) / len(test_docs):.0f}）")
    print(f"{'codec':>4} | {'命中':>4} {'n>=1':>5} {'n>=2':>5} | "
          f"{'16带全覆盖':>8} {'直解=0':>6} {'存在性':>7}")
    for name, c in codecs.items():
        hits, n1s, n2s, full, exact, sums = [], [], [], 0, 0, []
        for i, doc in enumerate(test_docs):
            uid = (0x1000 + i * 0x0111) & 0xFFFF
            m = c.embed(doc, uid, bias=1.0, rng=random.Random(i))
            rep = c.detect(m)
            n1 = sum(1 for st in rep.bands if st.n >= 1)
            n2 = sum(1 for st in rep.bands if st.n >= 2)
            hits.append(rep.n_dict_words); n1s.append(n1); n2s.append(n2)
            sums.append(rep.existence_score)
            if n1 == c.n_bands: full += 1
            if rep.uid == uid: exact += 1
        print(f"{name:>4} | {sum(hits)/len(test_docs):4.0f} "
              f"{sum(n1s)/len(test_docs):5.1f} {sum(n2s)/len(test_docs):5.1f} | "
              f"{full:>4}/{len(test_docs)}   {exact:>4}/{len(test_docs)}  "
              f"{sum(sums)/len(test_docs):7.1f}")


def main() -> None:
    pku = load_pku_pairs(20000)

    # ---- 两种语料 ----
    docs_w = make_docs_written()
    docs_p = make_docs_paws()
    print(f"书面语语料: {len(docs_w)} 篇（docs/*.md 窗口切片）")
    print(f"口语语料  : {len(docs_p)} 篇（PAWS 拼接）")

    # 每种语料用各自语料构造词林 codec（语料过滤），零感 codec 固定
    codecs_w = {
        "词林": build_cilin_codec(docs_w),
        "零感": build_zero_codec(docs_w),
    }
    codecs_p = {
        "词林": build_cilin_codec(docs_p),
        "零感": build_zero_codec(docs_p),
    }
    for tag, c in codecs_w.items():
        print(f"书面语 {tag}: {len(c._groups)} 组 / {c.stats}")

    # ================= Part 1: N 组文字对比（双语料） =================
    print("\n========== Part 1: N 组文字覆盖对比 ==========")
    cover_compare("书面语（主场景/AI 文本）", docs_w, codecs_w)
    cover_compare("口语叙事（挑战场景）", docs_p, codecs_p)

    # 书面语逐带平均 n_b
    print("\n书面语逐带平均 n_b：")
    for name, c in codecs_w.items():
        band_avg = [0.0] * c.n_bands
        for i, doc in enumerate(docs_w[:len(docs_w) // 2]):
            rep = c.detect(doc)
            for st in rep.bands:
                band_avg[st.band] += st.n
        print(f"  {name}: " + " ".join(f"{v/(len(docs_w)//2):.1f}" for v in band_avg))

    # ================= Part 2: 攻击谱（书面语主场景） =================
    print("\n========== Part 2: 攻击谱（书面语主场景，30 篇） ==========")
    test_docs = docs_w[:len(docs_w) // 2]
    paws_pairs = load_paws_positive()
    for tag, c in codecs_w.items():
        print(f"\n--- {tag} codec ---")
        tm_paws = transfer_matrix(c, paws_pairs, limit=2000)
        tm_pku = transfer_matrix(c, pku, limit=20000)
        print(f"  PAWS 转移: keep={tm_paws['keep_p']*100:.0f}% "
              f"grp={tm_paws['grp_sub_p']*100:.1f}% del={tm_paws['del_p']*100:.0f}%")
        print(f"  PKU  转移: keep={tm_pku['keep_p']*100:.0f}% "
              f"grp={tm_pku['grp_sub_p']*100:.1f}% del={tm_pku['del_p']*100:.0f}%")

        buckets = {k: [] for k in ("rt", "s30", "s50", "paws", "pku")}
        sumzs = {k: [] for k in buckets}
        for i, doc in enumerate(test_docs):
            uid = (0x1000 + i * 0x0111) & 0xFFFF
            m = c.embed(doc, uid, bias=1.0, rng=random.Random(i))
            buckets["rt"].append(c.masked_hamming(m, uid)[0])
            sumzs["rt"].append(c.detect(m).existence_score)

            rw, _ = synonym_attack(c, m, 0.30, 100 + i)
            buckets["s30"].append(c.masked_hamming(rw, uid)[0])
            sumzs["s30"].append(c.detect(rw).existence_score)

            rw, _ = synonym_attack(c, m, 0.50, 200 + i)
            buckets["s50"].append(c.masked_hamming(rw, uid)[0])
            sumzs["s50"].append(c.detect(rw).existence_score)

            rw = paraphrase_style_attack(c, m, 300 + i,
                                         tm_paws["del_p"], tm_paws["grp_sub_p"])
            buckets["paws"].append(c.masked_hamming(rw, uid)[0])
            sumzs["paws"].append(c.detect(rw).existence_score)

            rw = paraphrase_style_attack(c, m, 400 + i,
                                         tm_pku["del_p"], tm_pku["grp_sub_p"])
            buckets["pku"].append(c.masked_hamming(rw, uid)[0])
            sumzs["pku"].append(c.detect(rw).existence_score)

        for delta, name in ((0.3, "del.3"), (0.5, "del.5")):
            ok = 0
            for i, doc in enumerate(test_docs):
                uid = (0x1000 + i * 0x0111) & 0xFFFF
                cands = sorted({uid} | {
                    random.Random(2026 + i * 31 + s).randrange(1 << c.n_bands)
                    for s in range(N_CAND - 1)})
                for s in range(5):
                    m = c.embed(doc, uid, bias=1.0, rng=random.Random(1000 + s))
                    att = paragraph_delete(m, delta, 5000 + i * 10 + s)
                    best, _, _ = c.soft_match(att, cands, min_n=1, margin=0.0)
                    if best == uid:
                        ok += 1
            print(f"[{name}] soft 存活 {ok}/{len(test_docs)*5}（候选 {N_CAND}）")

        attack_report("嵌入往返(rt)", buckets["rt"], sumzs["rt"])
        attack_report("同义替换30%", buckets["s30"], sumzs["s30"])
        attack_report("同义替换50%", buckets["s50"], sumzs["s50"])
        attack_report("PAWS温和改写", buckets["paws"], sumzs["paws"])
        attack_report("PKU重度改写", buckets["pku"], sumzs["pku"])

    print("""
读表：
  · Part 1 的"16带全覆盖"检验定向补带是否跨文体泛化：
      零感词典是书面语导向（连词/副词/书面动词），对 AI 技术文本有效，
      对口语叙事文本（PAWS）命中骤降——文体适配是零感路径的边界。
  · 攻击谱在书面语主场景对比两种 codec 的存活边界。
  · Σ|z| 是存在性（soft 得分上限），衰减越快越接近 null。
""")


if __name__ == "__main__":
    main()
