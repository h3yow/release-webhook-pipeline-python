"""签名缺失/错误、载荷格式错误、未知事件类型：三类拒绝原因必须可区分。"""

from __future__ import annotations

from tests.conftest import make_event


def test_missing_signature_is_rejected_as_401(client, send):
    resp = send(client, make_event("push", 1), signature=None)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "invalid_signature"
    assert "缺少" in body["error"]["message"]


def test_bad_signature_is_rejected_as_401(client, send):
    resp = send(client, make_event("push", 1), signature="sha256=deadbeef")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


def test_wrong_secret_is_rejected_as_401(client, send):
    resp = send(client, make_event("push", 1), secret="attacker-secret")
    assert resp.status_code == 401


def test_malformed_signature_format_is_401(client, send):
    resp = send(client, make_event("push", 1), signature="not-a-hmac")
    assert resp.status_code == 401
    assert "格式" in resp.json()["error"]["message"]


def test_invalid_json_is_400(client, send):
    resp = send(client, b"{not-json")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_payload"


def test_missing_required_field_is_400(client, send):
    payload = make_event("push", 1)
    del payload["release_id"]
    resp = send(client, payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_payload"
    assert "release_id" in resp.json()["error"]["message"]


def test_bad_seq_type_is_400(client, send):
    payload = make_event("push", "one")  # type: ignore[arg-type]
    resp = send(client, payload)
    assert resp.status_code == 400


def test_unknown_event_type_is_422(client, send):
    resp = send(client, make_event("deploy.to-the-moon", 1))
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "unknown_event_type"
    assert "deploy.to-the-moon" in body["error"]["message"]


def test_valid_push_is_accepted(client, send):
    resp = send(client, make_event("push", 1))
    assert resp.status_code == 200
    assert resp.json()["decision"] in {"recorded", "advanced"}
