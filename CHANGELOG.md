# Changelog

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
