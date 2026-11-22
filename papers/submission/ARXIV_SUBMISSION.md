# arXiv 提交指南（凭据不在本环境，点击动作留给所有者）

## 论文 B（先投）
- 包：`paperB_arxiv_v1.tar.gz`（main.tex + sections/ + figures/ + refs.bib + main.bbl）
- 标题：Correlated Votes, Non-transferable Null Models, and the Template
  Attack: Pitfalls of Statistical Watermarks on Text
- 分类：cs.CR（主）+ cs.CL；评论字段可注明 companion paper
- License：arXiv non-exclusive（与 MIT artifact 无冲突）
- 摘要：见 paperB/main.tex 的 abstract 环境（纯文本粘贴，去掉 LaTeX 命令）

## 论文 A（B 挂出后一周内投，互为 companion）
- 包：`paperA_arxiv_v1.tar.gz`
- 标题：AAWM: Agent-Level Watermarking for Black-Box LLM Outputs
- 分类：cs.CR + cs.CL
- 摘要：见 paperA/main.tex

## 提交前 checklist
1. 本地 `tectonic main.tex` 复编译一遍（两包均已验证 0 error / 0 undefined）
2. arXiv 上传 tar.gz → 选择 pdflatex → 预览确认图表
3. 提交后把 arXiv ID 回填到 README（papers/PLAN.md W6 打勾）
