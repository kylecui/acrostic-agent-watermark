#!/usr/bin/env python3
"""exp_cross_domain_fpr.py: 跨域 FPR/TPR 实测（v0.15 质量通行证条目 2）。

问题动机：v0.15 之前的检出率声明全部来自 demo 语料（同域、自标定），
FPR 只在 ai_text_judgment_report 里以一句"4%"带过。本实验在**域外**
语料上实测存在性误报率与归因误报率，并把 TPR/FPR 并列——检出率必须
和误报率一起看才有意义。

域设计（全部为无水印 null 文本切分）：
  demo_in_domain   域内对照：demo_corpus 技术散文切块
  en_literature    域外：公版英文小说（pg84/pg1342/pg2701）
  zh_literature    域外：《红楼梦》章回（古典中文，对现代零感词典更偏远）
  repetitive_template  病态域：模板句重复（v0.15 开发中实测会误报的类别）
  short_fragments  弱区：600-1000 字短块（README 声明的降级区）

两种配置：
  uncalibrated  默认阈值常数（未标定，开箱状态）
  calibrated    null 模型按 demo 语料标定（文档推荐部署形态）

指标（每个域-配置）：
  FPR_existence   watermarked=True 比例（显式盐路径，每文本随机盐）
  FPR_attribution 注册库 50 用户下 user 非空比例（误归因率）
  TPR             对照：同一批文本嵌入 uid 后检出率（看权衡面）

用法: python experiments/exp_cross_domain_fpr.py
"""
from __future__ import annotations

import glob
import json
import os
import random
import sys

sys.path.insert(0, "src")

from aawm.keys import generate_session_salt
from aawm.plugins import Watermarker
from aawm.plugins.registry import UIDRegistry

N_TEXTS = 120          # 每域文本数（fixed seed 可复现）
N_SCAN = 40            # 扫描路径（无盐）子样本
N_USERS = 50           # 注册库规模（归因 FPR）
SEED = 20260911

DEMO_DIR = "src/aawm/data/demo_corpus"
EN_FILES = ["corpus/en/pg84.txt", "corpus/en/pg1342.txt", "corpus/en/pg2701.txt"]
ZH_GLOB = "corpus/zh/*.txt"


def _read_all(paths, encoding="utf-8"):
    return chr(10).join(
        open(p, encoding=encoding, errors="ignore").read() for p in paths)


def demo_corpus_text():
    return _read_all(sorted(glob.glob(os.path.join(DEMO_DIR, "*.md"))))


def _load_chunks(text, size, n, seed):
    """从大文本切 size 字符的块，跳过过短块，取 n 个（可复现）。"""
    rng = random.Random(seed)
    text = " ".join(text.split())
    step = max(1, size // 2)
    population = max(1, (max(1, len(text) - size)) // step)
    k = min(n * 3, population)
    starts = sorted(s * step for s in rng.sample(range(population), k))
    out = []
    for s in starts:
        chunk = text[s:s + size]
        if len(chunk) >= size * 0.9:
            out.append(chunk)
        if len(out) >= n:
            break
    return out


def build_domains():
    demo = demo_corpus_text()
    domains = {
        "demo_in_domain": _load_chunks(demo, 2500, N_TEXTS, SEED),
        "en_literature": _load_chunks(_read_all(EN_FILES), 2500, N_TEXTS, SEED + 1),
        "zh_literature": _load_chunks(_read_all(sorted(glob.glob(ZH_GLOB))),
                                      2500, N_TEXTS, SEED + 2),
        "short_fragments": _load_chunks(demo, 800, N_TEXTS, SEED + 3),
    }
    # 病态模板域：短句重复、零感词饱和（v0.15 开发中实测误报类别）
    tmpl = (
        "项目管理不仅需要规划，而且需要持续跟踪。团队成员应当明确目标，"
        "并且在执行过程中及时调整。质量标准必须统一，因此沟通非常重要。"
    )
    domains["repetitive_template"] = [tmpl * (6 + i % 5) for i in range(N_TEXTS)]
    return domains


def evaluate(domain_texts, lang, calib_corpus=None):
    """返回 (FPR_existence, FPR_attribution, TPR, n)。

    calib_corpus: None=不标定（默认常数）；list=用该语料拟合 null 模型。
    标定语料必须多篇（单篇拟合的 σ 只有盐间方差，跨文本方差缺失 →
    阈值崩塌、FPR 爆炸）。
    """
    wm = Watermarker(codec_mode="zero_cost", registry=UIDRegistry())
    for i in range(N_USERS):
        wm.registry.register(f"user-{i}", uid=1000 + i)
    if calib_corpus:
        wm.calibrate_null_model(calib_corpus)

    fp_exist = fp_attr = tp = n = 0
    for t in domain_texts:
        salt = generate_session_salt()
        r = wm.trace(t, session_salt=salt, language=lang)
        n += 1
        if r.watermarked:
            fp_exist += 1
        if r.user is not None:
            fp_attr += 1
        # TPR 对照：嵌入后检出（同盐路径）
        emb = wm.embed(t, user_id=1000, language=lang)
        t2 = wm.trace(emb.watermarked_text, session_salt=emb.session_salt,
                      bands=emb.bands, language=lang)
        if t2.watermarked:
            tp += 1
    return fp_exist / n, fp_attr / n, tp / n, n


def evaluate_scan(domain_texts, lang):
    """无盐候选扫描路径的 FPR（子样本）。"""
    wm = Watermarker(codec_mode="zero_cost", registry=UIDRegistry())
    fp = n = 0
    for t in domain_texts[:N_SCAN]:
        r = wm.trace(t, language=lang)
        n += 1
        if r.watermarked:
            fp += 1
    return fp / n, n


def main():
    domains = build_domains()
    lang_of = {"demo_in_domain": "zh", "repetitive_template": "zh",
               "short_fragments": "zh",
               "en_literature": "en", "zh_literature": "zh"}
    results = {}
    demo_calib = _load_chunks(demo_corpus_text(), 1200, 20, SEED + 9)
    for name, texts in domains.items():
        lang = lang_of[name]
        row = {}
        fpr_e, fpr_a, tpr, n = evaluate(texts, lang)
        row["uncalibrated"] = {"fpr_existence": round(fpr_e, 4),
                               "fpr_attribution": round(fpr_a, 4),
                               "tpr": round(tpr, 4), "n": n}
        # 跨域标定：用 demo（技术散文）标定，测域外文本——展示域不匹配代价
        fpr_e, fpr_a, tpr, n = evaluate(texts, lang, calib_corpus=demo_calib)
        row["calibrated_xdomain"] = {"fpr_existence": round(fpr_e, 4),
                                     "fpr_attribution": round(fpr_a, 4),
                                     "tpr": round(tpr, 4), "n": n}
        # 域内标定（教科书式正确部署）：同域前半拟合，后半测试
        half = max(5, len(texts) // 2)
        fpr_e, fpr_a, tpr, n = evaluate(texts[half:], lang,
                                        calib_corpus=texts[:half])
        row["calibrated_matched"] = {
            "fpr_existence": round(fpr_e, 4),
            "fpr_attribution": round(fpr_a, 4),
            "tpr": round(tpr, 4), "n": n}
        sfp, sn = evaluate_scan(texts, lang)
        row["scan_path"] = {"fpr_existence": round(sfp, 4), "n": sn}
        results[name] = row
        print(f"[{name}]")
        for k, v in row.items():
            print(f"    {k}: {v}")
    os.makedirs("docs", exist_ok=True)
    with open("experiments/cross_domain_fpr_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print()
    print("结果已写入 experiments/cross_domain_fpr_result.json")


if __name__ == "__main__":
    main()
