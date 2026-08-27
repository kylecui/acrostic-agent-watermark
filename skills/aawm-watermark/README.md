# aawm-watermark 技能包

给 AI agent 产出的**落盘文本交付物**自动/按需嵌入可溯源水印（AAWM 藏头式同义词水印），支持事后溯源定位泄露者。

- 对读者无感（零感词典模式），语义不变
- 持有密钥一方可溯源到具体 UID / 用户别名
- **Fail-open**：嵌入失败保留原文，绝不阻塞 agent 交付流程

> 本技能包管"交付物文件"。对话/终端里的文字回复流由 `aawm proxy` 代理网关在传输层拦截（见 [docs/cli_agent_proxy_guide.md](../../docs/cli_agent_proxy_guide.md)），两者互补。

## 目录结构

```
aawm-watermark/
├── SKILL.md                      # 技能指令（WorkBuddy/CodeBuddy/Codex 等可加载）
├── scripts/
│   ├── embed_files.sh            # 对落盘文本文件原位嵌入水印（fail-open）
│   └── trace_file.sh             # 对可疑文件执行溯源检测
├── hooks/
│   └── claude-code.hooks.json    # Claude Code PostToolUse 自动触发配置
└── README.md
```

## 快速开始

```bash
# 1. 安装 aawm（已安装可跳过）
pip install 'aawm[server]'

# 2. 初始化密钥与用户身份（首次）
aawm keygen --output key.json
aawm registry add alice --registry registry.json

# 3. 配置环境变量（建议写入 shell 配置 / agent 环境）
export AAWM_KEY=key.json
export AAWM_REGISTRY=registry.json
export AAWM_USER=alice          # 你的身份：UID 数字或别名
export AAWM_SKILL_DIR=/abs/path/to/aawm-watermark

# 3.5 标定语料（生产必做）——否则短文本可能检不出
mkdir -p calib && cp some_normal_texts/*.txt calib/   # 与交付物同领域的正常文本
export AAWM_CALIB=/abs/path/to/calib

# 4. 对交付物嵌入水印
bash "$AAWM_SKILL_DIR/scripts/embed_files.sh report.md summary.txt

# 5. 溯源（拿到可疑文件时）
bash "$AAWM_SKILL_DIR/scripts/trace_file.sh suspect.md
```

嵌入成功后会生成 `<文件>.meta.json`（含 `session_salt`/`seal`/`bands`）。**meta 与交付物必须一同归档**——溯源时自动读取，缺失则只能盲检（无 seal 校验）。

## 按平台安装

### 全局命令（推荐先做，任何 agent 都能发现）

aawm 装进 python 后其命令入口未必在所有 shell 的 PATH 中（实测 WorkBuddy 内置 python 的 Scripts 目录不在 PATH），导致 agent 检查 `command -v aawm` 失败、误判"没有现成水印工具"。运行安装脚本在用户 bin 目录（已在 PATH）生成 `aawm` / `aawm-embed` / `aawm-trace` 三个全局命令（bash + cmd 双版本）：

```bash
bash skills/aawm-watermark/scripts/install_local_shim.sh
command -v aawm        # 之后任何 bash/agent 都能命中
aawm embed <文件>      # 或语义化别名 aawm-embed <文件>
```

> WorkBuddy 升级内置 python 版本后 shim 失效，重跑一次即可（脚本自动取最新版本）。

### WorkBuddy / CodeBuddy

把技能包复制到**当前项目**的技能资源目录（WorkBuddy 启动时扫描加载）：

```bash
# 项目资源目录（把 <pid> 换成你的项目 ID，见 ~/.workbuddy/project-resources/ 下与项目 mcp.json 同级的目录名）
cp -r skills/aawm-watermark ~/.workbuddy/project-resources/<pid>/aawm-watermark
```

之后在 WorkBuddy 里说"给这个文件加水印 / 溯源"即会触发本技能；要求 agent 产出最终交付物时，它会按 SKILL.md 指令在交付前自动嵌入。

> 若 agent 仍不触发，可直接给命令：`aawm-embed 文件1 文件2` / `aawm-trace 可疑文件`。

### Claude Code（自动触发，无需记忆）

1. 把 `hooks/claude-code.hooks.json` 合并进项目的 `.claude/settings.json` 的 `hooks` 字段（或用户级 `~/.claude/settings.json`）。
2. 确保启动 Claude Code 的 shell 已 `export AAWM_KEY/AAWM_REGISTRY/AAWM_USER/AAWM_SKILL_DIR`。
3. 之后 Claude Code 每次 `Write`/`Edit` 落盘文本文件，都会**自动**调用 `embed_files.sh` 嵌入水印。

```bash
mkdir -p .claude
# 手动合并，或复制后改：
cp "$AAWM_SKILL_DIR/hooks/claude-code.hooks.json" .claude/settings.json
```

> hook 只监听 `Write|Edit|MultiEdit` 落盘；二进制扩展名在脚本内自动跳过。hook 失败不影响 Claude Code 主流程（fail-open）。

### 其他平台（Codex / opencode 等）

这些平台无 PostToolUse 式自动触发时，靠 SKILL.md 指令驱动：agent 产出交付物后主动调用 `scripts/embed_files.sh`。也可用平台自己的 hook/plugin 机制包装同一脚本（`--comma` 参数兼容逗号分隔路径列表）。

## 环境变量速查

| 变量 | 必填 | 说明 |
|---|---|---|
| `AAWM_USER` | 是 | 用户身份：UID 数字（`41244`）或注册库别名（`alice`） |
| `AAWM_KEY` / `AAWM_KEY_HEX` | 是* | master_key 文件路径 / hex，二选一 |
| `AAWM_REGISTRY` | 推荐 | 注册库 JSON 路径（别名解析与溯源必需） |
| `AAWM_LANGUAGE` | 否 | `auto`/`zh`/`en`，默认 `auto` |
| `AAWM_CODEC` | 否 | `zero_cost`/`default`/`hybrid`，默认 `zero_cost` |
| `AAWM_CALIB` | **生产必配** | p0/null 标定语料路径（目录或文件）。未标定时存在性阈值偏严（约 7.4），短文本可能检不出；标定后按实测 null 分布（约 1.0/带）判定，可靠 |
| `AAWM_DRY_RUN` | 否 | 非空时只打印命令不执行 |
| `AAWM_SKILL_DIR` | 否* | 技能包绝对路径（Claude Code hooks 需要） |

## 溯源运维

1. **meta 归档**：交付物与其 `.meta.json` 一同存档/备份（git、对象存储均可）。
2. **密钥分级**：`key.json` 只在溯源侧持有；嵌入侧可用 `AAWM_KEY_HEX` 或服务端封装，避免 key 扩散。
3. **多用户**：每个成员注册独立别名（`aawm registry add <别名> --registry registry.json`），交付时各自的 `AAWM_USER` 指向自己。
4. **组织级部署**：可把 `aawm serve`（HTTP 检测服务）或 `aawm proxy`（agent 网关）作为团队共享服务，SKILL 调用同一套密钥与注册库。

## 限制

- 仅 UTF-8 文本文件；二进制（图片/PDF/Office）自动跳过
- 对话/终端回复流由 `aawm proxy` 覆盖，本技能包不处理
- 超短文本嵌入容量有限；中英混合长文本需先用 `--calibrate-corpus` 标定以获得稳定存在性判定
