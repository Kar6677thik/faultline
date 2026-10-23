"""Use real importlib metadata and imports; unselected registrations never execute."""

import csv
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from faultline.adapter_loader import resolve_adapter
from faultline.adapters import AdapterError
from faultline.cli import app
from faultline.engine import run_one
from faultline.models import digest, load_document
from faultline.projects import Project

ROOT = Path(__file__).resolve().parents[1]
MODULE = "faultline_example_orders"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Install a minimal RECORD-based layout; Docker tests build/install a real wheel."""
    site = tmp_path / "site"
    source = site / MODULE / "__init__.py"
    source.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "examples/installed-orders-adapter/src" / MODULE / "__init__.py", source)
    info = site / "faultline_example_orders-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Name: faultline-example-orders\nVersion: 0.1.0\n")
    (info / "entry_points.txt").write_text(
        "[faultline.adapters]\n"
        "example_orders = faultline_example_orders:ExampleOrdersAdapter\n"
        "incomplete_orders = faultline_example_orders:IncompleteOrdersAdapter\n"
        "unused_orders = missing_unused_module:Factory\n"
    )
    record = info / "RECORD"
    with record.open("w", newline="") as stream:
        writer = csv.writer(stream)
        for path in sorted(site.rglob("*")):
            if path.is_file():
                writer.writerow([path.relative_to(site).as_posix(), "", ""])
    monkeypatch.syspath_prepend(str(site))
    raw = load_document(ROOT / "examples/orders/faultline.project.yaml")
    raw["adapter"]["entry_point"] = "example_orders"
    raw["scenario_paths"] = []
    project = Project.model_validate(raw)
    try:
        yield site, source, info, project
    finally:
        sys.modules.pop(MODULE, None)


def test_installed_adapter_uses_real_registration_and_composite_identity(installed, scenario):
    _site, source, _info, project = installed
    adapter, identity = resolve_adapter(project, scenario, "correct")
    assert adapter.description.adapter_id == "example_orders"
    assert identity["package"] == "faultline-example-orders"
    assert identity["package_version"] == "0.1.0"
    assert identity == resolve_adapter(project, scenario, "correct")[1]
    assert identity["content_hash"] == digest(
        {
            "files": identity["files"],
            "engine_content_hash": identity["engine"]["content_hash"],
            "entry": identity["factory"],
        }
    )
    assert "missing_unused_module" not in sys.modules
    source.write_text(source.read_text() + "\n# Changed installed bytes.\n")
    assert (
        identity["content_hash"] != resolve_adapter(project, scenario, "correct")[1]["content_hash"]
    )


@pytest.mark.parametrize("failure", ["missing", "version", "duplicate", "invalid_factory"])
def test_selection_errors_reject_before_import_or_docker(installed, scenario, monkeypatch, failure):
    site, _source, info, project = installed
    raw = project.configuration()
    if failure == "missing":
        raw["adapter"]["entry_point"] = "absent_orders"
    elif failure == "version":
        raw["adapter"]["package_version"] = "9.0.0"
    elif failure == "duplicate":
        shutil.copytree(info, site / "duplicate-0.1.0.dist-info")
    else:
        (info / "entry_points.txt").write_text(
            "[faultline.adapters]\nexample_orders = arbitrary.module:factory [extra]\n"
        )
    preflight = Mock(side_effect=AssertionError("must not contact Docker"))
    monkeypatch.setattr("faultline.engine.client_and_preflight", preflight)
    with pytest.raises(ValueError):
        run_one(scenario, "correct", site / "output", project=Project.model_validate(raw))
    preflight.assert_not_called()
    assert MODULE not in sys.modules
    assert not (site / "output").exists()


@pytest.mark.parametrize("bad_file", ["../escape.py", "editable.pth", "native.so", "extra.py"])
def test_unverifiable_inventory_rejects_before_import(installed, bad_file):
    site, _source, info, project = installed
    with (info / "RECORD").open("a") as stream:
        stream.write(f"{bad_file},,\n")
    if bad_file == "extra.py":
        (site / bad_file).symlink_to(info / "METADATA")
    with pytest.raises(AdapterError, match="provenance"):
        resolve_adapter(project)
    assert MODULE not in sys.modules


def test_fifo_and_oversized_inventory_do_not_block_or_import(installed):
    site, source, _info, project = installed
    source.unlink()
    os.mkfifo(source)
    started = time.monotonic()
    with pytest.raises(AdapterError, match="provenance"):
        resolve_adapter(project)
    assert time.monotonic() - started < 2
    source.unlink()
    source.write_bytes(b"x" * 1048577)
    with pytest.raises(AdapterError, match="provenance"):
        resolve_adapter(project)
    assert MODULE not in sys.modules


def test_two_installed_copies_of_same_distribution_are_ambiguous(installed, monkeypatch, tmp_path):
    _site, _source, info, project = installed
    other = tmp_path / "other-site"
    shutil.copytree(info, other / info.name)
    monkeypatch.syspath_prepend(str(other))
    with pytest.raises(ValueError, match="exactly one"):
        resolve_adapter(project)
    assert MODULE not in sys.modules


def test_shadowed_import_is_not_attributed_to_another_installed_source(
    installed, monkeypatch, tmp_path
):
    _site, source, _info, project = installed
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    shutil.copyfile(source, shadow / f"{MODULE}.py")
    monkeypatch.syspath_prepend(str(shadow))
    with pytest.raises(AdapterError, match="factory failed"):
        resolve_adapter(project)


def test_missing_record_or_source_is_not_a_valid_partial_inventory(installed):
    _site, source, info, project = installed
    source.unlink()
    with pytest.raises(AdapterError, match="provenance"):
        resolve_adapter(project)
    (info / "RECORD").unlink()
    with pytest.raises(AdapterError, match="provenance"):
        resolve_adapter(project)


def test_builtin_name_cannot_be_shadowed_by_a_distribution(installed):
    _site, source, info, project = installed
    source.write_text("raise AssertionError('must never import a reserved-name plugin')\n")
    (info / "entry_points.txt").write_text(
        "[faultline.adapters]\nbundled_orders = faultline_example_orders:ExampleOrdersAdapter\n"
    )
    raw = project.configuration()
    raw["adapter"]["entry_point"] = "bundled_orders"
    adapter, identity = resolve_adapter(Project.model_validate(raw))
    assert adapter.description.adapter_id == "bundled_orders"
    assert identity["package"] == "faultline"
    assert MODULE not in sys.modules


@pytest.mark.parametrize(
    ("replacement", "hook"),
    [
        ("raise RuntimeError('secret-test-token')", "load"),
        ("return object()", "describe"),
        (
            "return type('Missing', (), {'describe': lambda self: OrdersAdapter().describe()})()",
            "validate",
        ),
        ("return OrdersAdapter()", "describe"),
    ],
)
def test_factory_and_contract_errors_are_scrubbed(installed, replacement, hook):
    _site, source, _info, project = installed
    source.write_text(
        "from faultline.adapters import OrdersAdapter\n"
        f"def ExampleOrdersAdapter():\n    {replacement}\n"
    )
    with pytest.raises(AdapterError) as error:
        resolve_adapter(project)
    assert error.value.hook == hook
    assert "secret-test-token" not in json.dumps(error.value.diagnostic())


@pytest.mark.parametrize("phase", ["import", "describe", "validate"])
def test_preflight_alarm_bounds_installed_code_and_restores_handler(
    installed, monkeypatch, scenario, phase
):
    _site, source, _info, project = installed
    text = source.read_text()
    if phase == "import":
        text = "import time\ntime.sleep(30)\n" + text
    elif phase == "describe":
        text = text.replace("def describe(self):", "def describe(self):\n        time.sleep(30)")
        text = "import time\n" + text
    else:
        text += "\nExampleOrdersAdapter.validate = lambda *args: __import__('time').sleep(30)\n"
    source.write_text(text)
    monkeypatch.setattr("faultline.adapter_loader.PREFLIGHT_SECONDS", 0.1)
    previous = signal.getsignal(signal.SIGALRM)
    with pytest.raises(AdapterError) as error:
        resolve_adapter(project, scenario)
    assert error.value.code == "adapter_preflight_timeout"
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)


def test_keyboard_interrupt_is_not_relabelled_as_plugin_error(installed):
    _site, source, _info, project = installed
    source.write_text("raise KeyboardInterrupt\n")
    with pytest.raises(KeyboardInterrupt):
        resolve_adapter(project)


def test_project_parse_is_inert_but_validate_and_doctor_check_adapter(installed, monkeypatch):
    site, _source, _info, project = installed
    path = site / "faultline.project.yaml"
    path.write_text(json.dumps(project.configuration()))
    shutil.copyfile(ROOT / "examples/orders/control.yaml", site / "control.yaml")
    monkeypatch.setenv("DOCKER_HOST", "tcp://unavailable:2375")
    result = CliRunner().invoke(
        app, ["--project", str(path), "--format", "json", "validate", "control.yaml"]
    )
    assert result.exit_code == 0, result.output
    assert (
        json.loads(result.stdout)["summary"]["adapter_identity"]["entry_point"] == "example_orders"
    )
    raw = project.configuration()
    raw["adapter"]["package_version"] = "9.0.0"
    path.write_text(json.dumps(raw))
    preflight = Mock(side_effect=AssertionError("must not contact Docker"))
    monkeypatch.setattr("faultline.cli.client_and_preflight", preflight)
    result = CliRunner().invoke(app, ["--project", str(path), "doctor"])
    assert result.exit_code == 2, result.output
    preflight.assert_not_called()
