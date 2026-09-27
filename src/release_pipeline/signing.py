"""Webhook 签名校验。

约定（详见 docs/design.md）：

    signature = hex(HMAC_SHA256(secret, f"{event_id}.{raw_body}"))

平台在 ``X-Signature`` 头中给出十六进制摘要，服务用配置的密钥对
``X-Event-Id`` 与**原始字节请求体**重新计算并做恒定时间比较。
"""

from __future__ import annotations

import hashlib
import hmac


def _signed_content(event_id: str, raw_body: bytes) -> bytes:
    return event_id.encode("utf-8") + b"." + raw_body


def compute_signature(secret: str, event_id: str, raw_body: bytes) -> str:
    """按约定计算签名，便于服务端校验与测试/文档构造样例。"""
    return hmac.new(
        secret.encode("utf-8"),
        _signed_content(event_id, raw_body),
        hashlib.sha256,
    ).hexdigest()


def verify_signature(
    secret: str, event_id: str, raw_body: bytes, signature: str | None
) -> bool:
    """恒定时间比较签名；签名缺失或不匹配均返回 ``False``。"""
    if not event_id or not signature:
        return False
    expected = compute_signature(secret, event_id, raw_body)
    # 两者长度不一致时 hmac.compare_digest 也安全地返回 False
    return hmac.compare_digest(expected, signature.strip())
