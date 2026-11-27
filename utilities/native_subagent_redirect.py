#!/usr/bin/env python3
"""A person's session's native subagent call starts as a Hearting unit route when that is certain.

Claude `Agent` (older `Task`) and Codex `spawn_agent` (multi-agent v2: `collaborationspawn_agent`) are
intercepted before they run (`PreToolUse`). The agent name picks the depth (`NAMES`; a model or effort named
in the call picks the depth it is declared for), the call's prompt is the brief, and

    capability-route.py compose --start --unit general/explore --profile <depth> --ephemeral --prompt-file -

starts the route, as a session that typed it would. When the route started, the native call is refused with
one line naming it and telling how its answer comes back (`capabilities/unit.md`, A native subagent call).

The route starts only when all of this is known; anything else leaves the native call exactly as it was,
with nothing printed:
- Hearting started it: the start's own receipt, or a registry row of the route past its launch fence
  (`launch_started=1`) whose worker still runs. A start that did not get there is stopped with its whole
  process group, its route and any attempt it registered are closed the way the runtime closes a refused
  start, and nothing arms a later start (`work_start.CALLER_READS_REFUSAL`). The budget counts from the
  brief's writing; when the hook itself goes away first, the compose process ends its own group.
- The definition the harness picks for the name is Hearting's shipped one or the harness's built-in, and
  the call carries nothing a brief cannot (`_ARGUMENTS`): a project or user definition of the same name,
  history handed over (`fork`, `fork_context`, `fork_turns`), attachments (`items`), `resume`, `isolation`.
- No other hook or permission rule decides the same call: the harness runs matching hooks side by side and
  applies deny/ask rules after them, so a refusal there would come after the route started.
- The caller is a person's session: not a registered worker, and not a native subagent (a Codex thread
  whose spawn record cannot be read counts as unknown).

OpenCode is not intercepted: its `subagent` permission (`ask` when no rule matches) and other plugins'
before-hooks decide inside the tool, where the plugin cannot see them (`adapters/opencode/ADAPTATION.md`).

Usage (stdin: the harness payload):
  native_subagent_redirect.py --claude|--codex   print the PreToolUse deny JSON when started
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILITIES = ROOT / "utilities"
UNIT = "general/explore"
# The hook's own timeout is 180 s; a start measured at most 74 s (design 3.4). Past this budget the start
# is stopped and closed, which takes a few seconds more, and the native call runs instead.
BUDGET_SECONDS = 150
# How often the waiting hook reads the start's receipt (within the budget).
POLL_SECONDS = 0.05
_TOOLS = {"claude": {"Agent", "Task"}, "codex": {"spawn_agent", "collaborationspawn_agent"}}
# The arguments a unit route takes over: the brief, its title, the agent name and the depth's model/effort.
_ARGUMENTS = {"claude": {"description", "prompt", "subagent_type", "model", "run_in_background"},
              "codex": {"agent_type", "message", "task_name", "model", "reasoning_effort", "fork_context",
                        "fork_turns"}}
# The agent name a call gives, and the depth its route runs at (capabilities/unit.md, the name table).
NAMES = {
    "light": "light", "Explore": "light", "explorer": "light", "explore": "light", "memory-scout": "light",
    "balanced": "balanced", "worker": "balanced", "default": "balanced",
    "general-purpose": "balanced-deep", "Plan": "balanced-deep",
    "deep": "deep",
}
UNNAMED = {"claude": "balanced-deep", "codex": "balanced"}
# The agent a call without a name runs as, and the agents the harness itself defines.
DEFAULT_AGENT = {"claude": "general-purpose", "codex": "default"}
BUILT_IN = {"claude": {"Explore", "Plan", "general-purpose"}, "codex": {"default", "worker", "explorer"}}
DEPTHS = ("light", "balanced", "balanced-deep", "deep")
# The compose process the hook runs: the same command a session types, told that its caller reads a refused
# start itself (no option a person or an agent chooses). It holds one end of a pipe the hook keeps open: the
# hook writes `1` once it took the start; if the hook goes away first, the start's whole group goes too.
_BOOT = ("import os, runpy, signal, sys, threading\n"
         "sys.path[:] = [sys.argv[1]] + [p for p in sys.path if p not in ('', '.')]\n"
         "def _hold(fd):\n"
         "    try:\n"
         "        taken = os.read(fd, 1) == b'1'\n"
         "    except OSError:\n"
         "        taken = False\n"
         "    if not taken:\n"
         "        os.killpg(0, signal.SIGKILL)\n"
         "threading.Thread(target=_hold, args=(int(sys.argv[2]),), daemon=True).start()\n"
         "import work_start\n"
         "work_start.CALLER_READS_REFUSAL = True\n"
         "sys.argv = sys.argv[3:]\n"
         "runpy.run_path(sys.argv[0], run_name='__main__')\n")


class Skip(Exception):
    """Not a call to start here: the native call goes through."""


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalized(harness: str, payload: dict) -> dict:
    """`{session, name, prompt, title, model, effort}` of a subagent call to start, or `Skip`."""
    tool, session, args = payload.get("tool_name"), payload.get("session_id"), payload.get("tool_input")
    tool = str(tool or "").rsplit(".", 1)[-1]
    if tool not in _TOOLS.get(harness, ()) or not isinstance(args, dict):
        raise Skip("not-a-subagent-call")
    if not isinstance(session, str) or not session:
        raise Skip("no-session")
    if set(args) - _ARGUMENTS[harness]:
        # Codex `items` (attachments, or attachments beside a message, which the native tool refuses), Claude
        # `resume`/`isolation`, or any argument this table does not know: the brief alone cannot carry it.
        raise Skip("form-not-carried")
    if harness == "claude":
        name, prompt, title = args.get("subagent_type"), args.get("prompt"), args.get("description")
        if name == "fork":   # the whole conversation
            raise Skip("claude-form-kept")
    else:
        name, prompt, title = args.get("agent_type"), args.get("message"), args.get("task_name")
        # The thread's history handed over: v1 `fork_context`; v2 (`task_name`/`fork_turns`) forks all turns
        # unless `fork_turns` is `none`, and refuses `fork_context`.
        v2 = tool == "collaborationspawn_agent" or "task_name" in args or "fork_turns" in args
        if (args.get("fork_context") not in (None, False)
                or (v2 and ("fork_context" in args or _text(args.get("fork_turns")) != "none"))):
            raise Skip("codex-history-kept")
    prompt = _text(prompt)
    if not prompt:
        raise Skip("no-prompt")
    return {"session": session, "name": _text(name), "prompt": prompt, "title": _text(title),
            "model": _text(args.get("model")), "effort": _text(args.get("reasoning_effort"))}


def _profiles(harness: str) -> dict:
    """`{depth: {"model", "effort"}}` this harness's model configuration declares for each depth."""
    sys.path.insert(0, str(UTILITIES))
    from model_config import resolve_config
    values, _receipt = resolve_config(harness, source_root=ROOT)
    profiles = {}
    for depth in DEPTHS:
        tier, _, effort = values.get("CFG_MODEL_PROFILE_" + depth.upper().replace("-", "_"), "").partition(":")
        key = "CFG_TIER_" + tier.upper().replace("-", "_")
        model = tier[len("model/"):] if tier.startswith("model/") else values.get(key + "_MODEL", "")
        if model:
            profiles[depth] = {"model": model.lower(), "effort": (effort or values.get(key + "_EFFORT", "")).lower()}
    return profiles


def depth_for(harness: str, call: dict) -> str:
    """The depth of the route a call starts; `Skip` when the call names no depth this table knows."""
    name = call["name"]
    if not name:
        depth = UNNAMED[harness]
    elif name in NAMES:
        depth = NAMES[name]
    else:
        raise Skip("name-not-in-table")
    model, effort = call["model"].lower(), call.get("effort", "").lower()
    if not model and not effort:
        return depth
    profiles = _profiles(harness)

    def fits(candidate):
        declared = profiles.get(candidate) or {}
        return ((not model or declared.get("model") == model)
                and (not effort or declared.get("effort") == effort))

    for candidate in (depth, *DEPTHS):
        if fits(candidate):
            return candidate
    raise Skip("model-not-declared")


# --- The definition the harness picks for a name ------------------------------------------------------------

def _claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or Path.home() / ".claude")


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "").strip() or Path.home() / ".codex")


def _project_folders(cwd: str, folder: str, home: Path) -> list[Path]:
    """`<dir>/<folder>` for the working directory and every directory above it (Claude walks up for project
    agents; a wider walk only finds more reasons to stay native), except the user's own home folder."""
    start = Path(cwd).resolve()
    seen, found = {home.resolve()}, []
    for base in (start, *start.parents):
        candidate = (base / folder).resolve()
        if candidate not in seen:
            seen.add(candidate)
            found.append(base / folder)
    return found


def _frontmatter(text: str):
    """`({key: value}, body)` of an agent Markdown file, or None when it has no frontmatter."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            fields = {}
            for row in lines[1:index]:
                key, sep, value = row.partition(":")
                if sep and key and not key[0].isspace() and not key.startswith("#"):
                    fields[key.strip()] = value.strip().strip("\"'")
            return fields, "\n".join(lines[index + 1:])
    return None


def _toml(path: Path) -> dict:
    import tomllib
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _defined_names(harness: str, path: Path) -> set:
    """The agent name(s) one definition file declares (Claude: frontmatter `name`; Codex: `name`)."""
    if harness == "claude":
        parsed = _frontmatter(path.read_text(encoding="utf-8"))
        return {parsed[0]["name"]} if parsed and parsed[0].get("name") else set()
    name = _toml(path).get("name")
    return {name.strip()} if isinstance(name, str) and name.strip() else set()


def _is_shipped(harness: str, path: Path, name: str) -> bool:
    """The definition file is Hearting's own: a link into this release, or its text apart from the model pin
    activation renders for the user's model configuration."""
    shipped = ROOT / "adapters" / harness / "agents" / (name + (".md" if harness == "claude" else ".toml"))
    try:
        if path.is_symlink():
            return path.resolve() == shipped.resolve()
        if not shipped.is_file():
            return False
        if harness == "claude":
            mine, ours = _frontmatter(path.read_text(encoding="utf-8")), _frontmatter(shipped.read_text(encoding="utf-8"))
            return bool(mine and ours) and mine[1] == ours[1] and all(
                mine[0].get(key) == ours[0].get(key) for key in ("name", "description"))
        mine, ours = _toml(path), _toml(shipped)
        return all(mine.get(key) == ours.get(key) for key in ("name", "description", "developer_instructions"))
    except (OSError, ValueError):
        return False


def _definitions(harness: str, cwd: str, name: str) -> tuple[list[Path], list]:
    """`(user_files, other_definitions)` naming `name`: the user's own scope, and every other scope the
    harness reads (managed or system, project, a config declaration). An unreadable file raises."""
    if harness == "claude":
        home, suffix = _claude_home(), "*.md"
        others = [Path("/etc/claude-code"), Path("/Library/Application Support/ClaudeCode")]
        folders = [root / "agents" for root in others] + [root / ".claude" / "agents" for root in others]
        project = [folder / "agents" for folder in _project_folders(cwd, ".claude", home)]
        if os.environ.get("CLAUDE_PROJECT_DIR", "").strip():
            project += [folder / "agents" for folder in
                        _project_folders(os.environ["CLAUDE_PROJECT_DIR"].strip(), ".claude", home)]
    else:
        home, suffix = _codex_home(), "*.toml"
        folders = [Path("/etc/codex/agents")]
        project = [folder / "agents" for folder in _project_folders(cwd, ".codex", home)]
    user = [path for path in sorted((home / "agents").rglob(suffix)) if name in _defined_names(harness, path)] \
        if (home / "agents").is_dir() else []
    other = []
    for folder in (*folders, *project):
        if folder.is_dir():
            other += [path for path in sorted(folder.rglob(suffix)) if name in _defined_names(harness, path)]
    if harness == "codex":
        # A role declared in a config layer (`[agents.<name>]`), in any layer this caller can see.
        for config in [Path("/etc/codex/config.toml"), home / "config.toml",
                       *[folder / "config.toml" for folder in _project_folders(cwd, ".codex", home)]]:
            if config.is_file() and isinstance((_toml(config).get("agents") or {}).get(name), dict):
                other.append(config)
    return user, other


def require_shipped_definition(harness: str, name: str, cwd: str) -> None:
    """`Skip` unless the definition the harness picks for `name` is Hearting's shipped one or its built-in."""
    try:
        user, other = _definitions(harness, cwd, name)
    except (OSError, ValueError) as exc:
        raise Skip("definition-unreadable") from exc
    if other:
        raise Skip("definition-elsewhere")          # a project, managed or declared definition comes first
    if len(user) == 1 and _is_shipped(harness, user[0], name):
        return
    if not user and name in BUILT_IN[harness]:
        return
    raise Skip("definition-not-shipped")


# --- Other decisions on the same call -----------------------------------------------------------------------

def _hook_matches(harness: str, matcher, tools: set) -> bool:
    """The harness's matcher rule: empty or `*` is every tool, a plain list is exact names, else a regex."""
    if matcher in (None, "", "*"):
        return True
    if not isinstance(matcher, str):
        return True
    plain = r"[A-Za-z0-9_|]+" if harness == "codex" else r"[A-Za-z0-9_\-, |]+"
    if re.fullmatch(plain, matcher):
        return bool(tools & {part.strip() for part in re.split(r"[|,]", matcher)})
    try:
        return any(re.search(matcher, tool) for tool in tools)
    except re.error:
        return True


def _ours(harness: str, handler) -> bool:
    command = handler.get("command") if isinstance(handler, dict) else None
    if not isinstance(command, str) or handler.get("type", "command") != "command":
        return False
    if harness == "claude":
        return "subagent-model-default.sh --redirect" in command
    return "pretooluse-subagent-redirect.py" in command


def _hooks_decide(harness: str, hooks, tools: set) -> bool:
    """A `PreToolUse` handler other than this hook matches the call."""
    if not isinstance(hooks, dict):
        return False
    for group in hooks.get("PreToolUse") or []:
        if not isinstance(group, dict) or not _hook_matches(harness, group.get("matcher"), tools):
            continue
        if any(not _ours(harness, handler) for handler in group.get("hooks") or []):
            return True
    return False


def _rules_decide(settings: dict) -> bool:
    """A Claude deny or ask rule names the subagent tool: it applies whatever a hook returns."""
    permissions = settings.get("permissions") if isinstance(settings.get("permissions"), dict) else {}
    for kind in ("deny", "ask"):
        for rule in permissions.get(kind) or []:
            if isinstance(rule, str) and re.match(r"\s*(Agent|Task)\s*(\(|$)", rule):
                return True
    return False


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"not an object: {path}")
    return value


def _plugin_hooks(root: Path) -> list:
    """The hook sets an installed plugin declares: `hooks/hooks.json` and its manifest's `hooks`."""
    found = []
    if (root / "hooks" / "hooks.json").is_file():
        found.append(_json(root / "hooks" / "hooks.json").get("hooks"))
    for manifest in (root / ".claude-plugin" / "plugin.json", root / ".codex-plugin" / "plugin.json"):
        if not manifest.is_file():
            continue
        declared = _json(manifest).get("hooks")
        for entry in declared if isinstance(declared, list) else [declared]:
            if isinstance(entry, dict):
                found.append(entry.get("hooks", entry))
            elif isinstance(entry, str):
                found.append(_json((root / entry).resolve()).get("hooks"))
    return found


def _decision_sources(harness: str, cwd: str) -> list:
    """`[(hooks, settings)]` of every configuration this harness reads that this hook can see."""
    sources = []
    if harness == "claude":
        home = _claude_home()
        files = [home / "settings.json", home / "settings.local.json"]
        for managed in (Path("/etc/claude-code"), Path("/Library/Application Support/ClaudeCode")):
            files.append(managed / "managed-settings.json")
            if (managed / "managed-settings.d").is_dir():
                files += sorted((managed / "managed-settings.d").glob("*.json"))
        projects = _project_folders(cwd, ".claude", home)
        if os.environ.get("CLAUDE_PROJECT_DIR", "").strip():
            projects += _project_folders(os.environ["CLAUDE_PROJECT_DIR"].strip(), ".claude", home)
        for folder in projects:
            files += [folder / "settings.json", folder / "settings.local.json"]
        for path in files:
            if path.is_file():
                settings = _json(path)
                sources.append((settings.get("hooks"), settings))
        for plugin in sorted((home / "plugins" / "cache").glob("*/*/*")):
            sources += [(hooks, {}) for hooks in _plugin_hooks(plugin)]
        return sources
    home = _codex_home()
    layers = [Path("/etc/codex"), home, *_project_folders(cwd, ".codex", home)]
    for layer in layers:
        if (layer / "hooks.json").is_file():
            sources.append((_json(layer / "hooks.json").get("hooks"), {}))
        for name in ("config.toml", "requirements.toml"):
            if (layer / name).is_file():
                sources.append((_toml(layer / name).get("hooks"), {}))
    plugins = home / "plugins"
    for pattern in ("*", "*/*", "*/*/*", "*/*/*/*"):
        for plugin in sorted(plugins.glob(pattern)) if plugins.is_dir() else []:
            if plugin.is_dir():
                sources += [(hooks, {}) for hooks in _plugin_hooks(plugin)]
                if (plugin / "hooks.json").is_file():
                    sources.append((_json(plugin / "hooks.json").get("hooks"), {}))
    return sources


def require_sole_decision(harness: str, tool: str, cwd: str) -> None:
    """`Skip` when another hook or a permission rule can also decide this call: the harness runs matching
    hooks side by side and applies deny/ask rules after them, so this hook cannot see that decision."""
    tools = {tool, tool.rsplit(".", 1)[-1]} | ({"Agent", "Task"} if harness == "claude" else set())
    try:
        sources = _decision_sources(harness, cwd)
    except (OSError, ValueError) as exc:
        raise Skip("configuration-unreadable") from exc
    for hooks, settings in sources:
        if _hooks_decide(harness, hooks, tools) or (harness == "claude" and _rules_decide(settings)):
            raise Skip("another-decision")


# --- Who calls --------------------------------------------------------------------------------------------

def _is_registered_worker(harness: str, payload: dict, session: str) -> bool:
    """Registered workers and native subagents keep their own behavior (the question redirect's test). A
    sub-session record that cannot be read counts as a native subagent: nothing is certain about it."""
    import session_tidy as tidy
    if (tidy.is_worker() or os.environ.get("AGENT_SESSION_ROLE", "").lower()
            in {"owner", "worker", "auxiliary", "reviewer"} or bool(payload.get("agent_id"))):
        return True
    import session_identity
    return session_identity.known_sub_session_root(harness, session) != session


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


# --- The start ---------------------------------------------------------------------------------------------

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


def _receipt(out) -> dict | None:
    out.seek(0)
    return _last_json(out.read().decode("utf-8", "replace")) or None


def _started(receipt) -> bool:
    return bool(receipt) and receipt.get("state") in {"running", "completed"} and bool(receipt.get("route_id"))


def _interrupted(_number, _frame):
    raise TimeoutError("native-subagent-redirect-interrupted")


def _stop(child) -> None:
    """End the start's whole process group (its launcher too; a launch fence not yet released dies with it)."""
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except OSError:
        pass
    child.wait()


def start(harness: str, call: dict, depth: str, cwd: str, *, seconds: float = BUDGET_SECONDS) -> dict | None:
    """Run the start; its receipt when the route started, else None (the native call runs, nothing stays)."""
    deadline = time.monotonic() + seconds           # the budget counts from the brief's writing
    caller = os.getppid()
    env = compose_env(harness, call["session"])
    with tempfile.TemporaryFile() as given, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        # Files, not pipes: no write waits on the start, and a worker it launches keeps no handle the
        # harness waits on.
        given.write(brief(call).encode("utf-8"))
        given.seek(0)
        hold, held = os.pipe()
        try:
            child = subprocess.Popen(
                [sys.executable, "-B", "-c", _BOOT, str(UTILITIES), str(hold), str(UTILITIES / "capability-route.py"),
                 "compose", "--start", "--unit", UNIT, "--profile", depth, "--ephemeral", "--prompt-file", "-",
                 "--cwd", cwd],
                stdin=given, stdout=out, stderr=err, cwd=cwd, env=env, start_new_session=True, pass_fds=(hold,))
        except BaseException:
            os.close(held)
            raise
        finally:
            os.close(hold)
        receipt, previous = None, {}
        try:
            # The harness ends a hook past its own timeout with a signal: the start ends with it.
            for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                previous[number] = signal.signal(number, _interrupted)
            while True:
                ended = child.poll() is not None
                receipt = _receipt(out)
                if receipt is not None or ended:
                    break
                if time.monotonic() >= deadline or os.getppid() != caller:
                    raise TimeoutError("native-subagent-redirect-late")
                time.sleep(POLL_SECONDS)
        except BaseException:
            _stop(child)
            receipt = _receipt(out)                   # one flushed just before the stop still counts
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
        if _started(receipt):
            try:
                os.write(held, b"1")                  # taken: the compose process finishes on its own
            except OSError:
                pass                                  # it has already ended
            finally:
                os.close(held)
            return receipt
        _stop(child)
        os.close(held)
        err.seek(0)
        return settle(receipt, err.read().decode("utf-8", "replace"), env)


def _closed_by_the_start(receipt: dict) -> bool:
    """The compose process closed its own refused start (`capability-route.py _close_unstarted_record_free`)."""
    return (receipt.get("state") in ("waiting-capacity", "needs-attention")
            and not receipt.get("registered_attempts") and not receipt.get("auto_resume"))


def _route_api():
    import importlib.util
    spec = importlib.util.spec_from_file_location("native_subagent_redirect_route", UTILITIES / "capability-route.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def handed_over(route: dict, route_file: str, jobs: Path) -> dict | None:
    """A receipt for the route's attempt that crossed its launch fence and still runs, else None."""
    import work_start
    from parent_next_directive import parent_next, resume_command
    for aid, (status, meta) in work_start._rows(jobs).items():
        if (meta.get("route_id") == route["route_id"] and meta.get("launch_started") == "1"
                and status in ("open", "running")):
            directive, reason, _ = parent_next(meta.get("parent_completion_delivery", ""), aid, agent_home=ROOT)
            resume = resume_command(Path(route_file).resolve(), Path(jobs).resolve(), agent_home=ROOT)
            return {"state": "running", "route_id": route["route_id"], "unit_attempt_id": aid,
                    "parent_next": directive, "parent_next_reason": reason, "resume_command": resume,
                    "parent_next_command": resume + " --wait" if directive == "bounded-wait" else ""}
    return None


def withdraw(route: dict, route_file: str, jobs: Path, api) -> None:
    """Close a start that did not get there: stop each attempt it registered, then close the route the way
    the runtime closes a refused start. Each step is the existing one and repeating it is harmless."""
    import route_autoclose
    import route_parent_close
    import work_start
    for aid, (status, meta) in work_start._rows(jobs).items():
        if meta.get("route_id") == route["route_id"] and status in ("open", "running"):
            try:
                route_parent_close.stop_attempt(aid, jobs=jobs)
            except Exception:  # noqa: BLE001 -- the route still closes; a live attempt is read again below
                pass
    route_autoclose.close_one(Path(route["artifact_root"]), Path(route_file), route, "start-refused", api,
                              trigger="compose", now=time.time())


def settle(receipt: dict | None, errors: str, env: dict) -> dict | None:
    """After a start that did not confirm: the attempt it handed over keeps it (its receipt); otherwise
    nothing of it stays open or starts later, and the native call runs (None)."""
    receipt = receipt or {}
    if _closed_by_the_start(receipt):
        return None
    route_file = receipt.get("route_file") or next(
        (line[len("route_file="):].strip() for line in reversed(errors.splitlines())
         if line.startswith("route_file=")), "")
    if not route_file:
        return None                                   # no route was written
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)                            # the session the route belongs to stops and closes it
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        api = _route_api()
        jobs = Path(api._compose_default_jobs())
        found = handed_over(route, route_file, jobs)
        if found is None:
            try:
                withdraw(route, route_file, jobs, api)
            finally:
                found = handed_over(route, route_file, jobs)   # one the close could not stop keeps the start
        return found
    finally:
        os.environ.clear()
        os.environ.update(saved)


def reason_for(receipt: dict, depth: str) -> str:
    """The refusal line: the route, and how its answer reaches this session (the receipt's own directive)."""
    head = f"Started as Hearting unit route {receipt['route_id']} ({UNIT}, {depth}, no record)."
    if receipt.get("state") == "completed":
        summary = str(receipt.get("summary") or "").strip()
        if summary:
            return f"{head} It has already finished; its answer:\n{summary}\nDo not call it again."
        return (f"{head} It has already finished; read its result once with: {receipt.get('resume_command', '')} "
                "Do not call it again.")
    if receipt.get("parent_next") == "end-turn":
        return (f"{head} Its answer, or what stopped it, comes back to this session as a completion notice. "
                "Do not call it again; go on with other work or end the turn.")
    command = receipt.get("parent_next_command") or receipt.get("resume_command") or ""
    return (f"{head} No completion notice reaches this session: run this once to receive its answer: {command} "
            "Do not call it again.")


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
        require_shipped_definition(harness, call["name"] or DEFAULT_AGENT[harness], cwd)
        require_sole_decision(harness, str(payload.get("tool_name") or ""), cwd)
        receipt = start(harness, call, depth, cwd, seconds=seconds)
        return None if receipt is None else reason_for(receipt, depth)
    except BaseException:  # noqa: BLE001 -- any doubt, failure or interruption lets the native call run
        return None


def main(argv=None, stdin=None) -> int:
    try:
        argv = sys.argv[1:] if argv is None else argv
        harness = next((flag[2:] for flag in argv if flag in ("--claude", "--codex")), "")
        if harness not in _TOOLS:
            return 0
        payload = json.loads((stdin or sys.stdin).read() or "{}")
        reason = redirect(harness, payload)
        if reason is None:
            return 0
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}},
                         ensure_ascii=False, separators=(",", ":")))
        return 0
    except BaseException:  # noqa: BLE001 -- the hook never fails the call: the native subagent runs
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
