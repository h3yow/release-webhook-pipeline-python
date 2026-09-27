"""pytest 公共夹具：签名工具 + TestClient。"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from release_pipeline.app import create_app
from release_pipeline.signature import compute_signature

SECRET = "test-secret"


@pytest.fixture
def client() -> TestClient:
    app = create_app(secret=SECRET)
    return TestClient(app)


@pytest.fixture
def send():
    """返回一个发送函数：自动签名、组装头，可通过参数制造异常情况。"""

    def _send(
        client: TestClient,
        payload: dict | bytes,
        *,
        secret: str = SECRET,
        signature: str | None = "auto",
        event_type_header: str | None = None,
    ):
        if isinstance(payload, dict):
            raw = json.dumps(payload).encode("utf-8")
        else:
            raw = payload
        if signature == "auto":
            sig = "sha256=" + compute_signature(secret, raw)
        else:
            sig = signature
        headers = {}
        if sig is not None:
            headers["X-Signature"] = sig
        if event_type_header is not None:
            headers["X-Event-Type"] = event_type_header
        return client.post("/webhook", content=raw, headers=headers)

    return _send


@pytest.fixture
def event_logs(caplog):
    """捕获 release_pipeline 日志（收到的事件 + 判定结果）。"""
    caplog.set_level(logging.INFO, logger="release_pipeline")
    return caplog


def make_event(
    event_type: str,
    seq: int,
    *,
    event_id: str | None = None,
    release_id: str = "rel-1",
    version: str = "v1.0.0",
    reason: str | None = None,
) -> dict:
    """构造一个合法事件载荷。"""
    payload = {
        "event_id": event_id or f"evt-{event_type}-{seq}",
        "event_type": event_type,
        "release_id": release_id,
        "version": version,
        "seq": seq,
    }
    if reason is not None:
        payload["reason"] = reason
    return payload
