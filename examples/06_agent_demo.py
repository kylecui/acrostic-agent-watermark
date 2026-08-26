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
class FakeChatCompletions:
    """模拟 openai.chat.completions：固定返回一段长英文文本。"""

    def create(self, *args, **kwargs):
        class Msg:
            content = (
                "The platform collects telemetry from every distributed agent "
                "working in the fleet. Each agent watches a big stream of events, "
                "keeps a small record of important changes, and builds a short "
                "summary at the end of the reporting window. A strong supervisor "
                "groups the results into a common view, so the whole system stays "
                "easy to inspect. When an agent finds a hard problem it cannot fix "
                "alone, it sends a quick alert to the central team and asks for "
                "help. The team then checks whether the issue is new or old, "
                "whether it is critical or minor, and whether a fast patch is "
                "possible without a full restart of the service. The platform also "
                "supports a strong audit trail that records every important change "
                "made by any agent in the system, so a careful reviewer can always "
                "find the root cause of a hard problem."
            )

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

    # 存档：每次发布的 session_salt（溯源必需，可公开，写 DB/日志）
    archive = {}

    def on_embed(result, ctx):
        """中间件嵌入成功后的回调——把 salt 存档。

        没有这一步，中间件嵌入的水印事后无法溯源。
        result.user_id 是实际嵌入的 UID（int），用它作为存档键。
        """
        archive[result.user_id] = result.session_salt

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

    # 验证方拿到泄露文本 + 存档的 salt，即可溯源
    trace = watermarker.trace(leaked, session_salt=archive[bob_uid])

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
