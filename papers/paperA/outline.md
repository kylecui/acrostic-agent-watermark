# 论文 A 章节大纲（M6 骨架 · 随 E-A2/A3 数据迭代）

工作标题：*AAWM: Agent-Level Watermarking for Black-Box LLM Outputs*

论点（PLAN §5.1 冻结）：agent 级水印填 model 级（改 logits）与密码学
身份（签名非水印）之间的空白：不改 LLM、闭源 API 可用、归因到 agent
实例；双信道（Merkle 绑定 + 统计溯源）+ 16-bit 多用户 UID + abstain 协议。

## 1. Introduction
- gap 论证（3 段：model 级 / 行为级 / 密码学身份 → 中间空白）
- 贡献：系统设计 + 多用户编码 + abstain 协议 + **真实 LLM 语料评测**
  （区别于 Paper B 的构造语料）

## 2. System Design
- 双信道架构图；密钥派生（HKDF 树：green/band/binding/salt）
- 多用户编码：band bits + 容量自适应 + 冗余 layout + 注册库 soft-match
- abstain 协议与证据掩码（v0.15）
- 派生盐找回（meta 散失恢复）——运维韧性作为设计贡献

## 3. Experimental Setup（E-A1 语料）
- 1020 篇真实 LLM 交付物（SiliconFlow Qwen2.5-32B，3 体裁 × 2 语种，
  fixed seed，费用 ¥X 可溯源）——与 Paper B 构造语料的差异化

## 4. Detection & Attribution under LLM Paraphrase（E-A2）
- 15/30/50% 真实改写攻击下的 TPR / 归因存活 / abstain 率
- 域内标定 vs 未标定；与 Paper B Finding 2 的标定要求呼应

## 5. Quality: PPL of Watermarked Text（E-A3）
- Qwen2.5-7B-AWQ 本地困惑度：原文 vs 水印文 NLL 比（mean/median/p95）
- 声明边界：PPL 代理 + 零感词典设计意图；人工盲评列入 future work

## 6. Comparison & Related Work
- PostMark/SimMark/KGW 家族对照表（能力窗口差异：多用户 UID 是差异点）
- 诚实边界：同义狠攻下归因弃权（Paper B F3 呼应）

## 7. Limitations
- 对称密钥；词典覆盖的语种差异（英文 zero_cost 稀疏）；
  法律取证级不适用

## 附录
- 语料生成 prompt 全量；费用明细；适配器清单（OpenAI/LangChain/
  LiteLLM/AutoGen/CrewAI）
