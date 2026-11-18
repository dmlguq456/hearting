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
    before it is not.  Claude's own agent view that keeps a viewer needs a subscription login and
    is not started: the stand-in carries the two facts the link reads, nothing else of it.

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

SLEEP = "import time; time.sleep(900)"
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
        child = subprocess.Popen(
            [str(link), "-c", SLEEP, "--session-id", new, "--fork-session", "--resume", str(transcript)],
            env=run.home.env, cwd=str(run.home.workspace), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run.standins = [child]
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
        ok = bool(followed and back and delivered and changed)
        if not ok:
            run.note("followed=%s returned=%s delivered=%s" % (followed, back, delivered))
        return "pass" if ok else "fail"


SCENARIOS = (ParkedPanel,)
