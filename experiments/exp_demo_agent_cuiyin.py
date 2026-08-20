#!/usr/bin/env python3
"""exp_demo_agent_cuiyin.py: 通俗演示 —— 用 "agent-cuiyin" 作为水印。

流程（与真实部署一致）：
1. 注册 agent-cuiyin → 分配 UID（UIDRegistry，别名↔16-bit UID）
2. 把水印嵌入一段中文文字（GreenlistCodec.embed）
3. 检测：从文本解出 UID → 反查别名（GreenlistCodec.detect + registry.lookup）
4. 攻击阶梯：逐级加大"改写强度"，观察水印何时仍可解、何时彻底破坏
   - 嵌入往返（0% 修改）
   - 轻微改写（PAWS 实测转移概率：keep 83.7% / 组内同义 2.1% / 删改 14.1%）
   - 同组替换 10% / 30% / 50% / 70%（s10/s30/s50/s70）
5. 对照：一段完全没水印的文本（null），证明检测不是"总能解出东西"
"""
from __future__ import annotations

import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "experiments")

from aawm.greenlist import GreenlistCodec
from aawm.plugins.registry import UIDRegistry
from exp_real_corpus import synonym_attack
from exp_paws_attack import paraphrase_style_attack, load_paws_positive, transfer_matrix

KEY = bytes(range(32))
SALT = b"demo-agent-cuiyin-2026"


def build_codec() -> GreenlistCodec:
    """生产默认词典（v0.9 扩容 D3r 口径）中文 codec。"""
    codec = GreenlistCodec(KEY, SALT, language_tag=b"zh")
    return codec


def load_tm_paws() -> dict:
    """标定 PAWS 词典词级转移矩阵（轻微改写的真实参数）。"""
    paws = load_paws_positive()
    codec = GreenlistCodec(KEY, SALT, language_tag=b"zh")
    tm = transfer_matrix(codec, paws)
    return tm


def uid_bits(uid: int, n: int = 16) -> str:
    return f"{uid:016b}"


def main() -> None:
    print("=" * 72)
    print("第一步：注册水印标识")
    print("=" * 72)
    reg = UIDRegistry()  # 内存注册库（演示用）
    reg.register("agent-cuiyin")          # 本 agent
    reg.register("agent-xiaoming")        # 其他 agent（干扰项）
    reg.register("agent-external-01")
    for uid, alias in sorted(reg.list_all().items()):
        print(f"  0x{uid:04X}  (二进制 {uid_bits(uid)[-8:]}...)  <- {alias}")
    uid_cuiyin = reg.resolve_alias("agent-cuiyin")
    print(f"  -> agent-cuiyin 分配 UID = 0x{uid_cuiyin:04X} = {uid_cuiyin}")

    # ---- 一段"由 agent 生成"的中文文字（演示水印载体）----
    doc = (
        "随着人工智能技术的快速发展，智能体在内容创作、信息整理和知识服务等"
        "领域发挥着越来越重要的作用。这些智能体能够根据用户的需求，自动完成"
        "文本写作、数据分析和方案设计等复杂任务，极大地提高了工作效率。\n"
        "然而，智能体生成的内容也带来了新的挑战。如何判断一段文字究竟是由"
        "人类写作者完成的，还是由智能体自动生成的，已经成为内容管理和版权"
        "保护领域关注的重点问题。传统的检测方法往往依赖统计特征，但在生成"
        "模型不断升级的背景下，这些方法的准确性和可靠性都面临严峻考验。\n"
        "水印技术为解决这一问题提供了新的思路。通过在生成文本中嵌入不可见的"
        "标记信息，可以在需要的时候准确识别文本的来源和归属。这种技术不改变"
        "文本的含义，也不影响读者的阅读体验，却能为内容安全和版权保护提供"
        "坚实的技术支撑。\n"
        "本文介绍一种基于同义词替换的文本水印方法。该方法利用语言模型中同义词"
        "的多样性，在保持语义不变的前提下，把特定的编码信息巧妙地隐藏在看似"
        "普通的词语选择之中。即使文本经过轻微的修改或润色，水印仍然能够被"
        "可靠地检测出来。这种技术与传统的加密方法不同，它不需要改变文本的"
        "整体结构，而是从词语层面入手，实现隐蔽而稳健的溯源能力。\n"
    )

    codec = build_codec()
    rep0 = codec.detect(doc)
    print(f"\n  原文词典命中：{rep0.n_dict_words} 个词典词"
          f"（水印要藏在词典词的「颜色」里）")

    print("\n" + "=" * 72)
    print("第二步：嵌入水印（UID = agent-cuiyin）")
    print("=" * 72)
    marked = codec.embed(doc, uid_cuiyin, bias=1.0, rng=random.Random(42))
    rep1 = codec.detect(marked)
    print(f"  嵌入后词典命中：{rep1.n_dict_words} 个（每个词典词都参与编码）")
    print(f"  存在性得分 Σ|z| = {rep1.existence_score:.1f}（应远高于 null 水平）")
    print("\n  —— 带水印的文本（人眼看不出任何异常）——")
    print(marked[:300] + "……")

    # ---- null 对照：另一段没有水印的文字 ----
    null_doc = (
        "今天天气很好，阳光洒在窗台上。下午去书店逛了一圈，买了一本关于"
        "旅行的书，打算周末的时候找个安静的地方慢慢读。回来后煮了杯茶，"
        "坐在阳台上看了会儿云。傍晚给朋友打了个电话，聊了聊最近的工作和"
        "生活，大家都觉得时间过得真快。晚上简单做了顿饭，收拾完屋子，"
        "准备明天开始读那本新书。生活就是这样，平淡而充实。"
    )

    print("\n" + "=" * 72)
    print("第三步：检测 —— 从文本反解水印标识")
    print("=" * 72)
    rep_d = codec.detect(marked)
    print(f"  解出 UID = 0x{rep_d.uid:04X}")
    print(f"  存在性得分 = {rep_d.existence_score:.1f}")
    alias = reg.lookup(rep_d.uid)
    print(f"  注册库反查 -> {alias if alias else '未注册'}")
    match = reg.masked_nearest_match(rep_d.uid, active_mask=0xFFFF, max_hamming=3)
    print(f"  最近邻匹配 -> {match[1] if match else '无'}"
          f"（汉明距 {match[2] if match else '-'}）")

    # null 对照
    rep_null = codec.detect(null_doc)
    null_alias = reg.lookup(rep_null.uid)
    null_match = reg.masked_nearest_match(rep_null.uid, active_mask=0xFFFF, max_hamming=3)
    print(f"\n  [对照] 无标记文本：解出 UID=0x{rep_null.uid:04X}"
          f"（存在性 {rep_null.existence_score:.1f}，远低于标记文本），"
          f"反查={null_alias or '无'}, 最近邻匹配={'无' if null_match is None else null_match[1]}")
    print("  -> 说明：解出的 UID 是随机的，但存在性得分低 → 判定为无标记文本")

    print("\n" + "=" * 72)
    print("第四步：攻击阶梯 —— 修改到什么程度才破坏水印？")
    print("=" * 72)

    # PAWS 真实轻微改写参数
    tm = load_tm_paws()
    print(f"  PAWS 实测转移概率：保留 {tm['keep_p']*100:.1f}% / "
          f"组内同义替换 {tm['grp_sub_p']*100:.2f}% / "
          f"删除·组外 {tm['del_p']*100:.1f}%")

    candidates = sorted(set(reg.list_all()))
    rows = []

    def run_attack(tag: str, rw: str) -> None:
        rep = codec.detect(rw)
        d = bin(rep.uid ^ uid_cuiyin).count("1")
        best, score, gap = codec.soft_match(rw, candidates, min_n=1, margin=0.0)
        ok = best == uid_cuiyin
        # 带容错的最近邻（真实部署：注册库匹配容忍 ≤3 位翻转）
        nn = reg.masked_nearest_match(rep.uid, active_mask=0xFFFF, max_hamming=3)
        nn_ok = nn is not None and nn[0] == uid_cuiyin
        rows.append((tag, d, best, ok, nn_ok, rep.existence_score))

    # 0% 修改
    run_attack("嵌入往返", marked)

    # 轻微改写：PAWS 真实参数
    rw_paws = paraphrase_style_attack(codec, marked, 200, tm["del_p"], tm["grp_sub_p"])
    run_attack("PAWS 轻微改写", rw_paws)

    # 同组替换阶梯：10% / 30% / 50% / 70%
    for frac in (0.10, 0.30, 0.50, 0.70):
        rw, _ = synonym_attack(codec, marked, frac, 100)
        run_attack(f"同组替换 {int(frac*100)}%", rw)

    print(f"\n  {'攻击':<14} | {'硬解UID':>8} | {'与真值汉明':>8} | {'soft匹配':>8} | {'容错最近邻':>9} | {'存在性':>5}")
    print("  " + "-" * 72)
    for tag, d, best, ok, nn_ok, ex in rows:
        print(f"  {tag:<14} | 0x{best:04X}   | {d:2d}/16    | "
              f"{'✓' if ok else '✗':>8} | "
              f"{'✓ 仍是cuiyin' if nn_ok else '✗ 无法匹配':>9} | {ex:5.1f}")

    print("""
读表要点：
  · 硬解 UID：16 位里每位是一"票"，被改翻的位直接显示为汉明距；
  · soft 匹配：用逐带 z 幅度积分打分，而不是数位数——30% 替换时
    硬解已翻 2 位，soft 依然锁定 agent-cuiyin（幅度信息更抗噪）；
  · 容错最近邻：真实部署里注册库匹配容忍 ≤3 位翻转，所以
    "硬解翻 2 位"并不影响追溯到人，只要不是刻意毁掉一半以上证据。
""")

    print("\n" + "=" * 72)
    print("通俗解释")
    print("=" * 72)
    print("""
机制一句话：每个词典词被密钥染成"黑/白"两种颜色，16 个频带各守 UID 的一位。
嵌入 = 把每段的词换成"目标颜色"的同义词（语义不变，人眼无感）；
检测 = 统计每带黑白比例，偏离随机就解出 1/0，拼回 16 位 = UID。

轻微修改（删改个别词、换个别同义词）：每带还有足够多词保持原色，
统计结论不翻转 → 水印仍在。这就像 100 票里改掉 10 票，多数派不变。

改得越狠，每带"原色票"越少，随机波动越可能翻盘 → 某几位翻转 → UID 对不上。
50% 替换是临界区：短文本（每带 ~6 词）已有概率失守；
长文本（每带 ~14 词）统计基数大，70% 替换往往仍能扛住。
""")

    # 展示一个被 70% 破坏的例子文本片段
    rw70, _ = synonym_attack(codec, marked, 0.70, 100)
    rep70 = codec.detect(rw70)
    print("  [示例] 70% 替换后的文本开头（语义仍在，颜色已乱）：")
    print("  " + rw70[:120] + "……")
    print(f"  -> 此时解出 UID=0x{rep70.uid:04X}，与 0x{uid_cuiyin:04X} "
          f"汉明距 {bin(rep70.uid ^ uid_cuiyin).count('1')}/16")


if __name__ == "__main__":
    main()
