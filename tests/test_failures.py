"""失败终态、重试上限与留痕、取消/超时、回滚规则。"""

from __future__ import annotations

from tests.conftest import make_event


def test_build_failure_lands_in_explicit_terminal_state_with_reason(client, send):
    send(client, make_event("build.started", 1))
    resp = send(
        client,
        make_event("build.failed", 2, reason="单元测试编译失败"),
    )
    assert resp.status_code == 200

    detail = client.get("/releases/rel-1").json()
    assert detail["state"] == "build_failed"
    assert detail["failure_reason"] == "单元测试编译失败"
    assert detail["attempts"] == 1


def test_retry_after_failure_resumes_build_then_succeeds(client, send):
    send(client, make_event("build.started", 1))
    send(client, make_event("build.failed", 2, reason="oom"))
    retry = send(client, make_event("retry.requested", 3))
    assert retry.status_code == 200
    assert retry.json()["state"] == "building"

    send(client, make_event("build.succeeded", 4))
    send(client, make_event("deploy.started", 5))
    send(client, make_event("deploy.succeeded", 6))
    assert client.get("/releases/rel-1").json()["state"] == "succeeded"


def test_retry_is_capped_and_process_record_is_preserved(client, send, event_logs):
    # 首次构建失败 + 重试；retry.requested 本身把状态带回 building，
    # 因此失败事件可连续到达。第 3 次重试请求必须被拒绝。
    send(client, make_event("build.started", 1))
    seq = 2
    for i in range(3):
        send(client, make_event("build.failed", seq, reason=f"fail-{i}"))
        seq += 1
        retry = send(client, make_event("retry.requested", seq))
        seq += 1
        if i < 2:
            assert retry.status_code == 200
        else:
            # attempts 已达上限（3），重试被拒绝
            assert retry.status_code == 409
            assert "上限" in retry.json()["error"]["message"]

    detail = client.get("/releases/rel-1").json()
    assert detail["state"] == "build_failed"
    assert detail["attempts"] == 3
    # 过程记录完整：3 次失败原因都在历史里
    failed_records = [h for h in detail["history"] if h["event_type"] == "build.failed"]
    assert len(failed_records) == 3
    rejected_retries = [
        h for h in detail["history"] if h["event_type"] == "retry.requested" and not h["accepted"]
    ]
    assert len(rejected_retries) == 1
    assert any("上限" in r.message for r in event_logs.records)


def test_cancellation_from_active_state_is_terminal(client, send):
    send(client, make_event("build.started", 1))
    resp = send(client, make_event("release.cancelled", 2, reason="人工中止"))
    assert resp.status_code == 200

    detail = client.get("/releases/rel-1").json()
    assert detail["state"] == "cancelled"
    assert detail["failure_reason"] == "人工中止"

    # 终态后到达的任何事件都不能再改变状态
    late = send(client, make_event("build.succeeded", 3, event_id="late-1"))
    assert late.status_code == 409
    assert client.get("/releases/rel-1").json()["state"] == "cancelled"


def test_timeout_lands_in_timed_out_terminal_state(client, send):
    send(client, make_event("deploy.started", 1))
    resp = send(client, make_event("release.timed_out", 2))
    assert resp.status_code == 200
    detail = client.get("/releases/rel-1").json()
    assert detail["state"] == "timed_out"
    assert "超时" in detail["failure_reason"]


def test_rollback_only_allowed_after_success(client, send):
    # 未成功时请求回滚 -> 拒绝，规则可解释
    send(client, make_event("build.started", 1))
    early = send(client, make_event("rollback.requested", 2))
    assert early.status_code == 409
    assert "不允许" in early.json()["error"]["message"]

    # 走完发布成功后再回滚 -> ROLLED_BACK
    send(client, make_event("build.succeeded", 3))
    send(client, make_event("deploy.started", 4))
    send(client, make_event("deploy.succeeded", 5))
    ok = send(client, make_event("rollback.requested", 6))
    assert ok.status_code == 200
    assert ok.json()["state"] == "rolled_back"

    # 回滚后不可再回滚，也不可继续发布
    again = send(client, make_event("rollback.requested", 7))
    assert again.status_code == 409
    assert client.get("/releases/rel-1").json()["state"] == "rolled_back"


def test_illegal_transition_is_rejected_without_state_change(client, send):
    # 还没 build.started 就直接 build.succeeded
    resp = send(client, make_event("build.succeeded", 1))
    assert resp.status_code == 409
    assert "非法流转" in resp.json()["error"]["message"]
    detail = client.get("/releases/rel-1").json()
    assert detail["state"] == "pending"
