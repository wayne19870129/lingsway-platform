from decimal import Decimal

from backend.app.providers.base import CapacityDTO


class CapacityExceededError(RuntimeError):
    def __init__(self, capacity: CapacityDTO, requested_gb: Decimal) -> None:
        self.capacity = capacity
        self.requested_gb = requested_gb
        super().__init__(
            "capacity exceeded: "
            f"allocated={capacity.allocated_gb}GB, requested={requested_gb}GB, "
            f"reserved={capacity.reserved_gb}GB, total={capacity.total_gb}GB"
        )


def ensure_capacity(capacity: CapacityDTO, requested_gb: Decimal) -> None:
    """Enforce allocated + requested + operations reserve <= provider total."""
    if requested_gb <= 0:
        raise ValueError("requested quota must be positive")
    required = capacity.allocated_gb + requested_gb + capacity.reserved_gb
    if required > capacity.total_gb:
        raise CapacityExceededError(capacity, requested_gb)
