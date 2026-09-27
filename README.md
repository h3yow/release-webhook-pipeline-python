# release-webhook-pipeline-python

由代码托管平台 webhook 驱动的发布流水线**状态服务**示例。它接收平台推来的
发布事件，校验签名与内容，按状态机推进版本发布，并在**重复投递、乱序、
并发、构建失败/取消/超时**下保持最终状态稳定一致、过程可复现。

完整规则见 [`docs/design.md`](docs/design.md)。

## 能力一览

- **三类可区分的拒绝原因**：签名错误 `401 invalid_signature`、内容格式错误
  `400 invalid_payload`、未知事件类型 `422 unknown_event_type`。
- **幂等去重**：同一 `event_id` 重复投递只生效一次（`decision=duplicate`）。
- **顺序保护**：迟到的旧事件判为 `stale`，不会把状态改回去；先到的未来事件
  进入缓冲，缺口补齐后按序回放。
- **明确终态**：成功 / 构建失败 / 取消 / 超时 / 已回滚；构建失败最多重试 2 次，
  超限返回可解释的 `retry_rejected`；回滚只允许从 `succeeded` 发起。
- **过程可查**：`GET /releases/{id}` 返回状态、尝试记录与完整事件判定历史。

## 目录结构

```
src/release_pipeline/
  state.py                 状态、事件类型与常量
  events.py                事件 schema 与解析（pydantic）
  signing.py               HMAC-SHA256 签名计算/校验
  store.py                 状态存储抽象 + 线程安全内存实现
  service.py               状态机：去重/乱序/缓冲/重试/回滚
  webhook/handler.py       三层校验（签名 -> payload -> 事件类型）
  webhook/routes.py        FastAPI 路由与错误响应
  app.py                   应用组装
tests/                     27 个单测（去重/乱序/签名/失败/并发等）
scripts/send_event.py      向运行中的服务投递单个事件
scripts/demo_flow.py       一键演示完整场景（无需起服务）
docs/design.md             签名/去重顺序/状态流转的详细设计
```

## 本地运行

需要 uv（仓库已带 `uv.lock`，Python 3.14）。

```bash
uv sync --all-groups          # 安装运行与测试依赖
uv run pytest                 # 运行全部测试（27 个）
```

启动服务：

```bash
WEBHOOK_SECRET=dev-secret uv run uvicorn main:app --reload --port 8000
```

另开一个终端投递事件（脚本会自动签名）：

```bash
uv run python scripts/send_event.py release.created --sequence 0
uv run python scripts/send_event.py build.started    --sequence 1
uv run python scripts/send_event.py build.succeeded  --sequence 2
uv run python scripts/send_event.py deploy.succeeded --sequence 3

# 查询状态与完整判定历史
curl -s http://localhost:8000/releases/demo-release | python -m json.tool

# 验证三类拒绝原因
uv run python scripts/send_event.py release.created --sequence 9 --bad-signature  # 401
curl -s -X POST http://localhost:8000/webhook/events \
  -H 'X-Event-Id: x' -H 'X-Signature: 00' -d '{not-json'                      # 400
uv run python scripts/send_event.py merge.teleported --sequence 0             # 422
```

不想起服务也可以看完整链路演示（含重投、乱序、构建失败重试上限、回滚）：

```bash
uv run python scripts/demo_flow.py
```

## 接口速览

| 方法与路径 | 说明 |
| --- | --- |
| `POST /webhook/events` | 接收事件，头 `X-Event-Id` / `X-Signature`，体为事件 JSON |
| `GET /releases/{release_id}` | 查询当前状态、终态标记、尝试记录、判定历史 |

事件接收成功统一返回 HTTP 200，并用 `decision` 说明判定：
`applied` / `duplicate` / `stale` / `buffered` / `ignored` / `retry_rejected`。
