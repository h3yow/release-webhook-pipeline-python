"""正常流转、重复投递、乱序事件、并发到达的一致性测试。"""

from __future__ import annotations

import concurrent.futures

from tests.conftest import make_event

HAPPY_PATH = [
    ("push", 1),
    ("build.started", 2),
    ("build.succeeded", 3),
    ("deploy.started", 4),
    ("deploy.succeeded", 5),
]


def _drive(client, send, events, release_id="rel-1"):
    return [
        send(client, make_event(t, s, release_id=release_id)).status_code
        for t, s in events
    ]


def test_happy_path_reaches_succeeded(client, send, event_logs):
    statuses = _drive(client, send, HAPPY_PATH)
    assert statuses == [200] * 5

    detail = client.get("/releases/rel-1").json()
    assert detail["state"] == "succeeded"
    assert detail["last_seq"] == 5
    # 每个事件都留痕，且判定结果可在日志中看到
    assert len(detail["history"]) == 5
    assert any("判定=advanced" in r.message for r in event_logs.records)
    assert any("收到事件" in r.message for r in event_logs.records)


def test_duplicate_delivery_is_idempotent(client, send):
    first = send(client, make_event("build.started", 2, event_id="e-2"))
    duplicate = send(client, make_event("build.started", 2, event_id="e-2"))

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert first.json()["progressed"] is True
    assert duplicate.json()["decision"] == "duplicate"
    assert duplicate.json()["progressed"] is False

    detail = client.get("/releases/rel-1").json()
    assert detail["state"] == "building"
    # 重复投递也留痕，但状态只推进一次
    notes = [h["note"] for h in detail["history"]]
    assert any("幂等" in n for n in notes)
    assert detail["last_seq"] == 2


def test_out_of_order_old_event_cannot_roll_back_state(client, send, event_logs):
    _drive(client, send, [("push", 1), ("build.started", 2), ("build.succeeded", 3)])
    assert client.get("/releases/rel-1").json()["state"] == "build_succeeded"

    # 旧事件（seq=2 的另一个 build.started）晚到，不能把状态打回 building
    stale = send(
        client, make_event("build.started", 2, event_id="late-old-event")
    )
    assert stale.status_code == 409
    body = stale.json()["error"]
    assert body["code"] == "stale_event"
    assert "seq=2" in body["message"]

    detail = client.get("/releases/rel-1").json()
    assert detail["state"] == "build_succeeded"
    # 被拒绝事件同样出现在日志里，判定可复现
    assert any("状态机拒绝" in r.message for r in event_logs.records)


def test_event_with_lower_seq_than_current_is_rejected(client, send):
    _drive(client, send, [("build.started", 5), ("build.succeeded", 6)])
    resp = send(client, make_event("push", 4, event_id="old-push"))
    assert resp.status_code == 409
    assert "乱序" in resp.json()["error"]["message"]
    assert client.get("/releases/rel-1").json()["state"] == "build_succeeded"


def test_concurrent_duplicate_events_advance_state_once(client, send):
    event = make_event("build.started", 1, event_id="same-event-id")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(send, client, event) for _ in range(8)]
        results = [f.result() for f in futures]

    codes = sorted(r.status_code for r in results)
    assert codes == [200] * 8
    progressed = [r.json()["progressed"] for r in results]
    assert progressed.count(True) == 1

    detail = client.get("/releases/rel-1").json()
    assert detail["state"] == "building"
    assert detail["last_seq"] == 1
