"""示例 03：内容寻址锚点——抗插入/删除编辑的水印。

对比 v0.2（位置索引锚点，编辑即溃）与 v0.3（内容寻址锚点，编辑局部化）。

运行：python examples/03_edit_robustness.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm import (  # noqa: E402
    CADecoder,
    CAEmbedder,
    Decoder,
    Embedder,
    generate_master_key,
)

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

INSERT_WORDS = ["also", "just", "really", "quite", "often", "perhaps"]


def insert_attack(text: str, n: int, rng: random.Random) -> str:
    words = text.split(" ")
    for _ in range(n):
        words.insert(rng.randrange(len(words) + 1), rng.choice(INSERT_WORDS))
    return " ".join(words)


def delete_attack(text: str, n: int, rng: random.Random) -> str:
    words = [w for w in text.split(" ") if w]
    for _ in range(n):
        del words[rng.randrange(len(words))]
    return " ".join(words)


def main() -> None:
    key = generate_master_key()
    uid = 1001
    rng = random.Random(2026)

    print("=" * 62)
    print("示例 03：内容寻址锚点 vs 位置索引锚点（编辑攻击对比）")
    print("=" * 62)

    # --- 嵌入（两种方案，同一密钥同一盐）---
    salt = b"demo-salt-0123456"
    r2 = Embedder(key).embed(TEXT, user_id=uid, session_salt=salt)
    r3 = CAEmbedder(key).embed(TEXT, user_id=uid, session_salt=salt)

    print(f"\n[1] 嵌入 user_id={uid}")
    print(f"    v0.2 位置索引：{r2.n_anchors} 锚点，替换 {r2.n_replaced} 词")
    print(f"    v0.3 内容寻址：{r3.n_anchorable} 可锚定位，替换 {r3.n_replaced} 词")

    # --- 无攻击往返 ---
    d2 = Decoder(key).decode(r2.watermarked_text, salt)
    d3 = CADecoder(key).decode(r3.watermarked_text, salt)
    print(f"\n[2] 无攻击解码：v0.2 {'✓' if d2.success else '✗'} | "
          f"v0.3 {'✓' if d3.success else '✗'} "
          f"(uid={d3.user_id}, {d3.n_votes} 票)")

    # --- 编辑攻击 ---
    dec2, dec3 = Decoder(key), CADecoder(key)
    print(f"\n[3] 编辑攻击存活率（每组 {20} 次试验）：")
    print(f"    {'攻击':<12}{'v0.2':>8}{'v0.3':>8}")
    for label, attack in [
        ("插入 3 词", lambda t: insert_attack(t, 3, rng)),
        ("插入 10 词", lambda t: insert_attack(t, 10, rng)),
        ("删除 3 词", lambda t: delete_attack(t, 3, rng)),
        ("删除 10 词", lambda t: delete_attack(t, 10, rng)),
        ("插入+删除 20", lambda t: delete_attack(insert_attack(t, 10, rng), 10, rng)),
    ]:
        ok2 = sum(1 for _ in range(20) if dec2.decode(attack(r2.watermarked_text), salt).success)
        ok3 = sum(1 for _ in range(20) if dec3.decode(attack(r3.watermarked_text), salt).success)
        print(f"    {label:<12}{ok2:>4}/20{ok3:>4}/20")

    print(
        "\n结论：v0.2 的锚点 = 绝对位置索引，任何插入/删除都使后续锚点全体偏移，"
        "\n      水印一击即溃；v0.3 的锚点身份 = 局部上下文指纹（同义组 ID 构造，"
        "\n      替换不变），编辑只损失局部 2-3 票，由桶内多数表决吸收。"
    )


if __name__ == "__main__":
    main()
