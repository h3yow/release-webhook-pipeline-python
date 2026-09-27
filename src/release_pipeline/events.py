"""入站 webhook 事件的 schema 与解析。

平台投递的原始 JSON 由 :class:`WebhookPayload` 校验，再转换为领域事件
:class:`ReleaseEvent` 交给流水线服务处理。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError

from .errors import InvalidPayloadError, UnknownEventTypeError

# 平台支持的全部事件类型（字符串即 HTTP 头 X-Event-Type 的取值）
KNOWN_EVENT_TYPES = frozenset(
    {
        "release.created",
        "build.started",
        "build.succeeded",
        "build.failed",
        "build.retry_requested",
        "deploy.started",
        "deploy.succeeded",
        "release.canceled",
        "release.timed_out",
        "rollback.requested",
        "rollback.succeeded",
        "rollback.failed",
    }
)


class WebhookPayload(BaseModel):
    """对平台请求体的最小结构化校验。"""

    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    # 平台为每次发布维护的单调递增序号，用于识别乱序旧事件
    sequence: int = Field(ge=0)
    timestamp: str = Field(min_length=1)
    reason: str | None = None
    attempt: int | None = Field(default=None, ge=0)


@dataclass(frozen=True, slots=True)
class ReleaseEvent:
    """通过校验、交给状态机处理的领域事件。"""

    event_id: str
    event_type: str
    release_id: str
    sequence: int
    timestamp: str
    reason: str | None = None
    attempt: int | None = None


def parse_event(raw: dict) -> ReleaseEvent:
    """把原始 JSON dict 解析成领域事件。

    * 结构/schema 不合法 -> :class:`InvalidPayloadError`
    * 事件类型不认识 -> :class:`UnknownEventTypeError`
    """
    try:
        payload = WebhookPayload.model_validate(raw)
    except ValidationError as exc:
        raise InvalidPayloadError(str(exc)) from exc

    if payload.event_type not in KNOWN_EVENT_TYPES:
        raise UnknownEventTypeError(
            f"unknown event_type: {payload.event_type!r}"
        )

    return ReleaseEvent(
        event_id=payload.event_id,
        event_type=payload.event_type,
        release_id=payload.release_id,
        sequence=payload.sequence,
        timestamp=payload.timestamp,
        reason=payload.reason,
        attempt=payload.attempt,
    )
