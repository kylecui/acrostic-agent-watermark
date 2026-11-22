# claims.md（I-4）：claim → 实验 → 数据 → 章节 映射表

> 写作期间每加入一个可检验断言必须登记一行；投稿前逐条核对
> （PLAN §3.1）。状态: verified=实验已跑且数字已核；pending=待实验。

| # | claim（措辞即论文用语） | 实验 | 数据文件 | 章节 | 状态 |
|---|---|---|---|---|---|
| C1 | 无水印文本中同一词典词重复 N 次，存在性得分随 N 无界增长（N=640 时 Σ|z|=25.3，越过默认阈值 3 倍） | E-B1 | papers/paperB/data/eb1_correlated_votes.json | §3.1 | verified |
| C2 | 每词计票封顶到 3 使上述攻击得分钉死在 1.73（阈值的 22%） | E-B1 | 同上 | §3.3 | verified |
| C3 | 封顶值是权衡旋钮：域内未标定下 cap=3 的 TPR（62.5%）为各档最高且 FPR=0；cap≥5 时 FPR 升至 18.8–31.2% | E-B2 | eb2_cap_tradeoff.json | §3.3 | verified |
| C4 | 跨域阈值标定使存在性 FPR 从 0–6.9%（未标定）恶化到 3.5–84%；域内标定压回 0–7.7%（demo 域 46.7% 反例见 C4b：标定语料 14 篇过少时域内标定也不可靠） | E-B3 | eb3_domain_matrix.json | §4.2 | verified |
| C5 | 域内标定下 TPR 74–100%（demo 1500 字块 48% 为长度代价反例）；未标定域外 TPR 4–62% | E-B3 | 同上 | §4.2 | verified |
| C6 | 模板文未标定存在性 FPR 68–75%、误归因 FPR 13–28%（两次独立采样）；误归因不随库规模单调（1/10/50/200 用户：14%/46%/13%/1%），但胜出候选得分占比（0.30→0.64）与带方向一致率（0.64→0.81）随库规模上升——对齐是真实的，弃权门槛恰好卡在该机制上 | E-B6 | eb6_template_attribution.json | §5.2 | verified |
| C7 | 误归因机理：胜出候选得分占比最高 0.64、带方向一致率最高 0.81——信号与噪声候选真实对齐（>0.5 守门阈值被绕过） | E-B6 | 同上 | §5.2 | verified |
| C8 | 缓解分级：域内标定把模板文 FPR/误归因清零（0/0 vs 74%/28%）；相似度预过滤在本病态域拒绝率 100%（饱和，自然模板域需补测——登记为 E-B7b） | E-B7 | eb7_template_mitigation.json | §5.3 | verified（E-B7b 待补） |
| C9 | F1 是方案类别性质：在与 AAWM 无共享代码的独立参考实现（最小 z 检验水印）上，同一攻击构造产生相同的 |z|∝√N 增长曲线（N=640 时 25.3） | E-B4 | eb4_reference_impl.json | §3.4 | verified |
| C10 | F2 严重度依赖统计量：PostMark（存在比例统计量）跨域 FPR 仅 0.5–5%（vs AAWM z 检验 19–84%）；F1/F3 按构造不适用于有界统计量（模板文得分恒 0） | E-B5 | eb5_postmark_transfer.json | §4/§6 | verified |
| C11 | PostMark 官方 artifact 跨环境不可逐位复现：embedding table 为每次初始化随机子集（N4） | E-B5 废弃的阳性对照 | eb5_postmark_transfer.json | 附录 | verified（作为可复现性发现） |
| C11b | 本环境完整复跑官方嵌入管线后阳性对照恢复：TPR@1%FPR=1.0（n=60，presence 0.10→0.57） | E-A4 | papers/paperA/data/ea4_postmark_pipeline.json | §4.2 | verified |
| C12 | 自然度模板（槽位填充，unique 句比例 0.47–0.76）：未标定 FPR 30%、域内标定 2%、误归因为 0——误归因需要极端重复（纯复制），存在性误报在"有变化的模板"上依然高发 | E-B7b | eb7b_natural_template.json | §5.3 | verified |
| C13 | 相似度预过滤在自然模板域拒绝率 12%（仅拦截 <0.5 极端桶），放行集 FPR 4.6%——预过滤是兜底而非主缓解，主缓解仍是域内标定 | E-B7b | 同上 | §5.3 | verified |
