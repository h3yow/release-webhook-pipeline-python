"""事件接收阶段的可区分拒绝原因。"""

from __future__ import annotations


class WebhookError(Exception):
    """所有 webhook 拒绝原因的基类。"""

    status_code = 400
    code = "webhook_error"


class InvalidSignatureError(WebhookError):
    """签名缺失或校验不通过。"""

    status_code = 401
    code = "invalid_signature"


class InvalidPayloadError(WebhookError):
    """请求体不是合法 JSON 或不符合事件 schema。"""

    status_code = 400
    code = "invalid_payload"


class UnknownEventTypeError(WebhookError):
    """事件类型不在服务支持范围内。"""

    status_code = 422
    code = "unknown_event_type"
