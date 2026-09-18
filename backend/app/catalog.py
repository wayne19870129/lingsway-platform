"""The sellable plan catalogue -- one source of truth for what is on sale.

Before TASK-S06 the list of sellable ``plan_code`` values was hardcoded in
three unrelated places (``api/admin.py``'s capacity report and both customer
frontend pages), and the ``plan_*_price`` fields on :class:`Settings` were
read by nothing at all -- real prices existed only as rows in the ``plans``
table, so the settings were decorative. This module is the single place that
answers "what do we sell, and for how much".

Prices and currency still come from :class:`Settings` rather than being
literals here, so a deployment can reprice without a code change. The plan
*identity* (code, name, traffic size) is fixed here, because changing which
tiers exist is a product decision, not a deployment knob.

**This module does not create ``Plan`` rows.** ``deploy/lib/60_seed.sh``
deliberately refuses implicit seed data, so nothing in this repository writes
the catalogue to the database yet -- see TASK-S06's constraints and
``docs/30-backup-restore.md``-adjacent gaps in
``docs/83-project-continuity.md`` section 8.
"""

from dataclasses import dataclass
from decimal import Decimal

from backend.app.core.config import Settings

#: Fixed by the ``ck_plan_fixed_30_day_duration`` CHECK constraint on
#: ``plans`` (migration 0016) -- every plan is a 30-day cycle.
PLAN_DURATION_DAYS = 30

BYTES_PER_GIB = 1024**3


@dataclass(frozen=True, slots=True)
class PlanSpec:
    """One sellable tier, in the shape a ``Plan`` row needs."""

    plan_code: str
    name: str
    traffic_gb: int
    price: Decimal
    currency: str
    duration_days: int = PLAN_DURATION_DAYS

    @property
    def traffic_limit_bytes(self) -> int:
        return self.traffic_gb * BYTES_PER_GIB


#: Tier identity, in display order. Prices are resolved per-deployment by
#: :func:`sellable_plans`; only the identity is fixed here.
#: Decided by the product owner on 2026-09-18: four tiers, 30-day cycle.
#: 300GB and 1000GB are explicitly NOT sold -- both previously had orphaned
#: price configuration (``PLAN_300GB_PRICE`` in ``.env.example`` was read by
#: nothing, ``plan_1000gb_price`` had no matching plan code anywhere).
_TIERS: tuple[tuple[str, str, int, str], ...] = (
    ("PLAN_50GB", "50GB 月付", 50, "plan_50gb_price"),
    ("PLAN_100GB", "100GB 月付", 100, "plan_100gb_price"),
    ("PLAN_200GB", "200GB 月付", 200, "plan_200gb_price"),
    ("PLAN_500GB", "500GB 月付", 500, "plan_500gb_price"),
)

#: The only ``plan_code`` values a customer may buy. Every consumer -- the
#: ``/plans`` endpoint, the admin capacity report, both frontend pages --
#: derives its filter from this rather than repeating the list.
SELLABLE_PLAN_CODES: tuple[str, ...] = tuple(code for code, _, _, _ in _TIERS)


def sellable_plans(settings: Settings) -> tuple[PlanSpec, ...]:
    """Resolve the catalogue against one :class:`Settings` instance.

    Pure: no I/O, no database access, no global state. Callers that need
    ``Plan`` rows are responsible for creating them; this only says what
    those rows should contain.
    """
    return tuple(
        PlanSpec(
            plan_code=code,
            name=name,
            traffic_gb=traffic_gb,
            price=getattr(settings, price_field),
            currency=settings.plan_currency,
        )
        for code, name, traffic_gb, price_field in _TIERS
    )
