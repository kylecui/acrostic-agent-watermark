# 决策日志

## D-2026-09-11-01：网络与算力环境勘误（重大修正）

**背景**：PLAN v1.0 的风险登记册 D-1（无网络）承袭自项目旧文档的沙箱
记忆，未经实测。**实测推翻**：pypi/github/huggingface 全部可达；
本地 RTX 3060 12GB（空闲 ~7.7GB）可用；Pi 环境已配置多个外部 API
（SiliconFlow / Gemini / Zhipu / Kimi）。

**影响**：
- DP-1 默认由"(a) 参考实现降档"改为"先尝试官方外部代码复现"
- D-2（LLM 凭据阻塞）解除：E-A1/E-A2 走 Pi 已配置 API
- E-A3 的 PPL 用本地 GPU 跑本地 LM；Dipper 攻击优先走 API 改写，
  本地 Dipper（11B T5-4bit ~6GB）作为备选
- 新增基础设施任务 I-5（GPU 环境搭建：torch CUDA 版 + transformers）
- 论文 A 的启动阻塞仅剩"B 达 M2"一项，E-A1 语料生成可提前并行

**教训**：环境假设必须实测，不得从旧文档继承。
