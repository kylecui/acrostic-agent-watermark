# Changelog

## 0.13.0 (2026-08-28)

主线：P1/P2 产品化收尾——可运维（指标/审计/密钥轮换/meta 存储）、更可靠（CRC-16 / UID 冗余 / 词典指纹）。

> **升级注意（CRC-16 迁移）**：v0.13 起 `CAConfig.crc_bits` 默认 16（CRC-16/CCITT-FALSE），
> payload 从 24 bit 变 32 bit（16 UID + 16 CRC）。**v0.13 之前嵌入的文本**需显式传
> `CAConfig(crc_bits=8)` 解码；v0.13 嵌入的文本用旧配置解不出（CRC 位宽不匹配，安全失败）。

### P1-4 指标（`aawm.server.metrics` + `GET /metrics`）

- `Metrics` 计数器/观测值（`inc / observe / time_it`），文本格式渲染（Prometheus 兼容）
- `aawm serve` 新增 `GET /metrics` 端点：trace 检出/未检计数、abstain 计数、耗时分布

### P1-5 审计（`aawm.audit`）

- `AuditLogger`（append-only JSONL）+ 全局 `set_audit_logger / get_audit_logger / audit`
- `text_fingerprint(text)`：SHA-256 前 16 hex，事件统一携带
- CLI `embed/trace/find-meta/serve` 新增 `--audit-log FILE`；事件 schema 统一为
  `op: trace|embed|find_meta` + `source: cli|server|sdk`（CLI 原误用 `event` 键，已修正）
- **facade SDK 层挂载**（第六轮外部验证唯一缺口）：`Watermarker.embed/trace`
  经 `audit_sdk` 写 `source=sdk` 事件——`set_audit_logger` 后"3 行代码接入"
  的 SDK 主路径自动留痕；无全局记录器时零开销 no-op
- server 路径去重：`/v1/trace`、`/v1/embed`、`/v1/find-meta` 在请求层自行
  审计（source=server），内部调用经 `suppress_sdk_audit` 抑制 facade 重复
  审计——同一操作只落一条事件

### P1-6 密钥轮换（`KeyStore` 多版本 + `aawm rotate-key`）

- `KeyStore` 升级 v2 格式：`version/active/keys` 多版本并存，`rotate()` 追加新版本并切换
  active；`drop_version()` 应急删除（禁删 active）；兼容加载 legacy v1 单钥格式
- `embed` 写入 `key_version`；`trace` 按 meta 中的 `key_version` 取对应密钥——
  轮换后旧水印仍可溯源（双钥并行期）
- CLI `aawm rotate-key --key key.json [--drop N]`

### P1-7 meta 存储（`aawm.meta_store`，JSONL / SQLite 双后端）

- `FileMetaStore`（JSONL）/ `SqliteMetaStore`（SQLite 索引表）/ `open_meta_store(path)`
  按扩展名分发；接口：`put / get / find_by_text_hash / find_by_para_hash`
- CLI `embed --meta-store FILE` 嵌入自动存档；`find-meta --meta-store FILE`
  段落哈希反查（嫌疑文本逐段哈希查索引，**无需逐份 meta 文件**）

### P2-8 UID 冗余（`uid_redundancy`）

- `embed(uid_redundancy=r)`：UID 拆 r 份冗余嵌入（zero_cost/hybrid 模式，
  default 模式无自适应容量，传 r>1 显式抛 `ValueError`）
- `EmbedResult.uid_layout` 记录冗余布局；`trace(uid_layout=...)` 多数表决还原——
  段落裁剪 50% 仍可归因不翻转；容量代价：k_uid = k // r

### P2-9 词典指纹（`dict_version`）

- `GreenlistCodec.dict_version`：词典内容 SHA-256 指纹（16 hex，盐无关）
- `embed` 写入 meta；`trace` 重建 codec 后比对，结果记录在
  `TraceResult.dict_version_match`——溯源侧词典与嵌入侧不一致时显式暴露

### P2-10 CRC-16（`CAConfig.crc_bits=16` 默认）

- `crc16`（CRC-16/CCITT-FALSE，多项式 0x1021，初值 0xFFFF）+ `compute_crc` 按位宽分发
- CRC-16 使 32 桶投票信道单 bit 翻转检出从 ~1/256 提升到 ~1/65536
- chase 算法重写（CRC-16 路径）：候选池放宽为全部弱桶（按 `(margin, total)` 升序）+
  配额 4 + 全局试验上限 24（无上限枚举在密集词典随机文本上实测 FP 1.1% → 0.05%）
- CRC-8 路径保持 v0.12 旧行为不变（order 截断 + 配额 3）

### 测试

- 新增 `tests/test_v013_features.py`（39 项）：CRC-16 编解码/往返/位宽混用安全、
  dict_version 跨盐稳定、UID 冗余往返+裁剪归因、keystore 轮换（legacy 兼容/rotate/drop）、
  facade key_version 轮换溯源、meta 双后端、audit、metrics、/metrics 端点、
  CLI rotate-key/--meta-store/--audit-log
- 全量回归 397 passed（基线 357 → 397）

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
