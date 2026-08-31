from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from backend.app.domain.capacity import CapacityExceededError, ensure_capacity
from backend.app.providers.base import CapacityDTO


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    REJECTED_CAPACITY = "REJECTED_CAPACITY"


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    quota_gb: Decimal
    status: OrderStatus = OrderStatus.PENDING


class OrderState(Protocol):
    def mark_paid(self, order_id: str) -> None: ...

    def reject_capacity(self, order_id: str, reason: str) -> None: ...


def confirm_payment(order: Order, capacity: CapacityDTO, state: OrderState) -> None:
    """Mark payment only after the hard capacity guard succeeds."""
    try:
        ensure_capacity(capacity, order.quota_gb)
    except CapacityExceededError as exc:
        state.reject_capacity(order.order_id, str(exc))
        raise
    state.mark_paid(order.order_id)
