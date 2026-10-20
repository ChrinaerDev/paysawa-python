"""Webhook verification. Paysawa calls your app's webhook URL with a GET request: query parameters plus a `signature`
(a JWT signed with your app's webhook key). Always verify it before trusting the data."""
from __future__ import annotations

from typing import Any, Mapping

import jwt

from .errors import WebhookVerificationError
from .models import WebhookEvent


def verify_webhook(params: Mapping[str, Any], webhook_key: str) -> WebhookEvent:
    """Verify a webhook and return it as a WebhookEvent. Raises WebhookVerificationError if it is not authentic.

    `params` are the query parameters, as plain strings (Flask: request.args.to_dict(), FastAPI: dict(request.query_params),
    Django: request.GET.dict()). Besides checking the signature, every signed field is compared with the received one, so a
    valid signature cannot be replayed with other values (a different amount, for example).
    """
    if not webhook_key:
        raise WebhookVerificationError("A webhook_key is required to verify webhooks.", code="no_webhook_key")
    data = dict(params)
    signature = data.get("signature")
    if not signature:
        raise WebhookVerificationError("The webhook has no signature.", code="missing_signature")
    try:
        signed = jwt.decode(signature, webhook_key, algorithms=["HS256"], leeway=10)
    except jwt.ExpiredSignatureError:
        raise WebhookVerificationError("The webhook signature has expired.", code="expired_signature") from None
    except jwt.PyJWTError:
        raise WebhookVerificationError("The webhook signature is not valid for this webhook key.", code="invalid_signature") from None
    clean = {k: v for k, v in data.items() if k != "signature"}
    for key, value in signed.items():
        if key in ("iat", "exp"):
            continue
        if str(clean.get(key, "")) != str(value):
            raise WebhookVerificationError(f"Parameter '{key}' does not match what was signed.", code="tampered")
    amount = clean.get("amount")
    if isinstance(amount, str) and amount.isdigit():
        clean["amount"] = int(amount)
    return WebhookEvent(clean)
