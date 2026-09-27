#!/usr/bin/env python3
"""端到端演示（不依赖运行中的服务器，直接走 ASGI 应用）。

覆盖：正常推进、重复投递、乱序旧事件、构建失败与重试上限、回滚。
运行：uv run python scripts/demo_flow.py
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from release_pipeline.app import create_app
from release_pipeline.config import Settings
from release_pipeline.signing import compute_signature

SECRET = "dev-secret"
app = create_app(Settings(webhook_secret=SECRET))
client = TestClient(app)


def send(event_id: str, event_type: str, sequence: int, *,
         release_id: str = "rel-demo", reason: str | None = None,
         bad_signature: bool = False) -> None:
    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "release_id": release_id,
        "sequence": sequence,
        "timestamp": "2026-09-28T00:00:00Z",
    }
    if reason:
        payload["reason"] = reason
    body = json.dumps(payload).encode()
    signature = "0" * 64 if bad_signature else compute_signature(
        SECRET, event_id, body
    )
    response = client.post(
        "/webhook/events",
        content=body,
        headers={"X-Event-Id": event_id, "X-Signature": signature},
    )
    tag = "HTTP " + str(response.status_code)
    print(f"  -> {tag}: {response.text}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    section("正常发布流程")
    send("e0", "release.created", 0)
    send("e1", "build.started", 1)
    send("e2", "build.succeeded", 2)
    send("e3", "deploy.succeeded", 3)

    section("重复投递同一事件（幂等）")
    send("e1", "build.started", 1)

    section("乱序旧事件迟到（不回退状态）")
    send("e-late", "build.failed", 2, reason="late failure")

    section("错误签名被拒绝")
    send("e-x", "build.started", 9, bad_signature=True)

    section("构建失败与重试（另一次发布）")
    send("f0", "release.created", 0, release_id="rel-retry")
    send("f1", "build.started", 1, release_id="rel-retry")
    send("f2", "build.failed", 2, release_id="rel-retry", reason="oom")
    send("f3", "build.retry_requested", 3, release_id="rel-retry")
    send("f4", "build.started", 4, release_id="rel-retry")
    send("f5", "build.failed", 5, release_id="rel-retry", reason="oom again")
    send("f6", "build.retry_requested", 6, release_id="rel-retry")
    send("f7", "build.started", 7, release_id="rel-retry")
    send("f8", "build.failed", 8, release_id="rel-retry", reason="oom 3rd")
    send("f9", "build.retry_requested", 9, release_id="rel-retry")

    section("回滚演示（回到第一次成功的发布）")
    send("rb1", "rollback.requested", 4)
    send("rb2", "rollback.failed", 5, reason="flaky rollback script")
    send("rb3", "rollback.requested", 6)
    send("rb4", "rollback.succeeded", 7)

    section("最终状态查询")
    for rid in ("rel-demo", "rel-retry"):
        state = client.get(f"/releases/{rid}").json()
        print(f"  {rid}: state={state['state']} "
              f"attempts={state['build_attempts']} "
              f"history_events={len(state['history'])}")
        if state.get("failure_reason"):
            print(f"    failure_reason: {state['failure_reason']}")


if __name__ == "__main__":
    main()
