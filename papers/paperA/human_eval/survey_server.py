#!/usr/bin/env python3
"""survey_server.py: 盲评收集+汇总一体化服务（仅标准库）。

部署: HE_ADMIN_TOKEN=<随机串> nohup python3 survey_server.py 8791 &
端点:
  GET  /            测评页（survey_HE.html，题库已内联）
  POST /submit      参与者提交（校验 pid 格式、大小限 256KB、防覆盖）
  GET  /admin?token 汇总统计（pair 二项 / spot 检出-误报 / Likert；含剔除标记）
隐私: responses/ 仅存服务器本地，不进任何仓库；pid 由参与者自填，无身份字段。
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
RESP_DIR = os.path.join(ROOT, "responses")
SURVEY = os.path.join(ROOT, "dist", "survey_HE.html")
ADMIN_TOKEN = os.environ.get("HE_ADMIN_TOKEN", "dev-token-change-me")
PID_RE = re.compile(r"^(HE-\d{3}|N-\d{3})$")
MAX_BODY = 256 * 1024


def aggregate():
    files = sorted(os.listdir(RESP_DIR)) if os.path.isdir(RESP_DIR) else []
    pair_hits = pair_n = det_hits = det_n = fa_hits = fa_n = 0
    likert_vals = []
    rows = []
    excluded = []
    for fn in files:
        try:
            r = json.load(open(os.path.join(RESP_DIR, fn), encoding="utf-8"))
        except Exception:
            continue
        pid = r.get("pid", fn)
        sp = r.get("spots", {})
        orig_ids = [k for k in sp if str(k).startswith("o")]
        fa_r = sum(1 for k in orig_ids if sp[k].get("a") == "yes") / max(1, len(orig_ids))
        try:
            from datetime import datetime
            mins = ((datetime.fromisoformat(r["finished_at"].replace("Z", "+00:00"))
                     - datetime.fromisoformat(r["started_at"].replace("Z", "+00:00")))
                    .total_seconds() / 60)
        except Exception:
            mins = 99.0
        bad = fa_r > 0.9 or fa_r < 0.05 or mins < 5
        if bad:
            excluded.append({"pid": pid, "fa_rate": round(fa_r, 3),
                             "minutes": round(mins, 1)})
            continue
        for rec in r.get("pairs", {}).values():
            pair_n += 1
            if isinstance(rec, dict) and rec.get("c") == rec.get("m"):
                pair_hits += 1
        for k, v in sp.items():
            a = v.get("a")
            if str(k).startswith("o"):
                fa_n += 1
                fa_hits += (a == "yes")
            else:
                det_n += 1
                det_hits += (a == "yes")
        likert_vals += [v for v in r.get("likerts", {}).values()
                        if isinstance(v, int)]
        rows.append({"pid": pid, "minutes": round(mins, 1),
                     "pairs": len(r.get("pairs", {})),
                     "spots": len(sp), "likerts": len(r.get("likerts", {}))})

    def binom_p(k, n, p0=0.5):
        if n == 0:
            return 1.0
        mu, sd = n * p0, math.sqrt(n * p0 * (1 - p0))
        z = (k - 0.5 - mu) / sd if sd else 0
        return 0.5 * math.erfc(z / math.sqrt(2))

    return {
        "n_included": len(rows), "n_excluded": len(excluded),
        "excluded": excluded, "responses": rows,
        "pair": {"n": pair_n, "marked_preferred": pair_hits,
                 "rate": round(pair_hits / max(1, pair_n), 4),
                 "binom_p_gt_50pct": round(binom_p(pair_hits, pair_n), 4)},
        "spot": {"detection_rate": round(det_hits / max(1, det_n), 4),
                 "false_alarm_rate": round(fa_hits / max(1, fa_n), 4),
                 "n_marked": det_n, "n_orig": fa_n},
        "likert": {"n": len(likert_vals),
                   "mean": round(statistics.mean(likert_vals), 3) if likert_vals else None,
                   "median": statistics.median(likert_vals) if likert_vals else None},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def render_admin(a):
    esc = (lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;"))
    rows = "".join(
        f"<tr><td>{esc(r['pid'])}</td><td>{r['minutes']}</td>"
        f"<td>{r['pairs']}</td><td>{r['spots']}</td><td>{r['likerts']}</td></tr>"
        for r in a["responses"])
    exc = "".join(f"<li>{esc(e['pid'])} fa={e['fa_rate']} {e['minutes']}min</li>"
                  for e in a["excluded"])
    pr = a["pair"]["rate"]
    dr = a["spot"]["detection_rate"] - a["spot"]["false_alarm_rate"]
    lm = a["likert"]["mean"]
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>盲评汇总</title><style>body{{font-family:system-ui;max-width:760px;margin:24px auto;padding:0 16px}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:4px 10px}}
.kpi{{font-size:20px;font-weight:600}}</style></head><body>
<h1>盲评汇总（{a['generated_at']}）</h1>
<p>有效 {a['n_included']} 人 / 剔除 {a['n_excluded']} 人</p>
<h2>预注册判据对照</h2>
<ul>
<li>pair 水印侧更自然率: <span class="kpi">{pr}</span>
（不可察觉=0.50，binom p={a['pair']['binom_p_gt_50pct']}，n={a['pair']['n']}）</li>
<li>spot 检出-误报差: <span class="kpi">{dr:.4f}</span>
（检出 {a['spot']['detection_rate']} / 误报 {a['spot']['false_alarm_rate']}，&lt;0.10 不可察觉）</li>
<li>Likert 均分: <span class="kpi">{lm}</span>（≥4.0 自然，n={a['likert']['n']}）</li>
</ul>
<h2>回收明细</h2>
<table><tr><th>pid</th><th>用时(min)</th><th>pair</th><th>spot</th><th>likert</th></tr>{rows}</table>
<h2>剔除名单（规则见 PROTOCOL §3）</h2>
<ul>{exc or '<li>无</li>'}</ul>
<p><a href="/admin?token={ADMIN_TOKEN}&amp;json=1">JSON 原始数据</a></p>
</body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), a[0] % a[1:]))

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            if not os.path.exists(SURVEY):
                return self._send(500, "survey_HE.html 缺失：先跑 paper_ea5b_build_survey.py")
            return self._send(200, open(SURVEY, encoding="utf-8").read())
        if u.path == "/admin":
            q = urllib.parse.parse_qs(u.query)
            if q.get("token", [""])[0] != ADMIN_TOKEN:
                return self._send(403, "forbidden")
            a = aggregate()
            if q.get("json"):
                return self._send(200, json.dumps(a, ensure_ascii=False),
                                  "application/json; charset=utf-8")
            return self._send(200, render_admin(a))
        return self._send(404, "not found")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/submit":
            return self._send(404, "not found")
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0 or n > MAX_BODY:
            return self._send(400, json.dumps({"ok": False, "err": "bad size"}),
                              "application/json")
        try:
            r = json.loads(self.rfile.read(n))
        except Exception:
            return self._send(400, json.dumps({"ok": False, "err": "bad json"}),
                              "application/json")
        pid = str(r.get("pid", ""))
        if not PID_RE.match(pid):
            return self._send(400, json.dumps({"ok": False, "err": "bad pid"}),
                              "application/json")
        if not (r.get("pairs") and r.get("spots") and r.get("likerts")):
            return self._send(400, json.dumps({"ok": False, "err": "incomplete"}),
                              "application/json")
        os.makedirs(RESP_DIR, exist_ok=True)
        path = os.path.join(RESP_DIR, f"{pid}-{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False)
        return self._send(200, json.dumps({"ok": True}), "application/json")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    print(f"survey server on :{port}  admin token: {ADMIN_TOKEN[:4]}…")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
