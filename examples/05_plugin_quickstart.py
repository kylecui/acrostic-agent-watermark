"""示例 05：插件快速开始 —— 3 行代码接入水印。

演示 Watermarker Facade 的一键嵌入 + 一键溯源。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm.plugins import UIDRegistry, Watermarker


def main() -> None:
    # ------------------------------------------------------------------
    # 1. 初始化（带注册库）
    # ------------------------------------------------------------------
    registry = UIDRegistry(backend="memory")
    registry.register("agent-cuiyin", uid=0x1234)
    registry.register("agent-beta", uid=0x00FF)
    registry.register("agent-gamma", uid=0xFF00)

    watermarker = Watermarker(registry=registry)

    print("=" * 72)
    print("AAWM 插件快速开始")
    print("=" * 72)
    print(f"注册库用户: {registry.list_all()}")
    print()

    # ------------------------------------------------------------------
    # 2. 嵌入水印（Agent 拿到 LLM 输出后调这一行）
    # ------------------------------------------------------------------
    agent_output = (
        "The platform collects telemetry from every distributed agent working "
        "in the fleet. Each agent watches a big stream of events, keeps a small "
        "record of important changes, and builds a short summary at the end of "
        "the reporting window. A strong supervisor groups the results into a "
        "common view, so the whole system stays easy to inspect. When an agent "
        "finds a hard problem it cannot fix alone, it sends a quick alert to "
        "the central team and asks for help. The team then checks whether the "
        "issue is new or old, whether it is critical or minor, and whether a "
        "fast patch is possible without a full restart of the service. The "
        "platform also supports a strong audit trail that records every "
        "important change made by any agent in the system, so a careful "
        "reviewer can always find the root cause of a hard problem."
    )

    # 用别名嵌入（业务侧不需要管数字 UID）
    result = watermarker.embed(agent_output, user_id="agent-cuiyin")

    print("[嵌入] Agent 为用户 'agent-cuiyin' 生成的水印文本：")
    print(f"  UID: 0x{result.user_id:04X}")
    print(f"  词典命中: {result.n_dict_words} 词")
    print(f"  存在性得分: {result.existence_score:.1f}")
    print(f"  信道 A 签名: {'已签署' if result.seal else '未签署'}")
    print(f"  前后差异示例:")
    print(f"    原文前 80 字: {agent_output[:80]}...")
    print(f"    水印前 80 字: {result.watermarked_text[:80]}...")
    print()

    # ------------------------------------------------------------------
    # 3. 溯源（验证方拿到嫌疑文本后调这一行）
    # ------------------------------------------------------------------
    trace = watermarker.trace(
        result.watermarked_text,
        session_salt=result.session_salt,
        seal=result.seal,
    )

    print("[溯源] 验证方对水印文本的溯源结果：")
    print(f"  检出水印: {trace.watermarked}")
    print(f"  解码 UID: 0x{trace.uid:04X}" if trace.uid is not None else "  解码 UID: None")
    print(f"  匹配用户: {trace.user}")
    print(f"  汉明距: {trace.hamming_dist}")
    print(f"  置信度: {trace.confidence:.2f}")
    print(f"  篡改判定: {trace.tampered}")
    print()

    # ------------------------------------------------------------------
    # 4. 篡改检测演示
    # ------------------------------------------------------------------
    # 改一个词
    paras = result.watermarked_text.split(". ")
    paras[0] = paras[0].replace("telemetry", "surveillance")
    tampered_text = ". ".join(paras)

    trace2 = watermarker.trace(
        tampered_text,
        session_salt=result.session_salt,
        seal=result.seal,
    )

    print("[篡改检测] 改一个词后的溯源结果：")
    print(f"  检出水印: {trace2.watermarked}")
    print(f"  匹配用户: {trace2.user}（仍可溯源）")
    print(f"  篡改判定: {trace2.tampered}")
    print(f"  被改段落: {trace2.tampered_paragraphs}")
    print()

    # ------------------------------------------------------------------
    # 5. 无水印文本对照
    # ------------------------------------------------------------------
    trace3 = watermarker.trace(
        agent_output,  # 原始未水印文本
        session_salt=result.session_salt,
    )

    print("[对照] 无水印文本的检测结果：")
    print(f"  检出水印: {trace3.watermarked}")
    print(f"  存在性得分: {trace3.existence_score:.1f}（水印文本为 {trace.existence_score:.1f}）")
    print()

    print("=" * 72)
    print("结论：用户 ID 成功嵌入并复原，实现追溯目标。")
    print("=" * 72)


if __name__ == "__main__":
    main()
