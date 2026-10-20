# Changelog

## 0.1.0 - 2026-10-18
- First release: `Client` and `AsyncClient` for the Paysawa (Campay-style) API: `collect`, `withdraw`, `get_transaction`,
  `wait_for_transaction`, `get_balance`, `get_payment_link`, `verify_webhook`.
- Typed errors, automatic token handling, safe retries for reads (never for payments).
