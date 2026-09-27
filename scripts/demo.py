"""本地端到端演示：启动 TestClient 形态的服务，推一条完整发布链路。

演示内容：正常流转 -> 重复投递 -> 乱序旧事件 -> 构建失败 -> 重试 ->
取消/超时终态 -> 回滚规则。运行：uv run python scripts/demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from release_pipeline.app import create_app
from release_pipeline.signature import compute_signature

SECRET = "test-secret"
client = TestClient(create_app(secret=SECRET))


def post(payload: dict, *, release_id: str = "demo-1", bad_signature: bool = False):
    payload["release_id"] = release_id
    raw = json.dumps(payload).encode("utf-8")
    secret = "wrong" if bad_signature else SECRET
    headers = {
        "X-Signature": "sha256=" + compute_signature(secret, raw),
        "X-Event-Type": payload["event_type"],
    }
    resp = client.post("/webhook", content=raw, headers=headers)
    body = resp.json()
    print(f"  seq={payload['seq']:<2} {payload['event_type']:<20} "
          f"-> HTTP {resp.status_code} {body.get('decision') or body.get('error', {}).get('code')}")
    return resp


def event(t: str, seq: int, *, eid: str | None = None, reason: str | None = None):
    p = {
        "event_id": eid or f"demo-{t}-{seq}",
        "event_type": t,
        "release_id": "demo-1",
        "version": "v1.2.3",
        "seq": seq,
    }
    if reason:
        p["reason"] = reason
    return p


def main() -> None:
    print("== 1. 正常推进到发布成功 ==")
    post(event("push", 1))
    post(event("build.started", 2))
    post(event("build.succeeded", 3))
    post(event("deploy.started", 4))
    post(event("deploy.succeeded", 5))

    print("== 2. 重复投递同一事件（幂等，不再推进） ==")
    post(event("build.started", 2))

    print("== 3. 乱序旧事件晚到（409，状态不回退） ==")
    post(event("build.started", 2, eid="late-old"))

    print("== 4. 成功后回滚 ==")
    post(event("rollback.requested", 6))
    print("  回滚后再回滚：")
    post(event("rollback.requested", 7))

    print("== 5. 另一条发布：失败 -> 重试，达到上限后被拒 ==")
    post(event("build.started", 1), release_id="demo-2")
    seq = 2
    for i in range(3):
        # retry.requested 本身就会把状态带回 building，下一次失败可直接到达
        post(event("build.failed", seq, reason=f"编译失败#{i}"), release_id="demo-2")
        seq += 1
        post(event("retry.requested", seq), release_id="demo-2")
        seq += 1

    print("== 6. 拒绝原因可区分：坏签名 / 坏 JSON / 未知类型 ==")
    post(event("push", 1), bad_signature=True)
    raw = b"{oops"
    r = client.post(
        "/webhook",
        content=raw,
        headers={"X-Signature": "sha256=" + compute_signature(SECRET, raw)},
    )
    print(f"  非法 JSON -> HTTP {r.status_code} {r.json()['error']['code']}")

    print("\n最终状态：")
    for rid in ("demo-1", "demo-2"):
        d = client.get(f"/releases/{rid}").json()
        print(f"  {rid}: state={d['state']} attempts={d['attempts']} "
              f"历史记录={len(d['history'])} 条 failure_reason={d['failure_reason']!r}")


if __name__ == "__main__":
    main()
