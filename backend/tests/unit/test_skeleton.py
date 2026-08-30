from backend.app.main import application_name


def test_application_name() -> None:
    assert application_name() == "lingsway-platform"
