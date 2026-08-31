from decimal import Decimal

import pytest

from backend.app.domain.capacity import CapacityExceededError
from backend.app.domain.ordering import Order, confirm_payment
from backend.app.providers.base import CapacityDTO


class State:
    def __init__(self) -> None:
        self.events: list[str] = []

    def capacity(self) -> CapacityDTO:
        return CapacityDTO(Decimal("100"), Decimal("90"), Decimal("5"))

    def mark_paid(self, order_id: str) -> None:
        self.events.append(f"paid:{order_id}")

    def reject_capacity(self, order_id: str, reason: str) -> None:
        self.events.append(f"rejected:{order_id}")


def test_capacity_rejection_precedes_and_prevents_paid_state() -> None:
    state = State()
    with pytest.raises(CapacityExceededError):
        confirm_payment(Order("order-1", Decimal("10")), state.capacity(), state)
    assert state.events == ["rejected:order-1"]
