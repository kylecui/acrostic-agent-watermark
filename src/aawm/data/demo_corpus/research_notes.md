# 起步研究笔记

**时间**：2026-08-17
**目标**：用"藏头诗"思路实现 **agent 级**数字水印（非 model 级）

---

## 1. 领域扫描

### 1.1 Model 级 LLM 水印（**不是**我们要做的，但要理解边界）

| 方案 | 嵌入层 | 是否 distortion-free | 黑盒可用 | 备注 |
|---|---|---|---|---|
| **KGW** (Kirchenbauer et al., 2023, ICML) | logits（green list 偏置 δ） | 否（轻微偏置分布） | 仅需 tokenizer+hash | 鼻祖，z-score 检测 |
| **Aaronson** (2022, OpenAI 原型) | 采样（Gumbel-max + PRF） | 是（期望无偏） | 否（需 PRF 接入采样） | 未公开部署 |
| **SynthID-Text** (Google DeepMind, 2024, Nature) | 锦标赛采样 | 近似无偏 | 否 | Gemini 生产部署，~2000 万对话无质量差异 |
| **Kuditipudi** (2023) | permutation-based | 是 | 否 | 对编辑鲁棒 |
| **E2E-LLM-Watermark** (ICML 2025) | logits 端到端学习 | 否 | 否 | 联合优化编码器/解码器 |
| **StealthInk** (ICML 2025) | multi-bit，保分布 | 是 | 否 | 嵌入 userID/timestamp/modelID |

**共同特征**：全部需要**在 LLM 推理路径上动手**（改 logits 或改采样），不适用于闭源 API 用户。

### 1.2 Agent 行为水印（我们的近邻）

**Agent Guide**（Huang et al., arXiv 2504.05871, 2025-04，v3 2026-05）
- **唯一**明确区分"agent 行为水印"vs"LLM token 水印"的工作
- 把 agent 行为拆两层：
  - **behavior**：是否做某动作（如"是否收藏"）
  - **action**：具体执行（如"用什么标签收藏"）
- 水印偏置 **behavior 概率分布**，action 保持自然
- 检测：z 统计量，多轮累积
- 局限：聚焦 social media 场景，水印载体是"是否做动作"的二值选择，**未触及 token 变换**；信息容量低（每轮约 1 bit）

### 1.3 "藏头诗"式 token 水印（我们的直接借鉴源）

**Ensemble Watermarks**（Niess & Kern, ACL 2025）
- 三特征集成：acrostic（句首字母）+ sensorimotor（感官词类）+ red-green（KGW）
- acrostic 实现：每个新句首 token 偏置为以密钥派生字母开头
  - `logits[t] += δ_acro · 1{starts_with_acrostic_letter}`
- 检测：binomial 检验

**In-Context Watermarks (ICW)**（ICLR 2026）
- 四种黑盒策略：Unicode / Initials / Lexical / **Acrostics**
- Acrostics ICW：密钥 $k_s$ 采样序列 $\zeta$，让句首字母依次对齐 $\zeta$
- 检测：Levenshtein 距离 $d(\ell, \zeta)$ → z-score $D = (\mu - d)/\sigma$
- 关键发现：**模型能力越强，ICW 越可行**（gpt-o3-mini 几乎满分，gpt-4o-mini 只有 Unicode 可用）
- 仍属 **model 级**：依赖 LLM 指令遵循能力，水印在 LLM 输出阶段产生

### 1.4 Agent 身份与密码学归因（另一条路，非水印）

- **AgentPin / AgentID / AIP / HUMAN Verified AI Agent / Block Buzz (Nostr keypair) / Sigil**
- 全部走**密码学签名**路线：Ed25519 / ES256 / Biscuit token / DID
- 证明"这个 agent 是谁"，**不证明"这段输出是这个 agent 产的"**（除非每条输出都签名，但那就是 MAC/签名而非水印）
- 与本项目正交：可叠加（水印做统计归因，签名做身份证明）

### 1.5 关键洞察

现有工作分布在两极：
- **左极**：model 级 token 水印（KGW / SynthID / ICW / Ensemble）—— 精细、统计可验，但**必须改 LLM**
- **右极**：agent 密码学身份（AgentPin / AIP / Sigil）—— 不改 LLM，但**是签名不是水印**，不隐藏在输出里

**中间空白**：agent 在**不改 LLM**的前提下，对自己的**输出 token**做轻量变换嵌入水印。这正是本项目要填的位置。

---

## 2. 差异化定位

### 2.1 我们做什么

**Acrostic Agent Watermark (AAWM)**：agent 拿到 LLM 原始输出后，在**编排层**对若干 token 做轻量变换，使验证者凭密钥能如藏头诗般读出隐藏信号。

### 2.2 我们不做什么

- ❌ 不改 LLM 的 logits 或采样（那是 model 级）
- ❌ 不依赖 LLM 的指令遵循（那是 ICW）
- ❌ 不对输出做密码学签名（那是身份证明，非水印）
- ❌ 不做行为级二值水印（那是 Agent Guide）

### 2.3 与相邻工作的对比

| 维度 | KGW | SynthID-Text | ICW (Acrostics) | Agent Guide | **AAWM（本方案）** |
|---|---|---|---|---|---|
| 嵌入主体 | LLM 引擎 | LLM 引擎 | LLM（指令遵循） | Agent 行为层 | **Agent token 后处理层** |
| 需要 LLM 改造 | 是 | 是 | 否（但依赖能力） | 否 | **否** |
| 黑盒 API 可用 | 否 | 否 | 是（但质量随模型） | 是 | **是** |
| 水印载体 | token green list | 锦标赛 | 句首字母 | 行为二值 | **可变 token 的谓词满足** |
| 信息归属 | 模型 | 模型 | 模型（+提示） | agent | **agent 实例** |
| 验证所需 | tokenizer+key | classifier+key | key | key | **key（+轻量词典）** |
| 鲁棒性（paraphrase） | 中 | 中 | 高（句首稳） | 中（多轮） | **待测（设计目标：中-高）** |

---

## 3. 技术路线候选

### 路线 A：同义替换 + 谓词锚点（推荐 MVP）

**核心**：agent 在输出文本中，用密钥派生一组锚点位置，在每个锚点位置从同义候选 token 中选满足水印谓词的。

```
原始输出: "The system is fully operational and ready for deployment."
                ^                    ^                    ^
            锚点1                锚点2                锚点3
候选: {The, This}        {fully, completely}   {ready, prepared}
谓词: 哈希(密钥, 位置) → bit → "首字母在 A-M" 或 "在 N-Z"
```

**优点**：实现简单，黑盒可用，语义保持
**缺点**：需要同义词典；容量受可变 token 比例限制

### 路线 B：Unicode / 零宽字符（ICW Unicode 的 agent 化）

**核心**：agent 在 token 边界插入零宽字符编码水印比特。
**优点**：高容量、对读者透明
**缺点**：对 paraphrase 极脆弱（ICW 实验已证实），且易被平台清洗

### 路线 C：句式模板选择（Acrostics ICW 的 agent 化）

**核心**：agent 在多候选句式中选满足藏头约束的。
**优点**：对 paraphrase 鲁棒
**缺点**：容量低，依赖 agent 有多候选生成能力

### 路线 D：工具调用顺序编码

**核心**：agent 在多工具并行调用时，用调用顺序编码水印比特。
**优点**：完全在 agent 控制内，不影响文本
**缺点**：只适用于多工具场景，通用性差

**MVP 决策**：路线 A（同义替换谓词锚点）作为 v0.1，路线 C 作为 v0.2 扩展，路线 D 作为 agent-native 特色未来探索。

---

## 4. 关键设计问题（待 design.md 展开）

1. **锚点选择**：如何用密钥 + 上下文哈希派生锚点位置？如何避免被攻击者推断？
2. **候选生成**：agent 如何拿到同义候选？用词典、用 LLM 再生成、还是用 embedding 近邻？
3. **谓词设计**：谓词应满足什么性质？（可验证性、抗推断、语义保持、鲁棒性）
4. **统计检验**：检测时的 z-score 如何构造？零分布如何估计？
5. **鲁棒性边界**：对 paraphrase / 翻译 / 截断的鲁棒性目标是什么？
6. **容量下界**：给定目标 TPR@1%FPR，最少需要多少锚点？
7. **威胁模型**：攻击者能做什么？（知道有水印但不知密钥 / 主动 paraphrase / 主动 token 替换）

---

## 5. 参考文献

### Model 级 LLM 水印
- Kirchenbauer et al., 2023. "A Watermark for Large Language Models." ICML 2023.
- Aaronson, 2022. "My AI Safety Lecture for UT Effective Altruism." blog.
- Dathathri et al., 2024. "Scalable Watermarking for Identifying LLM Outputs." Nature.
- Kuditipudi et al., 2023. "Robust Distortion-free Watermarking for LLMs."
- Wong et al., 2025. "E2E-LLM-Watermark." ICML 2025. https://arxiv.org/abs/2505.02344
- Jiang et al., 2025. "StealthInk: A Multi-bit and Stealthy Watermark for LLMs." ICML 2025.
- Bahri & Wieting, 2025. "A Watermark for Black-Box Language Models." ICLRW 2025.

### Agent 行为水印
- Huang et al., 2025. "Agent Guide: A Simple Agent Behavioral Watermarking Framework." arXiv:2504.05871.

### 藏头诗式 token 水印
- Niess & Kern, 2025. "Ensemble Watermarks for Large Language Models." ACL 2025. https://arxiv.org/abs/2411.19563
- Anonymous, 2026. "In-Context Watermarks for Large Language Models." ICLR 2026.
- Abdelnabi & Fritz, 2024. "Stylometric Watermarks for LLMs." arXiv:2405.08400.

### Agent 身份与归因
- AgentPin: https://research.thirdkey.ai/blog/introducing-agentpin
- AIP (Agent Identity Protocol): https://openagents.org/blog/posts/2026-02-03-introducing-agent-identity
- HUMAN Verified AI Agent: https://www.humansecurity.com/learn/blog/human-verified-ai-agent-open-source
- Block Buzz (Nostr keypair): https://github.com/block/buzz
- Sigil: https://github.com/chaddhq/sigil

### 其他
- ALiSa (Acrostic Linguistic Steganography): IEEE SPL 2022. https://ui.adsabs.harvard.edu/abs/2022ISPL...29..687Y
- "Frontier LLM steganography cliff" (acrostics work, diluted schemes fail): https://hypogenic.ai/blog/weekly-entry-260413

---

## 6. 下一步

1. ✅ 起步研究完成
2. ⏳ 写 design.md（架构 + 算法 + 威胁模型）
3. ⏳ 实现 v0.1 MVP（路线 A：同义替换谓词锚点）
4. ⏳ 基准评测（与 KGW / ICW 对比 robustness / capacity / quality）
5. ⏳ 写 examples（含闭源 API 调用演示）
