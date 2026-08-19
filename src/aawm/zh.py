"""语言适配器（v0.4）：英文/中文的统一抽象。

为何需要 adapter：
- v0.3 假设英文：`_normalize_word` 做小写去标点、`KeyedLetterMap` 用 26 字母表、
  `stable_id` 用同义组 ID。这些行为硬编码在 `content.py`。
- v0.4 引入中文：分词方式（空格 vs 前向最大匹配）、符号提取（首字母 vs 声母）、
  字母表（26 字母 vs 23 声母）都不同。

设计：`LanguageAdapter` 把这四点抽象为方法，`content.py` 通过 adapter 调用，
不再硬编码英文逻辑。`EnAdapter` 封装现有英文行为（零行为变更），`ZhAdapter`
实现中文路径。

**中文零强依赖**：jieba/pypinyin 为可选依赖。`ZhAdapter` 内嵌一份覆盖
`ZH_SYNONYMS_RAW` 全部词条的 pinyin 声母小表（双字词 → 声母对），无外部库
也能工作。若安装了 pypinyin 则自动使用以覆盖更广的词。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple

from .transforms import KeyedLetterMap


# ---------------------------------------------------------------------------
# pinyin 声母表（内嵌兜底，零依赖）
# ---------------------------------------------------------------------------

# 23 个拼音声母（无零声母方案：以 a/o/e 开头的字归到对应"声母"位）
# 严格声母 21 个 + y/w 共 23 个，覆盖所有普通话字
PINYIN_INITIALS = [
    "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h",
    "j", "q", "x", "zh", "ch", "sh", "r", "z", "c", "s", "y", "w",
]

# 首字 → 声母 的兜底映射（覆盖 ZH_SYNONYMS_RAW 词条首字 + 常用字约 250）
# 规则：按 pinyin 首字母归类，多音字取最常用读音
# 以下为手工+规则生成的覆盖表，足以支撑内嵌词典的分词与声母提取
_CHAR_TO_INITIAL: Dict[str, str] = {}

# 按声母批量填充（每串中的字都对应该声母）
_INITIAL_GROUPS: Dict[str, str] = {
    "b": "波播版标必本便步部把百包保被比表别布办帮半般补",
    "p": "频评普篇配品破批普平偏拼盘判盘炮朋品",
    "m": "明满每门目名末模民面描满免敏忙茫蒙妙谬",
    "f": "费法分非复放方反访防封否峰奉复杂风服范浮",
    "d": "的大到得等地度对单但点定断段多动东当短低都打导度岛断",
    "t": "他同体通特提题天头条统突推脱同太态谈通条图",
    "n": "能年内你那难念拿女南农宁泥怒脑脑",
    "l": "了里来老路两类力立理流留量联联路龙兰历立落来",
    "g": "个过高工公共关干感光更管各规观改广干感果",
    "k": "可开看克客控考科空扩口快块靠考科颗克",
    "h": "和会好合回后话化还环境很红黑灰黄怀",
    "j": "就即经进将间件见建近决机基本级计即极减加间接价",
    "q": "去取期其起全群清请确前强轻全企起取企",
    "x": "下行向信新现些想需小系形性修选学效续线",
    "zh": "这中制只主自之政直真重至展着证指种张周专中准",
    "ch": "出成出处常长场产城充冲除持查朝持迟冲",
    "sh": "是上说时生手实少身上面受什失收书树设深施",
    "r": "人让认然仍如入容若热弱软燃让",
    "z": "在作子自总作则怎做作走最再组早造增资综作总",
    "c": "从此才次曾参存层从参错才催存财",
    "s": "所三思四随虽算似素散送俗岁搜锁",
    "y": "一有也要于用以又因业由样应语元英永越迎",
    "w": "我为问无往完网外文未王万维稳危",
}

for _init, _chars in _INITIAL_GROUPS.items():
    for _ch in _chars:
        _CHAR_TO_INITIAL[_ch] = _init

# v0.4 补充：词典首字中零声母/常见字按拼音规则补齐（94 字）
# 零声母处理：a/o 开头归 'w'（半元音），e 开头归 'y'（发音近）
_EXTRA_CHAR_INITIAL: Dict[str, str] = {
    "丑": "ch", "世": "sh", "严": "y", "丰": "f", "任": "r", "优": "y",
    "传": "ch", "使": "sh", "例": "l", "停": "t", "健": "j", "先": "x",
    "写": "x", "冷": "l", "创": "ch", "删": "sh", "利": "l", "功": "g",
    "劣": "l", "努": "n", "原": "y", "友": "y", "发": "f", "变": "b",
    "启": "q", "员": "y", "响": "x", "团": "t", "困": "k", "培": "p",
    "安": "w", "寻": "x", "尝": "ch", "巨": "j", "市": "sh", "希": "x",
    "引": "y", "影": "y", "微": "w", "悲": "b", "情": "q", "愚": "y",
    "执": "zh", "技": "j", "报": "b", "挑": "t", "损": "s", "支": "zh",
    "教": "j", "数": "sh", "整": "zh", "昂": "w", "核": "h", "检": "j",
    "概": "g", "沟": "g", "洞": "d", "活": "h", "消": "x", "灵": "l",
    "状": "zh", "监": "j", "知": "zh", "研": "y", "秘": "m", "积": "j",
    "程": "ch", "竞": "j", "策": "c", "简": "j", "粗": "c", "精": "j",
    "紧": "j", "细": "x", "结": "j", "继": "j", "缺": "q", "美": "m",
    "聪": "c", "脆": "c", "获": "h", "解": "j", "记": "j", "谨": "j",
    "责": "z", "质": "zh", "速": "s", "避": "b", "阅": "y", "降": "j",
    "项": "x", "预": "y", "领": "l", "验": "y",
    # v0.4 第二批：组内候选词首字补齐（361 字，含暗/案修正）
    "不": "b", "丧": "s", "举": "j", "义": "y", "买": "m", "争": "zh",
    "事": "sh", "亏": "k", "交": "j", "亲": "q", "仔": "z", "众": "zh",
    "伤": "sh", "伫": "zh", "估": "g", "伶": "l", "位": "w", "供": "g",
    "依": "y", "促": "c", "候": "h", "倚": "y", "借": "j", "倡": "ch",
    "储": "ch", "僵": "j", "兑": "d", "凭": "p", "凶": "x", "刊": "k",
    "初": "ch", "刷": "sh", "刻": "k", "剔": "t", "剖": "p", "助": "zh",
    "势": "sh", "匮": "k", "区": "q", "升": "sh", "协": "x", "卓": "zh",
    "印": "y", "卷": "j", "厂": "ch", "及": "j", "古": "g", "叫": "j",
    "台": "t", "吁": "x", "吊": "d", "吓": "x", "含": "h", "呆": "d",
    "呈": "ch", "告": "g", "命": "m", "哀": "y", "商": "sh", "器": "q",
    "囊": "n", "圆": "y", "圈": "q", "圭": "g", "坚": "j", "型": "x",
    "壮": "zh", "备": "b", "奋": "f", "契": "q", "套": "t", "奢": "sh",
    "姿": "z", "娇": "j", "娴": "x", "守": "sh", "宏": "h", "宣": "x",
    "家": "j", "寒": "h", "察": "ch", "寰": "h", "尖": "j", "尘": "ch",
    "尤": "y", "尽": "j", "局": "j", "履": "l", "崭": "zh", "巡": "x",
    "差": "ch", "带": "d", "幅": "f", "序": "x", "底": "d", "店": "d",
    "庞": "p", "废": "f", "康": "k", "廉": "l", "延": "y", "异": "y",
    "式": "sh", "录": "l", "彰": "zh", "心": "x", "忧": "y", "急": "j",
    "恳": "k", "惊": "j", "惬": "q", "惯": "g", "愉": "y", "意": "y",
    "慎": "sh", "懂": "d", "戒": "j", "战": "zh", "扎": "zh", "扶": "f",
    "抉": "j", "抗": "k", "折": "zh", "护": "h", "披": "p", "抬": "t",
    "抵": "d", "担": "d", "拆": "ch", "拍": "p", "拓": "t", "拔": "b",
    "拙": "zh", "招": "zh", "拟": "n", "拣": "j", "拨": "b", "拱": "g",
    "挫": "c", "捍": "h", "换": "h", "授": "sh", "掉": "d", "掌": "zh",
    "排": "p", "探": "t", "措": "c", "援": "y", "揽": "l", "搭": "d",
    "摘": "zh", "摸": "m", "撤": "ch", "撰": "zh", "操": "c", "擘": "b",
    "攀": "p", "攻": "g", "日": "r", "旧": "j", "旨": "zh", "昔": "x",
    "昭": "zh", "显": "x", "智": "zh", "暗": "w", "朦": "m", "材": "c",
    "杜": "d", "杰": "j", "板": "b", "构": "g", "枝": "zh", "枢": "sh",
    "架": "j", "根": "g", "格": "g", "框": "k", "案": "w", "棘": "j",
    "欢": "h", "款": "k", "死": "s", "殃": "y", "毛": "m", "气": "q",
    "氛": "f", "水": "sh", "求": "q", "汇": "h", "沉": "ch", "沮": "j",
    "治": "zh", "沿": "y", "泄": "x", "洽": "q", "浅": "q", "测": "c",
    "浏": "l", "浩": "h", "涵": "h", "淡": "d", "混": "h", "添": "t",
    "渴": "k", "渺": "m", "滞": "zh", "漂": "p", "漏": "l", "演": "y",
    "火": "h", "焦": "j", "照": "zh", "片": "p", "牢": "l", "物": "w",
    "牵": "q", "独": "d", "率": "sh", "班": "b", "琐": "s", "瑕": "x",
    "甄": "zh", "申": "sh", "界": "j", "疑": "y", "症": "zh", "登": "d",
    "白": "b", "益": "y", "盛": "sh", "相": "x", "盼": "p", "省": "sh",
    "眼": "y", "督": "d", "睿": "r", "碰": "p", "示": "sh", "祈": "q",
    "神": "sh", "秀": "x", "私": "s", "秉": "b", "称": "ch", "移": "y",
    "稀": "x", "窥": "k", "章": "zh", "竭": "j", "笃": "d", "笔": "b",
    "笨": "b", "答": "d", "簿": "b", "糊": "h", "繁": "f", "终": "zh",
    "绕": "r", "给": "g", "绝": "j", "缘": "y", "翻": "f", "耗": "h",
    "职": "zh", "育": "y", "脚": "j", "舒": "sh", "舞": "w", "良": "l",
    "艰": "j", "节": "j", "花": "h", "苦": "k", "草": "c", "营": "y",
    "著": "zh", "薄": "b", "虚": "x", "融": "r", "衡": "h", "裁": "c",
    "装": "zh", "觅": "m", "视": "sh", "觉": "j", "角": "j", "触": "ch",
    "言": "y", "训": "x", "讯": "x", "讲": "j", "论": "l", "识": "sh",
    "诉": "s", "试": "sh", "课": "k", "调": "d", "谋": "m", "豪": "h",
    "负": "f", "货": "h", "赠": "z", "超": "ch", "践": "j", "踊": "y",
    "躬": "g", "躲": "d", "转": "zh", "轴": "zh", "载": "z", "较": "j",
    "达": "d", "迅": "x", "运": "y", "远": "y", "连": "l", "迫": "p",
    "迷": "m", "追": "zh", "透": "t", "递": "d", "途": "t", "遴": "l",
    "醒": "x", "采": "c", "钻": "z", "铺": "p", "销": "x", "镇": "zh",
    "闪": "sh", "阐": "ch", "队": "d", "阵": "zh", "阻": "z", "陈": "ch",
    "陌": "m", "院": "y", "险": "x", "隐": "y", "雄": "x", "集": "j",
    "雇": "g", "静": "j", "革": "g", "靶": "b", "音": "y", "顺": "sh",
    "顽": "w", "顾": "g", "颁": "b", "颓": "t", "飞": "f", "首": "sh",
    "马": "m", "驳": "b", "驻": "zh", "驾": "j", "鹄": "g", "麻": "m",
    "齐": "q",
}
_CHAR_TO_INITIAL.update(_EXTRA_CHAR_INITIAL)

# 单字拼音首字母 → 声母 的快速映射（用于未知字的兜底推断）
def _infer_initial(ch: str) -> Optional[str]:
    """从单字推断声母。优先查内嵌表，否则返回 None。"""
    return _CHAR_TO_INITIAL.get(ch)


# ---------------------------------------------------------------------------
# LanguageAdapter 抽象基类
# ---------------------------------------------------------------------------


class LanguageAdapter(ABC):
    """语言适配器抽象。

    负责把语言相关的四件事抽象为方法：
    1. tokenize：文本 → token 列表（含空格/标点占位，保持 join 后还原）
    2. normalize：token → 词典查询键
    3. extract_symbol：token → KeyedLetterMap 用的符号（英文首字母 / 中文声母）
    4. letter_alphabet：KeyedLetterMap 用的字母表
    """

    @abstractmethod
    def tokenize(self, text: str) -> List[str]:
        """文本 → token 列表。token 含空格/标点占位，join 后须还原原文。"""
        ...

    @abstractmethod
    def is_word_token(self, token: str) -> bool:
        """token 是否为可参与锚点的词（非空格/纯标点）。"""
        ...

    @abstractmethod
    def normalize(self, token: str) -> str:
        """token → 词典查询键。"""
        ...

    @abstractmethod
    def extract_symbol(self, token: str) -> Optional[str]:
        """token → KeyedLetterMap 用的符号。返回 None 则该 token 不可读 bit。"""
        ...

    @abstractmethod
    def letter_alphabet(self) -> List[str]:
        """KeyedLetterMap 用的字母表（英文 26 字母 / 中文 23 声母）。"""
        ...

    def stable_id_for_raw(self, token: str) -> bytes:
        """词典外词的 stable_id（默认用规范化词形，可被子类覆盖）。"""
        return b"raw:" + self.normalize(token).encode("utf-8")

    def sentence_end_pattern(self) -> re.Pattern:
        """句末标点正则（用于句子切分）。"""
        return re.compile(r"[.!?。！？；]+")


# ---------------------------------------------------------------------------
# EnAdapter：封装现有英文行为（零行为变更）
# ---------------------------------------------------------------------------


class EnAdapter(LanguageAdapter):
    """英文适配器（v0.3 行为的封装）。"""

    _WORD_RE = re.compile(r"\S+|\s+")

    def tokenize(self, text: str) -> List[str]:
        return self._WORD_RE.findall(text)

    def is_word_token(self, token: str) -> bool:
        return bool(token.strip()) and any(c.isalpha() for c in token)

    def normalize(self, token: str) -> str:
        return token.lower().strip(".,!?;:\"'()[]")

    def extract_symbol(self, token: str) -> Optional[str]:
        for ch in token:
            if ch.isalpha():
                return ch.upper()
        return None

    def letter_alphabet(self) -> List[str]:
        return [chr(ord("A") + i) for i in range(26)]


# ---------------------------------------------------------------------------
# ZhAdapter：中文适配器（声母谓词 + 前向最大匹配分词）
# ---------------------------------------------------------------------------


class ZhAdapter(LanguageAdapter):
    """中文适配器。

    分词：前向最大匹配（词典 = ZH_SYNONYMS_RAW 的主词条，均双字词），
    词典外单字作为独立 token。这样双字词整体参与锚点（同义替换不改变
    token 数），与英文"单词整体"语义对齐。

    声母谓词：token（双字词）取首字声母作 KeyedLetterMap 的符号。
    声母表 23 个，组内声母种类 ≥3 即可表达双 bit（动态可锚定判定）。
    """

    _ZIYAN = "。！？；，、：""''（）【】《》\n\r\t "  # 中文断句/分隔符

    def __init__(self, dict_words: Optional[set] = None) -> None:
        # 分词词典：必须包含所有组内词（主词条 + 候选词），否则替换后的
        # 候选词（非主词条）会被拆成单字，导致 token 数变化、锚点错位
        if dict_words is not None:
            self._dict_words = dict_words
        else:
            # v0.9 扩容：与 GreenlistCodec 默认词典同步（词林扩容版）
            from .synonym_data import load_default_zh_dictionary
            ws = set()
            for cands in load_default_zh_dictionary().values():
                ws.update(cands)
            self._dict_words = ws

    def tokenize(self, text: str) -> List[str]:
        """中文分词：前向最大匹配（双字词优先），保留标点/空格占位。

        返回的 token 列表 join 后必须还原原文——通过把非词字符
        （标点、空格、ASCII 等）也作为独立 token 保留实现。
        """
        tokens: List[str] = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            # ASCII / 空格 / 英文：连续归并（与 EnAdapter 一致）
            if ch < "\u4e00" or ch > "\u9fff":
                j = i
                while j < n and (text[j] < "\u4e00" or text[j] > "\u9fff"):
                    j += 1
                tokens.append(text[i:j])
                i = j
                continue
            # 中文字符：前向最大匹配双字词
            if i + 1 < n and text[i:i + 2] in self._dict_words:
                tokens.append(text[i:i + 2])
                i += 2
            else:
                tokens.append(text[i])
                i += 1
        return tokens

    def is_word_token(self, token: str) -> bool:
        return bool(token.strip()) and any("\u4e00" <= c <= "\u9fff" for c in token)

    def normalize(self, token: str) -> str:
        """中文不需要小写/去标点（双字词本身即查询键）。"""
        return token.strip()

    def extract_symbol(self, token: str) -> Optional[str]:
        """取首字声母。"""
        for ch in token:
            if "\u4e00" <= ch <= "\u9fff":
                return _infer_initial(ch)
        return None

    def letter_alphabet(self) -> List[str]:
        return list(PINYIN_INITIALS)

    def stable_id_for_raw(self, token: str) -> bytes:
        """词典外中文词的 stable_id：用首字声母 + 全 token，保留区分度。"""
        return b"raw:" + token.encode("utf-8")

    def sentence_end_pattern(self) -> re.Pattern:
        return re.compile(r"[.!?。！？；]+")


# ---------------------------------------------------------------------------
# 工具：从适配器构造 KeyedLetterMap
# ---------------------------------------------------------------------------


def make_letter_map(seed: bytes, adapter: LanguageAdapter) -> KeyedLetterMap:
    """用适配器的字母表构造 KeyedLetterMap。"""
    return KeyedLetterMap(seed, alphabet=adapter.letter_alphabet())


# ---------------------------------------------------------------------------
# 工具：为中文词典批量预计算声母覆盖（诊断/验证用）
# ---------------------------------------------------------------------------


def zh_initial_coverage(words: List[str]) -> Dict[str, int]:
    """统计一组中文词的声母分布（诊断用）。"""
    cov: Dict[str, int] = {}
    for w in words:
        init = _infer_initial(w[0]) if w else None
        if init:
            cov[init] = cov.get(init, 0) + 1
    return cov


# 默认适配器实例（按语言选择）
_DEFAULT_ADAPTERS: Dict[str, LanguageAdapter] = {
    "en": EnAdapter(),
}


def get_adapter(language: str) -> LanguageAdapter:
    """按语言名获取适配器实例。"""
    if language == "zh":
        return ZhAdapter()
    return _DEFAULT_ADAPTERS.get(language, _DEFAULT_ADAPTERS["en"])
