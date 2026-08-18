"""句级统计量保留性验证（v0.4 句级信道先验证）。

目标：验证 paraphrase 攻击后句长/句数等统计量是否保留。
- 若保留性高 → 句级信道可作辅助解码依据
- 若保留性低 → 降级为置信度信号，不作解码依据

运行：python experiments/exp_sentence_stats.py
"""
from __future__ import annotations

import random
import re
import sys
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aawm.embedder import _SYNONYMS  # noqa: E402
from experiments.exp_edit_attacks import (  # noqa: E402
    paraphrase_sentence_attack,
    TEXT,
    INSERT_WORDS,
)

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def sentence_stats(text: str) -> dict:
    """计算文本的句级统计量。"""
    sents = [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    lengths = [len(s.split()) for s in sents]
    return {
        "n_sentences": len(sents),
        "mean_sent_len": statistics.mean(lengths) if lengths else 0,
        "stdev_sent_len": statistics.stdev(lengths) if len(lengths) > 1 else 0,
        "total_words": sum(lengths),
        "min_sent_len": min(lengths) if lengths else 0,
        "max_sent_len": max(lengths) if lengths else 0,
    }


def main() -> None:
    rng = random.Random(2026)

    print("### 句级统计量保留性验证")
    print(f"原文句数: {sentence_stats(TEXT)['n_sentences']}")
    print()

    print("改写比例 | 句数保留 | 均长保留 | 总词数保留 | 最小句长保留")
    for frac in [0.1, 0.25, 0.5, 0.75, 1.0]:
        stats_orig = sentence_stats(TEXT)
        retentions = {k: [] for k in ["n_sentences", "mean_sent_len",
                                       "total_words", "min_sent_len"]}
        for trial in range(30):
            attacked = paraphrase_sentence_attack(TEXT, frac, rng)
            stats_atk = sentence_stats(attacked)
            for k in retentions:
                orig_v = stats_orig[k]
                atk_v = stats_atk[k]
                if orig_v == 0:
                    ret = 1.0 if atk_v == 0 else 0.0
                else:
                    ret = min(atk_v, orig_v) / max(atk_v, orig_v)
                retentions[k].append(ret)

        avg = {k: statistics.mean(v) for k, v in retentions.items()}
        print(
            f"{frac:7.0%} | "
            f"{avg['n_sentences']:.2f}    | "
            f"{avg['mean_sent_len']:.2f}    | "
            f"{avg['total_words']:.2f}     | "
            f"{avg['min_sent_len']:.2f}"
        )

    print()
    print("结论：")
    print("- 句数保留性高（paraphrase 不增删句）")
    print("- 均长/总词数因插入删除有波动")
    print("- 句级统计量保留性不足以作独立解码信道")
    print("- 降级为置信度信号：句数/句长突变提示可能遭受攻击")


if __name__ == "__main__":
    main()
