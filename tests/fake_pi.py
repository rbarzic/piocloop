#!/usr/bin/env python3
"""A fake `pi --mode rpc` for testing PiClient without a network or an LLM.

Usage: fake_pi.py <scenario> [--mode rpc ...ignored...]

Each scenario exercises one hazard from DESIGN.md §4.
"""

from __future__ import annotations

import json
import re
import sys
import time

# Scenarios that claim to be streaming when polled, so the client's soft-poll
# fallback cannot mistake a stuck run for a finished one.
_PRETEND_STREAMING = {"busy", "wedged"}
_streaming = False


def work_the_plan(message: str) -> None:
    """Tick the first pending task, or append <plan-complete> when none remain.

    Emits the same tool events real `pi` would, so PlanTouched detection is
    exercised end to end.
    """
    match = re.search(r"(/\S+\.md)", message)
    if not match:
        return
    path = match.group(1)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return

    out({"type": "tool_execution_start", "toolCallId": "t-read",
         "toolName": "read", "args": {"path": path}})
    out({"type": "tool_execution_end", "toolCallId": "t-read",
         "toolName": "read", "isError": False})

    for i, line in enumerate(lines):
        if line.strip().startswith("- [ ]"):
            lines[i] = line.replace("- [ ]", "- [x]", 1)
            break
    else:
        lines.append("<plan-complete>fake agent finished the plan</plan-complete>")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    out({"type": "tool_execution_start", "toolCallId": "t-edit",
         "toolName": "edit", "args": {"path": path}})
    out({"type": "tool_execution_end", "toolCallId": "t-edit",
         "toolName": "edit", "isError": False})


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def raw(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def ok(cmd: str, rid, data=None) -> None:
    msg = {"type": "response", "command": cmd, "success": True}
    if rid is not None:
        msg["id"] = rid
    if data is not None:
        msg["data"] = data
    out(msg)


def settle() -> None:
    out({"type": "agent_start"})
    out({"type": "turn_start"})
    out({"type": "message_start", "message": {"role": "assistant"}})
    out({"type": "message_end", "message": {"role": "assistant"}})
    out({"type": "turn_end", "message": {}, "toolResults": []})
    out({"type": "agent_end", "willRetry": False})
    out({"type": "agent_settled"})


def main() -> int:
    global _streaming
    scenario = sys.argv[1] if len(sys.argv) > 1 else "normal"

    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            out({"type": "response", "command": "parse", "success": False, "error": "bad json"})
            continue

        ctype = cmd.get("type")
        rid = cmd.get("id")

        # A client must never answer fire-and-forget UI methods. If it does,
        # we announce it so the test can fail.
        if ctype == "extension_ui_response":
            if scenario == "status":
                out({"type": "unexpected_response_received", "raw": cmd})
            elif scenario == "dialog":
                out({"type": "dialog_resolved", "reply": cmd})
                settle()
            continue

        if ctype == "get_state":
            ok("get_state", rid,
               {"isStreaming": _streaming, "sessionId": "fake", "messageCount": 0})
            continue

        if ctype == "new_session":
            ok("new_session", rid, {"cancelled": False})
            continue

        if ctype == "abort":
            if scenario == "wedged":
                continue  # never answers: forces the respawn stage
            _streaming = False
            ok("abort", rid)
            out({"type": "agent_settled"})
            continue

        if ctype != "prompt":
            ok(ctype or "unknown", rid)
            continue

        # --- prompt handling, per scenario ---------------------------------

        if scenario == "deaf":
            # Accepts nothing, answers nothing: exercises the request deadline.
            continue

        ok("prompt", rid)

        if scenario == "normal":
            settle()

        elif scenario == "split":
            # One JSON record delivered in fragments across several writes,
            # plus two records sharing a single write.
            raw('{"type": "agent_')
            time.sleep(0.05)
            raw('start"}\n{"type": "turn_start"}\n')
            time.sleep(0.05)
            raw('{"type": "agent_settled"}')
            time.sleep(0.05)
            raw("\n")

        elif scenario == "u2028":
            # U+2028/U+2029 are legal inside JSON strings and must NOT be
            # treated as record separators.
            payload = {"type": "message_end",
                       "message": {"text": "a\u2028b\u2029c"}}
            raw(json.dumps(payload, ensure_ascii=False) + "\n")
            settle()

        elif scenario == "crlf":
            raw('{"type": "agent_settled"}\r\n')

        elif scenario == "badjson":
            raw("this is not json\n")
            raw("\n")  # blank frame
            settle()

        elif scenario == "dialog":
            # Blocking dialog with NO timeout field: `pi` waits forever unless
            # the client answers. settle() happens in the response branch above.
            out({
                "type": "extension_ui_request",
                "id": "dlg-1",
                "method": "select",
                "title": "Allow dangerous command?",
                "options": ["Allow", "Block"],
            })

        elif scenario == "status":
            out({
                "type": "extension_ui_request",
                "id": "st-1",
                "method": "setStatus",
                "statusKey": "zai-usage",
                "statusText": "[38;2;128;128;128mZ.ai:[39m 3%",
            })
            settle()

        elif scenario == "stderr_flood":
            # >512 KiB on stderr. If the client does not drain it, the OS pipe
            # fills and this write blocks forever — pyocloop bug H2.
            for i in range(4000):
                sys.stderr.write(f"noisy provider warning line {i}: " + "x" * 120 + "\n")
            sys.stderr.flush()
            settle()

        elif scenario == "planworker":
            work_the_plan(cmd.get("message", ""))
            settle()

        elif scenario == "dialogworker":
            # A blocking dialog mid-iteration must not stall the whole loop.
            out({"type": "extension_ui_request", "id": "dlg-1", "method": "confirm",
                 "title": "Apply this edit?", "message": "y/n"})
            work_the_plan(cmd.get("message", ""))
            settle()

        elif scenario == "stubborn":
            # Works, but never ticks a checkbox: exercises stall detection.
            settle()

        elif scenario == "silent":
            # Accepts the prompt then goes quiet forever, while honestly
            # reporting that it is not streaming: the soft poll must recover.
            pass

        elif scenario in ("busy", "wedged"):
            # Claims to be streaming forever and never settles: only the
            # iteration deadline can end this.
            _streaming = True

        elif scenario == "eof":
            sys.stdout.flush()
            return 3  # die mid-iteration, before settling

        else:
            settle()

    return 0


if __name__ == "__main__":
    sys.exit(main())
