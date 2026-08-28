# AAWM Agent 嵌入指南

> AAWM 是一个 **agent 级水印 SDK / framework**（Python 包只是它的分发形态）。
> 本文教你把水印能力嵌进任意 agent 系统，从「自研代码」到「低代码平台」全覆盖。

---

## 1. 三种接入形态，按需选择

| 形态 | 适用场景 | 改动量 | 示例 |
|---|---|---|---|
| **A. 通用 SDK 三件套** | 你自己写 agent 代码 | 3 行 | [examples/05_plugin_quickstart.py](../examples/05_plugin_quickstart.py) |
| **B. 适配器一行包装** | 用 openai / LangChain / LiteLLM / AutoGen / CrewAI | 1 行 | [examples/06_agent_demo.py](../examples/06_agent_demo.py) |
| **C. 低代码平台** | Dify / Coze 等无代码编排 | 配置 + 1 个 HTTP 调用 | 见 §5 |
| **D. CLI/IDE agent** | Claude Code / Codex / opencode / WorkBuddy / Antigravity / PI / Qwen Code 等黑盒进程 | 改 base_url | [cli_agent_proxy_guide.md](cli_agent_proxy_guide.md) |

无论哪种形态，核心都是同一个 `Watermarker` 对象：

```python
from aawm.plugins import Watermarker, UIDRegistry

registry = UIDRegistry(backend="memory")          # 用户注册库（可换成文件/数据库后端）
registry.register("alice", uid=0xA11C)
watermarker = Watermarker(registry=registry)
```

---

## 2. 形态 A：通用 SDK 三件套（任意自研 agent）

在 agent 产出文本后、返回给用户前调用 `embed`；归档 salt；事后用 `trace` 溯源：

```python
# 生成端：agent 拿到 LLM 原始输出后
result = watermarker.embed(agent_output, user_id="alice")

# 发布 result.watermarked_text 给用户
# 必须存档：result.session_salt（溯源凭据，可公开）、result.seal（篡改检测，可选）
```

```python
# 溯源端：验证方拿到疑似泄露的文本
trace = watermarker.trace(suspect_text, session_salt=stored_salt)
if trace.watermarked:
    print(f"泄露源自 {trace.user}（置信度 {trace.confidence:.2f}）")
```

适合所有「agent 就是代码」的场景。如果你只想拦最外层的输出，这是最稳的方式——
不需要理解任何框架内部机制。

---

## 3. 形态 B：适配器一行包装（主流框架）

所有适配器共享同一套中间件，**核心铁律是 Fail-open**：任何嵌入异常都透传原文，
绝不影响 agent 响应。且所有适配器都支持 `on_embed` 回调——**务必用它存档 salt**，
否则中间件嵌入的水印事后无法溯源（见 §4）。

### 3.1 OpenAI SDK（最常用）

```python
from openai import OpenAI
from aawm.plugins.adapters.openai_v1 import wrap_openai_client

client = wrap_openai_client(OpenAI(), watermarker, on_embed=archive_salt)
resp = client.chat.completions.create(..., user_id="alice")   # 输出自动嵌水印
```

同步/异步、流式/非流式全覆盖（`AsyncOpenAI` 用 `wrap_async_openai_client`）。
`user_id` 从 `create(**)` 参数 / 请求头 / 环境变量解析。

### 3.2 LangChain v1

```python
from langchain.agents import create_agent
from aawm.plugins.adapters.langchain_v1 import AAWMMiddleware

agent = create_agent(model=model, tools=[...], middleware=[AAWMMiddleware(watermarker)])
```

在 `after_model` 钩子给 LLM 输出嵌水印（含流式）。

### 3.3 LiteLLM Proxy（网关级）

```python
from aawm.plugins.adapters.litellm_proxy import setup_hooks
setup_hooks(watermarker, on_embed=archive_salt)
```

在 `proxy_config.yaml` 里 `callbacks: aawm_proxy_hooks` 即可对**所有**经网关的
LLM 调用自动嵌水印。

### 3.4 AutoGen（autogen-agentchat）

```python
from autogen_agentchat.agents import AssistantAgent
from aawm.plugins.adapters.autogen_v1 import wrap_autogen_agent

agent = wrap_autogen_agent(AssistantAgent(...), watermarker, user_id="alice")
response = await agent.on_messages(messages, cancellation_token)  # 输出已嵌水印
```

包装 `on_messages`，在模型返回后、交还给调用方前改写 `chat_message.content`。

### 3.5 CrewAI

```python
from aawm.plugins.adapters.crewai_v1 import setup_hooks
setup_hooks(watermarker, user_id="alice")

result = crew.kickoff()   # 所有 agent 的 LLM 输出自动嵌水印
```

注册 `after_llm_call` 全局 hook，每次 LLM 调用返回后嵌水印。
测试/热更新用 `clear_hooks()` 清理。

> 注意 AutoGen 与 CrewAI 适配器默认**不安装对应框架也能 import**，
> 但调用包装函数时会给出清晰提示：`pip install 'aawm[autogen]'` 等。

---

## 4. 关键：用 on_embed 存档 salt（中间件模式下溯源的前提）

适配器是「后处理中间件」，嵌入发生在框架内部——**session_salt 不会自动回到你手里**。
没有 salt，事后 `trace` 无法解码 UID。所以必须提供 `on_embed` 回调，在每次嵌入成功时存档：

```python
def archive_salt(result, ctx):
    """result: EmbedResult（含 session_salt）；ctx: 本次请求上下文"""
    db.save(
        salt=result.session_salt,     # 溯源凭据，可公开
        user_id=result.user_id,       # 实际嵌入的 UID
        text_hash=sha256(result.watermarked_text).hexdigest(),
        ts=now(),
    )

client = wrap_openai_client(OpenAI(), watermarker, on_embed=archive_salt)
```

溯源时从数据库按「文本哈希」或「用户 + 时间窗」反查 salt，再调 `trace`。
`on_embed` 回调本身也是 fail-open 的（存档失败不影响嵌入与响应）。

---

## 5. 形态 C：低代码平台（Dify / Coze）

Dify / Coze 没有可注入的 Python 进程，但都支持「在输出后调一个 HTTP 接口」——
这正是 AAWM HTTP server 的用武之地。

### 5.1 先起一个水印服务

```bash
aawm serve --key key.json --registry reg.json --port 8765
```

### 5.2 Dify：工作流「代码节点」后处理

1. 在 Dify 工作流末尾加一个「代码节点」
2. 把 LLM 输出传入，代码里 `requests.post("http://<aawm-host>:8765/v1/embed", json={...})`
3. 拿 `watermarked_text` 作为最终输出，`session_salt`/`bands` 写回变量存档

```python
import requests

def main(llm_output: str, user_id: str) -> dict:
    r = requests.post("http://aawm-host:8765/v1/embed", json={
        "text": llm_output,
        "user_id": user_id,
    }, timeout=10)
    return {"watermarked": r.json()["watermarked_text"],
            "session_salt": r.json()["session_salt"]}
```

### 5.3 Coze：插件或 API 网关

- **插件**：写一个「文本水印」插件，内部调 `/v1/embed`，工作流里插到 LLM 节点之后
- **API 网关**：把 aawm 放到 Coze 与企业自有 LLM 之间，或对 Coze 返回的文本统一走 `/v1/embed` 后处理

> 共同点：**只要输出文本经过一个可编程节点，就能嵌水印**。
> 低代码平台没有「进程内中间件」，因此推荐形态 A 的 HTTP API 而非适配器。

---

## 6. user_id 解析通道（适配器共享）

适配器不要求你显式传 user_id，按以下优先级自动解析：

| 优先级 | 来源 | 设置方式 |
|---|---|---|
| 1 | 显式参数 | `wrap_openai_client(..., user_id=...)` / `setup_hooks(..., user_id=...)` |
| 2 | 请求上下文 | OpenAI `create(**kwargs)` 的 `user_id=` / 请求头；LangChain `request.runtime.context` |
| 3 | contextvars | 请求入口 `set_user_id(...)`（自研 agent 请求级隔离） |
| 4 | 环境变量 | `AAWM_USER_ID`（兜底） |

**都解析不到 → 跳过嵌入**（不嵌入错误身份）。这是安全默认：宁可漏嵌，不错嵌。

---

## 7. 完整示例

- **端到端泄露溯源演示（推荐先跑）**：[examples/06_agent_demo.py](../examples/06_agent_demo.py)
  三个用户调 agent → 各自拿到水印文本 → 模拟泄露 → 溯源到具体用户。可离线运行。
- **三件套最小示例**：[examples/05_plugin_quickstart.py](../examples/05_plugin_quickstart.py)
- **API 参考**：[api_reference.md](api_reference.md)　**服务端部署**：[deployment.md](deployment.md)
