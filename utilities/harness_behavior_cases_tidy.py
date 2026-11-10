#!/usr/bin/env python3
"""Behaviour scenarios: session-tidy clear -> continue -> completion, no typing.

Loaded by ``harness_behavior_scenarios.py`` (``SCENARIOS`` below).  Each runs
on the real TUI in the base harness's throwaway HOME with the loopback model.

The first prompt is typed.  The model answers with one shell call that
registers a synthetic route-bound depth-1 child (the ``harness_behavior_cases_a``
helper, as a frame row so the seat handover applies to it) and books
``session_tidy.py tidy`` with a marked card.  The turn ends; the tidy helper
clears the window.  From then on nobody types.  The oracle is what reaches
the model:

* ``first_untyped``: the new conversation sends a request with no typed prompt;
* ``card_on_first_turn``: that first request carries the card;
* ``continue_sent``: a new-conversation request is the booked continue text;
* ``completion_woke``: once the child completes, a request carrying the
  attempt id arrives with no typed prompt, and the model's next shell call runs.

Only when one of these misses does the scenario type one probe prompt (what
the person did on 2026-11-10) and record what that turn carried.

Surfaces: ``tidy-continue`` uses the private tmux socket with an interactive
host shell (herdr hidden).  The ``-herdr`` scenarios start a private Herdr
server (the default session) inside the fixture HOME and run the TUI in its
root pane; ``HEARTING_SCENARIO_HERDR_BIN`` or the caller's ``PATH`` names the
executable, and no other Herdr server is read or written.

``-now``: the child completes right after the clear is observed.
``-gone``: the cleared session's carrier has ended before the completion (as a
carrier at an open gate does once the seat handed the wait on).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
import harness_behavior_cases_a as ca  # noqa: E402
import harness_behavior_cases_c as cc  # noqa: E402

CARD_MARK = "TIDYC-CARD-MARK"
CARD_BODY = (f"원래 목표: {CARD_MARK} 창 비우기 뒤 이어가기 검증\n"
             "진행 중: 픽스처 세션 정리\n기다리는 결정: 없음\n다음 할 일: 새 대화에서 이어가기\n"
             "관련 세션: 없음")
CONTINUE_TEXT = "이어서해"
NEXT = "TIDYC-NEXT-ACTION"
# Resolved from the driver's own PATH before the fixture hides herdr.
HERDR_BIN = os.environ.get("HEARTING_SCENARIO_HERDR_BIN") or shutil.which("herdr") or ""

# The cases_a register helper writes a review row; a route-bound frame row is the
# shape whose recipient follows the seat handover after a clear.
ROUTED_PATCH = (
    '    meta += ["launch_claimed=1",',
    '    if "routed" in flags:\n'
    '        import hashlib\n'
    '        digest = hashlib.sha256(attempt.encode()).hexdigest()\n'
    '        meta = [m if m != "worker_type=review" else "worker_type=frame" for m in meta]\n'
    '        meta += ["route_id=rt-" + digest[:16], "route_hash=sha256:" + digest, '
    '"route_node=one-shot"]\n'
    '    meta += ["launch_claimed=1",')


def install_helper(run, attempt: str) -> None:
    ca._install_helper(run, attempt)
    helper = run.home.root / "hb_fx.py"
    text = helper.read_text(encoding="utf-8")
    if ROUTED_PATCH[0] not in text:
        raise RuntimeError("cases_a register helper changed; update ROUTED_PATCH")
    helper.write_text(text.replace(ROUTED_PATCH[0], ROUTED_PATCH[1], 1), encoding="utf-8")


class HerdrTui:
    """A private Herdr server (default session) in the fixture HOME; one root pane."""

    def __init__(self, root, env, argv, cwd):
        self.root, self.env, self.argv, self.cwd = Path(root), env, argv, cwd
        self.started, self.proc, self.pane, self.log = False, None, "", None

    def h(self, *args, timeout=30):
        return subprocess.run([HERDR_BIN, *args], env=self.env, text=True,
                              capture_output=True, timeout=timeout)

    def start(self):
        endpoint = Path(self.env["HERDR_SOCKET_PATH"])
        self.log = (self.root / "herdr.log").open("w")
        self.proc = subprocess.Popen([HERDR_BIN, "server"], env=self.env, stdin=subprocess.DEVNULL,
                                     stdout=self.log, stderr=self.log)
        wait_for(lambda: endpoint.is_socket() or self.proc.poll() is not None, 10, 0.1)
        if not endpoint.is_socket():
            raise RuntimeError("private herdr unavailable")
        created = self.h("workspace", "create", "--cwd", str(self.cwd), "--label", "tidyc")
        value = json.loads(created.stdout or "{}").get("result", {}).get("root_pane")
        self.pane = value.get("pane_id") if isinstance(value, dict) else value
        if not self.pane:
            raise RuntimeError("no private pane: " + created.stdout + created.stderr)
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(self.env.items()))
        launch = self.root / "launch.sh"
        launch.write_text(f"cd {shlex.quote(str(self.cwd))} && exec env -i {exports} "
                          'HERDR_ENV="$HERDR_ENV" HERDR_PANE_ID="$HERDR_PANE_ID" '
                          'HERDR_SOCKET_PATH="$HERDR_SOCKET_PATH" HERDR_BIN_PATH="$HERDR_BIN_PATH" '
                          + " ".join(shlex.quote(a) for a in self.argv) + "\n", encoding="utf-8")
        time.sleep(1.5)
        self.h("pane", "send-text", self.pane, f"sh {launch}")
        self.h("pane", "send-keys", self.pane, "enter")
        self.started = True

    def screen(self):
        r = self.h("pane", "read", self.pane, "--source", "recent-unwrapped", "--lines", "120",
                   "--format", "text")
        try:
            data = json.loads(r.stdout).get("result", {})
            return data.get("text") or data.get("content") or ""
        except ValueError:
            return r.stdout or ""

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    def type_text(self, text, attempts=4):
        probe = text[:24]
        for _ in range(attempts):
            self.h("pane", "send-text", self.pane, text)
            if wait_for(lambda: probe in self.screen(), 6):
                time.sleep(0.6)
                self.h("pane", "send-keys", self.pane, "enter")
                return True
            self.h("pane", "send-keys", self.pane, "esc")
            time.sleep(1.5)
        return False

    def settle(self, quiet=3, timeout=45):
        deadline = time.time() + timeout
        last, since = None, time.time()
        while time.time() < deadline:
            current = self.screen()
            if current != last:
                last, since = current, time.time()
            elif time.time() - since >= quiet:
                return True
            time.sleep(0.5)
        return False

    def key(self, *keys):
        for key in keys:
            self.h("pane", "send-keys", self.pane, {"Enter": "enter", "Escape": "esc"}.get(key, key))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.log:
            self.log.close()
        self.started = False


def install_herdr_tui(run) -> None:
    if not HERDR_BIN:
        raise RuntimeError("herdr executable not found")
    env = run.home.env
    bindir = run.home.root / "herdr-bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "herdr").symlink_to(HERDR_BIN)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    env.pop("AGENT_HERDR_SESSION", None)   # the default server, as on a person's machine
    env["HERDR_SOCKET_PATH"] = str(Path(env["XDG_CONFIG_HOME"]) / "herdr" / "herdr.sock")
    module = cc._base_module(run)
    original = module.PrivateTui

    def factory(*args, **kwargs):
        module.PrivateTui = original     # one TUI per run
        return HerdrTui(run.home.root, *args[1:], **kwargs)
    module.PrivateTui = factory


DIAG = r"""
import importlib.util, json, sys
src, harness, path = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path[:0] = [src + '/utilities', src + '/tools']
spec = importlib.util.spec_from_file_location('ps', src + '/utilities/peer-steward.py')
ps = importlib.util.module_from_spec(spec); spec.loader.exec_module(ps)
req = json.load(open(path)); pane = (req.get('seat') or {}).get('pane')
out = {'pane': pane, 'new_session': req.get('new_session')}
try:
    out['look'] = ps._continue_look(pane, req)
    out['native'] = ps._continue_native_session(pane, harness, process_info=ps._retire_pane_info(pane))
except Exception as exc:
    out['error'] = repr(exc)
print(json.dumps(out, default=str)[:4000])
"""


# Samples each read ``_continue_look`` makes, with its time, from the clear on:
# the booking's own record says only which reason ended the continue.
SAMPLER = r"""
import importlib.util, json, os, sys, time
from pathlib import Path
src, harness, folder, seconds, out = sys.argv[1:6]
tag = sys.argv[6] if len(sys.argv) > 6 else 'driver'
spawned = tag != 'driver'      # one copy runs in the native TUI's own environment
sys.path[:0] = [src + '/utilities', src + '/tools']
spec = importlib.util.spec_from_file_location('ps', src + '/utilities/peer-steward.py')
ps = importlib.util.module_from_spec(spec); spec.loader.exec_module(ps)
from fleet.process_identity import process_identity
child, limit, end = None, time.time() + 600, None      # loaded before the clear; stops `seconds` after it
with open(out, 'a', encoding='utf-8') as sink:
    while time.time() < (end or limit):
        found = sorted(Path(folder).glob('*.json'))
        try:
            req = json.load(open(found[-1])) if found else {}
        except (OSError, ValueError):
            req = {}
        if req.get('status') != 'cleared':
            time.sleep(0.05)
            continue
        end = end or time.time() + float(seconds)
        row = {'t': round(time.time(), 3), 'env': tag}
        try:
            pane = (req.get('seat') or {}).get('pane')
            row.update(status=req.get('status'), new=req.get('new_session'),
                       continued=req.get('continued'))
            state, ident, _rc, why = ps._retire_target(pane)
            row['target'] = [state, ident.get('session_id'), why]
            info = ps._retire_pane_info(pane)
            row['fg'] = info and [[p.get('pid'), (p.get('argv') or [''])[0].rsplit('/', 1)[-1]]
                                  for p in info.get('foreground_processes', [])]
            if info:
                binding, why = ps._pane_shell_binding_result(pane, harness, info=info)
                row['binding'] = binding and binding.get('sid'), why
                if binding:
                    pid = binding['native_pid']
                    if not spawned:
                        spawned = True
                        import subprocess
                        raw = open(f'/proc/{pid}/environ', 'rb').read().split(b'\0')
                        native_env = dict(item.decode('utf-8', 'replace').split('=', 1)
                                          for item in raw if b'=' in item)
                        native_env['HB_SAMPLER'] = os.environ['HB_SAMPLER']
                        import session_tidy_clear as clear
                        socket = str((req.get('herdr_transport') or {}).get('socket_path') or '')
                        helper_env = clear._helper_env(native_env, socket_path=socket)
                        helper_env['HB_SAMPLER'] = os.environ['HB_SAMPLER']
                        child = [subprocess.Popen([sys.executable, '-B', '-c', os.environ['HB_SAMPLER'],
                                                   src, harness, folder, seconds, out, name],
                                                  env=env, cwd=cwd,
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                 for name, env, cwd in (
                                     ('native', native_env, os.readlink(f'/proc/{pid}/cwd')),
                                     ('helper', helper_env, str(clear.st.state_root())))]
                    ident2 = process_identity(pid, harness)
                    row['native_identity'] = [ident2.session_id, ident2.confidence]
                    registry = os.path.expanduser(f'~/.claude/sessions/{pid}.json')
                    try:
                        row['registry_sid'] = json.load(open(registry)).get('sessionId')
                    except (OSError, ValueError):
                        row['registry_sid'] = None
                if binding:
                    import pane_ownership as po
                    rec = po._process(pid)
                    nenv = {}
                    try:
                        nenv = dict(i.decode('utf-8', 'replace').split('=', 1) for i in
                                    open(f'/proc/{pid}/environ', 'rb').read().split(b'\0') if b'=' in i)
                    except OSError:
                        pass
                    sock = (ps.herdr_observation.socket_path_for_endpoint(ps._HERDR_SESSION or 'default')
                            or ps._herdr_env_for_server(ps._HERDR_SESSION).get('HERDR_SOCKET_PATH', ''))
                    row['wns'] = {'start_ok': bool(rec) and str(rec[5]) == binding['native_start'],
                                  'tty_fg': bool(rec) and bool(rec[3]) and rec[2] == rec[4],
                                  'runtime': po._runtime(pid), 'worker': po._registered_worker(pid),
                                  'native_session': po._native_session(pid, harness),
                                  'pane_env': nenv.get('HERDR_PANE_ID'), 'native_sock': nenv.get('HERDR_SOCKET_PATH'),
                                  'requested_sock': sock, 'my_sock': os.environ.get('HERDR_SOCKET_PATH'),
                                  'same_sock': po._same_socket_endpoint(nenv.get('HERDR_SOCKET_PATH', ''), sock,
                                                                         ps._HERDR_SESSION)}
                wrapped, observed = ps._wrapped_foreground(pane, harness, info)
                row['wrapped'] = [wrapped and wrapped.get('session_id'), observed]
                row['native'] = ps._continue_native_session(pane, harness, process_info=info)
            row['look'] = ps._continue_look(pane, req)
        except Exception as exc:
            row['error'] = repr(exc)[:300]
        row['t_end'] = round(time.time(), 3)
        sink.write(json.dumps(row, default=str) + chr(10)); sink.flush()
for proc in child or ():
    proc.wait(timeout=120)
"""


# The input-box judgement the clear look makes, on the same screen read, for a skip.
DRAFT_DIAG = r"""
import importlib.util, json, sys
src, harness, pane = sys.argv[1:4]
sys.path[:0] = [src + '/utilities', src + '/tools']
spec = importlib.util.spec_from_file_location('ps', src + '/utilities/peer-steward.py')
ps = importlib.util.module_from_spec(spec); spec.loader.exec_module(ps)
import peer_steward_duties as d
lines = ps._read_screen(pane) or []
marker = {'codex': '\u203a', 'claude': '\u276f'}.get(harness, '')
idx = next((i for i in range(len(lines) - 1, -1, -1)
            if marker and d._marker_remainder(lines[i], marker) is not None), None)
near = [] if idx is None else [d._plain(c) for c in lines[idx:idx + 3]]
print(json.dumps({'draft': d._draft_state(harness, lines), 'ready': d._screen_ready(harness, lines),
                  'marker_row': idx, 'rows': near,
                  'bright_after_marker': None if idx is None else
                  d._marker_remainder(lines[idx], marker)[1]}, ensure_ascii=False)[:3000])
"""


def _is_memory_worker(record) -> bool:
    return "ops/session-tidy-memory" in record["text"]


class TidyContinue(Scenario):
    name = "tidy-continue"
    summary = ("Session tidy clears the window; with no typing the new conversation starts "
               "with the card and a later completion is handled (private tmux).")
    surface = "tmux"
    late = True
    end_old_arm = False

    def __init__(self):
        self.attempt = "att-tidyc-" + uuid.uuid4().hex[:20]

    def prepare(self, run):
        if self.surface == "herdr":
            install_herdr_tui(run)
        else:
            cc.hide_herdr(run)
            cc.install_tui(run, host_shell=True)
        install_helper(run, self.attempt)

    def script(self, run):
        def tidy():
            return ("python3 " + shlex.quote(str(run.home.root / "hb_fx.py")) + " register routed && "
                    'python3 "$AGENT_HOME/utilities/session_tidy.py" tidy --text '
                    + shlex.quote(CARD_BODY) + " 2>&1; echo FX-TIDY-RC=$?")

        def answer(record, earlier):
            last, kind = record["last_text"], record["last_kind"]
            done_next = any(NEXT in (r["reply"].get("shell") or "") for r in earlier)
            if kind == "tool":
                return Reply("Next action finished." if NEXT in last else "정리 요청 완료")
            if self.attempt in record["text"] and not done_next:
                return Reply(shell=f"echo {NEXT}-$((20+22))")
            if "SCENARIO-TIDYC-START" in last and not any(
                    "FX-TIDY-RC" in (r["reply"].get("shell") or "") for r in earlier):
                return Reply(shell=tidy())
            if CONTINUE_TEXT in last:
                return Reply("이어서 진행합니다 TIDYC-CONTINUED")
            return Reply(f"OK {record['main_index']}")
        return answer

    # -- observation -------------------------------------------------------
    def _booking_paths(self, run):
        return sorted((cc.state_home(run) / "hearting" / "session-tidy" / "clear").glob("*.json"))

    def _reservations(self, run):
        found = []
        for path in self._booking_paths(run):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            found.append({"status": value.get("status"), "sid": value.get("sid"),
                          "new_session": value.get("new_session"),
                          "seat_kind": (value.get("seat") or {}).get("kind"),
                          "clear_claim": {k: (value.get("clear_claim") or {}).get(k)
                                          for k in ("state", "outcome")},
                          "continued": value.get("continued"), "reason": value.get("reason")})
        return found

    def diag(self, run):
        paths = self._booking_paths(run)
        if not paths:
            return None
        env = cc.tmux_env(run) if self.surface == "tmux" else run.home.env
        done = subprocess.run([sys.executable, "-B", "-c", DIAG, str(cc._source(run)), run.harness,
                               str(paths[-1])], env=env, text=True, capture_output=True, timeout=90)
        try:
            return json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"stdout": done.stdout[-800:], "stderr": done.stderr[-1500:]}

    def start_sampler(self, run, seconds=20):
        folder = cc.state_home(run) / "hearting" / "session-tidy" / "clear"
        env = cc.tmux_env(run) if self.surface == "tmux" else run.home.env
        out = run.home.root / "continue-look.jsonl"
        return subprocess.Popen([sys.executable, "-B", "-c", SAMPLER, str(cc._source(run)),
                                 run.harness, str(folder), str(seconds), str(out)],
                                env={**env, "HB_SAMPLER": SAMPLER}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), out

    def stop_sampler(self, sampler, wait=60):
        if not sampler:
            return None
        proc, out = sampler
        try:
            proc.wait(timeout=wait and 180)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        try:
            return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        except (OSError, ValueError):
            return None

    def _end_old_arm(self, run, old_sid):
        """End only this fixture's carrier process whose arm still names the cleared session."""
        arm = Path(run.home.env["AGENT_DISPATCH_JOBS"]).parent / "rewake-arms" / f"{self.attempt}.json"
        try:
            data = json.loads(arm.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"arm": None}
        out = {"arm_session": data.get("session_id"), "old": old_sid, "arms": data.get("arms"),
               "state": data.get("state"), "ended": False}
        if data.get("session_id") != old_sid or data.get("state") != "waiting":
            return out
        pid, start = int(data["holder"][0]), str(data["holder"][1])
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes()
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
            mine = str(run.home.root).encode() in environ and stat[19] == start
        except OSError:
            mine = False
        if mine:
            os.kill(pid, signal.SIGTERM)
            out["ended"] = True
        return out

    def drive(self, run):
        ev = run.evidence
        t0 = time.time()
        sampler = self.start_sampler(run)
        run.prompt("SCENARIO-TIDYC-START 등록하고 세션을 정리해 주세요")
        run.wait_idle(8, 120)
        mains = run.main_requests()
        # The new conversation starts after the model's reply to the tidy tool result.
        split = next((i + 1 for i, r in enumerate(mains)
                      if r["last_kind"] == "tool" and "FX-TIDY-RC" in r["last_text"]), len(mains))
        tool = next((t for t in cc.tool_texts(run) if "FX-TIDY-RC" in t), "")
        ev["tidy_output"] = tool[-1500:]
        if "FX-TIDY-RC" not in tool:
            run.note("tidy shell call never reached the model")
            self.stop_sampler(sampler, wait=0)
            return "inconclusive"

        def cleared():
            return [r for r in self._reservations(run)
                    if r.get("new_session") or r.get("status") == "cleared"] or None
        manual = "clear=manual" in tool
        hit = None if manual else wait_for(cleared, 90, 0.3)
        if manual:
            self.stop_sampler(sampler, wait=0)
            sampler = None
        ev["cleared_after_s"] = round(time.time() - t0, 1) if hit else None
        if not hit:
            ev["continue_look_samples"] = self.stop_sampler(sampler)
            ev["reservations_final"] = self._reservations(run)
            run.note("tidy answered clear=manual; nothing cleared without a person" if manual
                     else "the window was not cleared or the new session was not observed")
            return "gap" if manual else "fail"
        old_sid = hit[0].get("sid")

        def conversation():
            return [r for r in run.main_requests()[split:] if not _is_memory_worker(r)]
        time.sleep(3)
        ev["diag_after_clear"] = self.diag(run)
        completed_at = None
        if not self.late:
            completed_at = time.time()
            ev["complete"] = ca._helper(run, "complete")
        first = wait_for(lambda: conversation() or None, 150, 0.5)   # booking window is 120 s
        if first:
            req = first[0]
            ev["first_new_request"] = {"last_text": req["last_text"][-300:],
                                       "has_card": CARD_MARK in req["text"],
                                       "has_continue": CONTINUE_TEXT in req["last_text"],
                                       "has_attempt": self.attempt in req["text"]}
        else:
            ev["diag_after_window"] = self.diag(run)
        ev["continue_look_samples"] = self.stop_sampler(sampler)
        ev["first_untyped"] = bool(first)
        ev["card_on_first_turn"] = bool(first and CARD_MARK in first[0]["text"])
        ev["continue_sent"] = any(CONTINUE_TEXT in r["last_text"] for r in conversation())
        run.wait_idle(8, 60)
        if self.late:
            time.sleep(6)
            if self.end_old_arm:
                ev["old_arm_end"] = self._end_old_arm(run, old_sid)
                time.sleep(3)
            completed_at = time.time()
            ev["complete"] = ca._helper(run, "complete")
        got = wait_for(lambda: [r for r in conversation() if self.attempt in r["text"]] or None,
                       150, 1)
        run.wait_idle(8, 60)
        ev["completion_woke"] = bool(got)
        if got:
            ev["completion_seconds"] = round(got[0]["at"] - completed_at, 1)
        ev["next_action_ran"] = any(f"{NEXT}-42" in r["text"] for r in conversation())
        ev["probe_after_wait"] = ca._helper(run, "probe")
        ev["reservations_final"] = self._reservations(run)
        ok = bool(first) and ev["card_on_first_turn"] and ev["completion_woke"] \
            and ev["next_action_ran"]
        if not ok:
            count = len(run.main_requests())
            run.prompt("SCENARIO-TIDYC-PROBE 왜 이어서 안 하지?")
            run.wait_idle(8, 60)
            later = [r for r in run.main_requests()[count:] if not _is_memory_worker(r)]
            ev["typed_probe"] = {"requests": len(later),
                                 "card": any(CARD_MARK in r["text"] for r in later),
                                 "attempt": any(self.attempt in r["text"] for r in later)}
        yes = {True: "yes", False: "no"}
        run.note(f"first_untyped={yes[bool(first)]} continue={yes[ev['continue_sent']]} "
                 f"card_first={yes[ev['card_on_first_turn']]} "
                 f"completion={yes[ev['completion_woke']]} next={yes[ev['next_action_ran']]}")
        return "pass" if ok else "fail"


class TidyContinueHerdr(TidyContinue):
    name = "tidy-continue-herdr"
    summary = "tidy-continue in a private Herdr server (the completion lands after the continue)."
    surface = "herdr"


class TidyContinueHerdrNow(TidyContinueHerdr):
    name = "tidy-continue-herdr-now"
    summary = "tidy-continue-herdr with the completion right after the clear is observed."
    late = False


class TidyContinueHerdrGone(TidyContinueHerdr):
    name = "tidy-continue-herdr-gone"
    summary = "tidy-continue-herdr where the cleared session's carrier ended before the completion."
    end_old_arm = True


class TidyContinueHerdrShell(TidyContinueHerdr):
    """The window an operator with the session shell on has: the TUI runs under
    ``session-shell/entry.py`` -> ``host.cjs``, so the pane's foreground is the
    shell host and the native session is found through its attachment binding."""
    name = "tidy-continue-herdr-shell"
    summary = "tidy-continue-herdr with the TUI launched through Hearting's session shell."

    def prepare(self, run):
        module = cc._base_module(run)
        run.evidence["session_shell"] = module.enable_session_shell(run.home)
        super().prepare(run)
        factory, previous = module.PrivateTui, module.SESSION_SHELL
        module.SESSION_SHELL = True     # read by harness_argv for this run's TUI only

        def shell_factory(*args, **kwargs):
            module.SESSION_SHELL = previous
            return factory(*args, **kwargs)
        module.PrivateTui = shell_factory


class TidyContinueHerdrShellTwice(TidyContinueHerdrShell):
    """The same window cleared twice in a row keeps one session number (#1451)."""
    name = "tidy-continue-herdr-shell-twice"
    summary = "tidy-continue-herdr-shell, then a second tidy clear: the seat keeps its number."
    AGAIN = "SCENARIO-TIDYC-AGAIN"

    def script(self, run):
        first = super().script(run)

        def answer(record, earlier):
            if (self.AGAIN in record["last_text"] and record["last_kind"] != "tool"
                    and not any("FX-TIDY2-RC" in (r["reply"].get("shell") or "") for r in earlier)):
                return Reply(shell='python3 "$AGENT_HOME/utilities/session_tidy.py" tidy --text '
                             + shlex.quote(CARD_BODY) + " 2>&1; echo FX-TIDY2-RC=$?")
            if record["last_kind"] == "tool" and "FX-TIDY2-RC" in record["last_text"]:
                return Reply("두 번째 정리 요청 완료")
            return first(record, earlier)
        return answer

    def _tags(self, run):
        path = cc.state_home(run) / "agent-fleet" / "tag-assignments.json"
        try:
            rows = json.loads(path.read_text(encoding="utf-8")).get("assignments") or []
        except (OSError, ValueError):
            return {}
        return {row.get("session_id"): row.get("tag") for row in rows
                if row.get("harness") == run.harness}

    def drive(self, run):
        import threading
        ev = run.evidence
        seen, stop = {}, threading.Event()

        def watch():                       # every number any session held, in order
            while not stop.wait(0.2):
                for sid, tag in self._tags(run).items():
                    seen.setdefault(sid, tag)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            return self._twice(run, ev, seen)
        finally:
            stop.set()
            watcher.join(timeout=5)
            ev["tags_seen"] = dict(seen)

    def _twice(self, run, ev, seen):
        first = super().drive(run)
        ev["tags_after_first"] = self._tags(run)
        sids = [r.get("new_session") for r in self._reservations(run)]
        if first != "pass" or not sids or not sids[-1]:
            run.note("first clear did not finish; the second was not tried")
            return first
        middle = sids[-1]
        run.prompt(f"{self.AGAIN} 한 번 더 정리해 주세요")

        def cleared_again():
            return [r for r in self._reservations(run)
                    if r.get("sid") == middle and r.get("status") == "cleared"
                    and r.get("new_session")] or None
        again = wait_for(cleared_again, 150, 0.5)
        ev["second_clear"] = again
        if not again:
            run.note("the second tidy did not clear the window")
            return "fail"
        last = again[0]["new_session"]
        wait_for(lambda: (self._reservations(run)[-1].get("continued") or {}).get("state")
                 not in (None, "pending", "sending") or None, 150, 1)
        run.wait_idle(8, 60)
        ev["reservations_final"] = self._reservations(run)
        tags = self._tags(run)
        ev["tags_after_second"] = tags
        origin = next(iter(seen.values()), None)
        before = ev["tags_after_first"].get(middle)
        ev["tag_kept"] = bool(before) and tags.get(last) == before == origin
        ev["second_continue"] = (ev["reservations_final"][-1].get("continued") or {}).get("state")
        run.note(f"tag origin={origin} first={before} second={tags.get(last)} "
                 f"second_continue={ev['second_continue']}")
        return "pass" if ev["tag_kept"] and ev["second_continue"] == "sent" else "fail"


class TidyContinueNoticeHerdr(TidyContinueHerdr):
    """A continue that is not sent shows its line on the window with no typing.

    2026-11-10: the expired continue's line reached the person ~55 minutes later, with
    their next input. The booking window is shortened so the continue expires (the
    real case); Herdr keeps its default toast setting (``disabled``). The oracle is
    the pane's own screen: the result line painted by Hearting's existing display.
    """
    name = "tidy-continue-notice-herdr"
    summary = "an expired continue's line appears on the private Herdr pane before any typing."
    WINDOW = "0"      # expired at the first look, as the 2026-11-10 continue was

    def prepare(self, run):
        super().prepare(run)
        run.home.env["HEARTING_TIDY_TEST_ROOT"] = str(cc.state_home(run))
        run.home.env["HEARTING_TIDY_CONTINUE_WINDOW"] = self.WINDOW

    def script(self, run):
        first = super().script(run)

        def answer(record, earlier):
            if CONTINUE_TEXT in record["last_text"] and record["last_kind"] != "tool":
                return Reply("TIDYC-UNEXPECTED-CONTINUE")
            return first(record, earlier)
        return answer

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-TIDYC-START 등록하고 세션을 정리해 주세요")
        run.wait_idle(8, 120)
        mains = run.main_requests()
        split = next((i + 1 for i, r in enumerate(mains)
                      if r["last_kind"] == "tool" and "FX-TIDY-RC" in r["last_text"]), len(mains))
        tool = next((t for t in cc.tool_texts(run) if "FX-TIDY-RC" in t), "")
        ev["tidy_output"] = tool[-800:]
        if "FX-TIDY-RC" not in tool:
            run.note("tidy shell call never reached the model")
            return "inconclusive"

        def settled():
            rows = self._reservations(run)
            if any(r.get("status") in ("skipped", "failed", "unverified") for r in rows):
                return "clear-not-sent"       # probed at once: a later hook takes the line
            rows = [r for r in rows
                    if (r.get("continued") or {}).get("state") not in (None, "pending", "sending")]
            return rows or None
        done = wait_for(settled, 200, 0.5)
        ev["reservations_final"] = self._reservations(run)
        if done == "clear-not-sent":
            done = None
        if not done:
            ev["diag"] = self.diag(run)
            last = (ev["reservations_final"] or [{}])[-1]
            ev["screen_tail"] = run.tui.screen()[-2500:]
            if last.get("status") in ("skipped", "failed", "unverified"):
                # The clear itself was not sent: its own result line is the one to show.
                pane = getattr(run.tui, "pane", "")
                probe = subprocess.run([sys.executable, "-B", "-c", DRAFT_DIAG, str(cc._source(run)),
                                        run.harness, pane], env=run.home.env, text=True,
                                       capture_output=True, timeout=90)
                ev["draft_diag"] = (probe.stdout or probe.stderr)[-3000:]
                any_re = re.compile(r"(?:\[정리\]|\[tidy\])[^\[]{0,160}")
                seen = wait_for(lambda: self._painted(run, any_re), 100, 1)
                ev["clear_notice_painted"] = seen
            run.note(f"continue never settled: status={last.get('status')} "
                     f"reason={last.get('reason')} continued={last.get('continued')} "
                     f"clear_notice_painted={'yes' if ev.get('clear_notice_painted') else 'no'}")
            return "fail"
        settled_at = time.time()
        continued = done[-1]["continued"]
        ev["continued"] = continued

        # The result line names the input in quotes, "[정리] ... "이어서해"를 넣지 ...";
        # panel rows may wrap it, so frame bars and line breaks are folded first.
        line_re = re.compile(r'(?:\[정리\]|\[tidy\])[^\[]{0,120}?"' + re.escape(CONTINUE_TEXT) + '"')

        seen = wait_for(lambda: self._painted(run, line_re), 100, 1)   # status line: 60 s refresh
        ev["painted_after_s"] = round(time.time() - settled_at, 1) if seen else None
        ev["painted_lines"] = seen
        ev["screen_tail"] = run.tui.screen()[-2500:]
        typed_after = [r for r in run.main_requests()[split:] if not _is_memory_worker(r)]
        ev["requests_after_clear"] = [r["last_text"][-160:] for r in typed_after]
        run.note(f"continued={continued.get('state')}/{continued.get('reason')} "
                 f"painted={'yes' if seen else 'no'} after_s={ev['painted_after_s']}")
        return "pass" if seen else "fail"


    @staticmethod
    def _painted(run, pattern):
        lines = run.tui.screen().splitlines()
        # The side panel is the text between a row's last two frame bars; the
        # native rows (status line) are read without it.
        panel = [line.rsplit("│", 2)[1] for line in lines if line.count("│") >= 2]
        native = [line.rsplit("│", 2)[0] if line.count("│") >= 2 else line for line in lines]
        found = []
        for where, rows in (("panel", panel), ("native", native)):
            folded = " ".join(re.sub(r"[│┌┐└┘─]", " ", "\n".join(rows)).split())
            found += [{"where": where, "text": hit} for hit in pattern.findall(folded)]
        if found:
            found.append({"rows": [line.rstrip() for line in lines
                                   if "[tidy]" in line or "[정리]" in line or CONTINUE_TEXT in line]})
        return found or None


class TidyContinueNoticeHerdrShell(TidyContinueNoticeHerdr, TidyContinueHerdrShell):
    """The same expired continue in a window started through Hearting's session shell."""
    name = "tidy-continue-notice-herdr-shell"
    summary = "tidy-continue-notice-herdr with the TUI launched through Hearting's session shell."


SCENARIOS = (TidyContinue, TidyContinueHerdr, TidyContinueHerdrNow, TidyContinueHerdrGone,
             TidyContinueHerdrShell, TidyContinueHerdrShellTwice,
             TidyContinueNoticeHerdr, TidyContinueNoticeHerdrShell)
