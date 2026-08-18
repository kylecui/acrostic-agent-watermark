import hashlib, hmac, math, random, sys, statistics as st
sys.path.insert(0, '/workspace/acrostic-agent-watermark/src')
from collections import defaultdict
from aawm.synonym_data import EN_SYNONYMS_RAW, EN_SYNONYMS_EXTRA, ZH_SYNONYMS_RAW

raw = {**EN_SYNONYMS_RAW, **EN_SYNONYMS_EXTRA}
key = b"test-key-2026"; N = 16

def green(w): return (hmac.new(key, w.encode(), hashlib.sha256).digest()[0] >> 7) & 1
def getband(h): return (hmac.new(key + b"|band|", h.encode(), hashlib.sha256).digest()[0]) % N
def cp_green(ch): return (hmac.new(key + b"|cp|", ch.encode('utf-8'), hashlib.sha256).digest()[0] >> 7) & 1
def byte_green(b): return (hmac.new(key + b"|byte|", bytes([b]), hashlib.sha256).digest()[0] >> 7) & 1
def big_green(bg): return (hmac.new(key + b"|big|", bg.encode('utf-8'), hashlib.sha256).digest()[0] >> 7) & 1

word_owner = {}
for h, c in raw.items():
    for w in c: word_owner.setdefault(w, h)
disjoint = defaultdict(list)
for w, h in word_owner.items(): disjoint[h].append(w)
flippable = {h: c for h, c in disjoint.items() if len(set(green(w) for w in c)) > 1}
w2band = {w: getband(h) for h, c in flippable.items() for w in c}
w2group = {w: c for h, c in flippable.items() for w in c}

rng = random.Random(42)
filler = ["system","process","value","human","between","during","though","while",
          "should","would","might","place","group","number","change","right"]
heads = list(flippable.keys())

def make_en(n):
    out = []
    for _ in range(n):
        if rng.random() < 0.35: out.append(rng.choice(flippable[rng.choice(heads)]))
        else: out.append(rng.choice(filler))
    return " ".join(out)

def embed_en(text, uid, bias=1.0):
    bits = [(uid >> i) & 1 for i in range(N)]
    out = []
    for w in text.split():
        b = w2band.get(w)
        if b is not None and rng.random() < bias:
            want = bool(bits[b])
            pool = [x for x in w2group[w] if bool(green(x)) == want]
            out.append(rng.choice(pool) if pool else w)
        else: out.append(w)
    return " ".join(out)

def para_en(text, frac, prng):
    out = []
    for w in text.split():
        c = w2group.get(w)
        if c and prng.random() < frac: out.append(prng.choice(c))
        else: out.append(w)
    return " ".join(out)

zh_groups = dict(ZH_SYNONYMS_RAW)
def zh_green(w): return (hmac.new(key + b"|zh|", w.encode(), hashlib.sha256).digest()[0] >> 7) & 1
def zh_band(h): return (hmac.new(key + b"|zhband|", h.encode(), hashlib.sha256).digest()[0]) % N
zh_owner = {}
for h, c in zh_groups.items():
    for w in c: zh_owner.setdefault(w, h)
zh_dis = defaultdict(list)
for w, h in zh_owner.items(): zh_dis[h].append(w)
zh_flip = {h: c for h, c in zh_dis.items() if len(set(zh_green(w) for w in c)) > 1}
zh_w2band = {w: zh_band(h) for h, c in zh_flip.items() for w in c}
zh_w2group = {w: c for h, c in zh_flip.items() for w in c}
zh_filler = ["我们","可以","这个","一个","进行","以及","可能","因为","所以","但是",
             "如果","还有","现在","时间","地方","东西","时候","问题","方面","情况"]
zh_heads = list(zh_flip.keys())

def make_zh(n):
    toks = []
    for _ in range(n):
        if rng.random() < 0.4: toks.append(rng.choice(zh_flip[rng.choice(zh_heads)]))
        else: toks.append(rng.choice(zh_filler))
    return "".join(toks)

def embed_zh(text, uid, bias=1.0):
    bits = [(uid >> i) & 1 for i in range(N)]
    i = 0; res = []
    while i < len(text):
        two = text[i:i+2]
        b = zh_w2band.get(two)
        if b is not None and rng.random() < bias:
            want = bool(bits[b])
            pool = [x for x in zh_w2group[two] if bool(zh_green(x)) == want]
            res.append(rng.choice(pool) if pool else two)
            i += 2
        else:
            res.append(text[i]); i += 1
    return "".join(res)

def para_zh(text, frac, prng):
    res = []; i = 0
    while i < len(text):
        two = text[i:i+2]
        c = zh_w2group.get(two)
        if c and prng.random() < frac:
            res.append(prng.choice(c)); i += 2
        else:
            res.append(text[i]); i += 1
    return "".join(res)

band_words = defaultdict(list)
for h, c in flippable.items(): band_words[getband(h)].extend(c)
p0 = {b: sum(green(w) for w in ws)/len(ws) for b, ws in band_words.items()}
def det_word(text):
    zs = []; words = text.split()
    for b in range(N):
        bw = [w for w in words if w2band.get(w) == b]
        if len(bw) < 6: zs.append(0.0); continue
        g = sum(green(w) for w in bw); n = len(bw); pp = p0[b]
        zs.append((g - pp*n)/math.sqrt(pp*(1-pp)*n))
    return max(abs(z) for z in zs)

def make_layer_detector(green_fn, unit_fn):
    p0s = []
    def calib(texts):
        for t in texts:
            units = unit_fn(t)
            if units: p0s.append(sum(1 for u in units if green_fn(u))/len(units))
    def det(text):
        pp = st.mean(p0s) if p0s else 0.5
        units = unit_fn(text); n = len(units)
        if n < 100: return 0.0
        g = sum(1 for u in units if green_fn(u))
        return abs((g - pp*n)/math.sqrt(pp*(1-pp)*n))
    return calib, det

def bigrams(t):
    cs = list(t)
    return [cs[i]+cs[i+1] for i in range(len(cs)-1)]

cal_byte, det_byte = make_layer_detector(byte_green, lambda t: list(t.encode('utf-8')))
cal_cp, det_cp = make_layer_detector(cp_green, lambda t: list(t))
cal_big, det_big = make_layer_detector(big_green, bigrams)

en_plain = [make_en(800) for _ in range(15)]
cal_byte(en_plain); cal_cp(en_plain); cal_big(en_plain)

UID = 0xABCD
res = defaultdict(list)
prng = random.Random(7)
for t in range(20):
    text = make_en(800)
    res["plain"].append((det_word(text), det_byte(text), det_cp(text), det_big(text)))
    emb = embed_en(text, UID)
    res["emb"].append((det_word(emb), det_byte(emb), det_cp(emb), det_big(emb)))
    p30 = para_en(emb, 0.3, prng)
    res["p30"].append((det_word(p30), det_byte(p30), det_cp(p30), det_big(p30)))
    p50 = para_en(emb, 0.5, prng)
    res["p50"].append((det_word(p50), det_byte(p50), det_cp(p50), det_big(p50)))

print("="*78)
print("英文 800 词（20 试，嵌入=词层16带UID=0xABCD）：四种检测层 |z| 中位数")
print("="*78)
print(f"{'状态':<7} {'①词层(语言相关)':>18} {'②字节层':>10} {'③字符层':>10} {'④bigram层':>11}")
for lbl in ["plain","emb","p30","p50"]:
    name = {"plain":"无水印","emb":"有水印","p30":"30%改写","p50":"50%改写"}[lbl]
    cols = list(zip(*res[lbl]))
    meds = [st.median(c) for c in cols]
    print(f"{name:<7} {meds[0]:>18.2f} {meds[1]:>10.2f} {meds[2]:>10.2f} {meds[3]:>11.2f}")

# FPR 检查：无水印文本 |z| 的 p95 vs 有水印 p05
cols_p = list(zip(*res["plain"])); cols_e = list(zip(*res["emb"]))
cols_3 = list(zip(*res["p30"]))
print("\n阈值分析（|z|>4 为阳性）：")
for i, nm in enumerate(["①词层","②字节","③字符","④bigram"]):
    fpr = sum(1 for v in cols_p[i] if v > 4)/20
    tpr = sum(1 for v in cols_e[i] if v > 4)/20
    tpr30 = sum(1 for v in cols_3[i] if v > 4)/20
    print(f"  {nm}: FPR={fpr:.0%}  TPR={tpr:.0%}  TPR@30%para={tpr30:.0%}")

zh_plains = [make_zh(500) for _ in range(15)]
print("\n" + "="*78)
print("中文 500 字：语言无关层的真实绿占比（跨语言 p0 漂移）")
print("="*78)
db, dc, dg = [], [], []
for t in zh_plains:
    tb = list(t.encode('utf-8'))
    db.append(sum(1 for u in tb if byte_green(u))/len(tb))
    dc.append(sum(1 for u in t if cp_green(u))/len(t))
    g = bigrams(t); dg.append(sum(1 for u in g if big_green(u))/len(g))
print(f"中文无水印: 字节={st.median(db):.4f} 字符={st.median(dc):.4f} bigram={st.median(dg):.4f}  (0.5=无漂移)")

cal_byte2, det_byte2 = make_layer_detector(byte_green, lambda t: list(t.encode('utf-8')))
cal_cp2, det_cp2 = make_layer_detector(cp_green, lambda t: list(t))
cal_big2, det_big2 = make_layer_detector(big_green, bigrams)
cal_byte2(zh_plains); cal_cp2(zh_plains); cal_big2(zh_plains)

zh_res = defaultdict(list)
for t in range(20):
    text = make_zh(500)
    zh_res["plain"].append((det_byte2(text), det_cp2(text), det_big2(text)))
    emb = embed_zh(text, UID)
    zh_res["emb"].append((det_byte2(emb), det_cp2(emb), det_big2(emb)))
    p30 = para_zh(emb, 0.3, prng)
    zh_res["p30"].append((det_byte2(p30), det_cp2(p30), det_big2(p30)))

print(f"\n中文 500 字（20 试，嵌入=中文词层16带）：三种语言无关层 |z| 中位数")
print(f"{'状态':<7} {'②字节层':>10} {'③字符层':>10} {'④bigram层':>11}")
for lbl in ["plain","emb","p30"]:
    name = {"plain":"无水印","emb":"有水印","p30":"30%改写"}[lbl]
    cols = list(zip(*zh_res[lbl]))
    meds = [st.median(c) for c in cols]
    print(f"{name:<7} {meds[0]:>10.2f} {meds[1]:>10.2f} {meds[2]:>11.2f}")

zh_cols_p = list(zip(*zh_res["plain"])); zh_cols_e = list(zip(*zh_res["emb"]))
zh_cols_3 = list(zip(*zh_res["p30"]))
print("\n中文阈值分析（|z|>4 为阳性）：")
for i, nm in enumerate(["②字节","③字符","④bigram"]):
    fpr = sum(1 for v in zh_cols_p[i] if v > 4)/20
    tpr = sum(1 for v in zh_cols_e[i] if v > 4)/20
    tpr30 = sum(1 for v in zh_cols_3[i] if v > 4)/20
    print(f"  {nm}: FPR={fpr:.0%}  TPR={tpr:.0%}  TPR@30%para={tpr30:.0%}")
