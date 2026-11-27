#!/usr/bin/env python3
"""A person's session's native subagent call starts as a Hearting unit route.

Claude `Agent` (older `Task`), Codex `spawn_agent` (multi-agent v2: `collaborationspawn_agent`) and
OpenCode `subagent` (V1 `task`) are intercepted before they run (Claude/Codex `PreToolUse`, OpenCode
`tool.execute.before`). The agent name picks the depth (`NAMES`; a model named in the call picks the
depth that model is declared for), the call's prompt is the brief, and

    capability-route.py compose --start --unit general/explore --profile <depth> --ephemeral --prompt-file -

starts the route, as a session that typed it would. When the route started, the native call is refused
with one line naming it; the answer comes back to this session as the route's completion notice
(`capabilities/unit.md`, A native subagent call). Nothing else is asked of the agent.

A start never blocks the session: whenever the route cannot start now (the governor has no slot,
Hearting is not reachable, the start fails, is refused, or runs past `BUDGET_SECONDS`) nothing is
printed and the native call runs as before. The compose process reads its own refusal
(`work_start.CALLER_READS_REFUSAL`), so a refused start arms no later start and leaves no open route:
the native call and the route never both do the work. Registered workers, calls made inside a native
subagent, names the table does not list (`fork`, agents that stand in for one tool such as `web-fetch`,
user and plugin agents) and calls that hand over the session's own history are left alone.

Usage (stdin: the harness payload):
  native_subagent_redirect.py --claude|--codex   print the PreToolUse deny JSON when started
  native_subagent_redirect.py --opencode         print the reason and exit 1 when started
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILITIES = ROOT / "utilities"
UNIT = "general/explore"
# The hook's own timeout is 180 s (Claude, Codex) and the plugin's spawn cap 180 s (OpenCode); a start
# measured at most 74 s (design 3.4). Past this budget the native call runs instead.
BUDGET_SECONDS = 150
_TOOLS = {"claude": {"Agent", "Task"}, "codex": {"spawn_agent", "collaborationspawn_agent"},
          "opencode": {"subagent", "task"}}
# The agent name a call gives, and the depth its route runs at (capabilities/unit.md, the name table).
NAMES = {
    "light": "light", "Explore": "light", "explorer": "light", "explore": "light", "memory-scout": "light",
    "balanced": "balanced", "worker": "balanced", "default": "balanced",
    "general-purpose": "balanced-deep", "Plan": "balanced-deep",
    "deep": "deep",
}
OPENCODE_NAMES = {"general": "balanced"}
UNNAMED = {"claude": "balanced-deep", "codex": "balanced", "opencode": "balanced"}
DEPTHS = ("light", "balanced", "balanced-deep", "deep")
# The compose process the hook runs: the same command a session types, told that its caller reads a
# refused start itself (no option a person or an agent chooses).
_BOOT = ("import runpy, sys\n"
         "sys.path[:] = [sys.argv[1]] + [p for p in sys.path if p not in ('', '.')]\n"
         "import work_start\n"
         "work_start.CALLER_READS_REFUSAL = True\n"
         "sys.argv = sys.argv[2:]\n"
         "runpy.run_path(sys.argv[0], run_name='__main__')\n")


class Skip(Exception):
    """Not a call to start here: the native call goes through."""


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalized(harness: str, payload: dict) -> dict:
    """`{session, name, prompt, title, model}` of a subagent call to start, or `Skip`."""
    if harness == "opencode":
        tool, session, args = payload.get("tool"), payload.get("sessionID"), payload.get("args")
    else:
        tool, session, args = payload.get("tool_name"), payload.get("session_id"), payload.get("tool_input")
    tool = str(tool or "").rsplit(".", 1)[-1]
    if tool not in _TOOLS[harness] or not isinstance(args, dict):
        raise Skip("not-a-subagent-call")
    if not isinstance(session, str) or not session:
        raise Skip("no-session")
    if harness == "claude":
        name, prompt, title = args.get("subagent_type"), args.get("prompt"), args.get("description")
        # A fork carries the whole conversation, a resumed agent its own; an isolated worktree or a
        # remote run is a form the read-only unit does not take.
        if name == "fork" or args.get("resume") or args.get("isolation"):
            raise Skip("claude-form-kept")
    elif harness == "codex":
        name, prompt, title = args.get("agent_type"), args.get("message"), args.get("task_name")
        # The thread's history handed over (v1 `fork_context`, v2 `fork_turns`, whose default is all).
        if args.get("fork_context") is True or (tool == "collaborationspawn_agent"
                                                and args.get("fork_turns") != "none"):
            raise Skip("codex-history-kept")
    else:
        name = args.get("agent") if "agent" in args else args.get("subagent_type")
        prompt, title = args.get("prompt"), args.get("description")
        if args.get("session_id") or args.get("task_id") or args.get("sessionID"):
            raise Skip("opencode-continuation-kept")
    prompt = _text(prompt)
    if not prompt:
        raise Skip("no-prompt")
    name = _text(name)
    return {"session": session, "name": name, "prompt": prompt, "title": _text(title),
            "model": _text(args.get("model"))}


def _profile_models(harness: str) -> dict:
    """`{depth: model}` this harness's model configuration declares for each depth."""
    sys.path.insert(0, str(UTILITIES))
    from model_config import resolve_config
    values, _receipt = resolve_config(harness, source_root=ROOT)
    models = {}
    for depth in DEPTHS:
        row = values.get("CFG_MODEL_PROFILE_" + depth.upper().replace("-", "_"), "")
        tier = row.split(":", 1)[0]
        model = (tier[len("model/"):] if tier.startswith("model/")
                 else values.get("CFG_TIER_" + tier.upper().replace("-", "_") + "_MODEL", ""))
        if model:
            models[depth] = model.lower()
    return models


def depth_for(harness: str, call: dict) -> str:
    """The depth of the route a call starts; `Skip` when the call names no depth this table knows."""
    name = call["name"]
    if not name:
        depth = UNNAMED[harness]
    elif name in NAMES:
        depth = NAMES[name]
    elif harness == "opencode" and name in OPENCODE_NAMES:
        depth = OPENCODE_NAMES[name]
    else:
        raise Skip("name-not-in-table")
    model = call["model"].lower()
    if not model:
        return depth
    models = _profile_models(harness)
    if models.get(depth) == model:
        return depth
    for candidate in DEPTHS:
        if models.get(candidate) == model:
            return candidate
    raise Skip("model-not-declared")


def _is_registered_worker(harness: str, payload: dict, session: str) -> bool:
    """Registered workers and native subagents keep their own behavior (the question redirect's test)."""
    import session_tidy as tidy
    if (tidy.is_worker() or os.environ.get("AGENT_SESSION_ROLE", "").lower()
            in {"owner", "worker", "auxiliary", "reviewer"} or bool(payload.get("agent_id"))):
        return True
    import session_identity
    return session_identity.sub_session_root(harness, session) != session


def brief(call: dict) -> str:
    return (call["title"] + "\n\n" if call["title"] and call["title"] not in call["prompt"] else "") + call["prompt"] + "\n"


def compose_env(harness: str, session: str) -> dict:
    """The session's own identity, as its tool commands carry it: no inherited caller name or other
    harness's session id (core/OPERATIONS.md), and this harness's id set from the payload."""
    from harness_capabilities import session_env
    env = dict(os.environ)
    env.pop("AGENT_DISPATCH_CALLER_HARNESS", None)
    for names in session_env().values():
        for name in names:
            env.pop(name, None)
    env[session_env()[harness][0]] = session
    return env


def _last_json(text: str) -> dict:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return {}


def _interrupted(_number, _frame):
    raise TimeoutError("native-subagent-redirect-interrupted")


def start(harness: str, call: dict, depth: str, cwd: str, *, seconds: float = BUDGET_SECONDS) -> dict | None:
    """Run the start; the receipt when the route started, else None (refused, failed, or late)."""
    command = [sys.executable, "-B", "-c", _BOOT, str(UTILITIES), str(UTILITIES / "capability-route.py"),
               "compose", "--start", "--unit", UNIT, "--profile", depth, "--ephemeral", "--prompt-file", "-",
               "--cwd", cwd]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        # Files, not pipes: a worker the start launches keeps no handle the harness waits on.
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=cwd,
                                 env=compose_env(harness, call["session"]), start_new_session=True)
        previous = {}
        try:
            # The harness ends a hook past its own timeout with a signal: the start ends with it.
            for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                previous[number] = signal.signal(number, _interrupted)
            child.stdin.write(brief(call).encode("utf-8"))
            child.stdin.close()
            child.wait(timeout=seconds)
        except BaseException:
            # Late (or interrupted): the native call runs; a start still on its way is stopped.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            child.wait()
            return None
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
        out.seek(0)
        receipt = _last_json(out.read().decode("utf-8", "replace"))
    if receipt.get("state") in {"running", "completed"} and receipt.get("route_id"):
        return receipt
    return None


def reason_for(route_id: str, depth: str) -> str:
    return (f"Started as Hearting unit route {route_id} ({UNIT}, {depth}, no record). Its answer comes back "
            "to this session as a completion notice. Do not call it again; go on with other work or end the turn.")


def redirect(harness: str, payload: dict, *, seconds: float = BUDGET_SECONDS) -> str | None:
    """The refusal text when the route started, else None (the native call goes through)."""
    if harness not in _TOOLS or not isinstance(payload, dict):
        return None
    try:
        sys.path.insert(0, str(UTILITIES))
        call = normalized(harness, payload)
        if _is_registered_worker(harness, payload, call["session"]):
            return None
        depth = depth_for(harness, call)
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else os.getcwd()
        if not os.path.isdir(cwd):
            return None
        receipt = start(harness, call, depth, cwd, seconds=seconds)
        return None if receipt is None else reason_for(receipt["route_id"], depth)
    except BaseException:  # noqa: BLE001 -- any doubt, failure or interruption lets the native call run
        return None


def main(argv=None, stdin=None) -> int:
    try:
        argv = sys.argv[1:] if argv is None else argv
        harness = next((flag[2:] for flag in argv if flag in ("--claude", "--codex", "--opencode")), "")
        if harness not in _TOOLS:
            return 0
        payload = json.loads((stdin or sys.stdin).read() or "{}")
        reason = redirect(harness, payload)
        if reason is None:
            return 0
        if harness == "opencode":
            print(reason)
            return 1
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}},
                         ensure_ascii=False, separators=(",", ":")))
        return 0
    except BaseException:  # noqa: BLE001 -- the hook never fails the call: the native subagent runs
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
