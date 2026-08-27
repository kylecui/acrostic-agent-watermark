---
name: aawm-watermark
description: >
  AAWM 文本水印嵌入与溯源（AI agent 输出交付物打标）。当需要给 agent 产出的最终文本交付物（报告/文档/代码/摘要/会议纪要/营销文案等落盘文件）嵌入可溯源水印，或对疑似泄露文本执行水印溯源、定位泄露者时使用。适用场景：AI 生成内容泄露追责、内部文档/对外报告标识归属、事后溯源。用户常搜：加水印、打水印、溯源、检测水印、归属、泄露追踪、防泄露、watermark、trace。
tags: [水印, watermark, 溯源, trace, 泄露追踪, 归属标识, AI内容保护, agent输出, 交付物打标]
agent_created: true
---

# AAWM 交付物水印技能

为 AI agent 产出的**落盘文本交付物**嵌入可溯源水印（藏头式同义词水印），并支持事后溯源检测。水印对读者几乎无感（零感词典模式），但持有密钥的一方可在泄露后精准定位责任归属。

## 何时使用

- 完成最终交付物写作并落盘后，在交付前**必须**对目标文件嵌入水印（除非用户明确要求不打标）
- 对可疑文本文件执行溯源检测，判定是否来自本系统、定位泄露用户
- 用户直接要求"给文件加水印 / 溯源 / 检测"

## 核心操作

### 1. 嵌入水印（交付前必做）

```bash
scripts/embed_files.sh <文件路径> [更多文件...]
```

行为：
- 对每个**文本文件**原位嵌入水印（同义词级微调，语义不变）
- 元数据写入 `<文件>.meta.json`（含 `session_salt` + `seal` + `bands`，**溯源必用**，不可删除）
- 嵌入失败则**保留原文件**（fail-open），不阻塞交付流程，向用户提示即可

身份/密钥来自环境变量（见下表）；缺 `AAWM_USER` 时向用户确认身份，确认后再执行。

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `AAWM_USER` | 是 | 用户身份：UID 数字（如 `41244`）或注册库别名（如 `alice`） |
| `AAWM_KEY` | 是* | master_key 文件路径（与 `AAWM_KEY_HEX` 二选一） |
| `AAWM_KEY_HEX` | 是* | master_key hex，直接传入 |
| `AAWM_REGISTRY` | 推荐 | 注册库 JSON 文件路径（别名解析与溯源必需） |
| `AAWM_LANGUAGE` | 否 | `auto`/`zh`/`en`，默认 `auto` |
| `AAWM_CODEC` | 否 | `zero_cost`/`default`/`hybrid`，默认 `zero_cost`（零感，高自然） |
| `AAWM_SUPPLEMENTARY_DICT` | 否 | 补充词典 JSON 路径（hybrid 模式；给出且未设 `AAWM_CODEC` 时自动用 `hybrid`）。**溯源时须设同一份**，否则 codec 重建不一致会漏检。词条质量铁律见 §4 |
| `AAWM_METAS_DIR` | 推荐 | meta 统一归档目录：本地 meta 缺失时 `trace_file.sh` 自动用 `aawm find-meta` 在归档中反查（递归 `*.meta.json` 与 `*.jsonl` salt-archive） |
| `AAWM_CALIB` | **生产必配** | p0/null 标定语料路径（目录或文件）。**未标定时存在性阈值偏严**（默认 `1.0+1.6×带数`，约 7.4），短文本可能检不出；标定后阈值按实测 null 分布（约 1.0/带），检出可靠。建议用与交付物同领域、**不含交付物本身**的正常文本 |

### 2. 溯源检测

```bash
scripts/trace_file.sh <可疑文件> [更多文件...]
```

- 自动读取 `<文件>.meta.json` 作为 `salt`/`seal`/`bands` 输入；meta 缺失时盲检
- 输出：是否检出、解码 UID、匹配用户、置信度、篡改判定

### 2a. 不知道 meta 是哪份时（find-meta）

拿到一篇可疑文本但 meta 存档散落多处/不确定是哪份时（**没有正确 meta，信道 B 会用错误码本解码而漏检**）：

```bash
aawm find-meta <可疑文件> <meta目录或glob> --key key.json --registry registry.json
```

- 两级策略：① **段落哈希匹配（免密钥）**——seal 里的 `para_hashes` 是段落文本的纯 SHA256，逐段比对交集即可锁定存档（文本被部分改写也能靠未改段落命中）；② **信道 B 验证**——用每份 meta 的 salt+bands 解码，检出 UID + 匹配用户 + 篡改判定
- 候选参数支持：目录（递归 `*.meta.json` + proxy salt-archive `*.jsonl`）、glob 模式、单个文件，可多个
- hybrid 嵌入的文件需同时传 `--supplementary-dict`（与嵌入时同一份）
- 设置 `AAWM_METAS_DIR` 后 `trace_file.sh` 会在本地 meta 缺失时自动走归档反查
- **运维建议**：所有交付的 meta 统一归档到一个目录（如 `metas/`），find-meta 一条命令全量扫描

### 3. 身份与密钥初始化（首次使用）

```bash
aawm keygen --output key.json                 # 生成 master_key（妥善保管）
aawm registry add alice --registry registry.json   # 注册用户别名
mkdir -p calib                                # 准备标定语料：放入若干正常文本
export AAWM_KEY=key.json
export AAWM_REGISTRY=registry.json
export AAWM_USER=alice
export AAWM_CALIB=/path/to/calib              # 生产必配（见上表）
```

### 4. 补充词典（hybrid 模式）——词条质量铁律

`AAWM_CODEC=hybrid` + `AAWM_SUPPLEMENTARY_DICT=<json>` 可用自造补充词典提升特定文本的嵌入容量。但**替换发生在词组内，任何坏词条都会直接变成交付物里的病句**（实测教训：`午后开始→午后着手`、`踩着水花→踩着水珠`）。为待嵌文本生成补充词典时必须逐组自查：

1. **禁"同类异物"首字对**：共享首字、尾字不同的名词往往同域不同物——水花/水珠（溅起的水 vs 静态水滴）、窗户/窗棂（整窗 vs 窗格子）、信/信笺（信件 vs 信纸）。这类替换指称漂移，读者一眼识破。
2. **禁语法框架不同的词对**：动词搭配约束不同则不可换——着手/开始（"着手"强及物，必须接"着手做某事"；"开始"可独立作谓语）、打开/推开（打开灯 ≠ 推开灯）。
3. **禁语义域漂移**：温柔/温和（人的性情 vs 气候性格）、环境/氛围（开发环境 ≠ 开发氛围）。
4. **禁单字词条**：单字词进分词词典没有阻断词表，会把复合词误切——实测"看"组把"失望"切成"失|看"再替换成"失看"。零感基础词表的单字组（和/与、但/但是）配了专用阻断词表，自造补充词典没有这层保护，故补充词典一律用双字词。
5. **自检方法**：为每组词想象"原文词 → 候选词"逐一替换到句子中读一遍；任何一处拗口即删整组。宁缺毋滥——容量不足换 zero_cost 重嵌，也别留病句。
6. 词典格式：`{"原文词": ["原文词", "候选词", ...]}`，原文词必须在组内且居首。



技能包内含 `hooks/claude-code.hooks.json`——配置后 Claude Code 每次 `Write`/`Edit` 落盘文本文件会**自动**嵌入水印，无需人工记忆。配置方法见技能包内 `README.md` §3。

## 原则

- **Fail-open**：嵌入失败绝不破坏交付物、不阻塞流程
- **元数据是溯源的前提**：`<文件>.meta.json` 与交付文件必须一同归档（推荐统一存 `metas/` 目录）；meta 散失时用 `aawm find-meta`（§2a）从归档中反查。脱离正确 meta 的裸检大概率漏检——`session_salt` 决定码本映射，错误盐 = 错误码本
- **只有密钥方可见水印**：对读者无感，不宣称版权、不声明归属，仅用于事后溯源

## 限制

- 仅支持 UTF-8 文本文件（报告/文档/代码/文案）；二进制（图片/PDF/Office 文档）跳过
- 对话/终端里直接输出的文字回复不经过本技能——那部分由 `aawm proxy` 代理网关在传输层拦截（见 `docs/cli_agent_proxy_guide.md`）
- 超短文本（< 数十字）可嵌入容量有限，嵌入效果取决于词典覆盖
