from backend.app.main import app, application_name


def test_application_name() -> None:
    assert application_name() == "lingsway-platform"


def test_health_route_is_registered() -> None:
    assert any(getattr(route, "path", None) == "/health" for route in app.routes)
