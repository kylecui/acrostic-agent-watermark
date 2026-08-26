# CLI/IDE Agent 接入指南（代理网关）

> Claude Code、Codex、opencode、WorkBuddy、Antigravity、PI、Qwen Code 这类工具
> 是**黑盒独立进程**——无法 import SDK，也没有可注入的 Python 中间件。
> 它们的共同点是：**都支持自定义 API endpoint（base_url）**。
>
> AAWM 本地代理网关利用这一点：把工具的 base_url 指向本地代理，代理拦截响应
> 文本、嵌入水印后再交回工具。**工具零改造，用户无感。**
>
> 这是 [agent_embedding_guide.md](agent_embedding_guide.md) 的形态 D。
> 形态 A/B/C 覆盖自研代码与低代码平台；本文覆盖一切"独立进程式"CLI/IDE agent。

---

## 1. 架构速览

```
CLI/IDE agent（Claude Code / Codex / opencode / WorkBuddy / ...）
    │  唯一改动：base_url = http://127.0.0.1:8787
    ▼
AAWM Proxy（aawm proxy，本地 127.0.0.1:8787）
    │  1. 请求头 key → 查 key_map → 得到 UID（溯源身份）
    │  2. 换真实上游 key，转发到 OpenAI / Anthropic
    ▼
真实 API
    │  响应（非流式 JSON / SSE 流式）
    ▼
AAWM Proxy ── 文本嵌入水印（fail-open：任何异常透传原文）
    ▼
CLI/IDE agent（用户无感）
```

协议支持（代理已实现的三个端点）：

| 端点 | 协议 | 上游 base | 对应工具 |
|---|---|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions | `--upstream-openai` | opencode、PI、Qwen Code、CodeBuddy、Antigravity(openai-compatible) |
| `POST /v1/messages` | Anthropic Messages | `--upstream-anthropic` | Claude Code、WorkBuddy/CodeBuddy、PI、opencode(anthropic) |
| `POST /v1/responses` | OpenAI Responses | `--upstream-openai` | Codex、新版 opencode(responses) |

其余路径（`/v1/models`、count tokens 等）原样反向代理透传。

---

## 2. 快速开始（最小闭环）

### 2.1 准备密钥、注册库、key 映射

```bash
# 1. 生成 master_key
aawm keygen --output key.json

# 2. 注册用户（每台终端/每个用户一个别名）
aawm registry add alice --registry reg.json
aawm registry add bob   --registry reg.json

# 3. 给每个用户发一把 aawm key，并建立 key → UID 映射
#    key-map.json 的值支持：0x 前缀数字 / 十进制数字 / 已注册别名
cat > keys.json <<'EOF'
{
  "sk-aawm-alice": "alice",
  "sk-aawm-bob":   "0xA11C"
}
EOF
```

### 2.2 启动代理网关

```bash
aawm proxy \
  --key key.json --registry reg.json \
  --key-map keys.json \
  --upstream-openai    https://api.openai.com \
  --upstream-anthropic https://api.anthropic.com \
  --salt-archive salts.jsonl \
  --port 8787
```

> 上游 key 从环境变量 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 读取；
> 上游 base 也可用 `AAWM_UPSTREAM_OPENAI` / `AAWM_UPSTREAM_ANTHROPIC` 覆盖。
> 自建网关（如 vLLM/LiteLLM）场景下不配上游 key，客户端 key 原样转发。

### 2.3 验证

```bash
curl http://127.0.0.1:8787/v1/models -H "Authorization: Bearer sk-aawm-alice"
```

返回上游模型列表即网关在线。接下来把某个 agent 的 base_url 指过来即可。

---

## 3. 各 agent 接入配置

### 3.1 Claude Code（Anthropic 协议 → `/v1/messages`）

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
export ANTHROPIC_API_KEY="sk-aawm-alice"
claude
```

或写入 `~/.claude/settings.json` 对所有会话生效：

```json
{ "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
    "ANTHROPIC_API_KEY":  "sk-aawm-alice"
} }
```

> `ANTHROPIC_BASE_URL` 在 Claude Code **进程启动时读取一次**，改配置必须重启。

### 3.2 Codex CLI（OpenAI Responses 协议 → `/v1/responses`）

编辑 `~/.codex/config.toml`（注意 base_url 带 `/v1`，协议走 `responses`）：

```toml
model = "gpt-5.2-codex"        # 上游真实模型名
model_provider = "aawm"

[model_providers.aawm]
name = "AAWM Proxy"
base_url = "http://127.0.0.1:8787/v1"
env_key = "AAWM_CODEX_KEY"
wire_api = "responses"
requires_openai_auth = false
```

导出该 key 并启动：

```bash
export AAWM_CODEX_KEY="sk-aawm-alice"
codex
```

> Codex 新版只支持 `wire_api = "responses"`（对应代理 `/v1/responses`，已实现）；
> 你的版本若仍支持 `wire_api = "chat"` 也可用，走 `/v1/chat/completions`。

### 3.3 opencode（OpenAI-compatible → `/v1/chat/completions`）

`opencode.json`（项目根目录，或 `~/.config/opencode/opencode.json`）：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "aawm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "AAWM",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "{env:AAWM_OPENCODE_KEY}"
      },
      "models": {
        "claude-sonnet-4-6": { "name": "Claude (AAWM)" }
      }
    }
  }
}
```

```bash
export AAWM_OPENCODE_KEY="sk-aawm-alice"
opencode
```

> - `@ai-sdk/openai-compatible` 对应 `/v1/chat/completions`（当前推荐）；
>   若用 responses 变体则走 `/v1/responses`。
> - 新版 opencode 的 schema 为 `providers.<id>` + `package:
>   "@opencode-ai/ai/providers/openai-compatible"` + `settings.baseURL`，含义相同。

### 3.4 WorkBuddy / CodeBuddy Code（Anthropic 或 OpenAI 均可）

**CodeBuddy Code CLI**（Anthropic 协议）：

```bash
export CODEBUDDY_BASE_URL="http://127.0.0.1:8787"
export CODEBUDDY_API_KEY="sk-aawm-alice"
codebuddy
```

**WorkBuddy 桌面版**：设置 → 模型 → 添加模型 → 提供商选「自定义 / Custom」，
接口地址填 `http://127.0.0.1:8787`（Anthropic 协议）或
`http://127.0.0.1:8787/v1`（OpenAI 协议），模型名填上游模型名。

**或直接改 `~/.workbuddy/models.json`**（保存后热加载，无需重启）：

```json
[{
  "id": "claude-sonnet-4-6",
  "name": "claude-sonnet-4-6 (AAWM)",
  "vendor": "Custom",
  "url": "http://127.0.0.1:8787/v1/chat/completions",
  "apiKey": "sk-aawm-alice",
  "supportsToolCall": true,
  "supportsImages": false
}]
```

### 3.5 Antigravity（agy，Google）

**必须走 `customEndpoints` 的 openai-compatible 才能嵌入水印**。
Antigravity 默认的 Gemini 原生协议（`GOOGLE_GEMINI_BASE_URL`）目前是透传不嵌。

编辑 `~/.gemini/antigravity-cli/settings.json`（或 `~/.config/antigravity/config.toml`）：

```json
{
  "model": "gemini-3.5-flash",
  "customEndpoints": {
    "llmUrl": "http://127.0.0.1:8787/v1",
    "apiKey": "sk-aawm-alice",
    "provider": "openai-compatible"
  }
}
```

```bash
agy
```

### 3.6 PI（`~/.pi/agent/models.json`）

```json
{
  "providers": {
    "aawm": {
      "baseUrl": "http://127.0.0.1:8787/v1",
      "api": "openai-completions",
      "apiKey": "sk-aawm-alice",
      "models": [
        { "id": "claude-sonnet-4-6", "contextWindow": 200000, "maxTokens": 16384 }
      ]
    }
  }
}
```

```bash
pi --provider aawm --model claude-sonnet-4-6
```

> `api` 字段支持 `openai-completions`（走 `/v1/chat/completions`）与
> `anthropic-messages`（走 `/v1/messages`），二选一。

### 3.7 Qwen Code / 通义灵码（阿里系列）

编辑 `~/.qwen/settings.json`：

```json
{
  "modelProviders": {
    "openai": [{
      "id": "qwen3-coder-plus",
      "name": "qwen3-coder-plus (AAWM)",
      "baseUrl": "http://127.0.0.1:8787/v1",
      "envKey": "AAWM_QWEN_KEY"
    }]
  },
  "security": { "auth": { "selectedType": "openai" } },
  "model": { "name": "qwen3-coder-plus" }
}
```

```bash
export AAWM_QWEN_KEY="sk-aawm-alice"
qwen
```

> 简化方式：环境变量 `QWEN_CODE_BASE_URL="http://127.0.0.1:8787/v1"` +
> `QWEN_CODE_API_KEY="sk-aawm-alice"`。
> 通义灵码 IDE 插件在设置中添加自定义 endpoint 填同一地址即可。

---

## 4. key → user_id 映射策略

代理用**客户端请求头里的 key 识别身份**：

- OpenAI 协议：`Authorization: Bearer <key>`
- Anthropic 协议：`x-api-key: <key>`（或 Authorization）

`--key-map keys.json` 把 key 映射到 UID（值支持 `0x` 前缀 / 十进制 / 已注册别名）。
**查不到 key → 不嵌入**（fail-safe：宁可漏嵌，不错嵌）。

推荐策略：

| 粒度 | 做法 | 场景 |
|---|---|---|
| 按用户 | 每个员工一把 `sk-aawm-<name>` | 常规溯源：泄露文本 → 定位到人 |
| 按终端 | 每台开发机一把 key | 定位泄露出口设备 |
| 按项目/团队 | 共享一把 key 映射到团队 UID | 粗粒度审计 |

key 就是身份凭据，按访问控制流程发放/回收；被映射的 UID 必须是注册库中的合法 UID。

---

## 5. salt 归档与溯源（必须开）

**`--salt-archive salts.jsonl` 是溯源的前提**。每次嵌入成功，代理把
`(uid, session_salt, n_bits, codec_mode, ts)` 追加一行写入 JSONL：

```jsonl
{"ts": 1769068800.0, "uid": 42, "session_salt": "1a2b...", "n_bits": 48, "codec_mode": "default"}
```

溯源流程：

```bash
# 1. 在归档里按 uid/时间窗找到那次会话的 session_salt
grep '"uid": 42' salts.jsonl | tail -1

# 2. 用同一 master_key + 注册库做溯源
aawm trace --key key.json --registry reg.json \
  --salt <hex> suspect_leak.txt
```

> 流式会话全段共享**同一个 session_salt**（整流共享盐），
> 泄露文本无论截取哪一段，都能用该 salt 解码出同一个 UID。
> 非流式 Responses 响应中 `output` 与顶层 `output_text` 各自独立嵌入，
> 归档会记两条（同一响应的两个视图，都可通过各自 salt 溯源）。

---

## 6. 行为细节

- **非流式**：整段嵌入一次（OpenAI `message.content` / Responses `output_text`
  与 `output[].content[].text` / Anthropic `content[].text`）。
- **流式**：句子级嵌入——`text_delta` / `response.output_text.delta` 缓冲到句末
  标点整句改写；流末尾（`[DONE]` 前 / `response.completed` 前 / Anthropic 流结束）
  自动补发 flush 尾句，保证最后一个句子不丢。
- **工具调用不改**：`tool_calls` / `tool_use` / `function_call` /
  `reasoning_content` 原样透传——只嵌最终给用户的文本，不动工具参数。
- **fail-open 铁律**：任何嵌入异常都透传原文，绝不影响 agent 响应。
- **短文本跳过**：默认 `min_text_length=50`，太短不嵌入（可调）。
- **身份未映射**：key 不在 key_map → 透传不嵌。

---

## 7. 已知限制与路线图

| 限制 | 现状 | 影响 |
|---|---|---|
| Gemini 原生协议（`GOOGLE_GEMINI_BASE_URL`） | 代理对 `/v1beta/...` 透传不嵌 | Antigravity 请用 `customEndpoints` openai-compatible 接入（§3.5） |
| Responses 非流式双视图 | `output` 与 `output_text` 各自独立嵌入 | 同响应两条 salt 记录，溯源按视图取对应 salt |

---

## 8. 故障排查

| 症状 | 原因 | 解法 |
|---|---|---|
| `401 Unauthorized` | key 不在 `--key-map`；或上游 key 未配 | 检查 keys.json；配 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` |
| 404 / "unknown endpoint" | 协议不匹配（responses vs chat） | Codex 用 `wire_api = "responses"`；opencode 用 `@ai-sdk/openai-compatible` |
| 配置不生效 | 环境变量进程启动时读一次 | 改完配置**重启** agent |
| 文本没水印 | key 未映射 / 文本 < 50 字符 / 走了透传路径 | 核对 key_map、`--salt-archive` 是否写入 |
| salt 归档为空 | 没加 `--salt-archive`，或所有请求都 fail-open | 加上归档参数；看代理日志 `aawm.proxy` |
| 上游连接失败 | 上游 base 配错 | `curl {upstream}/v1/models` 直连验证 |
