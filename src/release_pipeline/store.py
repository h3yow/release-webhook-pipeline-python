"""发布状态的持久化抽象与内存实现。

生产环境应替换为带事务的持久实现；内存实现带"按发布加锁 +
已处理事件去重"，足够说明并发、重投、乱序下的处理规则。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .state import ReleaseState


@dataclass(slots=True)
class AttemptRecord:
    """一次构建尝试（首次或某次重试）的过程记录。"""

    attempt: int
    result: str  # "failed" / "succeeded"
    reason: str | None
    timestamp: str


@dataclass(slots=True)
class ReleaseRecord:
    """一次发布的完整状态与过程记录。"""

    release_id: str
    state: ReleaseState = ReleaseState.PENDING
    last_sequence: int = -1
    version: int = 1
    build_attempts: int = 0
    attempts: list[AttemptRecord] = field(default_factory=list)
    processed_event_ids: set[str] = field(default_factory=set)
    # 乱序到达的未来事件：sequence -> 已去重的事件，等缺口补齐后按序回放
    pending_by_sequence: dict[int, object] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    failure_reason: str | None = None
    rollback_reason: str | None = None


class ReleaseStore:
    """存储接口（持久化实现需提供同样的事务语义）。"""

    def get(self, release_id: str) -> ReleaseRecord | None:
        raise NotImplementedError

    def get_or_create(self, release_id: str) -> ReleaseRecord:
        raise NotImplementedError


class InMemoryReleaseStore(ReleaseStore):
    """线程安全的内存存储。"""

    def __init__(self) -> None:
        self._records: dict[str, ReleaseRecord] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def lock_for(self, release_id: str) -> threading.Lock:
        """返回某次发布专用的锁，保证同发布事件串行处理。

        锁对象一旦创建便不会移除：保证任何时刻持有方与获取方拿到的是
        同一把锁；不同发布之间互不阻塞。
        """
        with self._global_lock:
            lock = self._locks.get(release_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[release_id] = lock
            return lock

    def get(self, release_id: str) -> ReleaseRecord | None:
        return self._records.get(release_id)

    def get_or_create(self, release_id: str) -> ReleaseRecord:
        """必须在持有该发布的锁时调用（保证创建与后续判定原子）。"""
        record = self._records.get(release_id)
        if record is None:
            record = ReleaseRecord(release_id=release_id)
            self._records[release_id] = record
        return record

    def all_records(self) -> list[ReleaseRecord]:
        return list(self._records.values())
