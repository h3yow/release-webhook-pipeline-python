"""请求体解析：JSON 合法性、必需字段、事件类型识别。

三类问题分别抛出不同异常：

* 原始字节不是合法 JSON / 缺少必需字段 / 字段类型不对 -> InvalidPayloadError
* ``X-Event-Type`` 头或 JSON 中 ``event_type`` 的值不认识
  -> UnknownEventTypeError
"""

from __future__ import annotations

import json

from release_pipeline.errors import InvalidPayloadError, UnknownEventTypeError
from release_pipeline.events import Event, EventType

_REQUIRED_FIELDS = ("event_id", "release_id", "version", "seq")


def parse_event(raw_body: bytes, event_type_header: str | None) -> Event:
    """把原始请求体解析成 :class:`Event`。"""
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidPayloadError(f"请求体不是合法 JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise InvalidPayloadError("请求体必须是 JSON 对象")

    for field_name in _REQUIRED_FIELDS:
        if field_name not in payload:
            raise InvalidPayloadError(f"缺少必需字段: {field_name}")

    raw_type = event_type_header or payload.get("event_type")
    if not isinstance(raw_type, str) or not raw_type:
        raise InvalidPayloadError("缺少事件类型（X-Event-Type 头或 event_type 字段）")

    try:
        event_type = EventType(raw_type)
    except ValueError as exc:
        raise UnknownEventTypeError(f"未知事件类型: {raw_type!r}") from exc

    event_id = payload["event_id"]
    release_id = payload["release_id"]
    version = payload["version"]
    seq = payload["seq"]
    if not isinstance(event_id, str) or not event_id:
        raise InvalidPayloadError("event_id 必须是非空字符串")
    if not isinstance(release_id, str) or not release_id:
        raise InvalidPayloadError("release_id 必须是非空字符串")
    if not isinstance(version, str) or not version:
        raise InvalidPayloadError("version 必须是非空字符串")
    # bool 是 int 的子类型，单独排除，避免 True/False 混作序号
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise InvalidPayloadError("seq 必须是非负整数")

    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise InvalidPayloadError("reason 若提供必须是字符串")

    return Event(
        event_id=event_id,
        event_type=event_type,
        release_id=release_id,
        version=version,
        seq=seq,
        reason=reason,
    )
