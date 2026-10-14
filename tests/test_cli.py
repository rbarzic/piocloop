"""CLI-level tests for the Firejail options.

`firejail` itself must never be invoked; the `--firejail` error paths are
exercised by pretending it is (or is not) on PATH.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import piocloop.cli as cli_module
from piocloop.cli import app

runner = CliRunner()


def test_firejail_config_without_firejail_errors(tmp_path):
    cfg = tmp_path / "c.jsonc"
    cfg.write_text("{}")
    result = runner.invoke(app, ["run", "--debug", "--firejail-config", str(cfg)])
    assert result.exit_code != 0
    assert "requires --firejail" in result.output


def test_firejail_with_firejail_missing_from_path_errors(monkeypatch):
    real_which = cli_module.shutil.which

    def fake_which(name: str):
        if name == "firejail":
            return None
        return real_which(name)

    monkeypatch.setattr(cli_module.shutil, "which", fake_which)
    result = runner.invoke(app, ["run", "--debug", "--firejail", "--pi-bin", sys.executable])
    assert result.exit_code != 0
    assert "firejail" in result.output.lower()
    assert "PATH" in result.output


@pytest.mark.parametrize("args,needle", [
    (["--run-window", "PH off"], "Unsupported window"),
    (["--pause-window", "sunrise-sunset"], "Unsupported window"),
    (["--run-window", "not a window"], "Unsupported window"),
    (["--run-window", "24:00-06:00"], "Invalid start time"),
    (["--pause-window", "09:00-24:01"], "Invalid end time"),
    (["--run-window", "09:00-09:00"], "Equal start/end"),
    (["--schedule-timezone", "Invalid/Timezone"], "Unknown schedule timezone"),
])
def test_schedule_validation_precedes_pi_startup(monkeypatch, args, needle):
    def unexpected_probe(*args, **kwargs):
        pytest.fail("Pi must not be probed for an invalid schedule")

    monkeypatch.setattr(cli_module, "_pi_version", unexpected_probe)
    result = runner.invoke(app, ["run", "--debug", "--pi-bin", sys.executable, *args])
    assert result.exit_code == 2
    assert needle in result.output


def test_cli_passes_independent_windows_and_timezone(monkeypatch):
    import piocloop.tui as tui_module

    captured = {}

    def fake_app(config, **kwargs):
        captured["config"] = config
        captured.update(kwargs)
        return SimpleNamespace(run=lambda: None)

    monkeypatch.setattr(tui_module, "PiloopApp", fake_app)
    monkeypatch.setattr(cli_module, "_pi_version", lambda _: "0.82.1")
    result = runner.invoke(app, [
        "run", "--debug", "--pi-bin", sys.executable, "--run",
        "--run-window", "Fr 22:00-24:00", "--run-window", "Sa 00:00-06:00",
        "--pause-window", "02:00-03:00", "--pause-window", "04:00-05:00",
        "--schedule-timezone", "Europe/Paris",
    ])
    assert result.exit_code == 0, result.output
    schedule = captured["config"].schedule
    assert schedule.run_windows == ("Fr 22:00-24:00", "Sa 00:00-06:00")
    assert schedule.pause_windows == ("02:00-03:00", "04:00-05:00")
    assert schedule.timezone.key == "Europe/Paris"
    assert captured["auto_run"]
    assert schedule.evaluate(datetime(2026, 7, 17, 22, tzinfo=timezone.utc)).allowed


def test_cli_without_windows_preserves_unscheduled_config(monkeypatch):
    import piocloop.tui as tui_module

    captured = {}

    def fake_app(config, **kwargs):
        captured["config"] = config
        return SimpleNamespace(run=lambda: None)

    monkeypatch.setattr(tui_module, "PiloopApp", fake_app)
    monkeypatch.setattr(cli_module, "_pi_version", lambda _: "0.82.1")
    result = runner.invoke(app, ["run", "--debug", "--pi-bin", sys.executable])
    assert result.exit_code == 0, result.output
    assert captured["config"].schedule is None


def test_run_help_documents_schedule_options():
    result = runner.invoke(app, ["run", "--help"], env={"COLUMNS": "160", "TERM": "dumb"})
    assert result.exit_code == 0
    for name in ("--run-window", "--pause-window", "--schedule-timezone"):
        assert name in result.output
