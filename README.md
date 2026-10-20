# paysawa

Python SDK for the **Paysawa** mobile money gateway (MTN Mobile Money and Orange Money, XAF).
Collect payments, send money, create payment links and verify signed webhooks with a few lines of code.
It speaks Paysawa's Campay-style API, so the concepts are the same as Campay's.

```bash
pip install paysawa
```

## 1. Get your credentials

In your Paysawa dashboard create an **app**. It gives you three values (you can show them again any time):

| Value | What it is |
|---|---|
| App username | like `pa_1a2b3c4d5e6f7a8b` |
| App password | secret, never put it in front-end code or in git |
| Webhook key | verifies that webhooks really come from Paysawa |

A **test** app uses the sandbox (no real money). A **live** app moves real money.

## 2. Quick start

```python
from paysawa import Client

client = Client(
    app_username="pa_xxxxxxxxxxxxxxxx",
    app_password="your-app-password",
    webhook_key="whk_xxxxxxxx",
    base_url="https://paysawa.onrender.com/api/",   # or set PAYSAWA_BASE_URL
)

# Ask a customer to pay: they get a prompt on their phone and approve with their PIN.
tx = client.collect(amount=5000, phone="670000001", description="Order 1001",
                    external_reference="order-1001", wait=True)   # wait=True blocks until final
if tx.is_successful:
    print("Paid!", tx.reference)
else:
    print("Not paid:", tx.status)

print(client.get_balance().total_balance)                          # your Paysawa balance
client.withdraw(amount=1000, phone="690000001", description="Refund")   # pay money out
link = client.get_payment_link(amount=2000, description="Order 1002", redirect_url="https://shop.example.com/thanks")
print(link.link)                                                    # send this to your customer
```

Put the credentials in environment variables instead of code and use `Client.from_env()`:
`PAYSAWA_APP_USERNAME`, `PAYSAWA_APP_PASSWORD`, `PAYSAWA_WEBHOOK_KEY`, `PAYSAWA_BASE_URL`.

## 3. Statuses

Every transaction is `PENDING`, `SUCCESSFUL` or `FAILED` (`tx.is_pending`, `tx.is_successful`, `tx.is_failed`, `tx.is_final`).
A collection stays `PENDING` until the customer approves. Without `wait=True`, check it later with
`client.get_transaction(reference)` or use `client.wait_for_transaction(reference, timeout=120)`, or rely on webhooks.

## 4. Webhooks

Set your app's webhook URL in the dashboard. Paysawa calls it with a `GET` and query parameters plus a signed `signature`.
**Always verify it**:

```python
# Flask
@app.get("/webhooks/paysawa")
def paysawa_webhook():
    try:
        event = client.verify_webhook(request.args.to_dict())
    except WebhookVerificationError:
        return "invalid", 401
    if event.is_successful:
        mark_order_paid(event.external_reference, event.amount)
    return "ok"
```

```python
# FastAPI
@app.get("/webhooks/paysawa")
async def paysawa_webhook(request: Request):
    event = client.verify_webhook(dict(request.query_params))   # raises WebhookVerificationError if not authentic
    ...
```

Django: `client.verify_webhook(request.GET.dict())`. Reply with any 2xx status; otherwise Paysawa retries.
Check `event.amount` against the order before you ship anything.

## 5. Async

```python
from paysawa import AsyncClient

async with AsyncClient.from_env() as client:
    tx = await client.collect(5000, "670000001", "Order 1001", "order-1001", wait=True)
```

## 6. Errors

All errors inherit from `PaysawaError` (`.message`, `.status`, `.code`):

| Exception | When |
|---|---|
| `AuthenticationError` | wrong app username/password |
| `PermissionDeniedError` | live mode not enabled, account suspended |
| `InvalidRequestError` | bad amount or phone number, unknown reference, limits |
| `InsufficientFundsError` | withdrawal larger than your balance (amount + fee) |
| `RateLimitError` | too many requests |
| `ServerError` / `NetworkError` | Paysawa busy or unreachable |
| `WaitTimeoutError` | `wait_for_transaction` gave up (`.transaction` has the latest state) |
| `WebhookVerificationError` | the webhook is not authentic: ignore it |

Payments (collect, withdraw) are **never retried automatically**, so a retry can never charge twice.
Reads are retried twice. If a payment call fails with a network error, look the payment up by your
`external_reference` before trying again.

## 7. Sandbox numbers (test apps)

`670000001` succeeds, a number ending in `0000` fails, a number ending in `9999` stays pending.

## Development

```bash
pip install -e ".[dev]"
pytest
```
