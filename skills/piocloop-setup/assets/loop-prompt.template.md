Execute the next task from {{PLAN_FILE}}.

Before starting:
1. Read {{PLAN_FILE}} fully

Task selection (CRITICAL):
- Work through phases IN ORDER — complete Phase N before starting Phase N+1
- Pick the FIRST uncompleted task in the earliest incomplete phase
- Skip [MANUAL] and [BLOCKED] items
- NEVER batch tasks across different phases

Execute:
1. {{YYYYYY - the action to perform for the current item}}

After completion:
1. Update `{{OUTPUT_FILE - e.g. results/summary.md}}` accordingly
2. Update {{PLAN_FILE}} marking the completed item with [x]

3. If you cannot complete a task (permissions, external service, needs human input):
   - Add [BLOCKED: reason] to that task line in {{PLAN_FILE}}
   - Continue with other tasks

Completion check:
- If all non-[MANUAL] tasks are either [x] or [BLOCKED]:
  - Append `<plan-complete>SUMMARY_OF_WORK_DONE_AND_REMAINING_MANUAL_TASKS</plan-complete>`
    to the end of {{PLAN_FILE}}, starting at the beginning of a line
  - Stop
- Do NOT skip automatable tasks — if a task seems hard but doable, attempt it

<!--
Notes for whoever edits this template:
  - NO YAML frontmatter. piocloop sends this file to pi verbatim as the prompt;
    a header would be read by the model as literal content.
  - {{PLAN_FILE}} is the ONLY placeholder piocloop substitutes (absolute path).
    Keep every reference to the plan as {{PLAN_FILE}}; do NOT hardcode paths.
  - Mark [x] after EACH task — that is how the loop advances, and piocloop halts
    as STALLED if the completed count stops rising for 3 iterations.
  - <plan-complete> must start at the beginning of a line, once, at the very end.
  - Default filename is the dotfile ".loop-prompt.md" (piloop run --prompt default).
-->
