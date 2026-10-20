"""Paysawa clients (synchronous `Client` and asynchronous `AsyncClient`).

    from paysawa import Client

    client = Client(app_username="pa_...", app_password="...", webhook_key="whk_...", base_url="https://your-paysawa/api/")
    tx = client.collect(amount=5000, phone="670000001", description="Order 1001", external_reference="order-1001", wait=True)
    if tx.is_successful: ...
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Callable, Mapping, Optional, Union

import httpx

from .errors import AuthenticationError, NetworkError, PaysawaError, WaitTimeoutError, error_from_response
from .models import Result, Transaction, WebhookEvent
from .webhooks import verify_webhook

# Change this one line once Paysawa is hosted, or set PAYSAWA_BASE_URL, or pass base_url= to the client.
DEFAULT_BASE_URL = "https://paysawa.onrender.com/api/"

_sleep = time.sleep           # indirections so tests can run instantly
_monotonic = time.monotonic
_asleep = asyncio.sleep

Amount = Union[int, str]


def _base_url(value: Optional[str]) -> str:
    return (value or os.environ.get("PAYSAWA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/") + "/"


def _amount(value: Any) -> str:
    """Amounts are whole XAF. Accepts 5000, "5000", 5000.0; rejects 0, negatives and fractions."""
    try:
        number = float(str(value).strip())
    except ValueError:
        raise ValueError(f"amount must be a number, got {value!r}") from None
    if number <= 0 or number != int(number):
        raise ValueError(f"amount must be a whole positive number of XAF, got {value!r}")
    return str(int(number))


def _raise(response: Any) -> None:
    try:
        data = response.json()
    except ValueError:
        data = {}
    raise error_from_response(response.status_code, data)


def _collect_body(amount: Amount, phone: str, description: str, external_reference: Optional[str], currency: str) -> dict:
    return {"amount": _amount(amount), "currency": currency, "from": phone, "description": description, "external_reference": external_reference}


def _withdraw_body(amount: Amount, phone: str, description: str, external_reference: Optional[str], currency: str) -> dict:
    return {"amount": _amount(amount), "currency": currency, "to": phone, "description": description, "external_reference": external_reference}


def _link_body(amount: Amount, description: str, external_reference: Optional[str], redirect_url: Optional[str], currency: str) -> dict:
    body = {"amount": _amount(amount), "currency": currency, "description": description, "external_reference": external_reference}
    if redirect_url:
        body["redirect_url"] = redirect_url
    return body


def _env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        raise ValueError(f"Environment variable {name} is not set.")
    return value


class Client:
    """Synchronous client. Use as a context manager (`with Client(...) as client:`) or call close() when done."""

    def __init__(self, app_username: str, app_password: str, webhook_key: str = "", *, base_url: Optional[str] = None,
                 timeout: float = 30.0, max_retries: int = 2, http_client: Optional[httpx.Client] = None):
        if not app_username or not app_password:
            raise ValueError("app_username and app_password are required (create an app in your Paysawa dashboard).")
        self.base_url = _base_url(base_url)
        self.app_username = app_username
        self._password = app_password
        self.webhook_key = webhook_key
        self.max_retries = max(0, int(max_retries))
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, **kwargs: Any) -> "Client":
        """Reads PAYSAWA_APP_USERNAME, PAYSAWA_APP_PASSWORD, PAYSAWA_WEBHOOK_KEY (optional) and PAYSAWA_BASE_URL."""
        return cls(_env("PAYSAWA_APP_USERNAME"), _env("PAYSAWA_APP_PASSWORD"), _env("PAYSAWA_WEBHOOK_KEY", required=False), **kwargs)

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # ------------------------------------------------------------------ transport
    def _send(self, method: str, url: str, json: Any = None, headers: Optional[dict] = None) -> Any:
        try:
            return self._http.request(method, url, json=json, headers=headers or {})
        except httpx.TimeoutException:
            raise NetworkError("Paysawa did not answer in time.", code="timeout") from None
        except httpx.HTTPError:
            raise NetworkError(f"Cannot reach Paysawa at {self.base_url}.", code="unreachable") from None

    def _get_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and _monotonic() < self._expires_at:
                return self._token
            response = self._send("POST", self.base_url + "token/", {"username": self.app_username, "password": self._password})
            if response.status_code >= 400:
                _raise(response)
            data = response.json()
            self._token = data["token"]
            self._expires_at = _monotonic() + max(int(data.get("expires_in") or 300) - 30, 10)
            return self._token

    def _request(self, method: str, path: str, json: Any = None) -> dict:
        """Payments (POST) are never retried automatically, so a retry cannot create a second payment. Reads are."""
        retries_left = self.max_retries if method == "GET" else 0
        force_token = False
        while True:
            token = self._get_token(force=force_token)
            try:
                response = self._send(method, self.base_url + path, json, {"Authorization": f"Token {token}"})
            except NetworkError:
                if retries_left <= 0:
                    raise
                retries_left -= 1
                _sleep(0.5 * (self.max_retries - retries_left))
                continue
            if response.status_code == 401 and not force_token:
                force_token = True
                continue
            if response.status_code >= 500 and retries_left > 0:
                retries_left -= 1
                _sleep(0.5 * (self.max_retries - retries_left))
                continue
            if response.status_code >= 400:
                _raise(response)
            return response.json()

    # ------------------------------------------------------------------ API
    def collect(self, amount: Amount, phone: str, description: str = "", external_reference: Optional[str] = None, *,
                currency: str = "XAF", wait: bool = False, timeout: float = 120.0, interval: float = 3.0) -> Transaction:
        """Ask `phone` to pay `amount`. The customer approves on their phone. With wait=True this blocks until the payment
        is SUCCESSFUL or FAILED (or raises WaitTimeoutError). Returns a Transaction (reference, status, operator, ussd_code)."""
        tx = Transaction(self._request("POST", "collect/", _collect_body(amount, phone, description, external_reference, currency)))
        if wait and not tx.is_final:
            return self.wait_for_transaction(tx.reference, timeout=timeout, interval=interval)
        return tx

    def withdraw(self, amount: Amount, phone: str, description: str = "", external_reference: Optional[str] = None, *,
                 currency: str = "XAF", wait: bool = False, timeout: float = 120.0, interval: float = 3.0) -> Transaction:
        """Send `amount` from your Paysawa balance to `phone`. Raises InsufficientFundsError if amount + fee exceeds the balance."""
        tx = Transaction(self._request("POST", "withdraw/", _withdraw_body(amount, phone, description, external_reference, currency)))
        if wait and not tx.is_final:
            return self.wait_for_transaction(tx.reference, timeout=timeout, interval=interval)
        return tx

    def get_transaction(self, reference: str) -> Transaction:
        """Latest state of a collection, withdrawal (col_..., pay_...) or payment link (cs_...)."""
        return Transaction(self._request("GET", f"transaction/{reference}/"))

    def wait_for_transaction(self, reference: str, *, timeout: float = 120.0, interval: float = 3.0,
                             on_update: Optional[Callable[[Transaction], None]] = None) -> Transaction:
        deadline = _monotonic() + timeout
        tx = self.get_transaction(reference)
        while not tx.is_final:
            if on_update:
                on_update(tx)
            if _monotonic() >= deadline:
                raise WaitTimeoutError(f"Transaction {reference} is still pending after {timeout:g}s.", transaction=tx)
            _sleep(max(0.0, min(interval, deadline - _monotonic())))
            tx = self.get_transaction(reference)
        return tx

    def get_balance(self) -> Result:
        """{'total_balance': ..., 'held_balance': ..., 'currency': 'XAF'}. One balance per merchant."""
        return Result(self._request("GET", "balance/"))

    def get_payment_link(self, amount: Amount, description: str = "", external_reference: Optional[str] = None,
                         redirect_url: Optional[str] = None, *, currency: str = "XAF") -> Result:
        """Create a hosted payment page link. Returns {'link': ..., 'reference': 'cs_...'}."""
        return Result(self._request("POST", "get_payment_link/", _link_body(amount, description, external_reference, redirect_url, currency)))

    def verify_webhook(self, params: Mapping[str, Any]) -> WebhookEvent:
        """Verify a webhook received on your webhook URL with this client's webhook_key."""
        return verify_webhook(params, self.webhook_key)


class AsyncClient:
    """Asynchronous client with the same methods. Use `async with AsyncClient(...) as client:`."""

    def __init__(self, app_username: str, app_password: str, webhook_key: str = "", *, base_url: Optional[str] = None,
                 timeout: float = 30.0, max_retries: int = 2, http_client: Optional[httpx.AsyncClient] = None):
        if not app_username or not app_password:
            raise ValueError("app_username and app_password are required (create an app in your Paysawa dashboard).")
        self.base_url = _base_url(base_url)
        self.app_username = app_username
        self._password = app_password
        self.webhook_key = webhook_key
        self.max_retries = max(0, int(max_retries))
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._lock: Optional[asyncio.Lock] = None

    @classmethod
    def from_env(cls, **kwargs: Any) -> "AsyncClient":
        return cls(_env("PAYSAWA_APP_USERNAME"), _env("PAYSAWA_APP_PASSWORD"), _env("PAYSAWA_WEBHOOK_KEY", required=False), **kwargs)

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _send(self, method: str, url: str, json: Any = None, headers: Optional[dict] = None) -> Any:
        try:
            return await self._http.request(method, url, json=json, headers=headers or {})
        except httpx.TimeoutException:
            raise NetworkError("Paysawa did not answer in time.", code="timeout") from None
        except httpx.HTTPError:
            raise NetworkError(f"Cannot reach Paysawa at {self.base_url}.", code="unreachable") from None

    async def _get_token(self, force: bool = False) -> str:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not force and self._token and _monotonic() < self._expires_at:
                return self._token
            response = await self._send("POST", self.base_url + "token/", {"username": self.app_username, "password": self._password})
            if response.status_code >= 400:
                _raise(response)
            data = response.json()
            self._token = data["token"]
            self._expires_at = _monotonic() + max(int(data.get("expires_in") or 300) - 30, 10)
            return self._token

    async def _request(self, method: str, path: str, json: Any = None) -> dict:
        retries_left = self.max_retries if method == "GET" else 0
        force_token = False
        while True:
            token = await self._get_token(force=force_token)
            try:
                response = await self._send(method, self.base_url + path, json, {"Authorization": f"Token {token}"})
            except NetworkError:
                if retries_left <= 0:
                    raise
                retries_left -= 1
                await _asleep(0.5 * (self.max_retries - retries_left))
                continue
            if response.status_code == 401 and not force_token:
                force_token = True
                continue
            if response.status_code >= 500 and retries_left > 0:
                retries_left -= 1
                await _asleep(0.5 * (self.max_retries - retries_left))
                continue
            if response.status_code >= 400:
                _raise(response)
            return response.json()

    async def collect(self, amount: Amount, phone: str, description: str = "", external_reference: Optional[str] = None, *,
                      currency: str = "XAF", wait: bool = False, timeout: float = 120.0, interval: float = 3.0) -> Transaction:
        tx = Transaction(await self._request("POST", "collect/", _collect_body(amount, phone, description, external_reference, currency)))
        if wait and not tx.is_final:
            return await self.wait_for_transaction(tx.reference, timeout=timeout, interval=interval)
        return tx

    async def withdraw(self, amount: Amount, phone: str, description: str = "", external_reference: Optional[str] = None, *,
                       currency: str = "XAF", wait: bool = False, timeout: float = 120.0, interval: float = 3.0) -> Transaction:
        tx = Transaction(await self._request("POST", "withdraw/", _withdraw_body(amount, phone, description, external_reference, currency)))
        if wait and not tx.is_final:
            return await self.wait_for_transaction(tx.reference, timeout=timeout, interval=interval)
        return tx

    async def get_transaction(self, reference: str) -> Transaction:
        return Transaction(await self._request("GET", f"transaction/{reference}/"))

    async def wait_for_transaction(self, reference: str, *, timeout: float = 120.0, interval: float = 3.0,
                                   on_update: Optional[Callable[[Transaction], None]] = None) -> Transaction:
        deadline = _monotonic() + timeout
        tx = await self.get_transaction(reference)
        while not tx.is_final:
            if on_update:
                on_update(tx)
            if _monotonic() >= deadline:
                raise WaitTimeoutError(f"Transaction {reference} is still pending after {timeout:g}s.", transaction=tx)
            await _asleep(max(0.0, min(interval, deadline - _monotonic())))
            tx = await self.get_transaction(reference)
        return tx

    async def get_balance(self) -> Result:
        return Result(await self._request("GET", "balance/"))

    async def get_payment_link(self, amount: Amount, description: str = "", external_reference: Optional[str] = None,
                               redirect_url: Optional[str] = None, *, currency: str = "XAF") -> Result:
        return Result(await self._request("POST", "get_payment_link/", _link_body(amount, description, external_reference, redirect_url, currency)))

    def verify_webhook(self, params: Mapping[str, Any]) -> WebhookEvent:
        return verify_webhook(params, self.webhook_key)
