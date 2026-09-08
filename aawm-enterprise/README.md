# aawm-enterprise

AAWM 企业组件包（独立许可，非 MIT）。

> **边界声明（先读边界再谈能力）**
> - **只保护最终交付物**：聚合缓冲层把同一交付任务内的片段攒到文档成型点
>   整篇嵌入一次。**会话中途流出的片段不在此保护范围**——那是 MIT 核心
>   逐调用/流式整流的职责，两者按泄露窗口互补而非替代。
> - **短最终文档如实分级**：聚合后仍容量不足（k<10）时 `reliability` 如实
>   返回 `medium/low` 并附警告，不硬拒、不掩盖。
> - **护栏能力永不进商业层**：reliability 分级 / abstain / 弱嵌入警告全部
>   来自 MIT 核心，本包零算法发明、零判决逻辑覆盖。
> - 本包采用 **BUSL-1.1 + Additional Use Grant**（2028-09-08 起转为
>   Apache-2.0），依赖 MIT 核心 [acrostic-agent-watermark](https://github.com/kylecui/acrostic-agent-watermark)。

## 组件一：聚合水印缓冲层（AggregateWatermarker）

**解决什么**：agent 工作流把产出分成多次短调用（100–500 字/次），逐调用嵌入
每段 `reliability=low/medium`，短文本归因失败率高。聚合层把同一交付任务的
片段攒起来，在文档成型点整篇嵌入一次——k≥10（`reliability=high`），且
`uid_redundancy`（默认 3，可配置）让段落裁剪泄露仍可溯源（crop50 实测 5/5）。

### 快速开始

```python
from aawm import Watermarker
from aawm.plugins.context import Context
from aawm_enterprise import AggregateWatermarker

core = Watermarker(language="zh", codec_mode="zero_cost")
agg = AggregateWatermarker(core)          # uid_redundancy=3 可配

ctx = Context(user_id="agent-alice", session_id="task-42")
for chunk in agent_run():                 # 多次 LLM/工具调用
    agg.append("task-42", chunk, ctx)     # 原文透传，不拖延
flushed = agg.close("task-42")            # 拼整篇 → 预检 → 冗余嵌入 → meta
assert flushed.result.reliability == "high"
save_deliverable(flushed.watermarked_document)
```

### proxy 会话端点（可选，需 `pip install aawm-enterprise[proxy]`）

```python
from aawm_enterprise.proxy_ext import mount_enterprise
mount_enterprise(proxy_app, agg)   # 挂载 POST /v1/aawm/buffer/{session}[/close]
```

### A/S 模式护栏

同一 `(session, user_id)` 的流量只能走一种水印模式：`full`（请求级全文后嵌，
经核心 proxy `streamer_factory` 注入）或 `sentence`（句子级整流）。混用会产生
两套 bands，trace 语义错乱——护栏对 buffer 端点做单向检查，违反返回 409。

## 测试

```bash
pip install -e "aawm-enterprise[dev]"
pytest aawm-enterprise/tests/ -q
```

## 许可

BUSL-1.1（见 [LICENSE.enterprise](LICENSE.enterprise)）。核心 `aawm` 保持 MIT。
