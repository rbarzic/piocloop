# {{PLAN_TITLE}}

## Overview

{{GOAL_DESCRIPTION - one or two sentences on what this loop accomplishes.}}

Output / results are written to: `{{OUTPUT_FILE - e.g. results/summary.md}}`

## Backlog

### Phase 1: {{PHASE_NAME - e.g. Items 1-10}}

- [ ] **1** {{TASK_DESCRIPTION - one self-contained item; the agent sees only this}}
- [ ] **2** {{TASK_DESCRIPTION}}
- [ ] **3** {{TASK_DESCRIPTION}}
- [ ] **4** {{TASK_DESCRIPTION}}
- [ ] **5** {{TASK_DESCRIPTION}}
- [ ] **6** {{TASK_DESCRIPTION}}
- [ ] **7** {{TASK_DESCRIPTION}}
- [ ] **8** {{TASK_DESCRIPTION}}
- [ ] **9** {{TASK_DESCRIPTION}}
- [ ] **10** {{TASK_DESCRIPTION}}

### Phase 2: {{PHASE_NAME - next batch}}

- [ ] **11** {{TASK_DESCRIPTION}}
- [ ] **12** {{TASK_DESCRIPTION}}

<!--
Task line syntax (the parser reads the checkbox between [ and ]).
NOTE: the examples below deliberately omit the leading "- ", because any line
starting with "- [" is parsed as a real task even inside an HTML comment.

  [ ]                 pending      (the agent picks the FIRST of these)
  [x]  /  [X]         completed
  [MANUAL]            skipped by the loop (needs a human)
  [BLOCKED: reason]   could not complete; reason recorded

Prefix each with "- " when writing an actual task line.

Rules:
  - Execution order = file order (first pending line wins).
  - Each task must be self-contained: every iteration is a FRESH pi session.
  - Add a new "### Phase N: ..." section per ~10 tasks.
  - The loop ends when the prompt appends, at the start of a line:
        <plan-complete>summary</plan-complete>
  - piocloop halts as STALLED if the completed count does not rise for 3
    iterations. [BLOCKED] does not count as progress; use --max-stalls to
    raise or disable that guard.
-->
