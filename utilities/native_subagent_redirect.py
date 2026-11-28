#!/usr/bin/env python3
"""A person's session's native subagent call becomes a Hearting unit route when that is certain.

Claude `Agent` (older `Task`) and Codex `spawn_agent` (multi-agent v1) are intercepted before they run
(`PreToolUse`). Whether Hearting takes the call is decided once, before anything is registered; anything
that is not certain leaves the native call exactly as it was, with nothing printed and nothing registered:
- The definition the harness picks for the name is Hearting's shipped one (apart from the lines activation
  renders for the user's models) or the harness's built-in, and the call carries nothing a brief cannot.
- Nothing else decides the same call: no other hook or permission rule in a configuration the hook reads,
  no layer added on the harness's command line, and Hearting's own hook registered exactly once.
- The caller is a person's session or the one worker form below, and not a native subagent (unknown counts as
  not).

Then the hook runs, as a session that typed it would,

    capability-route.py compose --start --unit general/explore --profile <depth> --ephemeral --prompt-file -

and reads its start receipt. Once the receipt says Hearting holds the call (running, waiting for a free
worker, or ended) the native call is refused with one line naming the route and how its answer comes back
(`capabilities/unit.md`, A native subagent call). A usage limit or a failed start after that reaches the
session through the runtime's existing wait and its completion or attention notice, never as the native
call. Without a receipt that says Hearting holds the call (none, or a refusal) the native call runs. The hook
never stops a start. Two windows are left where both can run: a hook ended from outside (killed, the
harness's timeout) between the registration and its refusal line, and a compose that fails after registering
without writing such a receipt.

The one worker taken is a live Claude owner the session supervisor runs detached, proven by its row and announced
session, on a route sealing no native stage fallback, whose launch lets it run its wait (`_supervised_owner`,
`_wait_allowed`; its own launch layers count only when they decide the call). It starts in its own environment, so
the leg stops with it (`capabilities/unit.md`), and is told that wait, to repeat until the answer comes. A start
whose route compose closed (the governor's wait, a usage limit right after registration) lets the native call run.
Every other worker (stage, review, sub-session, foreground-scoped, a unit route's leg, Codex's `-c`) stays native.

OpenCode is not intercepted (`adapters/opencode/ADAPTATION.md`).

Usage (stdin: the harness payload):
  native_subagent_redirect.py --claude|--codex   print the PreToolUse deny JSON when Hearting took the call
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILITIES = ROOT / "utilities"
UNIT = "general/explore"
# How often the hook reads the start's receipt; the compose process finishes its bookkeeping on its own.
POLL_SECONDS = 0.05
_TOOLS = {"claude": {"Agent", "Task"}, "codex": {"spawn_agent"}}
# The arguments a unit route takes over: the brief, its title and the agent name. Anything else stays
# native: Codex multi-agent v2 (`task_name`, `fork_turns`), attachments (`items`), a model or an effort,
# Claude `resume` or `isolation`, and any argument this table does not know.
_ARGUMENTS = {"claude": {"description", "prompt", "subagent_type", "run_in_background"},
              "codex": {"agent_type", "message", "fork_context"}}
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
_SUFFIX = {"claude": ".md", "codex": ".toml"}
# Lines activation renders into a shipped definition for the user's models (`sync-native-agents.py`).
_RENDERED = {"claude": re.compile(r"(# profile:|model:|effort:)"),
             "codex": re.compile(r"(model|model_reasoning_effort)\s*=")}
# Harness command-line arguments that add definitions, settings, hooks or permission rules no file shows.
_LAUNCH_LAYERS = {"claude": {"--settings", "--agents", "--add-dir", "--plugin-dir", "--setting-sources",
                             "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
                             "--permission-prompt-tool"},
                  "codex": {"-c", "--config", "-p", "--profile", "--enable", "--disable"}}
_TOOL_FLAGS = ("--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools")
# The managed (Claude) and system (Codex) configuration roots the harness reads before the user's.
MANAGED = {"claude": (Path("/etc/claude-code"), Path("/Library/Application Support/ClaudeCode")),
           "codex": (Path("/etc/codex"),)}
# Where this release registers the hook, and the tool name its matcher names.
_REGISTRATION = {"claude": ("settings.json", "Agent"), "codex": ("hooks/hooks.json", "spawn_agent")}


class Skip(Exception):
    """Not a call Hearting takes: the native call goes through."""


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalized(harness: str, payload: dict) -> dict:
    """`{session, name, prompt, title}` of a subagent call Hearting can take, or `Skip`."""
    tool, session, args = payload.get("tool_name"), payload.get("session_id"), payload.get("tool_input")
    if str(tool or "").rsplit(".", 1)[-1] not in _TOOLS.get(harness, ()) or not isinstance(args, dict):
        raise Skip("not-a-subagent-call")
    if not isinstance(session, str) or not session or set(args) - _ARGUMENTS[harness]:
        raise Skip("form-not-carried")
    if harness == "claude":
        name, prompt, title = args.get("subagent_type"), args.get("prompt"), args.get("description")
    else:
        name, prompt, title = args.get("agent_type"), args.get("message"), ""
    # The session's history handed over (Claude `fork`, Codex `fork_context`), or a name that is no name.
    if name == "fork" or args.get("fork_context") not in (None, False) or not isinstance(name, (str, type(None))):
        raise Skip("form-not-carried")
    if not _text(prompt):
        raise Skip("no-prompt")
    return {"session": session, "name": _text(name), "prompt": _text(prompt), "title": _text(title)}


def depth_for(harness: str, call: dict) -> str:
    """The depth of the route a call starts; `Skip` when the name is not in the table."""
    if not call["name"]:
        return UNNAMED[harness]
    if call["name"] not in NAMES:
        raise Skip("name-not-in-table")
    return NAMES[call["name"]]


# --- The definition the harness picks for a name ------------------------------------------------------------

def _home(harness: str) -> Path:
    if harness == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or Path.home() / ".claude")
    return Path(os.environ.get("CODEX_HOME", "").strip() or Path.home() / ".codex")


def _walk(cwd: str, folder: str, home: Path) -> list[Path]:
    """`<dir>/<folder>` for `cwd` and every directory above it, except the user's own home folder (a wider walk
    only finds more reasons to stay native)."""
    start = Path(cwd).resolve()
    seen, found = {home.resolve()}, []
    for base in (start, *start.parents):
        if (base / folder).resolve() not in seen:
            seen.add((base / folder).resolve())
            found.append(base / folder)
    return found


def _main_checkout(cwd: str) -> str | None:
    """The main checkout of the linked git worktree `cwd` is in (Codex reads its project layers too)."""
    for base in (Path(cwd).resolve(), *Path(cwd).resolve().parents):
        marker = base / ".git"
        if marker.is_file():
            gitdir = (base / marker.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()).resolve()
            return str((gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve().parent)
        if marker.exists():
            return None
    return None


def _projects(harness: str, cwd: str) -> list[Path]:
    """The project folders the harness reads: `.claude` walking up from the working directory (and from
    CLAUDE_PROJECT_DIR); `.codex` walking up, and from a linked worktree's main checkout too."""
    roots = [cwd]
    if harness == "claude" and os.environ.get("CLAUDE_PROJECT_DIR", "").strip():
        roots.append(os.environ["CLAUDE_PROJECT_DIR"].strip())
    if harness == "codex" and _main_checkout(cwd):
        roots.append(_main_checkout(cwd))
    return [folder for root in roots for folder in _walk(root, "." + harness, _home(harness))]


def _comparable(harness: str, text: str) -> str:
    """A definition's text apart from the lines activation renders (Claude: in the frontmatter; Codex: before
    `developer_instructions`)."""
    head, sep, rest = text.partition("\n---\n" if harness == "claude" else "\ndeveloper_instructions")
    return "\n".join(line for line in head.split("\n") if not _RENDERED[harness].match(line)) + sep + rest


def _is_shipped(harness: str, path: Path, name: str) -> bool:
    """The file is Hearting's shipped definition of `name`: a link to it, or the same text apart from the
    rendered lines (an added field, a comment or any other change is not)."""
    shipped = ROOT / "adapters" / harness / "agents" / (name + _SUFFIX[harness])
    if path.is_symlink():
        return path.resolve() == shipped.resolve()
    return shipped.is_file() and (_comparable(harness, path.read_text(encoding="utf-8"))
                                  == _comparable(harness, shipped.read_text(encoding="utf-8")))


def _gives(harness: str, path: Path, name: str) -> bool:
    """The definition file can give the agent `name`. Hearting's own file gives its own name; Codex reads the
    TOML `name` (the file name without one); any other Claude file counts when the name stands anywhere in
    its frontmatter, so a comment, quotes or another YAML form cannot hide it."""
    if _is_shipped(harness, path, path.stem):
        return path.stem == name
    text = path.read_text(encoding="utf-8")
    if harness == "codex":
        found = tomllib.loads(text).get("name", path.stem)
        return isinstance(found, str) and found.strip() == name
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text.split("\n---", 1)[0]) is not None


def _definitions(harness: str, cwd: str, name: str) -> tuple[list[Path], list[Path]]:
    """`(user_files, others)` that can give the agent `name`: the user's own folder, and every other place the
    harness reads (managed or system, projects, a Codex `[agents.*]` role or the `config_file` it names)."""
    home = _home(harness)
    if harness == "claude":
        folders = [root / sub for root in MANAGED[harness] for sub in ("agents", ".claude/agents")]
    else:
        folders = [root / "agents" for root in MANAGED[harness]]
    folders += [project / "agents" for project in _projects(harness, cwd)]

    def giving(folder):
        return [path for path in sorted(folder.rglob("*" + _SUFFIX[harness]))
                if _gives(harness, path, name)] if folder.is_dir() else []
    user, others = giving(home / "agents"), [path for folder in folders for path in giving(folder)]
    if harness == "codex":
        for config in (*MANAGED[harness], home, *_projects(harness, cwd)):
            config = config / "config.toml"
            roles = tomllib.loads(config.read_text(encoding="utf-8")).get("agents") if config.is_file() else {}
            for key, role in (roles or {}).items():
                if isinstance(role, dict) and (key == name or (
                        "config_file" in role and _gives(harness, config.parent / role["config_file"], name))):
                    others.append(config)
    return user, others


def require_shipped_definition(harness: str, name: str, cwd: str) -> None:
    """`Skip` unless the definition the harness picks for `name` is Hearting's shipped one or its built-in."""
    user, others = _definitions(harness, cwd, name)
    if others:
        raise Skip("definition-elsewhere")          # a project, managed or declared definition comes first
    if not ((len(user) == 1 and _is_shipped(harness, user[0], name)) or (not user and name in BUILT_IN[harness])):
        raise Skip("definition-not-shipped")


# --- Other decisions on the same call -----------------------------------------------------------------------

def _hook_matches(harness: str, matcher, tools: set) -> bool:
    """The harness's matcher rule: empty or `*` is every tool, a plain list is exact names, else a regex."""
    if matcher in (None, "", "*") or not isinstance(matcher, str):
        return True
    plain = r"[A-Za-z0-9_|]+" if harness == "codex" else r"[A-Za-z0-9_\-, |]+"
    if re.fullmatch(plain, matcher):
        return bool(tools & {part.strip() for part in re.split(r"[|,]", matcher)})
    try:
        return any(re.search(matcher, tool) for tool in tools)
    except re.error:
        return True


def _rules_decide(settings: dict, tools: str = "Agent|Task") -> bool:
    """A Claude deny or ask rule names the subagent tool (or `tools`): it applies whatever a hook returns."""
    permissions = settings.get("permissions") if isinstance(settings.get("permissions"), dict) else {}
    return any(isinstance(rule, str) and re.match(rf"\s*({tools})\s*(\(|$)", rule)
               for kind in ("deny", "ask") for rule in permissions.get(kind) or [])


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"not an object: {path}")
    return value


def _plugin_hooks(root: Path) -> list:
    """The hook sets an installed plugin declares: `hooks/hooks.json` (or `hooks.json`) and its manifest's."""
    found = [_json(path).get("hooks") for path in (root / "hooks" / "hooks.json", root / "hooks.json")
             if path.is_file()]
    for manifest in (root / ".claude-plugin" / "plugin.json", root / ".codex-plugin" / "plugin.json"):
        declared = _json(manifest).get("hooks") if manifest.is_file() else None
        for entry in declared if isinstance(declared, list) else [declared] if declared else []:
            found.append(entry.get("hooks", entry) if isinstance(entry, dict)
                         else _json((root / entry).resolve()).get("hooks"))
    return found


def _decision_sources(harness: str, cwd: str) -> list:
    """`[(hooks, settings)]` of every configuration file this harness reads that the hook can see."""
    home = _home(harness)
    if harness == "claude":
        files = [home / "settings.json", home / "settings.local.json"]
        for managed in MANAGED[harness]:
            files += [managed / "managed-settings.json", *sorted((managed / "managed-settings.d").glob("*.json"))]
        files += [folder / name for folder in _projects(harness, cwd) for name in ("settings.json", "settings.local.json")]
        sources = [(settings.get("hooks"), settings) for settings in (_json(path) for path in files if path.is_file())]
        plugins = sorted((home / "plugins" / "cache").glob("*/*/*"))
    else:
        sources = []
        for layer in (*MANAGED[harness], home, *_projects(harness, cwd)):
            sources += [(_json(layer / "hooks.json").get("hooks"), {})] if (layer / "hooks.json").is_file() else []
            sources += [(tomllib.loads((layer / name).read_text(encoding="utf-8")).get("hooks"), {})
                        for name in ("config.toml", "requirements.toml") if (layer / name).is_file()]
        plugins = [path for pattern in ("*", "*/*", "*/*/*", "*/*/*/*") for path in sorted((home / "plugins").glob(pattern))]
    return sources + [(hooks, {}) for plugin in plugins if plugin.is_dir() for hooks in _plugin_hooks(plugin)]


def _harness_argv() -> list[str]:
    """The command line of the harness that runs this hook: the nearest ancestor that is not a shell or Python
    (`/proc`; where it cannot be read this raises and the call stays native)."""
    pid = os.getppid()
    for _ in range(12):
        argv = [arg.decode("utf-8", "replace") for arg in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1]]
        if argv and not re.fullmatch(r"-?(sh|bash|dash|zsh|ash|fish|env|python[0-9.]*)", Path(argv[0]).name):
            return argv
        pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    raise OSError("no harness process above this hook")


def _values(argv: list[str], index: int) -> list[str]:
    """What the flag at `index` was given: after its `=`, or the arguments up to the next flag."""
    if "=" in argv[index]:
        return [argv[index].split("=", 1)[1]]
    values = []
    for arg in argv[index + 1:]:
        if arg.startswith("-"):
            break
        values.append(arg)
    return values


def _given(argv: list[str], flags: tuple) -> list[str]:
    """Every rule the `flags` were given, one each; an empty value or no plain name with an optional `(…)` is `?`."""
    rules = [rule for index, arg in enumerate(argv) if arg.split("=", 1)[0] in flags
             for value in _values(argv, index) or [""]
             for rule in re.findall(r"[^\s,()]+(?:\([^()]*\))?|[()]", value) or [""]]
    return [rule if re.fullmatch(r"[A-Za-z][\w-]*(\([^()]*\))?", rule) else "?" for rule in rules]


def _worker_layer(flag: str, values: list[str]) -> bool:
    """A layer the owner's launch writes (`claude-session-supervisor.py`) that leaves this call alone: its park hook
    outside the terminal cleanup it alone refuses in, absolute folders with no `.claude`, rules naming no subagent."""
    if flag == "--settings" and len(values) == 1:
        park = shlex.quote(str(ROOT / "adapters" / "claude" / "hooks" / "registered-parent-park.py"))
        import dispatch_terminal_commit
        return json.loads(values[0]) == {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": park, "timeout": 10}]}]}} and dispatch_terminal_commit.load_active_cleanup_scope(
            Path(os.environ["AGENT_DISPATCH_JOBS"]).parent, os.environ["AGENT_DISPATCH_ATTEMPT_ID"]) is None
    if flag == "--add-dir":
        return bool(values) and all(os.path.isabs(value) and not (Path(value) / ".claude").exists() for value in values)
    rules = _given([flag, *values], (flag,)) if flag in _TOOL_FLAGS else ["?"]
    return "?" not in rules and not {rule.split("(")[0] for rule in rules} & {"Agent", "Task"}


def _wait_allowed(argv: list[str], cwd: str) -> bool:
    """The owner can run its wait (`_worker_command`) unprompted: its launch's own allow rule for it in a mode that
    applies it, and no Bash deny or ask rule in its launch or a settings file the hook reads (no bare Bash grant)."""
    from parent_next_directive import worker_utility
    home = os.environ["AGENT_HOME"]
    return (bool(home) and shlex.quote(home) == home
            and _given(argv, ("--permission-mode",)) in (["bypassPermissions"], ["acceptEdits"])
            and f"Bash({worker_utility(home, 'capability-route.py')} *)" in _given(argv, _TOOL_FLAGS[:2])
            and not any(rule.split("(")[0] in ("Bash", "?") for rule in _given(argv, _TOOL_FLAGS[2:]))
            and not any(_rules_decide(settings, "Bash") for _, settings in _decision_sources("claude", cwd)))


def require_sole_decision(harness: str, tool: str, cwd: str, worker: bool = False) -> None:
    """`Skip` unless this hook is the only decision on the call it can see: the harness runs matching hooks
    side by side and applies deny/ask rules after them, so another decision would come after the start. For the
    owner's call (`worker`) the layers its launch adds count only when they decide it, and its wait must run."""
    argv = _harness_argv()[1:]
    for index, arg in enumerate(argv):
        if arg.split("=", 1)[0] in _LAUNCH_LAYERS[harness] or (harness == "codex" and re.match(r"-[cp].", arg)):
            if not worker or not _worker_layer(arg.split("=", 1)[0], _values(argv, index)):
                raise Skip("launch-layer")
    if worker and not _wait_allowed(argv, cwd):
        raise Skip("wait-not-allowed")
    if harness == "claude" and (_home(harness) / "remote-settings.json").exists():
        raise Skip("server-managed-settings")      # the copy of settings an organization's server manages
    path, name = _REGISTRATION[harness]
    ours = {handler.get("command") for group in _json(ROOT / "adapters" / harness / path)["hooks"]["PreToolUse"]
            if name in str(group.get("matcher")).split("|") for handler in group["hooks"]}
    # Codex also picks `spawn_agent` hooks by the alias `Agent` (core/src/tools/hook_names.rs).
    tools, registered = {tool} | ({"Agent", "Task"} if harness == "claude" else {"Agent"}), 0
    for hooks, settings in _decision_sources(harness, cwd):
        if harness == "claude" and _rules_decide(settings):
            raise Skip("another-decision")
        for group in (hooks.get("PreToolUse") or []) if isinstance(hooks, dict) else []:
            if not isinstance(group, dict) or not _hook_matches(harness, group.get("matcher"), tools):
                continue
            # A group on every tool (matcher none, empty or `*`) decides nothing about this call, unless a handler's
            # own `if` (Claude: one permission rule) names the tool or cannot be read as one tool name; a matcher
            # that names or patterns the tool still decides. Where such a hook would refuse or ask, the route has
            # started.
            every_tool = group.get("matcher") in (None, "", "*")
            for handler in group.get("hooks") or []:
                if not (isinstance(handler, dict) and handler.get("type", "command") == "command"
                        and handler.get("command") in ours):
                    rule = handler.get("if") if isinstance(handler, dict) else None
                    named = rule.split("(", 1)[0].strip() if isinstance(rule, str) else ""
                    if every_tool and (rule is None or (named.isidentifier() and named not in tools)):
                        continue
                    raise Skip("another-decision")
                registered += 1
    if registered != 1:
        raise Skip("registered-twice-or-unseen")    # twice may run twice; unseen means a layer this hook cannot read


# --- Who calls --------------------------------------------------------------------------------------------

def _supervised_owner(session: str) -> None:
    """`Skip` unless the caller is a live Claude owner the session supervisor runs detached (its registry row; the
    environment only names it) on its route, this hook's Claude session the one it announced, the route sealing no
    native stage fallback (whose evidence would be lost), and a leg started here launching detached too (the receipt
    comes back at once, the leg stops with the owner: `dispatch-reap-watch.py`). Anything else stays native."""
    from dispatch_contract import attempt_governed_process_quiescence, row_is_subsession
    from dispatch_lifecycle import DETACHED, select_launch_lifecycle
    from dispatch_registry_cache import registry_view
    route = json.loads(Path(os.environ["AGENT_ROUTE_FILE"]).read_text(encoding="utf-8"))
    capability, route_id = route.get("capability"), route["route_id"]
    if any(hop.get("fallback_hop") == "native-subagent" and hop.get("candidates")
           for node in route["nodes"] for hop in node.get("fallback_hops") or []):
        raise Skip("native-stage-fallback")
    record, error = registry_view(Path(os.environ["AGENT_DISPATCH_JOBS"])).completion_attempt(
        os.environ["AGENT_DISPATCH_ATTEMPT_ID"])
    meta = {**record.completion_metadata(), "_status": record.status} if record is not None and not error else {}
    with open(meta.get("log_file") or os.devnull, "rb") as log:   # the session its supervisor announced first
        announced = [row.get("session_id") for row in map(_last_json, log.read(65536).decode("utf-8", "replace")
                     .splitlines()) if row.get("type") == "dispatch.supervisor.session"]
    if not (isinstance(capability, str) and capability.strip() and capability != "unit"
            and route_id and os.environ.get("AGENT_ROUTE_ID") == route_id
            and os.environ.get("AGENT_DISPATCH_COMPLETION_MODE") == "supervised" and meta.get("_status") in {"open", "running"}
            and meta.get("harness") == "claude" and meta.get("worker_type") == "owner" and not row_is_subsession(meta)
            and meta.get("launch_lifecycle") == DETACHED and route_id in {meta.get("route_id"), meta.get("owner_route_id")}
            and announced == [session] and attempt_governed_process_quiescence(meta).state == "live"
            and select_launch_lifecycle() == DETACHED):
        raise Skip("not-the-supervised-owner")


def caller(harness: str, payload: dict, session: str) -> str:
    """`session` (a person's) or `worker` (`_supervised_owner`); `Skip` for a native subagent, any other worker
    (the question redirect's test) or what cannot be told. A sub-session record that cannot be read counts as a
    native subagent: nothing is certain about it."""
    import session_identity
    import session_tidy as tidy
    if payload.get("agent_id") or session_identity.known_sub_session_root(harness, session) != session:
        raise Skip("native-subagent")
    if harness == "claude" and os.environ.get("AGENT_DISPATCH_CHILD") == "1":
        _supervised_owner(session)
        return "worker"
    if tidy.is_worker() or os.environ.get("AGENT_SESSION_ROLE", "").lower() in {"owner", "worker", "auxiliary", "reviewer"}:
        raise Skip("worker")
    return "session"


def brief(call: dict) -> str:
    return (call["title"] + "\n\n" if call["title"] and call["title"] not in call["prompt"] else "") + call["prompt"] + "\n"


def compose_env(harness: str, session: str) -> dict:
    """The session's own identity, as its tool commands carry it: no inherited caller name or other
    harness's session id (core/OPERATIONS.md), and this harness's id set from the payload. Nobody waits on
    this start's own bounded wait (the session is told to go on), as inside an automatic resume, so a full
    worker slot arms the runtime's capacity resume (`capacity_auto_resume.py`) and the answer comes later."""
    from harness_capabilities import session_env
    env = dict(os.environ)
    env.pop("AGENT_DISPATCH_CALLER_HARNESS", None)
    for names in session_env().values():
        for name in names:
            env.pop(name, None)
    env[session_env()[harness][0]] = session
    env["AGENT_CAPACITY_RESUME_CHAIN"] = "0"
    return env


# --- The start ---------------------------------------------------------------------------------------------

def _last_json(text: str) -> dict:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line) if line.strip().startswith("{") else None
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def registered(receipt: dict, worker: bool = False) -> bool:
    """The start receipt says Hearting holds the call: running, waiting for a worker, or already ended. A worker's
    start that follows no attempt holds nothing: compose closed that route (`_close_unstarted_record_free`: the
    governor's wait, or a leg stopped at a usage limit right after its registration; no later start is armed)."""
    held = receipt.get("state") in {"running", "completed"} or bool(receipt.get("registered_attempts"))
    return bool(receipt.get("route_id")) and (held or (bool(receipt.get("auto_resume")) if worker else
                                                       receipt.get("state") == "waiting-capacity" or bool(receipt.get("result"))))


def start(harness: str, call: dict, depth: str, cwd: str, worker: bool = False) -> dict | None:
    """Run the start as the caller would (a worker in its own environment); its receipt when Hearting holds the
    call, else None. The hook returns once the receipt is written and never stops the start: the compose process
    finishes on its own."""
    env = dict(os.environ) if worker else compose_env(harness, call["session"])
    with tempfile.TemporaryFile() as given, tempfile.TemporaryFile() as out:
        # Files, not pipes: no write waits on the start, and a worker it launches holds nothing the harness
        # waits on.
        given.write(brief(call).encode("utf-8"))
        given.seek(0)
        child = subprocess.Popen(
            [sys.executable, "-B", str(UTILITIES / "capability-route.py"), "compose", "--start", "--unit", UNIT,
             "--profile", depth, "--ephemeral", "--prompt-file", "-", "--cwd", cwd],
            stdin=given, stdout=out, stderr=subprocess.DEVNULL, cwd=cwd, env=env, start_new_session=True)
        while True:
            ended = child.poll() is not None
            out.seek(0)
            receipt = _last_json(out.read().decode("utf-8", "replace"))
            if receipt.get("state") or ended:
                return receipt if registered(receipt, worker) else None
            time.sleep(POLL_SECONDS)


def _worker_command(command: str) -> str:
    """The receipt's `capability-route.py start …` on the owner's launch release, as its allow rules name it."""
    from parent_next_directive import worker_utility
    if Path((argv := shlex.split(command))[1]).name != "capability-route.py" or argv[2] != "start":
        raise ValueError(f"not a start command: {command}")
    return f"{worker_utility(os.environ['AGENT_HOME'], 'capability-route.py')} {shlex.join(argv[2:])}"


def reason_for(receipt: dict, depth: str, harness: str = "", worker: bool = False) -> str:
    """The refusal line: the route, and how its answer reaches this session (the receipt's own directive; a
    worker gets no completion notice, so its line gives the receipt's wait, one start window per run)."""
    waiting = receipt.get("state") == "waiting-capacity"
    head = (f"{'Queued' if waiting else 'Started'} as Hearting unit route {receipt['route_id']} ({UNIT}, {depth}, "
            "no record)." + (" It waits for a free worker and does not run natively." if waiting else ""))
    if receipt.get("state") == "completed" and str(receipt.get("summary") or "").strip():
        return f"{head} It has already finished; its answer:\n{str(receipt['summary']).strip()}\nDo not call it again."
    ended, command = receipt.get("state") == "completed" or receipt.get("result"), receipt.get("resume_command")
    if worker:   # no notice reaches a worker: the receipt's own command, in the form its allow rules let it run
        run = _worker_command(command if ended else receipt.get("parent_next_command") or command + " --wait")
        return (f"{head} It has already ended; read its result once with: {run} Do not call it again." if ended else
                f"{head} No completion notice reaches a worker: run {run} to receive its answer. One run waits up to "
                "ten minutes; while it brings no answer yet, run the same command again. Do not call the subagent again.")
    if ended and str(command or "").strip():
        return f"{head} It has already ended; read its result once with: {command} Do not call it again."
    if ended or receipt.get("parent_next") == "end-turn":   # ended with no command to read it: a cancelled unit
        idle = (" If this session went idle before the queued worker started, the notice comes with the next message."
                if waiting and harness == "claude" else "")
        return (f"{head} Its answer, or what stopped it, comes back to this session as a completion notice.{idle} "
                "Do not call it again; go on with other work or end the turn.")
    command = receipt.get("parent_next_command") or receipt.get("resume_command") or ""
    return (f"{head} No completion notice reaches this session: run this once to receive its answer: {command} "
            "Do not call it again.")


def redirect(harness: str, payload: dict) -> str | None:
    """The refusal text when Hearting took the call, else None (the native call goes through)."""
    if harness not in _TOOLS or not isinstance(payload, dict):
        return None
    try:
        sys.path.insert(0, str(UTILITIES))
        call = normalized(harness, payload)
        worker = caller(harness, payload, call["session"]) == "worker"
        depth = depth_for(harness, call)
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else os.getcwd()
        if not os.path.isdir(cwd):
            return None
        require_shipped_definition(harness, call["name"] or DEFAULT_AGENT[harness], cwd)
        require_sole_decision(harness, str(payload.get("tool_name") or ""), cwd, worker)
    except BaseException:  # noqa: BLE001 -- any doubt or failure before the start lets the native call run
        return None
    try:
        receipt = start(harness, call, depth, cwd, worker)
        return None if receipt is None else reason_for(receipt, depth, harness, worker)
    except BaseException:  # noqa: BLE001 -- a start that cannot be read registered nothing this hook knows of
        return None


def main(argv=None, stdin=None) -> int:
    try:
        argv = sys.argv[1:] if argv is None else argv
        harness = next((flag[2:] for flag in argv if flag in ("--claude", "--codex")), "")
        if harness not in _TOOLS:
            return 0
        reason = redirect(harness, json.loads((stdin or sys.stdin).read() or "{}"))
        if reason is not None:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason}},
                             ensure_ascii=False, separators=(",", ":")))
        return 0
    except BaseException:  # noqa: BLE001 -- the hook never fails the call: the native subagent runs
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
