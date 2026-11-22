#!/usr/bin/env python3
"""paper_ea1_llm_corpus.py: E-A1——真实 LLM 交付物语料生成（经 SiliconFlow）。

并发版（8 工作线程，断点续跑）。费用纪律（PLAN 约束 7）：每调用落
api_usage.jsonl；预算上限 ¥200，逼近 80% 暂停。

可复现: python experiments/paper_ea1_llm_corpus.py [--n 170] [--workers 8]
产出: papers/paperA/data/llm_corpus_{zh,en}.jsonl + api_usage.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BUDGET_CNY = 200.0
WARN_FRAC = 0.8

ZH_GENRES = [
    ("咨询报告", "写一份结构化咨询报告，包含：背景与问题诊断、三条可执行建议、实施路线图。"
     "使用'不仅…而且''因此''应当'等书面连接词，正文 900-1300 字，不要标题以外的 markdown。"),
    ("法律文书", "写一份法律意见书片段，包含：事实陈述、法律分析、结论与风险提示。"
     "使用规范法律用语，正文 900-1300 字，不要 markdown 列表。"),
    ("研究交付物", "写一份技术调研报告的章节，包含：现状综述、方法对比（至少两种）、"
     "你的建议。使用学术书面语，正文 900-1300 字。"),
]
EN_GENRES = [
    ("consulting", "Write a structured consulting report section: background and problem "
     "diagnosis, three actionable recommendations, and an implementation roadmap. "
     "Use formal connectors (furthermore, therefore, should). 400-600 words, plain paragraphs."),
    ("legal", "Write a legal memo excerpt: statement of facts, legal analysis, conclusion "
     "and risk notes. Formal legal register. 400-600 words, plain paragraphs."),
    ("research", "Write a section of a technical survey report: overview of the state of "
     "the art, comparison of at least two methods, and a recommendation. "
     "Academic register. 400-600 words."),
]
TOPICS_ZH = ["制造业数字化转型", "跨境电商合规", "城市更新项目", "新能源汽车供应链",
             "医疗数据治理", "养老服务体系", "工业园区能耗优化", "职业教育改革",
             "社区团购运营", "文旅融合发展", "智慧农业试点", "中小企业融资",
             "数据要素流通", "基层医疗服务", "旧改安置方案", "跨境电商退税",
             "知识产权保护", "碳排放核算", "智慧物流网络", "县域商业体系",
             "预制菜产业", "直播电商监管", "个人信息保护", "算力中心建设"]
TOPICS_EN = ["digital transformation in manufacturing", "cross-border e-commerce compliance",
             "urban renewal projects", "EV supply chain resilience", "healthcare data governance",
             "eldercare service systems", "industrial park energy optimization",
             "vocational education reform", "community group-buying operations",
             "culture-tourism integration", "smart agriculture pilots", "SME financing",
             "data element circulation", "primary care services", "carbon accounting",
             "IP protection strategy", "compute infrastructure planning",
             "livestream commerce regulation", "personal data protection",
             "county-level retail networks", "prefabricated food industry",
             "smart logistics networks", "workforce reskilling", "water utility digitization"]

PRICE_PER_MTOK = 1.5  # Qwen2.5-32B 估算刊价（元/百万 token）；核算以账单为准


def call_api(model, prompt, key, max_retries=4):
    req = urllib.request.Request(
        "https://api.siliconflow.cn/v1/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.9,
            "max_tokens": 1400,
        }).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    last = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    raise last


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=170, help="每语种每体裁篇数")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--model", default="Qwen/Qwen2.5-32B-Instruct")
    args = ap.parse_args()

    key = os.environ["SILICONFLOW_API_KEY"]
    rng = random.Random(20260911)
    outdir = os.path.join("papers", "paperA", "data")
    os.makedirs(outdir, exist_ok=True)
    usage_path = os.path.join(outdir, "api_usage.jsonl")

    jobs = []
    for i in range(args.n):
        for genre, instr in ZH_GENRES:
            topic = TOPICS_ZH[rng.randrange(len(TOPICS_ZH))]
            jobs.append(("zh", genre, i, args.model, f"话题：{topic}。{instr}"))
        for genre, instr in EN_GENRES:
            topic = TOPICS_EN[rng.randrange(len(TOPICS_EN))]
            jobs.append(("en", genre, i, args.model, f"Topic: {topic}. {instr}"))

    done = set()
    for lang in ("zh", "en"):
        p = os.path.join(outdir, f"llm_corpus_{lang}.jsonl")
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                d = json.loads(line)
                done.add((lang, d["genre"], d["idx"]))

    todo = [jb for jb in jobs if (jb[0], jb[1], jb[2]) not in done]
    total = len(todo)
    lock = threading.Lock()
    spent_list = [0.0]
    finished = [0]

    def worker(job):
        lang, genre, idx, model, prompt = job
        with lock:
            if spent_list[0] >= BUDGET_CNY * WARN_FRAC:
                return
        d = call_api(model, prompt, key)
        text = d["choices"][0]["message"]["content"]
        u = d["usage"]
        cost = (u["prompt_tokens"] + u["completion_tokens"]) / 1e6 * PRICE_PER_MTOK
        rec = {"lang": lang, "genre": genre, "idx": idx, "topic_seed": prompt[:30],
               "text": text, "model": model, "usage": u,
               "est_cost_cny": round(cost, 5)}
        with lock:
            spent_list[0] += cost
            with open(os.path.join(outdir, f"llm_corpus_{lang}.jsonl"), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
            with open(usage_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"job": f"{lang}/{genre}/{idx}", "model": model,
                                    "usage": u,
                                    "est_cost_cny": round(cost, 5),
                                    "spent_cum": round(spent_list[0], 3)})
                        + chr(10))
            finished[0] += 1
            if finished[0] % 50 == 0:
                print(f"[{finished[0]}/{total}] spent~{spent_list[0]:.2f}CNY",
                      flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(worker, todo))

    print(f"done/aborted. est total ~{spent_list[0]:.2f} CNY (bill is authoritative)")


if __name__ == "__main__":
    main()
