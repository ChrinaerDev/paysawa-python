import time

import jwt
import pytest

from paysawa import Client, WebhookVerificationError, verify_webhook

KEY = "whk_test_key"


def signed(params, key=KEY, exp_in=3600):
    now = int(time.time())
    return {**params, "signature": jwt.encode({**params, "iat": now, "exp": now + exp_in}, key, algorithm="HS256")}


PARAMS = {"status": "SUCCESSFUL", "reference": "col_1", "amount": "5000", "currency": "XAF", "operator": "MTN",
          "code": "", "operator_reference": "op-1", "external_reference": "order-1001"}


def test_valid_webhook_becomes_an_event():
    event = verify_webhook(signed(PARAMS), KEY)
    assert event.is_successful and event.amount == 5000 and event.external_reference == "order-1001" and "signature" not in event


def test_client_verifies_with_its_own_key():
    client = Client("pa_x", "pw", KEY, base_url="http://x.test/api/")
    assert client.verify_webhook(signed(PARAMS)).reference == "col_1"


def test_changed_values_wrong_key_missing_and_expired_are_rejected():
    good = signed(PARAMS)
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook({**good, "amount": "1"}, KEY)
    assert exc.value.code == "tampered"
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook(signed(PARAMS, key="whk_other"), KEY)
    assert exc.value.code == "invalid_signature"
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook(PARAMS, KEY)
    assert exc.value.code == "missing_signature"
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook(signed(PARAMS, exp_in=-3600), KEY)
    assert exc.value.code == "expired_signature"
    with pytest.raises(WebhookVerificationError) as exc:
        verify_webhook(good, "")
    assert exc.value.code == "no_webhook_key"
