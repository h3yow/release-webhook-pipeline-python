"""FastAPI 入口：接收 webhook 并返回可区分的拒绝原因。

拒绝原因 -> HTTP 状态码::

    InvalidSignatureError   401 invalid_signature   签名缺失/格式错/不匹配
    InvalidPayloadError     400 invalid_payload     JSON 非法或缺必需字段
    UnknownEventTypeError   422 unknown_event_type  事件类型不认识
    StaleEventError         409 stale_event         乱序/非法流转/终态冲突/重试超限

重复投递（event_id 已处理）不视为错误：返回 200 且 ``decision="duplicate"``。
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from release_pipeline.errors import (
    InvalidPayloadError,
    InvalidSignatureError,
    StaleEventError,
    UnknownEventTypeError,
)
from release_pipeline.parser import parse_event
from release_pipeline.processor import EventProcessor
from release_pipeline.signature import verify_signature
from release_pipeline.store import ReleaseStore

logger = logging.getLogger("release_pipeline")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "test-secret")

_STATUS_CODES = {
    InvalidSignatureError: (401, "invalid_signature"),
    InvalidPayloadError: (400, "invalid_payload"),
    UnknownEventTypeError: (422, "unknown_event_type"),
    StaleEventError: (409, "stale_event"),
}


def create_app(secret: str | None = None) -> FastAPI:
    app = FastAPI(title="Release Webhook Pipeline")
    app.state.secret = secret or WEBHOOK_SECRET
    app.state.store = ReleaseStore()
    app.state.processor = EventProcessor(app.state.store)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    async def receive_webhook(request: Request) -> JSONResponse:
        raw_body = await request.body()
        signature = request.headers.get("x-signature")
        event_type_header = request.headers.get("x-event-type")

        # 第 1 关：签名
        try:
            verify_signature(app.state.secret, raw_body, signature)
        except (InvalidSignatureError,) as exc:
            logger.warning("收到事件但签名校验失败: %s", exc)
            return _error_response(exc)

        # 第 2 关：载荷 / 事件类型
        try:
            event = parse_event(raw_body, event_type_header)
        except (InvalidPayloadError, UnknownEventTypeError) as exc:
            logger.warning("收到事件但请求内容被拒绝: %s", exc)
            return _error_response(exc)

        logger.info(
            "收到事件 event_id=%s type=%s release=%s seq=%s",
            event.event_id,
            event.event_type.value,
            event.release_id,
            event.seq,
        )

        store: ReleaseStore = app.state.store
        processor: EventProcessor = app.state.processor
        release = store.get_or_create(event.release_id, event.version)

        # 第 3 关：同一发布串行化处理；乱序/非法流转等返回 409
        with store.lock(event.release_id):
            try:
                result = processor.process(release, event)
            except StaleEventError as exc:
                logger.warning("事件被状态机拒绝 event_id=%s: %s", event.event_id, exc)
                return _error_response(exc)

        return JSONResponse(
            status_code=200,
            content={
                "release_id": release.release_id,
                "state": release.state.value,
                "decision": result.decision,
                "progressed": result.progressed,
                "note": result.note,
            },
        )

    @app.get("/releases")
    def list_releases() -> dict[str, list[dict[str, object]]]:
        return {"releases": [_release_dict(r) for r in app.state.store.all_releases()]}

    @app.get("/releases/{release_id}")
    def get_release(release_id: str) -> JSONResponse:
        for release in app.state.store.all_releases():
            if release.release_id == release_id:
                return JSONResponse(_release_dict(release))
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": "发布不存在"}},
        )

    def _error_response(exc: Exception) -> JSONResponse:
        status, code = _STATUS_CODES[type(exc)]
        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": str(exc)}},
        )

    def _release_dict(release: object) -> dict[str, object]:
        from dataclasses import asdict

        data = asdict(release)
        data["state"] = release.state.value
        data["processed_event_ids"] = sorted(release.processed_event_ids)
        return data

    return app


app = create_app()
