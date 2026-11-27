#!/usr/bin/env python3
"""Behaviour scenario: which model a native sub-agent asks for (PG-13).

Loaded by ``harness_behavior_scenarios.py`` (``_load_extra``).

    ``subagent-model``  headless, the model spawns a built-in sub-agent with no
                        model and a Hearting catalog agent that carries a pin;
                        then, in a registered owner worker's home built from
                        the same home (``tools/profile/build-home.py``), it
                        names ``light``, ``balanced`` and ``deep``. The oracle
                        is the ``model`` of each sub-agent's own request at the
                        loopback model; a worker spawn passes when it carries
                        the model its definition pins.

The role default is the portable ``light`` profile
(``utilities/native_agents.py`` ``NATIVE_SUBAGENT_PROFILE``): Claude injects it
with ``hooks/subagent-model-default.sh``, Codex reads ``[agents]
default_subagent_model`` that release activation writes where the home's model
list carries it; OpenCode's built-ins keep the session model (PG-13 limit).

Each harness serves the role default the way a user's home does:

* Codex selects its own ``openai`` provider with ``openai_base_url`` on the
  loopback model, so the shipped pins apply (a home that can select another
  provider gets none, PG-52). The default is written only where the home's
  model list is read and carries the light model: the home names, through
  ``model_catalog_json``, the list this Codex binary ships (``codex debug
  models --bundled``), as a home whose list carries it does. The agents and
  config keys are then projected as release activation does
  (``sync-native-agents.py --runtime-home`` and ``_merge_codex_managed_keys``);
  a source without those keys measures as it is.
* OpenCode gets a whole-file ``models.conf`` copy whose tiers name loopback
  models and its agents rendered for that copy. Its built-ins keep the parent
  session's model: no plugin seam sees the parent's model per spawn (PG-13
  limit), so ``general`` measures as a gap.

Everything stays in the fixture root; no user config is read or written.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_behavior_scenarios as base  # noqa: E402
from harness_behavior_scenarios import Reply, Scenario  # noqa: E402
import harness_behavior_cases_e as ce  # noqa: E402

PG = {"subagent-model": "PG-13"}



def _codex_models() -> dict:
    """The tier models of the shipped Codex config (the loopback model lists them)."""
    text = (_source() / "adapters/codex/config/models.conf").read_text(encoding="utf-8")
    return dict(re.findall(r'(?m)^CFG_TIER_([A-Z_]+)_MODEL="?([^"\s#]+)', text))


def _codex_session() -> str:
    # The loopback model id: no sub-agent profile names it, so a sub-agent on the
    # session model is told apart (Codex runs it on fallback metadata).
    return base.FIXTURE_MODEL
OPENCODE_LIGHT, OPENCODE_DEEP = "hearting-light", "hearting-deep"
# (built-in agent with no model, catalog agent with a pin) per harness.
AGENTS = {"claude": ("Explore", "deep"), "codex": ("default", "deep"),
          "opencode": ("general", "deep")}
# The agents a worker calls by name (build-home.py WORKER_NATIVE_AGENTS).
WORKER_AGENTS = ("light", "balanced", "deep")
WORKER = "worker-"   # marker label prefix of a spawn inside the worker home

BUILD_WORKER = r"""
import json, os, sys
from pathlib import Path
source, harness, identity = sys.argv[1:4]
sys.path.insert(0, source + "/utilities")
import worker_runtime_home as w
values = w.prepare_worker_home(Path(source), harness, "owner", identity, env=dict(os.environ))
env = {**os.environ, **{k: v for k, v in values.items() if v is not None}}
args = {"claude": w.claude_worker_arguments, "codex": w.codex_worker_arguments}.get(harness)
print(json.dumps({"values": values, "argv": args(env) if args else []}))
"""


def _source() -> Path:
    return ce._base().SOURCE


def _extra_models(names):
    for module in list(sys.modules.values()):
        extra = getattr(module, "EXTRA_MODELS", None)
        if isinstance(extra, list) and hasattr(module, "ModelFixture"):
            extra.extend(sorted(set(names) - set(extra)))


def _render_agents(run, harness: str, agents_dir: Path, home: Path) -> dict:
    """Project the agents as release activation does for this home."""
    renderer = _source() / "adapters" / harness / "bin" / "sync-native-agents.py"
    done = subprocess.run([sys.executable, "-B", str(renderer), "--runtime-home", str(home), "--json"],
                          env=run.home.env, text=True, capture_output=True, timeout=120)
    if done.returncode != 0:
        return {"error": done.stderr[-400:]}
    plan = json.loads(done.stdout)
    rows = {}
    for row in plan["agents"]:
        rows[row["name"]] = {"status": row["status"], "model": row.get("model")}
        content = row.get("content")
        if row["status"] == "shipped" and harness == "codex":
            # Activation writes Codex agents as files: Codex refuses a linked role file.
            content = (renderer.parents[1] / "agents" / row["shipped_relpath"]).read_text(encoding="utf-8")
        if content is not None and row["status"] in ("rendered", "shipped"):
            dest = agents_dir / row["dest_name"]
            if dest.is_symlink() or dest.exists():
                dest.unlink()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
    return {"agents": rows, "default_subagent": plan.get("default_subagent")}


MANAGED_KEYS = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/tools/install")
import runtime_activation as ra
merge = getattr(ra, "_merge_codex_managed_keys", None)
config = Path(sys.argv[2])
if merge is None:
    print(json.dumps({"status": "source-has-no-managed-keys"}))
else:
    text, records = merge(config.read_text(encoding="utf-8"), Path(sys.argv[1]), "global", {})
    config.write_text(text, encoding="utf-8")
    print(json.dumps({"status": "merged", "records": sorted(records)}))
"""


class SubagentModelScenario(Scenario):
    name = "subagent-model"
    summary = ("Headless: a built-in sub-agent spawned with no model and a pinned catalog agent; "
               "the model each sub-agent's own request names.")
    needs_tui = False

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]

    def markers(self, agent: str):
        return f"SUBMODEL-MAIN-{agent}-{self.nonce}", f"SUBMODEL-SUB-{agent}-{self.nonce}"

    @staticmethod
    def labels(harness: str):
        """``{marker label: agent type}`` of every spawn this scenario drives."""
        return {**{agent: agent for agent in AGENTS[harness]},
                **{WORKER + agent: agent for agent in WORKER_AGENTS}}

    # -- per harness home --------------------------------------------------
    def prepare(self, run):
        getattr(self, f"_prepare_{run.harness}", lambda _run: None)(run)

    def _prepare_codex(self, run):
        _extra_models(sorted(set(_codex_models().values())))
        home = Path(run.home.env["CODEX_HOME"])
        config = home / "config.toml"
        text = config.read_text(encoding="utf-8")
        text = re.sub(r'(?m)^(model|model_provider)\s*=.*\n', "", text)
        text = re.sub(r'(?ms)^\[model_providers\.hearting_fixture\].*?(?=^\[|\Z)', "", text)
        text = (f'model = "{_codex_session()}"\nopenai_base_url = "{run.fixture.base}/v1"\n' + text)
        listed = subprocess.run([run.binary, "debug", "models", "--bundled"], env=run.home.env,
                                text=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        try:
            catalog = json.loads(listed.stdout)
            run.evidence["model_list"] = sorted(m["slug"] for m in catalog["models"])
            (home / "catalog.json").write_text(listed.stdout, encoding="utf-8")
            text = f'model_catalog_json = "{home / "catalog.json"}"\n' + text
        except (ValueError, KeyError, TypeError):
            run.evidence["model_list"] = {"unread": listed.stderr[-300:]}
        config.write_text(text, encoding="utf-8")
        run.home.env["OPENAI_API_KEY"] = run.home.env["CODEX_API_KEY"] = "sk-fixture-" + "0" * 20
        run.evidence["activation"] = _render_agents(run, "codex", home / "agents", home)
        done = subprocess.run([sys.executable, "-B", "-c", MANAGED_KEYS, str(_source()), str(config)],
                              env=run.home.env, text=True, capture_output=True, timeout=120)
        run.evidence["managed_keys"] = (json.loads(done.stdout.strip().splitlines()[-1])
                                        if done.returncode == 0 and done.stdout.strip()
                                        else {"error": done.stderr[-400:]})
        agents = re.search(r'(?ms)^\[agents\]\n(.*?)(?=^\[|\Z)', config.read_text(encoding="utf-8"))
        run.evidence["agents_table"] = agents.group(1).strip() if agents else None

    def _prepare_opencode(self, run):
        config_dir = Path(run.home.env["XDG_CONFIG_HOME"]) / "opencode"
        path = config_dir / "opencode.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        models = config["provider"]["fixture"]["models"]
        for name in (OPENCODE_LIGHT, OPENCODE_DEEP):
            models[name] = {**models[base.FIXTURE_MODEL], "name": name}
        path.write_text(json.dumps(config, indent=1), encoding="utf-8")
        shipped = (_source() / "adapters/opencode/config/models.conf").read_text(encoding="utf-8")
        conf = re.sub(r'(?m)^CFG_TIER_(LIGHT|MINI)_MODEL=.*$',
                      lambda m: f"CFG_TIER_{m.group(1)}_MODEL=fixture/{OPENCODE_LIGHT}", shipped)
        conf = re.sub(r'(?m)^CFG_TIER_BALANCED_DEEP_MODEL=.*$',
                      f"CFG_TIER_BALANCED_DEEP_MODEL=fixture/{OPENCODE_DEEP}", conf)
        user = config_dir / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(conf, encoding="utf-8")
        run.evidence["activation"] = _render_agents(run, "opencode", config_dir / "agents", config_dir)

    # -- model script --------------------------------------------------------
    def script(self, run):
        def answer(record, earlier):
            for agent, agent_type in self.labels(run.harness).items():
                main, sub = self.markers(agent)
                if ce._is_sub(record, main, sub):
                    return Reply(f"sub done {agent}")
                if main not in record["text"]:
                    continue
                calls = [str(r["reply"].get("shell") or "") for r in earlier if main in r["text"]]
                if record["last_kind"] == "tool":
                    found = re.search(r'"agent_id"\s*:\s*"([^"]+)"', record["last_text"])
                    if found and f"wait:{found.group(1)}" not in calls:
                        return Reply(shell=ce.wait_call(found.group(1)))
                    return Reply(f"parent done {agent}")
                if not any(c.startswith("spawn:") for c in calls):
                    return Reply(shell=ce.spawn_call(agent_type, f"{sub} say hi"))
                return Reply(f"parent done {agent}")
            return Reply("OK")
        return answer

    def session_model(self, harness: str) -> str:
        return _codex_session() if harness == "codex" else base.FIXTURE_MODEL

    def spawn(self, run, agent: str, env=None, extra=()) -> dict:
        main, sub = self.markers(agent)
        argv = {"claude": [run.binary, "-p", "--model", base.FIXTURE_MODEL, "--permission-mode",
                           "bypassPermissions"],
                "codex": [run.binary, "exec", "--skip-git-repo-check"],
                "opencode": [run.binary, "run"]}[run.harness]
        start = len(run.fixture.requests)
        done = subprocess.run([*argv, *extra, f"{main} spawn one sub-agent"],
                              env=run.home.env if env is None else env,
                              cwd=str(run.home.workspace), text=True, capture_output=True,
                              timeout=240, stdin=subprocess.DEVNULL)
        time.sleep(2)
        records = run.fixture.requests[start:]
        subs = [r.get("model") for r in records if ce._is_sub(r, main, sub)]
        mains = [r.get("model") for r in records if main in r["text"] and r["main"]]
        row = {"exit": done.returncode, "sub_models": subs, "session_models": sorted(set(mains)),
               "stderr_tail": done.stderr[-300:] if done.returncode else ""}
        if "stream-json" in extra:
            # Claude's start record lists the agent types this session loaded.
            for line in done.stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init":
                    row["session_agents"] = sorted(event.get("agents") or [])
                    break
        return row

    # -- the same home through a registered owner worker's home ---------------
    @staticmethod
    def _pinned_model(path: Path):
        found = re.search(r'(?m)^model\s*[:=]\s*"?([^"\s]+)"?\s*$', path.read_text(encoding="utf-8"))
        return found.group(1) if found else None

    def worker_definitions(self, run, values: dict) -> dict:
        """``{agent: pinned model or None}`` the worker session reads its agents from."""
        home = Path(values["HEARTING_WORKER_HOME"])
        agents = home / "agents"
        suffix = ".toml" if run.harness == "codex" else ".md"
        if run.harness == "opencode" and not agents.is_dir():
            # A V2 worker's shared service reads the home's own agents (build-home.py).
            agents = Path(run.home.env["XDG_CONFIG_HOME"]) / "opencode" / "agents"
        return {name: (self._pinned_model(agents / f"{name}{suffix}")
                       if (agents / f"{name}{suffix}").is_file() else None)
                for name in WORKER_AGENTS}

    def worker(self, run) -> dict:
        # The builder tells an OpenCode V2 runtime from V1 by the binary it runs.
        build_env = ({**run.home.env, "OPENCODE_BIN": run.binary} if run.harness == "opencode"
                     else run.home.env)
        built = subprocess.run([sys.executable, "-B", "-c", BUILD_WORKER, str(_source()), run.harness,
                                f"att-subagent-model-{self.nonce}"], env=build_env, text=True,
                               capture_output=True, timeout=120)
        if built.returncode != 0:
            return {"verdict": "fail", "error": built.stderr[-400:]}
        launch = json.loads(built.stdout.strip().splitlines()[-1])
        values = launch["values"]
        env = {key: value for key, value in build_env.items() if key not in values}
        env.update({key: value for key, value in values.items() if value is not None})
        manifest = Path(values["HEARTING_WORKER_HOME"]) / "worker-home.json"
        result = {"home_agents": json.loads(manifest.read_text(encoding="utf-8")).get("native_agents"),
                  "definitions": self.worker_definitions(run, values), "spawns": {}}
        extra = list(launch["argv"])
        if run.harness == "claude":
            extra += ["--output-format", "stream-json", "--verbose"]
        for agent in WORKER_AGENTS:
            result["spawns"][agent] = self.spawn(run, WORKER + agent, env=env, extra=extra)
        listed = [row.get("session_agents") for row in result["spawns"].values()]
        if run.harness == "claude":
            result["session_agents"] = listed[0]
        failures = []
        for agent, row in result["spawns"].items():
            want, got = result["definitions"][agent], row["sub_models"]
            # Claude sends an alias pin (`sonnet`) as the model id it resolves to.
            same = bool(want and got) and (got[0] == want or want.split("/")[-1] == got[0]
                                           or want in re.split(r"[^a-z0-9]+", got[0].lower()))
            if not same:
                failures.append(f"{agent}: definition {want or 'missing'} -> {got or 'no sub request'}")
        if run.harness == "claude" and not set(WORKER_AGENTS) <= set(result["session_agents"] or ()):
            failures.append(f"session agents {result['session_agents']}")
        result["verdict"] = "fail" if failures else "pass"
        result["failures"] = failures
        return result

    def drive(self, run):
        builtin, pinned = AGENTS[run.harness]
        ev = run.evidence
        ev["builtin"] = {"agent": builtin, **self.spawn(run, builtin)}
        ev["pinned"] = {"agent": pinned, **self.spawn(run, pinned)}
        ev["worker"] = self.worker(run)
        session = self.session_model(run.harness)
        b, p = ev["builtin"]["sub_models"], ev["pinned"]["sub_models"]
        run.note(f"session={session}; {builtin} (no model) -> {b or 'no sub request'}; "
                 f"{pinned} (pinned) -> {p or 'no sub request'}")
        run.note("worker by name: " + "; ".join(
            f"{agent} -> {row['sub_models'] or 'no sub request'}"
            for agent, row in ev["worker"].get("spawns", {}).items()))
        if not b or not p or ev["worker"]["verdict"] != "pass":
            for failure in ev["worker"].get("failures", ()):
                run.note("worker " + failure)
            return "fail"
        if b[0] == session:
            run.note("the built-in sub-agent inherited the session model")
            return "gap"
        return "pass" if p[0] != session else "gap"


SCENARIOS = [SubagentModelScenario]
