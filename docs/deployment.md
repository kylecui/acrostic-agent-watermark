# AAWM 部署文档

> 面向运维/平台团队：密钥管理、注册库运维、检测服务部署、性能调优。

---

## 1. 架构总览

```
┌────────────────────────────────────────────────────────┐
│ Agent 侧（嵌入方）                                       │
│                                                         │
│  LangChain Agent ──┐                                    │
│  LiteLLM Proxy ────┼──→ AAWM 中间件 ──→ 水印文本发布     │
│  自研 Agent ───────┘         │                          │
│                              │ 存档 session_salt/seal   │
└──────────────────────────────┼──────────────────────────┘
                               ▼
                    ┌─────────────────────┐
                    │ 发布记录存储          │
                    │ (text_hash, salt,   │
                    │  seal, user_id, ts) │
                    └─────────┬───────────┘
                              │
┌─────────────────────────────┼──────────────────────────┐
│ 验证方（溯源方）              ▼                          │
│                                                         │
│  aawm trace CLI ──────┐                                 │
│  aawm serve HTTP ─────┼──→ Watermarker.trace()          │
│  SDK 直接调用 ────────┘        │                        │
│                               ▼                        │
│                     TraceResult 判决                    │
└─────────────────────────────────────────────────────────┘
```

**两个角色**：
- **嵌入方**（Agent 平台）：持有 master_key，给所有输出嵌水印
- **验证方**（审计/风控）：持有**同一把** master_key + 发布记录，做溯源

> master_key 是对称的——嵌入和溯源用同一把钥匙。谁有钥匙谁就能嵌入，因此钥匙只在可信边界内（嵌入方服务 + 验证方服务）流转。

---

## 2. 密钥管理

### 2.1 生成与存储

```bash
# 生成（32 字节随机）
aawm keygen --output /etc/aawm/key.json
# 文件自动 chmod 600

# 或输出为环境变量格式（写进 secret manager）
aawm keygen --env AAWM_MASTER_KEY
# export AAWM_MASTER_KEY=6a1b2c3d...（64 hex 字符）
```

### 2.2 三种加载方式（按优先级）

```python
# 方式 A：文件
ks = KeyStore.from_file("/etc/aawm/key.json")

# 方式 B：环境变量
ks = KeyStore.from_env()  # 读 AAWM_MASTER_KEY

# 方式 C：统一入口（推荐，直接给 Watermarker）
wm = Watermarker(
    keystore=KeyStore.from_any(
        key_file="/etc/aawm/key.json",   # 优先
        # env_var="AAWM_MASTER_KEY",     # 次选
    )
)
```

### 2.3 密钥轮换

**当前版本（v0.6）的限制**：master_key 换了，旧水印无法再溯源。

轮换策略建议：
1. 新旧钥匙并行期：新旧两个 Watermarker 实例，检测时依次尝试
2. session_salt 是公开的——可以在发布记录里同时存 `key_version`，检测时直接选对应钥匙
3. 每个语言（en/zh）的绿名单是独立派生的（language_tag 隔离），不影响轮换复杂度

### 2.4 安全基线

- [ ] master_key 不进代码库、不进日志、不进错误堆栈
- [ ] key.json 权限 600（自动设置），目录 700
- [ ] 验证方与嵌入方的密钥同步走独立的 secret 通道
- [ ] 备份：密钥丢失 = 历史水印全部不可溯源，必须离线备份

---

## 3. UID 注册库运维

### 3.1 存储

```bash
# 文件后端（JSON，追加式写，原子替换）
aawm registry add "user-alice" --registry /var/lib/aawm/registry.json
```

文件格式：
```json
{
  "uid_bits": 16,
  "entries": [
    {"uid": 1, "alias": "user-alice"},
    {"uid": 2, "alias": "user-bob"}
  ]
}
```

### 3.2 容量与分配

- **16-bit UID = 65536 个用户**（UID 0 保留）
- 自动分配从 1 递增，指定 UID 需避免冲突（`register` 冲突会抛错）
- 单文件后端适合 ≤ 万级用户；更大规模建议换 SQLite/Postgres（实现 `UIDRegistry` 的子类即可，接口见 `api_reference.md`）

### 3.3 为什么最近邻匹配很重要

UID 逐位解码在改写攻击下会翻转个别 bit（实测 30% 改写汉明距 1-3）。
注册库最近邻匹配（默认 max_hamming=3）能把这些偏差纠回来。

**运维要点**：
- 用户数越多，随机 UID 撞近邻的概率越高。65536 个 UID 用满时，两个随机 UID 的期望汉明距是 8，max_hamming=3 仍安全
- **注册密度建议**：UID 空间占用 < 50% 时，误匹配概率可忽略；超过 50% 考虑缩小 max_hamming 或升 32-bit（需改 n_bands）

---

## 4. 检测服务部署

### 4.1 启动

```bash
# 最简
aawm serve --key /etc/aawm/key.json --port 8765

# 完整
aawm serve \
  --key /etc/aawm/key.json \
  --registry /var/lib/aawm/registry.json \
  --port 8765 \
  --log-level warning
```

### 4.2 systemd 单元示例

```ini
# /etc/systemd/system/aawm-trace.service
[Unit]
Description=AAWM Watermark Trace Service
After=network.target

[Service]
Type=simple
User=aawm
Environment=PYTHONPATH=/opt/aawm/src
ExecStart=/opt/aawm/venv/bin/python -m aawm.cli serve \
  --key /etc/aawm/key.json \
  --registry /var/lib/aawm/registry.json \
  --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 4.3 API 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /v1/health | 健康检查 |
| POST | /v1/trace | 溯源检测（公开给审计系统） |
| POST | /v1/embed | 嵌入（**内部用，务必加网络隔离**） |

> **安全提醒**：/v1/embed 能用水印密钥嵌入任意文本——生产部署应放在内网或加鉴权层（如 nginx basic auth / mTLS）。当前版本未内置鉴权。

### 4.4 p0 标定（提升检测精度，可选但推荐）

```python
# 一次性标定：用 50-100 篇真实无水印文本（与业务文本同分布）
wm.calibrate_p0(corpus_texts, language="en")
wm.calibrate_p0(corpus_texts_zh, language="zh")
```

标定修正逐带绿率基线（实测带间 p0 ∈ [0.411, 0.535]，不标定会有系统性偏移）。
标定结果当前不持久化——重启后需重新标定（v0.7 计划支持导出/导入）。

---

## 5. 性能调优

### 5.1 关键参数

| 参数 | 位置 | 默认 | 说明 |
|---|---|---|---|
| `bias` | `embed()` | 1.0 | 嵌入强度。1.0=全词典词参与（最强信号）；0.8=20%随机跳过（文本改动更少，鲁棒性略降） |
| `min_text_length` | `WatermarkMiddleware` | 50 | 短于该值不嵌入（避免碎片） |
| `n_bands` | `GreenlistCodec` | 16 | 频带数=UID 位宽。16=65536 用户 |
| `max_hamming` | `DetectionThresholds` | 3 | 注册库匹配容错。越大召回越高、误匹配风险越大 |
| `adaptive_factor` | `DetectionThresholds` | 2.0 | 存在性阈值系数。越大越保守（FPR 低 TPR 也低） |
| `existence_floor` | `DetectionThresholds` | 8.0 | 存在性阈值下限（极短文本兜底） |

### 5.2 调优决策表

| 症状 | 调整 |
|---|---|
| 无水印文本被误报（FPR 高） | `adaptive_factor` 2.0 → 2.5；或先做 p0 标定 |
| 真水印漏检（TPR 低） | 文本太短（词典词 < 30）；调低 `existence_floor`，或接受短文本只做 A 信道验证 |
| 改写后 UID 匹配失败 | `max_hamming` 3 → 4（注意注册密度）；或注册库补录 |
| 文本改动太多被用户投诉 | `bias` 1.0 → 0.85 |
| 流式延迟高 | 句子缓冲粒度已是最低（单句）；检查是否网络/上游慢 |

### 5.3 性能基准（实测，2026-08 沙箱环境）

| 文本长度（词） | 嵌入耗时 | 检测耗时 |
|---|---|---|
| 200 | 7.2 ms | 6.3 ms |
| 600 | 8.1 ms | 6.8 ms |
| 1200 | 10.0 ms | 6.7 ms |
| 2400 | 13.7 ms | 8.2 ms |

- 内存：Watermarker 实例 ~1 KB（词典构建 ~50 MB 进程级共享，Python import 一次性开销）
- 相对开销：嵌入耗时 < LLM 生成耗时的 0.1%（G8 目标 <10% 轻松达成）
- 详细数据见 `docs/performance.md`

---

## 6. 监控与可观测性

### 建议监控的指标

| 指标 | 采集点 | 告警阈值 |
|---|---|---|
| 嵌入失败率 | `WatermarkMiddleware` logger warning | > 0.1%（fail-open 频发=有 bug） |
| 嵌入跳过率（无上下文） | transform 返回 None | 突增=context 传递断了 |
| 平均嵌入耗时 | embed() 前后计时 | > 50ms（异常） |
| 检测 existence_score 分布 | trace() 结果 | 均值漂移=语料分布变化，重标定 p0 |
| salt 存档成功率 | 发布记录写入 | < 100%（缺 salt 的记录溯源降级） |

### 日志

中间件用 `aawm.plugin.middleware` logger，fail-open 时打 warning（含异常信息，**不含密钥**）。

---

## 7. 升级与兼容

| 版本 | 破坏性变更 | 迁移 |
|---|---|---|
| 0.5 → 0.6 | 无（纯新增插件层） | 算法层 API 完全不变 |
| 0.6 内 | `DetectionThresholds` 字段名变更（existence_score → adaptive_factor/existence_floor） | 只影响自定义阈值的调用方 |

---

## 8. 故障排查

**嵌入后 trace 不到（watermarked=False）**
1. 检查 trace 是否传了嵌入时的 session_salt（没传精度大降）
2. 文本词典命中数（n_dict_words）< 30？文本太短，属正常降级
3. 两边 master_key 是否一致（key.json 是否同一份）
4. 语言检测是否一致（一边 en 一边 zh 的 language_tag 不同，绿名单完全不同）

**uid 解出来但 user 为 None**
1. 注册库里没有该 UID 的近邻（汉明距 > max_hamming）
2. 文本被重度改写（>30%），UID 翻转过多——查 trace.hamming_dist
3. 调大 max_hamming 或接受降级

**fail-open 频发（日志大量 warning）**
1. 嵌入抛异常——看 warning 里的异常类型
2. 常见：user_id 超范围（16-bit 上限 65535）、注册库文件不可写
