"""CLI entry point for piocloop."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

import typer

app = typer.Typer(
    name="piloop",
    help="piocloop — orchestrate the PI coding agent to execute tasks from a PLAN.md file iteratively.",
    add_completion=False,
    no_args_is_help=True,
)

TESTED_PI_VERSIONS = ("0.82",)

_PLAN_TEMPLATE = """\
# Project Plan

## Overview

Describe the goal of this project here.

## Backlog

### Phase 1

- [ ] First task description
- [ ] Second task description
- [ ] Third task description
"""

_PROMPT_TEMPLATE = """\
Execute the next task from {{PLAN_FILE}}.

Before starting:
1. Read {{PLAN_FILE}} fully

Task selection (CRITICAL):
- Work through phases IN ORDER — complete Phase N before starting Phase N+1
- Pick the FIRST uncompleted task in the earliest incomplete phase
- Skip [MANUAL] and [BLOCKED] items
- NEVER batch tasks across different phases

Execute:
1. Apply the requested changes

After completion:
1. Update {{PLAN_FILE}} marking completed items with [x]

2. If you cannot complete a task (permissions, external service, needs human input):
   - Add [BLOCKED: reason] to that task line in {{PLAN_FILE}}
   - Continue with other tasks

Completion check:
- If all non-[MANUAL] tasks are either [x] or [BLOCKED]:
  - Append `<plan-complete>SUMMARY_OF_WORK_DONE_AND_REMAINING_MANUAL_TASKS</plan-complete>`
    to the end of {{PLAN_FILE}}, at the start of a line
  - Stop
- Do NOT skip automatable tasks — if a task seems hard but doable, attempt it
"""


def _pi_version(pi_bin: str) -> Optional[str]:
    try:
        out = subprocess.run(
            [pi_bin, "--version"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 else None


@app.command()
def run(
    model: Optional[str] = typer.Option(
        None, "-m", "--model",
        help="Model pattern or provider/id, e.g. zai/glm-5.2. List with: pi --list-models",
    ),
    thinking: Optional[str] = typer.Option(
        None, "--thinking",
        help="Thinking level: off|minimal|low|medium|high|xhigh|max",
    ),
    prompt: Path = typer.Option(".loop-prompt.md", "--prompt", help="Path to loop prompt file"),
    plan: Path = typer.Option("PLAN.md", "--plan", help="Path to plan file"),
    run_now: bool = typer.Option(False, "-r", "--run", help="Start iterations immediately"),
    max_iterations: int = typer.Option(100, "--max-iterations", help="Hard stop after N iterations"),
    iteration_timeout: float = typer.Option(
        1800.0, "--iteration-timeout", help="Seconds before an iteration is aborted"
    ),
    max_stalls: int = typer.Option(
        3, "--max-stalls", help="Stop after N iterations with no plan progress (0 disables)"
    ),
    dialog_policy: str = typer.Option(
        "cancel", "--dialog-policy",
        help="How to answer blocking extension dialogs: cancel|allow|deny",
    ),
    session_dir: Optional[Path] = typer.Option(None, "--session-dir", help="Passed through to pi"),
    no_session: bool = typer.Option(False, "--no-session", help="Do not persist pi sessions"),
    tools: Optional[str] = typer.Option(None, "--tools", help="Comma-separated tool allowlist"),
    exclude_tools: Optional[str] = typer.Option(
        None, "--exclude-tools", help="Comma-separated tool denylist"
    ),
    append_system_prompt: List[str] = typer.Option(
        [], "--append-system-prompt", help="Text or file appended to pi's system prompt (repeatable)"
    ),
    skill: List[str] = typer.Option([], "--skill", help="Skill file or directory (repeatable)"),
    approve: Optional[bool] = typer.Option(
        None, "--approve/--no-approve", help="Trust (or ignore) project-local pi files"
    ),
    pi_bin: str = typer.Option("pi", "--pi-bin", help="Path to the pi executable"),
    debug: bool = typer.Option(False, "-d", "--debug", help="Skip plan/prompt file validation"),
    verbose: bool = typer.Option(False, "--verbose", help="Log every raw RPC event"),
    log: Optional[Path] = typer.Option(None, "--log", help="Append all log entries to this file"),
) -> None:
    """Run the piocloop orchestration loop."""
    directory = os.getcwd()
    prompt_abs = prompt.resolve()
    plan_abs = plan.resolve()

    if dialog_policy not in ("cancel", "allow", "deny"):
        typer.echo(f"Error: --dialog-policy must be cancel|allow|deny, got {dialog_policy!r}", err=True)
        raise typer.Exit(2)

    if shutil.which(pi_bin) is None and not Path(pi_bin).exists():
        typer.echo(f"Error: {pi_bin!r} not found on PATH.", err=True)
        typer.echo("\nInstall it with:  npm install -g @earendil-works/pi-coding-agent\n", err=True)
        raise typer.Exit(1)

    version = _pi_version(pi_bin)
    if version and not any(version.startswith(v) for v in TESTED_PI_VERSIONS):
        typer.echo(
            f"Warning: pi {version} is outside the tested range "
            f"({', '.join(TESTED_PI_VERSIONS)}.x); the RPC protocol may differ.",
            err=True,
        )

    if not debug:
        if not plan_abs.exists():
            typer.echo(f"Error: Plan file not found: {plan_abs}", err=True)
            typer.echo("\nTip: run  piloop bootstrap .  to create starter files.\n", err=True)
            raise typer.Exit(1)
        if not prompt_abs.exists():
            typer.echo(f"Error: Prompt file not found: {prompt_abs}", err=True)
            typer.echo("\nTip: run  piloop bootstrap .  to create starter files.\n", err=True)
            raise typer.Exit(1)

    from .loop import LoopConfig
    from .pi_client import build_argv
    from .tui import PiloopApp

    argv = build_argv(
        pi_bin=pi_bin,
        model=model,
        thinking=thinking,
        session_dir=str(session_dir.resolve()) if session_dir else None,
        no_session=no_session,
        tools=tools,
        exclude_tools=exclude_tools,
        append_system_prompt=tuple(append_system_prompt),
        skills=tuple(skill),
        approve=approve,
    )

    config = LoopConfig(
        prompt_file=prompt_abs,
        plan_file=plan_abs,
        argv=argv,
        cwd=directory,
        dialog_policy=dialog_policy,
        max_iterations=max_iterations,
        iteration_timeout=iteration_timeout,
        max_stalls=max_stalls,
        verbose=verbose,
    )

    PiloopApp(config, model=model, auto_run=run_now, log_file=log).run()


@app.command()
def bootstrap(
    directory: Path = typer.Argument(Path("."), help="Directory to initialise"),
    force: bool = typer.Option(False, "-f", "--force", help="Overwrite existing files"),
) -> None:
    """Create a starter PLAN.md and .loop-prompt.md in DIRECTORY."""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)

    plan_file = directory / "PLAN.md"
    prompt_file = directory / ".loop-prompt.md"

    created, skipped = [], []
    for path, content in [(plan_file, _PLAN_TEMPLATE), (prompt_file, _PROMPT_TEMPLATE)]:
        if path.exists() and not force:
            skipped.append(path.name)
        else:
            path.write_text(content, encoding="utf-8")
            created.append(path.name)

    for name in created:
        typer.echo(f"  created  {directory / name}")
    for name in skipped:
        typer.echo(f"  skipped  {directory / name}  (exists; use --force to overwrite)")

    if created:
        typer.echo("\nNext steps:")
        typer.echo(f"  1. Edit {plan_file} — add your tasks")
        typer.echo(f"  2. Edit {prompt_file} — adjust instructions if needed")
        typer.echo("  3. Run: piloop run --model <provider/model>")


@app.command()
def doctor(
    pi_bin: str = typer.Option("pi", "--pi-bin", help="Path to the pi executable"),
) -> None:
    """Check that pi is installed and reachable."""
    path = shutil.which(pi_bin)
    if path is None:
        typer.echo(f"pi:      NOT FOUND ({pi_bin!r} is not on PATH)")
        raise typer.Exit(1)
    version = _pi_version(pi_bin)
    typer.echo(f"pi:      {path}")
    typer.echo(f"version: {version or 'unknown'}")
    if version and not any(version.startswith(v) for v in TESTED_PI_VERSIONS):
        typer.echo(f"warning: outside tested range ({', '.join(TESTED_PI_VERSIONS)}.x)")
