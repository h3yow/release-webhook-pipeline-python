"""发布状态的线程安全存储。

使用每 release 一把锁：同一发布的事件（包括平台重复投递、并发到达）
在同一临界区里完成"取聚合 -> 判定 -> 推进"，保证不会重复推进状态；
不同发布之间互不阻塞。
"""

from __future__ import annotations

import threading

from release_pipeline.models import Release


class ReleaseStore:
    """内存存储：按 release_id 聚合。"""

    def __init__(self) -> None:
        self._releases: dict[str, Release] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def _lock_for(self, release_id: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._locks.get(release_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[release_id] = lock
            return lock

    def get_or_create(self, release_id: str, version: str) -> Release:
        """获取或创建发布聚合（创建本身也要加锁，防止并发重复创建）。"""
        with self._registry_lock:
            release = self._releases.get(release_id)
            if release is None:
                release = Release(release_id=release_id, version=version)
                self._releases[release_id] = release
            return release

    def lock(self, release_id: str) -> threading.Lock:
        """返回某个发布对应的串行化锁。"""
        return self._lock_for(release_id)

    def all_releases(self) -> list[Release]:
        """返回当前所有发布（调试/测试用）。"""
        with self._registry_lock:
            return list(self._releases.values())
