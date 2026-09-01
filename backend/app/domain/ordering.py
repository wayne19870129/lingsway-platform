from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
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


class BillingOrderType(StrEnum):
    PURCHASE = "PURCHASE"
    RENEWAL = "RENEWAL"
    UPGRADE = "UPGRADE"
    ADDON = "ADDON"


@dataclass(frozen=True, slots=True)
class PaymentConfirmation:
    provider_code: str
    external_reference: str
    paid_at: datetime


@dataclass(frozen=True, slots=True)
class BillingCommand:
    """The DB-side command selected after a payment is confirmed."""

    order_id: str
    subscription_id: str | None
    order_type: BillingOrderType
    plan_id: str | None = None
    addon_bytes: int = 0


class OrderWorkflowState(Protocol):
    """Transactional DB state transitions owned by the ordering domain."""

    def transaction(self) -> AbstractContextManager[None]: ...

    def record_payment(self, order_id: str, receipt: PaymentConfirmation) -> None: ...

    def reject_capacity(self, order_id: str, reason: str) -> None: ...

    def prepare_purchase(self, command: BillingCommand) -> None: ...

    def apply_renewal(self, command: BillingCommand) -> None: ...

    def apply_upgrade(self, command: BillingCommand) -> None: ...

    def apply_addon(self, command: BillingCommand) -> None: ...

    def activate_subscription(self, command: BillingCommand) -> None: ...

    def mark_provision_pending(self, command: BillingCommand, error: str) -> None: ...

    def mark_provision_failed(self, command: BillingCommand, error: str) -> None: ...


def confirm_payment(order: Order, capacity: CapacityDTO, state: OrderState) -> None:
    """Mark payment only after the hard capacity guard succeeds."""
    try:
        ensure_capacity(capacity, order.quota_gb)
    except CapacityExceededError as exc:
        state.reject_capacity(order.order_id, str(exc))
        raise
    state.mark_paid(order.order_id)


def apply_paid_billing_change(command: BillingCommand, state: OrderWorkflowState) -> None:
    """Apply the selected non-purchase DB transition after payment is durable."""
    if command.subscription_id is None:
        raise ValueError("A billing change requires a target subscription")
    if command.order_type is BillingOrderType.RENEWAL:
        state.apply_renewal(command)
    elif command.order_type is BillingOrderType.UPGRADE:
        state.apply_upgrade(command)
    elif command.order_type is BillingOrderType.ADDON:
        if command.addon_bytes <= 0:
            raise ValueError("Add-on bytes must be positive")
        state.apply_addon(command)
    else:
        raise ValueError("Purchase orders are handled by the provisioning workflow")


def activate_paid_purchase(command: BillingCommand, state: OrderWorkflowState) -> None:
    """Close the DB-side purchase transition after all external steps succeed."""
    if command.order_type is not BillingOrderType.PURCHASE:
        raise ValueError("Only purchase orders can be activated by provisioning")
    state.activate_subscription(command)


def fail_paid_purchase(
    command: BillingCommand, error: str, state: OrderWorkflowState
) -> None:
    """Persist the paid-but-unusable terminal state after external compensation."""
    state.mark_provision_failed(command, error)
