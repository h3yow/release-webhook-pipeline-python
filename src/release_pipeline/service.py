"""发布流水线状态机。

处理规则（详见 docs/design.md）：

* 同一 ``event_id`` 重复投递 -> 幂等忽略（duplicate），状态不重复推进；
* ``sequence`` 小于等于已处理的最大序号 -> 旧事件（stale），忽略，
  已前进的状态不会被回退；
* ``sequence`` 超前（中间有缺口）-> 缓冲（buffered），缺口补齐后按序回放；
* 终态下只接受回滚类事件，其余推进一律忽略（ignored）；
* 构建失败允许至多 :data:`MAX_BUILD_RETRIES` 次重试，超限拒绝并解释原因；
* 每次判定都写入 history，保证过程可复现、原因可解释。

同一次发布的事件通过存储中"每发布一把锁"串行化，因此多个事件几乎
同时到达时去重与顺序判定仍然成立。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .events import ReleaseEvent
from .state import MAX_BUILD_RETRIES, ReleaseState
from .store import (
    AttemptRecord,
    InMemoryReleaseStore,
    ReleaseRecord,
    ReleaseStore,
)

logger = logging.getLogger("release_pipeline.service")


@dataclass(frozen=True, slots=True)
class HandleResult:
    """单个事件的判定结果（测试日志与接口响应共用）。"""

    accepted: bool
    release_id: str
    state: ReleaseState
    decision: str  # applied|duplicate|stale|ignored|buffered|retry_rejected
    reason: str | None = None


# 各事件允许的来源状态 -> 目标状态。不在表中的 (状态, 事件) 组合即非法推进。
_TRANSITIONS: dict[str, dict[ReleaseState, ReleaseState]] = {
    "release.created": {ReleaseState.PENDING: ReleaseState.PENDING},
    "build.started": {
        ReleaseState.PENDING: ReleaseState.BUILDING,
    },
    "build.succeeded": {ReleaseState.BUILDING: ReleaseState.DEPLOYING},
    "deploy.started": {ReleaseState.DEPLOYING: ReleaseState.DEPLOYING},
    "deploy.succeeded": {ReleaseState.DEPLOYING: ReleaseState.SUCCEEDED},
    "release.canceled": {
        ReleaseState.PENDING: ReleaseState.CANCELED,
        ReleaseState.BUILDING: ReleaseState.CANCELED,
        ReleaseState.DEPLOYING: ReleaseState.CANCELED,
    },
    "release.timed_out": {
        ReleaseState.PENDING: ReleaseState.TIMED_OUT,
        ReleaseState.BUILDING: ReleaseState.TIMED_OUT,
        ReleaseState.DEPLOYING: ReleaseState.TIMED_OUT,
    },
    "rollback.requested": {ReleaseState.SUCCEEDED: ReleaseState.ROLLING_BACK},
    "rollback.succeeded": {ReleaseState.ROLLING_BACK: ReleaseState.ROLLED_BACK},
    "rollback.failed": {ReleaseState.ROLLING_BACK: ReleaseState.SUCCEEDED},
}

# build.failed / build.retry_requested 需要单独处理计数，不进通用转换表
_BUILD_FAILED_EVENT = "build.failed"
_BUILD_RETRY_EVENT = "build.retry_requested"


class ReleasePipelineService:
    """持有存储、对单个事件做幂等/顺序判定并推进状态。"""

    def __init__(self, store: ReleaseStore | None = None) -> None:
        self.store: ReleaseStore = store or InMemoryReleaseStore()

    def handle(self, event: ReleaseEvent) -> HandleResult:
        """处理一个已通过签名与 schema 校验的事件。"""
        lock = self.store.lock_for(event.release_id)
        with lock:
            record = self.store.get_or_create(event.release_id)
            return self._handle_locked(record, event)

    # ---- 判定顺序：去重 -> 旧事件 -> 未来事件缓冲 -> 按序回放 ----
    def _handle_locked(
        self, record: ReleaseRecord, event: ReleaseEvent
    ) -> HandleResult:
        if event.event_id in record.processed_event_ids:
            result = HandleResult(
                accepted=True,
                release_id=record.release_id,
                state=record.state,
                decision="duplicate",
                reason=f"event_id {event.event_id!r} already processed",
            )
            self._log_and_record(record, event, result, state_changed=False)
            return result

        if event.sequence <= record.last_sequence:
            # 旧事件：只忽略、不改状态，也不登记已处理。
            # 它的重投依然稳定落在 stale 分支，天然幂等。
            result = self._ignore(
                record,
                event,
                "stale",
                f"event sequence {event.sequence} <= last applied "
                f"{record.last_sequence}; state stays {record.state.value}",
            )
            self._log_and_record(record, event, result, state_changed=False)
            return result

        # 未来事件先缓冲；同一序号只接受第一个（其余按重复事件处理）
        existing = record.pending_by_sequence.get(event.sequence)
        if existing is not None:
            result = self._ignore(
                record,
                event,
                "duplicate",
                f"sequence {event.sequence} already queued as event "
                f"{existing.event_id!r}",
            )
            self._log_and_record(record, event, result, state_changed=False)
            return result

        record.pending_by_sequence[event.sequence] = event

        # 从水位之后开始，连续无缺口地回放
        final_result: HandleResult | None = None
        next_seq = record.last_sequence + 1
        while True:
            queued = record.pending_by_sequence.pop(next_seq, None)
            if queued is None:
                break
            result = self._transition(record, queued)
            record.processed_event_ids.add(queued.event_id)
            record.last_sequence = queued.sequence
            self._log_and_record(
                record,
                queued,
                result,
                state_changed=result.decision == "applied",
            )
            final_result = result
            next_seq += 1

        if final_result is None:
            # 本事件被缓冲，等待更早序号的事件到达
            result = self._ignore(
                record,
                event,
                "buffered",
                f"event sequence {event.sequence} buffered; waiting for "
                f"sequence {record.last_sequence + 1}",
            )
            self._log_and_record(record, event, result, state_changed=False)
            return result

        return final_result

    def _transition(
        self, record: ReleaseRecord, event: ReleaseEvent
    ) -> HandleResult:
        if event.event_type == _BUILD_FAILED_EVENT:
            return self._handle_build_failed(record, event)
        if event.event_type == _BUILD_RETRY_EVENT:
            return self._handle_build_retry(record, event)

        targets = _TRANSITIONS.get(event.event_type, {})
        new_state = targets.get(record.state)
        if new_state is None:
            return self._ignore(
                record,
                event,
                "ignored",
                f"event {event.event_type!r} not allowed in state "
                f"{record.state.value}",
            )

        if event.event_type == "build.succeeded":
            record.attempts.append(
                AttemptRecord(
                    attempt=record.build_attempts + 1,
                    result="succeeded",
                    reason=event.reason,
                    timestamp=event.timestamp,
                )
            )

        reason = None
        if event.event_type == "release.canceled":
            reason = event.reason or "canceled by user"
        elif event.event_type == "release.timed_out":
            reason = event.reason or "pipeline timed out"
        return self._apply(record, event, new_state, reason=reason)

    def _handle_build_failed(
        self, record: ReleaseRecord, event: ReleaseEvent
    ) -> HandleResult:
        """构建失败：记录本次尝试，落 BUILD_FAILED；重试由显式事件驱动。"""
        if record.state is not ReleaseState.BUILDING:
            return self._ignore(
                record,
                event,
                "ignored",
                f"build.failed not allowed in state {record.state.value}",
            )

        failure_reason = event.reason or "build failed without reason"
        record.failure_reason = (
            f"attempt {record.build_attempts + 1} failed: {failure_reason}"
        )
        record.attempts.append(
            AttemptRecord(
                attempt=record.build_attempts + 1,
                result="failed",
                reason=failure_reason,
                timestamp=event.timestamp,
            )
        )
        return self._apply(
            record,
            event,
            ReleaseState.BUILD_FAILED,
            reason=(
                f"{record.failure_reason} "
                f"(retries used: {record.build_attempts}/{MAX_BUILD_RETRIES})"
            ),
        )

    def _handle_build_retry(
        self, record: ReleaseRecord, event: ReleaseEvent
    ) -> HandleResult:
        """仅 BUILD_FAILED 可重试；超过上限则保持终态并给出可解释原因。"""
        if record.state is not ReleaseState.BUILD_FAILED:
            return self._ignore(
                record,
                event,
                "ignored",
                f"build.retry_requested not allowed in state "
                f"{record.state.value}",
            )

        if record.build_attempts >= MAX_BUILD_RETRIES:
            return HandleResult(
                accepted=True,
                release_id=record.release_id,
                state=record.state,
                decision="retry_rejected",
                reason=(
                    f"max build retries {MAX_BUILD_RETRIES} exhausted "
                    f"(attempts so far: {record.build_attempts + 1}); "
                    f"last failure: {record.failure_reason}"
                ),
            )

        record.build_attempts += 1
        record.failure_reason = None
        return self._apply(
            record,
            event,
            ReleaseState.PENDING,
            reason=(
                f"retry {record.build_attempts}/{MAX_BUILD_RETRIES} accepted; "
                f"awaiting build.started"
            ),
        )

    def _apply(
        self,
        record: ReleaseRecord,
        event: ReleaseEvent,
        new_state: ReleaseState,
        decision: str = "applied",
        reason: str | None = None,
    ) -> HandleResult:
        previous = record.state
        # 自循环事件（如 release.created / deploy.started）不改版本号
        if new_state != previous:
            record.state = new_state
            record.version += 1
        if new_state in (ReleaseState.ROLLING_BACK, ReleaseState.ROLLED_BACK):
            record.rollback_reason = event.reason or record.rollback_reason
        return HandleResult(
            accepted=True,
            release_id=record.release_id,
            state=record.state,
            decision=decision,
            reason=reason or f"{previous.value} -> {new_state.value}",
        )

    def _ignore(
        self,
        record: ReleaseRecord,
        event: ReleaseEvent,
        decision: str,
        reason: str,
    ) -> HandleResult:
        return HandleResult(
            accepted=True,
            release_id=record.release_id,
            state=record.state,
            decision=decision,
            reason=reason,
        )

    def _log_and_record(
        self,
        record: ReleaseRecord,
        event: ReleaseEvent,
        result: HandleResult,
        state_changed: bool,
    ) -> None:
        """写入过程记录并打印"收到事件 -> 判定结果"日志。"""
        record.history.append(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "sequence": event.sequence,
                "timestamp": event.timestamp,
                "decision": result.decision,
                "state_after": result.state.value,
                "reason": result.reason,
            }
        )
        logger.info(
            "event received: id=%s type=%s release=%s seq=%s -> decision=%s "
            "state=%s changed=%s reason=%s",
            event.event_id,
            event.event_type,
            event.release_id,
            event.sequence,
            result.decision,
            result.state.value,
            state_changed,
            result.reason,
        )
