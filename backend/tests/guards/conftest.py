import socket
import subprocess
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def no_network_docker_or_shell(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def reject(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("guard tests must not use network, Docker, or shell")

    monkeypatch.setattr(socket, "socket", reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(subprocess, "run", reject)
    monkeypatch.setattr(subprocess, "Popen", reject)
    yield
