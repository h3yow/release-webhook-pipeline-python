"""领域错误：每一类拒绝原因对应一个独立异常类型。"""


class WebhookError(Exception):
    """webhook 处理错误基类。"""


class InvalidSignatureError(WebhookError):
    """签名缺失或校验失败。"""


class InvalidPayloadError(WebhookError):
    """请求体不是合法 JSON 或缺少必需字段。"""


class UnknownEventTypeError(WebhookError):
    """事件类型不在支持列表内。"""


class StaleEventError(WebhookError):
    """事件序列号落后于当前状态（乱序旧事件）。"""
