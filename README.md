# piocloop

A Python TUI that orchestrates the [PI coding agent](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
to execute tasks from a `PLAN.md` file iteratively, one session at a time.

piocloop is a port of [pyocloop](https://github.com/rbarzic/pyocloop) — same
concept, but it drives `pi --mode rpc` over stdin/stdout JSONL instead of an
OpenCode HTTP server plus SSE stream. That change, plus a real watchdog, fixes
the hangs documented in [`ANALYSIS.md`](ANALYSIS.md).

## How it works

1. piocloop starts a `pi --mode rpc` subprocess
2. On each iteration it starts a fresh session, sends your loop prompt (with the
   plan file path injected), and waits for `agent_settled`
3. PI reads the plan, executes the next task, marks it `[x]`, and appends
   `<plan-complete>` when everything is done
4. The TUI shows live progress: task counter, progress bar, current task, token
   count, cost, and elapsed/average time per iteration
5. The loop stops when PI writes `<plan-complete>` — or when the watchdog, an
   error streak, a stall, or `--max-iterations` intervenes

## Requirements

- Python 3.11+
- `pi` on your PATH, configured with a provider (developed against pi 0.82.1)

```bash
npm install -g @earendil-works/pi-coding-agent
```

## Installation

```bash
git clone https://github.com/rbarzic/piocloop
cd piocloop
pip install .
```

## Usage

```bash
piloop doctor                    # check pi is installed and reachable
piloop bootstrap .               # create starter PLAN.md and .loop-prompt.md
piloop run --model zai/glm-5.2
```

### `piloop run`

| Option | Default | Meaning |
|---|---|---|
| `-m, --model` | pi's default | Model pattern or `provider/id` |
| `--thinking` | pi's default | `off\|minimal\|low\|medium\|high\|xhigh\|max` |
| `--plan` | `PLAN.md` | Plan file |
| `--prompt` | `.loop-prompt.md` | Loop prompt file |
| `-r, --run` | off | Start iterating immediately |
| `--max-iterations` | 100 | Hard stop |
| `--iteration-timeout` | 1800 | Seconds before an iteration is aborted |
| `--max-stalls` | 3 | Stop after N iterations with no plan progress (0 disables) |
| `--dialog-policy` | `cancel` | How to answer blocking extension dialogs |
| `--session-dir`, `--no-session` | — | Passed through to pi |
| `--tools`, `--exclude-tools` | — | Passed through to pi |
| `--append-system-prompt`, `--skill` | — | Passed through to pi (repeatable) |
| `--approve / --no-approve` | pi's default | Project-local file trust |
| `--verbose`, `--log`, `--debug` | — | Diagnostics |

Keys: `S` start · `Space` pause · `A` abort current iteration · `R` retry ·
`Q` quit.

## Plan format

```markdown
- [ ] a task to do
- [x] a completed task
- [MANUAL] something a human must do — never attempted, excluded from progress
- [BLOCKED: reason] something the agent could not finish
```

The run ends when the agent appends, at the start of a line:

```
<plan-complete>summary of what was done</plan-complete>
```

## Agent skill (optional)

`skills/piocloop-setup/` is an [Agent Skills](https://agentskills.io/specification)
package you can copy into your own agent so it can set up piocloop runs for you —
turning "for each of these 40 items, do X" into a correctly formatted `PLAN.md`
and `.loop-prompt.md`. It is not loaded automatically; install it only if you
want it:

```bash
cp -r skills/piocloop-setup ~/.pi/agent/skills/     # PI
cp -r skills/piocloop-setup ~/.claude/skills/       # Claude Code
```

See [`skills/README.md`](skills/README.md) for other harnesses and for pointing
`pi` at this checkout without copying.

## Differences from pyocloop

| | pyocloop | piocloop |
|---|---|---|
| Agent | OpenCode | PI |
| Transport | HTTP + SSE on port 4096 | stdin/stdout JSONL |
| "Iteration done" signal | `session.idle` SSE event | `agent_settled` RPC event |
| Idle fallback | polled a field that does not exist | polls `get_state().isStreaming` |
| Iteration timeout | none | `--iteration-timeout`, then abort, then respawn |
| Iteration cap | none | `--max-iterations` |
| Progress stalls | undetected | `STALLED` state |
| Repeated errors | looped forever | error-streak circuit breaker |
| Blocking agent dialogs | n/a | auto-answered (`--dialog-policy`) |
| Tests | none | 119 |

`-a/--agent` and `-p/--port` are gone: PI has no named-agent concept (use
`--append-system-prompt` / `--skill`) and no server to bind.

## Why the rewrite is more reliable

[`ANALYSIS.md`](ANALYSIS.md) documents the pyocloop defects that motivated this
port — verified against a live OpenCode server, not inferred. The two that
caused silent hangs:

* the server's stdout pipe was never drained after startup, so the OS pipe
  eventually filled and the server blocked in `write()` and stopped serving;
* the loop's idle wait had no deadline, and its polling fallback tested a
  `session.idle` field that OpenCode does not return — so a single missed SSE
  event hung the run forever.

piocloop's design rules ([`DESIGN.md`](DESIGN.md) §4) are the direct response:
**every pipe gets a drainer, every wait gets a deadline, and lack of progress is
a first-class state.** The test suite mutation-checks the first two — removing
either fix makes the corresponding regression test hang.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q
```

Tests run against `tests/fake_pi.py`, a stub agent — no network, no API key and
no LLM required.

## License

MIT
