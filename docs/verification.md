# 验证说明（v0.15 起如实表述）

## 谁验证的、怎么验证的

本项目的验证**全部由项目作者执行**（单人维护仓库，提交历史可查），
部分轮次借助 AI 编码助手做独立复核（俗称"外部验证"的
VERIFICATION_REPORT #1–#10 即此类）。此前 README 称"七轮独立外部
验证"，v0.15 起改为如实表述：

- ✅ **多轮自验证 + AI 工具辅助复核**（10 轮，GitHub issue #1–#23 闭环）
- ✅ 全部验证产物公开可复现（fixed seed 实验脚本、逐字节核验记录）
- ❌ **尚无第三方独立审计**——密码学/统计安全结论未经独立专家复核

## 各轮验证做了什么

| 轮次 | 对象 | 方式 | 产物 |
|---|---|---|---|
| #1–#8 | v0.9–v0.13 功能与对抗 | 作者自测 + 工具复核 | CHANGELOG 对应条目 |
| #9（issue #18/#19/#20） | API 误用防护 | 复现→修复→回归测试 | tests/test_v0141_issue_fixes.py |
| #10（issue #23） | soft 归因真值缺席 | 复现→修复→回归测试 | tests/test_v0142_issue23_fix.py |
| v0.15 | 统计缺陷/盐找回/证据掩码 | 本仓库质量通行证 | tests/test_v015_quality_pass.py + docs/cross_domain_fpr_report.md + docs/embed_quality_report.md |

## 使用者应如何看待这些结论

1. **工程结论**（API 行为、往返成功、CLI 流程）可信度高——423+18 项
   自动化测试每次发版全绿，可随时本地复跑 `pytest tests/`。
2. **安全结论**（抗攻击、FPR/TPR 声明）按对应实验报告的限定条件采信：
   - 检出率/误报率只在**域内标定**前提下成立（跨域标定实测 FPR 19–53%，
     见 docs/cross_domain_fpr_report.md）
   - "检出 90–100%"是域内长文 + 域内标定的联合条件，不是通用承诺
3. **需要法律取证级归因的场景，本项目不适用**（README 边界表已声明，
   此结论同样未经第三方审计背书）。

## 复现入口

```bash
pytest tests/                                    # 441 项自动化测试
python experiments/exp_cross_domain_fpr.py       # 跨域 FPR（~10 分钟）
python experiments/exp_embed_quality.py          # 嵌入质量代理指标
```
