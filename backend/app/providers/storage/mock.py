from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256

from backend.app.providers.base import BlobDTO
from backend.app.providers.mock_support import raise_injected


@dataclass(slots=True)
class MockBlobStorage:
    failures: Mapping[str, Exception] = field(default_factory=dict)
    objects: dict[str, str] = field(default_factory=dict)

    def put(self, key: str, path: str) -> None:
        raise_injected(self.failures, "put")
        self.objects[key] = path

    def list(self, prefix: str) -> list[BlobDTO]:
        raise_injected(self.failures, "list")
        return [
            BlobDTO(key, len(path.encode()), self._checksum_value(path))
            for key, path in sorted(self.objects.items())
            if key.startswith(prefix)
        ]

    def delete(self, key: str) -> None:
        raise_injected(self.failures, "delete")
        self.objects.pop(key, None)

    def checksum(self, key: str) -> str:
        raise_injected(self.failures, "checksum")
        return self._checksum_value(self.objects[key])

    @staticmethod
    def _checksum_value(value: str) -> str:
        return sha256(value.encode()).hexdigest()
