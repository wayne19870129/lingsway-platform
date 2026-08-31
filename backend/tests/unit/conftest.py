import socket
import subprocess
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def forbid_network_docker_and_shell(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def reject(*args: object, **kwargs: object) -> None:
        raise AssertionError("unit tests must not access network, Docker, or shell")

    monkeypatch.setattr(socket, "socket", reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(subprocess, "run", reject)
    monkeypatch.setattr(subprocess, "Popen", reject)
    yield
