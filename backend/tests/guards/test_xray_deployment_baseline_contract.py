from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STACK_UP = ROOT / "deploy" / "lib" / "40_stack_up.sh"
RENDERER = ROOT / "ops" / "gateway" / "render_xray_routes.py"


def test_deployment_promotes_only_after_compose_and_xray_verification() -> None:
    source = STACK_UP.read_text(encoding="utf-8")

    main = source.index("main() {")
    render = source.index("python -m ops.gateway.render_xray_routes", main)
    compose_up = source.index("compose up --detach", main)
    promotion = source.index('verify_and_promote_xray "$expected_sha"', main)
    verification = source[source.index("verify_and_promote_xray()") : main]

    assert render < compose_up < promotion
    assert "--expected-sha \"$expected_sha\"" in source
    assert "port_is_listening 8443" in verification
    assert '"$health" != healthy' in verification


def test_standalone_renderer_reports_candidate_fingerprint_without_promoting() -> None:
    source = RENDERER.read_text(encoding="utf-8")

    assert "store.save" not in source
    assert "expected_repo_owned_sha=" in source
    assert "write_rendered_config(rendered), repo_owned_xray_fingerprint(rendered)" in source

