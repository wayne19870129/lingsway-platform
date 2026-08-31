from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from backend.app.providers.base import AccountUserDTO
from backend.app.providers.mock_support import raise_injected


@dataclass(slots=True)
class MockAccountingProvider:
    failures: Mapping[str, Exception] = field(default_factory=dict)
    users: dict[str, AccountUserDTO] = field(default_factory=dict)
    disabled_users: set[str] = field(default_factory=set)

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

    def get_connection_links(self, username: str) -> list[str]:
        raise_injected(self.failures, "get_connection_links")
        return [f"vless://{username}@mock.invalid:443"]

    def get_usage(self, username: str) -> int:
        raise_injected(self.failures, "get_usage")
        return 0
