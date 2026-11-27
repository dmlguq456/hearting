#!/usr/bin/env python3
"""A person's session's native subagent call, started as a Hearting unit route (H1).

Loaded by ``harness_behavior_scenarios.py`` (``_load_extra``).

``subagent-hook-seam``
    Measures the seam itself: a recording hook is added beside the installed ones (Claude
    ``PreToolUse`` ``Agent|Task``, Codex ``PreToolUse`` on every tool, OpenCode a second
    plugin's ``tool.execute.before``), and the session spawns one native subagent, in the TUI and
    headless. The oracle is whether the recording hook saw the subagent tool before the subagent's
    own request reached the model, and with which tool name and argument keys.

``subagent-redirect``
    The person's session (the TUI) calls its native subagent tool with a name of the table (the light
    name each harness knows). On Claude and Codex the installed hook starts it as a ``general/explore``
    unit route at ``light``, the native call is refused with the route's one line (no native subagent
    request reaches the model), the one worker answers and ends, and its answer comes back to the
    resting session as the completion notice (``Answer:``). On OpenCode, which is not intercepted, the
    native subagent runs and no unit route starts. The oracle also keeps the texts the session saw and
    three times, each named for what it spans: the call to the tool result
    (``call_to_tool_result_seconds``), the call to the first worker request
    (``call_to_worker_request_seconds``) and the fixture model's release of the worker's last reply to
    the notice (``release_to_notice_seconds``).

``subagent-redirect-no-answer``
    The same, but the worker ends with an empty answer: what the session is told when no answer comes.

``subagent-redirect-capacity``
    The dispatch class cap is one and another unit route holds that slot: the hook's start is refused by
    the governor, the native subagent runs as before, no later start is armed, no route is left open,
    and none starts after the slot frees.

``subagent-redirect-untouched``
    Calls the hook leaves alone: a user agent (a name not in the table), the user's own definition of a
    table name (the light name), the session's own history handed over (Claude ``fork``, Codex
    ``fork_context``), and a registered worker's call. No unit route starts for any of them.

``subagent-redirect-other-hook`` (Claude, Codex)
    Another ``PreToolUse`` hook that refuses the subagent call is registered beside Hearting's. The
    harness runs both side by side, so Hearting's hook must start nothing: no unit route row, no
    worker request and no native subagent request reach the model.

``subagent-redirect-usage-limit`` (Claude, Codex)
    The redirected route's worker hits a usage limit (a loopback 429 whose reset is about 45 s away) after
    the hook already refused the native call: the session must be told about its route (the answer, or
    the notice's next action) and no native subagent may run; whether the answer itself arrived after the
    reset is recorded beside the verdict.

``subagent-redirect-cost`` (Claude, Codex)
    One open session, one prompt and one fixture model: the call made under the registration from before
    this change (native), then, with the hook settings rewritten while the session stays open, the same
    call again. It records for each the call, the first subagent or worker request, the parent's tool
    result and the answer's arrival, and whether the open session took the new registration.

``subagent-redirect-faults`` (Claude, Codex)
    The registered redirect command is replaced, one at a time, by a command that fails the way a hook
    can fail (exits 1, is killed, prints garbage, runs past its timeout), and by the registration a session
    opened before this change carries (no ``--redirect``, 15 s). Each time the native subagent must still
    run. Exit 2 is the control: the harnesses read it (with stderr) as a block, so the native subagent must
    not run; the shared redirect never returns it, and Codex's runner turns the bridge's own failures into 0.

Everything stays in the fixture root; no user config is read or written.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
import harness_behavior_cases_b as cb  # noqa: E402
import harness_behavior_cases_e as ce  # noqa: E402
from harness_behavior_cases_e import TERMINAL_STATES, registry_rows, _base  # noqa: E402
import harness_behavior_cases_unit_route as unit  # noqa: E402
from harness_behavior_cases_unit_nested import _report_shell  # noqa: E402

# The subagent tool each harness offers, as the model sees it (evidence; the first seen is kept).
SPAWN_SCHEMAS: dict = {}
_SPAWN_TOOLS = ("Agent", "Task", "multi_agent_v1", "collaboration", "subagent", "task")


def _capture_schemas(module):
    if getattr(module, "_subagent_redirect_patched", False) or not hasattr(module, "_tool_names"):
        return
    original = module._tool_names

    def tool_names(payload):
        for tool in payload.get("tools") or []:
            if isinstance(tool, dict):
                name = tool.get("name") or (tool.get("function") or {}).get("name")
                if name in _SPAWN_TOOLS and name not in SPAWN_SCHEMAS:
                    SPAWN_SCHEMAS[name] = json.loads(json.dumps(tool))
        return original(payload)
    module._tool_names = tool_names
    module._subagent_redirect_patched = True


for _module in list(sys.modules.values()):
    if _module is not None and hasattr(_module, "ModelFixture") and hasattr(_module, "_tool_names"):
        _capture_schemas(_module)


def codex_spawn_namespace() -> str:
    """The namespace whose tools carry `spawn_agent` (multi-agent v1, or v2's `collaboration`)."""
    for name in ("collaboration", "multi_agent_v1"):
        tools = (SPAWN_SCHEMAS.get(name) or {}).get("tools") or []
        if any(isinstance(t, dict) and t.get("name") == "spawn_agent" for t in tools):
            return name
    return "multi_agent_v1"


def spawn_call(agent, prompt: str, *, description: str = "scenario subtask", extra=None) -> cb.NativeCall:
    """The harness's native subagent call naming `agent` (None: no name), with `extra` arguments."""
    def build(protocol, names):
        if protocol == "anthropic":
            args = {"description": description, "prompt": prompt, **({"subagent_type": agent} if agent else {})}
            return "Agent", {**args, **(extra or {})}
        if protocol == "responses":
            namespace = codex_spawn_namespace()
            args = {"message": prompt, **({"agent_type": agent} if agent else {})}
            if namespace == "collaboration":
                args["task_name"] = "scenario_subtask"
            return f"{namespace}{ce.NAMESPACE_SEP}spawn_agent", {**args, **(extra or {})}
        args = {"description": description, "prompt": prompt, **({"agent": agent} if agent else {})}
        return "subagent", {**args, **(extra or {})}
    return cb.NativeCall(f"spawn:{agent}", build)

# The agent each harness spawns: a built-in one, on every harness.
SEAM_AGENT = {"claude": "Explore", "codex": "default", "opencode": "general"}

PROBE = r'''
import json, sys, time
raw = sys.stdin.read()
try:
    payload = json.loads(raw or "{}")
except ValueError:
    payload = {"unreadable": raw[:400]}
args = payload.get("tool_input") if isinstance(payload, dict) else None
row = {"at": time.time(), "tool": payload.get("tool_name"), "agent_id": payload.get("agent_id"),
       "session_id": payload.get("session_id"),
       "keys": sorted(args) if isinstance(args, dict) else None, "payload": payload}
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, default=str) + "\n")
'''

OPENCODE_PROBE = r'''
import { appendFileSync } from "node:fs"
const file = __FILE__
export default {
  id: "hb-subagent-seam",
  setup: async (ctx) => {
    await ctx.tool.hook("execute.before", async (event) => {
      const args = event.input && typeof event.input === "object" ? event.input : {}
      appendFileSync(file, JSON.stringify({ at: Date.now() / 1000, tool: event.tool, session_id: event.sessionID,
        keys: Object.keys(args).sort(), payload: { tool: event.tool, args, sessionID: event.sessionID } }) + "\n")
    })
  },
}
'''


def _write_own(path: Path, text: str, root: Path) -> None:
    """Write the fixture home's own copy: a source install links some files back into the tree.

    A linked file is re-linked to a copy in the fixture root (Codex trusts only a linked
    `hooks.json`, `tools/install/codex_hook_trust.py`), so nothing is written through the link."""
    if path.is_symlink():
        own = root / f"own-{path.name}"
        own.write_text(text, encoding="utf-8")
        path.unlink()
        path.symlink_to(own)
        return
    path.write_text(text, encoding="utf-8")


def _own_dir(path: Path) -> Path:
    """The fixture home's own folder: a source install links some folders back into the tree (Claude's
    `agents/`), so a linked one becomes a real folder of links to the same entries before anything is added."""
    if path.is_symlink():
        target = path.resolve()
        path.unlink()
        path.mkdir()
        for entry in target.iterdir():
            (path / entry.name).symlink_to(entry)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _probe_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


class SubagentHookSeamScenario(Scenario):
    name = "subagent-hook-seam"
    summary = ("Measure: does the harness's pre-tool seam (Claude/Codex PreToolUse, OpenCode tool.execute.before) "
               "see a native subagent spawn, in the TUI and headless, and with which tool name and arguments.")

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]

    def markers(self, mode):
        return f"SUBSEAM-MAIN-{mode}-{self.nonce}", f"SUBSEAM-SUB-{mode}-{self.nonce}"

    def prepare(self, run):
        SPAWN_SCHEMAS.clear()
        self.log = run.home.root / "seam.jsonl"
        probe = run.home.root / "seam-probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        command = f"{sys.executable} {probe} {self.log}"
        if run.harness == "claude":
            path = Path(run.home.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
            settings = json.loads(path.read_text(encoding="utf-8"))
            settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
                {"matcher": "Agent|Task", "hooks": [{"type": "command", "command": command, "timeout": 10}]})
            _write_own(path, json.dumps(settings, indent=1), run.home.root)
        elif run.harness == "codex":
            path = Path(run.home.env["CODEX_HOME"]) / "hooks.json"
            hooks = json.loads(path.read_text(encoding="utf-8"))
            hooks.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
                {"matcher": ".*", "hooks": [{"type": "command", "command": command, "timeout": 10}]})
            _write_own(path, json.dumps(hooks, indent=1), run.home.root)
            run.evidence["hook_trust_again"] = run.home.trust_codex_hooks(run.binary)
        else:
            plugin = Path(run.home.env["XDG_CONFIG_HOME"]) / "opencode" / "plugins" / "hb-subagent-seam" / "index.ts"
            _own_dir(plugin.parent.parent)
            plugin.parent.mkdir(parents=True, exist_ok=True)
            plugin.write_text(OPENCODE_PROBE.replace("__FILE__", json.dumps(str(self.log))), encoding="utf-8")

    def script(self, run):
        agent = SEAM_AGENT[run.harness]

        def answer(record, earlier):
            for mode in ("tui", "headless"):
                main, sub = self.markers(mode)
                if ce._is_sub(record, main, sub):
                    return Reply(f"sub done {mode}")
                if main not in record["text"]:
                    continue
                calls = [str(r["reply"].get("shell") or "") for r in earlier if main in r["text"]]
                if record["last_kind"] == "tool":
                    import re
                    found = re.search(r'"agent_id"\s*:\s*"([^"]+)"', record["last_text"])
                    if (run.harness == "codex" and found and codex_spawn_namespace() == "multi_agent_v1"
                            and f"wait:{found.group(1)}" not in calls):
                        return Reply(shell=ce.wait_call(found.group(1)))
                    return Reply(f"parent done {mode}")
                if not any(c.startswith("spawn:") for c in calls):
                    return Reply(shell=spawn_call(agent, f"{sub} say hi"))
                return Reply(f"parent done {mode}")
            return Reply("OK")
        return answer

    def _subs(self, run, mode):
        main, sub = self.markers(mode)
        return [r for r in run.main_requests() if ce._is_sub(r, main, sub)]

    def _seen(self, rows, since):
        return [{k: r.get(k) for k in ("tool", "keys", "agent_id")} for r in rows if r.get("at", 0) >= since
                and any(word in str(r.get("tool") or "").lower() for word in ("agent", "task", "subagent", "spawn"))]

    def drive(self, run):
        ev = run.evidence
        began = time.time()
        main, _sub = self.markers("tui")
        run.prompt(f"{main} spawn one sub-agent")
        wait_for(lambda: self._subs(run, "tui"), 120, 1)
        run.wait_idle(6, 90)
        rows = _probe_rows(self.log)
        ev["tui"] = {"sub_request": bool(self._subs(run, "tui")), "seen": self._seen(rows, began),
                     "all_tools_seen": sorted({str(r.get("tool")) for r in rows if r.get("at", 0) >= began})}
        began = time.time()
        main, _sub = self.markers("headless")
        argv = {"claude": [run.binary, "-p", "--permission-mode", "bypassPermissions"],
                "codex": [run.binary, "exec", "--skip-git-repo-check"],
                "opencode": [run.binary, "run"]}[run.harness]
        done = subprocess.run([*argv, f"{main} spawn one sub-agent"], env=run.home.env, cwd=str(run.home.workspace),
                              text=True, capture_output=True, timeout=240, stdin=subprocess.DEVNULL)
        time.sleep(2)
        rows = _probe_rows(self.log)
        ev["headless"] = {"exit": done.returncode, "sub_request": bool(self._subs(run, "headless")),
                          "seen": self._seen(rows, began),
                          "all_tools_seen": sorted({str(r.get("tool")) for r in rows if r.get("at", 0) >= began}),
                          "stderr_tail": done.stderr[-300:] if done.returncode else ""}
        sample = next((r["payload"] for r in rows if r in rows and any(
            word in str(r.get("tool") or "").lower() for word in ("agent", "task", "subagent", "spawn"))), None)
        ev["sample_payload"] = sample
        ev["spawn_schemas"] = dict(SPAWN_SCHEMAS)
        for mode in ("tui", "headless"):
            run.note(f"{mode}: sub={ev[mode]['sub_request']} seen={[s['tool'] for s in ev[mode]['seen']]}")
        # Each mode is its own measurement: a headless run whose subagent never reached the model proves nothing.
        if not (ev["tui"]["sub_request"] and ev["headless"]["sub_request"]):
            return "inconclusive"
        return "pass" if ev["tui"]["seen"] and ev["headless"]["seen"] else "gap"


class SubagentHookSeamV2Scenario(SubagentHookSeamScenario):
    name = "subagent-hook-seam-v2"
    summary = ("The same measurement with Codex multi-agent v2 switched on (`[features] multi_agent_v2`, off by "
               "default in Codex 0.171.0): its spawn tool lives in the `collaboration` namespace.")

    def prepare(self, run):
        if run.harness == "codex":
            config = Path(run.home.env["CODEX_HOME"]) / "config.toml"
            text = config.read_text(encoding="utf-8")
            if "multi_agent_v2" not in text:
                text = text.replace("[features]\n", "[features]\nmulti_agent_v2 = true\n", 1)
            _write_own(config, text, run.home.root)
        super().prepare(run)


# --------------------------------------------------------------------------
# The redirect: a native subagent call started as a unit route
# --------------------------------------------------------------------------

LIGHT_NAME = {"claude": "Explore", "codex": "explorer", "opencode": "explore"}
STARTED = "Started as Hearting unit route"
QUESTION = "where does the fixture README say what the fixture is for? Answer with the file and line."
ANSWER_HEAD = "Answered: the fixture README line 1 says what it is for"
USER_AGENT = "hb-user-agent"
SPAWN_TOOL_NAMES = {"Agent", "Task", "multi_agent_v1", "collaboration", "subagent", "task"}
HEADLESS = {"claude": ["-p", "--permission-mode", "bypassPermissions"], "codex": ["exec", "--skip-git-repo-check"],
            "opencode": ["run"]}


def explore_rows(jobs):
    return [row for row in registry_rows(jobs) if row.get("unit") == "general/explore"]


def attempt_ids(jobs, route_id: str) -> list[str]:
    """The attempt ids the registry holds for a route (a notice without an answer names its attempt)."""
    found = []
    try:
        lines = Path(jobs).read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for line in lines:
        if route_id and route_id in line:
            found += [m for m in re.findall(r"\batt-[0-9a-f]{8,}\b", line) if m not in found]
    return found


def _label_of(text: str, markers: dict):
    """The label whose marker appears last in `text` (a session's history holds the earlier prompts too)."""
    found = [(text.rfind(marker), label) for label, marker in markers.items() if marker in text]
    return max(found)[1] if found else None


def _shells_with(earlier, mark):
    return [str(r["reply"].get("shell") or "") for r in earlier if mark in r["text"]]


class SubagentRedirectScenario(unit.UnitRouteScenario):
    name = "subagent-redirect"
    summary = ("The person's session calls its native subagent tool: the hook starts it as a general/explore unit "
               "route, the native call is refused with one line, and the answer comes back as the completion notice.")
    WAIT = 240
    empty_answer = False
    WORKER_CALL = "SUBREDIR-WORKERCALL"

    def __init__(self):
        super().__init__()
        self.nonce = uuid.uuid4().hex[:8]
        self.main = f"SUBREDIR-MAIN-{self.nonce}"
        self.sub = f"SUBREDIR-SUB-{self.nonce}"
        self.native = []          # (time, label) of every native subagent request that reached the model
        self.results = {}         # label -> (time, text) of the first tool result after that label's call
        self.spawned = {}         # label -> time the session's model made the call

    # -- what the session calls ------------------------------------------------------------------
    def calls(self, run):
        """`[(label, agent, extra arguments)]`, one prompt each."""
        return [("light", LIGHT_NAME[run.harness], None)]

    def prepare(self, run):
        super().prepare(run)
        if run.harness == "claude":
            # The fixture's tier models are the loopback id, which Claude's Agent schema refuses as a model; a native
            # call that runs keeps the shipped alias the model hook injects in a person's home (still the loopback).
            shipped = (_base().SOURCE / "adapters/claude/config/models.conf").read_text(encoding="utf-8")
            run.home.env["CLAUDE_NATIVE_SUBAGENT_MODEL"] = re.search(r"(?m)^CFG_TIER_LIGHT_MODEL=(\S+)", shipped).group(1)

    # -- the fixture model -----------------------------------------------------------------------
    def worker_reply(self, run, record, earlier):
        body = "" if self.empty_answer else ("{head}\n\n- README.md:1 says `unit fixture`: the fixture is for a "
                                             f"unit review ({self.nonce})\n")
        target, shell = _report_shell(record, ANSWER_HEAD, body)
        if not any("report.md" in s for s in _shells_with(earlier, self.sub) if s):
            return Reply(shell=shell)
        return Reply(f"artifact: {target}\nverdict: PASS\nblocker: none", gate=self.worker_end)

    def other_worker(self, run, record, earlier):
        """A worker this scenario started itself (a held slot, a worker's own call): see the subclasses."""
        return Reply("artifact: -\nverdict: BLOCKED\nblocker: not this scenario's worker")

    def script(self, run):
        self.worker_end = threading.Event()
        self.hold_end = threading.Event()
        calls = {label: (agent, extra) for label, agent, extra in self.calls(run)}
        markers = {label: f"{self.main}-{label}" for label in calls}
        subs = {label: f"{self.sub}-{label}" for label in [*calls, "worker"]}

        def answer(record, earlier):
            text, last = record["text"], record["last_text"]
            if unit.side_request(record):
                return Reply("TITLE: fixture subagent\nNOW: fixture")
            if "Dispatch metadata:" in text and self.WORKER_CALL not in text and _label_of(text, subs):
                return self.worker_reply(run, record, earlier)              # the unit worker the hook started
            if "Dispatch metadata:" in text:
                return self.other_worker(run, record, earlier)              # a worker the scenario started
            # A native subagent's own request: a fresh one carries only its prompt, a fork the session's history
            # with its prompt as the newest input.
            forked = (record["last_kind"] == "user" and self.main not in last and _label_of(last, subs))
            if (self.main not in text and _label_of(text, subs)) or forked:
                self.native.append((record["at"], _label_of(last if forked else text, subs)))
                return Reply(f"native subagent answer {self.nonce}")
            label = _label_of(text, markers)
            if label is None:
                return Reply("Nothing new.")
            if record["last_kind"] == "tool":
                if label not in self.results:
                    self.results[label] = (record["at"], last)
                found = re.search(r'"agent_id"\s*:\s*"([^"]+)"', last)
                if (run.harness == "codex" and found and codex_spawn_namespace() == "multi_agent_v1"
                        and not any(s.startswith("wait:") for s in _shells_with(earlier, markers[label]))):
                    return Reply(shell=ce.wait_call(found.group(1)))   # the parent waits, as a session would
                return Reply("The subagent runs; I will continue when its result arrives.")
            if self.results.get(label) and record["at"] > self.results[label][0]:
                return Reply("Got it.")                                     # a notice, or the next turn
            if label not in self.spawned:
                self.spawned[label] = time.time()
                agent, extra = calls[label]
                return Reply(shell=spawn_call(agent, f"{subs[label]}: {QUESTION}",
                                              description="Find the fixture README purpose", extra=extra))
            return Reply("Nothing new.")
        return answer

    # -- driving ---------------------------------------------------------------------------------
    def resting(self, run, label):
        marker = f"{self.main}-{label}"
        return [r for r in run.main_requests() if marker in r["text"] and "Dispatch metadata:" not in r["text"]
                and not unit.side_request(r) and _label_of(r["text"], {label: marker}) == label]

    def ask(self, run, label):
        """Type the prompt for `label` and wait for its tool result (one retype if the prompt did not land)."""
        for _attempt in range(2):
            before = len(run.main_requests())
            run.prompt(f"{self.main}-{label} hand the lookup to a subagent")
            if run.wait_requests(before + 1, 30):
                break
        return wait_for(lambda: label in self.results, 220, 1)

    def drive(self, run):
        try:
            return self.run_redirect(run, run.evidence)
        finally:
            self.worker_end.set()
            self.hold_end.set()

    def run_native(self, run, ev):
        """OpenCode is not intercepted: the native subagent runs and no unit route starts."""
        if not self.ask(run, "light"):
            run.note("the subagent call returned nothing to the session")
            return "inconclusive"
        wait_for(lambda: self.native, 60, 1)
        run.wait_idle(6, 60)
        at, text = self.results["light"]
        ev["call_to_tool_result_seconds"] = round(at - self.spawned["light"], 1)
        ev["tool_result"] = text[-800:]
        ev["native_subagent_requests"] = len(self.native)
        ev["explore_rows"] = len(explore_rows(self.jobs))
        run.note(f"native={len(self.native)}; rows={ev['explore_rows']}; refused-line={STARTED in text}")
        return "pass" if self.native and not ev["explore_rows"] and STARTED not in text else "fail"

    def run_redirect(self, run, ev):
        if run.harness == "opencode":
            return self.run_native(run, ev)
        if not self.ask(run, "light"):
            run.note("the subagent call returned nothing to the session")
            return "inconclusive"
        at, text = self.results["light"]
        ev["call_to_tool_result_seconds"] = round(at - self.spawned["light"], 1)
        ev["refusal"] = text[-1200:]
        rows = wait_for(lambda: explore_rows(self.jobs), 30, 1) or []
        ev["rows_after_start"] = [{k: r.get(k) for k in ("status", "unit", "worker_type", "model_profile", "harness",
                                                          "route_id", "dispatch_depth")} for r in rows]
        workers = [r for r in run.main_requests() if "Dispatch metadata:" in r["text"] and self.sub in r["text"]]
        if workers:
            ev["call_to_worker_request_seconds"] = round(workers[0]["at"] - self.spawned["light"], 1)
        run.wait_idle(6, 60)
        released = time.time()
        self.worker_end.set()
        wait_for(lambda: (lambda rows: rows and rows[-1]["status"] in TERMINAL_STATES)(explore_rows(self.jobs)),
                 self.WAIT, 2)
        started = explore_rows(self.jobs)
        # The end notice names the answer (`Answer:`); a notice that carries none names the route or its attempt.
        route_id = started[-1].get("route_id", "") if started else ""
        words = ["Answer:"] if not self.empty_answer else ([route_id, *attempt_ids(self.jobs, route_id)]
                                                          if route_id else ["rt-"])
        notice = wait_for(lambda: [r for r in self.resting(run, "light") if r["last_kind"] != "tool"
                                   and any(w in r["text"][-6000:] for w in words) and r["at"] > released], 150, 1)
        run.wait_idle(8, 60)
        rows = explore_rows(self.jobs)
        ev["row"] = ({k: rows[-1].get(k) for k in ("status", "unit", "worker_type", "model_profile", "harness",
                                                   "failure_class", "note", "route_id")} if rows else None)
        ev["native_subagent_requests"] = len(self.native)
        if notice:
            ev["release_to_notice_seconds"] = round(notice[0]["at"] - released, 1)
            ev["notice"] = notice[0]["last_text"][-2500:]
        ev["attention_notice"] = bool(notice) and "Answer:" not in ev.get("notice", "")
        ev["requests_after_notice"] = [r["last_text"][-300:] for r in self.resting(run, "light")
                                       if notice and r["at"] > notice[0]["at"]]
        run.note(f"refused={STARTED in ev['refusal']}; rows={len(rows)}; native={len(self.native)}; "
                 f"notice={'yes' if notice else 'no'}; call-to-result={ev.get('call_to_tool_result_seconds')}s; "
                 f"call-to-worker={ev.get('call_to_worker_request_seconds')}s; "
                 f"release-to-notice={ev.get('release_to_notice_seconds')}s")
        return self.verdict(run, ev, rows, notice)

    def verdict(self, run, ev, rows, notice):
        row = ev["row"] or {}
        ok = (STARTED in ev["refusal"] and len(rows) == 1 and row.get("model_profile") == "light"
              and row.get("harness") == run.harness and row.get("status") == "done"
              and not self.native and notice and self.nonce in ev.get("notice", ""))
        return "pass" if ok else "fail"

    # -- a unit route the scenario starts itself (another session's work) --------------------------
    def compose_other(self, run, marker, slug):
        (run.home.root / f"{slug}.md").write_text(f"{marker}-{self.nonce}: review the fixture change.\n",
                                                  encoding="utf-8")
        command = [sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), "compose", "--start",
                   "--unit", "qa/code-review", "--ephemeral", "--pin", f"worker={run.harness}",
                   "--prompt-file", str(run.home.root / f"{slug}.md"), "--cwd", str(run.home.workspace),
                   "--slug", slug]
        done = subprocess.run(command, env=run.home.env, cwd=str(run.home.workspace), text=True,
                              capture_output=True, timeout=300, stdin=subprocess.DEVNULL)
        return {"exit": done.returncode, "state": ce.last_json_object(done.stdout).get("state")}

    def report_then_end(self, record, earlier, marker, gate=None):
        target, shell = _report_shell(record, unit.PASS_HEAD, "{head}\n\n- reviewed\n")
        if not any("report.md" in s for s in _shells_with(earlier, marker) if s):
            return Reply(shell=shell)
        return Reply(f"artifact: {target}\nverdict: PASS\nblocker: none", gate=gate)


class SubagentRedirectNoAnswerScenario(SubagentRedirectScenario):
    name = "subagent-redirect-no-answer"
    summary = ("The redirected route's worker ends with an empty answer: what the session is told when no answer "
               "comes, and that nothing runs twice.")
    empty_answer = True

    def verdict(self, run, ev, rows, notice):
        ok = (STARTED in ev["refusal"] and len(rows) == 1 and not self.native and notice
              and "Answer:" not in ev.get("notice", ""))
        return "pass" if ok else "fail"


class SubagentRedirectCapacityScenario(SubagentRedirectScenario):
    name = "subagent-redirect-capacity"
    summary = ("The dispatch cap is one and another unit route holds it: the hook's start is refused, the native "
               "subagent runs as before, no later start is armed, no route is left open and none starts later.")
    HOLD = "SUBREDIR-HOLD"

    def prepare(self, run):
        super().prepare(run)
        run.home.env["AGENT_MODEL_WORKER_CLASS_LIMIT_DISPATCH"] = "1"

    def other_worker(self, run, record, earlier):
        if self.HOLD in record["text"]:
            return self.report_then_end(record, earlier, self.HOLD, gate=self.hold_end)
        return super().other_worker(run, record, earlier)

    def scratch_state(self):
        """Open routes without a row, and armed resumes, in the scratch roots and beside the registry."""
        rows = registry_rows(self.jobs)
        routed = {r.get("route_id") for r in rows}
        roots = {Path(r.get("artifact_root")) for r in rows if r.get("artifact_root")}
        open_routes = []
        for root in roots:
            for path in (root / ".runtime" / "routes").glob("rt-*.json"):
                if path.name.endswith(".outcome.json") or ".superseded" in path.name:
                    continue
                if path.stem not in routed and not path.with_name(path.stem + ".outcome.json").exists():
                    open_routes.append(path.name)
        resume_dir = self.jobs.resolve().parent / "capacity-resume"
        resumes = sorted(p.name for p in resume_dir.glob("*.json")) if resume_dir.is_dir() else []
        return {"open_routes_without_row": open_routes, "armed_resumes": resumes,
                "closed_unstarted": sorted(p.name for root in roots
                                           for p in (root / ".runtime" / "routes").glob("rt-*.outcome.json")
                                           if "start-refused" in p.read_text(encoding="utf-8", errors="replace"))}

    def run_redirect(self, run, ev):
        if run.harness == "opencode":
            run.note("OpenCode does not intercept the call: there is no start for the cap to refuse")
            return "not-applicable"
        ev["hold"] = self.compose_other(run, self.HOLD, "hb-hold-slot")
        held = wait_for(lambda: [r for r in registry_rows(self.jobs) if "hb-hold-slot" in r.get("slug", "")
                                 and r["status"] not in TERMINAL_STATES], 60, 1)
        if not held:
            run.note("the slot was not held")
            return "inconclusive"
        if not self.ask(run, "light"):
            run.note("the subagent call returned nothing to the session")
            return "inconclusive"
        at, text = self.results["light"]
        ev["call_to_tool_result_seconds"] = round(at - self.spawned["light"], 1)
        ev["tool_result"] = text[-800:]
        wait_for(lambda: self.native, 60, 1)
        run.wait_idle(6, 60)
        ev["native_subagent_requests"] = len(self.native)
        ev["explore_rows_while_held"] = len(explore_rows(self.jobs))
        ev["after_refusal"] = self.scratch_state()
        self.hold_end.set()
        wait_for(lambda: all(r["status"] in TERMINAL_STATES for r in registry_rows(self.jobs)
                             if "hb-hold-slot" in r.get("slug", "")), self.WAIT, 2)
        wait_for(lambda: False, 75, 5)     # a later start would come once the slot frees
        ev["explore_rows_after_free"] = len(explore_rows(self.jobs))
        ev["after_free"] = self.scratch_state()
        run.note(f"native={len(self.native)}; refused-line={STARTED in ev['tool_result']}; "
                 f"rows held/after={ev['explore_rows_while_held']}/{ev['explore_rows_after_free']}; "
                 f"open={ev['after_free']['open_routes_without_row']}; resumes={ev['after_free']['armed_resumes']}; "
                 f"closed-unstarted={len(ev['after_free']['closed_unstarted'])}")
        ok = (self.native and STARTED not in ev["tool_result"] and not ev["explore_rows_while_held"]
              and not ev["explore_rows_after_free"] and not ev["after_free"]["open_routes_without_row"]
              and not ev["after_free"]["armed_resumes"])
        return "pass" if ok else "fail"


class SubagentRedirectUntouchedScenario(SubagentRedirectScenario):
    name = "subagent-redirect-untouched"
    summary = ("Calls the hook leaves alone: a user agent, the session's history handed over (Claude fork, Codex "
               "fork_context) and a registered worker's own call; no unit route starts for any of them.")

    def prepare(self, run):
        super().prepare(run)
        self.user_agent(run)
        self.own_light(run)

    def own_light(self, run):
        """The user's own definition under the table's light name: the harness picks it, not the built-in."""
        model = ce.fixture_model_id(run.harness)
        name = LIGHT_NAME[run.harness]
        if run.harness == "claude":
            path = Path(run.home.env["CLAUDE_CONFIG_DIR"]) / "agents" / "hb-own-light.md"
            text = f"---\nname: {name}\ndescription: The user's own {name}.\n---\nAnswer in one line.\n"
        elif run.harness == "codex":
            path = Path(run.home.env["CODEX_HOME"]) / "agents" / "hb-own-light.toml"
            text = (f'name = "{name}"\ndescription = "The user\'s own {name}."\nmodel = "{model}"\n'
                    'developer_instructions = "Answer in one line."\n')
        else:
            return
        _own_dir(path.parent)
        path.write_text(text, encoding="utf-8")

    def user_agent(self, run):
        """One agent the user wrote (not in the table), on the fixture model."""
        model = ce.fixture_model_id(run.harness)
        if run.harness == "claude":
            path = Path(run.home.env["CLAUDE_CONFIG_DIR"]) / "agents" / f"{USER_AGENT}.md"
            text = f"---\nname: {USER_AGENT}\ndescription: A user agent of the fixture.\n---\nAnswer briefly.\n"
        elif run.harness == "codex":
            path = Path(run.home.env["CODEX_HOME"]) / "agents" / f"{USER_AGENT}.toml"
            text = (f'name = "{USER_AGENT}"\ndescription = "A user agent of the fixture."\nmodel = "{model}"\n'
                    'developer_instructions = "Answer briefly."\n')
        else:
            path = Path(run.home.env["XDG_CONFIG_HOME"]) / "opencode" / "agents" / f"{USER_AGENT}.md"
            text = f"---\ndescription: A user agent of the fixture.\nmode: subagent\nmodel: {model}\n---\nAnswer briefly.\n"
        _own_dir(path.parent)
        if path.is_symlink():
            path.unlink()
        path.write_text(text, encoding="utf-8")

    def calls(self, run):
        calls = [("user-agent", USER_AGENT, None)]
        if run.harness in ("claude", "codex"):
            calls.append(("own-light", LIGHT_NAME[run.harness], None))
        if run.harness == "claude":
            calls.append(("fork", "fork", None))
        if run.harness == "codex":
            # A full-history fork inherits the parent's agent type, so it names none (Codex refuses one).
            calls.append(("fork-context", None, {"fork_context": True}))
        return calls

    def other_worker(self, run, record, earlier):
        """A registered unit worker whose first step is a native subagent call of the table's light name."""
        if self.WORKER_CALL not in record["text"]:
            return super().other_worker(run, record, earlier)
        shells = _shells_with(earlier, self.WORKER_CALL)
        tools = set(record.get("tools") or ())
        if not any(s.startswith("spawn:") for s in shells):
            run.evidence["worker_subagent_tool"] = sorted(tools & SPAWN_TOOL_NAMES)
            if tools & SPAWN_TOOL_NAMES:
                return Reply(shell=spawn_call(LIGHT_NAME[run.harness], f"{self.sub}-worker: {QUESTION}",
                                              description="Find the fixture README purpose"))
        elif record["last_kind"] == "tool" and "worker" not in self.results:
            self.results["worker"] = (record["at"], record["last_text"])
        return self.report_then_end(record, earlier, self.WORKER_CALL)

    def run_redirect(self, run, ev):
        results = {}
        for label, _agent, _extra in self.calls(run):
            before_native, before_rows = len(self.native), len(explore_rows(self.jobs))
            self.ask(run, label)
            wait_for(lambda: len(self.native) > before_native, 30, 1)
            run.wait_idle(6, 60)
            results[label] = {"tool_result": (self.results.get(label) or (0, ""))[1][-600:],
                              "native": len(self.native) - before_native,
                              "rows": len(explore_rows(self.jobs)) - before_rows}
        before_native, before_rows = len(self.native), len(explore_rows(self.jobs))
        started = self.compose_other(run, self.WORKER_CALL, "hb-worker-call")
        wait_for(lambda: all(r["status"] in TERMINAL_STATES for r in registry_rows(self.jobs)
                             if "hb-worker-call" in r.get("slug", "")) and
                 [r for r in registry_rows(self.jobs) if "hb-worker-call" in r.get("slug", "")], self.WAIT, 2)
        results["worker"] = {"start": started, "subagent_tool": run.evidence.get("worker_subagent_tool"),
                             "tool_result": (self.results.get("worker") or (0, ""))[1][-600:],
                             "native": len(self.native) - before_native,
                             "rows": len(explore_rows(self.jobs)) - before_rows}
        ev["calls"] = results
        run.note("; ".join(f"{label}: native={r['native']} rows={r['rows']}" for label, r in results.items()))
        ok = all(r["rows"] == 0 and STARTED not in r.get("tool_result", "") for r in results.values())
        ok = ok and results["user-agent"]["native"]
        if "own-light" in results:
            ok = ok and results["own-light"]["native"]
        if results["worker"]["subagent_tool"]:
            ok = ok and results["worker"]["native"]
        else:
            run.note("the registered worker has no native subagent tool here")
        if run.harness == "claude":
            # A fork stays refused by the model hook, as before this change.
            ok = ok and "native-subagent-model-inheritance-ineligible" in results["fork"]["tool_result"]
        if run.harness == "codex":
            ok = ok and results["fork-context"]["native"]
        return "pass" if ok else "fail"


FAULTS = {
    # label: (what the registered command does after reading the payload, its timeout in seconds)
    "exit-1": ("exit 1", 30),
    "killed": ("kill -9 $$", 30),
    "garbage": ("echo not-json; exit 0", 30),
    "timeout": ("sleep 20", 3),
    "exit-2": ("echo hook-failed >&2; exit 2", 30),
}


class SubagentRedirectFaultsScenario(SubagentRedirectScenario):
    name = "subagent-redirect-faults"
    summary = ("The redirect hook fails the ways a hook can (exit 1, killed, garbage, past its timeout) and the old "
               "registration a session opened before this change carries: the native subagent runs every time.")
    needs_tui = False

    def prepare(self, run):
        super().prepare(run)
        if run.harness == "claude":
            self.hook_file = Path(run.home.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        elif run.harness == "codex":
            self.hook_file = Path(run.home.env["CODEX_HOME"]) / "hooks.json"
        else:
            self.hook_file = None
        self.hook_text = self.hook_file.read_text(encoding="utf-8") if self.hook_file else ""

    def calls(self, run):
        labels = [*FAULTS, "old-line"] if run.harness in ("claude", "codex") else []
        return [(label, LIGHT_NAME[run.harness], None) for label in labels]

    def registration(self, run, label):
        """The hook file with the redirect entry replaced for `label`."""
        data = json.loads(self.hook_text)
        groups = data["hooks"]["PreToolUse"]
        matcher = "Agent|Task" if run.harness == "claude" else "spawn_agent|collaborationspawn_agent"
        group = next(g for g in groups if g.get("matcher") == matcher)
        entry = group["hooks"][0]
        if label == "old-line":
            if run.harness == "claude":
                entry["command"] = entry["command"].replace(" --redirect", "")
                entry["timeout"] = 15
            else:
                groups.remove(group)            # a hooks.json from before this change has no spawn_agent entry
        else:
            body, seconds = FAULTS[label]
            entry["command"] = "sh -c " + shlex.quote(f"cat >/dev/null; {body}")
            entry["timeout"] = seconds
        return json.dumps(data, indent=1)

    def run_redirect(self, run, ev):
        if not self.hook_file:
            run.note("no command hook to replace on this harness (the plugin's fault paths are unit-tested)")
            return "not-applicable"
        results = {}
        for label, _agent, _extra in self.calls(run):
            _write_own(self.hook_file, self.registration(run, label), run.home.root)
            if run.harness == "codex":
                results.setdefault("_trust", {})[label] = (run.home.trust_codex_hooks(run.binary) or {}).get("status")
            before_native, before_rows = len(self.native), len(explore_rows(self.jobs))
            self.spawned.pop(label, None)
            began = time.time()
            done = subprocess.run([run.binary, *HEADLESS[run.harness], f"{self.main}-{label} hand the lookup to a subagent"],
                                  env=run.home.env, cwd=str(run.home.workspace), text=True, capture_output=True,
                                  timeout=240, stdin=subprocess.DEVNULL)
            wait_for(lambda: len(self.native) > before_native, 20, 1)
            results[label] = {"exit": done.returncode, "seconds": round(time.time() - began, 1),
                              "native": len(self.native) - before_native,
                              "rows": len(explore_rows(self.jobs)) - before_rows,
                              "tool_result": (self.results.get(label) or (0, ""))[1][-400:]}
        ev["faults"] = results
        run.note("; ".join(f"{label}: native={r['native']}" for label, r in results.items() if not label.startswith("_")))
        kept = [label for label in results if not label.startswith("_") and label != "exit-2"]
        ok = all(results[label]["native"] and not results[label]["rows"] for label in kept)
        # The control: exit 2 with stderr is the harness's block, so nothing runs (the redirect never exits 2).
        ev["exit_2_blocks"] = not results["exit-2"]["native"] and not results["exit-2"]["rows"]
        return "pass" if ok and ev["exit_2_blocks"] else "fail"


class SubagentRedirectOtherHookScenario(SubagentRedirectScenario):
    name = "subagent-redirect-other-hook"
    summary = ("Another PreToolUse hook refuses the subagent call beside Hearting's: the harness runs both side by "
               "side, so Hearting's hook starts nothing (no row, no worker request) and nothing runs natively.")
    REFUSAL = "refused-by-another-hook"

    def prepare(self, run):
        super().prepare(run)
        command = "sh -c " + shlex.quote(f"cat >/dev/null; echo {self.REFUSAL} >&2; exit 2")
        if run.harness == "claude":
            path = Path(run.home.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
            matcher = "Agent"
        elif run.harness == "codex":
            path = Path(run.home.env["CODEX_HOME"]) / "hooks.json"
            matcher = "spawn_agent|collaborationspawn_agent"
        else:
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
            {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": 10}]})
        _write_own(path, json.dumps(data, indent=1), run.home.root)
        if run.harness == "codex":
            run.evidence["hook_trust_again"] = (run.home.trust_codex_hooks(run.binary) or {}).get("status")

    def run_redirect(self, run, ev):
        if run.harness == "opencode":
            run.note("OpenCode does not intercept the call")
            return "not-applicable"
        if not self.ask(run, "light"):
            run.note("the subagent call returned nothing to the session")
            return "inconclusive"
        run.wait_idle(6, 60)
        wait_for(lambda: False, 20, 5)            # a start that went ahead would register by now
        _at, text = self.results["light"]
        workers = [r for r in run.main_requests() if "Dispatch metadata:" in r["text"] and self.sub in r["text"]]
        ev.update(tool_result=text[-800:], explore_rows=len(explore_rows(self.jobs)), worker_requests=len(workers),
                  native_subagent_requests=len(self.native), other_hook_refused=self.REFUSAL in text)
        run.note(f"rows={ev['explore_rows']}; worker requests={len(workers)}; native={len(self.native)}; "
                 f"other hook's refusal shown={ev['other_hook_refused']}")
        ok = (not ev["explore_rows"] and not workers and not self.native and STARTED not in text
              and ev["other_hook_refused"])
        return "pass" if ok else "fail"


class SubagentRedirectUsageLimitScenario(SubagentRedirectScenario):
    name = "subagent-redirect-usage-limit"
    summary = ("The redirected route's worker hits a usage limit after the hook refused the native call: what the "
               "session is told, that no native subagent runs, and whether the answer arrives after the reset.")
    WAIT = 300
    LIMIT_SECONDS = 45

    def __init__(self):
        super().__init__()
        self.limit_until = None
        self.limited = 0
        self.refused = threading.Event()

    def worker_reply(self, run, record, earlier):
        if self.limit_until is None:
            self.limit_until = int(time.time()) + self.LIMIT_SECONDS
        if time.time() < self.limit_until:
            self.limited += 1
            reset = datetime.fromtimestamp(self.limit_until, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            kind = "rate_limit_error" if record["protocol"] == "anthropic" else "usage_limit_reached"
            error = {"type": kind, "code": kind, "message": f"Usage limit reached. It resets {reset} (UTC).",
                     "resets_at": self.limit_until}
            # Held until the session has the hook's refusal: the limit comes after the start was confirmed.
            return Reply(gate=self.refused, http_error=(429, {"type": "error", "error": error}))
        return super().worker_reply(run, record, earlier)

    def run_redirect(self, run, ev):
        if run.harness == "opencode":
            run.note("OpenCode does not intercept the call")
            return "not-applicable"
        self.worker_end.set()                       # the answer is not held once the limit has passed
        if not self.ask(run, "light"):
            run.note("the subagent call returned nothing to the session")
            return "inconclusive"
        _at, text = self.results["light"]
        ev["refusal"] = text[-800:]
        refused_at = time.time()
        self.refused.set()
        answer = wait_for(lambda: [r for r in self.resting(run, "light") if r["at"] > refused_at
                                   and ANSWER_HEAD in r["text"][-8000:]], self.WAIT, 2)
        run.wait_idle(6, 60)
        rows = explore_rows(self.jobs)
        handles = [r.get("route_id") for r in rows if r.get("route_id")] + [r.get("attempt_id") for r in rows
                                                                            if r.get("attempt_id")]
        told = [r["last_text"][-700:] for r in self.resting(run, "light")
                if r["at"] > refused_at and r["last_kind"] != "tool"
                and any(handle in r["last_text"] for handle in handles)]
        ev.update(limited_requests=self.limited, native_subagent_requests=len(self.native),
                  rows=[{k: r.get(k) for k in ("status", "failure_class", "note", "attempt_id")} for r in rows],
                  told_before_answer=[t for t in told if ANSWER_HEAD not in t][:3],
                  refusal_to_answer_seconds=round(answer[0]["at"] - refused_at, 1) if answer else None)
        run.note(f"refused={STARTED in text}; 429s={self.limited}; rows={len(rows)}; native={len(self.native)}; "
                 f"answer after={ev['refusal_to_answer_seconds']}s")
        ev["answer_arrived"] = bool(answer)
        if not self.limited:
            return "inconclusive"
        # The session is told about its route (the answer, or the notice's next action); nothing runs natively.
        ok = STARTED in text and not self.native and (bool(answer) or bool(told))
        return "pass" if ok else "fail"


class SubagentRedirectCostScenario(SubagentRedirectScenario):
    name = "subagent-redirect-cost"
    summary = ("One open session, one prompt and one fixture model: the call under the registration from before this "
               "change (native), then the same call after the hook settings are rewritten with the session still "
               "open. Records call, first subagent or worker request, tool result and answer arrival for each.")

    def calls(self, run):
        return [("old", LIGHT_NAME[run.harness], None), ("new", LIGHT_NAME[run.harness], None)]

    def prepare(self, run):
        super().prepare(run)
        self.hook_file = None
        if run.harness == "claude":
            self.hook_file = Path(run.home.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        elif run.harness == "codex":
            self.hook_file = Path(run.home.env["CODEX_HOME"]) / "hooks.json"
        if not self.hook_file:
            return
        self.hook_text = self.hook_file.read_text(encoding="utf-8")
        _write_own(self.hook_file, SubagentRedirectFaultsScenario.registration(self, run, "old-line"), run.home.root)
        if run.harness == "codex":
            run.evidence["trust_old"] = (run.home.trust_codex_hooks(run.binary) or {}).get("status")

    def arrived(self, run, label, since):
        """The parent's first request that holds this call's answer: a fast native subagent's result can ride
        in the same request as the tool result; the unit route's answer is its notice (or a finished receipt)."""
        word = f"native subagent answer {self.nonce}" if label == "old" else ANSWER_HEAD
        return [r for r in self.resting(run, label) if r["at"] >= since and word in r["text"]]

    def run_redirect(self, run, ev):
        if not self.hook_file:
            run.note("OpenCode does not intercept the call: there is no old and new registration to compare")
            return "not-applicable"
        self.worker_end.set()                       # nothing is held: every answer comes when it is ready
        measured = {}
        for label in ("old", "new"):
            if label == "new":
                _write_own(self.hook_file, self.hook_text, run.home.root)   # the new line, the session still open
                if run.harness == "codex":
                    ev["trust_new"] = (run.home.trust_codex_hooks(run.binary) or {}).get("status")
                time.sleep(5)
            before_native, before_rows = len(self.native), len(explore_rows(self.jobs))
            if not self.ask(run, label):
                measured[label] = {"tool_result": None}
                continue
            call = self.spawned[label]
            at, text = self.results[label]
            done = wait_for(lambda: self.arrived(run, label, at), 200, 1)
            workers = [r["at"] for r in run.main_requests()
                       if "Dispatch metadata:" in r["text"] and f"{self.sub}-{label}" in r["text"]]
            firsts = [t for t, seen in self.native if seen == label] + workers
            measured[label] = {
                "redirected": STARTED in text, "tool_result": text[-600:],
                "call_to_tool_result_seconds": round(at - call, 1),
                "call_to_first_subagent_request_seconds": round(min(firsts) - call, 1) if firsts else None,
                "call_to_answer_seconds": round(done[0]["at"] - call, 1) if done else None,
                "native": len(self.native) - before_native, "rows": len(explore_rows(self.jobs)) - before_rows}
            run.wait_idle(6, 60)
        ev["cost"] = measured
        old, new = measured.get("old", {}), measured.get("new", {})
        ev["open_session_took_new_line"] = bool(new.get("redirected"))
        if old.get("call_to_answer_seconds") is not None and new.get("call_to_answer_seconds") is not None:
            ev["more_seconds"] = round(new["call_to_answer_seconds"] - old["call_to_answer_seconds"], 1)
        run.note(f"old: {old.get('call_to_answer_seconds')}s native={old.get('native')}; new: "
                 f"{new.get('call_to_answer_seconds')}s redirected={new.get('redirected')}; "
                 f"more={ev.get('more_seconds')}s")
        ok = (old.get("native") and not old.get("rows") and old.get("call_to_answer_seconds") is not None
              and new.get("tool_result") is not None)
        return "pass" if ok else "fail"


SCENARIOS = [SubagentHookSeamScenario, SubagentHookSeamV2Scenario, SubagentRedirectScenario,
             SubagentRedirectNoAnswerScenario, SubagentRedirectCapacityScenario, SubagentRedirectUntouchedScenario,
             SubagentRedirectFaultsScenario, SubagentRedirectOtherHookScenario, SubagentRedirectUsageLimitScenario,
             SubagentRedirectCostScenario]
