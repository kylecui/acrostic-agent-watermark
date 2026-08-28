"""示例 06：把水印嵌入真实 Agent —— 端到端泄露溯源演示。

场景：你的产品里跑着一个 Agent（调 LLM 给用户生成内容）。你希望
「任何一份流出的输出都能追溯到是哪个用户泄露的」。

本示例演示完整闭环：
    1. 用 ``wrap_openai_client`` 一行包装 LLM 客户端 —— Agent 输出自动嵌水印
    2. 三个用户分别调用 Agent，各自拿到不同（但都正常）的水印文本
    3. 模拟「用户 B 把输出发到了群里」→ 验证方 trace → 溯源到 B

可离线运行：用 FakeLLM 模拟 OpenAI SDK，不需要真实 API key。
真实接入时只需把 ``FakeLLM()`` 换成 ``openai.OpenAI()``，其余代码不变。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm.plugins import UIDRegistry, Watermarker
from aawm.plugins.adapters.openai_v1 import wrap_openai_client


# ======================================================================
# 1. 模拟 LLM：Agent 的底层模型（真实场景换成 openai.OpenAI()）
# ======================================================================
# v0.9 起英文默认走 zero_cost 零感词典。该模式依赖文本中的词典命中量
# （拼写变体/功能副词/安全对）：通用英文需 ≥600 词、词典密集文本可
# 更低；短文本会弱嵌入（embed 返回 weak_embed=True，trace 可能漏检）。
# 这里模拟一份 576 词的英文分析报告作为 LLM 输出。
LLM_OUTPUT = (
    "We need to analyze the data and organize the project before we can make a final decision. "
    "The team will prioritize the critical tasks and minimize the risk of unexpected errors. "
    "However, the results clearly demonstrate a significant improvement over the previous version. "
    "The whole system remains simple and easy to use, which is a genuine advantage for new users. "
    "The final outcome will be published in the annual report and highlighted in the summary. "
    "We should verify the evidence before we choose the best option for the entire process. "
    "The process was altered to reduce errors and improve the overall outcome of the project. "
    "Almost every example shows the same pattern throughout the entire period of the study. "
    "The supervisor will allocate the resources and assign the tasks to the appropriate teams. "
    "We need to highlight the main factors and strengthen the framework of the whole system. "
    "The team gathered all the data and prepared a comprehensive summary for the final report. "
    "Eventually, the platform will support a global and modern interface for all users. "
    "We frequently discuss the precise requirements before we start the actual work. "
    "The results are usually consistent, although occasionally the data varies across different periods. "
    "We should immediately evaluate the impact and assess the potential risks before we proceed. "
    "The main goal is to achieve a beneficial outcome while maintaining a reliable and sufficient process. "
    "We aim to optimize the workflow and visualize the entire pipeline in a clear manner. "
    "The team decided to postpone the release until we completely finish the remaining tasks. "
    "We need to emphasize the key findings and underscore the important implications for future work. "
    "The system should remain flexible enough to accommodate different types of requests. "
    "We can demonstrate the value of the approach through a simple example and a concrete instance. "
    "The overall perspective of the team is to choose a suitable method and follow the appropriate procedure. "
    "We must ensure that every part of the system remains consistent across different platforms. "
    "The researchers are eager to recognize the contribution of every member of the group. "
    "We should decrease the number of errors and reduce the amount of redundant work. "
    "The project has a clear purpose and a well-defined aim that everyone understands. "
    "We can obtain the necessary evidence from multiple sources and verify the results carefully. "
    "The new version will incorporate the feedback and amend the earlier mistakes. "
    "We should acknowledge the previous achievements and build upon the established foundation. "
    "It is important to protect the sensitive data and safeguard the entire system from threats. "
    "The team will continue to monitor the situation and adjust the strategy as needed. "
    "We can classify the results by type and kind, then compare each factor and element. "
    "The final decision will be based on the evidence and proof that we collect during the study. "
    "We need to establish a common ground and a shared perspective before we proceed further. "
    "The system was designed to handle a huge volume of data and process it efficiently. "
    "We should make the necessary amendments and revise the document before the final submission. "
    "Every period and phase of the project has its own challenges and opportunities. "
    "The goal and objective of this initiative is to improve the overall quality of the service. "
    "We can measure the impact and effect of each change through the collected data. "
    "The purpose and aim of the review is to identify the main benefits and advantages."
)


class FakeChatCompletions:
    """模拟 openai.chat.completions：固定返回一份英文分析报告（576 词）。"""

    def create(self, *args, **kwargs):
        class Msg:
            content = LLM_OUTPUT

        class Choice:
            message = Msg()
            finish_reason = "stop"

        class Resp:
            choices = [Choice()]

        return Resp()


class FakeChat:
    completions = FakeChatCompletions()


class FakeLLM:
    """模拟 openai.OpenAI()。"""

    def __init__(self):
        self.chat = FakeChat()


# ======================================================================
# 2. Agent 服务：初始化水印能力 + 一行接入
# ======================================================================
def main() -> None:
    # 注册库：产品里的所有用户
    registry = UIDRegistry(backend="memory")
    registry.register("alice", uid=0xA11C)
    registry.register("bob", uid=0xB0B)
    registry.register("carol", uid=0xC0A0)

    watermarker = Watermarker(registry=registry)

    # 存档：每次发布的溯源元数据（溯源必需，可公开，写 DB/日志）
    # zero_cost 自适应模式必须存档 bands/n_bits（编码的带集元数据）——
    # 没有它 trace 无法重建自适应解码路径（回退会误码/漏检）。
    archive = {}

    def on_embed(result, ctx):
        """中间件嵌入成功后的回调——把溯源元数据存档。

        没有这一步，中间件嵌入的水印事后无法溯源。
        result.user_id 是实际嵌入的 UID（int），用它作为存档键。
        """
        archive[result.user_id] = {
            "session_salt": result.session_salt,
            "bands": list(result.bands),
            "n_bits": result.n_bits,
        }

    # --- 关键：一行包装 LLM 客户端，Agent 输出自动嵌水印 + salt 自动存档 ---
    llm = wrap_openai_client(FakeLLM(), watermarker, on_embed=on_embed)

    def agent_generate(user: str) -> str:
        """Agent 的对外接口：给用户生成内容。

        user_id 通过 create(**) 的请求头/参数传入，中间件自动解析。
        """
        resp = llm.chat.completions.create(
            model="gpt-4o",
            user_id=user,          # ← 中间件从这里解析出是谁
        )
        return resp.choices[0].message.content

    print("=" * 72)
    print("AAWM × Agent 端到端演示（openai 适配器）")
    print("=" * 72)

    # ------------------------------------------------------------------
    # 3. 三个用户各自调用 Agent
    # ------------------------------------------------------------------
    outputs = {}
    for user in ("alice", "bob", "carol"):
        outputs[user] = agent_generate(user)

    print("\n[Agent 服务] 三个用户各拿到一份输出（内容看似正常，实含隐式水印）：")
    for user, text in outputs.items():
        print(f"  {user:6s} → {text[:60]}...")

    print("\n[关键] 同一份 LLM 原始输出，发给不同用户后文本各不相同：")
    pairs = list(outputs.items())
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            same = pairs[i][1] == pairs[j][1]
            print(f"  {pairs[i][0]} vs {pairs[j][0]}: {'相同!?' if same else '不同（水印差异化生效）'}")

    # ------------------------------------------------------------------
    # 4. 模拟泄露：bob 把输出发到了公开渠道
    # ------------------------------------------------------------------
    leaked = outputs["bob"]
    bob_uid = registry.resolve_alias("bob")
    print(f"\n[事件] 用户 bob 把输出泄露到了公开渠道：\n  {leaked[:60]}...")

    # 验证方拿到泄露文本 + 存档的溯源元数据，即可溯源
    bob_meta = archive[bob_uid]
    trace = watermarker.trace(
        leaked,
        session_salt=bob_meta["session_salt"],
        bands=bob_meta["bands"],
        n_bits=bob_meta["n_bits"],
    )

    print("\n[溯源] 验证方对泄露文本的判定：")
    print(f"  检出水印: {trace.watermarked}")
    if trace.uid is not None:
        print(f"  泄露者:   {trace.user}（UID 0x{trace.uid:05X}）")
    else:
        print("  泄露者:   None")
    print(f"  置信度:   {trace.confidence:.2f}")
    print(f"  篡改:     {'已篡改' if trace.tampered else '未检测（未传 seal）'}")

    assert trace.watermarked and trace.user == "bob", "溯源失败！"
    print("\n✓ 泄露者被准确追溯为 bob —— agent 级水印闭环完成。")

    # ------------------------------------------------------------------
    # 5. 对照：绕过 Agent 直接调裸 LLM 的输出无法溯源
    # ------------------------------------------------------------------
    raw = FakeLLM().chat.completions.create(model="gpt-4o")
    t0 = watermarker.trace(raw.choices[0].message.content)
    # 无 salt 时无法解码 UID——即使存在性误报 True，也还原不出用户
    print(f"\n[对照] 绕过 Agent 直接调裸 LLM 的输出：可溯源到用户？ "
          f"{'是' if t0.user is not None else '否（没有 salt，无法解码 UID）'}")


if __name__ == "__main__":
    main()
