# 聚合水印缓冲层（aawm-enterprise 组件一）设计

> **状态**：Phase 1 定稿 v0.2（2026-09-08）· 作者已拍板（§10 拍板记录）
> **决策关联**：PRODUCT_GAP_RECOMMENDATIONS.md §8（决策二）+
> commercialization_path.md §4.2/§5 Phase 1
> **许可约束**：独立目录 + 独立许可（BUSL-1.1，已定稿 §7），单向依赖
> 核心（本包 import `aawm`，`aawm` 绝不 import 本包）

---

## 1. 结论先行

**聚合水印缓冲层 = 会话级文档延迟嵌入器**，不是"拼长再拆短"。

**它的职责边界（作者确认）**：只处理 **agent 最终交付物的水印**——同一交付
任务内产生的多次短片段攒起来，在文档成型点拼成整篇嵌入一次，产出带水印的
最终文档。**不提供"边写边护"**：会话中途流出的片段不在本组件保护范围，
那是 MIT 核心逐调用/流式整流层的职责，两者按泄露窗口互补而非替代。

它解决的能力窗口短板是：agent 工作流把产出**分成多次短调用**（每次 100–500 字），
逐调用嵌入 → 每段 `reliability=low/medium`（k<10），七轮验证显示短文本归因
失败率高。聚合层在文档成型点一次嵌入整篇——整篇容量 k≥10（`reliability=high`），
且 v0.13 的 UID 冗余布局让**泄露方哪怕只外泄文档的段落裁剪，溯源仍存活**
（crop50 实测 5/5）。

### 为什么"拼长拆短"是错的（设计否决记录）

直觉方案"把短输出拼长嵌入、拆回各段返回给调用方"有三个致命问题：

1. **零感词典命中稀疏**：拼装后整篇有票，但拆回的单段可能整段无同义词命中
   → 无票段 trace 不到，等于没保护；
2. **历史输出不可改**：非流式适配器在片段产生时已把原文交给调用方，缓冲层
   无法事后把 marked 文本塞回去；
3. **bands/salt 按整篇计算**：拆回单段与整篇元数据错位，检测语义失真。

→ 唯一自洽的产品形态：**在交付物层面延迟嵌入**，让泄露物（导出的完整文档、
转发的报告）整体带水印。这与项目已拍板的"长文档交付物防泄露"叙事完全同构
（README 第一屏场景），是 C 形态 + B 叙事的商业层自然延伸。

---

## 2. 技术根基（全部来自 MIT 核心，商业层零算法发明）

| 核心能力 | 出处 | 聚合层怎么用 |
|---|---|---|
| `Watermarker.embed(text, uid, uid_redundancy=r, ...)` | facade.py | 整篇嵌入时 r>1，段落裁剪下 UID 存活 |
| `EmbedResult.reliability`（high/medium/low） | facade.py（v0.12） | flush 后的如实分级输出，不掩盖 |
| `Watermarker.estimate_capacity(text)` | facade.py | **守门员**：flush 前预检聚合文本容量，决定"够不够嵌 / 嵌哪档" |
| `reliability_tier(k, weak)` 分级锚点 | facade.py | k≥10→high 作为默认 flush 目标 |
| `calibrate_corpus` / `calibration` 标定 | facade.py | 生产部署先标定，阈值才准（与核心同约定） |
| `Context.session_id` | plugins/context.py | **聚合 key 的语义锚点**（现已存在但未参与盐派生） |
| `meta_store`（File/SQLite） | meta_store.py | 存档 salt/bands/key_version/dict_version/uid_layout |
| `audit_sdk`（source=sdk） | audit.py | 商业层调 wm.embed 自动留痕（§6 有边界问题） |

**不发明、不覆盖**：reliability 分级、abstain、弱嵌入警告全是核心的安全护栏，
聚合层只做"何时把片段聚成一篇、何时嵌、交付什么"的编排，绝不重写判决逻辑。

---

## 3. 组件边界与架构

### 3.1 包结构（独立仓库目录，独立发布）

```
aawm-enterprise/                  # 独立包（git 子目录或独立 repo，二选一）
├── LICENSE.enterprise            # BUSL-1.1（§7，作者拍板后定稿）
├── pyproject.toml                # name = "aawm-enterprise"；dependencies = ["acrostic-agent-watermark>=0.13.1"]
├── src/aawm_enterprise/
│   ├── __init__.py
│   ├── buffer.py                 # SessionBuffer：会话片段累积 + 容量预检
│   ├── aggregator.py             # AggregateWatermarker：flush 编排 + embed + meta 存档
│   └── policy.py                 # FlushPolicy：触发策略（容量/空闲/关闭/超限）
├── tests/                        # 独立测试（demo corpus 标定，见 §8）
└── README.md                     # 边界声明前置（聚合前泄露不护）
```

依赖方向（铁律，CI 检查）：`aawm_enterprise → aawm`，禁止反向 import。
MIT 主包（`src/aawm/`）保持零改动——若本设计需要核心小改（如 audit source
扩展，§6），作为独立 MIT PR 进核心，不进商业层。

### 3.2 核心数据流

```
 agent 片段 ──append(text, ctx)──► SessionBuffer[session_id]
                                      │ 原文透传返回（不嵌，不拖延输出）
                                      │ 片段累积 + 行号/段落边界记录
    flush 触发（容量达标 / 空闲超时 / doc.close() / 超限）
                                      ▼
                          AggregateWatermarker.flush(session_id)
                                      │ 1. 拼接全文（保留段落边界）
                                      │ 2. 预检 estimate_capacity（守门员 A）
                                      │ 3. embed(uid, uid_redundancy=r, sign=True)
                                      │ 4. EmbedResult 如实分级（high/medium/low）
                                      ▼
                    (watermarked_document, EmbedResult)
                                      │ 5. meta 存档（salt/bands/... 经 meta_store 或 on_embed 回调）
                                      ▼
                          交付给文档落盘/导出通道（调用方写文件）
```

### 3.3 会话粒度与 key

- **key = (session_id, user_id)**。`session_id` 复用 `Context.session_id`——
  语义即"一个交付任务的上下文"；缺省时组件退化为**单片段直嵌**（等同现
  有 MIT 行为），保证"不知道会话边界也能用，只是没聚合收益"。
- session_id 来源（与核心三级 context 链一致）：自研 Agent 调
  `set_user_context` / 框架 request context / HTTP 头 `X-AAWM-Session-Id`
  （proxy 增强场景，§5.2）。
- **会话生命周期**：`open(session_id)` 隐式于首次 append；`close(session_id)`
  显式收官（agent 工作流在文档导出点调用）；空闲超时自动 close（防泄漏悬挂）。

---

## 4. 行为细节

### 4.1 `append(text, ctx) -> str`

- 原文**透传返回**（与 streaming 整流不同：不做句子级即时嵌入——本层是
  文档级，输出通道不要求延迟）。
- 片段按 append 顺序累积；记录每个片段的 (起始行, 终止行) 段落边界，供
  flush 后把 marked 文档与原文做**段级对齐**（可选 diff 报告）。
- 幂等：同 (session_id, sha256(text)) 的重复 append 去重（agent 重试防双写）。

### 4.2 Flush 触发（FlushPolicy，可组合）

| 触发器 | 参数 | 说明 |
|---|---|---|
| 容量达标 | `target_capacity`（默认 10） | 预检 k≥10（high 档）即 flush——**A 守门** |
| 显式关闭 | `close(session_id)` | 文档成型点调用，最强语义 |
| 空闲超时 | `idle_timeout_s`（默认 300） | 会话静默超时自动收官，防悬挂缓冲 |
| 超限保护 | `max_buffer_chars`（默认 200k） | 防无限内存累积（到限强制 flush） |

**容量不足时的出口**（诚实分级，非硬拒绝——与 8-28 短文档策略一致）：
flush 触发但预检 k<6 → 返回 `low` 嵌入 + 明确警告"文本过短，事后归因可能
失败"；6≤k<10 → 返回 `medium`；调用方可按 `config.min_tier`（默认 `low`，
即总是尽力嵌）决定是否采纳。`abstain` 不是本层默认行为（与核心 abstain
协议解耦——那是 trace 侧判决）。

### 4.3 整篇嵌入参数

- `uid_redundancy`：构造参数 `uid_redundancy: int = 3`（**可配置，非 hardcode**；
  作者拍板默认 3——P2-8 实测 r=3 时 crop50 段落裁剪 UID 存活 5/5）。代价：
  UID 位空间 /r（k≥10 → k_uid≥3 位 → 注册库 ≤8 用户可无损区分；超过时软
  判决按低 k_uid 位 mask + 容量项 abstain 保护，见
  `_compute_attribution_confidence`）。配置项进 `pyproject`/构造签名，文档
  写明权衡表（r=1:8 用户/弱裁剪抗性 ↔ r=3:8 用户/强裁剪抗性）。
- `sign=True`：信道 A 签名（防篡改判定在泄露物上依然有效）。
- `session_salt`：**整篇一次生成并归档**（不逐片段）——trace 时传同一
  salt + bands + uid_layout 走冗余解码。

### 4.4 交付与溯源

- flush 返回 `watermarked_document` 字符串 + `EmbedResult`（含
  reliability/bands/salt/uid_layout/key_version/dict_version）。
- 调用方把 marked 文档落盘/导出 = 交付物；**meta 必须存档**（与核心
  on_embed 回调同约定），溯源时 `wm.trace(suspect, salt, bands, uid_layout,
  key_version, dict_version, archived_uid)` 一条调用完成存在性 + 归因 +
  篡改 + 词典版本校验。

---

## 5. 集成形态（两期）

### 5.1 P1.1 Agent 文档工作流（首期交付，与 P1.2 共享缓冲核心）

自研 Agent / 应用把每次生成的片段（LLM 输出、工具结果、拼接小节）经
`buffer.append(text, ctx)` 写入，任务收尾调 `buffer.close(session_id)`
拿整篇水印文档。**适用对象**：报告生成器、写作助手、多步研究 agent——
它们最终产出交付物文件，正是 B 叙事的目标用户。

接入示意（伪代码）:

```python
from aawm_enterprise import AggregateWatermarker
from aawm import Watermarker

core = Watermarker.from_config(key_file="key.json", registry_file="reg.json",
                               language="zh", codec_mode="zero_cost",
                               calibration="calibration.json")
agg = AggregateWatermarker(core, meta_backend="sqlite://meta.db")

ctx = Context(user_id="agent-alice", session_id="task-42")   # 复用核心 Context
for chunk in agent_run():            # 多次 LLM/工具调用
    agg.append(chunk, ctx)           # 原文透传，不拖延
doc, result = agg.close("task-42")   # 拼整篇 → 预检 → 冗余嵌入 → meta 存档
assert result.reliability == "high"  # 达标：k≥10
save_deliverable(doc)                # 落盘交付物
```

### 5.2 P1.2 proxy 会话聚合（**与 P1.1 同步进首期**，作者拍板）

原设计将 proxy 聚合排后（理由：HTTP 会话无显式结束信号，空闲超时是唯一
收官点，延迟交付与流式"边出边看"冲突）。作者拍板"能同步就不排后"——
同步是可行的：proxy 聚合与 P1.1 **共享同一缓冲核心**（SessionBuffer +
FlushPolicy + AggregateWatermarker），仅多一层 HTTP 接线。范围控制：

1. **不自动捕获流式输出**：proxy 保持现有每请求句子级整流（模式 S，见下）
   不变——自动捕获会触发"双水印"冲突；
2. **只加显式会话缓冲端点**（`X-AAWM-Session-Id` 路由）：
   - `POST /v1/aawm/buffer/{session}`：append 片段（body=text）
   - `POST /v1/aawm/buffer/{session}/close`：flush → 返回整篇水印文档 + meta
   - 应用侧在文档导出点调用 close，拿 marked 全文替换自己的草稿——与 P1.1
     `append/close` 语义一一对应；
3. **会话生命周期**：缓冲 key=(session, uid)；空闲超时自动 close（防悬挂），
   但**不作为唯一收官点**——显式 close 才是（与应用工作流对齐）。

**模式护栏（A/S 二选一，防双水印）**：同一 (session, uid) 上，proxy 若已在
做句子级整流（模式 S=streaming，实时逐请求），则该会话**禁止**再开聚合缓冲
（模式 A=aggregate）；反之亦然。二选一由路由配置决定（per-session 开关）。
违反会在 append 时报错（fail-open 返回原文 + 明确错误码），绝不静默双嵌——
同一文本两套 bands 会让 trace 语义错乱。

**首期 proxy 侧改动**：新增端点 + 会话模式路由，不触碰现有整流逻辑；buffer
核心复用（不重复实现）。

---

## 6. 审计边界（开放问题，需作者拍板）

核心 `audit_sdk` 事件 `source` 字段现为 `cli|server|sdk`。聚合层调
`wm.embed` 会落 `source=sdk` 事件——语义上没错（确实是 SDK 主路径嵌入），
但审计方无法区分"逐调用嵌入"与"聚合文档嵌入"。选项：

- **A（已拍板，零核心改动）**：聚合层接受 `source=sdk`，区分靠 meta 存档的
  session_id——够用即可；
- **B**：核心加 `source=enterprise` 枚举（MIT PR，+1 字段，tests 同步），
  商业层 embed 后由 audit 钩子显式覆盖——更清晰但碰核心。

**采用 A**；若 P1.2 proxy 场景审计需求浮现再评估 B。

---

## 7. 许可证与分发（已定稿）

- 组件放**独立目录/仓库**，`LICENSE.enterprise` 采用 **BUSL-1.1 +
  Additional Use Grant**（作者已拍板）：Grant 允许"非生产评估与个人学习"
  免费使用；生产/商业部署需商业授权。Change License 预设为 Apache-2.0
  （2 年后自动开源，给客户确定性）。理由：比纯专有有社区透明度，比 MIT
  保留商用门；业界先例（Kafka/Element）。
- 分发：独立 PyPI 包 `aawm-enterprise`（或私有 index），`dependencies`
  声明 `acrostic-agent-watermark>=0.13.1`——绝不同包混装（MIT 面纯净）。
- README（企业包）边界声明前置：聚合前片段不护、短最终文档 low 档如实
  标注、护栏能力（abstain 等）不进商业层。

**实现注意事项**：BUSL-1.1 全文 + Additional Use Grant + Change License
条款在实现阶段写入 `aawm-enterprise/LICENSE.enterprise`；具体授权范围措辞
（"非生产评估"的边界定义）以执业律师意见为准。

---

## 8. 测试计划（独立 tests/，与核心 399 项解耦）

| 用例 | 断言 |
|---|---|
| 容量达标 flush | 聚合 ≥1200 字中文（demo corpus 标定后）→ reliability=high，k≥10 |
| 段落裁剪溯源 | 整篇嵌入后裁掉 50% 段落 → trace 存活且 uid 正确（r=3） |
| 单段泄露（聚合前） | 仅 append 未 close 即外泄 → 无保护（文档化预期，断言 fail-open 透传原文） |
| 短最终文档诚实分级 | 聚合后仍 <600 字 → reliability=low 且 result 带警告标志，不硬拒 |
| 幂等去重 | 同片段重复 append → 缓冲只存一份 |
| 会话隔离 | 两 (session_id,user_id) 并发 append → flush 互不串扰 |
| meta 往返 | flush 存档 → trace(读 meta) → 归因 user 正确 |
| 依赖方向 CI | `import aawm_enterprise` 后扫描无 `aawm` 反向引用（核心树 grep 无 enterprise） |
| fail-open | append/flush 任一步异常 → 透传原文/抛错给调用方，绝不产出半截水印 |

测试纪律沿用项目约定：固定 key+盐、`codec_mode` 显式、集成用例先
`calibrate_corpus` 标定（记忆 MEMORY.md 测试约定）。

---

## 9. 验收定义（DoD）

1. `aawm-enterprise` 独立包可 `pip install`（依赖已发布核心 ≥0.13.1）；
2. 上述测试全绿（独立运行，不依赖核心 tests）；
3. 单作者首版无外部贡献 → 无需 CLA 触发（CLA 自首个外部 PR 生效）；
4. MIT 主包零改动（审计选 A），核心 399 项测试不回归；
5. 边界披露进企业包 README 顶部；
6. **P1.2**：proxy 会话缓冲端点（buffer/close）+ A/S 模式护栏生效——同
   (session, uid) 整流与聚合互斥，违反 fail-open 报错不双嵌；现有整流逻辑
   与单请求行为零回归。

---

## 10. 拍板记录（2026-09-08 作者确认）

| # | 决策点 | 拍板 |
|---|---|---|
| 1 | 形态 | **接受**"文档级延迟嵌入"——只处理 agent **最终产物**水印；"边写边护"归 MIT 逐调用层，两者互补 |
| 2 | `uid_redundancy` | **默认 3，可手工配置**（构造参数，非 hardcode，权衡表进文档） |
| 3 | 许可 | **接受推荐**：BUSL-1.1 + Additional Use Grant，Change License=Apache-2.0（2 年） |
| 4 | 审计边界 | **接受 A**（零核心改动，区分靠 meta 的 session_id） |
| 5 | P1.2 proxy 聚合 | **不排后，与 P1.1 同步进首期**——共享缓冲核心 + 显式会话端点；立 A/S 模式护栏（同一会话整流与聚合二选一，禁双嵌） |

*设计基于 MIT 核心 v0.13.1 公开 API 与验证报告证据链；Licensing 条款以执业律师意见为准。*
