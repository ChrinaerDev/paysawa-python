"""Exceptions raised by the SDK. Catch PaysawaError to catch them all."""
from __future__ import annotations

from typing import Any, Optional


class PaysawaError(Exception):
    """Base class. `status` is the HTTP status (None for network problems), `code` the machine-readable error code."""

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code

    def __str__(self) -> str:
        prefix = f"[{self.code}] " if self.code else ""
        return f"{prefix}{self.message}"


class AuthenticationError(PaysawaError):
    """Wrong app username/password, or an expired/invalid token (HTTP 401)."""


class PermissionDeniedError(PaysawaError):
    """The account cannot do this yet, e.g. live mode not enabled or account suspended (HTTP 403)."""


class InvalidRequestError(PaysawaError):
    """The request was refused: bad amount, bad phone number, unknown reference, limits... (HTTP 400/404/409/410/422)."""


class InsufficientFundsError(InvalidRequestError):
    """A withdrawal needs more than the available balance (amount + fee)."""


class RateLimitError(PaysawaError):
    """Too many requests (HTTP 429)."""


class ServerError(PaysawaError):
    """Paysawa had a problem or is busy (HTTP 5xx). Safe to retry reads; for payments check the transaction first."""


class NetworkError(PaysawaError):
    """Could not reach Paysawa (timeout, DNS, connection refused)."""


class WebhookVerificationError(PaysawaError):
    """A webhook did not pass signature verification. Do NOT trust it."""


class WaitTimeoutError(PaysawaError):
    """wait_for_transaction() gave up while the transaction was still pending. `.transaction` holds its latest state."""

    def __init__(self, message: str, *, transaction: Any = None):
        super().__init__(message, code="wait_timeout")
        self.transaction = transaction


def error_from_response(status: int, data: Any) -> PaysawaError:
    """Builds the right exception from an error response (flat {message, code} or {error: {code, message}})."""
    message, code = f"HTTP {status}", None
    if isinstance(data, dict):
        inner = data.get("error") if isinstance(data.get("error"), dict) else data
        message = str(inner.get("message") or data.get("detail") or message)
        code = inner.get("code")
    if code == "insufficient_funds":
        return InsufficientFundsError(message, status=status, code=code)
    if status == 401:
        return AuthenticationError(message, status=status, code=code)
    if status == 403:
        return PermissionDeniedError(message, status=status, code=code)
    if status == 429:
        return RateLimitError(message, status=status, code=code)
    if status >= 500:
        return ServerError(message, status=status, code=code)
    return InvalidRequestError(message, status=status, code=code)
