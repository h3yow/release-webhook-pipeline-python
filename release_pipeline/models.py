"""发布聚合根与处理记录。"""

from __future__ import annotations

from dataclasses import dataclass, field

from release_pipeline.states import ReleaseState


@dataclass
class TransitionRecord:
    """一次事件处理的留痕记录。"""

    event_id: str
    event_type: str
    seq: int
    from_state: str
    to_state: str
    accepted: bool
    note: str = ""


@dataclass
class Release:
    """一次版本发布的完整状态。"""

    release_id: str
    version: str
    state: ReleaseState = ReleaseState.PENDING
    last_seq: int = 0
    attempts: int = 0
    failure_reason: str | None = None
    history: list[TransitionRecord] = field(default_factory=list)
    processed_event_ids: set[str] = field(default_factory=set)
