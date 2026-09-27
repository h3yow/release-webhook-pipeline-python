"""事件处理核心：校验、去重、顺序控制与状态推进。

判定顺序（同一 release 的锁内执行）：

1. ``event_id`` 已处理过          -> 重复投递，幂等忽略（accepted=True, progressed=False）
2. ``seq < last_seq``             -> 乱序旧事件，拒绝（StaleEventError）
3. ``seq == last_seq`` 但 ID 不同 -> 同一序号的另一个事件，拒绝（StaleEventError）
4. 发布已处于终态                 -> 拒绝（StaleEventError，消息注明终态）
5. 取消 / 超时 / 重试等旁路事件   -> 走各自规则
6. 普通事件查合法流转表           -> 命中则推进，否则拒绝（StaleEventError，注明非法流转）

无论接受还是拒绝，都会写一条 TransitionRecord 并打印日志，
保证"收到的事件 + 判定结果"事后可查、可复现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from release_pipeline.events import Event, EventType
from release_pipeline.models import Release, TransitionRecord
from release_pipeline.states import (
    ACTIVE_STATES,
    MAX_RETRIES,
    RETRY_TRANSITIONS,
    TRANSITIONS,
    ReleaseState,
    is_terminal,
)
from release_pipeline.store import ReleaseStore

logger = logging.getLogger("release_pipeline")


@dataclass(frozen=True)
class ProcessResult:
    """一次事件处理的判定结果。"""

    accepted: bool
    progressed: bool
    state: ReleaseState
    decision: str
    note: str = ""
    from_state: ReleaseState | None = None


class EventProcessor:
    """把一个已验证事件应用到对应的 Release 聚合上。"""

    def __init__(self, store: ReleaseStore) -> None:
        self._store = store

    def process(self, release: Release, event: Event) -> ProcessResult:
        """在调用方已持有 release 锁的前提下处理事件。"""
        # 1) 基于 event_id 的幂等去重
        if event.event_id in release.processed_event_ids:
            result = ProcessResult(
                accepted=True,
                progressed=False,
                state=release.state,
                decision="duplicate",
                note="事件此前已处理过，幂等忽略",
                from_state=release.state,
            )
            self._record(release, event, result)
            return result

        # 2/3) 基于 seq 的乱序防护
        if event.seq < release.last_seq:
            raise self._stale(
                release,
                event,
                f"乱序事件 seq={event.seq} 落后于已处理序号 last_seq={release.last_seq}",
            )
        if event.seq == release.last_seq and release.last_seq > 0:
            raise self._stale(
                release,
                event,
                f"seq={event.seq} 已被另一个事件占用，疑似重复/冲突投递",
            )

        # 4) 终态防护：已经结束的发布不再接受任何事件
        if is_terminal(release.state):
            raise self._stale(
                release,
                event,
                f"发布已处于终态 {release.state.value}，拒绝任何后续事件",
            )

        # 5) 旁路事件
        if event.event_type is EventType.RELEASE_CANCELLED:
            return self._cancel_or_timeout(release, event, ReleaseState.CANCELLED)
        if event.event_type is EventType.RELEASE_TIMED_OUT:
            return self._cancel_or_timeout(release, event, ReleaseState.TIMED_OUT)
        if event.event_type is EventType.RETRY_REQUESTED:
            return self._retry(release, event)
        if event.event_type is EventType.ROLLBACK_REQUESTED:
            return self._rollback(release, event)

        # 6) 普通流转
        table = TRANSITIONS.get(event.event_type, {})
        target = table.get(release.state)
        if target is None:
            raise self._stale(
                release,
                event,
                f"非法流转：事件 {event.event_type.value} 不能作用于状态 {release.state.value}",
            )

        from_state = release.state
        progressed = target != from_state
        release.state = target
        if event.event_type in (EventType.BUILD_FAILED, EventType.DEPLOY_FAILED):
            release.attempts += 1
            release.failure_reason = event.reason or event.event_type.value
        if progressed:
            release.last_seq = event.seq
        release.processed_event_ids.add(event.event_id)

        result = ProcessResult(
            accepted=True,
            progressed=progressed,
            state=release.state,
            decision="advanced" if progressed else "recorded",
            note=f"{from_state.value} -> {target.value}",
            from_state=from_state,
        )
        self._record(release, event, result)
        return result

    # ---- 旁路事件规则 -------------------------------------------------

    def _cancel_or_timeout(
        self, release: Release, event: Event, target: ReleaseState
    ) -> ProcessResult:
        if release.state not in ACTIVE_STATES:
            raise self._stale(
                release,
                event,
                f"取消/超时事件不能作用于状态 {release.state.value}",
            )
        from_state = release.state
        release.state = target
        release.last_seq = event.seq
        release.processed_event_ids.add(event.event_id)
        if event.event_type is EventType.RELEASE_CANCELLED:
            release.failure_reason = event.reason or "被人工取消"
        else:
            release.failure_reason = event.reason or "发布超时"
        result = ProcessResult(
            accepted=True,
            progressed=True,
            state=release.state,
            decision="advanced",
            note=f"{from_state.value} -> {target.value}",
            from_state=from_state,
        )
        self._record(release, event, result)
        return result

    def _retry(self, release: Release, event: Event) -> ProcessResult:
        target = RETRY_TRANSITIONS.get(release.state)
        if target is None:
            raise self._stale(
                release,
                event,
                f"当前状态 {release.state.value} 不允许重试（仅 build_failed/deploy_failed 可重试）",
            )
        if release.attempts >= MAX_RETRIES:
            raise self._stale(
                release,
                event,
                f"重试次数已达上限 {MAX_RETRIES}（已失败 {release.attempts} 次），"
                "发布永久停留在失败终态",
            )
        from_state = release.state
        release.state = target
        release.last_seq = event.seq
        release.processed_event_ids.add(event.event_id)
        result = ProcessResult(
            accepted=True,
            progressed=True,
            state=release.state,
            decision="advanced",
            note=f"{from_state.value} -> {target.value}（第 {release.attempts} 次失败后的重试）",
            from_state=from_state,
        )
        self._record(release, event, result)
        return result

    def _rollback(self, release: Release, event: Event) -> ProcessResult:
        """回滚规则：只有发布成功（succeeded）后才允许回滚。"""
        if release.state is not ReleaseState.SUCCEEDED:
            raise self._stale(
                release,
                event,
                "回滚不允许：只有 succeeded 状态的发布才能回滚，"
                f"当前状态为 {release.state.value}",
            )
        from_state = release.state
        release.state = ReleaseState.ROLLED_BACK
        release.last_seq = event.seq
        release.processed_event_ids.add(event.event_id)
        result = ProcessResult(
            accepted=True,
            progressed=True,
            state=release.state,
            decision="advanced",
            note=f"{from_state.value} -> rolled_back",
            from_state=from_state,
        )
        self._record(release, event, result)
        return result

    # ---- 留痕 ---------------------------------------------------------

    def _stale(self, release: Release, event: Event, note: str):
        """构造乱序/拒绝结果并留痕。被拒绝事件不推进 last_seq。"""
        from release_pipeline.errors import StaleEventError

        result = ProcessResult(
            accepted=False,
            progressed=False,
            state=release.state,
            decision="rejected",
            note=note,
            from_state=release.state,
        )
        self._record(release, event, result)
        return StaleEventError(note)

    def _record(
        self, release: Release, event: Event, result: ProcessResult
    ) -> None:
        from_state = result.from_state or release.state
        release.history.append(
            TransitionRecord(
                event_id=event.event_id,
                event_type=event.event_type.value,
                seq=event.seq,
                from_state=from_state.value,
                to_state=result.state.value,
                accepted=result.accepted,
                note=result.note,
            )
        )
        logger.info(
            "事件判定 release=%s event_id=%s type=%s seq=%s state=%s -> 判定=%s 说明=%s",
            release.release_id,
            event.event_id,
            event.event_type.value,
            event.seq,
            release.state.value,
            result.decision,
            result.note,
        )
