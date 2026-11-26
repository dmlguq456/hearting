#!/usr/bin/env python3
"""Behaviour scenarios C: route presence/start/close and Fleet retire.

Loaded by ``harness_behavior_scenarios.py`` (``SCENARIOS`` below).  Each scenario
runs on the real Claude, Codex and OpenCode TUIs in the base harness's throwaway
HOME, loopback model and private tmux socket (``retire`` also on ``--surface herdr``).
The session tidy is measured by ``tidy-compact*`` (``harness_behavior_cases_tidy_compact``):
the old ``session-tidy-card`` scenario typed ``/clear``, which tidy no longer does.

Isolation added here, inside this file only:

* ``herdr`` is hidden from the fixture ``PATH`` (a shadow directory of symlinks
  replaces each PATH entry that holds it), so no Hearting helper can reach the
  operator's herdr server; tmux is then the only terminal surface.
* ``TMUX_TMPDIR`` points into the fixture root, so a bare ``tmux`` call without
  ``$TMUX`` finds no server instead of the operator's default one.
* Where a mechanism needs a physical pane seat (session tidy, retire), the pane
  process receives the private server's own ``$TMUX``/``$TMUX_PANE`` (tmux sets
  them; the base driver's ``env -i`` would otherwise drop them).
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402


# --------------------------------------------------------------------------
# Shared isolation helpers
# --------------------------------------------------------------------------

def _base_module(run):
    """The module object whose ``Run`` is executing (``__main__`` when run as a script)."""
    return sys.modules[type(run).__module__]


def _source(run) -> Path:
    return Path(_base_module(run).SOURCE)


def hide_herdr(run):
    """Shadow every PATH entry that carries ``herdr`` with a herdr-free symlink dir."""
    env = run.home.env
    shadow_root = run.home.root / "path-shadow"
    parts = []
    for index, part in enumerate(env.get("PATH", "").split(os.pathsep)):
        if part and (Path(part) / "herdr").exists():
            shadow = shadow_root / str(index)
            shadow.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                for entry in Path(part).iterdir():
                    if entry.name == "herdr":
                        continue
                    link = shadow / entry.name
                    if not link.exists():
                        with contextlib.suppress(OSError):
                            link.symlink_to(entry)
            part = str(shadow)
        parts.append(part)
    env["PATH"] = os.pathsep.join(parts)
    tmux_tmp = run.home.root / "tmux-tmp"
    tmux_tmp.mkdir(mode=0o700, exist_ok=True)
    env["TMUX_TMPDIR"] = str(tmux_tmp)
    run.evidence["herdr_visible"] = bool(subprocess.run(
        ["sh", "-c", "command -v herdr"], env=env, capture_output=True).stdout.strip())


def install_tui(run, *, host_shell: bool = False):
    """Make the next ``PrivateTui`` pass the private ``$TMUX``/``$TMUX_PANE`` through.

    ``host_shell``: the pane runs an interactive shell and the harness is started
    from it (``exec`` keeps the agent the shell's foreground child), so a normal
    exit returns to that shell as in a real terminal.
    """
    module = _base_module(run)
    original = module.PrivateTui

    class PassTui(original):
        def start(self):
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(self.env.items()))
            launch = (f"cd {shlex.quote(str(self.cwd))} && exec env -i {exports} "
                      'TMUX="$TMUX" TMUX_PANE="$TMUX_PANE" '
                      + " ".join(shlex.quote(a) for a in self.argv))
            if host_shell:
                script = self.socket_dir / "launch.sh"
                script.write_text(launch + "\n", encoding="utf-8")
                shell_env = " ".join(f"{k}={shlex.quote(self.env[k])}" for k in
                                     ("HOME", "PATH", "TMUX_TMPDIR") if k in self.env)
                command = (f'exec env -i TMUX="$TMUX" TMUX_PANE="$TMUX_PANE" {shell_env} '
                           "TERM=xterm-256color PS1='hb$ ' bash --noprofile --norc -i")
                self._tmux("new-session", "-d", "-s", "scenario", "-x", "200", "-y", "50",
                           "sh", "-c", command, check=True)
                self.started = True
                time.sleep(1.0)
                self._tmux("send-keys", "-t", "scenario", "-l", f"sh {script}")
                self._tmux("send-keys", "-t", "scenario", "Enter")
                return
            command = launch.replace("exec env -i", "env -i", 1) + \
                "; echo HEARTING-SCENARIO-TUI-EXIT=$?; sleep 3600"
            self._tmux("new-session", "-d", "-s", "scenario", "-x", "200", "-y", "50",
                       "sh", "-c", command, check=True)
            self.started = True

        def pane_command(self) -> str:
            done = self._tmux("display-message", "-p", "-t", "scenario", "#{pane_current_command}")
            return (done.stdout or "").strip()

        def alive(self) -> bool:
            if not host_shell:
                return super().alive()
            return self._tmux("has-session", "-t", "scenario").returncode == 0 \
                and self.pane_command() not in ("bash", "sh", "")

    def factory(*args, **kwargs):
        module.PrivateTui = original  # one TUI per run
        return PassTui(*args, **kwargs)

    module.PrivateTui = factory


def tmux_env(run) -> dict:
    """The fixture environment pointed at the private tmux server only."""
    tui = run.tui
    pid = tui._tmux("display-message", "-p", "#{pid}").stdout.strip() or "0"
    env = dict(run.home.env)
    env["TMUX"] = f"{tui.socket},{pid},0"
    return env


def pane_address(run) -> str:
    pane = run.tui._tmux("display-message", "-p", "-t", "scenario", "#{pane_id}").stdout.strip()
    return f"{pane}@{os.path.realpath(run.tui.socket)}"


def py(run, code: str, *args: str, env=None, timeout: float = 60) -> subprocess.CompletedProcess:
    source = _source(run)
    prelude = (f"import sys; sys.path[:0]=[{str(source / 'utilities')!r},"
               f"{str(source / 'tools')!r}]\n")
    return subprocess.run([sys.executable, "-B", "-c", prelude + code, *args],
                          env=env or run.home.env, cwd=str(run.home.workspace), text=True,
                          capture_output=True, timeout=timeout)


def state_home(run) -> Path:
    return Path(run.home.env["XDG_STATE_HOME"])


def tool_texts(run) -> list[str]:
    return [r["last_text"] for r in run.main_requests() if r["last_kind"] == "tool"]


def native_session_id(run) -> str:
    """The newest native session id the harness itself recorded."""
    harness, env = run.harness, run.home.env
    if harness == "claude":
        files = sorted(Path(env["CLAUDE_CONFIG_DIR"]).glob("projects/*/*.jsonl"),
                       key=lambda p: p.stat().st_mtime)
        return files[-1].stem if files else ""
    if harness == "codex":
        files = sorted(Path(env["CODEX_HOME"]).glob("sessions/**/rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime)
        match = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})$", files[-1].stem) if files else None
        return match.group(1) if match else ""
    for db in sorted(Path(env["XDG_DATA_HOME"]).glob("opencode/*.db")):
        with contextlib.suppress(sqlite3.Error):
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                tables = {r[0] for r in con.execute(
                    "select name from sqlite_master where type='table'")}
                table = "session_v2" if "session_v2" in tables else "session"
                row = con.execute(f"select id from {table} where parent_id is null "
                                  "order by time_created desc limit 1").fetchone()
            finally:
                con.close()
            if row:
                return row[0]
    return ""


class ProcessWatch:
    """Sample this fixture's processes and keep the argv of those naming a keyword."""

    def __init__(self, root: Path, keywords):
        import threading
        self.root, self.keywords = root, tuple(keywords)
        self.seen: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        fixture_processes = None
        for module in list(sys.modules.values()):
            fixture_processes = getattr(module, "fixture_processes", None) or fixture_processes
        while not self._stop.is_set():
            for pid in fixture_processes(self.root) if fixture_processes else ():
                with contextlib.suppress(OSError):
                    argv = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
                    text = argv.decode("utf-8", "replace").strip()
                    for word in self.keywords:
                        if word in text:
                            short = " ".join(Path(a).name if "/" in a else a
                                             for a in text.split()[:6])
                            if short not in self.seen:
                                self.seen.append(short)
            self._stop.wait(0.05)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)


# --------------------------------------------------------------------------
# 1. route-start-close
# --------------------------------------------------------------------------

class RouteStartClose(Scenario):
    name = "route-start-close"
    summary = ("Unrouted other-worktree commit is refused by route-presence-gate; the "
               "named compose command routes it; retry passes; finish closes the route.")

    def prepare(self, run):
        hide_herdr(run)
        root = run.home.root
        env = run.home.env
        # The gate exempts Git projects under the temp directory; the fixture's TMPDIR is
        # the fixture root itself, so give the session a temp dir beside the projects.
        tmp = root / "tmp"
        tmp.mkdir(exist_ok=True)
        env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(tmp)
        other = root / "other"
        git = ["git", "-c", "user.name=fx", "-c", "user.email=fx@example.invalid"]
        subprocess.run(["git", "init", "-q", str(other)], check=True, env=env)
        (other / "lib.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(git + ["-C", str(other), "add", "lib.txt"], check=True, env=env)
        subprocess.run(git + ["-C", str(other), "commit", "-qm", "base"], check=True, env=env)
        (other / "lib.txt").write_text("two\n", encoding="utf-8")
        (root / "fx-evidence.txt").write_text("fixture evidence\n", encoding="utf-8")
        (root / "fx-summary.md").write_text("fixture summary: one-line change\n",
                                            encoding="utf-8")
        self.other = other
        self.root = root
        self.state = {}

    def script(self, run):
        def commit():
            return (f"git -C {shlex.quote(str(self.other))} -c user.name=fx "
                    "-c user.email=fx@example.invalid commit -qam 'fx change' "
                    "&& echo FX-COMMIT-OK")

        def answer(record, earlier):
            done = [r["reply"].get("shell") or "" for r in earlier]
            text = record["last_text"]
            if record["last_kind"] != "tool":
                if "SCENARIO-ROUTE" in text and not any(done):
                    return Reply(shell=commit())
                return Reply("route scenario idle")
            if "FX-FINISH" in (done[-1] if done else ""):
                return Reply("route scenario done")
            if "has no route for" in text and not any("compose" in d for d in done):
                # OpenCode returns a thrown hook error as JSON with escaped newlines.
                plain = text.replace("\\n", "\n").replace('\\"', '"')
                lines = [ln.strip() for ln in plain.splitlines()
                         if "capability-route.py" in ln and " compose" in ln]
                if lines:
                    cmd = lines[0][lines[0].index("python3"):] if "python3" in lines[0] else lines[0]
                    self.state["compose_cmd"] = cmd
                    return Reply(shell=cmd + " 2>&1; echo FX-COMPOSE-RC=$?")
                return Reply("refused without a command")
            if "FX-COMPOSE-RC=" in text and done.count(commit()) < 2:
                found = re.findall(r'"route_id": "(rt-[A-Za-z0-9_-]+)"', text) or \
                    re.findall(r"\b(rt-[0-9a-f]{8,})\b", text)
                if found:
                    self.state["route_id"] = found[0]
                return Reply(shell=commit())
            if done and done[-1] == commit() and done.count(commit()) >= 2:
                route = self.state.get("route_id", "")
                cr = shlex.quote(str(_source(run) / "utilities/capability-route.py"))
                route_arg = f"--route {shlex.quote(route)} " if route else ""
                return Reply(shell=(
                    f"python3 {cr} finish {route_arg}--evidence "
                    f"{shlex.quote(str(self.root / 'fx-evidence.txt'))} --summary-file "
                    f"{shlex.quote(str(self.root / 'fx-summary.md'))} 2>&1; echo FX-FINISH-RC=$?"))
            return Reply("route scenario stopped")
        return answer

    def drive(self, run):
        run.prompt("SCENARIO-ROUTE commit the pending change in the other project")
        run.wait_idle(10, 220)
        texts = tool_texts(run)
        ev = run.evidence
        refusal = next((t for t in texts if "has no route for" in t), "")
        ev["first_tool_result"] = (texts[0][-900:] if texts else "")
        ev["refusal_seen"] = bool(refusal)
        ev["compose_cmd"] = self.state.get("compose_cmd", "")
        compose_out = next((t for t in texts if "FX-COMPOSE-RC=" in t), "")
        ev["compose_output"] = compose_out[-1500:]
        ev["compose_rc"] = (re.findall(r"FX-COMPOSE-RC=(\d+)", compose_out) or ["-"])[-1]
        commit_results = [t for t in texts if "FX-COMMIT-OK" in t or "has no route for" in t
                          or "commit" in t.lower()]
        ev["route_id"] = self.state.get("route_id", "")
        finish_out = next((t for t in texts if "FX-FINISH-RC=" in t), "")
        ev["finish_output"] = finish_out[-1500:]
        ev["finish_rc"] = (re.findall(r"FX-FINISH-RC=(\d+)", finish_out) or ["-"])[-1]
        log = subprocess.run(["git", "-C", str(self.other), "log", "--oneline"],
                             capture_output=True, text=True, env=run.home.env).stdout
        ev["other_log"] = log.strip().splitlines()
        committed = "fx change" in log
        ledgers = sorted((state_home(run) / "agent-fleet" / "route-chains").glob("*/*.jsonl"))
        ev["ledgers"] = {str(p.relative_to(state_home(run))): [
            {k: v for k, v in json.loads(line).items()
             if k in ("event", "route_id", "artifact_root", "automatic")}
            for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
            for p in ledgers}
        status = subprocess.run(
            [sys.executable, str(_source(run) / "utilities/capability-route.py"), "status",
             "--artifact-root", run.home.env["AGENT_ARTIFACT_ROOT"]]
            + ([ev["route_id"]] if ev["route_id"] else []),
            env=run.home.env, cwd=str(run.home.workspace), capture_output=True, text=True,
            timeout=60)
        ev["status_after"] = (status.stdout + status.stderr)[-1500:]
        ev["commit_attempts"] = len(commit_results)
        if not refusal:
            run.note("no route refusal reached the model; commit "
                     + ("went through unrouted" if committed else "did not happen"))
            return "gap" if committed else "fail"
        if ev["compose_rc"] != "0":
            run.note(f"compose named by the refusal failed rc={ev['compose_rc']}")
            return "fail"
        if not committed:
            run.note("retry after compose was still refused or failed")
            return "fail"
        if ev["finish_rc"] != "0":
            run.note(f"finish rc={ev['finish_rc']}")
            return "fail"
        closed = re.search(r"closed|finished|sealed|outcome|complete", ev["status_after"], re.I)
        if not closed:
            run.note("route status does not show closure")
            return "fail"
        run.note("refused -> compose -> retry committed -> finish closed")
        return "pass"


# --------------------------------------------------------------------------
# 2. retire
# --------------------------------------------------------------------------

WAIT_SOURCE = {"claude": "claude-asktool", "codex": "codex-appserver",
               "opencode": "opencode-question"}


def _on_herdr(run) -> bool:
    """The driver's ``--surface herdr``: the TUI is an agent of the private Herdr server."""
    return getattr(_base_module(run), "SURFACE", "tmux") == "herdr"


class Retire(Scenario):
    name = "retire"
    summary = ("peer-steward retire sends the normal exit to an idle session in the private "
               "tmux pane (or private Herdr pane with --surface herdr); the TUI exits and the "
               "session-end cleanup runs.")

    def prepare(self, run):
        if not _on_herdr(run):
            hide_herdr(run)
            install_tui(run, host_shell=True)

    def script(self, run):
        def answer(record, earlier):
            return Reply(f"retire scenario ok {record['main_index']}")
        return answer

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-RETIRE hello")
        if not run.wait_requests(1, 90):
            run.note("TUI never asked the model")
            return "inconclusive"
        run.wait_idle(5, 60)
        run.tui.settle(3, 30)
        sid = wait_for(lambda: native_session_id(run), 20)
        ev["session_id"] = sid
        if not sid:
            run.note("native session id not found")
            return "inconclusive"
        herdr = _on_herdr(run)
        env = dict(run.home.env) if herdr else tmux_env(run)
        request_id = "fx-req-1"
        seeded = py(run, (
            "from fleet import interaction; import sys\n"
            "print(interaction.set_wait(sys.argv[1], sys.argv[2], 'decision', sys.argv[3],"
            " request_id=sys.argv[4]))"), sid, run.harness, WAIT_SOURCE[run.harness],
            request_id)
        sidecar = state_home(run) / "agent-fleet" / "interactions" / run.harness / f"{sid}.json"
        ev["wait_seeded"] = seeded.stdout.strip() == "True" and sidecar.exists()
        target = run.tui.panes.get(run.tui.name, run.tui.name) if herdr else pane_address(run)
        ev["pane"] = target
        ev["pane_command_before"] = None if herdr else run.tui.pane_command()
        watcher = ProcessWatch(run.home.root, (
            "sessionend-lifecycle", "fleet-interaction-state", "session_summary_trigger",
            "peer-steward"))
        watcher.start()
        retire = subprocess.run(
            [sys.executable, str(_source(run) / "utilities/peer-steward.py"), "retire", target],
            env=env, cwd=str(run.home.workspace), capture_output=True, text=True, timeout=200)
        ev["retire_rc"] = retire.returncode
        ev["retire_output"] = (retire.stdout + retire.stderr)[-2000:]
        retired = "retired=true" in retire.stdout
        exited = bool(wait_for(lambda: not run.tui.alive(), 30))
        if not exited:
            # The implementation's own sequence, sent directly when retire did not act.
            run.note("retire did not exit the TUI; sent its exit keys directly")
            if run.harness == "claude":
                run.tui.key("Escape")
                if herdr:
                    run.tui.herdr("pane", "send-text", target, "/exit", timeout=10)
                else:
                    run.tui._tmux("send-keys", "-t", "scenario", "-l", "/exit")
                time.sleep(0.8)
                run.tui.key("Enter")
            else:
                run.tui.key("C-d")
            exited = bool(wait_for(lambda: not run.tui.alive(), 30))
            ev["manual_keys_exit"] = exited
        ev["pane_command_after"] = None if herdr else run.tui.pane_command()
        exited_at = time.time()
        # SessionEnd / session.deleted children are detached; give them a bounded while.
        cleared = bool(wait_for(lambda: not sidecar.exists(), 45, 0.5))
        ev["wait_cleared_after_exit_s"] = round(time.time() - exited_at, 1) if cleared else None
        watcher.stop()
        ev["session_end_processes_seen"] = watcher.seen
        survivors = []
        module = _base_module(run)
        for pid in module.fixture_processes(run.home.root):
            with contextlib.suppress(OSError):
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                survivors.append((pid, " ".join(Path(a.decode("utf-8", "replace")).name
                                                for a in argv[:3] if a)))
        ev["fixture_processes_after_exit"] = [name for _pid, name in survivors][:12]
        if not cleared and run.harness == "codex":
            # Codex keeps the thread in its app-server daemon after the TUI leaves; end
            # that fixture-owned daemon the way a host shutdown would and look again.
            import signal
            daemons = [pid for pid, name in survivors if "app-server" in name]
            for pid in daemons:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
            ev["codex_daemon_terminated"] = len(daemons)
            ev["wait_cleared_after_daemon_exit"] = bool(
                wait_for(lambda: not sidecar.exists(), 15, 0.5))
        ev["wait_sidecar_after"] = sidecar.exists()
        duties = []
        for path in sorted((state_home(run) / "hearting/dispatch/peer-steward/obligations"
                            ).glob("retire-*.json")):
            with contextlib.suppress(OSError, ValueError):
                duty = json.loads(path.read_text(encoding="utf-8"))
                duties.append({k: duty.get(k) for k in ("kind", "state", "result")})
        ev["retire_obligations"] = duties
        ev["pane_closed"] = (run.tui.herdr("agent", "get", run.tui.name, timeout=10).returncode != 0
                             if herdr else
                             run.tui._tmux("has-session", "-t", "scenario").returncode != 0)
        if not exited:
            run.note("TUI did not exit")
            return "fail"
        if not retired:
            run.note("peer-steward retire did not act: "
                     + (re.findall(r"reason=(\S+)", retire.stdout) or ["-"])[0])
        if not cleared:
            run.note("session-end cleanup did not clear the Fleet wait within 45 s of exit"
                     + ("; cleared only when the app-server daemon ended"
                        if ev.get("wait_cleared_after_daemon_exit") else ""))
        if retired and cleared:
            run.note("retire normal exit; shell returned; pane closed; wait cleared "
                     f"{ev['wait_cleared_after_exit_s']} s after exit")
            return "pass"
        if not retired and "agent-unknown" in retire.stdout:
            return "gap"
        return "fail"


SCENARIOS = [RouteStartClose, Retire]
