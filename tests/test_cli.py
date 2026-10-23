import json
from pathlib import Path

from typer.testing import CliRunner

from faultline.cli import app

ROOT = Path(__file__).resolve().parents[1]


def test_validate_json_does_not_require_docker(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://unavailable:2375")
    result = CliRunner().invoke(
        app, ["--format", "json", "validate", str(ROOT / "examples/orders/control.yaml")]
    )
    assert result.exit_code == 0
    assert '"cli_schema_version": 1' in result.stdout
    description = json.loads(result.stdout)["summary"]["adapter"]
    assert description["adapter_id"] == "bundled_orders"
    assert description["faults"] == ["response_suppress"]
    assert description["external_registration"] is False


def test_unsupported_scope_is_explicit_and_secrets_not_echoed(tmp_path):
    source = (ROOT / "examples/orders/control.yaml").read_text()
    source = source.replace(
        "Idempotency-Key: purchase-a-key",
        "Idempotency-Key: purchase-a-key\n        Authorization: secret-test-token",
    )
    path = tmp_path / "invalid.yaml"
    path.write_text(source)
    result = CliRunner().invoke(app, ["--format", "json", "validate", str(path)])
    assert result.exit_code == 2
    assert "secret-test-token" not in result.stdout
    assert "Idempotency-Key" in result.stdout
