# third_party 登记

## PostMark（lilakk/PostMark @ 347f159）
- 用途：E-B4/E-B5 外部复现对象（EMNLP 2024 官方实现）
- 许可证：仓库未附 LICENSE 文件——按研究复现/学术引用使用，论文中明确标注来源与 commit
- 数据：embedding 表与 paragram 词典来自 HF datasets/yapeichang/PostMark
- 关键结论（预研）：PostMark 检测统计量是**存在比例**（compute_presence，
  有界 [0,1]），不是 z 检验计票——F1（相关票膨胀）按构造不适用于它；
  F2/F3 可测（E-B5）。此"不同统计量 ⇒ 不同坑剖面"本身作为论文论点。

## 获取方式（不入库，.gitignore）
```bash
git clone --depth 1 https://github.com/lilakk/PostMark.git papers/third_party/PostMark
cd papers/third_party/PostMark
curl -LO https://huggingface.co/datasets/yapeichang/PostMark/resolve/main/filtered_data_100k_unique_250w_sentbound_nomic_embs.pkl
curl -LO https://huggingface.co/datasets/yapeichang/PostMark/resolve/main/paragram_xxl.pkl
```
（预研时所用 commit：347f159）
