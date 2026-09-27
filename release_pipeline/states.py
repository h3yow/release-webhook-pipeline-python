"""发布状态机：状态、终态与状态流转规则。

合法流转（不含重试/取消/超时这类旁路事件）::

    PENDING ──build.started──▶ BUILDING ──build.succeeded──▶ BUILD_SUCCEEDED
       ▲                          │                              │
       │(push 仅登记，不改状态)    │build.failed                  │deploy.started
       │                          ▼                              ▼
       │                       BUILD_FAILED ◀──retry.requested──┐  DEPLOYING
       │                          │                              │      │
    其它事件进入终态后一律拒绝     │ retry 达上限则永久停留        │      │deploy.succeeded
                                  └─retry.requested(计数+1)─▶BUILDING   ▼
                                                                    SUCCEEDED
    DEPLOY_FAILED ──retry.requested(计数+1)──▶ DEPLOYING              │
                                                                  rollback.requested
                                                                       ▼
                                                                  ROLLED_BACK

    任意非终态 ──release.cancelled──▶ CANCELLED
    任意非终态 ──release.timed_out──▶ TIMED_OUT
"""

from __future__ import annotations

from enum import Enum

from release_pipeline.events import EventType


class ReleaseState(str, Enum):
    """一次版本发布可能处于的状态。"""

    PENDING = "pending"
    BUILDING = "building"
    BUILD_SUCCEEDED = "build_succeeded"
    BUILD_FAILED = "build_failed"
    DEPLOYING = "deploying"
    DEPLOY_FAILED = "deploy_failed"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    ROLLED_BACK = "rolled_back"


#: 一旦进入就不可能再离开的状态。
#: 注意 SUCCEEDED 不在其中：发布成功后仍允许通过 rollback.requested 回滚；
#: 成功状态下收到其它事件会由流转表以"非法流转"拒绝。
#: 构建/部署失败也不算终态，失败后仍可在次数上限内重试。
TERMINAL_STATES: frozenset[ReleaseState] = frozenset(
    {
        ReleaseState.CANCELLED,
        ReleaseState.TIMED_OUT,
        ReleaseState.ROLLED_BACK,
    }
)

#: 失败状态回到对应"进行中"状态的重试流转，次数上限由 processor 判定
RETRY_TRANSITIONS: dict[ReleaseState, ReleaseState] = {
    ReleaseState.BUILD_FAILED: ReleaseState.BUILDING,
    ReleaseState.DEPLOY_FAILED: ReleaseState.DEPLOYING,
}

#: 可被取消 / 超时打断的"活跃"状态
ACTIVE_STATES: frozenset[ReleaseState] = frozenset(
    {
        ReleaseState.PENDING,
        ReleaseState.BUILDING,
        ReleaseState.BUILD_SUCCEEDED,
        ReleaseState.BUILD_FAILED,
        ReleaseState.DEPLOYING,
        ReleaseState.DEPLOY_FAILED,
    }
)

#: 普通事件的合法流转表：事件类型 -> {当前状态 -> 目标状态}
TRANSITIONS: dict[EventType, dict[ReleaseState, ReleaseState]] = {
    # push 只负责登记一次发布，不改变状态；幂等接受
    EventType.PUSH: {ReleaseState.PENDING: ReleaseState.PENDING},
    EventType.BUILD_STARTED: {
        ReleaseState.PENDING: ReleaseState.BUILDING,
    },
    EventType.BUILD_SUCCEEDED: {
        ReleaseState.BUILDING: ReleaseState.BUILD_SUCCEEDED,
    },
    EventType.BUILD_FAILED: {
        ReleaseState.BUILDING: ReleaseState.BUILD_FAILED,
    },
    EventType.DEPLOY_STARTED: {
        ReleaseState.BUILD_SUCCEEDED: ReleaseState.DEPLOYING,
    },
    EventType.DEPLOY_SUCCEEDED: {
        ReleaseState.DEPLOYING: ReleaseState.SUCCEEDED,
    },
    EventType.DEPLOY_FAILED: {
        ReleaseState.DEPLOYING: ReleaseState.DEPLOY_FAILED,
    },
}
# 注：rollback.requested 只允许发生在发布成功之后，规则见 processor._rollback

#: 最多重试次数（不含首次执行）；达到后失败状态即成为永久终态
MAX_RETRIES: int = 3


def is_terminal(state: ReleaseState) -> bool:
    """状态是否为不可再流转的终态。"""
    return state in TERMINAL_STATES
