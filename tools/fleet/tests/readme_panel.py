"""Public README demo for the session panel: English, neutral, synthetic.

The panel of one ``readme_demo`` session, drawn off-screen. Its rows come from the shell
backend's own projection (``utilities/session_shell.py`` ``panel_projection``) over the README
demo world: the route rows are the ones Fleet's session line reads from the same
``readme_demo.snapshot()`` (``session_route_status``), so the panel head and Fleet's row for the
same session show the same values. Only the backend's readers of live state (the native session,
answer waits, prompt record, message ledger, request records, memory journal, open work) are
replaced by the demo values below. The ROUTE popup is the backend's own popup reading
(``_panel_popup``) over Fleet's view model of the same demo world, so it reads as Fleet's screens of
that world do; the REPO row's status button and its popup read a demo campaign status document
written into the temporary HOME with the backend's own status reader (``fleet.session_status``).
The panel and its popups are painted by ``utilities/session-shell/panel-view.cjs`` under node, with
the question editor ``block-input.cjs`` for focus and answers.

Nothing here reads the real home directory, a registry, herdr, tmux or the network; no live
window, session or model is started. ``readme_captures.py`` draws the published images from it.
"""
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
for _path in (str(ROOT), str(ROOT / "tools"), str(ROOT / "utilities")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

PANEL_VIEW = ROOT / "utilities" / "session-shell" / "panel-view.cjs"
BLOCK_INPUT = ROOT / "utilities" / "session-shell" / "block-input.cjs"

# Sessions of the demo world the panel images show (``readme_demo.snapshot()`` rows).
API, API_REVIEW, API_NOTES, ML = ("readme-claude-api", "readme-codex-api", "readme-claude-notes",
                                  "readme-claude-ml")


def _ago(now, minutes):
    return now - minutes * 60


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


def _received(now):
    """What each demo session's own prompt record kept: peer messages it received (the same
    exchanges, at the same minutes, as ``readme_demo``'s peer records Fleet's PEERS reads)."""
    return {
        API: [{"source": "peer", "sender": "Codex [b2]", "sender_harness": "codex",
               "sender_session_id": API_REVIEW, "kind": "reply", "at": _ago(now, 19),
               "text": "Pagination review done: 2 findings, both in cursor.py; details in the report."}],
        API_NOTES: [{"source": "peer", "sender": "Claude [4f]", "sender_harness": "claude",
                     "sender_session_id": API, "kind": "steer", "at": _ago(now, 8),
                     "text": "The release notes can wait for the limiter; I will ping you."}],
        ML: [{"source": "peer", "sender": "OpenCode [e8]", "sender_harness": "opencode",
              "sender_session_id": "readme-oc-ml", "kind": "reply", "at": _ago(now, 6),
              "text": "8 loader workers is the knee: 1.9x over 4, flat after that."}],
    }


def _sent(now):
    """Each demo session's own sent-message ledger rows (``_sent_peer_messages``)."""
    return {
        API: [{"to": {"harness": "claude", "session_id": API_NOTES, "name": "web-api-9c"},
               "summary": "The release notes can wait for the limiter; I will ping you.",
               "at": _ago(now, 8), "message_id": "m-demo-1", "kind": "steer",
               "status": "received", "refs": []}],
        API_REVIEW: [{"to": {"harness": "claude", "session_id": API, "name": "web-api-4f"},
                      "summary": "Pagination review done: 2 findings, both in cursor.py; details in the report.",
                      "at": _ago(now, 19), "message_id": "m-demo-3", "kind": "reply",
                      "status": "received", "refs": []}],
        ML: [{"to": {"harness": "opencode", "session_id": "readme-oc-ml", "name": "ml-train-e8"},
              "summary": "Profile 4, 8 and 16 loader workers and tell me where it flattens.",
              "at": _ago(now, 14), "message_id": "m-demo-2", "kind": "steer",
              "status": "received", "refs": []}],
    }


def _request(rid, title, state, requester, recipient, updated, revisions, reports=(), body=""):
    return {"id": rid, "title": title, "state": state, "updated": updated, "cancel": None,
            "requester": requester, "recipient": recipient, "revisions": revisions,
            "reports": list(reports), "body": body}


def _requests(now):
    """The request records each demo session is a party to (``_request_views``)."""
    four_f = {"harness": "claude", "session_id": API, "name": "web-api-4f"}
    b2 = {"harness": "codex", "session_id": API_REVIEW, "name": "web-api-b2"}
    tests = _request(
        "rq-7d21c4", "Test run", "partial", four_f, b2, _ago(now, 2),
        [{"n": 1, "label": "", "state": "current", "transfer_ref": "t-demo-1"}],
        [{"n": 1, "kind": "partial", "revision": 1, "at": _ago(now, 2), "transfer_ref": "t-demo-2",
          "result": "31 of 40 suites pass; 9 still running", "remaining": "9 suites"}],
        body="Re-run the API test suites on feat/rate-limit and report the result.\n"
             "A partial report is fine while suites are still running.")
    review = _request(
        "rq-3a90e1", "Review the pagination fix", "final", four_f, b2, _ago(now, 19),
        [{"n": 1, "label": "", "state": "current", "transfer_ref": "t-demo-3"}],
        [{"n": 1, "kind": "final", "revision": 1, "at": _ago(now, 19), "transfer_ref": "t-demo-4",
          "result": "2 findings, both in cursor.py"}],
        body="Review the pagination off-by-one fix and report findings.")
    seen = {"t-demo-1": {"state": "received"}, "t-demo-2": {"state": "received"},
            "t-demo-3": {"state": "received"}, "t-demo-4": {"state": "queued"}}
    out = [{"record": tests, "side": "out", "rows": seen, "gone": False, "body": tests["body"]},
           {"record": review, "side": "out", "rows": seen, "gone": False, "body": review["body"]}]
    inbound = [{"record": tests, "side": "in", "rows": seen, "gone": False, "body": tests["body"]}]
    return {API: out, API_REVIEW: inbound}


def _questions(now):
    """The answer-wait board of the demo session that waits on you (``[9c]``)."""
    owner = {"harness": "claude", "session_id": API_NOTES}
    return {API_NOTES: [
        {"id": "q-demo-layout", "revision": "r1", "request_state": "open", "answerable": True,
         "answer_kind": "message", "owner": owner, "since": _ago(now, 7),
         "questions": [{"id": "layout", "question": "Which layout for the 2.4 notes?",
                        "choices": ["Per release", "Per component", "Keep 2.3's"],
                        "recommended": 0}]},
        {"id": "q-demo-scope", "revision": "r1", "request_state": "open", "answerable": True,
         "answer_kind": "message", "owner": owner, "since": _ago(now, 5),
         "questions": [{"id": "scope", "question": "Include internal API changes?",
                        "choices": ["Public only", "Public and internal"],
                        "recommended": 0}]},
    ]}


def _memory(clock):
    """The newest saved memory records of each demo project and the global ones (``recent_memory``:
    both, newest first), each with its tier and scope as the memory system names them. The panel
    counts a record's age from the wall clock, so ``clock`` is the capture's own real time."""
    def record(kind, tier, scope, minutes, text):
        return {"type": kind, "tier": tier, "scope": scope, "ts": _iso(_ago(clock, minutes)),
                "headline_id": None, "headline": "", "snippet": text}
    everywhere = [record("lesson", "working", "global", 95, "Keep long test runs on the shared runner."),
                  record("user-correction", "durable", "global", 150, "Give times in UTC, not local time."),
                  record("preference", "durable", "global", 1500,
                         "Short change reports: what changed, what was checked, what is left.")]
    return {
        "web-api": [record("decision", "durable", "project", 41, "The limit lives in the gateway, per API key."),
                    *everywhere],
        "ml-train": [everywhere[0], everywhere[1],
                     record("decision", "durable", "project", 180, "Evaluate on the 16 kHz subset first."),
                     everywhere[2]],
    }


def _git(worktrees):
    """A clean Git observation: no operation under way, up to date, ``worktrees`` linked ones."""
    return {"repo": 1, "operation": "none", "dirty": 0, "ahead": 0, "behind": 0,
            "extra_worktrees": worktrees}


STATUS_DOCUMENT = """# web-api status

Updated 2026-11-16 09:20

**One line**: the login rate limit is in execute; the 2.4 release notes wait on two choices.

## Decisions needed
- The layout of the 2.4 release notes ([9c] asks)
- Whether the notes include internal API changes ([9c] asks)

## Next
- Finish the limiter tests, then the change report
- Re-run the API suites on feat/rate-limit ([b2], rq-7d21c4)

## Recently closed
- Fix the pagination off-by-one: two findings in cursor.py, fixed
"""


def write_status_document(project, now, minutes=55):
    """A demo campaign status document for ``project`` (``campaigns/<campaign>/STATUS.md``, the
    project status document ``core/ARTIFACT_META.md`` names), changed ``minutes`` before ``now``."""
    import artifact_admission as admission
    root = Path(project) / ".agent_reports"
    root.mkdir(parents=True, exist_ok=True)
    admission.ensure_root_identity(root)
    page = root / "campaigns" / "2026-11-10_rate-limit" / "STATUS.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(STATUS_DOCUMENT, encoding="utf-8")
    os.utime(page, (now - minutes * 60, now - minutes * 60))
    return Path(project)


def with_status_document(snapshot, now):
    """``(snapshot, {project: folder})``: the demo world with web-api's status document, written into
    the temporary HOME (``write_status_document``) and read by Fleet's own collector
    (``collectors.project_status``) for the sessions of that project, so Fleet's project line, the
    panel's status button and its popup read the same page."""
    import copy
    from fleet.collectors import project_status
    folders = {"web-api": write_status_document(Path(os.environ["HOME"]) / "demo-projects" / "web-api", now)}
    documents = {}
    for session in snapshot.sessions:
        folder = folders.get(os.path.basename(str(session.cwd or "")))
        if folder is not None:
            project_status.clear_cache()
            with mock.patch("time.time", return_value=now):
                documents[session.cwd] = project_status.collect([str(folder)], now).get(str(folder))
    value = copy.copy(snapshot)
    value.project_status = documents
    return value, folders


class World:
    """The demo world's panel inputs, keyed by native session id."""

    def __init__(self, snapshot, now, hosts=None):
        from tools.fleet.session_line import session_route_status
        # The status document of the project the first picture shows, in the temporary HOME.
        snapshot, self.status_projects = with_status_document(snapshot, now)
        self.snapshot, self.now, self.hosts = snapshot, now, hosts
        self.sessions = {s.session_id: s for s in snapshot.sessions if not s.is_child}
        self.tags = {(s.harness, s.session_id): "[" + s.session_tag + "]"
                     for s in self.sessions.values() if s.session_tag}
        with mock.patch("time.time", return_value=now):
            self.labels, self.panels = session_route_status(snapshot, include_details=True)
        self.received, self.sent = _received(now), _sent(now)
        self.requests, self.questions, self.memory = _requests(now), _questions(now), _memory(time.time())
        self._view_model = None
        self._status = {}

    def view_model(self):
        """Fleet's view model of this world at its moment, built as the backend's popups build it
        (``_popup_view_model``: the same builder over the collected snapshot), read once."""
        if self._view_model is None:
            from fleet.explorer import live
            with mock.patch("time.time", return_value=self.now):
                self._view_model = live.build_view_model(self.snapshot, now=self.now)
        return self._view_model

    def status_folder(self, sid):
        """The temporary folder holding the demo status document of ``sid``'s project, else None
        (a project without one shows no status button, as one without an artifact root)."""
        found = self.status_projects.get(os.path.basename(str(self.sessions[sid].cwd or "")))
        return str(found) if found else None

    def status(self, sid):
        """The REPO row's status button value (``session_shell._panel_status``: ``session_status.read``)
        for ``sid``'s project, read once at the world's moment; None without a document folder."""
        if sid not in self._status:
            folder = self.status_folder(sid)
            value = None
            if folder:
                import session_shell
                from fleet import session_status
                session_status.clear_cache()
                session = self.sessions[sid]
                binding = {"harness": session.harness, "sid": sid}
                with mock.patch("time.time", return_value=self.now):
                    value = session_shell._panel_status(binding, {"cwd": folder, "routes": []}).get("status")
            self._status[sid] = value
        return self._status[sid]

    def tag(self, harness, sid):
        return self.tags.get((harness, sid), "")

    def values(self, harness, sid):
        s = self.sessions[sid]
        quota = [("5h", 18, None), ("7d", 40, None)] if harness == "claude" else (
            [("5h", s.rl_5h, None), ("7d", s.rl_7d, None)] if s.rl_5h is not None else [])
        return {"cwd": s.cwd, "model": s.model, "effort": s.effort, "ctx_pct": s.ctx_pct,
                "active_context_tokens": None, "context_window_tokens": None,
                "compaction_pressure": None, "harness": harness, "title": s.title,
                "now": s.summary or None, "native_turn": None,
                "git": _git(1 if any(j.parent_sid == sid for j in self.snapshot.jobs) else 0),
                "branch": s.branch, "weekly_pct": quota[1][1] if len(quota) > 1 else None,
                "quota_windows": quota, "model_quotas": [], "jobs": None}

    def view(self):
        waiting = {}
        for sid, items in self.questions.items():
            harness = self.sessions[sid].harness
            waiting[(harness, sid)] = [item["id"] for item in items]
        # ``session_doers`` is the map Fleet's shared producer publishes (the model and effort of each
        # session it read), here read from the demo snapshot by the same function.
        import human_wait_state
        from fleet.explorer import live
        view = {"route_panels": self.panels, "by_session": waiting,
                "session_doers": human_wait_state.session_doers(self.snapshot),
                "items": [item for items in self.questions.values() for item in items]}
        # The RESOURCES section: the compute hosts' sample as the shared producer turns it into its view (`resources_view`, with the
        # session records of the same pass for the numbers that hold a GPU), and the repository names of the sessions (`session_repos`).
        _rows, index = live._session_records(self.snapshot, self.now, live._resolver(self.snapshot))
        for key, repo in live.session_repos((_rows, index)).items():
            view["session_doers"][key] = {**view["session_doers"].get(key, {}), "repo": repo}
        if self.hosts:
            view["resources"] = human_wait_state.resources_view(self.hosts, self.now, index)
        return view

    def counts(self, harness, sid):
        mine = len(self.questions.get(sid, ()))
        return (sum(len(v) for v in self.questions.values()), mine)

    def display(self, sid):
        return {"received": self.received.get(sid, []), "human_state": "none", "human": None}


def popup_width(cols, kind="text"):
    """``panel-view.cjs`` ``popupWidth``: a popup's width in a window of ``cols`` cells."""
    if cols < 28:
        return max(4, cols)
    if cols < 80:
        return cols - 4
    # 11-18: the routes popup alone stands up to 196 cells, the question popup up to 132, the other table popups up to 116.
    return (min(196, cols - 4) if kind == "route" else min(132, cols - 4) if kind == "question"
            else min(116, cols - 4) if kind == "table" else 76)


# The other harnesses' usage windows (demo values; the session's own harness takes ``World.values``).
DEMO_USAGE = {"claude": {"rl_5h": 18, "rl_7d": 40, "rl_ms": []},
              "codex": {"rl_5h": 24, "rl_7d": 41, "rl_ms": []},
              "opencode": {"rl_5h": 9, "rl_7d": 33, "rl_ms": []}}


def projection(world, harness, sid, *, columns=40, rows=46, state="working", popup_columns=None, side=False):
    """``panel_projection``'s payload for one demo session, its live readers replaced."""
    import human_wait_state
    import session_shell
    import session_tidy
    from fleet import session_tags, session_view, session_work
    from fleet.collectors import steward, usage_cache
    now = world.now

    def observe(h, pid, start, s, **_):
        return world.values(h, s), []

    def memory(cwd, limit=2):
        return [dict(row) for row in world.memory.get(os.path.basename(str(cwd or "")), [])][:limit], []

    patches = [
        mock.patch("time.time", return_value=now),
        mock.patch.object(human_wait_state, "cached_statusline",
                          lambda h, s: (world.counts(h, s), world.labels.get(h + "\0" + s, ""))),
        mock.patch.object(human_wait_state, "cached_view", lambda request=False: world.view()),
        mock.patch.object(session_tidy, "resolve_seat", lambda *a, **k: None),
        mock.patch.object(session_tidy, "prompt_display", lambda seat, h, s, **k: world.display(s)),
        mock.patch.object(session_tidy, "waiting_notice_display",
                          lambda *a, with_level=False, **k: ("", [], None) if with_level else ("", [])),
        mock.patch.object(session_tidy, "session_seat", lambda *a, **k: None),
        mock.patch.object(session_tidy, "_prompt_seq_path", lambda *a, **k: Path("/nonexistent/seq")),
        mock.patch.object(session_tags, "assigned_tag", lambda h, s: world.tag(h, s).strip("[]")),
        mock.patch.object(session_view, "observe", observe),
        mock.patch.object(steward, "role_flag", lambda h, s, cwd=None: bool(world.sessions[s].steward)),
        mock.patch.object(session_work, "recent_memory", memory),
        mock.patch.object(session_work, "observe", lambda cwd, ids=(): (session_work.WorkRows(), [])),
        mock.patch.object(session_shell, "_sent_peer_messages",
                          lambda s, limit=8: ([dict(row) for row in world.sent.get(s, [])], [])),
        mock.patch.object(session_shell, "_request_views",
                          lambda h, s, n, errors=None: (json.loads(json.dumps(world.requests.get(s, []))), [])),
        mock.patch.object(session_shell, "session_questions",
                          lambda h, s, view=None, retained=(), elsewhere=None: json.loads(json.dumps(world.questions.get(s, [])))),
        mock.patch.dict(os.environ, {"TERM": "xterm-256color"}),
        # The USAGE section reads the usage records Fleet keeps for the other two harnesses.
        mock.patch.object(usage_cache, "read", lambda name: {"payload": dict(DEMO_USAGE.get(name, {}))}),
    ]
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        os.environ.pop("COLORTERM", None)
        os.environ.pop("NO_COLOR", None)
        payload = session_shell.panel_projection(harness, sid, native_pid=81000 + len(sid),
                                                 native_start=1, columns=columns, rows=rows,
                                                 state=state, expanded=True, popup_columns=popup_columns, side=side)
    # The host puts the status button's value in the model (``host.cjs`` ``statusDoc``).
    status = world.status(sid)
    if isinstance(status, dict) and isinstance(status.get("word"), str):
        payload["panel"] = {**payload["panel"], "status_doc": {"word": status["word"], "has_doc": status.get("has_doc") is True}}
    return payload


def popup_read(world, sid, kind, *, columns, opened="", rows=30):
    """What the backend answers the host for one popup of a demo session (``_panel_popup``):
    ``routes``, Fleet's own table drawn from Fleet's view model of the same demo world, or
    ``status``, the status document the REPO row's button names (the button's value comes with the
    request, as the host sends it), its live readers replaced as in ``projection``."""
    import session_shell
    from fleet import session_status, session_tags, session_view
    session = world.sessions[sid]
    binding = {"harness": session.harness, "sid": sid, "native_pid": 81000 + len(sid), "native_start": 1}

    def observe(h, pid, start, s, **_):
        return world.values(h, s), []
    patches = [
        mock.patch("time.time", return_value=world.now),
        mock.patch.object(session_shell, "_popup_view_model", lambda h, s, k: world.view_model()),
        mock.patch.object(session_tags, "assigned_tag", lambda h, s: world.tag(h, s).strip("[]")),
        mock.patch.object(session_view, "observe", observe),
        mock.patch.dict(os.environ, {"TERM": "xterm-256color"}),
    ]
    request = {"kind": kind, "columns": columns, "open": opened, "page": False, "rows": rows}
    if kind == "status":
        status = world.status(sid) or {}
        request.update(cwd=world.status_folder(sid), routes=[],
                       status={key: status.get(key) for key in ("word", "title", "has_doc", "path", "slot")})
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        os.environ.pop("COLORTERM", None)
        os.environ.pop("NO_COLOR", None)
        session_status.clear_cache()
        result = session_shell._panel_popup(binding, request)
    if not result.get("ok"):
        raise RuntimeError(f"{kind} popup: {result}")
    return result


def unreceived_lines(world, sid):
    """The lines the MESSAGES popup reads under the messages (the backend's ``r`` action: this
    session's own peer messages not yet received). Every demo message was received, so the
    pending list is empty and the backend answers its own empty line."""
    import peer_steward_status
    import session_shell
    session = world.sessions[sid]
    binding = {"harness": session.harness, "sid": sid, "native_pid": 81000 + len(sid), "native_start": 1}
    with mock.patch.object(peer_steward_status, "_peer_status_rows", lambda s, h: ([], 0)):
        result = session_shell._block_action(binding, {"action": "r"})
    return list(result.get("lines") or ())


# Paints one or more panel frames with the real renderer and question editor; prints the rows.
# The panel is rendered as ``host.cjs`` renders it (``panelViewNow``: the editor's summary and sent
# lines, the status button's value in the model). With ``window`` set, a popup is laid out over that
# window as ``host.cjs`` ``popupNow`` lays it: the question popup while the editor is focused, or the
# table popup ``table`` names (``ROUTE`` or ``STATUS`` with the backend's rows, or ``MESSAGES``),
# with the sections that have something in them, the section's own head as its title and its keys;
# ``veil`` is the gray everything behind a popup reads in (``panel-view`` ``veil``), null where none.
_NODE = r"""
const fs=require('node:fs');
const [viewPath,blockPath]=process.argv.slice(1);
const view=require(viewPath),create=require(blockPath);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const plain=part=>({text:part.text,fg:part.foreground??null,bg:part.background??null,
  dim:part.dim===true,bold:part.bold===true,reverse:part.reverse===true});
const cellWidth=s=>[...String(s)].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const SECTIONS=['STATUS','NOW','QUESTIONS','ROUTE','MESSAGES'];
// host.cjs: tableParts, statusDoc, sectionLabel, sections, label, tableGuide, wrapGuide, tableTitle
// (the same code, over the job's model and table instead of the host's globals).
function tableParts(result){
  const styles=Array.isArray(result.styles)?result.styles:[];
  return (Array.isArray(result.rows)?result.rows:[]).map(row=>(Array.isArray(row)?row:[]).map(run=>({text:String(run?.[0]??''),...(styles[run?.[1]]||{}),
    ...(Number.isInteger(run?.[2])&&run[2]>=0&&run[2]<=400?{cells:run[2]}:{})})));
}
const statusDoc=m=>m.status_doc&&typeof m.status_doc.word==='string'?m.status_doc:null;
const sectionLabel=(m,name)=>name==='STATUS'?String(statusDoc(m)?.word||'STATUS'):name;
function sections(m,block,table){
  const list=v=>Array.isArray(v)&&v.length>0;
  const has={STATUS:!!statusDoc(m),NOW:!!m.now,QUESTIONS:!!block.summary()||!!m.wait||!!m.wait_guide,ROUTE:list(m.routes)||!!m.route_label,
    MESSAGES:list(m.messages)||list(m.requests)};
  const current=table?table.kind:'QUESTIONS';
  return SECTIONS.filter(name=>has[name]||name===current);
}
function tableGuide(m,kind,expanded=true){
  const label=(name,fallback)=>m.labels?.[name]||fallback;
  const own=kind==='ROUTE'?label('popup_route','↑↓ route · Enter open'):'';
  const fold=label(expanded?'popup_fold':'popup_unfold',expanded?'p fold panel':'p open panel');
  return [own,label('popup_keys','←→ section · PgUp/PgDn').replace('{panel}',fold)].filter(Boolean);
}
function wrapGuide(lines,inner,rows){
  return view.keyRows(lines.flatMap(line=>String(line).split(' · ')),inner,{rows,cellWidth});
}
function tableTitle(m,kind,table){
  const dim=v=>({text:v,dim:true});
  if(kind==='ROUTE'){const r=/^route (\d+)(.*)$/.exec(String(m.route_label||'').trim());return r?[{text:r[1]},dim(' ROUTE'+r[2])]:[dim('ROUTE')];}
  if(kind==='STATUS'){const word=sectionLabel(m,'STATUS'),whole=String(table?.result?.title||word);
    return whole.startsWith(word)&&whole.length>word.length?[dim(word),{text:whole.slice(word.length)}]:[dim(whole)];}
  return [dim('MESSAGES')];
}
(async()=>{
  const jobs=JSON.parse(fs.readFileSync(0,'utf8')),out=[];
  for(const job of jobs){
    const p=job.payload;
    // An answer is on its way once sent (`submitted`): the panel keeps its receipt line.
    const block=create({changed(){},answer:async()=>({state:'submitted'}),action(){},page(){},cellWidth});
    block.update(p.questions||[],p.help||'',p.block||{});
    for(const key of job.keys||[]){
      if(key==='enter')block.enter();else if(key==='leave')block.leave();else block.input(Buffer.from(key));
      await tick();await tick();
    }
    const base={...p.panel,answerable:(p.questions||[]).some(i=>i.answerable===true&&(i.request_state||'open')==='open')};
    const model=base;
    const configuration=job.configuration||{};
    const r=view.render({width:job.width,height:job.height,side:job.side,fill:job.side,model,
      native:p.native_display||{},state:job.state,sid:job.sid,harness:job.harness,frame:job.frame||0,since:job.since,
      sections:p.block,configuration,routeScroll:0,focused:block.focused,cellWidth,
      cwd:'',background:'dark',folded:job.folded===true,brief:()=>block.summary(),sent:()=>block.sent()});
    let popup=null;
    const table=job.table||null;
    if(job.window&&(block.focused||table)){
      const {cols,rows}=job.window,kind=table?(table.kind==='ROUTE'?'route':'table'):'question',width=view.popupWidth(cols,kind),inner=Math.max(0,width-4),room=view.popupRoom(rows,0);
      const names=sections(base,block,table),current=Math.max(0,names.indexOf(table?table.kind:'QUESTIONS'));
      let title,body;
      if(table){
        const all=table.kind==='MESSAGES'?[...view.messagesBody({model:base,columns:inner,cellWidth}),
          ...(table.lines||[]).slice(0,room).map(text=>[{text,dim:true}])]:tableParts(table.result||{});
        const fits=room-tableGuide(base,table.kind).length;
        const place=all.length>Math.max(1,fits)?`1/${all.length}`:'';
        const guide=wrapGuide([place,...tableGuide(base,table.kind)].filter(Boolean),inner,Math.max(1,room-1)).map(text=>[{text,dim:true}]);
        const space=Math.max(1,room-guide.length);
        title=tableTitle(base,table.kind,table);
        body=[...all.slice(0,space),...guide];
      }else{
        const label=[{text:block.summary()?.label||'QUESTIONS',dim:true}];
        title=label;
        if(block.question){const all=block.detail({budget:room+1,columns:inner});title=all[0]||label;body=all.slice(1);}
        else body=block.detail({budget:room,columns:inner});
      }
      const shown=view.popup({cols,rows,band:0,kind,title,body,sections:names,labels:names.map(name=>sectionLabel(base,name)),
        current,model:base,configuration,cellWidth,background:'dark'});
      const veil=view.veil(configuration,{colors:base.colors??256,noColor:base.no_color===true,background:'dark'});
      popup={left:shown.left,top:shown.top,rows:shown.rows.map(row=>row.map(plain)),veil};
    }
    out.push({rows:r.rows.map(row=>row.map(plain)),popup});
  }
  process.stdout.write(JSON.stringify(out));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""


def paint(jobs):
    """``{"rows", "popup"}`` for each job (``payload``, ``width``, ``height``, ``side``, ``state`` …):
    the panel's rows of parts, and the popup laid over ``window`` (``{"left", "top", "rows", "veil"}``)
    while the editor is focused or a ``table`` popup stands (else None)."""
    result = subprocess.run(["node", "-e", _NODE, str(PANEL_VIEW), str(BLOCK_INPUT)],
                            input=json.dumps(jobs), capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


# --- painting -------------------------------------------------------------------------------
# The panel's parts carry xterm-256 indices (``style_components``); they are painted on the same
# cell grid, fonts and background ``explorer_capture`` paints Fleet with, and the six Fleet hues
# take the same richer RGB Fleet's capture uses, so a value reads in the same color in both.

def _xterm(index):
    base = ((0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205),
            (0, 205, 205), (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0),
            (255, 255, 0), (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255))
    if index < 16:
        return base[index]
    if index >= 232:
        level = 8 + 10 * (index - 232)
        return (level, level, level)
    index -= 16
    steps = (0, 95, 135, 175, 215, 255)
    return (steps[index // 36], steps[(index // 6) % 6], steps[index % 6])


def _richer():
    from tools.fleet import render_cells
    opened = {index: tuple(round(v * 255 / 1000) for v in render_cells._RICHER_RGB_1000[name])
              for name, index in render_cells._MUTED_256.items() if name in render_cells._RICHER_RGB_1000}
    return opened


def _colour(value, default, richer):
    if isinstance(value, str) and value.startswith("#") and len(value) == 7:
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    if isinstance(value, int) and 0 <= value <= 255:
        return richer.get(value) or _xterm(value)
    return default


def cell_width(char):
    """The panel's own cell rule (``panel-view.cjs`` ``measure``): above U+2E7F a glyph is wide."""
    return 2 if ord(char) > 0x2E7F else 1


def cells(rows):
    """Rows of parts as rows of ``(glyph, fg, bg, dim, bold, reverse, width)`` cells."""
    out = []
    for row in rows:
        line = []
        for part in row:
            for char in part.get("text") or "":
                if ord(char) in (0xFE0F, 0x200D):
                    continue
                line.append((char, part.get("fg"), part.get("bg"), part.get("dim"), part.get("bold"),
                             part.get("reverse"), cell_width(char)))
        out.append(line)
    return out


def draw(canvas, fonts, grid, left, top):
    """Paint ``grid`` (``cells()``) with its top-left cell at pixel ``(left, top)``."""
    from tools.fleet.tests import explorer_capture as capture
    richer = _richer()
    for y, row in enumerate(grid):
        x = 0
        for glyph, fg, bg, dim, bold, reverse, width in row:
            back = _colour(bg, capture.BACKGROUND, richer)
            fore = _colour(fg, capture.DEFAULT_FG, richer)
            if dim:
                fore = capture.blend(fore, back, 0.42)
            if reverse:
                fore, back = back, fore
            px, py = left + x * capture.CELL_W, top + y * capture.CELL_H
            canvas.rectangle([px, py, px + width * capture.CELL_W - 1, py + capture.CELL_H - 1], fill=back)
            glyph = capture.GLYPH_FALLBACK.get(glyph, glyph)
            if glyph.strip():
                family = ("wide" if width == 2 else "symbols" if "⠀" <= glyph <= "⣿"
                          else "latin") + ("-bold" if bold else "")
                font = fonts.get(family) or fonts.get("latin")
                if font is not None:
                    canvas.text((px, py - 1), glyph, font=font, fill=fore)
            x += width


def fonts():
    from PIL import ImageFont
    from tools.fleet.tests import explorer_capture as capture
    return {name: ImageFont.truetype(file, capture.FONT_PX)
            for name, file in capture.FONTS.items() if Path(file).exists()}


def image(grid, cols, rows):
    """One grid as its own picture, ``cols`` × ``rows`` cells."""
    from PIL import Image, ImageDraw
    from tools.fleet.tests import explorer_capture as capture
    picture = Image.new("RGB", (cols * capture.CELL_W, rows * capture.CELL_H), capture.BACKGROUND)
    draw(ImageDraw.Draw(picture), fonts(), grid, 0, 0)
    return picture


# --- the illustrative conversation area -------------------------------------------------------
# Plain text rows beside the panel: what a person typed (``›``) and what the agent said, in the
# terminal's own colors. Not a vendor screen: no vendor prompt box, spinner or message glyphs.

CONVERSATION = {
    API: (
        [("› ", "dim"), ("Add a rate limit to the login API, test it, and leave a report.", "you")],
        [],
        [("  Before work starts, here is what I understood:", "body")],
        [("    intent     ", "dim"), ("limit login attempts per API key, with tests and a report", "body")],
        [("    scope      ", "dim"), ("src/auth/ and tests/auth/; not the other endpoints", "body")],
        [("    done when  ", "dim"), ("the limiter tests pass and the change report is written", "body")],
        [("  Proceed, Edit, or Stop?", "body")],
        [],
        [("› ", "dim"), ("Proceed", "you")],
        [],
        [("  Route ", "body"), ("code · staged", "dim"), (": plan ✓ › execute ● › test › report", "body")],
        [("  The execute worker edits web-api-wt/execute, on its own branch.", "body")],
        [],
        [("  ", "body"), ("[b2] codex", "codex"), (" finished the pagination review: 2 findings in cursor.py.", "body")],
        [("  I asked ", "body"), ("[b2]", "codex"), (" to re-run the API tests on feat/rate-limit (rq-7d21c4).", "body")],
        [("  Partial report so far: 31 of 40 suites pass, 9 still running.", "body")],
        [],
        [("› ", "dim"), ("▏", "you")],
    ),
    API_NOTES: (
        [("› ", "dim"), ("Draft the 2.4 release notes from the merged pull requests.", "you")],
        [],
        [("  I grouped 23 merged changes into features, fixes and internal work.", "body")],
        [("  Two choices before I write it; they are in the panel below.", "body")],
    ),
    ML: (
        [("› ", "dim"), ("Compare the ASR checkpoints on the 16 kHz subset.", "you")],
        [],
        [("  Route ", "body"), ("lab · staged", "dim"), (": setup ✓ › eval-asr ● › eval-sep › report", "body")],
        [("  Setup prepared 1,200 clips; eval-asr and eval-sep run as separate workers.", "body")],
        [],
        [("  ", "body"), ("[e8] opencode", "opencode"), (": 8 loader workers is the knee, 1.9x over 4.", "body")],
        [],
        [("› ", "dim"), ("▏", "you")],
    ),
}
_CONVERSATION_STYLE = {"you": {"fg": 253, "bold": True}, "body": {"fg": None}, "dim": {"dim": True},
                       "claude": {"fg": 209}, "codex": {"fg": 147}, "opencode": {"fg": 153}}


# The window's last line under the native screen is the shell's one line in every harness (spec rule 60):
# the working folder at the left, a dim ``hearting v<release>`` at the right end.
FOLDER = {API: "~/web-api", API_REVIEW: "~/web-api", API_NOTES: "~/web-api", ML: "~/ml-train"}


def last_line(sid, cols):
    from tools.fleet.tests import readme_demo
    left, right = " " + FOLDER.get(sid, "~"), "hearting " + readme_demo.VERSION
    gap = max(1, cols - len(left) - len(right) - 1)
    return [{"text": left, "dim": True}, {"text": " " * gap}, {"text": right, "dim": True}]


def conversation(sid, cols, rows, *, top=1):
    """The illustrative conversation of a demo session as panel-shaped rows of parts."""
    out = [[] for _ in range(top)]
    for line in CONVERSATION.get(sid, ()):
        out.append([{"text": (" " + text) if index == 0 else text, **_CONVERSATION_STYLE[key]}
                    for index, (text, key) in enumerate(line)])
    out = out[:rows - 1]
    return out + [[] for _ in range(rows - 1 - len(out))] + [last_line(sid, cols)]


# --- framing --------------------------------------------------------------------------------
SCREEN = (10, 12, 19)          # the backdrop the windows stand on
EDGE = (43, 46, 60)            # a window's one-pixel edge
BAR = (21, 24, 36)             # a window's title bar
BAR_TEXT = (138, 138, 154)
BAR_H = 26


def window(picture, title):
    """``picture`` inside a plain window frame whose bar names what it shows."""
    from PIL import Image, ImageDraw
    width, height = picture.size
    out = Image.new("RGB", (width + 2, height + BAR_H + 2), EDGE)
    canvas = ImageDraw.Draw(out)
    canvas.rectangle([1, 1, width, BAR_H], fill=BAR)
    for index in range(3):
        canvas.ellipse([10 + index * 15, 9, 19 + index * 15, 18], fill=(47, 51, 67))
    canvas.text((62, 4), title, font=fonts().get("latin"), fill=BAR_TEXT)
    out.paste(picture, (1, BAR_H + 1))
    return out


def screen(rows_of_windows, *, gap=12, pad=14):
    """Windows laid out in rows (each row left to right) on the backdrop."""
    from PIL import Image
    widths = [sum(w.size[0] for w in row) + gap * (len(row) - 1) for row in rows_of_windows]
    heights = [max(w.size[1] for w in row) for row in rows_of_windows]
    canvas = Image.new("RGB", (max(widths) + 2 * pad, sum(heights) + gap * (len(heights) - 1) + 2 * pad), SCREEN)
    y = pad
    for row, width, height in zip(rows_of_windows, widths, heights):
        x = pad + (max(widths) - width) // 2
        for item in row:
            canvas.paste(item, (x, y))
            x += item.size[0] + gap
        y += height + gap
    return canvas


def caption(text, cols, *, key=""):
    """One caption row under a picture: the key in bold, then what it does, dim."""
    row = [{"text": " "}]
    if key:
        row += [{"text": key, "fg": 253, "bold": True}, {"text": "  "}]
    row += [{"text": text, "dim": True}]
    return image(cells([[], row]), cols, 2)


def beside(left, right, cols_left, gap=2):
    """Two grids side by side, the left one padded to ``cols_left`` cells and a ``gap``."""
    out = []
    for index in range(max(len(left), len(right))):
        row = list(left[index]) if index < len(left) else []
        used = sum(cell[-1] for cell in row)
        row += [(" ", None, None, False, False, False, 1)] * max(0, cols_left + gap - used)
        out.append(row + (list(right[index]) if index < len(right) else []))
    return out


def overlay(grid, layer, top, left):
    """``grid`` (``cells()`` rows) with ``layer`` (``cells()`` rows) laid over it from cell
    ``(top, left)``, the way the host writes the popup over the scene's rows."""
    def columns(row):
        out = []
        for cell in row:
            out.append(cell)
            out.extend([None] * (cell[-1] - 1))
        return out
    blank = (" ", None, None, False, False, False, 1)
    out = [columns(row) for row in grid]
    for index, row in enumerate(layer):
        y = top + index
        if not 0 <= y < len(out):
            continue
        line = out[y]
        x = left
        for cell in row:
            end = x + cell[-1]
            line.extend([blank] * max(0, end - len(line)))
            # A wide glyph the layer cuts in half gives its other half back as a blank.
            if line[x] is None:
                start = x
                while start > 0 and line[start] is None:
                    start -= 1
                line[start] = blank
            if end < len(line) and line[end] is None:
                line[end] = blank
            line[x:end] = [cell] + [None] * (cell[-1] - 1)
            x = end
    return [[cell for cell in line if cell is not None] for line in out]


def veiled(grid, tone):
    """``grid`` as it reads behind a popup (``host.cjs`` ``veilRow``): every cell in the one gray
    ``tone``, no other color, no background, no weight."""
    return [[(glyph, tone, None, False, False, False, width) for glyph, _fg, _bg, _dim, _bold, _rev, width in row]
            for row in grid]
