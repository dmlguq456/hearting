#!/usr/bin/env python3
"""Background-park scenario for ``harness_behavior_scenarios.py`` (11-18).

``parked-panel``
    Claude's "send to background" leaves the window as a viewer of the old session id while the
    conversation goes on as another session in another process.  The real Claude TUI runs under the
    session shell in the fixture HOME (the viewer, with its own record); a second process stands in
    for the background session (``--session-id NEW --fork-session --resume <OLD>.jsonl``, a
    ``kind=bg`` record with the job id, its own display tap, title and Fleet number), and the
    viewer's record is given ``parkedJobId`` as Claude writes it.  Pass: the panel the window draws
    names NEW's number and context while the link holds, and the original's again when the viewer's
    record no longer holds the job; a row still addressed to the old id that was made after the
    stand-in started is delivered to NEW through the installed hook boundary, once, and one made
    before it is not.  Ownership (11-27): the stand-in runs the installed SessionStart hook
    (``source=fork``) as its own child, as the background process would; afterwards a request
    addressed to the old id, the old id's steward marker and its watch answer for NEW, and the
    transition row is written once.  Claude's own agent view that keeps a viewer needs a
    subscription login and is not started: the stand-in carries the two facts the link reads,
    nothing else of it.

    python3 utilities/harness_behavior_scenarios.py --scenario parked-panel --harness claude \
        --session-shell --output-dir DIR

Synthetic data stays under the fixture root; nothing touches the operator's HOME.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
from harness_behavior_cases_b import fixture_python, session_id  # noqa: E402

# The background stand-in: it runs what it is handed as its own child (a hook of the session it
# stands for), so the hook finds this process as its runtime, as under the real daemon.
STANDIN = r"""
import json, os, subprocess, sys, time
inbox = os.environ.get("PARKED_STANDIN_INBOX", "")
ready = os.path.join(inbox, "ready")
with open(ready + ".tmp", "w") as handle:          # the ready signal: argv is readable by now
    handle.write(str(os.getpid()))
os.replace(ready + ".tmp", ready)
deadline = time.time() + 900
while time.time() < deadline:
    for name in sorted(os.listdir(inbox)) if inbox and os.path.isdir(inbox) else ():
        if not name.endswith(".job"):
            continue
        path = os.path.join(inbox, name)
        job = json.load(open(path))
        os.unlink(path)
        done = subprocess.run(job["argv"], input=job.get("stdin", ""), text=True, capture_output=True,
                              env=job["env"], cwd=job["cwd"], timeout=60)
        out = path[:-4] + ".out"
        with open(out + ".tmp", "w") as handle:
            json.dump({"rc": done.returncode, "stdout": done.stdout[-4000:], "stderr": done.stderr[-4000:]}, handle)
        os.replace(out + ".tmp", out)
    time.sleep(0.1)
"""
JOB = "e675a253"

SEED_CODE = r"""
import json, os, time
from fleet import titles, session_tags, session_registry
sid, pid, start = args["sid"], args["pid"], args["start"]
titles.write(sid, "held conversation", summary="NOW the held conversation goes on", now=time.time())
tag = session_tags.assign_observed("claude", sid, pid=pid, proc_start=start)
import human_wait_state as hws
route = {"id": "rt-held", "title": "route of the held session", "state": "running", "worker_stage": "plan",
         "shape": "one-shot", "owner": "autopilot-code", "stages": [{"name": "plan", "st": "active"}]}
key = "claude\0" + sid
ok = hws.publish_cache({"items": [], "complete": True, "observed_at": time.time(),
                        "route_sessions": {key: time.time()}, "route_status": {key: "route 1"},
                        "route_panels": {key: [route]}, "health": []})
print(json.dumps({"tag": tag, "published": ok}))
"""

DELIVER_CODE = r"""
import io, json, time
pm = load("_peer_message_parked", "utilities/peer-message.py")
old, new, started = args["old"], args["new"], args["started"]
sender = {"harness": "unknown", "session_id": "", "name": None}
def pend(body, created):
    text, ref = pm.prepare_peer_message(body, sender, {"harness": "claude", "session_id": old, "name": None}, defer=True)
    row = pm._read_pending(ref)
    row.update(created=created)
    pm._save_pending(row)
    return ref
before = [pend("PARKED-BEFORE-%d" % i, started - 100 - i) for i in range(3)]
after = pend("PARKED-AFTER", started + 5)
out = []
for _ in range(2):
    stream = io.StringIO()
    pm.emit_peer_context({"harness": "claude", "session_id": new}, "UserPromptSubmit", stream=stream, native_delivery=True)
    out.append(json.loads(stream.getvalue())["hookSpecificOutput"]["additionalContext"] if stream.getvalue() else "")
print(json.dumps({"first": out[0][:60], "second": out[1],
                  "states": {"after": pm._read_pending(after)["state"],
                             "before": [pm._read_pending(r)["state"] for r in before]}}))
"""


OWN_CODE = r"""
import json, uuid
import peer_request as pr
import session_tidy as tidy
pm = load("_peer_message_parked_own", "utilities/peer-message.py")
old = args["old"]
native = tidy._native_session_key("claude", old)
seat = ""
for path in sorted((tidy.state_root() / "sessions").glob("*.jsonl")):
    if path.stem != native and any(old in line for line in path.read_text().splitlines()):
        seat = path.stem
        break
worker = str(uuid.uuid4())
marker = pm.steward_marker_path("claude", old)
marker.parent.mkdir(parents=True, exist_ok=True)
marker.write_text(json.dumps({"schema_version": 1, "harness": "claude", "session_id": old,
    "since": "2026-11-27T00:00:00Z", "generation": "parked-own-1127", "seat_key": seat,
    "targets": {worker: {"harness": "codex", "session_id": worker, "source": "explicit",
                         "kind": "explicit", "ts": "2026-11-27T00:00:00Z"}}}))
watches = pm.peer_state_root() / "peer-watches"
watches.mkdir(parents=True, exist_ok=True)
(watches / "parked-own-watch.json").write_text(json.dumps({"watch_id": "parked-own-watch",
    "steward": {"harness": "claude", "session_id": old}, "steward_seat": {"key": seat},
    "server": "default", "target": "w9:p9", "agent": {"harness": "codex", "session_id": worker},
    "until": ["done"], "armed_ts": "2026-11-27T00:00:00Z", "deadline": None}))
record, _entry = pr.open_request({"harness": "claude", "session_id": str(uuid.uuid4()), "name": None},
                                 {"harness": "claude", "session_id": old, "name": None}, title="PARKED-REQUEST")
print(json.dumps({"seat": seat, "request": record["id"]}))
"""

OWNED_CODE = r"""
import json
import peer_request as pr
import dispatch_seat_handover as handover
import session_tidy as tidy
old, new, request = args["old"], args["new"], args["request"]
rows = set()
for path in (tidy.state_root() / "sessions").glob("*.jsonl"):
    for line in path.read_text().splitlines():
        item = json.loads(line)
        if item.get("event") == "parked-fork":
            rows.add((item.get("from"), item.get("sid"), json.dumps(item.get("parked"), sort_keys=True)))
watch = handover.resolve_watch("parked-own-watch") or {}
print(json.dumps({"rows": len(rows),
                  "request_follows": pr.follow({"harness": "claude", "session_id": old})["session_id"] == new,
                  "request_listed_for_new": request in [r["id"] for r in pr.session_requests("claude", new)["in"]],
                  "steward_new": bool(handover.resolved_steward_relations("claude", new)),
                  "steward_old": bool(handover.resolved_steward_relations("claude", old)),
                  "watch_holder_new": (watch.get("holder") or {}).get("sid") == new}))
"""


class ParkedPanel(Scenario):
    name = "parked-panel"
    summary = ("The window of a conversation sent to the background draws the held session's number and "
               "context in its panel; rows for the old id made after the split reach the held session once.")

    def script(self, run):
        def answer(record, earlier):
            return Reply("PARKED-OK %d" % record["main_index"])
        return answer

    def cleanup(self, run):
        for child in getattr(run, "standins", ()):
            child.kill()
            child.wait()

    def _records(self, run):
        directory = Path(run.home.env["CLAUDE_CONFIG_DIR"]) / "sessions"
        found = {}
        for path in directory.glob("*.json") if directory.is_dir() else ():
            try:
                found[int(path.stem)] = (path, json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return found

    def _panel(self, run, label):
        screen = run.tui.screen()
        run.evidence.setdefault("screens", {})[label] = screen
        return screen

    def drive(self, run):
        run.prompt("SCENARIO-PARKED hello")
        run.wait_idle(6, 120)
        old = session_id(run)
        viewer = next(((pid, path, record) for pid, (path, record) in self._records(run).items()
                       if record.get("sessionId") == old), None)
        if not (old and viewer):
            run.note("the real window's session id or record was not found")
            return "fail"
        vpid, vpath, vrecord = viewer
        new = str(uuid.uuid4())
        link = Path(run.home.root) / "standin" / "claude"
        link.parent.mkdir(exist_ok=True)
        if not link.exists():
            link.symlink_to(sys.executable)
        config = Path(run.home.env["CLAUDE_CONFIG_DIR"])
        transcript = config / "projects" / "fixture" / (old + ".jsonl")
        inbox = Path(run.home.root) / "standin" / "inbox"
        inbox.mkdir(exist_ok=True)
        child = subprocess.Popen(
            [str(link), "-c", STANDIN, "--session-id", new, "--fork-session", "--resume", str(transcript)],
            env={**run.home.env, "PARKED_STANDIN_INBOX": str(inbox)}, cwd=str(run.home.workspace),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run.standins = [child]
        # Read the stand-in's /proc entry only once it says it runs (no fixed wait).
        if not wait_for(lambda: (inbox / "ready").exists() or child.poll() is not None, 30) \
                or child.poll() is not None:
            run.note("the background stand-in did not start")
            return "fail"
        stat = Path("/proc/%d/stat" % child.pid).read_text().rsplit(")", 1)[1].split()
        start = stat[19]
        started = time.time()
        (config / "sessions" / ("%d.json" % child.pid)).write_text(json.dumps(
            {"pid": child.pid, "sessionId": new, "cwd": str(run.home.workspace), "startedAt": int(started * 1000),
             "procStart": start, "kind": "bg", "jobId": JOB, "status": "busy"}))
        (config / ".statusline").mkdir(exist_ok=True)
        (config / ".statusline" / (new + ".json")).write_text(json.dumps(
            {"session_id": new, "pid": child.pid, "proc_start": start, "cwd": str(run.home.workspace),
             "model": {"display_name": "Held Model", "id": "held-model"}, "effort": {"level": "max"},
             "context_window": {"used_percentage": 23, "context_window_size": 1000000}}))
        seeded = fixture_python(run, SEED_CODE, {"sid": new, "pid": child.pid, "start": start})
        run.evidence["seeded"] = seeded
        before = self._panel(run, "before-link")
        # Claude writes parkedJobId into the viewer's record when the conversation is sent away.
        vrecord["parkedJobId"] = JOB
        tmp = vpath.with_suffix(".tmp")
        tmp.write_text(json.dumps(vrecord))
        os.replace(tmp, vpath)
        tag = str((seeded or {}).get("tag") or "")
        followed = wait_for(lambda: "23%" in run.tui.screen() and ("[%s]" % tag) in run.tui.screen(), 40)
        after = self._panel(run, "linked")
        run.evidence["tag_of_held_session"] = tag
        run.evidence["panel_followed"] = bool(followed)
        # The person comes back: the window's record holds no job, the panel reads the original again.
        vrecord.pop("parkedJobId", None)
        tmp.write_text(json.dumps(vrecord))
        os.replace(tmp, vpath)
        back = wait_for(lambda: "23%" not in run.tui.screen(), 40)
        self._panel(run, "link-ended")
        run.evidence["panel_returned"] = bool(back)
        # Delivery: restore the link, then the held session's own hook boundary takes rows once.
        vrecord["parkedJobId"] = JOB
        tmp.write_text(json.dumps(vrecord))
        os.replace(tmp, vpath)
        delivery = fixture_python(run, DELIVER_CODE, {"old": old, "new": new, "started": started})
        run.evidence["delivery"] = delivery
        states = (delivery or {}).get("states") or {}
        delivered = ("PARKED-AFTER" in str((delivery or {}).get("first"))
                     and (delivery or {}).get("second") == ""
                     and states.get("after") == "received"
                     and all(state != "received" for state in states.get("before", ["received"])))
        run.evidence["delivered_once_after_start_only"] = bool(delivered)
        changed = before != after
        run.evidence["screen_changed_by_link"] = changed
        # Ownership: the old id's request, steward marker and watch; then NEW's own start hook.
        own = fixture_python(run, OWN_CODE, {"old": old})
        run.evidence["owned_by_old"] = own
        hooked = self._standin_hook(run, inbox, {"session_id": new, "hook_event_name": "SessionStart",
                                                 "source": "fork", "cwd": str(run.home.workspace)})
        again = self._standin_hook(run, inbox, {"session_id": new, "hook_event_name": "UserPromptSubmit",
                                                "prompt": "PARKED-NEXT", "cwd": str(run.home.workspace)})
        run.evidence["standin_hooks"] = [hooked, again]
        owned = fixture_python(run, OWNED_CODE, {"old": old, "new": new,
                                                 "request": str((own or {}).get("request") or "")})
        run.evidence["owned_after_new_hook"] = owned
        moved = bool(owned and owned.get("rows") == 1 and owned.get("request_follows")
                     and owned.get("request_listed_for_new") and owned.get("steward_new")
                     and not owned.get("steward_old") and owned.get("watch_holder_new"))
        run.evidence["ownership_followed"] = moved
        ok = bool(followed and back and delivered and changed and moved)
        if not ok:
            run.note("followed=%s returned=%s delivered=%s ownership=%s" % (followed, back, delivered, moved))
        return "pass" if ok else "fail"

    def _standin_hook(self, run, inbox, payload):
        """Run the installed Claude hook for ``payload`` as a child of the background stand-in."""
        config = Path(run.home.env["CLAUDE_CONFIG_DIR"])
        env = {key: value for key, value in run.home.env.items()
               if not key.startswith(("HEARTING_SHELL_", "HERDR_"))}
        name = "%d-%s" % (time.time_ns(), uuid.uuid4().hex[:6])
        job = {"argv": ["sh", str(config / "hooks" / "run-hook.sh"), "bash", "session-card-inject.sh"],
               "stdin": json.dumps(payload), "env": env, "cwd": str(run.home.workspace)}
        tmp = inbox / (name + ".tmp")
        tmp.write_text(json.dumps(job))
        os.replace(tmp, inbox / (name + ".job"))
        out = inbox / (name + ".out")
        if not wait_for(out.exists, 60):
            return {"rc": None, "reason": "the stand-in did not run the hook"}
        return json.loads(out.read_text())


SCENARIOS = (ParkedPanel,)
