#!/usr/bin/env python3
"""exp_weighted_detect.py: 检测端加权 z —— 按组内颜色构成加权，救回 s50 同义替换失守文档。

动机（design §13.x）：
  攻击（同组随机替换）翻转 token 颜色的概率 = f·(1 − q_c)，其中
  q_c = token 所在组内"与其同色"的词占比。这是词典+密钥的静态属性，
  与文本内容无关。q_c 高的 token 攻击后大概率保持颜色（证据稳定），
  q_c≈0.5 的 token 证据中性（攻击后随机），q_c 低的 token 证据反向。

  现状 z = (g − p0·n)/√(p0(1−p0)n) 等权累计——脆弱 token 与稳定 token
  一视同仁，攻击后噪声被等权放大，s50 失守 11/30（ZH D3r）。

  加权 z = (Σ w·green − p0w·Σ w)/√(p0w(1−p0w)·Σ w²)，权重变体：
    v0 等权        w = 1
    v1 有符号      w = 2·q_c − 1        （攻击后保持色的净期望贡献）
    v2 正权重剪裁  w = max(2·q_c − 1, 0)（剔除中性/反向 token）
    v3 比例        w = q_c

对照：ZH D3r 生产词典，30 篇 PAWS 拼接文档，s30/s50/s70 攻击谱，
四权重变体下 soft_match 存活率 + 逐篇诊断。

用法: python experiments/exp_weighted_detect.py
"""
from __future__ import annotations

import random
import sys
from collections import Counter

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from aawm.synonym_data import ZH_SYNONYMS_RAW
from dict_build import build_cilin_dict
from exp_paws_attack import KEY, SALT, N_SENT, N_DOCS, load_paws_positive
from exp_real_corpus import synonym_attack

N_TEST = N_DOCS // 2


# ---------------------------------------------------------------------------
# 数据与 codec（与 exp_dict_expansion.zh_section 的 D3r 完全一致）
# ---------------------------------------------------------------------------
def build_d3r():
    paws = load_paws_positive()
    base = GreenlistCodec(KEY, SALT, language_tag=b"zh")

    def n_dict(s):
        return sum(1 for _, n in base._tokenizer(s) if n and n in base._w2group)

    kept = [p for p in paws if n_dict(p[0]) >= 2]
    rng = random.Random(7)
    rng.shuffle(kept)
    docs = [" ".join(kept[i * N_SENT:(i + 1) * N_SENT][j][0] for j in range(N_SENT)) + " "
            for i in range(N_DOCS)]
    test_docs, null_docs = docs[:N_TEST], docs[N_TEST:]

    raw_equal = build_cilin_dict("corpus/dict/cilin_extended.txt")
    merged = dict(ZH_SYNONYMS_RAW)
    used = {w for ws in ZH_SYNONYMS_RAW.values() for w in ws}
    for k, ws in raw_equal.items():
        ws2 = [w for w in ws if w not in used]
        if len(ws2) >= 2:
            merged[k] = ws2
            used.update(ws2)
    codec = GreenlistCodec(KEY, SALT, dictionary=merged, language_tag=b"zh")
    codec.calibrate_p0(docs[N_TEST:])
    return codec, test_docs, null_docs


# ---------------------------------------------------------------------------
# 加权检测：权重是"词 -> w"的静态表（词典+密钥派生，与文本无关）
# ---------------------------------------------------------------------------
def build_weight_tables(codec):
    """对每个词计算三种权重。"""
    w1, w2, w3 = {}, {}, {}
    for head, members in codec._groups.items():
        n = len(members)
        q = {g: sum(1 for w in members if codec.green(w) == g) / n
             for g in (0, 1)}
        for w in members:
            c = codec.green(w)
            qc = q[c]
            w1[w] = 2 * qc - 1
            w2[w] = max(2 * qc - 1, 0.0)
            w3[w] = qc
    return w1, w2, w3


def weighted_z_per_band(codec, text, wtab, min_n=1, p0w=None):
    """加权逐带 z。p0w: {band: 加权 p0}，None 时用 0.5。"""
    sw = [0.0] * codec.n_bands
    sg = [0.0] * codec.n_bands
    sw2 = [0.0] * codec.n_bands
    for _raw, norm in codec._tokenizer(text):
        b = codec._w2band.get(norm)
        if b is None:
            continue
        wt = wtab.get(norm, 0.0)
        if wt == 0.0:
            continue
        sw[b] += wt
        sg[b] += wt * codec.green(norm)
        sw2[b] += wt * wt
    z = {}
    for b in range(codec.n_bands):
        n, g, s2 = sw[b], sg[b], sw2[b]
        if n < min_n:
            continue
        p0 = (p0w or {}).get(b, 0.5)
        var = p0 * (1 - p0) * s2
        z[b] = (g - p0 * n) / (var ** 0.5) if var > 0 else 0.0
    return z


def calibrate_p0w(codec, null_docs, wtab):
    """在 null 语料上标定逐带加权 p0。"""
    sw, sg = [0.0] * codec.n_bands, [0.0] * codec.n_bands
    for text in null_docs:
        for _raw, norm in codec._tokenizer(text):
            b = codec._w2band.get(norm)
            if b is None:
                continue
            wt = wtab.get(norm, 0.0)
            if wt == 0.0:
                continue
            sw[b] += wt
            sg[b] += wt * codec.green(norm)
    p = 1.0
    return {b: (sg[b] + p) / (sw[b] + 2 * p) for b in range(codec.n_bands) if sw[b] > 0}


def soft_match_w(z_by_band, candidates):
    """用加权 z 做软判决，返回 (best_uid, best_score, gap)。"""
    cands = sorted(set(candidates))
    scored = sorted(
        ((sum(z * (1 if ((c >> b) & 1) else -1) for b, z in z_by_band.items()), c)
         for c in cands),
        key=lambda x: x[0], reverse=True,
    )
    best_score, best_uid = scored[0]
    second = scored[1][0] if len(scored) > 1 else best_score - 1e9
    return best_uid, best_score, best_score - second


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    codec, test_docs, null_docs = build_d3r()
    true_uids = [(0x1000 + i * 0x0111) & 0xFFFF for i in range(N_TEST)]
    candidates = sorted(set(range(1, 33)) | set(true_uids))

    nw = len({w for ws in codec._groups.values() for w in ws})
    print(f"D3r 词典: {len(codec._groups)} 组 / {nw} 词")

    w1, w2, w3 = build_weight_tables(codec)
    tables = {"v0 等权": None, "v1 有符号": w1, "v2 正剪裁": w2, "v3 比例": w3}
    p0w = {name: (None if name == "v0 等权" else calibrate_p0w(codec, null_docs, wt))
           for name, wt in tables.items()}

    # 权重分布画像
    ws = list(w1.values())
    print(f"\n权重分布 (v1 有符号): 均值={sum(ws)/len(ws):.3f} "
          f"|w|>=0.4 占比={sum(1 for x in ws if abs(x) >= 0.4)/len(ws)*100:.0f}% "
          f"w<=0 占比={sum(1 for x in ws if x <= 0)/len(ws)*100:.0f}%")

    print(f"\n{'攻击':5s} | {'v0':>4s} | {'v1':>4s} | {'v2':>4s} | {'v3':>4s} "
          f"| 汉明均值(v0/v1/v2/v3)")
    for frac in (0.30, 0.50, 0.70):
        ok = {name: 0 for name in tables}
        ham = {name: [] for name in tables}
        lost_v0, saved = [], []
        for i, doc in enumerate(test_docs):
            uid = true_uids[i]
            marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
            rw, _ = synonym_attack(codec, marked, frac, 100 + i)
            # v0：现有检测管线（detect + soft 打分）
            rep = codec.detect(rw, min_n=1)
            z_v0 = {st.band: st.z for st in rep.bands if st.has_signal}
            best_v0, *_ = soft_match_w(z_v0, candidates)
            if best_v0 == uid:
                ok["v0 等权"] += 1
            else:
                lost_v0.append(i)
            # 加权变体
            for name in ("v1 有符号", "v2 正剪裁", "v3 比例"):
                z = weighted_z_per_band(codec, rw, tables[name], 1, p0w[name])
                best, *_ = soft_match_w(z, candidates)
                if best == uid:
                    ok[name] += 1
                    if name != "v0 等权" and best_v0 != uid:
                        saved.append((i, name))
            for name, wt in tables.items():
                z = (None if wt is None else weighted_z_per_band(codec, rw, wt, 1, p0w[name]))
                if wt is None:
                    ham[name].append(bin(rep.uid ^ uid).count("1"))
                else:
                    best, *_ = soft_match_w(z, candidates)
                    ham[name].append(bin(best ^ uid).count("1"))
        hm = " / ".join(f"{sum(h)/len(h):.2f}" for h in ham.values())
        tag = f"s{int(frac*100)}"
        print(f"{tag:5s} | "
              + " | ".join(f"{ok[n]:2d}" for n in tables)
              + f" | {hm}")
        if lost_v0:
            print(f"   v0 失守 {len(lost_v0)} 篇: {lost_v0}；v1/v2 救回: {saved}")

    # 逐篇诊断 s50：v0 vs v2 的 z 对比（挑前 6 篇 v0 失守文档）
    print("\n===== s50 失守文档逐带诊断（v0 vs v2 正剪裁）=====")
    print(f"{'篇':>2s} | {'n_dict':>6s} | {'active':>6s} | "
          f"{'翻转带(v0)':>10s} | v0 Σ|z| | v2 Σ|z|")
    shown = 0
    for i, doc in enumerate(test_docs):
        uid = true_uids[i]
        marked = codec.embed(doc, uid, bias=1.0, rng=random.Random(i))
        rw, _ = synonym_attack(codec, marked, 0.50, 100 + i)
        rep = codec.detect(rw, min_n=1)
        z0 = {st.band: st.z for st in rep.bands if st.has_signal}
        best0, *_ = soft_match_w(z0, candidates)
        if best0 == uid or shown >= 8:
            continue
        z2 = weighted_z_per_band(codec, rw, w2, 1, p0w["v2 正剪裁"])
        flipped = [b for b, z in z0.items()
                   if (((uid >> b) & 1) == 1) != (z > 0)]
        n_dict = codec.detect(rw).n_dict_words
        s0 = sum(abs(v) for v in z0.values())
        s2 = sum(abs(v) for v in z2.values())
        print(f"{i:2d} | {n_dict:6d} | {len(z0):6d} | {str(flipped):>10s} | "
              f"{s0:7.2f} | {s2:6.2f}")
        shown += 1


if __name__ == "__main__":
    main()
