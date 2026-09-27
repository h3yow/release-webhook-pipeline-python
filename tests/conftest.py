"""测试公共夹具：签名工具、客户端、可观测日志。

单测约定：每次"收到事件 -> 判定结果"都会由服务/处理器打印日志，
测试通过 caplog 断言日志里确实记录了事件与判定。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from release_pipeline.app import create_app
from release_pipeline.config import Settings
from release_pipeline.service import ReleasePipelineService
from release_pipeline.signing import compute_signature


@pytest.fixture
def secret() -> str:
    return "test-secret"


@pytest.fixture
def service() -> ReleasePipelineService:
    return ReleasePipelineService()


@pytest.fixture
def client(secret: str) -> TestClient:
    app = create_app(Settings(webhook_secret=secret))
    return TestClient(app)


@dataclass
class EventBuilder:
    """构造事件载荷并按正确方式签名、投递。"""

    secret: str
    client: TestClient

    def body(
        self,
        event_id: str,
        event_type: str,
        release_id: str = "r1",
        sequence: int = 0,
        timestamp: str = "2026-09-28T00:00:00Z",
        reason: str | None = None,
        attempt: int | None = None,
    ) -> bytes:
        payload: dict[str, Any] = {
            "event_id": event_id,
            "event_type": event_type,
            "release_id": release_id,
            "sequence": sequence,
            "timestamp": timestamp,
        }
        if reason is not None:
            payload["reason"] = reason
        if attempt is not None:
            payload["attempt"] = attempt
        return json.dumps(payload).encode("utf-8")

    def sign(self, event_id: str, body: bytes, override: str | None = None) -> str:
        if override is not None:
            return override
        return compute_signature(self.secret, event_id, body)

    def send(
        self,
        event_id: str,
        event_type: str,
        release_id: str = "r1",
        sequence: int = 0,
        *,
        reason: str | None = None,
        signature: str | None = None,
        event_id_header: str | None = None,
        raw_body: bytes | None = None,
    ):
        body = raw_body or self.body(
            event_id, event_type, release_id, sequence, reason=reason
        )
        headers = {
            "X-Event-Id": event_id if event_id_header is None else event_id_header,
            "X-Signature": self.sign(event_id, body, signature),
        }
        return self.client.post("/webhook/events", content=body, headers=headers)


@pytest.fixture
def events(secret: str, client: TestClient) -> EventBuilder:
    return EventBuilder(secret=secret, client=client)


@pytest.fixture
def observed(caplog):
    """打开服务/处理器日志捕获，便于断言"事件+判定"被打印。"""
    caplog.set_level(logging.INFO, logger="release_pipeline")
    return caplog
