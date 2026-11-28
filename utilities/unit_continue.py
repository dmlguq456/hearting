"""Continue a unit route's worker: one more turn of the same route and node.

A unit route (`compose --start --unit <unit> …`) runs one registered leg. After it ends and the route
closes, `capability-route.py correct --route <id> --message-file <text>` asks for another turn: a new
attempt of the same route and node that wakes the previous turn's harness session when it can and starts
a fresh one (with the original brief and the previous result) when it cannot. The turn begins its own
artifact cycle, a child of the previous turn's (`artifact_producer.begin(fresh_cycle=…)`): each turn has its
own report folder and terminal evidence. This module holds the meaning those callers share: the kept text, the chain of turns, the one
question "may the next turn start now", the session decision, the one-file session transplant and the
turn's prompt. It owns no process, no poller and no new state beyond the text store.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import NamedTuple, Optional

from dispatch_contract_roots import dispatch_state_root
from environment_inheritance import inherited_environment

STORE = "unit-continue"
TEXT_BYTES = 65536                                   # the same limit an owner's answer has
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")     # the same id shape an owner's answer has
REASONS = ("session-missing", "harness-changed", "store-unsupported", "resume-refused")


class ContinueError(ValueError):
    """A typed refusal: `str(error)` is the reason."""


# -- the kept text ------------------------------------------------------------------------------

def _store_path(jobs, route_id) -> Path:
    return dispatch_state_root(jobs) / STORE / f"{route_id}.json"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def entries(jobs, route_id) -> list:
    path = _store_path(jobs, route_id)
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("requests", [])
    except (OSError, ValueError):
        return []


def submit(jobs, route_id, text, request_id=None, *, source_session=None, unit=None) -> dict:
    """Keep `text` for the route's next turn: `{"entry": …, "duplicate": bool}`.

    `source_session` and `unit` (the `unit:<family/name>` label) are only noted on a new entry, for the sender's
    work-style record (`memory_work_labels`); they never decide anything here and a duplicate keeps its first.

    The same id with the same text is a duplicate (nothing new is kept); the same id with other text is
    refused. Without `request_id` the id is a digest of the text, so sending a file twice is one request."""
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > TEXT_BYTES:
        raise ContinueError("correction-text-empty-or-oversized")
    digest = _digest(text)
    request_id = request_id or "input-" + _digest([route_id, digest])[:32]
    if not REQUEST_ID.fullmatch(request_id):
        raise ContinueError("correction-id-invalid")
    path = _store_path(jobs, route_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if path.is_symlink():
            raise ContinueError("unit-continue-state-unsafe")
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "schema": "unit_continue_v1", "route_id": route_id, "requests": []}
        old = next((item for item in value["requests"] if item["id"] == request_id), None)
        if old is not None:
            if old["digest"] != digest:
                raise ContinueError("correction-id-content-conflict")
            return {"entry": old, "duplicate": True}
        entry = {"id": request_id, "digest": digest, "text": text, "at": round(time.time(), 1)}
        if source_session:
            entry["source_session"] = source_session
        if unit:
            entry["unit"] = unit
        value["requests"].append(entry)
        handle, name = tempfile.mkstemp(prefix=".unit-continue-", dir=path.parent)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
        os.chmod(name, 0o600)
        os.replace(name, path)
        return {"entry": entry, "duplicate": False}
    finally:
        os.close(fd)


# -- the chain of turns ---------------------------------------------------------------------------

def attempt_id_for(route_id, node, request_id) -> str:
    """The attempt of the turn a kept request starts (the first turn's id is `work_start.attempt_id`)."""
    return "att-" + hashlib.sha256(f"{route_id}:{node}:{request_id}".encode()).hexdigest()[:32]


def turn_of(meta) -> int:
    try:
        return max(1, int(meta.get("turn") or 1))
    except (TypeError, ValueError):
        return 1


OWNER = "owner"   # the node name `work_start._slot` gives a route's owner


def chain(rows, route_id, node) -> list:
    """`[(attempt_id, status, meta)]` of the route's turns on `node`, oldest first. `rows` is `work_start._rows`.

    `node` OWNER is the route's depth-1 owners in registry order: a replacement owner (an answer, an
    observation, a usage limit) belongs to the turn it took over, a later request starts the next turn."""
    if node == OWNER:
        return [(aid, status, meta) for aid, (status, meta) in rows.items()
                if meta.get("worker_type") == "owner" and str(meta.get("dispatch_depth")) == "1"
                and route_id in {meta.get("owner_route_id"), meta.get("route_id")}]
    turns = [(aid, status, meta) for aid, (status, meta) in rows.items()
             if meta.get("route_id") == route_id and meta.get("route_node") == node
             and meta.get("worker_type") != "owner"]
    return sorted(turns, key=lambda item: turn_of(item[2]))      # stable: registry order within a turn


def last_turn(turns) -> int:
    return max((turn_of(meta) for _, _, meta in turns), default=0)


def recorded_leg(meta) -> bool:
    """The recording, read-only unit route can keep its session and continue."""
    from unit_ephemeral import is_scratch_root
    return bool(meta.get("capability") == "unit" and meta.get("workflow_completion") == "runtime-v1"
                and meta.get("dispatch_depth") == "1" and meta.get("route_node") == "unit"
                and meta.get("artifact_root") and not is_scratch_root(meta["artifact_root"]))


def capacity_turn_options(source, argv) -> dict:
    """A replacement finishes the same logical turn, carrying its original request text."""
    text = "Continue the unfinished work after the usage limit. Preserve any earlier completed findings."
    for index, token in enumerate(argv[:-1]):
        if token == "--continuation-text":
            text = argv[index + 1]       # The interrupted turn's own request, even across another capacity stop.
            break
    return {"--continues-attempt-id": source["attempt_id"], "--turn": str(turn_of(source)),
            "--continuation-text": text}


def continued_by_later_turn(jobs, meta, *, launched=False) -> bool:
    """Whether a later turn of this unit route has taken over from this attempt (a row names it as `continues_attempt_id`;
    with `launched`, only a later turn whose launch started counts, as a registration alone runs nothing).

    A turn so taken over has nothing left to settle: its closure record was set aside when the next turn
    reopened the route, and the route's end (and its closure) is the last turn's. Only a unit leg's row can
    be continued, and an owner only on a route that kept a later request for it (`correct` on its closed
    route), so every other row answers False without reading the registry."""
    attempt = meta.get("attempt_id")
    route_id = (meta.get("owner_route_id") or meta.get("route_id")) if meta.get("worker_type") == "owner" else meta.get("route_id")
    if not attempt or not route_id or not jobs:
        return False
    if meta.get("capability") != "unit" and not (meta.get("worker_type") == "owner" and _store_path(jobs, route_id).exists()):
        return False
    from dispatch_registry_cache import registry_view
    return any((row := record.completion_metadata()).get("continues_attempt_id") == attempt
               and (not launched or row.get("launch_started") == "1")
               for record in registry_view(Path(jobs)).route_alias_candidates(route_id))


def superseded_count(path) -> int:
    """How many closure records of this route were set aside (each reopening sets one aside)."""
    route_file = Path(path)
    return len(list(route_file.parent.glob(f"{route_file.stem}.superseded-*.outcome.json")))


class Turn(NamedTuple):
    attempt_id: str
    prev_attempt_id: str
    turn: int
    text: str
    request_id: str


def next_turn(jobs, route, node, rows) -> Optional[Turn]:
    """The oldest kept request no turn has started yet, as the turn it would start (None when none)."""
    turns = chain(rows, route["route_id"], node)
    if not turns:
        return None
    for entry in entries(jobs, route["route_id"]):
        aid = attempt_id_for(route["route_id"], node, entry["id"])
        if aid not in rows:
            return Turn(aid, turns[-1][0], last_turn(turns) + 1, entry["text"], entry["id"])
    return None


def waiting_request(jobs, meta) -> bool:
    """Whether a request kept for this unit leg's route has no turn yet (the end notice then names `start`)."""
    from dispatch_completion_join import exact_attempt_row
    route_id, node = meta.get("route_id"), meta.get("route_node")
    if not route_id or not node:
        return False
    for entry in entries(jobs, route_id):
        try:
            exact_attempt_row(Path(jobs), attempt_id_for(route_id, node, entry["id"]))
        except Exception:  # noqa: BLE001 -- no row for it: it still waits
            return True
    return False


def notice_lines(jobs, meta, agent_home=None) -> tuple:
    """`(head, note)` an end notice adds for a unit leg: for a continued turn, which turn it is and its session, and
    for a turn a started later turn took over, that it is not the latest result (`head`); for a route with a request
    still kept, the `start` that runs it (`note`). Either may be ''."""
    if meta.get("capability") != "unit":
        if meta.get("worker_type") == "owner" and meta.get("session"):
            # An owner that took over from an earlier owner of its route says which session it ran in.
            if meta.get("continues_attempt_id"):
                return f"Turn {meta.get('turn') or '?'} of this route (session: {meta['session']}).", ""
            return (f"This owner took over from {meta.get('automatic_retry_of') or 'an earlier owner'} "
                    f"(session: {meta['session']}).", "")
        return "", ""
    head = ""
    if meta.get("continues_attempt_id"):
        head = f"Turn {meta.get('turn') or '?'} of this unit route (session: {meta.get('session') or 'unknown'})."
    if jobs and continued_by_later_turn(jobs, meta, launched=True):
        # A late notice of a turn a later, started turn took over: its result is not the route's latest.
        head = (f"{head} A later turn continued it" if head else
                "An earlier turn of this unit route; a later turn continued it") + ", so this is not its latest result."
    note = ""
    if jobs and meta.get("route_file") and waiting_request(jobs, meta):
        from parent_next_directive import resume_command
        note = ("A request kept for this unit route is waiting for its next turn; run "
                + resume_command(Path(meta["route_file"]), jobs, agent_home=agent_home or Path(__file__).resolve().parents[1])
                + " to start it.")
    return head, note


def ready_to_continue(jobs, route, path, node, rows) -> Optional[Turn]:
    """The next turn when it may start now, else None: the one judgment `_slot` and `_advance` share.

    It may start when a kept request has no turn yet, the last turn ended, and the route is either closed
    (closure recorded, completion settled, not closed by autoclose) or already reopened for it (a set-aside
    closure record is newer than the last turn's end: the count of them has caught up with the turns).
    A running turn, a turn that ended but whose route is not yet closed, and no request are all None."""
    turn = next_turn(jobs, route, node, rows)
    if turn is None:
        return None
    turns = chain(rows, route["route_id"], node)
    last_id, last_status, last_meta = turns[-1]
    if last_status != "done":
        return None
    from dispatch_notice_state import closed_outcome
    closed = closed_outcome(Path(path), route)
    if closed:
        if closed.get("autoclose") or closed.get("finish_pending"):
            return None
        if node == OWNER and closed.get("terminal_gate_proven") is not True:
            return None
        from dispatch_terminal_commit import owner_completion_state
        # An owner's route takes a later request only after a PASS it settled; a unit route also after a FAIL.
        settled = {"complete"} if node == OWNER else {"complete", "not-applicable"}
        if owner_completion_state(jobs, last_status, last_meta).state not in settled:
            return None
        return turn
    return turn if superseded_count(path) >= last_turn(turns) else None


def reopen(path) -> Optional[Path]:
    """Set the closure record aside so the route can start its next turn (never deletes it); the set-aside path."""
    from capability_route_close import retire_stale_closure
    return retire_stale_closure(Path(path))


def restore_closure(path, set_aside) -> bool:
    """Put a closure `reopen` set aside back when the next turn did not start; never over a newer closure."""
    from capability_route_close import outcome_path
    try:
        os.link(set_aside, outcome_path(Path(path)))
    except OSError:
        return False        # the route was closed again meanwhile, or the set-aside record is gone: leave both
    with contextlib.suppress(OSError):
        os.unlink(set_aside)
    return True


# -- the session ------------------------------------------------------------------------------------

class SessionPlan(NamedTuple):
    kind: str                       # "resumed" | "fresh"
    reason: str                     # "" when resumed, else one of REASONS
    session_id: str
    source: str                     # the file to carry to the new home ("" when nothing moves)
    relative: str = ""              # that file's place under a worker home

    @property
    def row_value(self) -> str:
        return "resumed" if self.kind == "resumed" else f"fresh:{self.reason}"


def _log_text(meta) -> str:
    try:
        return Path(meta.get("log_file") or meta.get("log") or "").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# The session a supervised owner announces before its first turn (Claude and OpenCode supervisors).
SUPERVISOR_SESSION = re.compile(r'"type"\s*:\s*"dispatch\.supervisor\.session"[^\n]*?"session_id"\s*:\s*"([^"]+)"')


def session_id_of(meta) -> str:
    """The harness session a finished turn ran in ('' when it cannot be read)."""
    if meta.get("session_id"):
        return meta["session_id"]
    text = _log_text(meta)
    announced = SUPERVISOR_SESSION.search(text) if meta.get("worker_type") == "owner" else None
    if announced:
        return announced.group(1)
    harness = meta.get("harness")
    pattern = {"codex": r'"thread_id"\s*:\s*"([^"]+)"', "opencode": r'"sessionID"\s*:\s*"([^"]+)"',
               "claude": r'"session_id"\s*:\s*"([^"]+)"'}.get(harness)
    found = re.search(pattern, text) if pattern else None
    return found.group(1) if found else ""


def session_file(meta, session_id) -> Optional[Path]:
    """The one file that holds a turn's session in its worker home (Claude, Codex); None when absent."""
    home = meta.get("runtime_home") or ""
    if not session_id or not home or not os.path.isabs(home):
        return None
    root = Path(home)
    pattern = {"claude": f"projects/*/{session_id}.jsonl",
               "codex": f"sessions/*/*/*/rollout-*{session_id}.jsonl"}.get(meta.get("harness"))
    if pattern is None:
        return None
    return next((p for p in sorted(root.glob(pattern)) if p.is_file()), None)


def opencode_session_exists(session_id) -> bool:
    """OpenCode keeps sessions in one shared store; `session export` answers whether the id is in it."""
    try:
        done = subprocess.run(["opencode", "session", "export", session_id], capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=30, check=False, env=inherited_environment())
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def ended_without_result(meta) -> bool:
    """Whether the runtime, not the model, ended this attempt with no result: its supervisor failed, its process
    was killed or went silent (`route_authority`'s `runtime` and `silent` ends). A usage limit is not one."""
    from dispatch_replacement import DEATH_NOTES
    from dispatch_supervisor_terminal import runtime_ended_without_result
    return runtime_ended_without_result(meta) or meta.get("note") in DEATH_NOTES


def session_plan(jobs, prev_attempt_id, harness, *, exists=None) -> SessionPlan:
    """Whether the turn after `prev_attempt_id` wakes its session on `harness`, or starts a fresh one.

    A session the runtime ended without a result (killed mid-tool, a supervisor error, a resume that failed)
    is not woken: the next attempt starts fresh, so one broken session is never retried in the same lineage."""
    exists = exists or opencode_session_exists
    from dispatch_completion_join import exact_attempt_row
    try:
        meta = exact_attempt_row(Path(jobs), prev_attempt_id).metadata
    except Exception:  # noqa: BLE001 -- an unreadable previous row is a missing session
        return SessionPlan("fresh", "session-missing", "", "")
    if meta.get("harness") != harness:
        return SessionPlan("fresh", "harness-changed", "", "")
    if ended_without_result(meta):
        return SessionPlan("fresh", "resume-refused", "", "")
    session_id = session_id_of(meta)
    if harness == "opencode":
        from opencode_runtime import is_v2
        if not is_v2():
            return SessionPlan("fresh", "store-unsupported", "", "")
        if session_id and exists(session_id):
            return SessionPlan("resumed", "", session_id, "")
        return SessionPlan("fresh", "session-missing", "", "")
    source = session_file(meta, session_id)
    if source is None:
        return SessionPlan("fresh", "session-missing", "", "")
    return SessionPlan("resumed", "", session_id, str(source), str(source.relative_to(meta["runtime_home"])))


def plan_for_args(args, jobs, harness) -> Optional[SessionPlan]:
    """The launch's session plan (None for a first turn), decided once and kept on `args`."""
    prev = getattr(args, "continues_attempt_id", None)
    if not prev:
        return None
    if getattr(args, "session_plan", None) is None:
        args.session_plan = session_plan(jobs, prev, harness)
    return args.session_plan


def owner_predecessor(args) -> str:
    """The earlier owner of the same route this owner launch takes over from: the one it replaces
    (`--automatic-retry-of`: an answer, an observation, a usage limit) or the turn it follows
    (`--continues-attempt-id`: a later request on a closed route). '' for a route's first owner."""
    if getattr(args, "worker_type", None) != "owner" or str(getattr(args, "dispatch_depth", "")) != "1":
        return ""
    return getattr(args, "automatic_retry_of", None) or getattr(args, "continues_attempt_id", None) or ""


def owner_plan_for_args(args, jobs, harness, *, keeps_session=True) -> Optional[SessionPlan]:
    """The same session decision a unit turn uses, for an owner that takes over from an earlier owner.

    `keeps_session` False is a launch whose transport cannot wake a session (no supervisor): it starts fresh."""
    prev = owner_predecessor(args)
    if not prev:
        return None
    if getattr(args, "session_plan", None) is None:
        plan = session_plan(jobs, prev, harness)
        if plan.kind == "resumed" and not keeps_session:
            plan = SessionPlan("fresh", "resume-refused", "", "")
        args.session_plan = plan
    return args.session_plan


def transplant(plan, new_home) -> None:
    """Carry the one session file (and Claude's same-named folder) into the new worker home; never move the source."""
    if plan is None or plan.kind != "resumed" or not plan.source:
        return
    source = Path(plan.source)
    destination = Path(new_home) / plan.relative
    if destination.is_file() and os.path.samefile(source, destination):
        return      # the new attempt runs in the same home (a networked Codex owner's home is its worktree's): nothing moves
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, destination)
        sibling = source.with_suffix("")
        if sibling.is_dir():
            shutil.copytree(sibling, destination.with_suffix(""), symlinks=True, dirs_exist_ok=True)
        if not destination.is_file() or destination.stat().st_size != source.stat().st_size:
            raise ContinueError("session-transplant-incomplete")
    except (OSError, ContinueError):
        # A partial copy would hold the session id a fresh start of this launch then uses.
        with contextlib.suppress(OSError):
            destination.unlink()
        raise


# -- the turn's prompt ------------------------------------------------------------------------------

def previous_result(jobs, prev_attempt_id, *, after_capacity=False) -> str:
    """The previous turn's sealed summary, crossing an interrupted turn if necessary."""
    from dispatch_completion_join import exact_attempt_row
    try:
        import work_intent
        meta = exact_attempt_row(Path(jobs), prev_attempt_id).metadata
        scope = ("route_id", "route_hash", "route_node")
        seen = set()
        while meta["attempt_id"] not in seen:
            aid = meta["attempt_id"]
            seen.add(aid)
            summary = work_intent.sealed_summary(meta, aid)
            if summary or not after_capacity or not meta.get("continues_attempt_id"):
                return summary
            prior = exact_attempt_row(Path(jobs), meta["continues_attempt_id"]).metadata
            if any(meta.get(key) != prior.get(key) for key in scope):
                break
            meta = prior
    except Exception:  # noqa: BLE001 -- no prior summary is a valid handoff
        pass
    return ""


def turn_prompt(args, plan, metadata_block, brief, jobs) -> str:
    """What the continued turn is told. A woken session already holds the brief and the kernel's rules:
    it gets this turn's dispatch facts and text. A capacity retry finishes the existing report.
    A fresh session gets the original brief and the previous result with the text."""
    number = int(getattr(args, "turn", 0) or 2)
    text = getattr(args, "continuation_text", "") or ""
    after_capacity = bool(getattr(args, "automatic_retry_of", None))
    previous = previous_result(jobs, args.continues_attempt_id, after_capacity=after_capacity)
    carried = f"Previous turn's result (turn {number - 1}):\n{previous}\n\n" if previous else ""
    if after_capacity:
        opening = "The worker stopped at a usage limit. Resume the interrupted turn"
        report = ("Finish the interrupted turn's report under artifact_output_dir. "
                  "If that file already exists, continue from what is there. Keep earlier findings.")
        request = f"{carried}Request for the interrupted turn:\n{text.rstrip()}\n"
        if plan is not None and plan.kind == "resumed":
            return (f"{opening} in this session within the same scope. {report} End as before.\n\n"
                    f"{metadata_block}\n{request}")
        reason = plan.reason if plan else "session-missing"
        return (f"{brief.rstrip()}\n\n{opening} in a fresh session ({reason}) within the same scope. "
                f"{report}\n\n{request}")
    if plan is not None and plan.kind == "resumed":
        return (f"This is turn {number} of the same assignment, in the same session. The dispatch facts below "
                f"are this turn's: write this turn's report under its artifact_output_dir, and keep the "
                f"earlier report as it is. End as before.\n\n{metadata_block}\n{carried}"
                f"Request for this turn:\n{text.rstrip()}\n")
    return (f"{brief.rstrip()}\n\nThis is turn {number} of this assignment; the earlier turn's session could "
            f"not be woken ({plan.reason if plan else 'session-missing'}), so you start fresh. Write this "
            f"turn's report under your artifact_output_dir and leave the earlier report as it is.\n\n"
            f"{carried}Request for this turn:\n{text.rstrip()}\n")


OWNER_SCOPE = ("This launch applies its own permissions and write scope; nothing the earlier attempt was granted "
               "carries over. Stay within the approved scope of the original assignment.")


def owner_turn_prompt(args, plan, metadata_block, prompt, recovery, jobs) -> str:
    """What an owner that takes over from an earlier owner of the same route is told (`owner_predecessor`).

    A woken session already holds the assignment, the kernel and the route steps: it gets this attempt's
    dispatch facts (with this launch's commit policy and the node's declared scope, `launch_policy_prompt`)
    and only what is new, the recovery context of a replacement (an answer, the runtime's
    observation, a usage limit) or the request of a later turn. A fresh session gets the whole prompt as
    before, the same new text and why it starts fresh."""
    reason = plan.reason if plan is not None and plan.kind != "resumed" else "session-missing"
    resumed = plan is not None and plan.kind == "resumed"
    if getattr(args, "continues_attempt_id", None) and not getattr(args, "automatic_retry_of", None):
        number = int(getattr(args, "turn", 0) or 2)
        previous = previous_result(jobs, args.continues_attempt_id)
        carried = f"Previous turn's result (turn {number - 1}):\n{previous}\n\n" if previous else ""
        request = (f"{carried}Request for this turn, from the session that opened this work. It is a follow-up "
                   f"within the same scope, not an approval of work beyond it:\n"
                   f"{(getattr(args, 'continuation_text', '') or '').rstrip()}\n")
        kept = "The earlier turn ended PASS; its report, review records and closure stay as they are."
        if resumed:
            return (f"This is turn {number} of the same assignment on the same route, in the same session. {kept} "
                    f"{OWNER_SCOPE} The dispatch facts below are this turn's: write this turn's report under its "
                    f"artifact_output_dir. End with the final handoff as before.\n\n{metadata_block}\n{request}")
        return (f"{prompt.rstrip()}\n\nThis is turn {number} of this assignment. {kept} Its session could not be "
                f"woken ({reason}), so you start fresh; write this turn's report under your artifact_output_dir."
                f"\n\n{request}")
    if resumed:
        return (f"This continues the earlier owner's work on the same route, in its own session, as a new attempt. "
                f"{OWNER_SCOPE} The dispatch facts below are this attempt's. Read the context that follows, then "
                f"continue; end with the final handoff as before.\n\n{metadata_block}{recovery}")
    number, asked = taken_over_turn(jobs, args.automatic_retry_of)
    later = (f"The earlier owner was turn {number} of this route, a later request after the first turn ended PASS; "
             f"that turn's request:\n{asked.rstrip()}\n") if asked else ""
    return (f"{prompt}{recovery}{later}\nThe earlier owner's session could not be woken ({reason}), so this attempt "
            "starts fresh from the assignment and the recovery context above.\n")


def taken_over_turn(jobs, attempt) -> tuple:
    """`(turn, request text)` of the later owner turn `attempt` belongs to, through its replacements;
    `(1, "")` for a route's first turn or an unreadable row."""
    from dispatch_completion_join import exact_attempt_row
    seen = set()
    try:
        while attempt and attempt not in seen and len(seen) < 16:
            seen.add(attempt)
            meta = exact_attempt_row(Path(jobs), attempt).metadata
            if meta.get("continues_attempt_id"):
                route_id = meta.get("owner_route_id") or meta.get("route_id")
                text = next((entry["text"] for entry in entries(jobs, route_id)
                             if attempt_id_for(route_id, OWNER, entry["id"]) == attempt), "")
                return turn_of(meta), text
            attempt = meta.get("automatic_retry_of") or meta.get("replacement_original_attempt_id")
    except Exception:  # noqa: BLE001 -- an unreadable lineage carries no later request
        pass
    return 1, ""


# -- how long a finished turn's worker home is kept ---------------------------------------------------

def keep_window(meta, rows, default, main_state="unknown") -> float:
    """Seconds a finished worker home waits before it may be removed (`default` for every home but one kind).

    The one kind is the last turn of a unit route that is still open to another turn: the home holds the
    session the next turn wakes. A turn a later turn already continued, and a route the runtime closed
    after it sat unused, keep the ordinary window. A leg a worker started is kept while that worker's row
    is live; a leg the main session started while that session lives (`main_state` live), for the
    ordinary window once it is gone (dead), and for the route's own unused-route window when it cannot
    be told (unknown). Live is capped at that same unused-route window, so a session left open for days
    does not keep every home it called. The two numbers are `route_autoclose`'s, not new ones."""
    if meta.get("capability") != "unit" or meta.get("workflow_completion") != "runtime-v1":
        return default
    attempt = meta.get("attempt_id")
    if any(other.meta.get("continues_attempt_id") == attempt for other in rows.values()):
        return default
    route_file = meta.get("route_file")
    try:
        closed = json.loads(Path(route_file).with_name(Path(route_file).stem + ".outcome.json").read_text())
    except (OSError, ValueError, TypeError):
        closed = None
    if isinstance(closed, dict) and closed.get("autoclose"):
        return default
    import route_autoclose
    caller = rows.get(meta.get("caller_attempt_id") or "")
    if meta.get("caller_attempt_id"):
        return route_autoclose.IDLE_SECONDS if caller is not None and caller.status in {"open", "running"} else default
    if main_state == "live":
        return route_autoclose.IDLE_SECONDS
    return route_autoclose.IDLE_SECONDS if main_state == "unknown" else route_autoclose.QUIET_SECONDS
