"""发布状态、事件类型与常量定义。"""

from __future__ import annotations

import enum


class ReleaseState(str, enum.Enum):
    """一次发布在其生命周期内可能处于的状态。

    终态（不会再被任何事件推进）：
    SUCCEEDED / BUILD_FAILED / CANCELED / TIMED_OUT / ROLLED_BACK
    """

    PENDING = "pending"            # 已创建/等待（重新）构建
    BUILDING = "building"          # 构建中
    DEPLOYING = "deploying"        # 构建成功，部署中
    SUCCEEDED = "succeeded"        # 发布成功（终态，可回滚）
    BUILD_FAILED = "build_failed"  # 构建失败（终态，可重试）
    CANCELED = "canceled"          # 被取消（终态）
    TIMED_OUT = "timed_out"        # 超时（终态）
    ROLLING_BACK = "rolling_back"  # 回滚执行中
    ROLLED_BACK = "rolled_back"    # 已回滚（终态）


TERMINAL_STATES = frozenset(
    {
        ReleaseState.SUCCEEDED,
        ReleaseState.BUILD_FAILED,
        ReleaseState.CANCELED,
        ReleaseState.TIMED_OUT,
        ReleaseState.ROLLED_BACK,
    }
)

# 构建失败后允许的最大重试次数（不含首次执行）
MAX_BUILD_RETRIES = 2

# 同一次发布的并发/乱序保护之外，平台重投的幂等键
DEDUP_EVENT_ID_HEADER = "X-Event-Id"
