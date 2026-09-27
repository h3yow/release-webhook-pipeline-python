# 发布流水线状态服务 — 设计说明

一个由代码托管平台 webhook 驱动的**发布状态机**。服务本身不执行构建/部署，
只接收平台推来的事件、做校验、推进状态，并保证在**异常输入、重复投递、
乱序与并发**下状态仍然说得清楚、可复现。

---

## 1. 事件与签名校验

### 1.1 请求形态

```
POST /webhook/events
Headers:
  X-Event-Id: <平台生成的事件唯一 ID>
  X-Signature: <十六进制 HMAC-SHA256 摘要>
  Content-Type: application/json
Body: 见下方 schema
```

```json
{
  "event_id": "evt-001",
  "event_type": "build.started",
  "release_id": "rel-42",
  "sequence": 1,
  "timestamp": "2026-09-28T10:00:00Z",
  "reason": "optional, free text",
  "attempt": null
}
```

* `event_id`：平台为每次投递生成的全局唯一 ID，**重投保持不变**，用于幂等去重。
* `sequence`：平台为**单次发布**维护的单调递增整数（从 0 开始），用于识别乱序。
* `event_id` 头必须与请求体中的 `event_id` 一致，防止把有效签名挪用到别的事件。

### 1.2 签名算法

```
signature = hex( HMAC_SHA256( secret, event_id + "." + raw_body ) )
```

* 只对**原始字节请求体**计算，服务端在校验通过前不依赖任何 JSON 解析结果；
* 使用 `hmac.compare_digest` 做恒定时间比较，避免时序侧信道；
* 密钥通过环境变量 `WEBHOOK_SECRET` 配置（默认 `dev-secret`，仅限本地）。

生成一个合法签名：

```bash
uv run python - <<'PY'
from release_pipeline.signing import compute_signature
body = b'{"event_id":"e1","event_type":"release.created",' \
       b'"release_id":"r1","sequence":0,"timestamp":"t"}'
print(compute_signature("dev-secret", "e1", body))
PY
```

### 1.3 拒绝原因（三类明确区分）

校验顺序固定，错误互不混淆：

| 顺序 | 检查内容 | 失败时 HTTP | `error` 取值 |
| --- | --- | --- | --- |
| 1 | `X-Event-Id` / `X-Signature` 存在且 HMAC 匹配 | 401 | `invalid_signature` |
| 2 | 请求体是 JSON 对象且符合 schema，头/体 event_id 一致 | 400 | `invalid_payload` |
| 3 | `event_type` 属于已知类型 | 422 | `unknown_event_type` |

响应体形如：`{"error": "invalid_signature", "detail": "..."}`，`detail`
给出可定位问题的具体原因。**被拒绝的事件不会产生任何状态变化。**

### 1.4 支持的事件类型

`release.created`、`build.started`、`build.succeeded`、`build.failed`、
`build.retry_requested`、`deploy.started`、`deploy.succeeded`、
`release.canceled`、`release.timed_out`、`rollback.requested`、
`rollback.succeeded`、`rollback.failed`。

---

## 2. 去重与顺序处理规则

服务对每个 `release_id` 维护一条记录和一把专用锁：同一次发布的事件
**串行处理**，不同发布互不阻塞。单条事件的判定顺序为：

1. **重复投递（duplicate）**
   `event_id` 已处理过 → 直接返回首次结果，**不再推进状态、不增加版本号**。
   即使平台把同一事件投递 N 次，状态机只生效一次。

2. **旧事件（stale）**
   `sequence <= last_sequence`（该发布已应用的最大序号）→ 忽略，状态保持不变。
   旧事件**不登记**为已处理，因此它的任何重投都稳定地再次判为 stale，
   已前进的发布状态永远不会被迟到的旧事件改回去。

3. **未来乱序事件（buffered）**
   `sequence > last_sequence + 1`（中间有缺口）→ 暂存在该发布的
   `pending_by_sequence` 中，不推进状态。当缺失序号补齐时，缓冲区从水位开始
   **连续回放**，保证事件最终严格按 sequence 顺序各应用一次。同序号的重复
   占位按 duplicate 处理。

4. **正常应用（applied）**
   进入状态机；如果当前状态不允许该事件（例如还没构建就 build.succeeded），
   判定为 `ignored` 并记录原因，状态同样不变。

所有判定（含 ignored / buffered / duplicate / stale）都会写入该发布的
`history`，并在日志中打印“收到的事件 → 判定结果”，因此整条链路可复现。

---

## 3. 状态流转

### 3.1 状态图

```
 release.created     build.started      build.succeeded     deploy.succeeded
 (pending) ───────▶ pending ─────────▶ building ─────────▶ deploying ────────▶ succeeded
                       │                   │ │                   │                │
                       │                   │ │  build.failed     │                │ rollback.requested
                       │                   │ ▼                   │                ▼
                       │                   │ build_failed        │           rolling_back
 release.canceled /    │                   │ ▲   │               │            │        │
 release.timed_out     │                   │ │   │ retry_requested            │        │
（pending/building/    └───────────────────┘ │   ▼ (≤ 2 次)      │   rollback.failed   rollback.succeeded
 deploying 均适用）                          └ pending           │            │        │
                                                               canceled /    ▼        ▼
                                                               timed_out   succeeded  rolled_back
```

### 3.2 终态

| 终态 | 如何到达 | 之后的行为 |
| --- | --- | --- |
| `succeeded` | deploy.succeeded | 只接受 rollback.* 系列 |
| `build_failed` | build.failed | 只接受 build.retry_requested（有次数上限） |
| `canceled` | release.canceled（pending/building/deploying 时） | 拒绝一切推进 |
| `timed_out` | release.timed_out（同上） | 拒绝一切推进 |
| `rolled_back` | rollback.succeeded | 拒绝一切推进 |

终态下收到不允许的事件一律 `ignored`，状态不回退。

### 3.3 构建失败与重试

* `build.failed` 只能在 `building` 状态应用，落 `build_failed`；
  每次失败都会在 `attempts` 中留下 `{attempt, result, reason, timestamp}`。
* 重试是**显式**的 `build.retry_requested` 事件：接受后状态回到 `pending`，
  等待平台再推 `build.started`。
* 最多允许 **2 次重试**（即首次执行 + 2 次重试，共 3 次尝试），由
  `MAX_BUILD_RETRIES` 定义。超过上限后：
  * `build.retry_requested` 判定为 `retry_rejected`，状态保持 `build_failed`，
    返回原因中写明“已用次数 / 上限 / 最后一次失败原因”；
  * 直接推 `build.started` 也无法绕过限制，判定为 `ignored`。

### 3.4 回滚规则（明确且可解释）

* 只有 `succeeded` 的发布可以发起 `rollback.requested`，进入 `rolling_back`；
  其他状态请求回滚一律 `ignored`（原因注明当前状态不允许）。
* `rollback.failed`：回滚脚本失败，状态**回到 `succeeded`**（线上版本仍是
  成功版本），并保留失败原因；平台可再次请求回滚。
* `rollback.succeeded`：进入终态 `rolled_back`，此后拒绝一切事件。

---

## 4. 数据与可观测性

`GET /releases/{release_id}` 返回：当前状态、是否终态、版本号、已应用水位
`last_sequence`、重试次数、失败/回滚原因、每次构建尝试记录 `attempts`、
以及完整 `history`（每个收到事件的类型、序号、判定、应用后状态、原因）。

日志（logger 名 `release_pipeline.service` / `release_pipeline.webhook`）示例：

```
event received: id=e2 type=build.started release=rel-42 seq=1
  -> decision=applied state=building changed=True reason=pending -> building
event received: id=e2 type=build.started release=rel-42 seq=1
  -> decision=duplicate state=succeeded changed=False reason=event_id 'e2' already processed
```

## 5. 存储说明

当前实现为线程安全的内存存储（`InMemoryReleaseStore`），适用于单实例演示与
测试。接口 `ReleaseStore` 已隔离：替换为数据库实现时，需要在**单个数据库事务**
内完成“读记录 → 去重/顺序判定 → 写状态/历史”，并对同一 release 串行化，
即可保持本文档的全部语义。内存实现重启后状态丢失，请勿直接用于生产。
