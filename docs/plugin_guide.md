# AAWM 插件接入指南

> **一句话**：任何 Agent 接入本插件后，输出文本自动携带当前用户 ID 的水印；事后凭密钥可复原溯源到具体用户。

三种接入方式，按你的技术栈选择：

| 场景 | 接入方式 | 代码量 |
|---|---|---|
| 用 LangChain v1 构建 Agent | [方式一：LangChain 中间件](#方式一langchain-v1-中间件) | 3 行 |
| 走 LiteLLM Proxy 网关 | [方式二：LiteLLM Proxy 钩子](#方式二litellm-proxy-钩子) | 配置文件 |
| 直接用 OpenAI SDK | [方式二·五：OpenAI SDK 包装](#方式二五openai-sdk-包装) | 2 行 |
| 自研 Agent / 其他框架 | [方式三：纯 SDK 调用](#方式三纯-sdk-调用) | 5-10 行 |

---

## 前置准备

### 安装

```bash
pip install -e .                        # 基础（纯算法层）
pip install -e ".[langchain]"           # + LangChain 适配器
pip install -e ".[litellm]"             # + LiteLLM 适配器
pip install -e ".[llm]"                 # + OpenAI SDK 适配器
pip install -e ".[server]"              # + 检测服务（FastAPI）
```

### 初始化密钥和注册库

```bash
# 1. 生成 master_key（32 字节，妥善保管——丢了就无法溯源）
aawm keygen --output key.json

# 2. 注册用户（别名 → 16-bit UID，自动分配或指定）
aawm registry add "user-alice" --registry registry.json
aawm registry add "user-bob" --registry registry.json

# 3. 查看
aawm registry list --registry registry.json
```

> **密钥安全**：master_key 是溯源的唯一凭证。建议放 KMS / 环境变量，不要提交进代码库。
> `key.json` 已自动 chmod 600。

### 零感模式（v0.7 起；v0.9 中英双语）

中英文输出默认都走 `zero_cost` 模式并建议做 null 标定——嵌入对文本观感几乎
无扰动，且标定后存在性检测的误报率显著下降（实测 13/30 → 0/30）。
英文零感词典（拼写变体 + 功能副词）对通用文本命中稀疏，容量小于中文与
default 词林，适合 AI 长输出；极短英文看统计行容量。

```python
from aawm.plugins import Watermarker

wm = Watermarker.from_config(
    "key.json", "registry.json",
    codec_mode="zero_cost",                       # default / zero_cost / hybrid
    calibration="calibration.json",               # v0.12 标定文件（aawm calibrate 产出，推荐）
)
# 或现场语料标定（大语料每次构造都要拟合，慢）：
# wm = Watermarker.from_config("key.json", "registry.json",
#     codec_mode="zero_cost",
#     calibrate_corpus=open("normal_outputs.txt", encoding="utf-8").readlines())
```

标定用 `aawm calibrate ./corpus/ -o calibration.json` 一次性产出，语料是
**未加水印的正常 Agent 输出**（几十篇即可，纯文本、无结构要求）。
`hybrid` 模式额外传 `supplementary_dict={词: [同义词, ...]}` 补带，适合需要
比 `zero_cost` 更大容量的场景。`default` 模式行为与 v0.6 完全一致（向后兼容）。

**嵌入 → 溯源（自适应路径）**：

```python
result = wm.embed(agent_output, user_id="user-alice")
# 发布 result.watermarked_text
# 存档：result.session_salt + result.bands + result.n_bits   ← 三者缺一不可

if result.reliability != "high":
    # v0.12：容量分级。medium（6-9 bit）归因可能失败；low（<6 bit 或弱嵌入）
    # 结论仅供参考。短文本不会被拒嵌——照常嵌水印并标注降级，聚合多份
    # 存档仍可溯源；如需单篇可靠归因，加长文本（中文 ≥1200 字）再嵌。
    print(f"⚠ 可靠性 {result.reliability}，建议加长文本")

trace = wm.trace(suspect_text,
                 session_salt=result.session_salt,
                 bands=result.bands,        # 不传会退化到 default 检测口径
                 n_bits=result.n_bits)
if trace.watermarked and trace.attribution_abstain:
    print("检出该文档含水印，但归因置信不足——不可判定具体用户（防对抗误归因）")
```

**v0.13 可选增强**（均为向后兼容，不传行为不变）：

```python
# UID 冗余（zero_cost/hybrid）：段落裁剪 50% 仍可归因；容量代价 k_uid=k//r
result = wm.embed(agent_output, user_id="user-alice", uid_redundancy=2)
# 存档额外带 result.uid_layout，溯源时回传：
trace = wm.trace(suspect_text, session_salt=..., bands=...,
                 n_bits=..., uid_layout=result.uid_layout)

# 密钥轮换后溯源旧水印：trace 自动按 meta/EmbedResult 的 key_version 取钥，
# 也可显式指定 wm.trace(..., key_version=1)。
# 词典指纹比对：wm.trace(..., dict_version=result.dict_version)
#   → trace.dict_version_match=False 说明溯源侧词典与嵌入侧不一致，归因需谨慎。
```

CLI 等价写法：

```bash
aawm embed input.txt --key key.json --user user-alice \
      --registry registry.json --codec-mode zero_cost \
      --calibration calibration.json -o marked.txt
aawm trace marked.txt --key key.json --registry registry.json --meta marked.meta.json
```

`embed` 自动把 `bands/n_bits/capacity/codec_mode` 写进 `meta.json`，`trace
--meta` 会读取回传，无需手工维护。

---

## 方式一：LangChain v1 中间件

适合用 `langchain.agents.create_agent` 构建的 Agent。

```python
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from aawm.plugins import Watermarker
from aawm.plugins.adapters.langchain_v1 import AAWMMiddleware

# 1. 初始化（进程级，一次即可）
watermarker = Watermarker.from_config(
    key_file="key.json",
    registry_file="registry.json",
)

# 2. 挂载中间件（就这两行）
middleware = AAWMMiddleware(watermarker)

# 3. 正常创建 agent
model = init_chat_model("openai:gpt-4o")
agent = create_agent(
    model=model,
    tools=[...],
    middleware=[middleware],   # ← 挂上
)

# 用户上下文通过 LangChain 的 context 传递
result = agent.invoke(
    {"messages": [{"role": "user", "content": "写一段产品介绍"}]},
    config={"configurable": {"user_id": "user-alice"}},  # ← 用户标识
)
# agent 的输出已自动携带 user-alice 的水印
```

**行为说明**：
- 模型输出经过 `after_model` 钩子时自动嵌入水印
- `tool_calls` 非空的响应**不会**被改写（工具调用参数保持原样）
- 嵌入失败时**透传原文**（fail-open，绝不影响响应）
- 文本短于 50 字符时跳过嵌入（容量不足）

---

## 方式二：LiteLLM Proxy 钩子

适合所有流量走 LiteLLM Proxy 网关的场景（语言/框架无关）。

```python
# litellm_hooks.py —— 放在 Proxy 可加载的路径
from aawm.plugins import Watermarker
from aawm.plugins.adapters.litellm_proxy import setup_hooks

setup_hooks(
    Watermarker.from_config(
        key_file="key.json",
        registry_file="registry.json",
    )
)
```

```yaml
# proxy_config.yaml
model_list:
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY

litellm_settings:
  callbacks: litellm_hooks   # ← 挂载钩子模块
```

**用户标识传递**：通过虚拟 key 的 metadata。

```bash
# 给 user-alice 发一个虚拟 key
litellm --config proxy_config.yaml &
litellm-admin create_key \
  --user-id user-alice \
  --metadata '{"user_id": "user-alice"}'
```

之后所有用这个 key 的请求，响应自动带 user-alice 的水印。

**流式响应**：`async_post_call_streaming_iterator_hook` 会做**句子级缓冲重写**——缓冲到句末标点，整句嵌入后释放。用户看到的流式效果略有延迟（一句一句出），但每句都已带水印。

---

## 方式二·五：OpenAI SDK 包装

适合直接使用 `openai.OpenAI()` / `openai.AsyncOpenAI()` 的应用（不经过 LangChain/LiteLLM）。

```python
from openai import OpenAI, AsyncOpenAI
from aawm.plugins import Watermarker
from aawm.plugins.adapters.openai_v1 import (
    wrap_openai_client,
    wrap_async_openai_client,
)

watermarker = Watermarker.from_config("key.json", "registry.json")

# 同步客户端
client = wrap_openai_client(OpenAI(), watermarker)
resp = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    user_id="user-alice",      # ← 用户标识（也支持 headers/contextvars/env）
)
print(resp.choices[0].message.content)  # 已自动嵌水印

# 异步客户端
async_client = wrap_async_openai_client(AsyncOpenAI(), watermarker)
resp = await async_client.chat.completions.create(
    model="gpt-4o", messages=[...], user_id="user-bob",
)
```

**行为说明**：
- `wrap_*` 是纯 duck-typing 包装（import 无需安装 openai），包装后 `create` 返回的 `message.content` 自动嵌水印
- `stream=True` 时自动包装流，**句子级缓冲重写**（与方式二一致）
- `user_id` 缺省时按 ContextChain 三级解析（请求头 `X-AAWM-User-Id` / contextvars / 环境变量）
- 同样 fail-open：任何嵌入异常都透传原文

---

## 方式三：纯 SDK 调用

适合自研 Agent 或任何 Python 后处理场景。

### 非流式（最简）

```python
from aawm.plugins import Watermarker

watermarker = Watermarker.from_config("key.json", "registry.json")

# Agent 拿到 LLM 输出后：
result = watermarker.embed(llm_output, user_id="user-alice")
print(result.watermarked_text)   # ← 发布这个
print(result.session_salt)       # ← 存档（溯源时需要，可公开）
print(result.seal)               # ← 存档（篡改检测用）
```

### 流式（句子级缓冲）

```python
from aawm.plugins import Watermarker, WatermarkMiddleware, StreamingWatermarker, Context

mw = WatermarkMiddleware(Watermarker.from_config("key.json", "registry.json"))
streamer = StreamingWatermarker(mw)
ctx = Context(user_id="user-alice")

for delta in llm_stream():
    output = streamer.feed(delta, ctx)   # 缓冲到句末，返回已嵌入的整句
    print(output, end="", flush=True)

print(streamer.flush(), end="")          # 流结束，输出剩余缓冲
```

### 自研框架的上下文注入（contextvars）

```python
from aawm.plugins import set_user_context, reset_user_context

# 请求入口（中间件/装饰器里）
tokens = set_user_context(user_id="user-alice", session_id="sess-123")
try:
    response = handle_request(request)
    # 响应处理时 EnvVarContextProvider 会自动从 contextvars 读到 user_id
finally:
    reset_user_context(tokens)
```

---

## 溯源（验证方操作）

### SDK 溯源

```python
from aawm.plugins import Watermarker

watermarker = Watermarker.from_config("key.json", "registry.json")

# 需要嵌入时存档的 session_salt（从你的发布记录里取）
trace = watermarker.trace(
    suspect_text,
    session_salt=stored_salt,   # 可选，但强烈建议
    seal=stored_seal,           # 可选，篡改检测用
    bands=stored_bands,         # v0.7：零感模式必须回传（embed 返回）
    n_bits=stored_n_bits,       # v0.7：零感模式建议回传
    archived_uid=stored_uid,    # v0.11：嵌入时存档的 UID（盐外证据，见下）
)

print(trace.watermarked)     # True/False —— 有没有水印
print(trace.uid)             # 解码出的 UID
print(trace.user)            # 注册库最近邻匹配的用户别名
print(trace.confidence)      # 置信度 [0,1]
print(trace.tampered)        # None=无seal无法判定 / True=被篡改 / False=未篡改
print(trace.tampered_paragraphs)  # 被改段落索引
```

**盐外证据（v0.11，防路径依赖）**：攻击下"检出"本质盐无关——错误盐/多条
盐扫描也会检出一个存在性信号，但解码 UID 常失真；若只按解码结果归因，
裸 API 消费者（不持 meta、不查库直接调 `trace()`）会把文本归给错误用户。
嵌入时把 UID 存档（`result.user_id`），溯源时回传 `archived_uid`：
trace 会做解码 UID 与存档 UID 的交叉校验，不一致即 `attribution_abstain=True`
且 `uid/user=None`——**宁可 abstain 也不输出可能错误的归因**。
CLI `find-meta` / server `/v1/trace` 持 meta 时自动完成该校验，无需手工传。

#### 软判决匹配（v0.7；v0.10 起默认启用）

**v0.10 起 `soft_match=True`、`match_margin_ratio=0.3` 是默认值**——
溯源默认走软判决注册库匹配（逐带 z 打点积分，弱证据带参与），
30% 同组改写攻击下匹配率 20→27/30。以下写法为显式风格（与默认等价）：

```python
trace = watermarker.trace(
    suspect_text,
    session_salt=stored_salt,
    soft_match=True,      # v0.10 起为默认，可省略
    match_margin=2.0,     # 最优-次优得分差下限（低于则 abstain，uid=None）
    match_margin_ratio=0.3,   # v0.10 起为默认自适应置信系数
)
print(trace.soft_gap)     # 最优-次优得分差；gap<生效 margin 说明置信不足需复核
```

**软判决拒绝（v0.10，行为变更）**：margin 门限拒绝（soft_uid=None）时
不再回退硬解码 UID——攻击下存在性常存活但解码不可靠，硬解码正是
"高置信度错误归因"的来源。此时 `attribution_confidence=0` 触发 abstain，
`uid/user` 置 None。

**自适应置信阈值（v0.8）**：长文本（词典词上千）下 gap 的统计尺度
随 √n_dict 增长，固定 `match_margin=2.0` 会偏松——重度改写后错误
匹配的 gap 可能仍超 2.0（"自信地错"）。传 `match_margin_ratio` 可让
生效阈值变为 `max(match_margin, ratio·√n_dict)`，短文本由绝对项主导、
长文本由比例项主导：

```python
trace = watermarker.trace(
    suspect_text,
    session_salt=stored_salt,
    soft_match=True,
    match_margin=2.0,
    match_margin_ratio=0.5,   # 长文本/重度攻击场景推荐
)
```

实测错误匹配的 gap/√n_dict 上界跨语料稳定 ≈0.22，正确匹配均值
0.5~0.7；ratio 是"宁可 abstain 也不错"的权衡旋钮——ratio 越高
错误越少但 abstain 越多（0.5 时 s50/pku 错误清零，s30 召回 19→8）。

注意：软判决只提升"有损文本的归属能力"，不改变存在性判定；
`trace.watermarked` 仍由 Σ|z| 阈值决定（未嵌水印文本不会被误归因）。

#### 归因置信度与 abstain（v0.10，对抗防御）

对抗场景最危险的失败模式是**高置信度错误归因**——存在性存活（`watermarked=True`）
但 UID 解错，仍输出一个错误的具体用户。为此 trace 提供独立于存在性
`confidence` 的归因分数：

```python
print(trace.attribution_confidence)  # [0,1]，=判别力×容量充分性
print(trace.attribution_abstain)     # True=检出但归因置信不足，uid/user 已置 None
```

- **判别力**：软判决路径用 `gap/√n_dict` 线性映射（≤0.22→0，≥0.4→1）；
  margin 门限拒绝时直接 0。硬判决路径按汉明距映射。
- **容量充分性**：自适应 k-bit 空间内注册库 UID 掩码碰撞（如 n_bits=6 下
  UID 1 与 65 均 mask 成 1）时二者数学上不可区分，cap=0 一票否决。
- `attribution_confidence < 0.5` 时 abstain：`uid/user/hamming_dist` 置空，
  CLI 退出码 3、server 返回"不可判定"——**宁可不说也不说错**。

### CLI 溯源

```bash
aawm trace suspect.txt \
  --key key.json \
  --registry registry.json \
  --meta marked.meta.json    # 嵌入时生成的元数据（含 salt+seal）
```

### HTTP 溯源服务

```bash
aawm serve --key key.json --registry registry.json --port 8765
```

```bash
curl -X POST http://localhost:8765/v1/trace \
  -H "Content-Type: application/json" \
  -d '{"text": "嫌疑文本...", "session_salt": "<hex>"}'
```

响应：
```json
{
  "watermarked": true,
  "uid": 4660,
  "user": "user-alice",
  "hamming_dist": 1,
  "confidence": 0.45,
  "tampered": false,
  "existence_score": 18.1,
  "attribution_confidence": 0.83,
  "attribution_abstain": false
}
```

abstain 时（归因置信不足）`uid`/`user` 为 `null`、`attribution_abstain=true`，
调用方应输出"不可判定用户"而非猜测。

---

## 判决矩阵

拿到 `TraceResult` 后的判定逻辑：

| watermarked | uid/user 匹配 | attribution_abstain | tampered | 结论 |
|---|---|---|---|---|
| True | user 有值 | False | False | **高置信归属**：文本是 `user` 的，未被篡改 |
| True | user 有值 | False | True | **篡改确认 + 溯源**：文本被改过，但仍可归属到 `user` |
| True | uid/user 为 None | True | — | **不可判定**：检出但归因置信不足（低容量掩码碰撞 / margin 拒绝），不要猜测用户 |
| True | user 为 None | False | — | 有水印但 UID 偏差大（重度改写）；考虑注册库扩容或降低 max_hamming |
| False | — | — | — | 无水印（或改写超过 75%）；非本密钥体系产出 |

---

## 常见问题

**Q: 嵌入会改变文本多少？**
A: 只做同义词替换（如 platform→system, big→large），语义不变。英文典型替换率 5-15%，中文更低。句子感知设计保证替换不跨句。

**Q: 多用户同文本会怎样？**
A: 不同 user_id 产生不同的同义词选择，水印文本不同。这是设计特性——泄露后可区分源头。

**Q: session_salt 必须存吗？**
A: 强烈建议。没有 salt 时存在性检测精度下降（绿名单派生自 salt）。salt 可公开（不是秘密），随发布记录存档即可。

**Q: 水印能抗多强的改写？**
A: 实测（AI 文本）：存在性检测抗 75% 改写（TPR 100%）；UID 精确还原抗 10% 改写，30% 改写时注册库最近邻匹配可纠错（中文 73%）。详见 `docs/ai_text_judgment_report.md`。

**Q: 支持 Markdown/JSON 输出吗？**
A: 嵌入只替换自然语言词汇，不碰代码块、JSON 键名。但建议对结构化输出（纯 JSON）关闭水印——词典命中低且可能破坏严格校验。

**Q: 中英文混合文本？**
A: 自动语言检测（含 CJK 判 zh）。混合文本按主语言处理，英文部分用英文词典、中文部分用中文词典分别嵌入。
