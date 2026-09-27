"""webhook 签名校验（HMAC-SHA256）。

平台在投递时用共享密钥对 *原始请求体* 计算 HMAC-SHA256，
并把十六进制摘要放在 ``X-Signature`` 头中，形如::

    X-Signature: sha256=<hex digest>

服务端用完全相同的方式重算并使用恒定时间比较，避免时序侧信道。
"""

from __future__ import annotations

import hashlib
import hmac

from release_pipeline.errors import InvalidSignatureError

_PREFIX = "sha256="


def compute_signature(secret: str, body: bytes) -> str:
    """计算请求体的十六进制 HMAC-SHA256 签名（不含 ``sha256=`` 前缀）。"""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str | None) -> None:
    """校验签名；签名缺失、格式错误或不匹配时抛 InvalidSignatureError。

    三类情况使用不同的错误信息，但异常类型相同（都是签名问题），
    便于调用方与"载荷格式错误""事件类型未知"区分开。
    """
    if signature is None:
        raise InvalidSignatureError("缺少 X-Signature 签名头")

    if not signature.startswith(_PREFIX):
        raise InvalidSignatureError("签名格式错误，应为 sha256=<hex> 形式")

    provided = signature[len(_PREFIX):].strip()
    expected = compute_signature(secret, body)
    if not hmac.compare_digest(provided, expected):
        raise InvalidSignatureError("签名与请求体不匹配")
