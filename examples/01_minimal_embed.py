"""最小示例：嵌入并解码用户 ID（decode 模式主线）。

演示：
1. agent 用 master_key 为用户 42 的输出生成水印
2. 验证方（持同一密钥）从水印文本中还原出用户 ID
3. 错误密钥 / 无水印文本均无法通过校验
"""
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


def main() -> None:
    print("=" * 62)
    print("AAWM v0.2 — agent 级用户 ID 水印（藏头诗式 token 变换）")
    print("=" * 62)

    # [1] 密钥：agent 持有 master_key，验证方共享同一密钥
    master_key = generate_master_key()
    print(f"\n[1] master_key: {master_key.hex()[:32]}...（32 字节，双方共享）")

    # [2] 为用户 42 嵌入水印
    embedder = Embedder(master_key)
    result = embedder.embed(SAMPLE_TEXT, user_id=42)
    print(f"\n[2] 嵌入 user_id=42")
    print(f"    编码: {result.code_name}（{result.codeword_bits} bit 码字）")
    print(f"    锚点: {result.n_anchors} 个词典内词位")
    print(f"    天然命中 {result.n_natural} | 替换 {result.n_replaced} | 跳过 {result.n_skipped}")

    # 展示文本差异
    orig_words = SAMPLE_TEXT.split()
    wm_words = result.watermarked_text.split()
    diffs = [(i, a, b) for i, (a, b) in enumerate(zip(orig_words, wm_words)) if a != b]
    print(f"    可见替换 {len(diffs)} 处（同义替换，语义不变）:")
    for i, a, b in diffs[:8]:
        print(f"      ...{a!r} -> {b!r}")
    if len(diffs) > 8:
        print(f"      ...（共 {len(diffs)} 处）")

    # [3] 正确密钥解码
    decoder = Decoder(master_key)
    dec = decoder.decode(result.watermarked_text, result.session_salt)
    print(f"\n[3] 解码（正确密钥）:")
    print(f"    success={dec.success}, 还原 user_id={dec.user_id}")
    print(f"    观测误码 {dec.n_errors}/{dec.n_anchors}（{dec.error_rate:.1%}）")
    assert dec.success and dec.user_id == 42, "往返解码失败"

    # [4] 错误密钥解码
    wrong = Decoder(generate_master_key())
    dec = wrong.decode(result.watermarked_text, result.session_salt)
    print(f"\n[4] 解码（错误密钥）:")
    print(f"    success={dec.success}, user_id={dec.user_id}（应失败）")
    assert not dec.success

    # [5] 无水印原文解码
    dec = decoder.decode(SAMPLE_TEXT, result.session_salt)
    print(f"\n[5] 解码（无水印原文）:")
    print(f"    success={dec.success}, user_id={dec.user_id}（应失败）")
    assert not dec.success

    print("\n" + "=" * 62)
    print("结论：水印可解码还原用户身份；密钥敏感；无水印不误报。")
    print("=" * 62)


if __name__ == "__main__":
    main()
