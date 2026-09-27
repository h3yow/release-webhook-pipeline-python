"""FastAPI 路由：接收事件、查询发布状态。"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request, Response

from ..errors import WebhookError
from ..service import ReleasePipelineService
from ..state import TERMINAL_STATES
from .handler import WebhookHandler


def create_router(handler: WebhookHandler, service: ReleasePipelineService) -> APIRouter:
    router = APIRouter()

    @router.post("/webhook/events")
    async def receive_event(
        request: Request,
        x_event_id: str | None = Header(default=None),
        x_signature: str | None = Header(default=None),
    ) -> Response:
        raw_body = await request.body()
        result = handler.handle_raw(raw_body, x_event_id, x_signature)
        return _json_response(
            200,
            {
                "release_id": result.release_id,
                "state": result.state.value,
                "decision": result.decision,
                "reason": result.reason,
            },
        )

    @router.get("/releases/{release_id}")
    async def get_release(release_id: str) -> Response:
        record = service.store.get(release_id)
        if record is None:
            return _json_response(404, {"error": "release_not_found"})
        return _json_response(
            200,
            {
                "release_id": record.release_id,
                "state": record.state.value,
                "terminal": record.state in TERMINAL_STATES,
                "version": record.version,
                "last_sequence": record.last_sequence,
                "build_attempts": record.build_attempts,
                "failure_reason": record.failure_reason,
                "rollback_reason": record.rollback_reason,
                "attempts": [
                    {
                        "attempt": a.attempt,
                        "result": a.result,
                        "reason": a.reason,
                        "timestamp": a.timestamp,
                    }
                    for a in record.attempts
                ],
                "history": record.history,
            },
        )

    return router


def _json_response(status_code: int, payload: dict) -> Response:
    import json

    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        status_code=status_code,
        media_type="application/json",
    )


def register_error_handlers(app) -> None:  # type: ignore[no-untyped-def]
    """把领域拒绝原因映射为带区分度的 HTTP 响应。"""

    @app.exception_handler(WebhookError)
    async def _handle(_request, exc: WebhookError):  # noqa: ANN202
        return _json_response(
            exc.status_code,
            {"error": exc.code, "detail": str(exc)},
        )
