"""示例 04：v0.5 双信道签名 —— 信道 A（防篡改）× 信道 B（溯源）联合判决。

复现 docs/design.md §13.6 协同矩阵：
  场景 1 原文验证     -> A intact + B 出 UID        => 高置信归属
  场景 2 篡改一段     -> A TAMPERED(定位段) + B 出 UID => 篡改确认 + 溯源双证据
  场景 3 词典词改写   -> A 失败 + B 仍出 UID         => 已被编辑，B 单独溯源
  场景 4 无水印文本   -> B 存在性得分低               => 非本密钥体系产出

运行：python examples/04_dual_channel.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm import (  # noqa: E402
    BandReport,
    BindingSeal,
    BindingVerdict,
    DocumentBinder,
    GreenlistCodec,
    VerdictKind,
    generate_master_key,
    generate_session_salt,
)

UID = 0x1234

# UID 注册库（部署形态：溯源 = 解码值对库做汉明距最近邻匹配，而非要求逐位相等）
UID_REGISTRY = {
    0x1234: "agent-cuiyin",
    0x00FF: "agent-beta",
    0xFF00: "agent-gamma",
    0xABCD: "agent-delta",
}

PARAS = [
    # 5 段 × ~120 词，600 词量级 —— §13.3 实证该长度下 16-bit UID 往返 100%
    "The platform collects telemetry from every distributed agent working in the "
    "fleet. Each agent watches a big stream of events, keeps a small record of "
    "important changes, and builds a short summary at the end of the reporting "
    "window. A strong supervisor groups the results into a common view, so the "
    "whole system stays easy to inspect. When an agent finds a hard problem it "
    "cannot fix alone, it sends a quick alert to the central team and asks for "
    "help. The team then checks whether the issue is new or old, whether it is "
    "critical or minor, and whether a fast patch is possible without a full "
    "restart of the service.",

    "The embedder looks for good anchor positions where a quick synonym swap "
    "can carry a hidden bit without changing the meaning of the original "
    "sentence. Common adjectives and common verbs give a rich pool of possible "
    "candidates, and a strong key derivation makes the final mapping hard to "
    "predict for outsiders. The tool prefers positions inside long sentences "
    "because they offer a stable context for the hash computation. It also "
    "avoids rare words, since a rare choice would look strange to a careful "
    "reader. Every accepted anchor adds a tiny amount of evidence, and the "
    "message becomes clear only when enough anchors agree on the same answer.",

    "The verifier recomputes the same anchors with the shared key and reads "
    "the bits back into a user identifier. Minor edits only damage a small "
    "part of the message, so the identifier still survives moderate "
    "rewriting. If an attacker replaces half of the words, the signal gets "
    "weak, yet the decoder only needs the sign of every band statistic to "
    "recover the identifier, which is a much easier job than proving that a "
    "watermark exists at all. This gap between the two tasks explains why "
    "tracing survives longer than detection, and why a registry of known "
    "identifiers is more useful than a blind test in practice.",

    "A second channel binds each paragraph with a keyed hash chain. Any "
    "attempt to modify the text after signing breaks the chain and reveals "
    "exactly which paragraph was touched by the attacker. The chain also "
    "covers the order of the paragraphs, so moving a block from one place to "
    "another is visible even when the content itself stays intact. Because "
    "the construction is conservative about normalization, harmless changes "
    "such as extra spaces or line breaks never raise a false alarm, while a "
    "single altered word always does.",

    "Together the two channels form a practical signature for agent output: "
    "one channel is strict and fragile, the other is loose and robust. If "
    "both agree, the attribution is strong. If the strict channel fails but "
    "the robust one still returns the same identifier, the text was edited "
    "after signing, and the source is still known. If both fail, the text "
    "most likely never came from this key family at all. This simple "
    "decision table is the core of the design.",
]


def make_text() -> str:
    return "\n\n".join(PARAS)


def paraphrase(codec: GreenlistCodec, text: str, frac: float, seed: int) -> str:
    """把 frac 比例的词典词随机换成组内其他候选（模拟轻量改写）。"""
    rng = random.Random(seed)
    out = []
    for tok in text.split(" "):
        low = tok.lower()
        grp = codec._w2group.get(low)
        if grp is not None and rng.random() < frac:
            alts = [x for x in grp if x != low]
            if alts:
                c = rng.choice(alts)
                tok = c.capitalize() if tok[:1].isupper() else c
        out.append(tok)
    return " ".join(out)


def band_line(rep: BandReport) -> str:
    zs = " ".join(f"{st.z:+5.1f}" for st in rep.bands)
    return f"    逐带 z: [{zs}]  Σ|z|={rep.existence_score:.1f}"


def main() -> None:
    master = generate_master_key()
    salt = generate_session_salt()

    codec = GreenlistCodec(master, salt)
    binder = DocumentBinder(master, salt)

    print(f"词典管线：{codec.stats}")
    print()

    # ---- 签发：B 嵌入 UID -> A 对嵌入后文本签名（AAD 绑定 UID 声明） ----
    original = make_text()
    marked = codec.embed(original, UID, bias=1.0, rng=random.Random(1))
    seal = binder.sign(marked, aad=UID.to_bytes(2, "big"))

    print(f"UID = 0x{UID:04X}，嵌入后签署。段落 seal：{len(seal.para_hashes)} 段")
    print("=" * 72)

    # ---- 场景 1：原文验证 ----
    v = binder.verify(marked, seal)
    rep = codec.detect(marked)
    print("[场景 1] 原文验证")
    print(f"    A: {v.kind.value:10s} root_match={v.root_match}")
    print(f"    B: uid=0x{rep.uid:04X} (期望 0x{UID:04X})  词典词 {rep.n_dict_words}")
    print(band_line(rep))
    print(f"    => 判决: {'高置信归属 0x%04X' % rep.uid if v.ok and rep.uid == UID else '异常'}")
    print()

    # ---- 场景 2：篡改一段 ----
    # 注意目标选 B 不会替换的非词典词（reporting/window/quarter 都不在词典），
    # 确保 replace 一定落在嵌入后的文本上
    paras = marked.split("\n\n")
    assert "reporting window" in paras[0], "篡改目标必须存在于嵌入后文本"
    paras[0] = paras[0].replace(
        "at the end of the reporting window", "at the beginning of the reporting quarter"
    )
    tampered = "\n\n".join(paras)
    v2 = binder.verify(tampered, seal)
    rep2 = codec.detect(tampered)
    print("[场景 2] 篡改第 3 段（语义反转攻击）")
    print(f"    A: {v2.kind.value:10s} 被改段索引={v2.mismatched_indices}")
    print(f"    B: uid=0x{rep2.uid:04X}")
    print(band_line(rep2))
    print(f"    => 判决: {'篡改确认 + 溯源归属 0x%04X 双证据' % rep2.uid if not v2.ok and rep2.uid == UID else '见上'}")
    print()

    # ---- 场景 3：词典词改写（paraphrase） ----
    rewritten = paraphrase(codec, marked, frac=0.30, seed=9)
    v3 = binder.verify(rewritten, seal)
    rep3 = codec.detect(rewritten)
    ham = bin(rep3.uid ^ UID).count("1")
    # 注册库最近邻匹配（§13.3 部署策略：解码鲁棒性 > 存在性鲁棒性）
    best_uid, best_dist = min(
        ((u, bin(rep3.uid ^ u).count("1")) for u in UID_REGISTRY),
        key=lambda t: t[1],
    )
    print("[场景 3] 30% 词典词改写")
    print(f"    A: {v3.kind.value:10s}（段落内容已变）")
    print(f"    B: uid=0x{rep3.uid:04X}  对真值汉明距={ham}")
    print(band_line(rep3))
    print(f"    => 判决: 已被改写；注册库最近邻 0x{best_uid:04X}"
          f"({UID_REGISTRY[best_uid]}) 距离={best_dist}"
          f" -> {'匹配成功' if best_uid == UID else '匹配失败'}"
          f"（自然语料词典率 18% < 实验模拟 35%，逐位解码降级为近邻匹配）")
    print()

    # ---- 场景 4：无水印文本 ----
    plain = make_text()  # 未嵌入
    rep4 = codec.detect(plain)
    print("[场景 4] 无水印对照文本")
    print(f"    B: uid=0x{rep4.uid:04X}（随机游走值）")
    print(band_line(rep4))
    print(f"    => 判决: 存在性得分 {rep4.existence_score:.1f} 显著低于场景 1 的 "
          f"{rep.existence_score:.1f}；部署时用 UID 注册库匹配而非盲检")
    print("=" * 72)

    # ---- 内积形式 ----
    dot_true = codec.dot_score(marked, UID)
    dot_fake = codec.dot_score(marked, 0xFFFF ^ UID)
    print(f"⟨v(text), τ_uid⟩ = {dot_true:+.1f}（真 UID） vs {dot_fake:+.1f}（反相 UID）")


if __name__ == "__main__":
    main()
