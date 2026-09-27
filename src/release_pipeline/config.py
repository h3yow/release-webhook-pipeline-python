"""服务运行配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    webhook_secret: str = "dev-secret"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(webhook_secret=os.environ.get("WEBHOOK_SECRET", "dev-secret"))
