# Acrostic Agent Watermark (AAWM)

**v0.6 通用 Agent 插件** —— 把水印能力封装为 SDK 中间件，任意 Agent 接入 3 行代码即可自动嵌入用户 ID 水印，实现事后溯源。

> **v0.6 快速接入**（3 行代码）：
> ```python
> from aawm.plugins import Watermarker
> wm = Watermarker.from_config("key.json", "registry.json")  # 一次性初始化
> result = wm.embed(agent_output, user_id="user-cuiyin")       # 嵌入水印
> trace = wm.trace(suspect_text, session_salt=result.session_salt)  # 溯源
> ```
> 详见 [docs/plugin_guide.md](docs/plugin_guide.md)。

---

> **定位声明**：本项目做的是 **agent 级**水印，而非 **model 级**水印。
> - Model 级水印（如 KGW / SynthID-Text / Aaronson）：在 LLM 生成阶段修改 logits，水印嵌在"模型采样过程"里，需要一个被改造过的 LLM 才能产出水印文本。
> - Agent 级水印（本项目）：agent 拿到任意 LLM 的原始输出后，**自主选择若干 token 做轻量变换**，使验证者凭密钥能如藏头诗般读出隐藏信号——**包括这段输出属于哪个用户**。LLM 本身不需要任何修改，水印来自 agent 的"编排层"而非"模型层"。

## 为什么需要"agent 级"水印

| 维度 | Model 级水印 | **Agent 级水印（本方案）** |
|---|---|---|
| 嵌入主体 | LLM 推理引擎 | Agent 编排层（工具调用前后处理） |
| 依赖模型改造 | 强依赖（需改 logits / 自定义采样） | 不依赖（黑盒 API 即可） |
| 闭源 API 可用 | 否（OpenAI/Anthropic 不开放 logits） | **是** |
| 谁可嵌入 | 模型 owner | **任意 agent 开发者** |
| 水印归属 | 模型身份 | **agent 实例身份** |
| 验证者需要的访问 | tokenizer + 哈希密钥 | 密钥（+ 可选 tokenizer） |
| 防伪范围 | "这段文本是某模型生成的" | **"这段输出是某 agent 实例产出的"** |

关键差异：当多个 agent 共用同一个底层 LLM（如都调 GPT-4o），model 级水印只能证明"这段文本来自 GPT-4o"，无法区分是 agent A 还是 agent B 产出；agent 级水印可以精确到**实例**，支撑多 agent 系统里的归因、审计、追责。

## "藏头诗"比喻的工程化

传统藏头诗：每句首字母对齐密钥序列，拼出隐藏信息。本项目把这个思路推广到 **token 层面的可验证变换**，并进一步做到**每用户唯一**：

1. **位置选择**：agent 用密钥派生一组"锚点位置"（在可表达同义替换的词位上）
2. **符号映射**：每个锚点位置派生一个密钥控制的"首字母 → bit"映射（26 字母伪随机 13/13 二分）
3. **编码嵌入**：用户 ID → CRC-8 → 纠错码 → 每锚点 1 bit，agent 在锚点处选择映射到目标 bit 的同义词
4. **解码溯源**：验证者重算锚点与映射，读出 bit 序列 → 纠错 → CRC 校验 → **还原用户 ID**

同一文本发给不同用户 → 不同的同义词选择 → 不同的水印文本；泄露后可解码溯源到具体用户。

## 快速开始

### v0.6 通用 Agent 插件（推荐）

```bash
# 安装
pip install -e .

# 初始化密钥和注册库
aawm keygen -o key.json
aawm registry add key.json reg.json --alias agent-cuiyin

# CLI 嵌入 + 溯源
aawm embed input.txt --key key.json --user agent-cuiyin --registry reg.json -o marked.txt
aawm trace marked.txt --key key.json --registry reg.json --meta marked.txt.meta.json
```

Python SDK 3 行接入：

```python
from aawm.plugins import Watermarker

wm = Watermarker.from_config("key.json", "registry.json")
result = wm.embed(agent_output, user_id="agent-cuiyin")
# 发布 result.watermarked_text，存档 result.session_salt + result.seal

# 事后溯源
trace = wm.trace(suspect_text, session_salt=result.session_salt)
if trace.watermarked:
    print(f"泄露源自用户 {trace.user} (置信度 {trace.confidence:.2f})")
```

LangChain 适配器（1 行接入）：

```python
from aawm.plugins.adapters.langchain_v1 import AAWMMiddleware

# 在 Agent middleware 链中加入 AAWMMiddleware，自动对输出嵌水印
```

LiteLLM Proxy 适配器：

```python
from aawm.plugins.adapters.litellm_proxy import setup_hooks
setup_hooks(watermarker)  # 注册全局 hook，所有 LLM 调用自动嵌水印
```

OpenAI SDK 适配器（直接包装客户端，同步/异步/流式都支持）：

```python
from aawm.plugins.adapters.openai_v1 import wrap_openai_client
client = wrap_openai_client(openai.OpenAI(), watermarker, on_embed=archive_salt)
resp = client.chat.completions.create(..., user_id="user-alice")
# resp.choices[0].message.content 已自动嵌水印
```

AutoGen / CrewAI 适配器（2026 主流多智能体编排）：

```python
from aawm.plugins.adapters.autogen_v1 import wrap_autogen_agent
agent = wrap_autogen_agent(AssistantAgent(...), watermarker, user_id="alice")

from aawm.plugins.adapters.crewai_v1 import setup_hooks
setup_hooks(watermarker, user_id="alice")   # crew.kickoff() 输出自动嵌水印
```

> 所有适配器都是 **Fail-open**（嵌入失败绝不影响 agent 响应），且支持
> `on_embed` 回调存档 session_salt——中间件嵌入模式下溯源的前提。
> 低代码平台（Dify/Coze）接入方案见 agent 嵌入指南。
> Claude Code / Codex / opencode / WorkBuddy / Antigravity / PI / Qwen Code
> 等 CLI/IDE agent 是黑盒进程，只需把 base_url 指向本地代理网关即可零改造接入。
> 见 [docs/cli_agent_proxy_guide.md](docs/cli_agent_proxy_guide.md)。
>
> 对 agent **产出的落盘交付物**（报告/文档/代码）按需/自动打标：直接用
> [skills/aawm-watermark](skills/aawm-watermark) 技能包——SKILL 指令 +
> `embed_files.sh`/`trace_file.sh` 脚本 + Claude Code PostToolUse 自动触发 hook。

> 详见 [docs/agent_embedding_guide.md](docs/agent_embedding_guide.md) | [docs/plugin_guide.md](docs/plugin_guide.md) | [docs/api_reference.md](docs/api_reference.md) | [docs/deployment.md](docs/deployment.md) | [docs/cli_agent_proxy_guide.md](docs/cli_agent_proxy_guide.md) | [skills/aawm-watermark](skills/aawm-watermark)

### v0.7 中文零感水印（codec 模式）

中文场景默认 `zero_cost` 模式（词典小、嵌入对文本观感几乎无扰动）；需要更大容量时用 `hybrid` 或 `default`：

```python
from aawm.plugins import Watermarker

# 零感模式 + null 语料标定（显著降低误报）
wm = Watermarker(codec_mode="zero_cost",
                 calibrate_corpus=["正常输出文本1", "正常输出文本2", ...])

result = wm.embed(agent_output, user_id=42)
# 发布 result.watermarked_text；存档 session_salt + bands + n_bits
# （trace 时三者回传，缺 bands 会退化到 default 阈值，检测口径不同）

trace = wm.trace(suspect_text,
                 session_salt=result.session_salt,
                 bands=result.bands,
                 n_bits=result.n_bits)
if trace.watermarked:
    print(f"泄露源自用户 {trace.user or trace.uid}  "
          f"(存活带 {trace.active_bands}/{trace.capacity})")
```

标定语料是**未加水印的正常输出**（几十篇即可）。CLI 等价命令：

```bash
aawm embed input.txt --key key.json --user 42 \
      --codec-mode zero_cost --calibrate-corpus ./corpus/ -o marked.txt
aawm trace marked.txt --key key.json --meta marked.txt.meta.json \
      --codec-mode zero_cost --calibrate-corpus ./corpus/
```

> 容量 < 用户 UID 位数时，解码 UID 为低位截断值（如 42 → 0x000A），这是 k-bit 语义，配合注册库 `--registry` 的 soft_match 可映射回全宽 UID。

### v0.4 核心算法 API（底层）

核心 API（v0.4 内容寻址 + 句子感知，推荐）：

```python
from aawm import CAEmbedder, CADecoder, CAConfig, generate_master_key

key = generate_master_key()
embedder = CAEmbedder(key)  # 默认 v0.4: sentence_aware=True, language="en"
decoder = CADecoder(key)

# agent 为用户 42 的输出生成水印
result = embedder.embed(agent_output_text, user_id=42)
# 发布 result.watermarked_text，存档 result.session_salt

# 验证方溯源（容忍插入/删除/同义替换/部分 paraphrase）
d = decoder.decode(suspect_text, session_salt)
if d.success:
    print(f"泄露源自用户 {d.user_id}")   # 42
```

中文水印（v0.4 新增）：

```python
from aawm import CAEmbedder, CADecoder, CAConfig, generate_master_key

key = generate_master_key()
cfg = CAConfig(language="zh", min_anchorable=20)  # 声母谓词 + 中文词典
embedder = CAEmbedder(key, cfg)
decoder = CADecoder(key, cfg)

result = embedder.embed("这是一段需要加水印保护的中文文本...", user_id=42)
d = decoder.decode(result.watermarked_text, result.session_salt)
assert d.success and d.user_id == 42
```

v0.2 API（`Embedder` / `Decoder`，位置索引锚点）仍可用，供对比实验。

详见 [docs/design.md](docs/design.md)。

## 项目状态

✅ **v0.7 中文零感 / 混合词典模式（266 项测试通过）**：
- **三种 codec 模式**：`default`（全词林，向后兼容）/ `zero_cost`（零感词典）/ `hybrid`（零感打底 + 补充词表补带）
- **零感词典**：75 组常用双字词（"不仅→不但/不只/不只是"），嵌入对文本观感几乎无扰动
- **自适应编解码**：`embed_adaptive / detect_adaptive / soft_match_adaptive`，容量按文本活动词自动伸缩
- **k-bit 容量语义**：UID 编码在 `n_bits` 位空间，容量不足时取低 `n_bits` 位（配合注册库 soft_match 映射回全宽 UID）
- **embed 自检重试**：嵌入后回验解码 UID + 信号余量 ≥1.5×阈值，自动换盐挑选强信号
- **null 语料标定**：`--calibrate-corpus` 用每带 ratio 模型（Σ|z|/m）5-salt 3σ 拟合 null 阈值，显著降低误报
- **CLI / HTTP 端到端**：meta.json 携带 `bands/n_bits/capacity`，trace 时回传即可精确溯源
- **测试覆盖**：Facade 级 e2e（往返/冗余/标定/注册库 soft_match）+ server 级自适应往返

✅ **v0.6 通用 Agent 插件已实现（204 项测试通过）**：
- **Watermarker Facade**：统一 API 封装 GreenlistCodec + DocumentBinder + UIDRegistry
- **Fail-open 中间件**：任何嵌入异常 → 透传原始文本，绝不阻断 Agent 响应
- **LangChain v1 适配器**：`AgentMiddleware` hook，`after_model` 自动嵌入
- **LiteLLM Proxy 适配器**：全局 hook，非流式 + 流式均支持
- **CLI 工具**：`aawm keygen/registry/embed/trace/serve` 全流程命令行
- **FastAPI 检测服务**：HTTP API 提供远程溯源能力
- **UID 注册库**：16-bit UID（65536 用户），最近邻匹配纠错（Hamming ≤ 3）
- **自适应存在性阈值**：`max(8.0, 2.0 × √n_dict_words)`，适配变长文本
- **上下文解析链**：Framework → EnvVar/contextvars → HTTP headers 三级优先
- **句子级流式水印**：缓冲到句末标点，整句嵌入后释放

✅ v0.4 句子边界感知指纹 + 中文支持：
- 句子边界感知指纹：句首词左邻 `_BOS`、句末词右邻 `_EOS`，重写单句只损失该句的票，不污染邻句锚点
- 词典扩充：926 → 2363 词条（3.4 倍），621 稳定化组
- 中文支持：声母谓词（23 声母）+ 前向最大匹配分词 + 中文同义词典，零强依赖
- LanguageAdapter 抽象：英文/中文统一接口，`CAConfig(language="zh")` 切换
- paraphrase 评测 + 句级统计量验证（统计量保留性不足，降级为置信度信号）

✅ v0.3 内容寻址锚点：
- 锚点身份 = 局部上下文指纹（同义组 ID 构造，替换不变）
- 投票桶信道：每锚点独立投票 payload 位，桶内多数表决 + CRC + 弱桶 chase
- 编辑局部性：插入/删除 10 词存活 30/30（v0.2 为 0/30）；30 次混合编辑存活 24/30
- 嵌入 skip 严格为零（多密钥验证）

✅ v0.2 用户 ID 编码水印：
- 16 bit 用户 ID + 8 bit CRC + 交织重复码纠错（spread3 / hamming74 可选）
- 密钥派生符号映射（KeyedLetterMap，防合谋统计 / 防 framing）
- 926 词条稳定化同义词典

详见 [docs/design.md §9-12](docs/design.md)。

## 目录结构

```
acrostic-agent-watermark/
├── README.md                  # 本文件
├── pyproject.toml             # 包配置（v0.7.0，含 CLI entry point）
├── docs/
│   ├── research_notes.md      # 起步研究：领域扫描、相关工作、差异化定位
│   ├── design.md              # 设计文档：架构、算法、威胁模型、v0.2-v0.5
│   ├── plugin_guide.md        # v0.6 插件集成指南（3 场景 + FAQ）
│   ├── deployment.md          # v0.6 部署运维（密钥管理 + systemd + 监控）
│   ├── performance.md         # v0.6 性能基准（600 词 8.1ms 嵌入）
│   ├── api_reference.md       # v0.6 API 参考（全部插件层类 + CLI + HTTP）
│   └── capability_examples.md # 双语能力边界示例
├── src/
│   └── aawm/
│       ├── __init__.py        # v0.7.0，lazy-loading 插件符号
│       ├── keys.py            # 密钥派生（HKDF-SHA256）
│       ├── coding.py          # 信道编码：CRC-8 / 重复码 / 交织重复码 / 汉明(7,4)
│       ├── greenlist.py       # 绿名单编解码器（信道B：16 带统计溯源；自适应路径）
│       ├── collocation.py     # v0.7 搭配词约束（boundary_safe 边界稳定性）
│       ├── data/zh_zero_cost.json  # v0.7 零感词典（75 组双字词）
│       ├── binding.py         # DocumentBinder（信道A：Merkle-HMAC 段落绑定）
│       ├── content.py         # 内容寻址锚点 + 句子边界感知
│       ├── cli.py             # v0.6 CLI 工具（keygen/registry/embed/trace/serve）
│       ├── server/
│       │   └── api.py         # v0.6 FastAPI 检测服务
│       └── plugins/           # v0.6 插件层
│           ├── facade.py     # Watermarker Facade（核心 API）
│           ├── middleware.py  # Fail-open 中间件
│           ├── keystore.py   # 密钥管理（memory/file/env）
│           ├── registry.py   # UID 注册库（最近邻匹配纠错）
│           ├── context.py    # 上下文解析链（3 级优先）
│           ├── streaming.py  # 句子级流式水印
│           └── adapters/
│               ├── openai_v1.py      # OpenAI SDK 包装（同步/异步/流式）
│               ├── langchain_v1.py   # LangChain Agent 适配器
│               ├── litellm_proxy.py  # LiteLLM Proxy 适配器
│               ├── autogen_v1.py     # AutoGen (agentchat) 适配器
│               └── crewai_v1.py      # CrewAI LLM hooks 适配器
├── tests/                     # 272 项测试（核心 + 插件 + 适配器）
├── examples/
│   ├── 01_minimal_embed.py    # 嵌入并解码用户 ID
│   ├── 02_multiuser_robustness.py  # 多用户区分 + 攻击鲁棒性
│   ├── 03_edit_robustness.py  # 内容寻址 vs 位置索引的编辑攻击对比
│   ├── 05_plugin_quickstart.py # v0.6 插件端到端示例
│   └── 06_agent_demo.py       # v0.7 真实 agent 端到端泄露溯源演示
├── experiments/
│   ├── exp_edit_attacks.py    # 编辑攻击系统评测 + paraphrase 评测
│   └── exp_sentence_stats.py # v0.4 句级统计量保留性验证
└── benchmarks/                # 基准评测（robustness, capacity, quality）
```

## 许可证

待定（拟 MIT 或 Apache-2.0）。
