# AAWM API 参考（v0.7 插件层）

> 算法层 API（GreenlistCodec / DocumentBinder / CAEmbedder 等）见 `docs/design.md`。
> 本文只覆盖插件层（`aawm.plugins`）。

---

## Watermarker

统一 Facade：一键嵌入 + 一键溯源。组合信道 B（绿名单溯源）+ 信道 A（Merkle 防篡改）+ UID 注册库。

```python
from aawm.plugins import Watermarker

wm = Watermarker(registry=registry)                  # 纯内存（每次随机密钥）
wm = Watermarker(keystore=ks, registry=reg)          # 显式密钥+注册库
wm = Watermarker.from_config("key.json", "reg.json") # 文件配置（推荐）
```

### 构造

```python
Watermarker(
    master_key: bytes | str | None = None,   # hex 字符串或 bytes；None 用 keystore 或随机
    *,
    keystore: KeyStore | None = None,         # 密钥存储（优先于 master_key）
    registry: UIDRegistry | None = None,      # UID 注册库（None=无注册库，str 别名哈希为 UID）
    language: str = "auto",                   # "en" / "zh" / "auto"（按 CJK 检测）
    thresholds: DetectionThresholds | None = None,
    codec_mode: str = "zero_cost",        # v0.7+ codec：default/zero_cost/hybrid（中英双语）
    supplementary_dict: dict | None = None,   # v0.7 hybrid 模式的补充词表
    calibrate_corpus: list[str] | None = None,  # v0.7 null 语料（零感模式阈值标定）
    calibration: dict | str | None = None,   # v0.12 标定文件（aawm calibrate 产出的
                                              # JSON dict 或文件路径）——一次标定处处复用
) -> None
```

**codec 模式（v0.7 起；v0.9 起中英文各自有零感词典，均生效）**：

| 模式 | 词典 | 适用 |
|---|---|---|
| `default` | 全词林（中文 4538 组 / 英文同义组） | 向后兼容，容量大但替换扰动明显 |
| `zero_cost` | 零感词典（中文 136 组双字词 / 英文 133 组拼写变体+功能副词+安全对） | **推荐**，嵌入对文本观感几乎无扰动 |
| `hybrid` | 零感打底 + `supplementary_dict` 补带 | 需要比 zero_cost 更大容量时 |

英文零感词典对通用文本命中稀疏（通用英文容量明显小于 default 词林），
适合 AI 长输出等零感词密度高的场景；极短英文受容量限制，建议看统计行。

`calibrate_corpus` 提供**未加水印**的正常输出样本后，`_fit_null_model`
用每带归一化 ratio 模型（Σ|z|/m）在 5 个 salt 上采样、3σ 上界作为
null 阈值——能显著降低零感模式的误报（实测 null 误报 13/30 → 0/30）。

`calibration`（v0.12）装载 `aawm calibrate` 产出的标定文件（null 模型 +
p0 词频表）：词频表盐无关，运行时按当前密钥/盐重算精确 p0，与现场
`calibrate_corpus` 标定**数学等价**。两者同时给出时 corpus 优先。

### `embed()`

```python
result = wm.embed(
    text: str,
    user_id: int | str,                # int=UID 直传；str=别名（经注册库映射，无注册库则哈希）
    *,
    session_salt: bytes | None = None,  # None→自动生成
    sign: bool = True,                  # 是否签信道 A（Merkle 防篡改）
    language: str | None = None,        # 覆盖实例默认
    bias: float = 1.0,                  # 嵌入强度 [0,1]
    rng_seed: int | None = None,        # 指定后确定性嵌入
    n_bits: int | None = None,          # v0.7 自适应模式编码位数（None=满容量；<容量留冗余带抗替换）
) -> EmbedResult
```

**自检重试（v0.7）**：中文自适应模式嵌入后会用 `detect_adaptive` 回验解码
UID，并要求存在性得分 ≥1.5×阈值；不达标自动换盐重试（最多 4 次），
返回信号最强的结果。根因防御：同义候选与邻字成词（如"不但"+"是"→"但是"）
时该词颜色无法翻转会导致单带误码。

### `trace()`

```python
result = wm.trace(
    text: str,
    *,
    session_salt: bytes | None = None,   # 嵌入时的盐（强烈建议传）
    seal: BindingSeal | None = None,     # 嵌入时的签名（篡改检测用）
    language: str | None = None,
    soft_match: bool = True,             # v0.10 起默认启用软判决注册库匹配（见下）
    match_margin: float = 2.0,           # 软判决置信阈值（最优-次优得分差下限）
    match_margin_ratio: float | None = 0.3,  # v0.10 起默认 0.3 自适应置信系数（见下）
    bands: list[int] | None = None,      # v0.7 自适应路径的带列表（embed 返回，需回传）
    n_bits: int | None = None,           # v0.7 自适应路径的编码位数
    archived_uid: int | None = None,     # v0.11 嵌入时存档的 UID（盐外证据，见下）
) -> TraceResult
```

**自适应路径（v0.7）**：传 `bands` 时走 `detect_adaptive`（零感/混合词典的
逐带 z 检测），存在性阈值用 null 线性模型或 `adaptive_intercept/slope`；
不传则走旧 `detect`（default 词典），两者检测口径不同，trace 时务必
回传 embed 返回的 `bands/n_bits`。

**盐外证据（v0.11，防路径依赖）**：`archived_uid` 是嵌入时存档的 UID
真值（meta/服务端存档持有）。攻击下"检出"本质盐无关——解码 UID 常失真，
错误盐也可能检出一个（失真）UID；仅靠解码 UID 归因会在裸 API/多盐扫描
场景下把结果归给错误用户。传入 `archived_uid` 后，trace 在归因前做
盐外交叉校验：解码 UID 与存档 UID 不满足 `_uid_alias_match`（见下）即
置 `attribution_abstain=True`、`uid/user=None`，绝不输出可能错误的归因。
CLI `find-meta` / server `/v1/trace` 持 meta 时自动传入；直接调 `trace()`
的 API 消费者应显式回传嵌入时存档的 UID。

`_uid_alias_match(uid, archived_uid, n_bits)` 掩码对齐语义：解码 UID 与
存档 UID 精确相等，或按 `n_bits` 掩码 `uid == (archived_uid & ((1<<n_bits)-1))`
视为一致（`n_bits=0` 非自适应时只认精确相等）；`archived_uid` 为数字字符串
时自动转 int。

**软判决注册库匹配（v0.7 鲁棒性增强；v0.10 起默认启用）**：`soft_match=True`
时，用 `GreenlistCodec.soft_match` 对注册库全部 UID 逐带 z 打点积分
（min_n=1，弱证据带参与），替代"解码 UID + 汉明最近邻"路径。
只在水印存在性判定通过后采纳软匹配结果（soft_match 是候选区分器，
null 文本也可能与某候选方向对齐）。实测：30% 同组改写攻击下
匹配率 20→27/30，PAWS 温和改写 22→25/30；`match_margin=2.0`
可把错误匹配全部转为 abstain。

**软判决拒绝（v0.10，默认行为变更）**：当 margin 门限拒绝（最优-次优
得分差不足，soft_uid=None）时，trace **不再回退硬解码 UID**——攻击下
存在性常存活但解码不可靠，硬解码恰恰是"高置信度错误归因"（存在性
存活但 UID 解错、仍输出错误用户）的来源。软判决拒绝时归因置信度
`attribution_confidence` 直接判 0，触发 abstain（uid/user=None）。

**自适应置信阈值（v0.8；v0.10 起默认 0.3）**：`match_margin_ratio`
提供按证据量放大的置信余量，生效阈值 `max(match_margin, ratio·√n_dict)`
——短文本由绝对项主导、长文本由比例项主导。解决固定绝对 margin
对长文本偏松（50% 改写下错误 gap 仍超 2.0，"自信地错"）。实测错误
匹配的 gap/√n_dict 上界跨语料稳定 ≈0.22，正确匹配均值 0.5~0.7；
ratio 是"宁可 abstain 也不错"的权衡旋钮，按部署语料调（详见 design
§13.11 / capability §二·五·D）。None 时纯绝对阈值（v0.7 兼容）。

**归因置信度（v0.10）**：`attribution_confidence = 判别力 × 容量充分性`
，独立于存在性 `confidence`（后者只反映"信号多强"，不反映"UID 解对
没有"）：
- 判别力：软判决路径用 `gap/√n_dict` 线性映射（≤0.22→0，≥0.4→1，
  锚点来自跨语料实测）；margin 门限拒绝时直接为 0。硬判决路径（显式
  `soft_match=False`）按汉明距映射（0→1，≥max_hamming→0）。无注册库
  对比时给上限 0.5。
- 容量充分性：自适应 k-bit 空间内注册库 UID 掩码后若有碰撞（如
  n_bits=6 下 UID 1 与 65 均 mask 成 1），二者在数学上不可区分，
  cap=0 一票否决。
- `attribution_confidence < attribution_floor`（默认 0.5）时
  `attribution_abstain=True` 且 uid/user/hamming_dist 置空——输出
  "不可判定"，而非一个可能错误的具体用户。

### 其他方法

| 方法 | 说明 |
|---|---|
| `detect_only(text, *, session_salt=None, language=None) -> bool` | 只判存在性 |
| `calibrate_p0(corpus: list[str], language="en") -> None` | 无水印语料标定逐带基线 |
| `calibrate_null_model(corpus: list[str]) -> None` | v0.12 拟合 null 阈值模型 + 构建 p0 词频表 |
| `export_calibration() -> dict` | v0.12 导出标定（可 json.dump 成标定文件复用） |
| `estimate_capacity(text, *, language=None) -> int` | v0.12 嵌入前容量预检（不改文本，返回 k-bit 容量） |
| `reliability_tier(capacity: int, weak_embed: bool) -> str`（静态） | v0.12 容量分级：≥10=high / 6-9=medium / <6 或 weak=low |
| `registry -> UIDRegistry \| None` | 注册库访问 |
| `keystore -> KeyStore` | 密钥存储访问 |

---

## EmbedResult

```python
@dataclass
class EmbedResult:
    watermarked_text: str          # 水印后文本（发布这个）
    session_salt: bytes            # 会话盐（溯源需要，可公开，必须存档）
    user_id: int                   # 实际嵌入的 UID
    user_alias: str | None         # 用户别名（str 嵌入时有）
    seal: BindingSeal | None       # 信道 A 签名（sign=True 时）
    language: str                  # "en" / "zh"
    n_dict_words: int              # 词典命中词数（容量指标）
    existence_score: float         # 嵌入后自检的存在性得分
    # v0.7 自适应路径
    codec_mode: str = "default"    # default/zero_cost/hybrid
    bands: list[int] = []          # 活动带列表（trace 时必须回传）
    capacity: int = 0              # 活动带数（k-bit 容量）
    n_bits: int = 0                # 实际编码位数（含冗余时 < capacity）
    # v0.10 弱嵌入警示（自检存在性余量 = existence_score/阈值）
    margin_ratio: float = 0.0      # 自检余量（自适应模式）；>=1.5 视为信号充足
    weak_embed: bool = False       # True=余量 <1.5（短文本/词典稀疏），
                                   #   trace 可能漏检或归因 abstain，应加大文本再嵌
    # v0.12 可靠性分级（default 模式恒 high；adaptive 按容量分级，不拒嵌）
    reliability: str = "high"      # high(≥10bit) / medium(6-9bit, 归因可能失败)
                                   #   / low(<6bit 或 weak_embed, 结论仅供参考)
```

## TraceResult

```python
@dataclass
class TraceResult:
    watermarked: bool              # 存在性判定
    uid: int | None                # 解码 UID（watermarked=False 时 None）
    user: str | None               # 注册库匹配的别名
    hamming_dist: int              # 与最近邻的汉明距（-1=无注册库/无匹配）
    confidence: float              # [0,1]，existence_score/confidence_scale
    tampered: bool | None          # None=无 seal；True=被篡改；False=完整
    tampered_paragraphs: list[int] # 被改段落索引
    band_report: BandReport        # 逐带明细（算法层透传）
    existence_score: float         # Σ|z|
    n_dict_words: int              # 词典命中数
    soft_uid: int | None           # 软判决匹配 UID（soft_match=True 时）
    soft_gap: float                # 软判决最优-次优得分差（未启用=-1.0）
    # v0.10 归因置信度
    attribution_confidence: float  # [0,1] 归因可靠性（=判别力×容量充分性）
    attribution_abstain: bool      # True=检出但归因置信不足，uid/user 已置 None
    # v0.7 自适应路径
    codec_mode: str = "default"    # default/zero_cost/hybrid
    bands: list[int] = []          # 检测用的带列表（embed 回传）
    capacity: int = 0              # 嵌入时的容量
    n_bits: int = 0                # 嵌入时的编码位数
    active_bands: int = 0          # 攻击后仍存活的活动带数
```

## DetectionThresholds

```python
@dataclass
class DetectionThresholds:
    adaptive_factor: float = 2.0   # 存在性阈值 = max(floor, factor × √n_dict_words)
    existence_floor: float = 8.0   # 阈值下限
    confidence_scale: float = 40.0 # 置信度归一化分母
    max_hamming: int = 3           # 注册库匹配容错
    # v0.7 自适应路径阈值（无 null 标定时的默认线性常数）
    adaptive_intercept: float = 1.0
    adaptive_slope: float = 1.6    # 阈值 ≈ intercept + slope × 活动带数
    # v0.10 归因置信度判定参数（对抗场景"高置信度错误归因"防御）
    attribution_floor: float = 0.5   # AC < 此值 → abstain（uid=None）
    gap_error_hi: float = 0.22       # gap/√n_dict ≤ 此值视为错误区间（实测上界）
    gap_ok_lo: float = 0.4           # gap/√n_dict ≥ 此值视为可靠区间
    capacity_full_width: int = 16    # 自适应 k-bit 空间参考宽度
    hard_no_cands_cap: float = 0.5   # 无候选对比时判别力上限
```

---

## KeyStore

```python
KeyStore(master_key: bytes | None = None)                     # None→随机 32B
KeyStore.from_file(path, *, create=False) -> KeyStore         # JSON 文件
KeyStore.from_env(var="AAWM_MASTER_KEY") -> KeyStore          # hex 环境变量
KeyStore.from_any(master_key=None, *, key_file=None, env_var=None) -> KeyStore

ks.get() -> bytes                    # 取密钥
ks.save(path) -> None                # 持久化（自动 chmod 600）
ks.export_env(var="AAWM_MASTER_KEY") -> str  # "export AAWM_MASTER_KEY=<hex>"

generate_key(length=32) -> bytes     # 模块级便捷函数
```

---

## UIDRegistry

```python
UIDRegistry(backend="memory", path=None, *, uid_bits=16)
# backend: "memory"（默认，进程内）| "file"（JSON 持久化）

reg.register(alias: str, uid: int | None = None) -> int    # 注册，返回 UID（幂等）
reg.resolve_alias(alias: str) -> int                       # 别名→UID（不存在自动注册）
reg.lookup(uid: int) -> str | None                        # UID→别名
reg.nearest_match(uid, max_hamming=3) -> tuple | None      # (uid, alias, dist)
reg.masked_nearest_match(uid, active_mask=0xFFFF, max_hamming=3) -> tuple | None
reg.list_all() -> dict[int, str]                          # 全量快照
len(reg) / alias in reg / uid in reg                      # 协议支持
```

**自定义后端**：继承并覆写 `_persist()` / `_load()` 即可接 SQLite/Postgres。

---

## Context 与 ContextProvider

```python
@dataclass(frozen=True)
class Context:
    user_id: int | str | None   # None=无效上下文（跳过嵌入）
    session_id: str | None
    language: str | None        # "en"/"zh"
    metadata: dict

    ctx.is_valid() -> bool      # user_id 非 None
    ctx.language_tag() -> bytes # b"en" / b"zh"
```

### 三级解析链

```python
chain = ContextChain.default()
# 等价于 ContextChain([
#     FrameworkContextProvider(),  # 框架 context（LangChain request / LiteLLM metadata / dict）
#     EnvVarContextProvider(),     # contextvars → 环境变量 AAWM_USER_ID
#     HeaderContextProvider(),     # X-AAWM-User-Id 请求头
# ])

ctx = chain.resolve(request=req)                  # 按优先级，首个有效胜出
ctx = chain.resolve(user_api_key_dict=uak)        # LiteLLM 场景
ctx = chain.resolve(headers=http_headers)         # 代理场景
ctx = chain.resolve(context={"user_id": 42})      # 显式 dict
chain2 = chain.prepend(my_provider)               # 高优先级插队
```

### 自研框架的 contextvars 注入

```python
tokens = set_user_context(user_id="u1", session_id="s1", language="zh")
try:
    ...
finally:
    reset_user_context(tokens)
```

---

## WatermarkMiddleware

框架无关的中间件核心（所有适配器调用它）。

```python
mw = WatermarkMiddleware(
    watermarker: Watermarker,
    context_chain: ContextChain | None = None,   # None→默认链
    *,
    min_text_length: int = 50,                   # 短文跳过
    skip_if_no_context: bool = True,             # 无 user_id 跳过
)

marked, result = mw.transform(text, ctx=None, **ctx_kwargs) -> tuple[str, EmbedResult | None]
# fail-open：异常时返回 (原文, None)

mw.should_embed(response) -> bool        # tool_calls 检查 + 文本非空检查
mw.extract_text(response) -> str         # OpenAI / LangChain / str 多格式
mw.write_back(response, new_text) -> Any # 原地改写 response
```

---

## StreamingWatermarker

```python
streamer = StreamingWatermarker(mw, *, flush_timeout_ms=2000)

out = streamer.feed(delta: str, ctx: Context | None = None) -> str
# 缓冲到句末标点（.!?。！？；\n），整句嵌入后返回
# 返回 "" 表示还在缓冲

tail = streamer.flush() -> str   # 流结束，嵌入并返回剩余

streamer.buffered_length -> int  # 当前缓冲长度
streamer.total_buffered -> int   # 累计接收
streamer.total_emitted -> int    # 累计输出
```

**行为细节**：
- 单句 < 20 字符时原样透传（不嵌入）
- 每句独立调用 `middleware.transform`——句子感知锚点设计保证跨句不污染

---

## 框架适配器

### LangChain v1（`aawm.plugins.adapters.langchain_v1`）

```python
from aawm.plugins.adapters.langchain_v1 import AAWMMiddleware

mw = AAWMMiddleware(
    watermarker,
    context_chain=None,          # None→默认链（读 request.runtime.context）
    *,
    min_text_length=50,
    skip_if_no_context=True,
)
# 挂到 create_agent(..., middleware=[mw])
# 钩子：after_model（非流式）+ after_model_stream（流式）
```

未安装 langchain 时实例化抛 `ImportError`。

### LiteLLM Proxy（`aawm.plugins.adapters.litellm_proxy`）

```python
from aawm.plugins.adapters.litellm_proxy import setup_hooks

setup_hooks(watermarker, context_chain=None, *, min_text_length=50)
# 之后模块内的两个 hook 函数即生效：
#   async_post_call_success_hook(data, user_api_key_dict, response)
#   async_post_call_streaming_iterator_hook(data, user_api_key_dict, response)
# 用户标识从 user_api_key_dict.metadata["user_id"] 读取
```

未 `setup_hooks` 时 hooks 透传（不嵌入不报错）。

### OpenAI SDK（`aawm.plugins.adapters.openai_v1`）

```python
from aawm.plugins.adapters.openai_v1 import (
    wrap_openai_client,
    wrap_async_openai_client,
)

client = wrap_openai_client(openai.OpenAI(), watermarker,
                            context_chain=None, *,
                            min_text_length=50,
                            skip_if_no_context=True)
# 包装后 client.chat.completions.create(...) 的输出自动嵌水印
# stream=True 时自动做句子级缓冲重写；user_id 可从 create 参数/请求头/contextvars 解析

async_client = wrap_async_openai_client(openai.AsyncOpenAI(), watermarker)
# 用法同同步版，await create(...) 输出自动嵌水印
```

纯 duck-typing 包装（import 无需安装 openai），fail-open 语义与其余适配器一致。

---

## CLI（`aawm`）

```bash
aawm keygen [--output FILE | --env VAR]           # 默认输出 hex 到 stdout
aawm registry add ALIAS [--uid N] [--registry F]
aawm registry list [--registry F]
aawm registry find UID [--registry F]             # UID 支持 0x 前缀
aawm embed INPUT --key F --user ID [--registry F] [--language L] [--no-sign] [-o OUT]
aawm trace INPUT --key F [--registry F] [--salt HEX | --meta META.json]
aawm serve --key F [--registry F] [--port 8765] [--log-level info]
# INPUT 为 "-" 时读 stdin；embed -o 时同时生成 OUT.meta.json（salt+seal）
```

**v0.7 中文 codec 选项**（`embed` / `trace` / `serve` 通用）：

```bash
aawm embed input.txt --key key.json --user 42 \
      --codec-mode zero_cost --calibration calibration.json -o marked.txt
#   --codec-mode {default,zero_cost,hybrid}    # 默认 zero_cost（中英双语）
#   --calibration FILE                         # v0.12 标定文件（aawm calibrate 产出，推荐）
#   --calibrate-corpus DIR|FILE                # null 语料（目录下所有 .txt 或单文件）
#   --n-bits N                                 # embed 用：编码位数（None=满容量）
```

**v0.12 标定命令**：

```bash
aawm calibrate ./corpus/ -o calibration.json   # 同领域语料（几十篇正常输出）
aawm calibrate --demo -o calibration.json      # 包内置示例语料（快速体验）
# 产出 null 阈值模型 + p0 词频表（盐无关，运行时按当前密钥重算）；
# --key 可选：null 模型与密钥无关，词频表按用户密钥重算
```

- `embed` 的 meta.json 额外写入 `codec_mode / bands / capacity / n_bits / reliability`
- `embed` stderr 输出 `[可靠性] high|medium|low`（容量分级；短文本降级不拒嵌）
- `trace` 读 meta.json 自动回传 `bands/n_bits`；stderr 输出
  `自适应: 容量=… 存活带=…`
- `serve` 接受 `--codec-mode / --calibration / --calibrate-corpus` 配置服务端 watermarker

trace 退出码：0=检出水印，2=未检出。

---

## HTTP API（`aawm serve`）

### POST /v1/trace

```json
// 请求
{"text": "...", "session_salt": "<hex>", "seal": {...}, "language": "en",
 "bands": [2,5,9], "n_bits": 6, "archived_uid": 17}   // v0.7：自适应路径需回传 bands/n_bits
// v0.11：archived_uid 可选，嵌入时存档的 UID（盐外证据）；传入后解码 UID
// 与存档 UID 交叉校验，不一致即 abstain（绝不归因到错误用户）
// session_salt/seal 可选；seal 结构同 embed 响应

// 响应
{"watermarked": true, "uid": 4660, "user": "user-alice", "hamming_dist": 1,
 "confidence": 0.45, "tampered": false, "tampered_paragraphs": [],
 "existence_score": 18.1, "n_dict_words": 46,
 "codec_mode": "zero_cost", "capacity": 6, "n_bits": 6, "active_bands": 5,
 "attribution_confidence": 0.8, "attribution_abstain": false}
// v0.10：attribution_abstain=true 时 uid/user/hamming_dist 为 null/-1
// （归因置信不足，输出"不可判定"而非可能错误的 UID/用户）
```

### POST /v1/embed

```json
// 请求
{"text": "...", "user_id": "user-alice", "session_salt": null, "sign": true,
 "n_bits": null}   // v0.7：自适应模式编码位数（可选）

// 响应
{"watermarked_text": "...", "session_salt": "<hex>", "user_id": 4660,
 "user_alias": "user-alice", "has_seal": true, "existence_score": 18.1,
 "codec_mode": "zero_cost", "bands": [2,5,9], "capacity": 6, "n_bits": 6,
 "margin_ratio": 2.1, "weak_embed": false, "reliability": "high"}
// v0.10：weak_embed=true（自检余量 <1.5）为弱嵌入警告——文本信号不足，
// trace 可能漏检；应加大文本或换词典密度更高的语料再嵌
// v0.12：reliability=high|medium|low 容量分级（短文本自动降级，仍正常嵌入）
```

### POST /v1/find-meta

meta 散失时，在候选存档中反查来源（与 CLI `aawm find-meta` 同规则）：

```json
// 请求
{"text": "...", "candidates": [
   {"session_salt": "<hex>", "bands": [2,5,9], "n_bits": 6,
    "seal": {...}, "label": "doc_00.meta.json", "archived_uid": 17}
 ], "language": "zh", "max_trace": 10}
// archived_uid 可选：嵌入时的存档 UID（盐外证据，解码交叉校验用）

// 响应
{"watermarked": true, "matched_index": 0, "matched_label": "doc_00.meta.json",
 "uid": 17, "user": "u1", "hamming_dist": 0, "confidence": 0.83,
 "existence_score": 33.4, "tampered": true, "tampered_paragraphs": [0],
 "para_overlap": 5, "para_total": 8,
 "attribution_confidence": 0.8, "attribution_abstain": false}
// v0.10 裁决：段哈希内容证据优先 + 解码 UID 与 archived_uid 交叉校验；
// 解码失真/多候选冲突时 attribution_abstain=true、uid/user=null（不可判定），
// 绝不输出可能错误的 UID/用户（攻击下"检出"盐无关，多条盐都会检出）
```

### GET /v1/health

```json
{"status": "ok", "watermarker_initialized": true}
```

> 服务端 watermarker 模式由 `aawm serve --codec-mode … --calibration …` 配置；<br>
> 也可在代码里 `set_watermarker(Watermarker(codec_mode="zero_cost", …))` 后 `create_app()`。

---

## 便捷导入

`aawm` 顶层包懒加载插件符号：

```python
from aawm import Watermarker, UIDRegistry, KeyStore, ...  # 等价 aawm.plugins.*
```
