# 判别实验：字节层信号是"密钥信号"还是"换词风格伪影"
import hashlib, hmac, math, random, sys, statistics as st
sys.path.insert(0, '/workspace/acrostic-agent-watermark/src')
from collections import defaultdict
from aawm.synonym_data import EN_SYNONYMS_RAW, EN_SYNONYMS_EXTRA

raw = {**EN_SYNONYMS_RAW, **EN_SYNONYMS_EXTRA}
key = b"test-key-2026"; N = 16

def green(w): return (hmac.new(key, w.encode(), hashlib.sha256).digest()[0] >> 7) & 1
def getband(h): return (hmac.new(key + b"|band|", h.encode(), hashlib.sha256).digest()[0]) % N
def byte_green_k(k, b): return (hmac.new(k + b"|byte|", bytes([b]), hashlib.sha256).digest()[0] >> 7) & 1

word_owner = {}
for h, c in raw.items():
    for w in c: word_owner.setdefault(w, h)
disjoint = defaultdict(list)
for w, h in word_owner.items(): disjoint[h].append(w)
flippable = {h: c for h, c in disjoint.items() if len(set(green(w) for w in c)) > 1}
w2band = {w: getband(h) for h, c in flippable.items() for w in c}
w2group = {w: c for h, c in flippable.items() for w in c}

# ===== 诊断 1：绿词半区 vs 红词半区的字节绿率 =====
all_words = [w for c in flippable.values() for w in c]
green_words = [w for w in all_words if green(w) == 1]
red_words = [w for w in all_words if green(w) == 0]
def words_byte_rate(ws, k):
    bs = [b for w in ws for b in w.encode('utf-8')]
    return sum(1 for b in bs if byte_green_k(k, b)) / len(bs)
print("诊断1：词典两半的字节绿率（key 相同）")
for kname, k in [("嵌入key", key), ("错误key", b"WRONG-KEY")]:
    rg = words_byte_rate(green_words, k); rr = words_byte_rate(red_words, k)
    print(f"  {kname}: 绿词半区={rg:.4f}  红词半区={rr:.4f}  差={rg-rr:+.4f}")

# 词长分布
lg = sum(len(w) for w in green_words)/len(green_words)
lr = sum(len(w) for w in red_words)/len(red_words)
print(f"  平均词长: 绿半区={lg:.2f}字节 红半区={lr:.2f}字节")

# ===== 诊断 2：UID 平衡性 + 错误 key 检测 =====
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

def byte_z(text, k, p0):
    bs = list(text.encode('utf-8')); n = len(bs)
    g = sum(1 for b in bs if byte_green_k(k, b))
    return (g - p0*n)/math.sqrt(p0*(1-p0)*n)

# 标定两个 key 的 p0
calib = [make_en(800) for _ in range(15)]
P0_own = st.mean([sum(1 for b in t.encode() if byte_green_k(key, b))/len(t.encode()) for t in calib])
P0_wrong = st.mean([sum(1 for b in t.encode() if byte_green_k(b"WRONG-KEY", b))/len(t.encode()) for t in calib])
print(f"\n诊断2：p0 标定  自有key={P0_own:.4f}  错误key={P0_wrong:.4f}")

print(f"\n{'UID':>8} {'z(正确key)':>12} {'z(错误key)':>12} {'词层绿净偏':>12}")
for uid in [0x0000, 0xFFFF, 0x5555, 0xAAAA, 0xABCD, 0x1234]:
    zs_own, zs_wrong, net = [], [], []
    for t in range(10):
        text = make_en(800)
        emb = embed_en(text, uid)
        zs_own.append(byte_z(emb, key, P0_own))
        zs_wrong.append(byte_z(emb, b"WRONG-KEY", P0_wrong))
        # 词层净偏：被换词的绿占比 - 0.5
        dw = [w for w in emb.split() if w2band.get(w) is not None]
        net.append(sum(green(w) for w in dw)/len(dw) - 0.5)
    print(f"0x{uid:04X} {st.median(zs_own):>+12.2f} {st.median(zs_wrong):>+12.2f} {st.median(net):>+12.3f}")

# ===== 诊断 3：随机改写（无密钥的攻击者同义替换）后的字节层 z =====
def para_en(text, frac, prng):
    out = []
    for w in text.split():
        c = w2group.get(w)
        if c and prng.random() < frac: out.append(prng.choice(c))
        else: out.append(w)
    return " ".join(out)
# 无水印文本被第三方"随机同义改写"后，我们的字节层检测会误报吗？
prng = random.Random(3)
print(f"\n诊断3：无水印文本 + 第三方随机改写 → 自有 key 字节层 z")
for frac in [0.0, 0.3, 1.0]:
    zs = []
    for t in range(10):
        text = make_en(800)
        para = para_en(text, frac, prng)
        zs.append(byte_z(para, key, P0_own))
    print(f"  改写率 {frac:.0%}: median z = {st.median(zs):+.2f}")
