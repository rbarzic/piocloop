"""Smoke tests for the Textual view.

These do not re-test loop behaviour (that lives in test_loop.py); they verify
the view mounts, renders every state, and wires keys to the engine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from piocloop.loop import (
    STATE_COMPLETE,
    STATE_ERROR,
    STATE_PAUSING,
    STATE_READY,
    STATE_RUNNING,
    STATE_STALLED,
    LoopConfig,
)
from piocloop.tui import PiloopApp

FAKE_PI = str(Path(__file__).parent / "fake_pi.py")


@pytest.fixture
def config(tmp_path: Path) -> LoopConfig:
    plan = tmp_path / "PLAN.md"
    plan.write_text("- [ ] task one\n- [ ] task two\n", encoding="utf-8")
    prompt = tmp_path / ".loop-prompt.md"
    prompt.write_text("Work on {{PLAN_FILE}}\n", encoding="utf-8")
    return LoopConfig(
        prompt_file=prompt,
        plan_file=plan,
        argv=[sys.executable, FAKE_PI, "planworker"],
        soft_poll_interval=0.2,
        command_timeout=0.3,
        max_iterations=5,
    )


async def test_app_mounts_and_runs_a_plan(config: LoopConfig, tmp_path: Path):
    app = PiloopApp(config, model="fake/model", auto_run=True)
    async with app.run_test() as pilot:
        for _ in range(200):
            await pilot.pause()
            if app.engine.state == STATE_COMPLETE:
                break
        assert app.engine.state == STATE_COMPLETE
        assert "<plan-complete>" in config.plan_file.read_text(encoding="utf-8")
        await pilot.press("q")


async def test_header_renders_in_every_state(config: LoopConfig):
    """A missing icon/colour entry would blow up mid-run, so render them all."""
    app = PiloopApp(config, model="fake/model")
    async with app.run_test() as pilot:
        await pilot.pause()
        for state in (
            STATE_READY, STATE_RUNNING, STATE_PAUSING, STATE_STALLED,
            STATE_COMPLETE, STATE_ERROR,
        ):
            app.engine.state = state
            header = app._build_header()
            assert state.upper() in header
        await pilot.press("q")


async def test_agent_output_cannot_break_rich_markup(config: LoopConfig):
    """Agent text containing brackets must not be parsed as markup."""
    app = PiloopApp(config, model="fake/model")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.log_line("ai", "wrote [bold]not markup[/bold] and [BLOCKED: x]")
        await pilot.pause()
        await pilot.press("q")


async def test_start_key_starts_the_loop(config: LoopConfig):
    app = PiloopApp(config, auto_run=False)
    async with app.run_test() as pilot:
        for _ in range(100):
            await pilot.pause()
            if app.engine.state == STATE_READY:
                break
        assert app.engine.state == STATE_READY

        await pilot.press("s")
        for _ in range(100):
            await pilot.pause()
            if app.engine.iteration > 0:
                break
        assert app.engine.iteration > 0
        await pilot.press("q")


async def test_log_file_is_written(config: LoopConfig, tmp_path: Path):
    log_file = tmp_path / "run.log"
    app = PiloopApp(config, auto_run=True, log_file=log_file)
    async with app.run_test() as pilot:
        for _ in range(200):
            await pilot.pause()
            if app.engine.state == STATE_COMPLETE:
                break
        await pilot.press("q")
    assert log_file.exists()
    contents = log_file.read_text(encoding="utf-8")
    assert "[start]" in contents
    assert "Iteration 1" in contents
