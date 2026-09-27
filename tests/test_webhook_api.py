"""HTTP 层：签名错误、payload 错误、未知事件类型、正常推进、重复投递、乱序。"""

from __future__ import annotations

import json
import logging

from release_pipeline.signing import compute_signature


def test_valid_signature_advances_state(events, observed):
    r = events.send("e1", "release.created", "r1", 0)
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "pending"
    assert body["decision"] == "applied"

    # 单测日志中必须同时出现"收到的事件"与"判定结果"
    assert any("e1" in m and "release.created" in m for m in observed.messages)
    assert any("decision=applied" in m for m in observed.messages)


def test_bad_signature_rejected_401(events, client, secret):
    body = events.body("e2", "release.created", "r1", 1)
    r = client.post(
        "/webhook/events",
        content=body,
        headers={"X-Event-Id": "e2", "X-Signature": "0" * 64},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_signature"
    # 被拒绝的事件绝不能推进任何状态
    assert client.get("/releases/r1").status_code == 404


def test_missing_signature_header_rejected_401(events, client):
    body = events.body("e3", "release.created", "r1", 0)
    r = client.post(
        "/webhook/events",
        content=body,
        headers={"X-Event-Id": "e3"},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_signature"


def test_malformed_json_rejected_400(events, client, secret):
    raw = b"{not-json"
    r = client.post(
        "/webhook/events",
        content=raw,
        headers={
            "X-Event-Id": "e4",
            "X-Signature": compute_signature(secret, "e4", raw),
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_payload"
    assert "not valid JSON" in r.json()["detail"]


def test_schema_violation_rejected_400_with_distinct_reason(
    events, client, secret
):
    raw = json.dumps(
        {
            "event_id": "e5",
            "event_type": "build.started",
            "release_id": "r1",
            "sequence": -3,  # 非法：序号必须 >= 0
            "timestamp": "t",
        }
    ).encode()
    r = client.post(
        "/webhook/events",
        content=raw,
        headers={
            "X-Event-Id": "e5",
            "X-Signature": compute_signature(secret, "e5", raw),
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_payload"
    assert r.json()["error"] != "unknown_event_type"


def test_unknown_event_type_rejected_422(events, client, secret):
    raw = json.dumps(
        {
            "event_id": "e6",
            "event_type": "merge.teleported",
            "release_id": "r1",
            "sequence": 0,
            "timestamp": "t",
        }
    ).encode()
    r = client.post(
        "/webhook/events",
        content=raw,
        headers={
            "X-Event-Id": "e6",
            "X-Signature": compute_signature(secret, "e6", raw),
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "unknown_event_type"
    assert "merge.teleported" in body["detail"]


def test_header_body_event_id_mismatch_rejected_400(events, client, secret):
    # 用头里的 id 签名也没用：载荷内 event_id 必须与头一致
    body = events.body("e7", "release.created", "r1", 0)
    r = client.post(
        "/webhook/events",
        content=body,
        headers={
            "X-Event-Id": "e7-forged",
            "X-Signature": compute_signature(secret, "e7-forged", body),
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_payload"


def test_duplicate_delivery_via_http_is_idempotent(events, client):
    events.send("dup1", "release.created", "r1", 0)
    r2 = events.send("dup1", "release.created", "r1", 0)
    r3 = events.send("dup1", "release.created", "r1", 0)
    assert r2.json()["decision"] == "duplicate"
    assert r3.json()["decision"] == "duplicate"
    state = client.get("/releases/r1").json()
    applied = [
        h for h in state["history"] if h["decision"] == "applied"
    ]
    assert len(applied) == 1


def test_out_of_order_stale_event_does_not_change_final_state(events, client):
    events.send("c", "release.created", "r1", 0)
    events.send("bs", "build.started", "r1", 1)
    events.send("bok", "build.succeeded", "r1", 2, reason="ok")
    final = events.send("ds", "deploy.succeeded", "r1", 3)
    assert final.json()["state"] == "succeeded"

    # 旧的失败事件迟到：必须被拒绝推进，终态保持 succeeded
    stale = events.send("late", "build.failed", "r1", 2, reason="late boom")
    assert stale.json()["decision"] == "stale"
    state = client.get("/releases/r1").json()
    assert state["state"] == "succeeded"
    assert state["terminal"] is True


def test_concurrent_duplicate_posters_advance_once(events, client):
    from concurrent.futures import ThreadPoolExecutor

    events.send("c", "release.created", "r1", 0)

    def post(_):
        return events.send("bs", "build.started", "r1", 1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(post, range(12)))
    decisions = [r.json()["decision"] for r in responses]
    assert decisions.count("applied") == 1
    assert decisions.count("duplicate") == 11
    assert client.get("/releases/r1").json()["state"] == "building"


def test_build_failure_records_distinct_reason(events, client, observed):
    events.send("c", "release.created", "r1", 0)
    events.send("bs", "build.started", "r1", 1)
    failed = events.send("bf", "build.failed", "r1", 2, reason="oom-killed")
    assert failed.json()["state"] == "build_failed"
    detail = client.get("/releases/r1").json()
    assert "oom-killed" in detail["failure_reason"]
    assert detail["attempts"][0]["reason"] == "oom-killed"
    # 失败原因必须能与签名错误区分开
    bad_sig = events.send(
        "x", "build.started", "r1", 3, signature="nope"
    )
    assert bad_sig.status_code == 401


def test_retry_rejection_is_explained(events, client):
    from release_pipeline.state import MAX_BUILD_RETRIES

    events.send("c", "release.created", "r1", 0)
    seq = 1
    for i in range(MAX_BUILD_RETRIES + 1):
        events.send(f"bs{i}", "build.started", "r1", seq); seq += 1
        events.send(f"bf{i}", "build.failed", "r1", seq, reason="flaky"); seq += 1
        if i < MAX_BUILD_RETRIES:
            r = events.send(f"rt{i}", "build.retry_requested", "r1", seq)
            assert r.json()["state"] == "pending"; seq += 1
    rejected = events.send("rtx", "build.retry_requested", "r1", seq)
    assert rejected.json()["decision"] == "retry_rejected"
    assert "max build retries" in rejected.json()["reason"]
