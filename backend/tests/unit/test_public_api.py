from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import Table, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.public import login, me, register
from backend.app.core.database import Base
from backend.app.dependencies import get_auth_context, require_admin
from backend.app.schemas.public import CustomerCreate, LoginRequest


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables: list[Table] = [
        Base.metadata.tables["customers"],
        Base.metadata.tables["jwt_sessions"],
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine, tables=tables)


def test_register_login_and_current_customer(db_session: Session) -> None:
    registered = register(
        CustomerCreate(
            email="customer@example.com",
            password="correct horse battery staple",
            nickname="Customer",
        ),
        request=None,  # type: ignore[arg-type]
        db=db_session,
    )
    logged_in = login(
        LoginRequest(email="CUSTOMER@EXAMPLE.COM", password="correct horse battery staple"),
        request=None,  # type: ignore[arg-type]
        db=db_session,
    )
    context = get_auth_context(
        db_session,
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=logged_in.access_token),
    )
    current = me(context.customer)

    assert registered.email == "customer@example.com"
    assert current.email == "customer@example.com"
    assert current.role.value == "CUSTOMER"


def test_customer_role_is_rejected_by_admin_dependency(db_session: Session) -> None:
    register(
        CustomerCreate(email="ordinary@example.com", password="correct horse battery staple"),
        request=None,  # type: ignore[arg-type]
        db=db_session,
    )
    logged_in = login(
        LoginRequest(email="ordinary@example.com", password="correct horse battery staple"),
        request=None,  # type: ignore[arg-type]
        db=db_session,
    )
    context = get_auth_context(
        db_session,
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=logged_in.access_token),
    )

    with pytest.raises(HTTPException) as exc_info:
        require_admin(context.customer)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Admin role required"
