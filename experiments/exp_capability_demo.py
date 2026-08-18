#!/usr/bin/env python3
"""capability demo: 生成双语能力边界示例（成功 + 失败），输出结构化结果。

跑完输出 JSON 到 stdout（由文档生成步骤消费）。
"""
from __future__ import annotations

import json
import random
import sys

sys.path.insert(0, "src")

from aawm.binding import DocumentBinder, VerdictKind
from aawm.greenlist import GreenlistCodec

KEY = bytes(range(32))
SALT = b"demo-salt-2026"

EN_TEMPLATES = [
    "The {w} collects telemetry from every {w} agent in the fleet.",
    "Each agent processes a {w} volume of events and produces a {w} summary.",
    "The {w} selects good anchor positions where a quick swap can carry a bit.",
    "A strong key derivation makes the mapping {w} to outsiders.",
    "The verifier recomputes the same anchors and reads the bits back into an id.",
    "Minor edits only damage a {w} part of the {w}, so it still survives.",
    "The team believes the {w} is {w} and the cost is {w}.",
    "After a {w} analysis the conclusion was {w} and the result was {w}.",
]
EN_FILLER = [
    "system", "process", "value", "human", "between", "during",
    "though", "while", "should", "would", "might", "place",
    "group", "number", "change", "right",
]


def make_en_text(n_slots: int, seed: int, codec: GreenlistCodec) -> str:
    """模拟自然文本：~35% 词典词（随机组内随机词）+ 65% 填充词。"""
    r = random.Random(seed)
    group_list = list(codec._groups.values())
    out, made = [], 0
    while made < n_slots:
        if r.random() < 0.35:
            out.append(r.choice(r.choice(group_list)))
            made += 1
        else:
            out.append(r.choice(EN_FILLER))
    return " ".join(out)


ZH_TEMPLATES = [
    "这份报告的内容非常{w}，需要尽快处理。",
    "团队的表现十分{w}，客户对此{w}。",
    "系统运行{w}，各项指标{w}，结果{w}。",
    "我们认为该方案{w}，但其成本仍然{w}。",
    "经过{w}的分析，结论是{w}的。",
]


def make_zh_text(n_slots: int, seed: int, codec: GreenlistCodec) -> str:
    r = random.Random(seed)
    group_list = list(codec._groups.values())
    sents, made = [], 0
    while made < n_slots:
        t = r.choice(ZH_TEMPLATES)
        picks = [r.choice(g) for g in r.sample(group_list, t.count("{w}"))]
        for p in picks:
            t = t.replace("{w}", p, 1)
        sents.append(t)
        made += len(picks)
    return "\n".join(sents)


def en_paraphrase(codec: GreenlistCodec, text: str, frac: float, seed: int):
    r = random.Random(seed)
    toks = codec._tokenizer(text)
    n_dict = sum(1 for _, n in toks if n is not None)
    n_target = int(n_dict * frac)
    out, changed = [], 0
    for raw, norm in toks:
        grp = codec._w2group.get(norm) if norm else None
        if grp and changed < n_target and r.random() < frac + 0.35:
            out.append(r.choice([x for x in grp if x != norm]))
            changed += 1
        else:
            out.append(raw)
    return "".join(out), changed


def zh_paraphrase(codec: GreenlistCodec, text: str, frac: float, seed: int):
    return en_paraphrase(codec, text, frac, seed)


def band_summary(rep):
    return {
        "uid": f"0x{rep.uid:04X}",
        "n_dict": rep.n_dict_words,
        "sum_abs_z": round(rep.existence_score, 1),
        "per_band": [
            {"b": st.band, "n": st.n, "z": round(st.z, 2)} for st in rep.bands if st.has_signal
        ],
    }


def diff_words(a: str, b: str, limit=6):
    """找前 limit 处词级差异（英文按空格，中文按词典 token 简化）。"""
    ta, tb = a.split(), b.split()
    diffs, i = [], 0
    for wa, wb in zip(ta, tb):
        if wa != wb:
            diffs.append(f"{wa} → {wb}")
            i += 1
            if i >= limit:
                break
    return diffs


def main():
    results = {}

    # ============ 英文 ============
    en = GreenlistCodec(KEY, SALT, language_tag=b"en")
    null_corpus = [make_en_text(600, s, en) for s in range(100, 108)]
    en.calibrate_p0(null_corpus)

    UID = 0x1234
    text_en = make_en_text(600, 7, en)
    marked_en = en.embed(text_en, UID, bias=1.0)
    rep_en = en.detect(marked_en)
    rep_null = en.detect(text_en)

    # E1 往返
    results["E1_roundtrip"] = {
        "lang": "en",
        "uid_expected": f"0x{UID:04X}",
        "uid_decoded": f"0x{rep_en.uid:04X}",
        "hamming": bin(rep_en.uid ^ UID).count("1"),
        "before_sumz": round(rep_null.existence_score, 1),
        "after_sumz": round(rep_en.existence_score, 1),
        "sample_diffs": diff_words(text_en, marked_en),
        "head_before": text_en[:150],
        "head_after": marked_en[:150],
        "verdict": "成功" if rep_en.uid == UID else "失败",
    }

    # E2 三档改写
    e2 = {}
    for frac, seed in [(0.30, 9), (0.50, 11), (0.70, 13)]:
        rw, nch = en_paraphrase(en, marked_en, frac, seed)
        rep = en.detect(rw)
        e2[f"rewrite_{int(frac*100)}"] = {
            "changed": nch,
            "total": rep_en.n_dict_words,
            "uid": f"0x{rep.uid:04X}",
            "hamming": bin(rep.uid ^ UID).count("1"),
            "sum_abs_z": round(rep.existence_score, 1),
            "sample_diffs": diff_words(marked_en, rw, 4),
            "exact_match": rep.uid == UID,
        }
    results["E2_rewrite_en"] = e2

    # E3 错误密钥
    bad = GreenlistCodec(b"K" * 32, SALT, language_tag=b"en")
    rep_bad = bad.detect(marked_en)
    results["E3_wrong_key"] = {
        "uid": f"0x{rep_bad.uid:04X}",
        "hamming_vs_true": bin(rep_bad.uid ^ UID).count("1"),
        "sum_abs_z": round(rep_bad.existence_score, 1),
    }

    # ============ 信道 A 篡改 ============
    binder = DocumentBinder(KEY, SALT)
    paras_en = [make_en_text(150, s, en) for s in (1, 2, 3, 4)]
    doc_en = "\n\n".join(paras_en)
    seal = binder.sign(doc_en, aad=f"uid:{UID:04x}".encode())

    tampered_paras = paras_en.copy()
    tampered_paras[2] = tampered_paras[2].replace(
        "significant", "insignificant"
    ) if "significant" in tampered_paras[2] else tampered_paras[2] + " An attacker inserted this sentence to change the meaning."
    doc_tampered = "\n\n".join(tampered_paras)
    v_tamper = binder.verify(doc_tampered, seal)
    v_intact = binder.verify(doc_en, seal)
    reordered = "\n\n".join([paras_en[2], paras_en[0], paras_en[3], paras_en[1]])
    v_reorder = binder.verify(reordered, seal)

    results["E4_binding"] = {
        "intact": v_intact.kind.value,
        "tampered": v_tamper.kind.value,
        "tampered_indices": v_tamper.mismatched_indices,
        "tampered_head": tampered_paras[2][:100],
        "reordered": v_reorder.kind.value,
        "matched_indices_reorder": v_reorder.matched_indices[:4],
    }

    # ============ 英文失败案例 ============
    # F1 短文本：40 slot（模拟词典率 35% → 仅 ~14 词典词，每带样本不足）
    short = make_en_text(40, 21, en)
    short_marked = en.embed(short, UID, bias=1.0)
    rep_short = en.detect(short_marked)
    results["F1_short_text"] = {
        "n_slots": 40,
        "n_dict": rep_short.n_dict_words,
        "zero_coverage_bands": sum(1 for st in rep_short.bands if not st.has_signal),
        "uid": f"0x{rep_short.uid:04X}",
        "hamming": bin(rep_short.uid ^ UID).count("1"),
        "sum_abs_z": round(rep_short.existence_score, 1),
        "sample": short[:120],
    }

    # F2 70% 改写后信道 A 也失效（已被改写）
    rw70, _ = en_paraphrase(en, marked_en, 0.70, 13)
    seal_full = binder.sign(marked_en, aad=f"uid:{UID:04x}".encode())
    v_rw = binder.verify(rw70, seal_full)
    results["F2_heavy_rewrite_breaks_A"] = {
        "verdict_A": v_rw.kind.value,
        "mismatched_paras_ratio": f"{len(v_rw.mismatched_indices)}/{len(seal_full.para_hashes)}",
    }

    # F3 零覆盖带陷阱：词表受限的自然风格文本（band 覆盖不全是真实部署风险）
    limited_words = ["good", "simple", "rapid", "bright"] * 30 + EN_FILLER * 8
    r_l = random.Random(5)
    r_l.shuffle(limited_words)
    limited_text = " ".join(limited_words)
    limited_marked = en.embed(limited_text, UID, bias=1.0)
    rep_limited = en.detect(limited_marked)
    zero_b = [st.band for st in rep_limited.bands if not st.has_signal]
    results["F3_band_coverage"] = {
        "n_dict": rep_limited.n_dict_words,
        "zero_bands": zero_b,
        "uid": f"0x{rep_limited.uid:04X}",
        "hamming": bin(rep_limited.uid ^ UID).count("1"),
        "flipped_bits_in_zero_bands": [b for b in zero_b if ((UID >> b) & 1) == 1],
        "note": "零覆盖带默认解 bit=0；真实 UID 在该带为 1 时必然翻位",
    }

    # ============ 中文 ============
    zh = GreenlistCodec(KEY, SALT, language_tag=b"zh")
    zh_null = [make_zh_text(600, s, zh) for s in range(100, 108)]
    zh.calibrate_p0(zh_null)

    UID_ZH = 0x5678
    text_zh = make_zh_text(600, 7, zh)
    marked_zh = zh.embed(text_zh, UID_ZH, bias=1.0)
    rep_zh = zh.detect(marked_zh)
    rep_zh_null = zh.detect(text_zh)

    results["Z1_roundtrip"] = {
        "lang": "zh",
        "uid_expected": f"0x{UID_ZH:04X}",
        "uid_decoded": f"0x{rep_zh.uid:04X}",
        "hamming": bin(rep_zh.uid ^ UID_ZH).count("1"),
        "before_sumz": round(rep_zh_null.existence_score, 1),
        "after_sumz": round(rep_zh.existence_score, 1),
        "sample_diffs": diff_words(text_zh, marked_zh),
        "head_before": text_zh[:60],
        "head_after": marked_zh[:60],
        "verdict": "成功" if rep_zh.uid == UID_ZH else "失败",
    }

    z2 = {}
    for frac, seed in [(0.30, 9), (0.50, 11), (0.70, 13)]:
        rw, nch = zh_paraphrase(zh, marked_zh, frac, seed)
        rep = zh.detect(rw)
        z2[f"rewrite_{int(frac*100)}"] = {
            "changed": nch,
            "total": rep_zh.n_dict_words,
            "uid": f"0x{rep.uid:04X}",
            "hamming": bin(rep.uid ^ UID_ZH).count("1"),
            "sum_abs_z": round(rep.existence_score, 1),
            "sample_diffs": diff_words(marked_zh, rw, 4),
            "exact_match": rep.uid == UID_ZH,
        }
    results["Z2_rewrite_zh"] = z2

    # Z3 中文错误密钥
    zh_bad = GreenlistCodec(b"K" * 32, SALT, language_tag=b"zh")
    rep_zh_bad = zh_bad.detect(marked_zh)
    results["Z3_wrong_key"] = {
        "uid": f"0x{rep_zh_bad.uid:04X}",
        "hamming_vs_true": bin(rep_zh_bad.uid ^ UID_ZH).count("1"),
        "sum_abs_z": round(rep_zh_bad.existence_score, 1),
    }

    # Z4 中英文密钥隔离
    rep_cross = en.detect(marked_zh)
    results["Z4_cross_lang"] = {
        "uid_reading_zh_text_with_en_codec": f"0x{rep_cross.uid:04X}",
        "n_dict": rep_cross.n_dict_words,
        "note": "英文 codec 读中文文本：词典词 0（词表不重叠），无信号",
    }

    # Z5 边界漂移坑（修复演示）
    edge_text = "各项指标非常稳定。"
    edge_marked = zh.embed(edge_text, 0xFFFF, bias=1.0)
    rep_edge = zh.detect(edge_marked)
    results["Z5_boundary"] = {
        "input": edge_text,
        "output": edge_marked,
        "note": "替换'指标'或'稳定'时，_boundary_safe 保证替换词与邻字不成新词（如'项目'）",
        "roundtrip_uid": f"0x{rep_edge.uid:04X}",
        "n_dict": rep_edge.n_dict_words,
    }

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
