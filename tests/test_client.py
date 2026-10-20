import json
import os

import httpx
import pytest

import paysawa
from paysawa import (
    AsyncClient, AuthenticationError, Client, InsufficientFundsError, InvalidRequestError, NetworkError,
    PermissionDeniedError, RateLimitError, ServerError, Transaction, WaitTimeoutError,
)
import paysawa.client as client_module

client_module._sleep = lambda seconds: None          # keep the tests instant


async def _no_sleep(seconds):
    return None


client_module._asleep = _no_sleep
BASE = "http://paysawa.test/api/"


def make_server(**state):
    state.setdefault("calls", [])
    state.setdefault("tokens", 0)
    state.setdefault("script", {})   # path suffix -> list of responses (last one repeats)

    def respond(path):
        queue = state["script"].get(path)
        if not queue:
            return None
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item[0], json=item[1])

    def handler(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        state["calls"].append((request.method, path, body, {k.lower(): v for k, v in request.headers.items()}))
        if path.endswith("/token/"):
            state["tokens"] += 1
            if body.get("password") != "secret":
                return httpx.Response(401, json={"message": "Invalid app username or password.", "code": "invalid_credentials"})
            return httpx.Response(200, json={"token": f"T{state['tokens']}", "expires_in": 3600})
        for suffix in state["script"]:
            if path.endswith(suffix):
                return respond(suffix)
        return httpx.Response(404, json={"message": "no such endpoint", "code": "not_found"})

    return handler, state


def make_client(**state):
    handler, st = make_server(**state)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return Client("pa_demo", "secret", "whk_demo", base_url=BASE, http_client=http), st


def calls_to(state, suffix):
    return [c for c in state["calls"] if c[1].endswith(suffix)]


def test_collect_sends_a_campay_style_request_and_caches_the_token():
    client, st = make_client(script={"/collect/": [(200, {"reference": "col_1", "ussd_code": "*126#", "operator": "MTN", "status": "PENDING"})],
                                     "/balance/": [(200, {"total_balance": 10, "held_balance": 0, "currency": "XAF"})]})
    tx = client.collect(5000, "670000001", "Order 1001", "order-1001")
    client.get_balance()
    assert isinstance(tx, Transaction) and tx.reference == "col_1" and tx["operator"] == "MTN" and tx.is_pending and not tx.is_final
    assert st["tokens"] == 1
    method, path, body, headers = calls_to(st, "/collect/")[0]
    assert headers["authorization"] == "Token T1"
    assert body == {"amount": "5000", "currency": "XAF", "from": "670000001", "description": "Order 1001", "external_reference": "order-1001"}


def test_withdraw_uses_the_to_field_and_reports_status():
    client, st = make_client(script={"/withdraw/": [(200, {"reference": "pay_1", "status": "SUCCESSFUL", "amount": 1000, "currency": "XAF", "operator": "ORANGE"})]})
    tx = client.withdraw("1000", "690000001", "Refund")
    assert tx.is_successful and tx.is_final and tx.operator == "ORANGE"
    assert calls_to(st, "/withdraw/")[0][2]["to"] == "690000001"


def test_expired_token_is_replaced_once():
    handler, st = make_server(script={"/balance/": [(401, {"message": "Invalid or expired token.", "code": "unauthorized"}),
                                                    (200, {"total_balance": 4875, "held_balance": 0, "currency": "XAF"})]})
    client = Client("pa_demo", "secret", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.get_balance().total_balance == 4875
    assert st["tokens"] == 2


def test_wrong_credentials_raise_authentication_error():
    handler, _ = make_server()
    client = Client("pa_demo", "wrong", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthenticationError) as exc:
        client.get_balance()
    assert exc.value.code == "invalid_credentials" and exc.value.status == 401


def test_error_responses_become_specific_exceptions():
    client, _ = make_client(script={
        "/withdraw/": [(402, {"message": "Available balance is 0 XAF, 1025 XAF needed (amount + fee).", "code": "insufficient_funds"})],
        "/collect/": [(422, {"message": "expected a Cameroonian mobile number", "code": "invalid_phone"})],
        "/get_payment_link/": [(403, {"message": "Live mode is not enabled for this account yet.", "code": "live_not_enabled"})],
        "/balance/": [(429, {"message": "Too many requests, slow down.", "code": "rate_limited"})],
    })
    with pytest.raises(InsufficientFundsError) as exc:
        client.withdraw(1000, "690000001")
    assert exc.value.code == "insufficient_funds" and isinstance(exc.value, InvalidRequestError)
    with pytest.raises(InvalidRequestError) as exc:
        client.collect(1000, "12345")
    assert exc.value.code == "invalid_phone" and "[invalid_phone]" in str(exc.value)
    with pytest.raises(PermissionDeniedError):
        client.get_payment_link(2000)
    with pytest.raises(RateLimitError):
        client.get_balance()


def test_payments_are_never_retried_but_reads_are():
    client, st = make_client(script={"/collect/": [(503, {"message": "busy", "code": "busy"})],
                                     "/balance/": [(503, {"message": "busy", "code": "busy"}), (200, {"total_balance": 5, "held_balance": 0, "currency": "XAF"})]})
    with pytest.raises(ServerError):
        client.collect(1000, "670000001")
    assert len(calls_to(st, "/collect/")) == 1                       # a retry could have charged the customer twice
    assert client.get_balance().total_balance == 5
    assert len(calls_to(st, "/balance/")) == 2


def test_network_problems_are_wrapped():
    client, st = make_client(script={"/collect/": [httpx.ConnectError("refused")], "/balance/": [httpx.ConnectError("refused")]})
    with pytest.raises(NetworkError) as exc:
        client.collect(1000, "670000001")
    assert exc.value.code == "unreachable" and len(calls_to(st, "/collect/")) == 1
    with pytest.raises(NetworkError):
        client.get_balance()
    assert len(calls_to(st, "/balance/")) == 3                       # 1 try + 2 retries


def test_wait_for_transaction_polls_until_final():
    pending = (200, {"reference": "col_1", "status": "PENDING"})
    done = (200, {"reference": "col_1", "status": "SUCCESSFUL", "amount": 5000})
    client, st = make_client(script={"/collect/": [(200, {"reference": "col_1", "status": "PENDING", "operator": "MTN", "ussd_code": "*126#"})],
                                     "/transaction/col_1/": [pending, pending, done]})
    tx = client.collect(5000, "670000001", wait=True)
    assert tx.is_successful and tx.amount == 5000
    assert len(calls_to(st, "/transaction/col_1/")) == 3


def test_wait_for_transaction_times_out_with_the_latest_state():
    client, _ = make_client(script={"/transaction/col_1/": [(200, {"reference": "col_1", "status": "PENDING"})]})
    clock = {"t": 0.0}
    client_module._monotonic = lambda: clock.__setitem__("t", clock["t"] + 50.0) or clock["t"]
    try:
        with pytest.raises(WaitTimeoutError) as exc:
            client.wait_for_transaction("col_1", timeout=120, interval=3)
    finally:
        import time
        client_module._monotonic = time.monotonic
    assert exc.value.transaction.is_pending and exc.value.code == "wait_timeout"


def test_balance_and_payment_link_and_attribute_access():
    client, st = make_client(script={"/balance/": [(200, {"total_balance": 4875, "mtn_balance": None, "orange_balance": None, "held_balance": 0, "currency": "XAF"})],
                                     "/get_payment_link/": [(200, {"link": "https://paysawa.onrender.com/pay/cs_1", "reference": "cs_1"})]})
    assert client.get_balance().total_balance == 4875
    link = client.get_payment_link(2000, "Order", "order-1", "https://shop.example.com/thanks")
    assert link.link.endswith("/pay/cs_1") and link["reference"] == "cs_1"
    assert calls_to(st, "/get_payment_link/")[0][2]["redirect_url"] == "https://shop.example.com/thanks"
    with pytest.raises(AttributeError):
        link.nothing_here


def test_bad_input_fails_before_any_request():
    client, st = make_client()
    for bad in (0, -5, 5.5, "abc"):
        with pytest.raises(ValueError):
            client.collect(bad, "670000001")
    with pytest.raises(ValueError):
        Client("", "x")
    assert st["calls"] == []


def test_from_env():
    os.environ.update({"PAYSAWA_APP_USERNAME": "pa_env", "PAYSAWA_APP_PASSWORD": "pw", "PAYSAWA_WEBHOOK_KEY": "whk_env", "PAYSAWA_BASE_URL": "http://x.test/api"})
    try:
        client = Client.from_env()
        assert client.app_username == "pa_env" and client.webhook_key == "whk_env" and client.base_url == "http://x.test/api/"
        os.environ.pop("PAYSAWA_APP_PASSWORD")
        with pytest.raises(ValueError):
            Client.from_env()
    finally:
        for key in ("PAYSAWA_APP_USERNAME", "PAYSAWA_APP_PASSWORD", "PAYSAWA_WEBHOOK_KEY", "PAYSAWA_BASE_URL"):
            os.environ.pop(key, None)


async def test_async_client_collect_and_wait():
    handler, st = make_server(script={"/collect/": [(200, {"reference": "col_7", "status": "PENDING"})],
                                      "/transaction/col_7/": [(200, {"reference": "col_7", "status": "PENDING"}), (200, {"reference": "col_7", "status": "FAILED", "reason": "x"})]})
    client = AsyncClient("pa_demo", "secret", "whk_demo", base_url=BASE, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    tx = await client.collect(5000, "670000000", wait=True)
    assert tx.is_failed and tx.reason == "x"
    assert st["tokens"] == 1


def test_package_exports():
    assert paysawa.__version__ and paysawa.PENDING == "PENDING" and callable(paysawa.verify_webhook)
