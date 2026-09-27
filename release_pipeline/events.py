"""webhook 事件类型定义与解析。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EventType(str, Enum):
    """平台支持的发布相关事件类型。"""

    PUSH = "push"
    BUILD_STARTED = "build.started"
    BUILD_SUCCEEDED = "build.succeeded"
    BUILD_FAILED = "build.failed"
    DEPLOY_STARTED = "deploy.started"
    DEPLOY_SUCCEEDED = "deploy.succeeded"
    DEPLOY_FAILED = "deploy.failed"
    RELEASE_CANCELLED = "release.cancelled"
    RELEASE_TIMED_OUT = "release.timed_out"
    RETRY_REQUESTED = "retry.requested"
    ROLLBACK_REQUESTED = "rollback.requested"


@dataclass(frozen=True)
class Event:
    """归一化后的 webhook 事件。"""

    event_id: str
    event_type: EventType
    release_id: str
    version: str
    seq: int
    reason: str | None = None
