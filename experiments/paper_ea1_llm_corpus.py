#!/usr/bin/env python3
"""paper_ea1_llm_corpus.py: E-A1——真实 LLM 交付物语料生成（经 SiliconFlow）。

语料设计（论文 A 的目标场景：长文档交付物）：
  zh: 咨询报告 / 法律文书 / 研究交付物，各 ~170 篇
  en: consulting report / legal memo / research brief，各 ~170 篇
每篇 800-1500 中文字 / 400-700 英文词；话题由 seed 生成保证多样性。

费用纪律（PLAN §3 约束 7）：每调用落 usage JSONL；预算上限 ¥200，
逼近 80% 暂停。断点续跑：已存在的 (lang, genre, idx) 跳过。

可复现: python experiments/paper_ea1_llm_corpus.py [--n 170]
产出: papers/paperA/data/llm_corpus_{zh,en}.jsonl + api_usage.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.request

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
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(3 * (attempt + 1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=170, help="每语种每体裁篇数")
    ap.add_argument("--model-zh", default="Qwen/Qwen2.5-32B-Instruct")
    ap.add_argument("--model-en", default="Qwen/Qwen2.5-32B-Instruct")
    args = ap.parse_args()

    key = os.environ["SILICONFLOW_API_KEY"]
    rng = random.Random(20260911)
    outdir = os.path.join("papers", "paperA", "data")
    os.makedirs(outdir, exist_ok=True)
    usage_path = os.path.join(outdir, "api_usage.jsonl")

    # 价格（SiliconFlow 官方刊价，元/百万 token）；估算用 prompt+completion 合计
    PRICE_PER_MTOK = 1.5  # Qwen2.5-32B 约价；消费数字在 usage.jsonl 留真值便于核算
    spent = 0.0

    jobs = []
    for i in range(args.n):
        for genre, instr in ZH_GENRES:
            topic = TOPICS_ZH[rng.randrange(len(TOPICS_ZH))]
            jobs.append(("zh", genre, i, args.model_zh,
                         f"话题：{topic}。{instr}"))
        for genre, instr in EN_GENRES:
            topic = TOPICS_EN[rng.randrange(len(TOPICS_EN))]
            jobs.append(("en", genre, i, args.model_en,
                         f"Topic: {topic}. {instr}"))

    done = set()
    for lang in ("zh", "en"):
        p = os.path.join(outdir, f"llm_corpus_{lang}.jsonl")
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                d = json.loads(line)
                done.add((lang, d["genre"], d["idx"]))

    total = len(jobs)
    for j, (lang, genre, idx, model, prompt) in enumerate(jobs):
        if (lang, genre, idx) in done:
            continue
        if spent >= BUDGET_CNY * WARN_FRAC:
            print(f"[预算告警] 已花费约 ¥{spent:.2f}，暂停（续跑请重跑本脚本）")
            break
        d = call_api(model, prompt, key)
        text = d["choices"][0]["message"]["content"]
        u = d["usage"]
        cost = (u["prompt_tokens"] + u["completion_tokens"]) / 1e6 * PRICE_PER_MTOK
        spent += cost
        rec = {"lang": lang, "genre": genre, "idx": idx, "topic_seed": prompt[:30],
               "text": text, "model": model, "usage": u, "est_cost_cny": round(cost, 5)}
        with open(os.path.join(outdir, f"llm_corpus_{lang}.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with open(usage_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"job": j, "model": model, "usage": u,
                                "est_cost_cny": round(cost, 5),
                                "spent_cum": round(spent, 3)}) + "\n")
        if j % 25 == 0:
            print(f"[{j}/{total}] {lang}/{genre}/{idx} 累计≈¥{spent:.2f}", flush=True)
        time.sleep(0.4)

    print(f"完成/中止。估算总花费 ≈ ¥{spent:.2f}（真值以 SiliconFlow 账单为准）")


if __name__ == "__main__":
    main()
