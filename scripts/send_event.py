#!/usr/bin/env python3
"""本地联调脚本：对运行中的服务投递一个（正确/错误签名的）事件。

用法：
    # 终端 1
    uv run uvicorn main:app --reload
    # 终端 2
    uv run python scripts/send_event.py release.created --sequence 0
    uv run python scripts/send_event.py build.started   --sequence 1
    uv run python scripts/send_event.py build.failed    --sequence 2 --reason "oom"
    uv run python scripts/send_event.py build.retry_requested --sequence 3
    # 故意签错名，观察 401 invalid_signature
    uv run python scripts/send_event.py release.created --sequence 5 --bad-signature
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import uuid

from release_pipeline.signing import compute_signature

DEFAULT_URL = "http://localhost:8000/webhook/events"


def main() -> int:
    parser = argparse.ArgumentParser(description="投递一个发布流水线事件")
    parser.add_argument("event_type")
    parser.add_argument("--release-id", default="demo-release")
    parser.add_argument("--event-id", default=None)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--timestamp", default="2026-09-28T00:00:00Z")
    parser.add_argument("--reason", default=None)
    parser.add_argument("--secret", default="dev-secret")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--bad-signature",
        action="store_true",
        help="故意发送错误签名（用于验证 401）",
    )
    args = parser.parse_args()

    event_id = args.event_id or f"evt-{uuid.uuid4().hex[:8]}"
    payload: dict = {
        "event_id": event_id,
        "event_type": args.event_type,
        "release_id": args.release_id,
        "sequence": args.sequence,
        "timestamp": args.timestamp,
    }
    if args.reason is not None:
        payload["reason"] = args.reason
    body = json.dumps(payload).encode("utf-8")

    signature = "0" * 64 if args.bad_signature else compute_signature(
        args.secret, event_id, body
    )
    request = urllib.request.Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Event-Id": event_id,
            "X-Signature": signature,
        },
    )
    # 显式绕过环境里的 HTTP 代理，保证 127.0.0.1 的联调请求直达本地服务
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request) as response:
            print(response.status, response.read().decode())
    except urllib.error.HTTPError as exc:
        print(exc.code, exc.read().decode(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
