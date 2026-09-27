# Release Webhook Pipeline（Python）

由代码托管平台 webhook 驱动的**发布流水线状态服务**。它接收平台推来的发布事件，
校验签名后按顺序推进版本发布状态，并保证：

- 签名错误、载荷格式错误、未知事件类型三类拒绝**原因可区分**；
- 平台**重复投递**、事件**并发到达**不会重复推进状态（幂等）；
- **乱序到达的旧事件**不能把已经前进的状态改回去；
- 构建失败、取消、超时都落到**明确终态**；失败重试有**次数上限**并保留全过程记录；
- 回滚请求有**明确且可解释**的处理规则。

## 目录结构

```
release_pipeline/
  signature.py   HMAC-SHA256 签名校验
  parser.py      请求体解析（JSON / 必需字段 / 事件类型）
  events.py      事件类型与事件数据模型
  states.py      状态机：状态、终态、合法流转表、重试上限
  models.py      Release 聚合与 TransitionRecord 留痕
  store.py       按 release 加锁的线程安全内存存储
  processor.py   核心：去重、乱序防护、状态推进、重试/回滚规则
  app.py         FastAPI 入口与错误码映射
tests/           pytest 测试（含并发场景）
main.py          本地启动入口
```

## 1. 签名校验方式

平台与服务共享一个密钥（环境变量 `WEBHOOK_SECRET`，默认 `test-secret`）。

- 对**原始请求体字节**计算 `HMAC-SHA256`，十六进制摘要放入请求头：

  ```
  X-Signature: sha256=<hex digest>
  X-Event-Type: build.started        # 也可在 JSON body 里放 event_type
  ```

- 服务端用相同算法重算，使用 `hmac.compare_digest` 恒定时间比较。
- 签名缺失、前缀格式错误、摘要不匹配分别给出不同消息，但统一返回：

  | 情况 | HTTP | error.code |
  |---|---|---|
  | 签名缺失/格式错/不匹配 | 401 | `invalid_signature` |
  | 不是合法 JSON / 缺必需字段 / 字段类型错 | 400 | `invalid_payload` |
  | 事件类型不认识 | 422 | `unknown_event_type` |
  | 乱序/非法流转/终态冲突/重试超限 | 409 | `stale_event` |

事件体必需字段：`event_id`、`release_id`、`version`、`seq`（非负整数）；
事件类型取 `X-Event-Type` 头或 body 的 `event_type`；`reason` 可选。

支持的事件类型：`push`、`build.started`、`build.succeeded`、`build.failed`、
`deploy.started`、`deploy.succeeded`、`deploy.failed`、
`release.cancelled`、`release.timed_out`、`retry.requested`、`rollback.requested`。

## 2. 去重与顺序处理规则

同一 `release_id` 的所有事件在**同一把锁**内串行处理（不同发布互不阻塞），
判定顺序如下：

1. **幂等去重**：`event_id` 已处理过 → 返回 200，`decision="duplicate"`，
   状态不变（重复投递是平台正常行为，不算错误）。
2. **乱序防护**：`seq < last_seq` → 409 拒绝，消息注明落后的序号；
   `seq == last_seq` 但事件 ID 不同（同序号冲突）→ 409 拒绝。
3. **终态防护**：已处于 `cancelled / timed_out / rolled_back` → 任何后续事件 409。
4. 旁路事件（取消、超时、重试、回滚）走各自规则。
5. 普通事件查合法流转表；不允许的流转 → 409，消息注明
   “事件 X 不能作用于状态 Y”。
6. 只有真正推进状态时才更新 `last_seq`；被拒绝事件不会移动水位线，
   但**仍会写一条留痕记录和日志**，保证事后可复现。

> 说明：`push` 只用于登记一次发布（状态保持 `pending`），因此重复的 push
> 按 `event_id` 幂等接受。

## 3. 状态流转

```
pending ──build.started──▶ building ──build.succeeded──▶ build_succeeded
                              │                                │
                         build.failed                    deploy.started
                              ▼                                ▼
                         build_failed  ◀─retry.requested─▶  deploying
                              │  (attempts+1，≤3)               │
                    超限后永久停留                     deploy.succeeded / deploy.failed
                                                                 │
                                              succeeded ──rollback.requested──▶ rolled_back
任意活跃状态 ──release.cancelled──▶ cancelled
任意活跃状态 ──release.timed_out──▶ timed_out
```

**终态**：`succeeded` 之后只允许回滚；`cancelled / timed_out / rolled_back`
不可再离开；`build_failed / deploy_failed` 是可重试的失败状态。

**重试规则**：只有 `build_failed / deploy_failed` 能接收 `retry.requested`；
每次失败 `attempts + 1`，失败原因记入 `failure_reason` 与历史；
最多重试 **3 次**（`MAX_RETRIES=3`），达到上限后重试请求返回 409 并说明
“重试次数已达上限”，发布永久停留在失败状态。全部尝试过程均可通过
`GET /releases/{id}` 的 `history` 字段复盘。

**回滚规则**：只有 `succeeded` 状态的发布能接收 `rollback.requested`
（流转到 `rolled_back`）；其它状态请求回滚返回 409，消息明确说明
“只有 succeeded 状态的发布才能回滚，当前状态为 X”。

## 4. 接口

- `POST /webhook` — 接收事件，返回 `{release_id, state, decision, progressed, note}`
- `GET  /releases` — 所有发布
- `GET  /releases/{release_id}` — 发布详情（含 `history` 留痕）
- `GET  /health`

## 5. 本地跑起来验证

```bash
# 安装依赖（需要 Python 3.14，使用 uv）
uv sync

# 启动服务
uv run python main.py            # 默认 127.0.0.1:8000

# 另一个终端：用演示脚本跑完整链路（含正确签名、重复投递、乱序、失败重试）
uv run python scripts/demo.py
```

也可以手动发一个带签名的请求：

```bash
SECRET=test-secret
BODY='{"event_id":"e1","event_type":"build.started","release_id":"r1","version":"v1.0.0","seq":1}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.*= //')"
curl -s -X POST localhost:8000/webhook \
  -H "X-Signature: $SIG" -H "X-Event-Type: build.started" \
  -H "Content-Type: application/json" -d "$BODY"
```

查看状态与完整过程记录：

```bash
curl -s localhost:8000/releases/r1 | python -m json.tool
```

## 6. 运行测试

```bash
uv run pytest                    # 全部用例
uv run pytest -k duplicate       # 只看重复投递
uv run pytest -k order           # 只看乱序
```

测试覆盖：重复投递幂等、乱序旧事件不可回退、8 路并发只推进一次、
签名缺失/错误、非法 JSON、未知事件类型、构建失败留痕、重试上限、
取消/超时终态、回滚规则。测试运行时日志会打印**收到的每个事件**及其
**判定结果**（同时写入 `test-events.log`）。
