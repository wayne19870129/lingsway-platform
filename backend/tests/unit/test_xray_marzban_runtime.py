from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest

from backend.app.providers.gateway.xray_file import (
    MarzbanXrayControlError,
    MarzbanXrayRuntime,
)


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(
        self,
        requests: list[tuple[str, str, Mapping[str, object] | None]],
        *,
        response_payload: object,
        request_status: int = 200,
        transport_error: bool = False,
    ) -> None:
        self.requests = requests
        self.response_payload = response_payload
        self.request_status = request_status
        self.transport_error = transport_error

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, *, data: Mapping[str, str]) -> _Response:
        self.requests.append(("POST", url, data))
        return _Response(
            200,
            {"access_token": "secret-bearer", "token_type": "bearer"},
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: Mapping[str, object] | None = None,
    ) -> _Response:
        del headers
        self.requests.append((method, url, json))
        if self.transport_error:
            raise httpx.ConnectError("ambiguous transport", request=httpx.Request(method, url))
        return _Response(self.request_status, self.response_payload)


def _runtime(tmp_path: Path) -> MarzbanXrayRuntime:
    config_path = tmp_path / "xray_config.json"
    config_path.write_text(
        json.dumps({"outbounds": [{"tag": "candidate"}]}), encoding="utf-8"
    )
    return MarzbanXrayRuntime(
        config_path=config_path,
        backup_dir=tmp_path / "backups",
        control_base_url="https://marzban:8000",
        admin_username="admin",
        admin_password="private-password",
        verify_tls=False,
    )


def test_reload_posts_exact_installed_candidate_and_discards_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[tuple[str, str, Mapping[str, object] | None]] = []
    candidate = {"outbounds": [{"tag": "candidate"}]}
    fake = _Client(requests, response_payload=candidate)
    monkeypatch.setattr(
        "backend.app.providers.gateway.xray_file.httpx.Client",
        lambda **_kwargs: fake,
    )

    runtime = _runtime(tmp_path)
    runtime.reload()

    assert requests[0][0:2] == ("POST", "https://marzban:8000/api/admin/token")
    assert requests[0][2] == {
        "username": "admin",
        "password": "private-password",
    }
    assert requests[1][0:2] == ("PUT", "https://marzban:8000/api/core/config")
    assert requests[1][2] == candidate
    assert not hasattr(runtime, "_token")
    assert "private-password" not in repr(runtime)
    assert "secret-bearer" not in repr(runtime)


def test_reload_rejects_response_mismatch_without_exposing_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[tuple[str, str, Mapping[str, object] | None]] = []
    fake = _Client(requests, response_payload={"different": True})
    monkeypatch.setattr(
        "backend.app.providers.gateway.xray_file.httpx.Client",
        lambda **_kwargs: fake,
    )

    with pytest.raises(MarzbanXrayControlError, match="did not match"):
        _runtime(tmp_path).reload()


def test_reload_does_not_retry_transport_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[tuple[str, str, Mapping[str, object] | None]] = []
    fake = _Client(
        requests,
        response_payload={},
        transport_error=True,
    )
    monkeypatch.setattr(
        "backend.app.providers.gateway.xray_file.httpx.Client",
        lambda **_kwargs: fake,
    )

    with pytest.raises(MarzbanXrayControlError, match="transport layer"):
        _runtime(tmp_path).reload()

    assert [method for method, _url, _body in requests] == ["POST", "PUT"]


@pytest.mark.parametrize("started", [True, False])
def test_health_requires_started_core_and_marzban_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, started: bool
) -> None:
    requests: list[tuple[str, str, Mapping[str, object] | None]] = []
    fake = _Client(requests, response_payload={"started": started})
    monkeypatch.setattr(
        "backend.app.providers.gateway.xray_file.httpx.Client",
        lambda **_kwargs: fake,
    )

    class _Socket:
        def __enter__(self) -> _Socket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        "backend.app.providers.gateway.xray_file.socket.create_connection",
        lambda address, timeout: (
            pytest.fail(f"unexpected health address {address!r}")
            if address != ("marzban", 8443)
            else _Socket()
        ),
    )

    report = _runtime(tmp_path).health()

    assert report.healthy is started
    assert requests[1][0:2] == ("GET", "https://marzban:8000/api/core")


def test_health_is_unhealthy_when_marzban_port_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[tuple[str, str, Mapping[str, object] | None]] = []
    fake = _Client(requests, response_payload={"started": True})
    monkeypatch.setattr(
        "backend.app.providers.gateway.xray_file.httpx.Client",
        lambda **_kwargs: fake,
    )
    monkeypatch.setattr(
        "backend.app.providers.gateway.xray_file.socket.create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    assert _runtime(tmp_path).health().healthy is False
