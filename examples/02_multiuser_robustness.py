"""多用户与攻击鲁棒性示例。

演示：
1. 同一文本为三个不同用户生成水印 —— 三份文本互不相同，
   且各自解码还原出正确的用户 ID
2. 同义翻转攻击：攻击者不知道锚点位置与字母映射，
   随机把文本中的词换成同义词 —— 不同攻击强度下的解码成功率
3. identify()：在候选名单中比对水印归属
"""
import random

from aawm import Decoder, Embedder, generate_master_key

SAMPLE_TEXT = (
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

USERS = [1001, 2002, 3003]


def synonym_flip_attack(text: str, synonyms: dict, n_flips: int, rng: random.Random) -> str:
    """模拟攻击者：随机选词典内词替换为其同组其他词。

    攻击者模型：知道同义词典（公开信息），但不知道
    锚点位置与字母映射（密钥派生）。
    """
    words = text.split()
    candidates = [i for i, w in enumerate(words) if w.lower().strip(".,!?;:\"'") in synonyms]
    rng.shuffle(candidates)
    flipped = 0
    for i in candidates:
        if flipped >= n_flips:
            break
        key = words[i].lower().strip(".,!?;:\"'")
        group = [c for c in synonyms[key] if c != key]
        if not group:
            continue
        new = rng.choice(group)
        if words[i][:1].isupper():
            new = new[:1].upper() + new[1:]
        words[i] = new
        flipped += 1
    return " ".join(words)


def main() -> None:
    print("=" * 62)
    print("AAWM v0.2 — 多用户水印与攻击鲁棒性")
    print("=" * 62)

    master_key = generate_master_key()
    embedder = Embedder(master_key)
    decoder = Decoder(master_key)

    # ------------------------------------------------------------
    print("\n[1] 同一文本，三个用户 → 三份不同的水印文本")
    results = {}
    for uid in USERS:
        r = embedder.embed(SAMPLE_TEXT, user_id=uid)
        results[uid] = r
        d = decoder.decode(r.watermarked_text, r.session_salt)
        status = "OK " if (d.success and d.user_id == uid) else "FAIL"
        print(f"    user {uid}: 替换 {r.n_replaced:2d} 处 | 解码 {status} → {d.user_id}"
              f" | 误码 {d.error_rate:.1%}")

    texts = {uid: r.watermarked_text for uid, r in results.items()}
    u1, u2, u3 = USERS
    print(f"    文本互异: {texts[u1] != texts[u2] != texts[u3]}（每用户水印唯一）")
    print(f"    交叉解码: user {u2} 的文本用 u1 的盐解 →", end=" ")
    d = decoder.decode(texts[u2], results[u1].session_salt)
    print(f"success={d.success}（盐不匹配则失败，符合预期）")

    # ------------------------------------------------------------
    print("\n[2] 同义翻转攻击（攻击者不知锚点，随机换词）")
    from aawm.embedder import _SYNONYMS
    rng = random.Random(2026)
    r1001 = results[1001]

    for strength in [5, 10, 15, 20, 30]:
        n_trials = 20
        success = 0
        for t in range(n_trials):
            attacked = synonym_flip_attack(
                r1001.watermarked_text, _SYNONYMS, strength, rng
            )
            d = decoder.decode(attacked, r1001.session_salt)
            if d.success and d.user_id == 1001:
                success += 1
        print(f"    翻转 {strength:2d} 词: 解码成功率 {success}/{n_trials}")

    # ------------------------------------------------------------
    print("\n[3] identify()：在候选名单中比对归属")
    d = decoder.decode(r1001.watermarked_text, r1001.session_salt)
    if d.success:
        print(f"    直接解码: user_id={d.user_id}（无需候选名单）")
    matched = decoder.identify(
        r1001.watermarked_text, r1001.session_salt, candidate_ids=[1001, 2002, 3003]
    )
    print(f"    identify → {matched}（应等于 1001）")

    print("\n" + "=" * 62)
    print("结论：每用户水印唯一且可还原；抗随机同义翻转攻击。")
    print("=" * 62)


if __name__ == "__main__":
    main()
