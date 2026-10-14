"""CLI-level tests for the Firejail options.

`firejail` itself must never be invoked; the `--firejail` error paths are
exercised by pretending it is (or is not) on PATH.
"""

from __future__ import annotations

import sys

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
