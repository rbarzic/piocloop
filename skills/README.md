# piocloop skills

Optional agent skills you can **copy into your own agent** so it knows how to
drive piocloop. Nothing here is loaded automatically — piocloop itself never
reads this directory. Install a skill only if you want your agent to have it.

## `piocloop-setup`

Teaches an agent to turn a request like

> "For each of these 40 items, do X and record the result in results.md"

into the two files piocloop needs (`PLAN.md` and `.loop-prompt.md`), correctly
formatted, and to give you the right `piloop run` command.

It encodes the rules that are easy to get wrong: the `{{PLAN_FILE}}` placeholder,
per-task `[x]` marking, the `<plan-complete>` tag's column-0 requirement, and how
stall detection interacts with `[BLOCKED]` tasks.

## Install

The skill follows the [Agent Skills standard](https://agentskills.io/specification),
so it works with any harness that implements it. Copy the whole
`piocloop-setup/` directory (SKILL.md plus `references/` and `assets/`).

**PI** — global, for every project:

```bash
mkdir -p ~/.pi/agent/skills
cp -r skills/piocloop-setup ~/.pi/agent/skills/
```

…or per project (requires the project to be trusted):

```bash
mkdir -p .pi/skills
cp -r /path/to/piocloop/skills/piocloop-setup .pi/skills/
```

…or without copying at all, pointing `pi` straight at the checkout:

```bash
pi --skill /path/to/piocloop/skills/piocloop-setup
```

**Claude Code:**

```bash
mkdir -p ~/.claude/skills
cp -r skills/piocloop-setup ~/.claude/skills/
```

**OpenAI Codex:**

```bash
mkdir -p ~/.codex/skills
cp -r skills/piocloop-setup ~/.codex/skills/
```

`pi` can also read another harness's skill directory directly, without copying —
add it to `~/.pi/agent/settings.json`:

```json
{
  "skills": ["~/.claude/skills"]
}
```

## Verify it loaded

In `pi`, run `/skill:piocloop-setup`, or just ask: *"set up a piocloop run that
does X for each of these items"*. In Claude Code, ask the same — skills load on
demand based on the description.

## Note for agents running *inside* a loop

This skill is for the agent that **sets a run up**, not the one executing tasks
inside it. The in-loop agent gets its instructions from `.loop-prompt.md`, which
is sent fresh every iteration; put per-iteration guidance there, not here.
