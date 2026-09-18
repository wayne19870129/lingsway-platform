"""TASK-S06: the sellable plan catalogue is one source of truth.

These tests lock down a product decision (which tiers exist, at what price)
and the structural property that made it worth centralising: no consumer may
carry its own copy of the plan-code list.
"""

from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.admin import ADMIN_CAPACITY_PLAN_CODES
from backend.app.api.public import list_plans
from backend.app.catalog import (
    BYTES_PER_GIB,
    PLAN_DURATION_DAYS,
    SELLABLE_PLAN_CODES,
    sellable_plans,
)
from backend.app.core.config import Settings
from backend.app.core.database import Base
from backend.app.models import Plan, PlanStatus

#: The product owner's decision of 2026-09-18. Changing this table is a
#: pricing change and needs the same sign-off the original decision had.
EXPECTED_TIERS = (
    ("PLAN_50GB", 50, Decimal("30.00")),
    ("PLAN_100GB", 100, Decimal("50.00")),
    ("PLAN_200GB", 200, Decimal("80.00")),
    ("PLAN_500GB", 500, Decimal("120.00")),
)


def test_catalogue_matches_the_agreed_tiers_exactly() -> None:
    plans = sellable_plans(Settings())

    assert [(p.plan_code, p.traffic_gb, p.price) for p in plans] == list(EXPECTED_TIERS)


def test_every_tier_is_priced_in_cny_on_a_thirty_day_cycle() -> None:
    for plan in sellable_plans(Settings()):
        assert plan.currency == "CNY"
        # The plans table carries a CHECK constraint fixing this at 30; a
        # catalogue entry that disagreed would be rejected at insert time.
        assert plan.duration_days == PLAN_DURATION_DAYS == 30


def test_traffic_limit_is_exact_binary_gib() -> None:
    plans = {plan.plan_code: plan for plan in sellable_plans(Settings())}

    assert plans["PLAN_50GB"].traffic_limit_bytes == 50 * BYTES_PER_GIB
    assert plans["PLAN_500GB"].traffic_limit_bytes == 500 * BYTES_PER_GIB


def test_prices_come_from_settings_and_are_not_a_second_hardcoded_copy() -> None:
    """The whole point of resolving against Settings is that a deployment can
    reprice without a code change. If the catalogue ever inlined the numbers
    instead, this test would catch it."""
    repriced = replace(
        Settings(), plan_50gb_price=Decimal("33.00"), plan_currency="USD"
    )

    plans = {plan.plan_code: plan for plan in sellable_plans(repriced)}

    assert plans["PLAN_50GB"].price == Decimal("33.00")
    assert plans["PLAN_50GB"].currency == "USD"


def test_withdrawn_tiers_are_not_sellable() -> None:
    """PLAN_300GB and PLAN_1000GB both had orphaned price configuration
    before TASK-S06 (one in .env.example read by nothing, one in Settings
    with no matching plan code). Neither is sold."""
    assert "PLAN_300GB" not in SELLABLE_PLAN_CODES
    assert "PLAN_1000GB" not in SELLABLE_PLAN_CODES
    assert not hasattr(Settings(), "plan_1000gb_price")


def test_admin_capacity_report_uses_the_same_list_as_customers() -> None:
    """Regression guard for the drift this task removed: the admin capacity
    report and the customer plan list used to hardcode the same four codes
    independently."""
    assert ADMIN_CAPACITY_PLAN_CODES is SELLABLE_PLAN_CODES


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [Base.metadata.tables["plans"]]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine, tables=tables)
    engine.dispose()


def _plan(plan_code: str, price: str, status: PlanStatus = PlanStatus.ACTIVE) -> Plan:
    return Plan(
        plan_code=plan_code,
        name=plan_code,
        traffic_limit_bytes=50 * BYTES_PER_GIB,
        duration_days=PLAN_DURATION_DAYS,
        price=Decimal(price),
        currency="CNY",
        route_group_code="DEFAULT",
        status=status,
    )


def test_list_plans_hides_active_plans_outside_the_catalogue(db_session: Session) -> None:
    """The filtering moved from each frontend page into the endpoint, so an
    ACTIVE row outside the catalogue -- a legacy tier, a migration artefact,
    the withdrawn PLAN_1000GB -- must never be offered for purchase."""
    db_session.add_all(
        [
            _plan("PLAN_50GB", "30.00"),
            _plan("PLAN_500GB", "120.00"),
            _plan("PLAN_1000GB", "200.00"),
            _plan("LEGACY_TIER", "10.00"),
        ]
    )
    db_session.flush()

    offered = {plan.plan_code for plan in list_plans(db_session)}

    assert offered == {"PLAN_50GB", "PLAN_500GB"}


def test_list_plans_still_hides_inactive_catalogue_plans(db_session: Session) -> None:
    """Narrowing by plan_code must not have replaced the ACTIVE check."""
    db_session.add_all(
        [
            _plan("PLAN_50GB", "30.00"),
            _plan("PLAN_100GB", "50.00", status=PlanStatus.INACTIVE),
        ]
    )
    db_session.flush()

    assert {plan.plan_code for plan in list_plans(db_session)} == {"PLAN_50GB"}
