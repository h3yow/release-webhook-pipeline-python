"""把原始 HTTP 请求解析、校验并交给流水线服务。

校验顺序固定（便于拒绝原因可区分）：
1. 签名 -> InvalidSignatureError (401)
2. JSON / schema -> InvalidPayloadError (400)
3. 事件类型 -> UnknownEventTypeError (422)
"""

from __future__ import annotations

import json
import logging

from ..errors import InvalidPayloadError, InvalidSignatureError
from ..events import parse_event
from ..service import HandleResult, ReleasePipelineService
from ..signing import verify_signature

logger = logging.getLogger("release_pipeline.webhook")


class WebhookHandler:
    def __init__(self, service: ReleasePipelineService, secret: str) -> None:
        self.service = service
        self.secret = secret

    def handle_raw(
        self,
        raw_body: bytes,
        event_id_header: str | None,
        signature_header: str | None,
    ) -> HandleResult:
        """对原始请求体完成三层校验后交给服务，非法即抛出对应错误。"""
        # 1) 签名：基于 X-Event-Id 与原始字节体，先于任何解析
        if not event_id_header:
            self._reject("invalid_signature", "missing X-Event-Id header")
        if not verify_signature(
            self.secret, event_id_header or "", raw_body, signature_header
        ):
            self._reject(
                "invalid_signature",
                "signature missing or does not match HMAC-SHA256 of "
                "'<event_id>.<raw_body>'",
            )

        # 2) JSON 解析 + schema 校验
        try:
            raw = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._reject("invalid_payload", f"body is not valid JSON: {exc}")
        if not isinstance(raw, dict):
            self._reject("invalid_payload", "JSON body must be an object")

        # 头里的 event_id 必须与载荷一致，防止签名被挪用到别的事件
        if raw.get("event_id") != event_id_header:
            self._reject(
                "invalid_payload",
                "event_id in body does not match X-Event-Id header",
            )

        event = parse_event(raw)  # schema -> InvalidPayload; 类型 -> UnknownEventType

        logger.info(
            "webhook accepted for parsing: event_id=%s type=%s release=%s seq=%s",
            event.event_id,
            event.event_type,
            event.release_id,
            event.sequence,
        )
        return self.service.handle(event)

    def _reject(self, code: str, detail: str) -> None:
        logger.warning("webhook rejected: reason=%s detail=%s", code, detail)
        if code == "invalid_signature":
            raise InvalidSignatureError(detail)
        if code == "invalid_payload":
            raise InvalidPayloadError(detail)
        # parse_event 内部直接抛 UnknownEventTypeError，这里保留兜底
        raise InvalidPayloadError(detail)
