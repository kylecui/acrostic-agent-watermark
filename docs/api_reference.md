# AAWM API 参考（v0.6 插件层）

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
) -> None
```

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
) -> EmbedResult
```

### `trace()`

```python
result = wm.trace(
    text: str,
    *,
    session_salt: bytes | None = None,   # 嵌入时的盐（强烈建议传）
    seal: BindingSeal | None = None,     # 嵌入时的签名（篡改检测用）
    language: str | None = None,
) -> TraceResult
```

### 其他方法

| 方法 | 说明 |
|---|---|
| `detect_only(text, *, session_salt=None, language=None) -> bool` | 只判存在性 |
| `calibrate_p0(corpus: list[str], language="en") -> None` | 无水印语料标定逐带基线 |
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
```

## TraceResult

```python
@dataclass
class TraceResult:
    watermarked: bool              # 存在性判定
    uid: int | None                # 解码 UID（watermarked=False 时 None）
    user: str | None               # 注册库最近邻匹配的别名
    hamming_dist: int              # 与最近邻的汉明距（-1=无注册库/无匹配）
    confidence: float              # [0,1]，existence_score/confidence_scale
    tampered: bool | None          # None=无 seal；True=被篡改；False=完整
    tampered_paragraphs: list[int] # 被改段落索引
    band_report: BandReport        # 逐带明细（算法层透传）
    existence_score: float         # Σ|z|
    n_dict_words: int              # 词典命中数
```

## DetectionThresholds

```python
@dataclass
class DetectionThresholds:
    adaptive_factor: float = 2.0   # 存在性阈值 = max(floor, factor × √n_dict_words)
    existence_floor: float = 8.0   # 阈值下限
    confidence_scale: float = 40.0 # 置信度归一化分母
    max_hamming: int = 3           # 注册库匹配容错
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

trace 退出码：0=检出水印，2=未检出。

---

## HTTP API（`aawm serve`）

### POST /v1/trace

```json
// 请求
{"text": "...", "session_salt": "<hex>", "seal": {...}, "language": "en"}
// session_salt/seal 可选；seal 结构同 embed 响应

// 响应
{"watermarked": true, "uid": 4660, "user": "user-alice", "hamming_dist": 1,
 "confidence": 0.45, "tampered": false, "tampered_paragraphs": [],
 "existence_score": 18.1, "n_dict_words": 46}
```

### POST /v1/embed

```json
// 请求
{"text": "...", "user_id": "user-alice", "session_salt": null, "sign": true}

// 响应
{"watermarked_text": "...", "session_salt": "<hex>", "user_id": 4660,
 "user_alias": "user-alice", "has_seal": true, "existence_score": 18.1}
```

### GET /v1/health

```json
{"status": "ok", "watermarker_initialized": true}
```

---

## 便捷导入

`aawm` 顶层包懒加载插件符号：

```python
from aawm import Watermarker, UIDRegistry, KeyStore, ...  # 等价 aawm.plugins.*
```
