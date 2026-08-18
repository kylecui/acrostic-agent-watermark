"""编辑攻击对比评测：v0.2 位置索引锚点 vs v0.3 内容寻址锚点。

攻击模型（模拟被动编辑/轻度改写，非定向水印移除）：
- insert(n)：随机位置插入 n 个常用副词/连接词
- delete(n)：随机删除 n 个词
- flip(n)：随机把 n 个词典词换成同义词
- mix(n)：n 次随机插入/删除/翻转各 1/3
- paraphrase_sentence(frac, rng)：按句切分，随机选 frac 比例句子做
  同义组内全词替换 + 若干插删 + 词序轻微打乱（v0.4 新增）

运行：python experiments/exp_edit_attacks.py
"""
from __future__ import annotations

import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm import (  # noqa: E402
    CADecoder,
    CAEmbedder,
    CAConfig,
    Decoder,
    Embedder,
    generate_master_key,
)
from aawm.embedder import _SYNONYMS  # noqa: E402

TEXT = (
    "The team released a new version of the platform this week. "
    "The company said the update will help every customer who uses the service. "
    "Our goal is simple: we want to make the product fast, clear, and useful for each user. "
    "The new framework allows people to build complex features in a short time. "
    "Developers can test every part of the system before the final release. "
    "If the team finds an issue, they can fix the problem quickly and send the update to every user. "
    "The report shows strong results this quarter. "
    "Sales grew at a rapid pace, and the customer base expanded into new areas. "
    "The analysis suggests the modern design was a key reason for the growth. "
    "People really like the clear layout and the quick response time. "
    "Our plan for the future is to improve the core system and add more tools. "
    "We will keep the price low, because we want the service to stay affordable for small teams. "
    "The company believes this approach will create real value for every customer. "
    "Security is a major focus of the new version. "
    "The team will check every request and remove any risk before it can cause harm. "
    "Users can change their settings and choose the level of protection they need. "
    "If a question comes up, our support team will answer fast and explain every detail. "
    "This project is important for the whole company. "
    "It shows our team can solve hard problems and deliver quality work. "
    "We expect the platform to become the standard tool for teams that value speed and simplicity."
)

INSERT_WORDS = [
    "also", "just", "really", "quite", "often", "perhaps", "maybe", "simply",
    "actually", "certainly", "largely", "mostly", "namely", "overall", "roughly",
]

UID = 1001
N_TRIALS = 30


def insert_attack(text: str, n: int, rng: random.Random) -> str:
    words = text.split(" ")
    for _ in range(n):
        i = rng.randrange(len(words) + 1)
        words.insert(i, rng.choice(INSERT_WORDS))
    return " ".join(words)


def delete_attack(text: str, n: int, rng: random.Random) -> str:
    words = [w for w in text.split(" ") if w]
    for _ in range(n):
        if len(words) <= 10:
            break
        del words[rng.randrange(len(words))]
    return " ".join(words)


def flip_attack(text: str, n: int, rng: random.Random) -> str:
    words = text.split(" ")
    cands = [
        i for i, w in enumerate(words)
        if w.lower().strip(".,!?;:\"'()[]") in _SYNONYMS
    ]
    rng.shuffle(cands)
    done = 0
    for i in cands:
        if done >= n:
            break
        key = words[i].lower().strip(".,!?;:\"'()[]")
        grp = [c for c in _SYNONYMS[key] if c != key]
        if not grp:
            continue
        nw = rng.choice(grp)
        if words[i][:1].isupper():
            nw = nw[:1].upper() + nw[1:]
        words[i] = nw
        done += 1
    return " ".join(words)


def mix_attack(text: str, n: int, rng: random.Random) -> str:
    for _ in range(n):
        op = rng.random()
        if op < 1 / 3:
            text = insert_attack(text, 1, rng)
        elif op < 2 / 3:
            text = delete_attack(text, 1, rng)
        else:
            text = flip_attack(text, 1, rng)
    return text


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def paraphrase_sentence_attack(
    text: str, frac: float, rng: random.Random
) -> str:
    """模拟保守的整句重写（paraphrase）。

    按句切分，随机选 frac 比例的句子，每句做：
    - 组内同义词全词替换（词典词换成同组词）
    - 随机插入 1-2 个副词
    - 随机删除 1 个非词典词
    - 词序轻微打乱（交换相邻非词典词对，≤2 次）

    这模拟"不改变句意但改变用词和顺序"的保守重写——既不是全文重写
    （理论边界，零依赖无法抵抗），也不是单纯编辑。v0.4 句子边界感知
    指纹应使被改句只损失该句的票，邻句锚点存活。
    """
    sentences = _SENT_SPLIT_RE.split(text)
    n_attack = max(1, int(len(sentences) * frac))
    indices = rng.sample(range(len(sentences)), min(n_attack, len(sentences)))

    out = []
    for i, sent in enumerate(sentences):
        if i not in indices:
            out.append(sent)
            continue
        out.append(_rewrite_sentence(sent, rng))
    return " ".join(out)


def _rewrite_sentence(sent: str, rng: random.Random) -> str:
    """对单句做保守重写：同义替换 + 轻微插删 + 词序微调。

    强度分级：
    - 同义全替换：词典词全换成同组词（必做，模拟"换说法"）
    - 插删：每个句子 0-1 次插入、0-1 次删除（轻度，不改句意）
    - 词序微调：交换 1 对相邻非词典词（可选）
    """
    words = sent.split(" ")
    words = [w for w in words if w]

    # 1) 组内同义词全词替换（必做）
    for i, w in enumerate(words):
        key = w.lower().strip(".,!?;:\"'()[]")
        if key in _SYNONYMS:
            grp = [c for c in _SYNONYMS[key] if c != key]
            if grp:
                nw = rng.choice(grp)
                if w[:1].isupper():
                    nw = nw[:1].upper() + nw[1:]
                words[i] = nw

    # 2) 随机插入 0-1 个副词（轻度）
    if rng.random() < 0.5:
        pos = rng.randrange(len(words) + 1)
        words.insert(pos, rng.choice(INSERT_WORDS))

    # 3) 随机删除 0-1 个非词典词（轻度）
    if rng.random() < 0.5 and len(words) > 6:
        non_dict = [
            i for i, w in enumerate(words)
            if w.lower().strip(".,!?;:\"'()[]") not in _SYNONYMS
        ]
        if non_dict:
            del words[rng.choice(non_dict)]

    # 4) 词序微调：交换 1 对相邻非词典词（可选）
    if rng.random() < 0.3 and len(words) > 4:
        for _ in range(3):  # 找一对可交换的
            i = rng.randrange(len(words) - 1)
            w1 = words[i].lower().strip(".,!?;:\"'()[]")
            w2 = words[i + 1].lower().strip(".,!?;:\"'()[]")
            if w1 not in _SYNONYMS and w2 not in _SYNONYMS:
                words[i], words[i + 1] = words[i + 1], words[i]
                break

    return " ".join(words)


ATTACKS = {
    "insert": insert_attack,
    "delete": delete_attack,
    "flip": flip_attack,
    "mix": mix_attack,
}

STRENGTHS = [1, 3, 5, 10, 20, 30]


def survival(decoder, wm_text: str, salt: bytes) -> bool:
    d = decoder.decode(wm_text, salt)
    return bool(d.success and d.user_id == UID)


def main() -> None:
    key = generate_master_key()
    rng = random.Random(2026)

    emb2, dec2 = Embedder(key), Decoder(key)
    r2 = emb2.embed(TEXT, user_id=UID)
    emb3, dec3 = CAEmbedder(key), CADecoder(key)
    r3 = emb3.embed(TEXT, user_id=UID)

    print(f"文本规模：{len(TEXT.split())} 词；"
          f"v0.2 锚点 {r2.n_anchors}（替换 {r2.n_replaced}）；"
          f"v0.3 可锚定 {r3.n_anchorable}（替换 {r3.n_replaced}）")
    print(f"每组 {N_TRIALS} 次试验，报告正确还原 UID 的存活数\n")

    for name, attack in ATTACKS.items():
        print(f"### {name} 攻击")
        print("强度 | v0.2 | v0.3")
        for n in STRENGTHS:
            ok2 = sum(
                1 for _ in range(N_TRIALS)
                if survival(dec2, attack(r2.watermarked_text, n, rng), r2.session_salt)
            )
            ok3 = sum(
                1 for _ in range(N_TRIALS)
                if survival(dec3, attack(r3.watermarked_text, n, rng), r3.session_salt)
            )
            print(f"{n:4d} | {ok2:4d} | {ok3:4d}")
        print()

    # paraphrase 评测：v0.3（sentence_aware=False）vs v0.4（sentence_aware=True）
    print("### paraphrase_sentence 攻击（v0.3 跨句指纹 vs v0.4 句子边界感知）")
    emb_v03 = CAEmbedder(key, CAConfig(sentence_aware=False))
    dec_v03 = CADecoder(key, CAConfig(sentence_aware=False))
    emb_v04 = CAEmbedder(key, CAConfig(sentence_aware=True))
    dec_v04 = CADecoder(key, CAConfig(sentence_aware=True))
    r_v03 = emb_v03.embed(TEXT, user_id=UID)
    r_v04 = emb_v04.embed(TEXT, user_id=UID)

    print(f"文本规模：v0.3 可锚定 {r_v03.n_anchorable}；v0.4 可锚定 {r_v04.n_anchorable}")
    print("改写比例 | v0.3 | v0.4")
    for frac in [0.1, 0.25, 0.5, 0.75, 1.0]:
        ok_v03 = sum(
            1 for _ in range(N_TRIALS)
            if bool(
                (d := dec_v03.decode(
                    paraphrase_sentence_attack(r_v03.watermarked_text, frac, rng),
                    r_v03.session_salt,
                )).success and d.user_id == UID
            )
        )
        ok_v04 = sum(
            1 for _ in range(N_TRIALS)
            if bool(
                (d := dec_v04.decode(
                    paraphrase_sentence_attack(r_v04.watermarked_text, frac, rng),
                    r_v04.session_salt,
                )).success and d.user_id == UID
            )
        )
        print(f"{frac:7.0%} | {ok_v03:4d} | {ok_v04:4d}")


if __name__ == "__main__":
    main()
