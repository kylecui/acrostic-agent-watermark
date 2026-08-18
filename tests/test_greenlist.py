"""信道 B（绿名单 × 频带统计）单元测试：v0.5。"""
import random

import pytest

from aawm.greenlist import BandReport, GreenlistCodec
from aawm.keys import generate_master_key, generate_session_salt

pytestmark = pytest.mark.order(4)


def make_codec(seed: int = 42) -> GreenlistCodec:
    rng = random.Random(seed)
    master = bytes(rng.randrange(256) for _ in range(32))
    salt = bytes(rng.randrange(256) for _ in range(16))
    return GreenlistCodec(master, salt)


def make_text(n_words: int, seed: int = 7) -> str:
    """模拟文本：约 35% 词典词 + 65% 填充词（与 exp_token_layer 一致）。"""
    rng = random.Random(seed)
    codec = make_codec()
    filler = [
        "system", "process", "value", "human", "between", "during",
        "though", "while", "should", "would", "might", "place",
        "group", "number", "change", "right",
    ]
    heads = list(codec._groups.keys())
    out = []
    for _ in range(n_words):
        if rng.random() < 0.35:
            out.append(rng.choice(codec._groups[rng.choice(heads)]))
        else:
            out.append(rng.choice(filler))
    return " ".join(out)


# ---------------------------------------------------------------------------
# 必修课管线不变量
# ---------------------------------------------------------------------------

class TestPipelineInvariants:
    def test_disjoint_ownership(self):
        """必修课 1：每个词唯一组归属（w2band 单值）。"""
        codec = make_codec()
        # w2band 本身是 dict，构造保证单值；再验证词不出现在多个组
        seen = {}
        for head, members in codec._groups.items():
            for w in members:
                assert seen.get(w, head) == head, f"词 {w} 属于多个组"
                seen[w] = head

    def test_all_groups_flippable(self):
        """必修课 2：保留组内都有两种颜色。"""
        codec = make_codec()
        for head, members in codec._groups.items():
            colors = {codec.green(w) for w in members}
            assert len(colors) == 2, f"组 {head} 是单色组，应被过滤"

    def test_stats_sane(self):
        codec = make_codec()
        s = codec.stats
        assert s["n_groups"] > 400          # 实验基线：485 组左右
        assert s["n_words"] > s["n_groups"]  # 平均组规模 > 1
        assert s["n_bands"] == 16

    def test_band_assignment_deterministic(self):
        codec = make_codec()
        head = next(iter(codec._groups))
        assert codec.band_of_group(head) == codec.band_of_group(head)


# ---------------------------------------------------------------------------
# 嵌入 / 检测往返
# ---------------------------------------------------------------------------

class TestRoundtrip:
    @pytest.mark.parametrize("uid", [0x0000, 0xFFFF, 0x1234, 0xABCD, 0x5678])
    def test_roundtrip_600_words(self, uid):
        codec = make_codec()
        text = make_text(600, seed=uid)
        marked = codec.embed(text, uid, bias=1.0, rng=random.Random(uid))
        report = codec.detect(marked)
        assert report.uid == uid
        assert report.existence_score > 20  # 600 词应远超 null（~40.6 median 是 800 词无水印）

    def test_roundtrip_preserves_nonwords(self):
        codec = make_codec()
        text = "The big, fast (and good) result -- verified twice; ok?"
        marked = codec.embed(text, 0x1234, rng=random.Random(0))
        # 标点与结构保留：词数一致，非词字符原样
        import re
        w_orig = re.findall(r"[A-Za-z']+", text)
        w_mark = re.findall(r"[A-Za-z']+", marked)
        assert len(w_orig) == len(w_mark)
        assert marked.count("(") == text.count("(")
        assert marked.count("--") == text.count("--")

    def test_capitalization_style_preserved(self):
        codec = make_codec()
        text = "Big results matter."
        marked = codec.embed(text, 0x00FF, rng=random.Random(1))
        first_word = marked.split()[0]
        assert first_word[0].isupper(), "句首大写风格应保留"


# ---------------------------------------------------------------------------
# 无水印文本不应命中嵌入 UID
# ---------------------------------------------------------------------------

class TestNullBehavior:
    def test_unmarked_text_uid_scattered(self):
        """无水印文本解出的 UID 不应系统性聚集在某个值。"""
        codec = make_codec()
        uids = [codec.detect(make_text(600, seed=1000 + i)).uid for i in range(8)]
        # 8 个独立文本解出 8 个不同 UID（碰撞概率极低）
        assert len(set(uids)) >= 7

    def test_calibrate_p0_centers_z(self):
        """必修课 3：标定后无水印文本的逐带 z 均值接近 0。"""
        codec = make_codec()
        corpus = [make_text(800, seed=2000 + i) for i in range(6)]
        codec.calibrate_p0(corpus)
        # 标定语料自身的 z 应接近 0
        zs = []
        for text in corpus:
            rep = codec.detect(text)
            zs.extend(st.z for st in rep.bands if st.has_signal)
        mean_z = sum(zs) / len(zs)
        assert abs(mean_z) < 0.15, f"标定后 mean z = {mean_z:.3f}，仍有系统偏移"

    def test_wrong_codec_key_fails(self):
        """错误密钥下检测不出 UID（信道 B 的密钥安全性）。"""
        c1, c2 = make_codec(1), make_codec(2)
        text = make_text(600, seed=99)
        uid = 0x1234
        marked = c1.embed(text, uid, rng=random.Random(0))
        rep = c2.detect(marked)
        assert rep.uid != uid


# ---------------------------------------------------------------------------
# 鲁棒性（轻量复现 §13.3 趋势）
# ---------------------------------------------------------------------------

class TestRobustness:
    def _paraphrase(self, text: str, frac: float, seed: int) -> str:
        """随机替换 frac 比例的词典词为组内其他候选（模拟改写）。"""
        rng = random.Random(seed)
        codec = make_codec()
        words = text.split()
        out = []
        for w in words:
            low = w.lower()
            grp = codec._w2group.get(low)
            if grp is not None and rng.random() < frac:
                alts = [x for x in grp if x != low]
                out.append(rng.choice(alts) if alts else w)
            else:
                out.append(w)
        return " ".join(out)

    def test_30pct_rewrite_decodes(self):
        codec = make_codec()
        uid = 0xBEEF
        marked = codec.embed(make_text(600, seed=5), uid, rng=random.Random(0))
        rewritten = self._paraphrase(marked, 0.30, seed=11)
        assert codec.uid_hamming(rewritten, uid) <= 1  # 允许 1 bit 摄动

    def test_bias_09_majority_bits(self):
        codec = make_codec()
        uid = 0x1234
        marked = codec.embed(make_text(600, seed=6), uid, bias=0.9, rng=random.Random(0))
        assert codec.uid_hamming(marked, uid) <= 3


# ---------------------------------------------------------------------------
# 内积形式 ⟨v, τ⟩
# ---------------------------------------------------------------------------

class TestDotScore:
    def test_dot_prefers_true_uid(self):
        codec = make_codec()
        uid, other = 0x0F0F, 0xF0F0
        marked = codec.embed(make_text(600, seed=8), uid, rng=random.Random(0))
        assert codec.dot_score(marked, uid) > codec.dot_score(marked, other)

    def test_dot_unmarked_near_zero(self):
        """标定 p0 后（必修课 3），无水印文本的 ⟨v,τ⟩ 接近 0。"""
        codec = make_codec()
        corpus = [make_text(800, seed=3000 + i) for i in range(6)]
        codec.calibrate_p0(corpus)
        scores = [codec.dot_score(make_text(600, seed=4000 + i), u)
                  for i, u in enumerate((0x1111, 0x2222, 0x3333))]
        # null 下 ⟨v,τ⟩ ~ N(0, ~√n_bands)；放 2.5σ 容限
        assert max(abs(s) for s in scores) < 12, f"scores={scores}"
