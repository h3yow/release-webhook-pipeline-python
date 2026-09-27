"""签名计算与校验。"""

from __future__ import annotations

from release_pipeline.signing import compute_signature, verify_signature


def test_compute_is_deterministic_hex():
    body = b'{"event_id":"e1"}'
    sig1 = compute_signature("s", "e1", body)
    sig2 = compute_signature("s", "e1", body)
    assert sig1 == sig2
    assert len(sig1) == 64  # SHA256 hex
    int(sig1, 16)  # 必须是合法十六进制


def test_secret_event_id_body_any_change_invalidates():
    body = b"payload"
    sig = compute_signature("secret-a", "e1", body)
    assert verify_signature("secret-a", "e1", body, sig)
    assert not verify_signature("secret-b", "e1", body, sig)
    assert not verify_signature("secret-a", "e2", body, sig)
    assert not verify_signature("secret-a", "e1", b"payload ", sig)


def test_missing_or_empty_signature_rejected():
    body = b"payload"
    assert not verify_signature("s", "e1", body, None)
    assert not verify_signature("s", "e1", body, "")
    assert not verify_signature("s", "", body, "whatever")


def test_whitespace_tolerated_but_content_must_match():
    body = b"payload"
    sig = compute_signature("s", "e1", body)
    assert verify_signature("s", "e1", body, f"  {sig}  ")
    assert not verify_signature("s", "e1", body, sig[:-1] + "0")
