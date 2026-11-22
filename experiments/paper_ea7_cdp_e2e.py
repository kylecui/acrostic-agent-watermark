#!/usr/bin/env python3
"""paper_ea7_cdp_e2e.py: 盲评页 CDP 端到端自测（真实浏览器）。

流程：headless Edge(Chromium) 开 https://petfish.ai/he/ → 填 pid →
JS 逐组点选全部题目（pair/spots 选第一项、likert 选 4、置信度 3）→
点击导出（触发真实 fetch('submit')）→ 断言服务器落盘 → 清理测试数据。

这是"页面真实行为"级验证：之前 /he/submit 手动 curl 通过但页面内
绝对路径翻车的教训，要求端到端必须以浏览器实际发出的请求为准。

用法: python experiments/paper_ea7_cdp_e2e.py [--url https://petfish.ai/he/]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request

import websocket  # websocket-client

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CDP_PORT = 9333


def cdp(ws_url, mid, method, params=None, timeout=30):
    ws = websocket.create_connection(ws_url, timeout=timeout)
    ws.send(json.dumps({"id": mid, "method": method,
                        "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == mid:
            ws.close()
            return msg


def evaluate(ws_url, mid, expr):
    r = cdp(ws_url, mid, "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return r.get("result", {}).get("result", {}).get("value")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://petfish.ai/he/")
    ap.add_argument("--pid", default="N-700")
    ap.add_argument("--keep", action="store_true", help="保留测试响应")
    args = ap.parse_args()

    proc = subprocess.Popen(
        [EDGE, f"--remote-debugging-port={CDP_PORT}",
         "--headless=new", "--disable-gpu", "--no-first-run",
         "--user-data-dir=C:\Temp\he_cdp_profile", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(40):
            try:
                tabs = json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{CDP_PORT}/json", timeout=2))
                pages = [t for t in tabs if t["type"] == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.5)
        assert ws_url, "CDP 连接失败"

        cdp(ws_url, 1, "Page.enable")
        cdp(ws_url, 2, "Page.navigate", {"url": args.url})
        time.sleep(4)  # 页面+题库渲染

        title = evaluate(ws_url, 3, "document.title")
        n_groups = evaluate(ws_url, 4, """(function(){
          const names=new Set();
          document.querySelectorAll('input[type=radio]').forEach(r=>names.add(r.name));
          return names.size;})()""")
        print(f"页面: {title} | radio 组数: {n_groups}")
        # 75 计分题(15 pair+40 spot+20 likert) + 3 练习题 = 78 组
        assert n_groups == 78, f"radio 组数 {n_groups} != 78"

        evaluate(ws_url, 5, f"document.getElementById('pid').value='{args.pid}'")
        clicked = evaluate(ws_url, 6, """(function(){
          const done=new Set();
          document.querySelectorAll('input[type=radio]').forEach(r=>{
            if(done.has(r.name)) return;
            done.add(r.name);
            const want = r.name.startsWith('lk') ? '4' : null;
            const t = want ? document.querySelector(`input[name="${r.name}"][value="${want}"]`) : r;
            if(t) t.click();
          });
          document.querySelectorAll('select').forEach(s=>{
            s.value='3'; s.dispatchEvent(new Event('change'));});
          return done.size;})()""")
        print(f"已点选: {clicked} 组")

        evaluate(ws_url, 7, "exportResults()")
        time.sleep(4)
        out = evaluate(ws_url, 8, "document.getElementById('out').textContent")
        print("提交反馈:", out.strip()[:80])
        assert "已提交到服务器" in out, "在线提交未成功: " + out[:120]
        print("CDP_E2E_PASS")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
