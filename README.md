# piocloop

A Python TUI that orchestrates the [PI coding agent](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
to execute tasks from a `PLAN.md` file iteratively, one session at a time.

piocloop is a port of [pyocloop](https://github.com/rbarzic/pyocloop) — same
concept, but it drives `pi --mode rpc` over stdin/stdout JSONL instead of an
OpenCode HTTP server plus SSE stream. That change, plus a real watchdog, fixes
the hangs documented in [`ANALYSIS.md`](ANALYSIS.md).

## How it works

1. piocloop starts a fresh `pi --mode rpc` subprocess for each iteration
2. It sends your loop prompt (with the plan file path injected) and waits for
   `agent_settled`
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
piloop --version                 # show the piocloop version
piloop doctor                    # show piocloop/pi versions and check pi is reachable
piloop bootstrap .               # create starter PLAN.md and .loop-prompt.md
piloop run --model zai/glm-5.2
```

### Using an OpenRouter model

Set your OpenRouter API key, then prefix the OpenRouter model ID with
`openrouter/`. For example, Kimi K3 is `moonshotai/kimi-k3` on OpenRouter:

```bash
export OPENROUTER_API_KEY="your-openrouter-api-key"
piloop run --model openrouter/moonshotai/kimi-k3
```

You can check that PI's model catalog contains it with:

```bash
pi --list-models openrouter | grep kimi-k3
```

### `piloop run`

| Option | Default | Meaning |
|---|---|---|
| `-m, --model` | pi's default | Model pattern or `provider/id` |
| `--thinking` | pi's default | `off\|minimal\|low\|medium\|high\|xhigh\|max` |
| `--plan` | `PLAN.md` | Plan file |
| `--prompt` | `.loop-prompt.md` | Loop prompt file |
| `-r, --run` | off | Start iterating immediately (subject to schedule) |
| `--run-window` | unrestricted | Weekly OSM window permitting new jobs (repeatable) |
| `--pause-window` | none | Weekly OSM window blocking new jobs (repeatable) |
| `--schedule-timezone` | `UTC` | IANA timezone used for all windows |
| `--max-iterations` | 100 | Hard stop |
| `--iteration-timeout` | 1800 | Seconds before an iteration is aborted |
| `--max-stalls` | 3 | Stop after N iterations with no plan progress (0 disables) |
| `--dialog-policy` | `cancel` | How to answer blocking extension dialogs |
| `--session-dir`, `--no-session` | — | Passed through to pi |
| `--tools`, `--exclude-tools` | — | Passed through to pi |
| `--append-system-prompt`, `--skill` | — | Passed through to pi (repeatable) |
| `--approve / --no-approve` | pi's default | Project-local file trust |
| `--firejail` | off | Run `pi` inside a firejail sandbox |
| `--firejail-config` | — | JSONC file with firejail options (requires `--firejail`) |
| `--verbose`, `--log`, `--debug` | — | Diagnostics |

Keys: `S` start · `Space` pause · `A` abort current iteration · `R` retry ·
`Q` quit.

## Timed execution

Schedule admission of new jobs using a weekly subset of the established
[OpenStreetMap `opening_hours` syntax](https://wiki.openstreetmap.org/wiki/Key:opening_hours/specification).
A job is one loop iteration/Pi session. **Entering a blocked period never
interrupts the current job:** it finishes normally, then the loop waits before
starting the next job. Existing watchdog deadlines and explicit abort controls
still apply.

### Different weekday, Saturday, and Sunday windows

```bash
piloop run --run \
  --run-window 'Mo-Fr 18:00-23:00; Sa 09:00-18:00; Su 12:00-16:00' \
  --schedule-timezone Europe/Paris
```

New jobs may start Monday–Friday 18:00–23:00, Saturday 09:00–18:00, and Sunday
12:00–16:00. Outside these windows the loop waits. Use the same expression with
`--pause-window` to block those periods instead.

### Pause during business hours and maintenance

```bash
piloop run --run \
  --pause-window 'Mo-Fr 09:00-17:00' \
  --pause-window '02:00-03:00' \
  --schedule-timezone Europe/Paris
```

### Overnight windows: no split required

```bash
piloop run --run \
  --run-window '22:00-06:00' \
  --pause-window '02:00-03:00' \
  --schedule-timezone Europe/Paris
```

This permits new jobs overnight, except during maintenance. A weekday selector
names the **starting day**: `Fr 22:00-06:00` runs from Friday 22:00 through
Saturday 06:00, not Friday morning.

If you prefer splitting that window, these independent options are equivalent:

```bash
--run-window 'Fr 22:00-24:00' \
--run-window 'Sa 00:00-06:00'
```

**There is no pause/resume transition, abort, or extra launch caused by crossing
midnight.** Starts are inclusive and ends exclusive: `[start, end)`. `24:00`
means midnight at the end of the selected day and is valid only as an endpoint.
At Saturday 00:00 the second segment already matches as the first ends. Adjacent
pause windows likewise remain continuously blocked. Normal job completion can
still start another job inside a permitted window.

### Combining windows and controls

- Repeated `--run-window` options form a union; with none, running is unrestricted.
- Repeated `--pause-window` options form a union; **pause takes precedence**.
- Each option is evaluated independently. A matching pause expression means
  “pause”; do not append `off`.
- Semicolon rules within one expression follow OSM's rule-override semantics.
  For independent overnight and daytime windows, prefer separate options:
  `--run-window 'Fr 22:00-06:00' --run-window 'Sa 09:00-18:00'`. Saturday
  00:00–06:00 and 09:00–18:00 are allowed; 06:00–09:00 is blocked.
- Starting inside a blocked period waits before the first job. `--run`/Start is
  still required; the schedule does not start a ready loop by itself.
- Space toggles a **separate manual pause**. Neither Space nor Retry bypasses
  scheduling. A manual pause survives the end of a scheduled pause; Retry is
  still required for error, stall, and max-iteration holds.
- If a blocked period ends before the current job finishes, the next job can
  start normally. No stale scheduled-pause latch remains.
- Waiting does not count as an iteration or affect error/stall counters or
  average execution duration. Completion is reported without waiting for the
  next permitted window. Quit interrupts scheduled waiting promptly.

### Timezones and supported syntax

The default is **UTC**, not machine-local time. Select an IANA name such as
`Europe/Paris`; the TUI and logs display the timezone, blocking reason, and next
combined permission time when known. During execution, blocked windows show
`PAUSING`; after the current job finishes they show `PAUSED`.

Windows use local wall time in the selected zone. Both occurrences of a repeated
DST hour are evaluated, while nonexistent spring-forward times do not create
fictitious execution opportunities. The scheduler reevaluates current permission
after clock changes or machine suspension instead of replaying missed triggers.

Supported expressions use explicit `HH:MM-HH:MM` ranges, weekday lists/ranges
(`Mo,We,Fr`, `Mo-Fr`), comma-separated time ranges, day-specific semicolon rules,
overnight ranges, and `24/7`. Equal start/end times are rejected: use
`00:00-24:00` for a full day. Expressions are limited to 4096 characters.
Holiday/date selectors, solar times, comments/modifiers (including `off`), and
uncertain/open-ended ranges are not supported and fail validation before Pi
startup. The full OSM language is deliberately not advertised as supported.

Parsing uses [`opening-hours-py`](https://remi-dupre.github.io/opening-hours-rs/opening_hours.html).
Its supported platforms have prebuilt wheels; source builds require Rust.
`tzdata` supplies portable IANA timezone data. For the design and acceptance
criteria, see [the timed execution plan](plan_for_timed_execution.md).

## Optional Firejail sandboxing

Pass `--firejail` to launch every `pi` child inside a Firejail sandbox:

```bash
piloop run --firejail --model openrouter/moonshotai/kimi-k3
```

Without a config file this applies hardening only (`--noprofile`, drop all
capabilities, no new privileges) and whitelists the working directory. The
network is **not** restricted in that mode — a warning is printed to stderr.

To also restrict the network to a set of TCP destinations, give a JSONC config
with `--firejail-config`:

```jsonc
{
  "allow": [
    { "host": "aiserver01.onio.com", "port": 8000 },
    { "host": "onio-ws09.onio.com", "port": 18080 }
  ],
  "whitelist": ["~/pi-eval-agent", "~/.nvm"],
  "read_only": ["~/.nvm"],
  "blacklist": ["/icwork"],
  "iface": "eth0"
}
```

```bash
piloop run --firejail --firejail-config ./firejail.jsonc
```

* **`allow`** — TCP destinations the sandbox may reach. Each host is resolved
  to one IPv4 at startup; a mapping is written to a static `hosts` file so no
  DNS is needed inside the sandbox, and the netfilter rules default to DROP
  everything else.
* **`whitelist`** (writable), **`read_only`**, **`blacklist`** — path rules.
  `~` is expanded. The working directory is always whitelisted.
* **`iface`** — network interface for `--net`. Defaults to the device from
  `ip route show default`.

> In restricted mode all IPv6 is dropped; only the allowed IPv4 TCP
> destinations (and loopback) are reachable.
> Because Firejail hides the rest of the home directory, the whitelist must
> include pi's agent directory and the node install.

> `--firejail` must be installed (`sudo apt install firejail`).
> `--firejail-config` without `--firejail` is an error.

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
| Session boundary | persistent server session | fresh Pi child per iteration |
| Idle fallback | polled a field that does not exist | polls `get_state().isStreaming` |
| Iteration timeout | none | `--iteration-timeout`, then abort, then respawn |
| Iteration cap | none | `--max-iterations` |
| Progress stalls | undetected | `STALLED` state |
| Repeated errors | looped forever | error-streak circuit breaker |
| Blocking agent dialogs | n/a | auto-answered (`--dialog-policy`) |
| Tests | none | Fake-agent, watchdog, scheduling, and TUI coverage |

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
