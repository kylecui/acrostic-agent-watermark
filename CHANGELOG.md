# Changelog

## 0.12.0 (2026-08-28)

主线：开箱体验（P0 产品化）——标定从"专家步骤"变成"一条命令"，短文本从"可能悄悄漏检"变成"明确分级仍嵌入"。

### P0-1 `aawm calibrate` 命令（一次标定、处处复用）

- 新增 `aawm calibrate <corpus> | --demo -o calibration.json`：产出标定文件（null 阈值模型 + p0 词频表）
- `--demo` 用包内置示例语料（5 篇中文技术散文，随 wheel 分发 `data/demo_corpus/`），开箱 30 秒体验全流程
- **p0_vocab 词频表机制**：词频表盐无关（按盐无关全词典词集 `_all_words` 统计），运行时用当前密钥/盐重算精确 p0，与现场 corpus 标定**数学等价**——标定文件 ~900 字节即等效携带整个语料
- null 模型密钥无关：同一份标定文件跨密钥复用；embed/trace/serve/proxy/find-meta 统一 `--calibration FILE`
- Python：`Watermarker(calibration=...)` / `from_config(calibration=...)` 接受 dict 或文件路径；`calibrate_null_model(corpus)` + `export_calibration()`

### P0-2 快速开始"必然成功"路径

- README 快速开始改写：keygen → registry → `calibrate --demo` → embed `--calibration` → trace（含无文本时用包内置示例长文直接体验的命令）
- user_guide / api_reference / plugin_guide 全面统一为标定文件流程（消除 README 与 user_guide 的标定方式矛盾）；修正 meta 文件名示例（`marked.meta.json`）

### P0-3 容量预检 + 可靠性分级（短文本不拒嵌）

- `EmbedResult.reliability`：`high`（容量 ≥10 bit，中文约 ≥1200 字）/ `medium`（6-9 bit，检出常存活、归因可能失败）/ `low`（<6 bit 或 weak_embed，结论仅供参考）
- CLI embed 输出 `[可靠性]` 分级说明（low 附原因与建议）；meta.json 写入 `reliability`；未标定运行打印 `[提示]` 引导 calibrate
- `server /v1/embed` 响应新增 `reliability` 字段；proxy salt 归档记录新增 `reliability`
- `wm.estimate_capacity(text)`：嵌入前容量预检（不改文本，随机盐估计）；`Watermarker.reliability_tier()` 静态分级规则

### 测试

- 新增 `tests/test_calibration.py`（9 项）：calibrate CLI 端到端、标定文件与 corpus 等价性、跨密钥复用、路径传参、reliability 分级、estimate_capacity、短文本不拒嵌

## 0.11.1 (2026-08-28)

- CI：新增 PyPI Trusted Publisher 发布 workflow（push tag `v*` → test → build → publish，OIDC 免 token）
- tests：import-error 测试守卫改为检测包已安装（`importlib.util.find_spec`）而非已加载（`sys.modules`），修复安装 langchain extras 的 CI 环境下误失败

## 0.11.0 (2026-08-28)

自 0.9.0 以来三轮迭代，主线：将"宁可弃权也不错误归因"贯穿 API 层与运营层。

### 归因防御（v0.10 轮）

- 新增 `attribution_confidence`（判别力 × 容量充分性，独立于存在性置信度）
- abstain 协议：AC < 0.5 时 `uid/user` 置 `None`，CLI 退出码 3
- `trace(soft_match=True, match_margin_ratio=0.3)` 成为默认；margin 拒绝后不再回退硬解码
- 低容量掩码碰撞一票否决
- 英文零感词典 `en_zero_cost.json`（133 组拼写变体/副词/安全对），英文默认走 zero_cost；null 模型按语言独立拟合；m=0 空证据守卫

### 运营层防御（第三轮）

- `EmbedResult.weak_embed` 警告标志；CLI embed 打印弱嵌入警告与可操作建议（中文 ≥1200 字 / 英文词典密集 ≥600 词）
- find-meta / trace 持 meta 时执行存档 UID 交叉校验（盐外证据），解码失真 → "不可判定"
- find-meta 裁决改进：多候选检出且无段哈希证据 → "不可判定"，不再输出错误 meta 结论

### API 层防御（第四轮）

- `facade.trace()` 新增 `archived_uid` 参数：存档 UID 交叉校验下沉到 API 层，消除 CLI/find-meta 与裸 API 之间的路径依赖；支持 k-bit 掩码对齐（`_uid_alias_match`）
- `server /v1/trace` 透传 `archived_uid`，由 facade 统一校验

### 工程卫生

- 示例 04/06 修复（英文默认词典切换的回归）
- 测试套件 305 → 348 项并全面确定性化（固定 key+盐、通用英文文本显式 `codec_mode="default"`），连续 3 轮全绿
- 新增 `tests/test_facade_archived_uid.py`：多盐扫描"绝不错误 UID/用户"护栏

### 已知边界（结构性，文档化）

- 中度攻击（≥50% 裁剪 / 同义替换叠加）下 UID 归因崩溃 → 弃权而非错误归因
- 存在性统计量（Σ|z|）本质盐无关，多盐扫描存在性误报为结构性现象
- 无盐盲检不可能（盐是绿名单派生种子，设计使然）

## 0.9.0

- 首个公开版本：中文零感水印、内容寻址锚点、投票信道+纠错、密钥派生映射、防伪造、框架适配器（OpenAI/LangChain/LiteLLM/autogen/CrewAI）、FastAPI server
