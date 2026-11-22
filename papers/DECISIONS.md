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

## D-2026-09-11-02：E-B5 阳性对照废弃与 N4 可复现性发现

**事实**：PostMark 的 `NomicEmbed.__init__` 在每次进程内用
`random.sample` 从 10 万词嵌入中抽 3266 词构造 embedding table
（`models.py` L170-176），随机状态取决于 import 后、初始化前的
random 消耗顺序。官方 artifact 的水印词表绑定他们进程内的表——
跨环境无法逐位复现（本环境实测同批 text2 的 TPR@1%FPR 仅 0.27）。

**处置**：E-B5 只主张 null 侧迁移结论（P1/P2），阳性对照标记
invalid_env_mismatch；N4 作为论文的可复现性发现（§6 讨论）。

## D-2026-09-11-03：W4/W5 完成记录

- 论文 B 全文初稿完成（abstract + 8 节 + 附录，ACL 格式骨架）。
- W5 内审：12/12 数字核对通过（容差 ±0.006，含 3 处舍入对齐说明）；
  E-B8 回归 441 项核心测试全绿；六条纪律逐条过——
  claim 映射 13 条全 verified；种子固定；阴性结果（EB2 cap1 TPR=0、
  EB3 demo matched 46.7%、EB5 阳性对照失效）全部入文未隐藏；
  数字全带域/标定/路径口径；baseline 忠实官方参数；措辞红线遵守
  （"zero-cost"未作主张，用 low-distortion 表述）。
- LaTeX 编译未验证：本机无 pdflatex，编译检查列入拿到 TeX 环境后的
  第一个动作（登记为 W6 前置项）。

## D-2026-09-11-04：事故记录——模型权重被 LFS 截获入库及处置

**事故**：E-A1/A3 提交时 `git add -A` 把 ModelScope 下载的 Qwen 权重
（3B fp16 + 7B AWQ，约 11.6GB）带入提交；仓库 .gitattributes 的 LFS
规则将 safetensors 存入 .git/lfs（普通 gc 不可见，故首次 gc 后仍 12G）。

**处置**（均已执行并验证）：
1. `git rm --cached models` + `models/` 入 .gitignore，amend 提交——
   权重留在工作区供实验、不进版本库；
2. `git filter-branch` 清除历史中误提交的 paragram_xxl.pkl（113MB），
   删 refs/original 后 gc——历史无 >2MB 对象；
3. `git lfs prune` 清孤儿 LFS 对象 4 个/.git 从 12G 降至 3.0M；
4. `git fsck --strict` 干净；filter-branch 重写了本地 tag 引用
   （v0.14.x 内容未变、SHA 不变），下次 fetch 以 origin 为准即可。

**教训**：`git add -A` 前先确认 .gitattributes 的 LFS 范围与大文件
落地路径；实验产物目录（models/、.venv/）应先入 .gitignore 再工作。
EOF

**教训**：`git add -A` 前先确认 .gitattributes 的 LFS 范围与大文件
落地路径；实验产物目录（models/、.venv/）应先入 .gitignore 再工作。
