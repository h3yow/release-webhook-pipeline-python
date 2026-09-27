"""FastAPI 应用组装。"""

from __future__ import annotations

from fastapi import FastAPI

from .config import Settings
from .service import ReleasePipelineService
from .webhook.handler import WebhookHandler
from .webhook.routes import create_router, register_error_handlers


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = ReleasePipelineService()
    handler = WebhookHandler(service, settings.webhook_secret)

    app = FastAPI(
        title="Release Webhook Pipeline",
        version="0.1.0",
        description="Webhook 驱动的发布流水线状态服务",
    )
    app.include_router(create_router(handler, service))
    register_error_handlers(app)

    # 暴露给测试与本地脚本，便于直接检查状态
    app.state.service = service
    app.state.handler = handler
    return app
