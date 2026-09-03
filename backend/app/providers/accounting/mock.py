from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from backend.app.providers.base import AccountUserDTO, UsageDTO
from backend.app.providers.mock_support import raise_injected


@dataclass(slots=True)
class MockAccountingProvider:
    failures: Mapping[str, Exception] = field(default_factory=dict)
    users: dict[str, AccountUserDTO] = field(default_factory=dict)
    disabled_users: set[str] = field(default_factory=set)
    usage_bytes: dict[str, int] = field(default_factory=dict)

    def create_user(
        self, username: str, quota_bytes: int, expire_at: datetime | None
    ) -> AccountUserDTO:
        raise_injected(self.failures, "create_user")
        user = AccountUserDTO(username, quota_bytes, expire_at)
        self.users[username] = user
        return user

    def disable_user(self, username: str) -> None:
        raise_injected(self.failures, "disable_user")
        self.disabled_users.add(username)
        current = self.users.get(username)
        if current is not None:
            self.users[username] = AccountUserDTO(
                current.username, current.quota_bytes, current.expire_at, enabled=False
            )

    def set_quota(self, username: str, quota_bytes: int) -> None:
        raise_injected(self.failures, "set_quota")
        current = self.users[username]
        self.users[username] = AccountUserDTO(
            current.username, quota_bytes, current.expire_at, current.enabled
        )

    def set_expire(self, username: str, expire_at: datetime) -> None:
        raise_injected(self.failures, "set_expire")
        current = self.users[username]
        self.users[username] = AccountUserDTO(
            current.username, current.quota_bytes, expire_at, current.enabled
        )

    def get_connection_links(self, username: str) -> list[str]:
        raise_injected(self.failures, "get_connection_links")
        return [f"vless://{username}@mock.invalid:443"]

    def get_usage(self, username: str) -> UsageDTO:
        raise_injected(self.failures, "get_usage")
        used_bytes = self.usage_bytes.get(username, 0)
        return UsageDTO(
            tenant_id=username,
            used_gb=Decimal(used_bytes) / Decimal(1024**3),
            measured_at=datetime.now(UTC),
            used_bytes=used_bytes,
            status="disabled" if username in self.disabled_users else "active",
        )

    def set_mock_usage(self, username: str, used_bytes: int) -> None:
        self.usage_bytes[username] = used_bytes
