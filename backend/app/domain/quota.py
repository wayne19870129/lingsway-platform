from decimal import Decimal

BYTES_PER_GIB = 1024**3


def quota_gb_to_bytes(quota_gb: Decimal) -> int:
    """Convert sold quota one-to-one, without hidden buffer or overbooking."""
    if quota_gb <= 0:
        raise ValueError("quota must be positive")
    exact_bytes = quota_gb * BYTES_PER_GIB
    if exact_bytes != exact_bytes.to_integral_value():
        raise ValueError("quota cannot represent a whole number of bytes")
    return int(exact_bytes)
