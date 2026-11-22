#!/usr/bin/env python3
"""paper_ea5b_build_survey.py: 把 item_pool.json 内联进测评页，生成分发文件。

为什么需要构建步骤（回答"html 不能直接引入 json 么"）：
浏览器的 fetch()/XHR 在 file:// 协议下被 CORS 禁止（本地文件互读
是安全红线），双击打开 HTML 时无法旁加载 item_pool.json。两个解法：
  (a) 把 JSON 在构建时内联进 HTML（本脚本，推荐——单文件分发）
  (b) python -m http.server 起本地服务后 fetch（适合集中实测，不适合
      众包/邮件发放）
本脚本走 (a)：产物 papers/paperA/human_eval/dist/survey_HE.html 单文件
可直接双击或托管。

用法: python experiments/paper_ea5b_build_survey.py
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.join(os.path.dirname(__file__), "..",
                    "papers", "paperA", "human_eval")
MARKER = "/*PASTE_ITEM_POOL_JSON_HERE*/ null;"


def main() -> None:
    tpl_path = os.path.join(BASE, "survey_template.html")
    pool_path = os.path.join(BASE, "item_pool.json")
    out_dir = os.path.join(BASE, "dist")
    out_path = os.path.join(out_dir, "survey_HE.html")

    import re
    tpl = open(tpl_path, encoding="utf-8").read()
    pool = json.load(open(pool_path, encoding="utf-8"))

    if MARKER in tpl:
        # 模式 A：模板带占位符 → 注入 JSON
        inline = "const POOL = " + json.dumps(pool, ensure_ascii=False) + ";"
        html = tpl.replace("const POOL = " + MARKER, "const POOL = " + inline) \
            if ("const POOL = " + MARKER) in tpl else tpl.replace(MARKER, inline)
        mode = "注入"
    else:
        # 模式 B：模板已被内联 → 校验 seed 一致后直通
        m = re.search(r"const POOL = (\{.*?\});", tpl, re.S)
        assert m, "模板既无占位符也无内联 POOL，状态异常"
        inlined = json.loads(m.group(1))
        assert inlined.get("meta", {}).get("seed") == pool["meta"]["seed"], \
            "内联 POOL 与 item_pool.json 的 seed 不一致——请重新内联"
        html = tpl
        mode = "校验直通"

    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"模式 {mode} → 产物: {out_path} ({os.path.getsize(out_path)//1024} KB)")
    content = open(out_path, encoding="utf-8").read()
    assert '"pair_items"' in content and MARKER not in content
    print("校验: POOL 非空且占位符已清除 ✓")


if __name__ == "__main__":
    main()
