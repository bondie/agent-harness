#!/usr/bin/env python3
"""PermissionRequest hook: let a viewer answer a permission prompt while it shows in the terminal.

Off unless `permission_bridge.enabled` is true in `$HARNESS_HOME/guard.json`. Claude Code shows
the prompt and runs this hook at the same time, and whichever answers first applies. The hook
writes the request (session, agent, tool and its input) to `$HARNESS_HOME/permissions/<id>.json`
in a folder only the user can read, then waits up to `wait_seconds` for `<id>.decision.json`
with `{"behavior": "allow"}` or `{"behavior": "deny", "message": "…"}`, accepted only from a
regular file of the same user. It stops waiting without a decision when the request file is
gone, which `session_state.py` does once the call ran or the turn ended (the person answered
in the terminal). Deny rules still apply to an allow. Standard library only; an internal error
is printed to stderr and leaves the prompt to the terminal.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from session_state import describe, harness_home, read

DEFAULTS: dict[str, Any] = {"enabled": False, "wait_seconds": 3540, "poll_seconds": 0.25}
DENIED = "The person denied this from the session viewer."


def settings(environ: Mapping[str, str]) -> dict[str, Any]:
    values = dict(DEFAULTS)
    section = read(os.path.join(harness_home(environ), "guard.json")).get("permission_bridge")
    if not isinstance(section, dict):
        return values
    if isinstance(section.get("enabled"), bool):
        values["enabled"] = section["enabled"]
    for key in ("wait_seconds", "poll_seconds"):
        value = section.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            values[key] = float(value)
    return values


def private_directory(environ: Mapping[str, str]) -> str:
    directory = os.path.join(harness_home(environ), "permissions")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def write_private(path: str, data: Mapping[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_decision(path: str) -> dict[str, Any] | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return None
    decision = read(path)
    return decision if decision.get("behavior") in ("allow", "deny") else None


def output(decision: Mapping[str, Any]) -> dict[str, Any]:
    if decision["behavior"] == "allow":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }
    message = decision.get("message")
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": message if isinstance(message, str) and message else DENIED,
            },
        }
    }


def remove(*paths: str) -> None:
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            continue


def bridge(
    event: Mapping[str, Any],
    environ: Mapping[str, str],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    if event.get("hook_event_name") != "PermissionRequest":
        return None
    config = settings(environ)
    if not config["enabled"]:
        return None
    directory = private_directory(environ)
    request_id = uuid.uuid4().hex
    request_path = os.path.join(directory, f"{request_id}.json")
    decision_path = os.path.join(directory, f"{request_id}.decision.json")
    tool = event.get("tool_name") if isinstance(event.get("tool_name"), str) else None
    write_private(
        request_path,
        {
            "version": 1,
            "id": request_id,
            "session_id": event.get("session_id"),
            "agent_id": event.get("agent_id") or None,
            "agent_type": event.get("agent_type"),
            "cwd": event.get("cwd"),
            "tool_name": tool,
            "tool_input": event.get("tool_input"),
            "summary": describe(tool, event.get("tool_input")),
            "created_epoch": time.time(),
        },
    )
    deadline = clock() + config["wait_seconds"]
    try:
        while clock() < deadline:
            if not os.path.exists(request_path):
                return None
            decision = read_decision(decision_path)
            if decision is not None:
                return output(decision)
            sleep(config["poll_seconds"])
        return None
    finally:
        remove(request_path, decision_path)


def main(
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    environ: Mapping[str, str] = os.environ,
) -> int:
    try:
        event = json.load(stdin)
        verdict = bridge(event, environ) if isinstance(event, dict) else None
    except Exception as error:
        print(f"permission_bridge.py left the prompt to the terminal: {error!r}", file=sys.stderr)
        return 0
    if verdict is not None:
        json.dump(verdict, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
