"""状态机：重复投递 / 乱序 / 终态 / 重试上限 / 回滚 / 并发。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from release_pipeline.events import ReleaseEvent
from release_pipeline.service import ReleasePipelineService
from release_pipeline.state import MAX_BUILD_RETRIES, ReleaseState


def make_event(
    event_id: str,
    event_type: str,
    sequence: int,
    release_id: str = "r1",
    reason: str | None = None,
) -> ReleaseEvent:
    return ReleaseEvent(
        event_id=event_id,
        event_type=event_type,
        release_id=release_id,
        sequence=sequence,
        timestamp=f"2026-09-28T00:00:{sequence:02d}Z",
        reason=reason,
    )


def drive_happy_path(service: ReleasePipelineService, release_id: str = "r1"):
    steps = [
        ("c", "release.created", 0),
        ("bs", "build.started", 1),
        ("bok", "build.succeeded", 2),
        ("ds", "deploy.succeeded", 3),
    ]
    results = []
    for eid, et, seq in steps:
        results.append(
            service.handle(make_event(eid, et, seq, release_id=release_id))
        )
    return results


def test_happy_path_reaches_succeeded(service):
    results = drive_happy_path(service)
    assert [r.decision for r in results] == ["applied"] * 4
    assert results[-1].state is ReleaseState.SUCCEEDED
    record = service.store.get("r1")
    assert [a.result for a in record.attempts] == ["succeeded"]


def test_duplicate_delivery_is_idempotent(service, caplog):
    caplog.set_level(logging.INFO, logger="release_pipeline")
    drive_happy_path(service)
    state_before = service.store.get("r1").state
    version_before = service.store.get("r1").version

    # 同一 event_id + 同一内容重投三次
    dup = make_event("bs", "build.started", 1)
    decisions = [service.handle(dup).decision for _ in range(3)]
    assert decisions == ["duplicate"] * 3

    record = service.store.get("r1")
    assert record.state is state_before
    assert record.version == version_before
    # history 只记一次 applied 的 build.started
    applied_bs = [
        h for h in record.history
        if h["event_type"] == "build.started" and h["decision"] == "applied"
    ]
    assert len(applied_bs) == 1
    # 日志中能看到"收到事件"与 duplicate 判定
    assert any("event received" in m and "build.started" in m for m in caplog.messages)
    assert any("decision=duplicate" in m for m in caplog.messages)


def test_out_of_order_old_event_does_not_roll_back(service):
    drive_happy_path(service)  # 已到 succeeded
    # 乱序到达的旧事件：seq=2 的 build.failed 试图把状态拉回失败
    late_fail = make_event("late", "build.failed", 2, reason="late failure")
    result = service.handle(late_fail)
    assert result.decision == "stale"
    assert service.store.get("r1").state is ReleaseState.SUCCEEDED

    # 旧事件重投仍稳定判为 stale：不登记、不推进，状态不变（效果幂等）
    again = service.handle(late_fail)
    assert again.decision == "stale"
    assert service.store.get("r1").state is ReleaseState.SUCCEEDED


def test_future_events_buffered_until_gap_filled(service, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="release_pipeline")
    # 先到 seq=2，缺口未补：缓冲
    buffered = service.handle(
        make_event("bok", "build.succeeded", 2)
    )
    assert buffered.decision == "buffered"
    assert service.store.get("r1").state is ReleaseState.PENDING

    # seq=0 到达：seq 0 应用后回放仍缺 seq=1，因此 seq=2 继续等待
    service.handle(make_event("c", "release.created", 0))
    assert service.store.get("r1").state is ReleaseState.PENDING

    # seq=1 到达：1、2 连续回放，最终状态只按正确顺序推进一次
    result = service.handle(make_event("bs", "build.started", 1))
    assert result.state is ReleaseState.DEPLOYING
    record = service.store.get("r1")
    assert record.last_sequence == 2
    assert record.pending_by_sequence == {}
    assert any("decision=buffered" in m for m in caplog.messages)


def test_illegal_transition_in_active_state_is_ignored(service):
    service.handle(make_event("c", "release.created", 0))
    # 还没构建就发 build.succeeded
    result = service.handle(make_event("x", "build.succeeded", 1))
    assert result.decision == "ignored"
    assert result.state is ReleaseState.PENDING
    assert "not allowed in state" in result.reason


def test_build_failure_lands_in_build_failed_with_reason(service):
    service.handle(make_event("c", "release.created", 0))
    service.handle(make_event("bs", "build.started", 1))
    result = service.handle(
        make_event("bf", "build.failed", 2, reason="compile error")
    )
    assert result.state is ReleaseState.BUILD_FAILED
    record = service.store.get("r1")
    assert record.failure_reason == "attempt 1 failed: compile error"
    assert record.attempts[0].result == "failed"
    assert record.attempts[0].reason == "compile error"


def test_retries_have_cap_and_keep_process_record(service):
    seq = 0
    service.handle(make_event("c", "release.created", seq)); seq += 1
    for i in range(MAX_BUILD_RETRIES + 1):
        service.handle(make_event(f"bs{i}", "build.started", seq)); seq += 1
        failed = service.handle(
            make_event(f"bf{i}", "build.failed", seq, reason=f"error-{i}")
        ); seq += 1
        assert failed.state is ReleaseState.BUILD_FAILED
        if i < MAX_BUILD_RETRIES:
            retry = service.handle(
                make_event(f"rt{i}", "build.retry_requested", seq)
            ); seq += 1
            # 重试被接受的当场回到 PENDING，等待下一次 build.started
            assert retry.state is ReleaseState.PENDING
            assert retry.decision == "applied"

    # 第 3 次失败后，重试额度耗尽
    rejected = service.handle(
        make_event("rt-final", "build.retry_requested", seq)
    )
    assert rejected.decision == "retry_rejected"
    assert rejected.state is ReleaseState.BUILD_FAILED
    assert "max build retries 2 exhausted" in rejected.reason

    # build.started 不能绕过重试上限把终态拉回
    bypass = service.handle(make_event("bs-late", "build.started", seq + 1))
    assert bypass.decision == "ignored"
    assert bypass.state is ReleaseState.BUILD_FAILED

    record = service.store.get("r1")
    assert [a.attempt for a in record.attempts] == [1, 2, 3]
    assert [a.reason for a in record.attempts] == [
        "error-0", "error-1", "error-2"
    ]


def test_canceled_and_timed_out_are_terminal(service):
    service.handle(make_event("c", "release.created", 0, release_id="rc"))
    canceled = service.handle(
        make_event("x", "release.canceled", 1, release_id="rc", reason="manual")
    )
    assert canceled.state is ReleaseState.CANCELED
    after = service.handle(make_event("y", "build.started", 2, release_id="rc"))
    assert after.decision == "ignored"
    assert after.state is ReleaseState.CANCELED

    service.handle(make_event("c2", "release.created", 0, release_id="rt"))
    service.handle(make_event("bs2", "build.started", 1, release_id="rt"))
    timed = service.handle(
        make_event("to", "release.timed_out", 2, release_id="rt")
    )
    assert timed.state is ReleaseState.TIMED_OUT
    assert service.store.get("rt").state is ReleaseState.TIMED_OUT


def test_rollback_rules_are_explicit(service):
    drive_happy_path(service, release_id="rg")
    # 回滚失败 -> 回到 SUCCEEDED，可再次请求
    assert service.handle(
        make_event("rb1", "rollback.requested", 4, release_id="rg")
    ).state is ReleaseState.ROLLING_BACK
    failed = service.handle(
        make_event("rbf", "rollback.failed", 5, release_id="rg", reason="flaky")
    )
    assert failed.state is ReleaseState.SUCCEEDED
    assert service.handle(
        make_event("rb2", "rollback.requested", 6, release_id="rg")
    ).state is ReleaseState.ROLLING_BACK
    done = service.handle(
        make_event("rbd", "rollback.succeeded", 7, release_id="rg")
    )
    assert done.state is ReleaseState.ROLLED_BACK

    # 终态后不再接受任何推进
    assert service.handle(
        make_event("late", "rollback.requested", 8, release_id="rg")
    ).decision == "ignored"

    # 未成功的发布不允许回滚
    service.handle(make_event("c", "release.created", 0, release_id="rn"))
    rejected = service.handle(
        make_event("rx", "rollback.requested", 1, release_id="rn")
    )
    assert rejected.decision == "ignored"
    assert "not allowed in state" in rejected.reason


def test_concurrent_same_event_advances_once(service):
    event = make_event("bs", "build.started", 1)
    service.handle(make_event("c", "release.created", 0))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.handle(event), range(16)))
    decisions = sorted(r.decision for r in results)
    assert decisions.count("applied") == 1
    assert decisions.count("duplicate") == 15
    record = service.store.get("r1")
    assert record.state is ReleaseState.BUILDING
    assert record.version == 2  # 初始 1 + 仅一次推进


def test_concurrent_events_serialized_consistent_final_state(service):
    """同发布的乱序并发批次，最终状态只取决于最大序号的有效事件。"""
    events = [
        make_event("c", "release.created", 0),
        make_event("bs", "build.started", 1),
        make_event("bok", "build.succeeded", 2),
        make_event("ds", "deploy.succeeded", 3),
        make_event("late-bf", "build.failed", 1, reason="late"),
        make_event("late-bs", "build.started", 0),
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(service.handle, events))
    assert service.store.get("r1").state is ReleaseState.SUCCEEDED
