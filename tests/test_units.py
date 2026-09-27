"""不经过 HTTP 的纯单元测试：签名与解析器。"""

from __future__ import annotations

import json

import pytest

from release_pipeline.errors import (
    InvalidPayloadError,
    InvalidSignatureError,
    UnknownEventTypeError,
)
from release_pipeline.events import EventType
from release_pipeline.parser import parse_event
from release_pipeline.signature import compute_signature, verify_signature


def test_signature_roundtrip():
    body = b'{"a": 1}'
    sig = compute_signature("s3cr3t", body)
    verify_signature("s3cr3t", body, f"sha256={sig}")  # 不抛异常即通过


def test_signature_rejects_tampered_body():
    sig = compute_signature("s3cr3t", b'{"a": 1}')
    with pytest.raises(InvalidSignatureError):
        verify_signature("s3cr3t", b'{"a": 2}', f"sha256={sig}")


def test_parse_event_from_header_type():
    raw = json.dumps(
        {"event_id": "e1", "release_id": "r1", "version": "v9", "seq": 0}
    ).encode()
    event = parse_event(raw, "build.started")
    assert event.event_type is EventType.BUILD_STARTED
    assert event.reason is None


def test_parse_event_from_body_type():
    raw = json.dumps(
        {
            "event_id": "e1",
            "event_type": "build.failed",
            "release_id": "r1",
            "version": "v9",
            "seq": 3,
            "reason": "boom",
        }
    ).encode()
    event = parse_event(raw, None)
    assert event.event_type is EventType.BUILD_FAILED
    assert event.reason == "boom"


def test_parse_rejects_non_object_body():
    with pytest.raises(InvalidPayloadError):
        parse_event(b"[1,2,3]", "push")


def test_parse_rejects_unknown_type():
    raw = json.dumps(
        {"event_id": "e1", "release_id": "r1", "version": "v9", "seq": 0}
    ).encode()
    with pytest.raises(UnknownEventTypeError):
        parse_event(raw, "no.such.event")


def test_parse_rejects_negative_seq():
    raw = json.dumps(
        {"event_id": "e1", "release_id": "r1", "version": "v9", "seq": -1}
    ).encode()
    with pytest.raises(InvalidPayloadError):
        parse_event(raw, "push")
