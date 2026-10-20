from __future__ import annotations

from typing import Any

PENDING = "PENDING"
SUCCESSFUL = "SUCCESSFUL"
FAILED = "FAILED"


class Result(dict):
    """A plain dict that also allows attribute access: result["reference"] == result.reference."""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


class Transaction(Result):
    """A collection or withdrawal. `status` is PENDING, SUCCESSFUL or FAILED."""

    __slots__ = ()

    @property
    def is_pending(self) -> bool:
        return self.get("status") == PENDING

    @property
    def is_successful(self) -> bool:
        return self.get("status") == SUCCESSFUL

    @property
    def is_failed(self) -> bool:
        return self.get("status") == FAILED

    @property
    def is_final(self) -> bool:
        return self.get("status") in (SUCCESSFUL, FAILED)


class WebhookEvent(Transaction):
    """A verified webhook: status, reference, amount (int), currency, operator, external_reference..."""

    __slots__ = ()
