# AAWM 用户手册（product-ready）

> Acrostic Agent Watermark —— 面向 AI agent 输出交付物的可溯源文本水印。
> 本文按**使用方式**组织，覆盖安装、配置、使用、验证四步，任何环境（Windows / Linux / macOS）都可照做。
> 文中的所有命令都在真实环境中冒烟通过；命令旁的 `[验证]` 标记表示该步有可自动核对的输出。

---

## 目录

- [1. 一分钟看懂 AAWM](#1-一分钟看懂-aawm)
- [2. 安装与初始化](#2-安装与初始化)
- [3. 使用方式一：CLI 命令行](#3-使用方式一cli-命令行)
- [4. 使用方式二：技能包（agent 交付物自动打标）](#4-使用方式二技能包agent-交付物自动打标)
- [5. 使用方式三：Python SDK（自研代码）](#5-使用方式三python-sdk自研代码)
- [6. 使用方式四：框架适配器（OpenAI / LangChain / LiteLLM / AutoGen / CrewAI）](#6-使用方式四框架适配器)
- [7. 使用方式五：HTTP 检测服务（低代码平台 / 审计系统）](#7-使用方式五http-检测服务)
- [8. 使用方式六：代理网关（CLI/IDE agent 零改造接入）](#8-使用方式六代理网关cliide-agent-零改造接入)
- [9. 溯源实战：三种场景](#9-溯源实战三种场景)
- [10. 生产配置清单](#10-生产配置清单)
- [11. 故障排查](#11-故障排查)
- [12. 已知限制](#12-已知限制)

---

## 1. 一分钟看懂 AAWM

AAWM 给 AI agent 产出的文本嵌入**人眼几乎无感、密钥可精确溯源**的水印。核心概念只有 4 个：

| 概念 | 是什么 | 你要做的 |
|---|---|---|
| `master_key` | 一把对称密钥（32 字节），**嵌入与溯源用同一把** | 生成后妥善保管，只在可信方之间流转 |
| `UID` / 别名 | 每个用户一个编号（16-bit，0~65535），可配别名 | 用 `aawm registry add <别名>` 注册 |
| `meta` 文件 | 每次嵌入生成的存档（`session_salt` + `seal` + `bands`） | **与交付物一同归档，溯源必用** |
| `标定` | 用同领域正常文本拟合检测阈值（`aawm calibrate` 产出标定文件） | **生产必配**，否则检测阈值偏严会漏检 |

三个铁律：

1. **meta 必须归档**。`session_salt` 决定码本映射——没有正确 meta，检测就像拿错字典，大概率漏检。
2. **生产必须标定**。不标定时存在性阈值按最坏情况取（实测约 `1.6×带数`），中等信号文本会被漏检；标定后阈值按真实 null 分布（约 `1.0/带`）取值，检出可靠。用 `aawm calibrate ./corpus -o calibration.json` 一次标定，之后 embed/trace 统一传 `--calibration`。
3. **嵌入失败绝不破坏交付物**（fail-open）。任何异常都透传原文。

两条工作流：

```
【嵌入侧】 写好的文本 → aawm embed → 带水印文本（发布）+ meta（归档）
【溯源侧】 拿到可疑文本 → aawm trace（有 meta）/ aawm find-meta（没 meta）→ UID → 用户
```

支持 6 种使用方式，选适合你的：

| 方式 | 适合谁 | 改动量 | 章节 |
|---|---|---|---|
| CLI | 个人 / 手工打标溯源 | 0 代码 | §3 |
| 技能包 | AI agent 产出落盘交付物时自动打标 | 装脚本 + 环境变量 | §4 |
| Python SDK | 自研 agent 代码 | 3 行 | §5 |
| 框架适配器 | 用 OpenAI/LangChain/LiteLLM/AutoGen/CrewAI | 1 行 | §6 |
| HTTP 服务 | 低代码平台（Dify/Coze）、审计系统远程溯源 | 配置 + HTTP 调用 | §7 |
| 代理网关 | Claude Code / Codex / opencode 等黑盒 CLI agent | 改 base_url | §8 |

---

## 2. 安装与初始化

### 2.1 环境要求

| 项 | 要求 |
|---|---|
| Python | **≥ 3.10**（开发与测试环境为 3.13，实测可用） |
| 操作系统 | Windows / Linux / macOS（Windows 建议用 Git Bash 跑 shell 脚本） |
| 网络 | 离线可用（纯本地算法，仅 HTTP 服务 / 代理网关需要网络） |

### 2.2 安装 aawm

**方式 A：从源码安装（推荐，含全部功能）**

```bash
git clone <仓库地址> acrostic-agent-watermark
cd acrostic-agent-watermark

# 基础安装（CLI + Python SDK）
pip install -e .

# 或带服务端（HTTP 服务 / 代理网关需要 uvicorn/fastapi）
pip install -e '.[server]'

# 或带框架适配器（用哪个装哪个）
pip install -e '.[langchain]'    # LangChain
pip install -e '.[litellm]'      # LiteLLM
pip install -e '.[nlp]'          # NLTK 分词
pip install -e '.[llm]'          # openai / anthropic SDK
```

**方式 B：从 PyPI 安装**

```bash
pip install aawm[server]
```

**方式 C：不安装，直接用源码目录**

```bash
export PYTHONPATH=/path/to/acrostic-agent-watermark/src
python -m aawm.cli --help
```

> **Windows 特别提示**：如果 `aawm` 命令不在 PATH（例如装进了某个内置 Python 的 Scripts 目录），运行技能包里的 shim 安装脚本，把命令装到用户 bin 目录（见 §4.2）。之后 bash / PowerShell / cmd 都能直接敲 `aawm`。

### 2.3 验证安装

```bash
aawm --help
# 期望输出子命令列表：
#   {keygen,registry,embed,trace,find-meta,serve,proxy}
# [验证] 能看到子命令列表即安装成功
```

### 2.4 初始化（每个环境只做一次）

```bash
# 1) 生成 master_key（chmod 600 自动设置）
aawm keygen -o key.json
# [验证] 输出: master_key 已保存到 key.json

# 2) 注册用户别名（每个要溯源的人一个）
aawm registry add alice --registry reg.json
aawm registry add bob   --registry reg.json
# [验证] 输出: 注册成功: alice -> UID 0x0001 (1)

# 3) 查看注册库
aawm registry list --registry reg.json
# [验证] 输出表格，含 alice=0x0001、bob=0x0002

# 4) 标定（生产必配，见 §10.1）
# 快速体验：用包内置示例语料 30 秒产出标定文件
aawm calibrate --demo -o calibration.json
# 生产：用同领域正常文本标定（几十篇 .txt/.md 放一个目录）
# aawm calibrate ./calib -o calibration.json
```

> `key.json` 就是命脉：**丢失 = 历史水印全部无法溯源**，请立即备份并离线保存。不要把 key 提交进代码库。

---

## 3. 使用方式一：CLI 命令行

### 3.1 嵌入水印（embed）

```bash
aawm embed <输入文件> --key key.json --user alice \
      --registry reg.json --codec-mode zero_cost \
      --calibration calibration.json -o marked.txt
```

| 参数 | 说明 |
|---|---|
| `<输入文件>` | UTF-8 文本文件；`-` 表示从 stdin 读取 |
| `--user` | 用户：UID 数字（`42` / `0x002A`）或注册库别名（`alice`） |
| `--key` / `--key-hex` | 密钥文件路径 / hex，二选一 |
| `--registry` | 注册库路径（别名解析与溯源需要） |
| `--codec-mode` | `zero_cost`（默认，零感）/ `hybrid`（+补充词典）/ `default`（旧词林，病句率高不推荐） |
| `--calibration` | 标定文件路径（`aawm calibrate` 产出），**生产必配**；一次标定处处复用，推荐方式 |
| `--calibrate-corpus` | 标定语料路径（目录或单文件），每次运行现场拟合（大语料慢） |
| `--supplementary-dict` | hybrid 模式的补充词典 JSON |
| `--n-bits` | 编码位数（默认满容量；小于容量留冗余带抗替换） |
| `--no-sign` | 不签信道 A（不做防篡改，不推荐） |
| `-o` | 输出文件。**不传则把水印文本打到 stdout** |

执行后的产物：

```
marked.txt            ← 带水印的交付物（发布给用户）
marked.meta.json      ← meta 存档（salt+seal+bands，与交付物一同归档！）
```

stderr 会打印统计行——**这是最重要的可验证输出**：

```
[统计] UID=0x0001, 词典命中=48, 存在性=25.5
[自适应] 模式=zero_cost, 容量=16 bit, 编码=16 bit, bands=[0,1,2,...,15]
[可靠性] high —— 容量充足且信号达标，检出与归因均可靠
```

**怎么读统计行**：
- `词典命中` 是文本里被词典识别的词数。**低于 ~30 说明文本太短，trace 大概率检不出**（容量限制，见 §12）。
- `容量` 是可用 bit 数。达到 16 bit 才能编码完整 UID；小于 16 bit 时 UID 取低 `n_bits` 位（k-bit 语义，配合注册库仍能匹配回用户）。
- `存在性` 是嵌入后的信号强度，越高越稳。
- `[可靠性]` 是容量分级：`high`（≥10 bit，中文约 ≥1200 字）检出归因均稳；`medium`（6-9 bit）检出常存活、归因可能失败；`low`（<6 bit 或弱嵌入）仅供参考。**短文本不会被拒嵌**——照常嵌水印并标注降级（meta.json 的 `reliability` 字段同值），聚合多份存档仍可溯源。

```bash
# stdin / stdout 用法示例
cat report.md | aawm embed - --key key.json --user alice --registry reg.json > marked.md
```

### 3.2 溯源（trace）

```bash
aawm trace <可疑文件> --key key.json --registry reg.json \
      --meta marked.meta.json --calibration calibration.json
```

| 参数 | 说明 |
|---|---|
| `--meta` | 嵌入时的 meta 文件（推荐，含 salt+seal+bands） |
| `--salt` | 只有 salt 时手动传 hex（例如从盐归档反查到的） |
| 都不传 | 盲检，**大概率漏检**（见 §9.3） |

期望输出（完整、未篡改）：

```
检出水印: 是
解码 UID: 0x0001
匹配用户: alice (汉明距=0)
置信度: 0.64
存在性得分: 25.5
词典命中: 48
自适应: 容量=16 bit, 存活带=16/16
篡改判定: 否
```

**退出码**：`0` = 检出；`2` = 未检出。可作脚本判断。

**怎么读输出**：
- `检出水印: 是` = 信道 B 存在性判定通过（有隐藏信号）。
- `解码 UID` + `匹配用户` = 溯源到具体用户。
- `篡改判定` = 信道 A 验证：`是` 表示文本被改过（后面 `被改段落` 指出是哪几段）；`否` = 完整未改。**文本被改写时，信道 B 通常仍能检出 UID，同时信道 A 报篡改**——这是预期行为，不矛盾。

### 3.3 meta 散失时：find-meta（重点）

拿到一篇可疑文本，但**不知道它的 meta 是哪个**时，用 `find-meta` 在归档里反查：

```bash
aawm find-meta <可疑文件> <meta目录或glob> \
      --key key.json --registry reg.json --calibration calibration.json
```

- 候选可以是：目录（**递归**扫描 `*.meta.json` + proxy 盐归档 `*.jsonl`）、glob 模式、单文件，可多个。
- 两级策略：① **段落哈希匹配（免密钥）**——先把嫌疑文本按段哈希，与每份 meta 的 `seal.para_hashes` 求交集排序；② **信道 B 验证**——对命中的候选逐个用其 salt+bands 解码，以检出与否裁决。
- **最终裁决（v0.10）**：攻击下"检出"本质盐无关（同一文本多条盐都会检出），因此 find-meta **不取第一个检出者**。裁决规则：
  1. **段哈希内容证据优先**——嫌疑文本包含该 meta 的未改段落是最强来源锁定；即使其水印被破坏，也宁可 abstain 在该 meta 上，绝不改判到无内容证据却"检出"的错误 meta；
  2. **解码 UID 与存档交叉校验**——trace 解码的 UID 必须与 meta 存档的 `user_id`/`uid` 一致（自适应 k-bit 掩码对齐也算一致）；不一致说明解码失真，输出"不可判定"（退出码 3），绝不输出可能错误的 UID；
  3. **多候选检出冲突**——无段哈希证据时多个 meta 同时"检出"无法区分真伪，保守 abstain。

期望输出（部分改写 + 存在干扰 meta 的真实结果）：

```
候选 meta: 2 份，嫌疑文本段落数: 8

[段落哈希匹配]（免密钥，命中文本中未改写的段落）
  7/8 段匹配（存档 8 段）  UID=1 (alice)  metas\marked_long.meta.json ← 命中

[信道 B 验证]（用各 meta 的 salt+bands 解码，最多试 10 份）
  metas\marked_long.meta.json: 检出（存在性=25.5 置信度=0.64） UID=0x0001 匹配 alice (汉明距=0)

结论: 匹配 meta = metas\marked_long.meta.json
  UID=0x0001, 用户=alice, 汉明距=0, 置信度=0.64
  篡改判定: 是
  被改段落: [0]
```

> 即使文本被改写了一两段（上面 `被改段落: [0]`），未改的段落仍能让段哈希锁定正确 meta，随后信道 B 精确解码。**运维建议：所有交付物的 meta 统一归档到一个目录（如 `metas/`），find-meta 一条命令全量扫描。**

### 3.4 其他命令速查

```bash
aawm registry list --registry reg.json              # 列出用户
aawm registry find 0x0001 --registry reg.json       # UID → 别名
aawm keygen --env AAWM_MASTER_KEY                   # 输出 export 环境变量格式
aawm serve --key key.json --registry reg.json       # 起 HTTP 检测服务（§7）
aawm proxy --key key.json --key-map keys.json ...   # 起代理网关（§8）
```

---

## 4. 使用方式二：技能包（agent 交付物自动打标）

**适用**：AI agent（Claude Code / WorkBuddy / Codex 等）产出的**落盘文本交付物**（报告/文档/代码/摘要/会议纪要/营销文案）在交付前自动嵌入水印。

技能包位置：`skills/aawm-watermark/`（仓库内）。包含：

```
aawm-watermark/
├── SKILL.md                      # 技能指令（agent 加载后按此执行）
├── scripts/
│   ├── embed_files.sh            # 对落盘文本文件原位嵌入水印（fail-open）
│   ├── trace_file.sh             # 对可疑文件执行溯源
│   └── install_local_shim.sh     # 把 aawm 命令装进 PATH（Windows 必备）
└── hooks/
    └── claude-code.hooks.json    # Claude Code PostToolUse 自动触发
```

### 4.1 快速开始（手动触发）

```bash
# 0) 安装 aawm + 初始化（见 §2，跳过已完成的步骤）
#    把技能包路径记下来
export AAWM_SKILL_DIR=/path/to/acrostic-agent-watermark/skills/aawm-watermark

# 1) 配置环境变量（建议写进 shell 配置）
export AAWM_KEY=key.json
export AAWM_REGISTRY=reg.json
export AAWM_USER=alice            # 你的身份：UID 数字或注册库别名
export AAWM_CALIB=/path/to/calib  # 生产必配（见 §10.1）

# 2) 对交付物嵌入水印（原位替换，meta 写到 <文件>.meta.json）
bash "$AAWM_SKILL_DIR/scripts/embed_files.sh report.md summary.txt

# [验证] 输出:
#   已嵌入水印: report.md（meta: report.md.meta.json）
#   已嵌入水印: summary.txt（meta: summary.txt.meta.json）

# 3) 溯源（拿到可疑文件时）
bash "$AAWM_SKILL_DIR/scripts/trace_file.sh suspect.md
# [验证] 输出 "检出水印: 是 / 匹配用户: alice ..."，退出码 0
```

脚本行为：
- **fail-open**：任一文件嵌入失败 → 保留原文件、警告到 stderr，不中断其余文件，不阻塞交付。
- **二进制跳过**：图片/PDF/Office/压缩包等扩展名自动跳过。
- **meta 命名**：约定为**替换扩展名**（`a.md → a.meta.json`），与文件同目录。

### 4.2 Windows：让任何 agent 都能找到 aawm 命令

WorkBuddy 内置 Python 的 Scripts 目录通常不在 PATH，导致 `command -v aawm` 失败。跑一次 shim 安装脚本：

```bash
bash skills/aawm-watermark/scripts/install_local_shim.sh
# [验证] 输出: 完成。验证:
#   bash  -> command -v aawm && aawm --help
command -v aawm   # 现在任何 bash/agent 都能命中
```

生成 `aawm` / `aawm-embed` / `aawm-trace` 三个命令（bash + cmd 双版本）到用户 bin 目录。
> WorkBuddy 升级内置 Python 后 shim 失效，重跑一次即可（脚本自动取最新版本）。

### 4.3 环境变量速查

| 变量 | 必填 | 说明 |
|---|---|---|
| `AAWM_USER` | 是 | 用户身份：UID 数字（`41244`）或注册库别名（`alice`） |
| `AAWM_KEY` / `AAWM_KEY_HEX` | 是* | master_key 文件路径 / hex，二选一 |
| `AAWM_REGISTRY` | 推荐 | 注册库 JSON 路径（别名解析与溯源必需） |
| `AAWM_CALIB` | **生产必配** | 标定语料路径（目录或文件） |
| `AAWM_LANGUAGE` | 否 | `auto`（默认）/ `zh` / `en` |
| `AAWM_CODEC` | 否 | `zero_cost`（默认，零感）/ `hybrid` / `default` |
| `AAWM_SUPPLEMENTARY_DICT` | 否 | hybrid 补充词典 JSON；给出且未设 `AAWM_CODEC` 时自动用 hybrid。**溯源须配同一份** |
| `AAWM_METAS_DIR` | 推荐 | meta 统一归档目录。本地 meta 缺失时 `trace_file.sh` 自动用 `aawm find-meta` 反查 |
| `AAWM_DRY_RUN` | 否 | 非空时只打印命令不执行 |
| `AAWM_SKILL_DIR` | 否* | 技能包绝对路径（Claude Code hooks 需要） |

### 4.4 Claude Code：落盘自动打标（无需记忆）

```bash
# 1) 把 hook 合并进项目 .claude/settings.json（或用户级 ~/.claude/settings.json）的 hooks 字段
cp "$AAWM_SKILL_DIR/hooks/claude-code.hooks.json" .claude/settings.json

# 2) 确保启动 Claude Code 的 shell 已 export 上述环境变量
# 之后每次 Write/Edit 落盘文本文件都会自动嵌入水印（hook 失败不影响主流程）
```

### 4.5 其他平台（Codex / opencode 等）

无 PostToolUse 自动触发时，靠 SKILL.md 指令驱动：agent 产出交付物后主动调用 `embed_files.sh`；或用自己的 hook 机制包装同一脚本（`--comma` 参数兼容逗号分隔路径列表）：

```bash
bash "$AAWM_SKILL_DIR/scripts/embed_files.sh" --comma "a.md,b.md,c.txt"
```

---

## 5. 使用方式三：Python SDK（自研代码）

### 5.1 三件套（3 行接入）

```python
from aawm.plugins import Watermarker

# 一次性初始化（key 文件不存在会自动创建；registry 可省略）
wm = Watermarker.from_config("key.json", "reg.json",
                             codec_mode="zero_cost",
                             calibration="calibration.json")  # aawm calibrate 产出

# 嵌入：agent 产出文本后、发布给用户前
result = wm.embed(agent_output, user_id="alice")
print(result.reliability)  # high / medium / low —— 容量分级（短文本降级不拒嵌）
# 发布 result.watermarked_text
# 存档 result.session_salt + result.bands（可公开，溯源必用）
# 嵌入前可预估容量（不改文本）：k = wm.estimate_capacity(text)

# 溯源：验证方拿到可疑文本后
trace = wm.trace(suspect_text,
                 session_salt=result.session_salt,
                 bands=result.bands,
                 n_bits=result.n_bits)
if trace.watermarked:
    print(f"泄露源自用户 {trace.user}（置信度 {trace.confidence:.2f}）")
```

### 5.2 更精确的构造方式

```python
from aawm.plugins import Watermarker, UIDRegistry
from aawm.plugins.keystore import KeyStore

wm = Watermarker(
    keystore=KeyStore.from_file("key.json"),          # 或 KeyStore.from_env() 读 AAWM_MASTER_KEY
    registry=UIDRegistry(backend="file", path="reg.json"),
    language="auto",                                   # auto / zh / en
    codec_mode="zero_cost",                            # zero_cost / hybrid / default
    supplementary_dict={...},                          # hybrid 用
    calibrate_corpus=[...],                            # 现场语料标定（慢；推荐用下面的标定文件）
    calibration="calibration.json",                    # 标定文件路径或 dict（aawm calibrate 产出）
)
```

### 5.3 API 签名速览

```python
result = wm.embed(
    text,                # 原始文本
    user_id,             # int=UID；str=别名（经注册库映射）
    session_salt=None,   # 固定盐（一般不管，自动生成）
    sign=True,           # 签信道 A（防篡改）
    bias=1.0,            # 嵌入强度（<1.0 改动更少、鲁棒性略降）
    n_bits=None,         # 编码位数（None=满容量）
)
# result.watermarked_text / .session_salt / .user_id / .user_alias
#       / .seal / .bands / .capacity / .n_bits / .existence_score / .n_dict_words
#       / .reliability（high/medium/low 容量分级）/ .margin_ratio / .weak_embed

trace = wm.trace(
    text,
    session_salt=None,   # 嵌入时存档的盐
    seal=None,           # 信道 A 签名（验证篡改）
    bands=None,          # 嵌入时存档的带集（自适应模式）
    n_bits=None,         # 嵌入时的编码位数
    soft_match=False,    # 软判决匹配（文本受损时更稳）
)
# trace.watermarked / .uid / .user / .confidence / .tampered
#       / .tampered_paragraphs / .existence_score / .active_bands
```

### 5.4 验证

```bash
# 跑官方最小示例（离线，直接看输出）
python examples/05_plugin_quickstart.py
python examples/06_agent_demo.py     # 三个用户泄露溯源演示
# [验证] 两个示例正常结束且打印 UID 溯源结果
```

---

## 6. 使用方式四：框架适配器

所有适配器共用一个中间件，**核心铁律是 Fail-open**：嵌入异常透传原文，绝不影响响应。所有适配器都支持 `on_embed` 回调——**务必用它存档 salt**，否则中间件嵌入的水印事后无法溯源（这是中间件模式下溯源的前提）。

```python
def archive_salt(result, ctx):
    """result: EmbedResult（含 session_salt）；ctx: 本次请求上下文"""
    db.save(
        salt=result.session_salt.hex(),     # 溯源凭据，可公开
        uid=result.user_id,
        bands=list(result.bands),           # 自适应模式必须
        n_bits=result.n_bits,
        text_hash=sha256(result.watermarked_text).hexdigest(),
        ts=now(),
    )
```

### 6.1 OpenAI SDK（最常用）

```python
from openai import OpenAI
from aawm.plugins.adapters.openai_v1 import wrap_openai_client

client = wrap_openai_client(OpenAI(), watermarker, on_embed=archive_salt)
resp = client.chat.completions.create(..., user_id="alice")  # 输出自动嵌水印
```

同步/异步、流式/非流式全覆盖（`AsyncOpenAI` 用 `wrap_async_openai_client`）。
`user_id` 从 `create(**kwargs)` 参数 / 请求头 / 环境变量 `AAWM_USER_ID` 自动解析；解析不到 → 跳过嵌入（安全默认）。

### 6.2 LangChain v1

```python
from langchain.agents import create_agent
from aawm.plugins.adapters.langchain_v1 import AAWMMiddleware

agent = create_agent(model=model, tools=[...], middleware=[AAWMMiddleware(watermarker)])
```

### 6.3 LiteLLM Proxy（网关级）

```python
from aawm.plugins.adapters.litellm_proxy import setup_hooks
setup_hooks(watermarker, on_embed=archive_salt)
# proxy_config.yaml 里 callbacks: aawm_proxy_hooks 即可对全部经网关的调用自动嵌水印
```

### 6.4 AutoGen / CrewAI

```python
from aawm.plugins.adapters.autogen_v1 import wrap_autogen_agent
agent = wrap_autogen_agent(AssistantAgent(...), watermarker, user_id="alice")

from aawm.plugins.adapters.crewai_v1 import setup_hooks
setup_hooks(watermarker, user_id="alice")
result = crew.kickoff()   # 所有 LLM 输出自动嵌水印
```

> AutoGen/CrewAI 适配器默认不安装对应框架也能 import，调用包装函数时若缺框架会提示 `pip install 'aawm[...]'`。

### 6.5 验证

```bash
# 无框架也能跑的核心 e2e（不依赖 OpenAI key）
python examples/05_plugin_quickstart.py
# [验证] 输出含用户 UID 解码与溯源结果
```

---

## 7. 使用方式五：HTTP 检测服务

**适用**：低代码平台（Dify / Coze）在输出后调 HTTP 接口嵌水印；审计系统远程调用溯源。

### 7.1 启动

```bash
aawm serve --key key.json --registry reg.json --port 8765 \
      --calibration calibration.json --log-level warning
# [验证] 输出: AAWM 检测服务启动于 http://0.0.0.0:8765
```

> **务必标定**（`--calibration` 标定文件或 `--calibrate-corpus` 语料目录）。
> 实测：同样一篇水印文本，标定后 `trace` 检出（存在性 25.5 ≥ 标定阈值），
> 不标定则漏检（默认阈值 26.6 > 25.5）。服务端不标定 = 线上漏检。

### 7.2 端点

| 方法 | 路径 | 用途 | 鉴权 |
|---|---|---|---|
| GET | `/v1/health` | 健康检查 | 无 |
| POST | `/v1/embed` | 嵌入（**内部用**） | **建议加网络隔离/鉴权** |
| POST | `/v1/trace` | 溯源 | 公开给审计系统 |
| POST | `/v1/find-meta` | 候选 meta 反查 | 公开 |

### 7.3 curl 验证

```bash
# 健康检查
curl -s http://127.0.0.1:8765/v1/health
# {"status":"ok","watermarker_initialized":true}

# 溯源（把 meta 里字段回传）
curl -s -X POST http://127.0.0.1:8765/v1/trace \
  -H "Content-Type: application/json" \
  -d '{
    "text": "<可疑文本>",
    "session_salt": "<hex>",
    "bands": [0,1,4,...],
    "n_bits": 16,
    "seal": {"merkle_root":"<hex>","para_hashes":["<hex>",...],"aad":"0001","version":1}
  }'
# {"watermarked":true,"uid":1,"user":"alice","hamming_dist":0,"tampered":false,...}

# 嵌入（响应里取 watermarked_text 发布、session_salt+bands 存档）
curl -s -X POST http://127.0.0.1:8765/v1/embed \
  -H "Content-Type: application/json" \
  -d '{"text":"<原始文本>","user_id":"alice"}'
# {"watermarked_text":"...","session_salt":"<hex>","bands":[...],"n_bits":16,...}
```

### 7.4 部署注意

- `/v1/embed` 能用水印密钥嵌入任意文本——生产必须放内网或加鉴权层（nginx basic auth / mTLS），当前版本未内置鉴权。
- systemd 单元示例见 `docs/deployment.md` §4.2。
- 用 Python 代码启动：`from aawm.server.api import create_app, set_watermarker`。

---

## 8. 使用方式六：代理网关（CLI/IDE agent 零改造接入）

**适用**：Claude Code / Codex / opencode / WorkBuddy / Antigravity / PI / Qwen Code 等**黑盒独立进程**。它们无法 import SDK，但都支持自定义 `base_url`——把 base_url 指向本地代理，代理拦截响应文本、嵌入水印后再交回。**工具零改造，用户无感。**

```
CLI/IDE agent ── base_url = http://127.0.0.1:8787 ──▶ AAWM Proxy ──▶ 真实 API
   （唯一改动）                                      ▲ 替换上游 key      │
   用户无感 ◀──────────── 水印文本 ◀──────────────────┴── 响应文本嵌水印 ◀┘
```

### 8.1 准备

```bash
# 1) 密钥 + 注册库（见 §2.4）
# 2) 给每个用户/终端发一把 aawm key，并建 key → UID 映射
cat > keys.json <<'EOF'
{
  "sk-aawm-alice": "alice",     # 值支持：已注册别名 / 0x 前缀数字 / 十进制数字
  "sk-aawm-bob":   "0x0002"
}
EOF
```

### 8.2 启动

```bash
aawm proxy \
  --key key.json --registry reg.json \
  --key-map keys.json \
  --upstream-openai    https://api.openai.com \
  --upstream-anthropic https://api.anthropic.com \
  --salt-archive salts.jsonl \
  --port 8787
# [验证] 输出: AAWM 代理网关启动于 http://127.0.0.1:8787
#              已映射客户端 key: 2 个
#              salt 归档: salts.jsonl
```

- 上游 key 从环境变量 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 读取；上游 base 也可用 `AAWM_UPSTREAM_OPENAI` / `AAWM_UPSTREAM_ANTHROPIC` 覆盖。
- 自建网关（vLLM/LiteLLM）场景不配上游 key，客户端 key 原样转发。
- **`--salt-archive` 是溯源的前提**——每次嵌入成功都会追加一行 JSONL 记录（uid/session_salt/n_bits/codec_mode/bands/seal）。

```bash
# 启动后健康验证
curl http://127.0.0.1:8787/v1/models -H "Authorization: Bearer sk-aawm-alice"
# [验证] 返回上游模型列表（透传成功）
```

### 8.3 各 agent 接入配置

**Claude Code**（Anthropic 协议 → `/v1/messages`）：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
export ANTHROPIC_API_KEY="sk-aawm-alice"
claude
# 或写 ~/.claude/settings.json 的 env 字段（改完需重启进程）
```

**Codex CLI**（OpenAI Responses → `/v1/responses`），编辑 `~/.codex/config.toml`：

```toml
model = "gpt-5.2-codex"
model_provider = "aawm"

[model_providers.aawm]
name = "AAWM Proxy"
base_url = "http://127.0.0.1:8787/v1"
env_key = "AAWM_CODEX_KEY"
wire_api = "responses"
requires_openai_auth = false
```

```bash
export AAWM_CODEX_KEY="sk-aawm-alice"
codex
```

**opencode**（OpenAI-compatible → `/v1/chat/completions`），编辑 `opencode.json`：

```json
{
  "provider": {
    "aawm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "AAWM",
      "options": { "baseURL": "http://127.0.0.1:8787/v1", "apiKey": "{env:AAWM_OPENCODE_KEY}" },
      "models": { "claude-sonnet-4-6": { "name": "Claude (AAWM)" } }
    }
  }
}
```

```bash
export AAWM_OPENCODE_KEY="sk-aawm-alice"
opencode
```

**WorkBuddy 桌面版**：设置 → 模型 → 添加模型 → 自定义，接口地址填 `http://127.0.0.1:8787`（Anthropic 协议）或 `http://127.0.0.1:8787/v1`（OpenAI 协议）；或直接改 `~/.workbuddy/models.json`。

**Antigravity**：必须走 `customEndpoints` 的 openai-compatible（Gemini 原生协议是透传不嵌）：

```json
{ "model": "gemini-3.5-flash", "customEndpoints": {
    "llmUrl": "http://127.0.0.1:8787/v1",
    "apiKey": "sk-aawm-alice",
    "provider": "openai-compatible" } }
```

**Qwen Code**：`~/.qwen/settings.json` 的 `modelProviders` 加 openai 项；或简化 `export QWEN_CODE_BASE_URL="http://127.0.0.1:8787/v1"` + `QWEN_CODE_API_KEY="sk-aawm-alice"`。

> 更多平台与细节见 `docs/cli_agent_proxy_guide.md`。

### 8.4 行为细节

- **协议**：支持 OpenAI Chat Completions（`/v1/chat/completions`）、Anthropic Messages（`/v1/messages`）、OpenAI Responses（`/v1/responses`）；其余路径原样透传。
- **流式**：句子级嵌入，流结束自动 flush 尾句。
- **工具调用不改**：`tool_calls`/`tool_use` 原样透传，只嵌最终给用户的文本。
- **短文本跳过**：默认 `min_text_length=50`，太短不嵌入（可调）。
- **身份未映射**：key 不在 key_map → 透传不嵌（宁可漏嵌，不错嵌）。

### 8.5 验证完整闭环

```bash
# 1. 从盐归档反查某次会话的 salt
grep '"uid": 1' salts.jsonl | tail -1

# 2. 用同一把 key 溯源（--salt 传 hex；有 seal/bands 也可直接 trace --meta）
aawm trace suspect_leak.txt --key key.json --registry reg.json \
  --salt <hex> --calibration calibration.json
# [验证] 输出 "检出水印: 是 / 解码 UID: 0x0001 / 匹配用户: alice"
```

---

## 9. 溯源实战：三种场景

| 场景 | 用什么命令 | 备注 |
|---|---|---|
| **有 meta 文件**（最理想） | `aawm trace x.txt --key key.json --registry reg.json --meta x.meta.json` | 直接、精确 |
| **meta 散失，但有归档目录** | `aawm find-meta x.txt metas/ --key key.json --registry reg.json` | 段哈希免密钥定位 + 信道 B 确认 |
| **什么都没有**（裸文本） | `aawm trace x.txt --key key.json` | **大概率漏检**；只适合"快速排除" |

### 9.1 有 meta：直接 trace

```bash
aawm trace marked_long.txt --key key.json --registry reg.json \
      --meta marked_long.meta.json --calibration calibration.json
# 检出水印: 是 / 解码 UID: 0x0001 / 匹配用户: alice (汉明距=0) / 篡改判定: 否
```

### 9.2 没 meta 但有归档：find-meta

```bash
# 把所有交付物的 meta 收进一个目录（运维规范）
mkdir -p metas && cp *.meta.json metas/

# 拿到可疑文本后一键反查
aawm find-meta suspect_leak.txt metas --key key.json --registry reg.json --calibration calibration.json
# 8/8 段匹配 → 信道 B 检出 UID=0x0001 → 结论: 匹配 meta = metas/marked_long.meta.json
```

文本被改写也能命中（未改段落仍匹配），并精确定位被改段落（`篡改判定: 是 / 被改段落: [0]`）。

### 9.3 裸文本盲检（会漏检，慎用）

```bash
aawm trace marked_long.txt --key key.json --registry reg.json
# 检出水印: 否        ← 注意：实际是有水印的！
# 存在性得分: 11.5
```

原因：没有正确 `session_salt`，检测用的码本是随机码本，信号消失。**盲检"检出"才是铁证，"未检出"什么也证明不了。** 一定要用 find-meta 把 meta 找出来再下结论。

---

## 10. 生产配置清单

### 10.1 标定语料（最重要）

**作用**：用一批"没有水印的正常文本"拟合 null 分布，把存在性阈值从保守默认值（约 `1.6×带数`）降到实测值（约 `1.0/带`）。不标定会漏检中等信号文本（实测一篇存在性 25.5 的文本，标定后检出、未标定漏检）。

**怎么做**（v0.12 起推荐标定文件，免每次现场拟合）：

```bash
# 1) 一次性标定（语料 = 50~100 篇同领域正常文本，最少十几篇）
aawm calibrate ./calib -o calibration.json   # 生产
aawm calibrate --demo -o calibration.json    # 快速体验（包内置示例语料）
# 2) embed/trace/serve/proxy 统一传 --calibration calibration.json
```

- 标定文件携带 null 阈值模型 + p0 词频表（盐无关），运行时按当前密钥/盐
  重算，与现场语料标定**数学等价**——文件路径与 corpus 路径可互换。
- 旧方式仍支持：CLI `--calibrate-corpus <目录>`、技能包 `AAWM_CALIB=<目录>`、
  Python `Watermarker(..., calibrate_corpus=[...])`。
- Python：`Watermarker(..., calibration="calibration.json")`（路径或 dict 均可）。
- **嵌入与溯源两侧要用同一份标定**（同一文件或同一语料）。

### 10.2 meta 归档（第二重要）

- 每次嵌入产出 `<文件>.meta.json`，**与交付物一同归档**（git / 对象存储）。
- 建议统一进 `metas/` 目录——find-meta 一条命令全量反查。
- proxy 网关的 `--salt-archive salts.jsonl` 也是 meta 归档的一种（JSONL 格式，find-meta 直接支持）。

### 10.3 密钥管理

- `key.json` 权限 600（自动设置），目录 700；**离线备份**（丢失 = 全部历史水印不可溯源）。
- 不进代码库、不进日志、不进错误堆栈。
- 密钥是**对称的**——谁有 key 谁就能嵌入，只在可信边界内流转。
- 轮换：master_key 换了旧水印无法溯源；新旧并行期用两个 Watermarker 实例依次尝试，并给发布记录记 `key_version`。

### 10.4 上线前自查

- [ ] `aawm embed` 的统计行：词典命中 ≥ 30、容量 ≥ 16 bit？
- [ ] 已标定（`aawm calibrate` + `--calibration` / `AAWM_CALIB`）？embed 输出 reliability=high（或知悉 medium/low 的降级风险）？
- [ ] meta 已归档（本地 `<文件>.meta.json` 或 `metas/` 目录或盐归档）？
- [ ] 用自己嵌入的文本跑一遍 `trace`，确认"检出水印: 是"？
- [ ] `key.json` 已备份且未进代码库？
- [ ] `serve`/`proxy` 端口只在可信网络监听？

---

## 11. 故障排查

| 症状 | 原因 | 解法 |
|---|---|---|
| embed 统计行"词典命中"< 30 | 文本太短/词典覆盖不足 | 属容量限制（§12）；换 hybrid + 补充词典，或接受该文本无法可靠溯源 |
| trace 输出"检出水印: 否"但信度/存在性不低 | 未标定，阈值偏严 | 配 `--calibration` / `AAWM_CALIB` 后重试 |
| trace 输出"检出水印: 否"，篡改判定也是"否" | meta 没传对 / 盲检 | 用 `find-meta` 找回正确 meta；确认 trace 传了 `--meta` 或 `--salt` |
| hybrid 嵌入后 trace 漏检 | 溯源没传同一份补充词典 | 溯源加 `--supplementary-dict`（与嵌入同一份） |
| 解码出乱码 UID（如 0x0098） | meta 取错（多份候选时盲取） | 用 `find-meta` 而非猜；trace_file.sh 已拒绝多候选猜测 |
| `aawm` 命令找不到 | Python Scripts 不在 PATH | 跑 `install_local_shim.sh`（§4.2）；或 `python -m aawm.cli` |
| serve/proxy 起不来 "uvicorn 未安装" | 没装 server extra | `pip install 'aawm[server]'` |
| proxy 响应没水印 | key 未映射 / 文本 <50 字符 / 走了透传路径 | 核对 `--key-map`；看 `--salt-archive` 是否写入；代理日志 `aawm.proxy` |
| 401 Unauthorized | 客户端 key 不在 key-map，或上游 key 未配 | 检查 keys.json；配 `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` |
| 英文文本 trace 不可靠 | 英文 codec 为统计解码，UID 有 ~30% 误码 | 英文场景按需用 `soft_match=True` 软判决；测试层已用重试吸收 |
| WorkBuddy 升级后 aawm 失效 | shim 指向旧 Python | 重跑 `install_local_shim.sh` |

---

## 12. 已知限制

1. **极短文本**：词典命中不足 ~30 词时（约 <40 常用词命中），embed 可能静默成功但 trace 检不出——**交付前看统计行**。容量 <16 bit 时 UID 取低 `n_bits` 位（k-bit 语义，配合注册库仍能匹配）。
2. **meta 是溯源前提**：`session_salt` 决定码本映射，错误/缺失 meta 会导致漏检。盲检"未检出"不能作为排除依据。
3. **仅 UTF-8 文本**：图片/PDF/Office 等二进制不支持。
4. **英文 UID 解码**：英文 codec 为统计解码，UID 有约 30% 误码率，建议用软判决路径。
5. **未标定阈值偏严**：不配标定语料时存在性阈值保守，中等信号文本可能漏检（生产必配 `--calibration`）。
6. **密钥轮换**：master_key 更换后旧水印无法再溯源（需并行期策略）。

---

## 附：真实冒烟记录（本手册命令的实测输出）

| 命令 | 关键输出 |
|---|---|
| `aawm keygen -o key.json` | `master_key 已保存到 key.json` |
| `aawm registry add alice --registry reg.json` | `注册成功: alice -> UID 0x0001 (1)` |
| `aawm embed ... -o marked_long.txt` | `[统计] UID=0x0001, 词典命中=48, 存在性=25.5`，容量=16 bit |
| `aawm trace ... --meta marked_long.meta.json --calibration calibration.json` | `检出水印: 是 / 匹配用户: alice (汉明距=0)`，退出码 0 |
| 同命令**不带** `--calibration` | `检出水印: 否`（阈值 26.6 > 25.5）——标定的必要性 |
| `aawm find-meta suspect_rewritten.txt metas/ ...` | `7/8 段匹配` → 信道 B 检出 alice；`篡改判定: 是 / 被改段落: [0]` |
| `aawm serve ... --calibration calibration.json` + curl `/v1/trace` | `{"watermarked":true,"uid":1,"user":"alice",...}` |
| `aawm proxy ...` + curl chat completion | 响应被水印化；`salts.jsonl` 追加记录（含 bands+seal） |
