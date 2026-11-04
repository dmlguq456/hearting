#!/usr/bin/env python3
"""Closed apply for the session-tidy worker: ``mem tidy-apply`` and ``mem tidy-undo``.

The tidy worker only proposes ``actions_v1.json``; this module is the one writer.
It never deletes: the closed set is ``add`` · ``supersede`` · ``reinforce`` and
anything else is skipped with a reason.  It calls the existing public functions of
``mem`` (``write_record`` / ``supersede`` / ``reinforce`` / ``activate``) one after
another under one global lock, writing each step into the batch's undo journal
*before* it acts.

    mem tidy-apply <actions.json> [--input <input_v1.json>] [--cwd DIR]
    mem tidy-undo  <batch-id>

``tidy-apply`` prints one last line (summary + how to undo) and writes the machine
result to ``<state>/session-tidy/runs/<batch>/result.json`` next to ``undo.json``.

actions_v1.json::

    {"schema_version": 1, "batch_id": "...", "input_digest": "<sha256 of input file>",
     "actions": [
       {"kind": "add", "type": "lesson", "tier": "durable", "body": "...",
        "source_key": "...", "new_ref": "r1", "duplicate_group": "g1", "evidence": "..."},
       {"kind": "supersede", "old_id": "<id>", "body": "..." | "new_ref": "r1"},
       {"kind": "reinforce", "target_id": "<id>"}]}

Order of work: the user's own choices (``input.user_choices`` plus the waiting
folder ``decisions/pending``) first, each as one ``decision`` record unless its
source exists; then the model's actions, which share what is left of the ten new
records per batch.  A batch of choices alone may exceed ten: all are written and
the summary says so.  Undo of a new record takes it out of use by superseding it
with one ``tidy-undo`` marker record (the existing public functions cannot make a
record inactive without a successor); undo of a supersede does that and then
``activate``s the old record; undo of a reinforce restores strength and
last-access.  If anything touched the records after the batch, undo refuses and
says which.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from typing import Optional

HERE = Path(__file__).resolve().parent
UTILITIES = HERE.parents[1] / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

import session_tidy as st  # noqa: E402
import tidy_decisions as td  # noqa: E402

SCHEMA = 1
ACTOR = td.DECISION_ACTOR
MAX_NEW = 10
MAX_GROUPS = 5
MODEL_BODY_MAX = 1500
SOURCE_MAX = 200
ALLOWED_KINDS = ("add", "supersede", "reinforce")
FORBIDDEN_KINDS = ("delete", "prune", "merge", "graduate", "reattribute", "restore", "activate", "force")
FORBIDDEN_TYPES = ("profile", "handoff")
TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
APPLY_LOCK_WAIT_SEC = 1800.0


class ApplyError(Exception):
    """A usage or state problem reported on one stderr line (exit 2)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _quiet(fn, *args, **kwargs):
    """Call a public ``mem`` function that prints; return (result, printed text)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        value = fn(*args, **kwargs)
    return value, out.getvalue().strip()


def run_dir(batch_id: str) -> Path:
    return st.state_root() / "runs" / batch_id


def _read_actions(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApplyError(f"actions file unreadable: {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ApplyError("actions file must be a JSON object")
    return doc


def _digest_of_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Database facts (read only)
# ---------------------------------------------------------------------------

def _counts(mem) -> dict:
    con = mem.get_con()
    try:
        records = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        try:
            tomb = con.execute("SELECT COUNT(*) FROM sync_transactional_graveyard").fetchone()[0]
        except Exception:  # noqa: BLE001 - table absent on an old schema
            tomb = 0
    finally:
        con.close()
    lines = 0
    with contextlib.suppress(OSError):
        with open(mem.GRAVEYARD, "rb") as handle:
            lines = sum(1 for _ in handle)
    return {"records": records, "graveyard": lines + tomb}


def _row(con, rid: str):
    return con.execute(
        "SELECT id, tier, scope, type, cwd_origin, status, delivery_state, injection_flag, "
        "expires, strength, last_accessed, superseded_by, canonical_id, body "
        "FROM records WHERE id=?", (rid,)).fetchone()


def _target_problem(mem, con, rid: str, pkey: str) -> str:
    """Why ``rid`` may not be superseded/reinforced by tidy, else ''.

    Same fence as ``curate-snapshot``'s allowlist: this project's active durable or
    working records that are not pending, not flagged and not expired; profile,
    global and other-project records never qualify.
    """
    row = _row(con, rid) if isinstance(rid, str) and rid else None
    if row is None:
        return "nonexistent"
    (_id, tier, scope, rtype, cwd_origin, status, delivery, flagged, expires,
     _strength, _last, _sup, _canon, _body) = row
    if status != "active":
        return "inactive"
    if rtype == "profile":
        return "profile-protected"
    if delivery == "pending":
        return "pending-protected"
    if scope == "global":
        return "global-protected"
    if cwd_origin != pkey:
        return "other-project"
    if flagged:
        return "flagged"
    if tier == "working" and expires and expires < mem.today():
        return "expired"
    return ""


def _digest(con, rid: str) -> Optional[dict]:
    row = _row(con, rid)
    if row is None:
        return None
    return {"id": rid, "status": row[5], "strength": row[9] or 1, "superseded_by": row[11],
            "body_sha": _sha(row[13] or "")}


# ---------------------------------------------------------------------------
# Undo journal
# ---------------------------------------------------------------------------

class Journal:
    """``runs/<batch>/undo.json``: every step is written before it is taken."""

    def __init__(self, directory: Path, batch_id: str, pkey: str, cwd: str):
        self.path = directory / "undo.json"
        self.doc = {"schema_version": SCHEMA, "batch_id": batch_id, "project_key": pkey,
                    "cwd": cwd, "created": _now_iso(), "ops": [], "undone": None}

    @classmethod
    def load(cls, directory: Path) -> Optional["Journal"]:
        doc = st.read_json(directory / "undo.json")
        if not isinstance(doc, dict) or not isinstance(doc.get("ops"), list):
            return None
        journal = cls(directory, str(doc.get("batch_id")), str(doc.get("project_key")), str(doc.get("cwd")))
        journal.doc = doc
        return journal

    def flush(self) -> None:
        st.atomic_write_json(self.path, self.doc)

    def intent(self, **op) -> dict:
        op["seq"] = len(self.doc["ops"])
        op["state"] = "intent"
        self.doc["ops"].append(op)
        self.flush()
        return op

    def done(self, op: dict, **fields) -> None:
        op.update(fields)
        op["state"] = "done"
        self.flush()

    def drop(self, op: dict) -> None:
        op["state"] = "dropped"
        self.flush()


# ---------------------------------------------------------------------------
# tidy-apply
# ---------------------------------------------------------------------------

def _choice_payloads(input_doc: Optional[dict], cwd: str) -> list:
    out = []
    for entry in (input_doc or {}).get("user_choices") or []:
        if not isinstance(entry, dict):
            continue
        question, answers = entry.get("question"), entry.get("answers")
        if not td._text(question) or not answers:
            continue
        origin = {"kind": "transcript", "harness": entry.get("harness") or "",
                  "session": entry.get("session") or entry.get("sid") or entry.get("call_id") or ""}
        out.append(td.make_payload(question, answers, options=entry.get("options"),
                                   note=entry.get("note") or "", origin=origin, cwd=cwd,
                                   asked_at=td._text(entry.get("asked_at"))))
    return out


def _validate_static(index: int, action, seen_sources: set, groups: list) -> Optional[str]:
    """Reason to reject an action before touching the store, else None."""
    if not isinstance(action, dict):
        return "not-an-object"
    kind = action.get("kind")
    if kind in FORBIDDEN_KINDS:
        return f"forbidden-kind:{kind}"
    if kind not in ALLOWED_KINDS:
        return f"unknown-kind:{kind}"
    group = action.get("duplicate_group")
    if group not in (None, ""):
        group = str(group)[:64]
        if group not in groups:
            if len(groups) >= MAX_GROUPS:
                return "duplicate-group-limit"
            groups.append(group)
    source = action.get("source_key")
    if source not in (None, ""):
        if not isinstance(source, str) or len(source) > SOURCE_MAX:
            return "source-invalid"
        if source.startswith(td.KEY_PREFIX):
            return "source-reserved"
        if source in seen_sources:
            return "duplicate-source"
        seen_sources.add(source)
    body = action.get("body")
    if kind in ("add", "supersede") and body not in (None, ""):
        if not isinstance(body, str) or not body.strip():
            return "body-invalid"
        if len(body) > MODEL_BODY_MAX:
            return "body-too-large"
    if kind == "add":
        if not isinstance(body, str) or not body.strip():
            return "body-missing"
        if action.get("scope", "project") != "project":
            return "scope-not-project"
        if action.get("tier", "working") not in ("working", "durable"):
            return "tier-invalid"
        rtype = action.get("type", "lesson")
        if not isinstance(rtype, str) or not TYPE_RE.match(rtype) or rtype in FORBIDDEN_TYPES:
            return "type-invalid"
    elif kind == "supersede":
        if not isinstance(action.get("old_id"), str) or not action["old_id"]:
            return "old-id-missing"
        has_body = isinstance(body, str) and bool(body.strip())
        has_ref = isinstance(action.get("new_ref"), str) and bool(action["new_ref"])
        if has_body == has_ref:
            return "supersede-needs-body-xor-new-ref"
        if action.get("scope", "project") != "project":
            return "scope-not-project"
        rtype = action.get("type")
        if rtype is not None and (not isinstance(rtype, str) or not TYPE_RE.match(rtype)
                                  or rtype in FORBIDDEN_TYPES):
            return "type-invalid"
        if action.get("tier") not in (None, "working", "durable"):
            return "tier-invalid"
    elif kind == "reinforce":
        if not isinstance(action.get("target_id"), str) or not action["target_id"]:
            return "target-id-missing"
    return None


class Batch:
    """One apply run: state, counters and the result document."""

    def __init__(self, mem, batch_id: str, cwd: str, pkey: str, directory: Path):
        self.mem, self.batch_id, self.cwd, self.pkey, self.dir = mem, batch_id, cwd, pkey, directory
        self.journal = Journal(directory, batch_id, pkey, cwd)
        self.result = {
            "schema_version": SCHEMA, "batch_id": batch_id, "cwd": cwd, "project_key": pkey,
            "status": "applied", "started": _now_iso(), "finished": None,
            "added": [], "superseded": [], "reinforced": [], "choice_added": [],
            "skipped": [], "rejected": [], "discarded_over_budget": 0,
            "choice_over_cap": 0, "before": {}, "after": {}, "failure": None,
        }

    # -- choice records --------------------------------------------------

    def write_choice(self, payload: dict, pending_path: Optional[Path]) -> None:
        mem = self.mem
        found = td.existing_decision(mem, payload, self.pkey)
        if found:
            self.result["skipped"].append({"kind": "choice", "reason": "source-exists", "id": found})
            if pending_path:
                td.discard_pending(pending_path)
            return
        op = self.journal.intent(op="add", source=payload["source"], tier=td.DECISION_TIER,
                                 type=td.DECISION_TYPE, via="choice")
        status, rid = td.write_decision(mem, payload, self.pkey)
        if status != "written":
            self.journal.drop(op)
            self.result["skipped"].append({"kind": "choice", "reason": "duplicate" if status == "skipped"
                                           else "quality", "id": rid})
        else:
            con = mem.get_con()
            try:
                self.journal.done(op, id=rid, after=_digest(con, rid))
            finally:
                con.close()
            self.result["choice_added"].append(rid)
        if pending_path:
            td.discard_pending(pending_path)

    # -- model actions ----------------------------------------------------

    def _new_record(self, action: dict, tier: str, rtype: str, source: str, label: str):
        mem = self.mem
        con = mem.get_con()
        try:
            if mem.find_by_source(tier, "project", rtype, source, self.pkey, con):
                return None, "source-exists"
            body = action["body"]
            if mem.find_dup(tier, "project", mem.sanitize(body)[0], self.pkey, con=con):
                return None, "duplicate"
        finally:
            con.close()
        op = self.journal.intent(op="add", source=source, tier=tier, type=rtype, via=label)
        rid = mem.write_record(
            tier, "project", rtype, body, cwd_origin=self.pkey, source=source, quiet=True,
            journal_action="tidy-" + label, journal_insert_only=True, journal_actor=ACTOR,
            journal_cwd=self.cwd, headline=action.get("headline") if isinstance(action.get("headline"), str) else None)
        if not rid:
            self.journal.drop(op)
            return None, "quality"
        con = mem.get_con()
        try:
            self.journal.done(op, id=rid, after=_digest(con, rid))
        finally:
            con.close()
        return (rid, op), ""

    def apply_actions(self, actions: list, budget: int, block_reason: Optional[str]) -> None:
        mem = self.mem
        seen_sources: set = set()
        groups: list = []
        refs: dict = {}
        remaining = budget
        old_used: set = set()
        for index, action in enumerate(actions):
            kind = action.get("kind") if isinstance(action, dict) else None
            reason = block_reason or _validate_static(index, action, seen_sources, groups)
            if reason:
                self.result["rejected"].append({"index": index, "kind": kind, "reason": reason})
                continue
            con = mem.get_con()
            try:
                problem = ""
                if kind == "supersede":
                    problem = ("old-used-in-batch" if action["old_id"] in old_used
                               else _target_problem(mem, con, action["old_id"], self.pkey))
                elif kind == "reinforce":
                    problem = ("target-used-in-batch" if action["target_id"] in old_used
                               else _target_problem(mem, con, action["target_id"], self.pkey))
                if problem:
                    self.result["rejected"].append({"index": index, "kind": kind, "reason": problem})
                    continue
                old_row = _row(con, action["old_id"]) if kind == "supersede" else None
            finally:
                con.close()
            if kind == "reinforce":
                self._reinforce(index, action["target_id"])
                old_used.add(action["target_id"])
                continue
            if kind == "supersede" and action.get("new_ref"):
                new_id = refs.get(action["new_ref"])
                if not new_id:
                    self.result["rejected"].append({"index": index, "kind": kind,
                                                    "reason": "new-ref-not-created"})
                    continue
                self._supersede(index, action["old_id"], new_id)
                old_used.add(action["old_id"])
                continue
            # add, or supersede carrying its own body: both create one new record.
            if remaining <= 0:
                self.result["discarded_over_budget"] += 1
                continue
            if kind == "add":
                tier, rtype = action.get("tier", "working"), action.get("type", "lesson")
            else:
                tier = action.get("tier") or old_row[1]
                rtype = action.get("type") or old_row[3]
                if rtype in FORBIDDEN_TYPES:
                    self.result["rejected"].append({"index": index, "kind": kind, "reason": "type-invalid"})
                    continue
            source = action.get("source_key") or "tidy-add:" + _sha(" ".join(action["body"].split()).lower())[:24]
            made, why = self._new_record(action, tier, rtype, source, kind)
            if made is None:
                self.result["skipped"].append({"kind": kind, "reason": why, "index": index})
                continue
            rid, _op = made
            remaining -= 1
            if kind == "add":
                self.result["added"].append(rid)
                if action.get("new_ref"):
                    refs[str(action["new_ref"])] = rid
            else:
                self._supersede(index, action["old_id"], rid, created=True)
                old_used.add(action["old_id"])

    def _supersede(self, index: int, old_id: str, new_id: str, created: bool = False) -> None:
        mem = self.mem
        con = mem.get_con()
        try:
            before = _digest(con, old_id)
        finally:
            con.close()
        op = self.journal.intent(op="supersede", old_id=old_id, new_id=new_id, before=before)
        ok, text = _quiet(mem.supersede, old_id, new_id)
        if not ok:
            self.journal.drop(op)
            self.result["skipped"].append({"kind": "supersede", "reason": "supersede-refused: " + text[:120],
                                           "index": index})
            if created:
                # The new record was only made for this supersede; do not leave it in use.
                self._retire_created(new_id)
            return
        con = mem.get_con()
        try:
            self.journal.done(op, after={"old": _digest(con, old_id), "new": _digest(con, new_id)})
        finally:
            con.close()
        if created and new_id in self.result["added"]:
            self.result["added"].remove(new_id)
        self.result["superseded"].append({"old": old_id, "new": new_id})

    def _retire_created(self, rid: str) -> None:
        marker = _marker(self.mem, self.batch_id, self.pkey, self.cwd)
        if marker:
            _quiet(self.mem.supersede, rid, marker)

    def _reinforce(self, index: int, rid: str) -> None:
        mem = self.mem
        con = mem.get_con()
        try:
            row = _row(con, rid)
        finally:
            con.close()
        before = {"strength": row[9] or 1, "last_accessed": row[10]}
        op = self.journal.intent(op="reinforce", id=rid, before=before)
        ok, text = _quiet(mem.reinforce, rid)
        if not ok:
            self.journal.drop(op)
            self.result["skipped"].append({"kind": "reinforce", "reason": "reinforce-refused: " + text[:120],
                                           "index": index})
            return
        con = mem.get_con()
        try:
            self.journal.done(op, after=_digest(con, rid))
        finally:
            con.close()
        self.result["reinforced"].append(rid)


def _tidy_line(t, batch: str, text: str) -> str:
    """``[tidy] batch <id>: …``; the session-tidy runner finds the apply summary by ``[tidy]``."""
    return "[tidy] " + t("tidy.apply.batch", batch=batch, text=text)


def summary_line(result: dict) -> str:
    t = st._ui_translator()
    batch = result["batch_id"]
    undo = t("tidy.apply.undo_hint", batch=batch)
    added = len(result["added"]) + len(result["choice_added"])
    parts = []
    if result["status"] in ("partial", "integrity-violation"):
        done = added + len(result["superseded"]) + len(result["reinforced"])
        parts.append(t("tidy.apply.partial", count=done) if result["status"] == "partial"
                     else t("tidy.apply.integrity"))
        if result.get("failure"):
            parts.append(str(result["failure"])[:100])
        return _tidy_line(t, batch, " · ".join(parts) + f" — {undo}")
    parts = [t("tidy.apply.added", count=added), t("tidy.apply.updated", count=len(result["superseded"])),
             t("tidy.apply.reinforced", count=len(result["reinforced"])),
             t("tidy.apply.skipped", count=len(result["skipped"]))]
    if result["rejected"]:
        parts.append(t("tidy.apply.rejected", count=len(result["rejected"])))
    if result["discarded_over_budget"]:
        parts.append(t("tidy.apply.over_budget", count=result["discarded_over_budget"]))
    if result["choice_over_cap"]:
        parts.append(t("tidy.apply.choice_over_cap", limit=MAX_NEW, count=result["choice_over_cap"]))
    return _tidy_line(t, batch, " · ".join(parts) + f" — {undo}")


def apply_batch(mem, doc: dict, input_doc: Optional[dict], cwd: str, input_digest: Optional[str] = None) -> dict:
    """Apply one batch under the caller's apply lock; return the result document."""
    batch_id = str(doc.get("batch_id") or "")
    if not batch_id:
        batch_id = "b-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + os.urandom(3).hex()
    if not BATCH_RE.match(batch_id):
        raise ApplyError(f"invalid batch_id: {batch_id!r}")
    directory = st.ensure_dir(run_dir(batch_id))
    prior = st.read_json(directory / "result.json")
    if isinstance(prior, dict) and prior.get("batch_id") == batch_id:
        prior["already_applied"] = True
        return prior
    interrupted = Journal.load(directory)
    if interrupted is not None and interrupted.doc.get("ops"):
        result = {"schema_version": SCHEMA, "batch_id": batch_id, "status": "partial",
                  "failure": st._ui_translator()("tidy.apply.interrupted"), "added": [], "choice_added": [],
                  "superseded": [], "reinforced": [], "skipped": [], "rejected": [],
                  "discarded_over_budget": 0, "choice_over_cap": 0, "already_applied": True}
        ops = [op for op in interrupted.doc["ops"] if op.get("state") == "done"]
        result["added"] = [op["id"] for op in ops if op["op"] == "add"]
        result["superseded"] = [{"old": op["old_id"], "new": op["new_id"]} for op in ops if op["op"] == "supersede"]
        result["reinforced"] = [op["id"] for op in ops if op["op"] == "reinforce"]
        return result

    pkey = td.project_origin(mem, cwd)
    batch = Batch(mem, batch_id, cwd, pkey, directory)
    batch.result["before"] = _counts(mem)
    block_reason = None
    declared = doc.get("input_digest")
    if declared and input_digest and declared != input_digest:
        block_reason = "input-digest-mismatch"
    actions = doc.get("actions")
    if actions is None:
        actions = []
    if not isinstance(actions, list):
        raise ApplyError("actions must be a list")
    try:
        # 1. the user's own choices first: from the input, then the waiting folder.
        pending = [(path, payload) for path, payload in td.list_pending()
                   if td.project_origin(mem, payload.get("cwd") or cwd) == pkey]
        queue = [(None, payload) for payload in _choice_payloads(input_doc, cwd)] + pending
        seen = set()
        for path, payload in queue:
            if payload["source"] in seen:
                if path:
                    td.discard_pending(path)
                continue
            seen.add(payload["source"])
            batch.write_choice(payload, path)
        written = len(batch.result["choice_added"])
        batch.result["choice_over_cap"] = max(0, written - MAX_NEW)
        # 2. the model's actions, with what is left of the ten new records.
        batch.apply_actions(actions, max(0, MAX_NEW - written), block_reason)
    except ApplyError:
        raise
    except BaseException as exc:  # noqa: BLE001 - stop and report a partial batch
        if isinstance(exc, KeyboardInterrupt):
            raise
        batch.result["status"] = "partial"
        batch.result["failure"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    batch.result["after"] = _counts(mem)
    before, after = batch.result["before"], batch.result["after"]
    if batch.result["status"] == "applied" and (
            after["records"] < before["records"] or after["graveyard"] != before["graveyard"]):
        batch.result["status"] = "integrity-violation"
        batch.result["failure"] = (f"records {before['records']}→{after['records']}, "
                                   f"graveyard {before['graveyard']}→{after['graveyard']}")
    batch.result["finished"] = _now_iso()
    batch.result["summary"] = summary_line(batch.result)
    batch.journal.flush()
    st.atomic_write_json(directory / "result.json", batch.result)
    return batch.result


def apply_command(mem, args) -> int:
    """``mem tidy-apply``: prints one last summary line."""
    previous_cwd = os.getcwd()
    try:
        actions_path = Path(args.actions)
        doc = _read_actions(actions_path)
        input_path = Path(args.input) if args.input else None
        if input_path is None and (actions_path.parent / "input_v1.json").is_file():
            input_path = actions_path.parent / "input_v1.json"
        input_doc, input_digest = None, None
        if input_path is not None:
            try:
                input_doc = json.loads(input_path.read_text(encoding="utf-8"))
                input_digest = _digest_of_file(input_path)
            except (OSError, ValueError) as exc:
                raise ApplyError(f"input file unreadable: {input_path}: {exc}") from exc
            if not isinstance(input_doc, dict):
                raise ApplyError("input file must be a JSON object")
        cwd = str(Path(args.cwd or (input_doc or {}).get("cwd") or os.getcwd()).resolve())
        if not Path(cwd).is_dir():
            raise ApplyError(f"project folder not found: {cwd}")
        os.chdir(cwd)
        os.environ["MEM_ACTOR"] = ACTOR
        try:
            with td.apply_lock(timeout=APPLY_LOCK_WAIT_SEC):
                result = apply_batch(mem, doc, input_doc, cwd, input_digest)
        except td.LockBusy:
            raise ApplyError("another tidy is applying; try again shortly") from None
        except st.StateError as exc:
            raise ApplyError(str(exc)) from exc
    except ApplyError as exc:
        sys.stderr.write(f"[tidy] {exc}\n")
        return 2
    finally:
        with contextlib.suppress(OSError):
            os.chdir(previous_cwd)
    print(result.get("summary") or summary_line(result))
    return 0 if result["status"] == "applied" else 1


# ---------------------------------------------------------------------------
# tidy-undo
# ---------------------------------------------------------------------------

def _marker(mem, batch_id: str, pkey: str, cwd: str) -> Optional[str]:
    """The one record that stands in as the successor of everything undo takes out of use."""
    source = f"tidy-undo:{batch_id}"
    con = mem.get_con()
    try:
        found = mem.find_by_source("working", "project", "tidy-undo", source, pkey, con)
    finally:
        con.close()
    if found:
        return found
    # A stored memory record, not screen text: it stays as earlier releases wrote it.
    body = (f"정리 되돌림 표식: 묶음 {batch_id}의 새 기록을 사용에서 내렸습니다. "
            "이 기록에는 따로 기억할 내용이 없습니다.")
    return mem.write_record("working", "project", "tidy-undo", body, cwd_origin=pkey, source=source,
                            quiet=True, journal_action="tidy-undo", journal_insert_only=True,
                            journal_actor=ACTOR, journal_cwd=cwd, headline=f"정리 되돌림 {batch_id}")


def _adopt(mem, con, op: dict, pkey: str) -> bool:
    """An op that only reached ``intent``: decide from the store whether it landed."""
    if op["op"] == "add":
        found = mem.find_by_source(op.get("tier"), "project", op.get("type"), op.get("source"),
                                   pkey, con)
        if found:
            op["id"], op["after"] = found, _digest(con, found)
            return True
        return False
    if op["op"] == "supersede":
        now = _digest(con, op["old_id"])
        if now and now["status"] == "superseded" and now["superseded_by"] == op["new_id"]:
            op["after"] = {"old": now, "new": _digest(con, op["new_id"])}
            return True
        return False
    if op["op"] == "reinforce":
        now = _digest(con, op["id"])
        if now and now["strength"] == (op["before"]["strength"] + 1):
            op["after"] = now
            return True
        return False
    return False


def _preflight(mem, journal: Journal, marker_hint: Optional[str], pkey: str) -> Optional[str]:
    """Reason undo must refuse (something changed the records after the batch), else None."""
    t = st._ui_translator()
    con = mem.get_con()
    try:
        for op in journal.doc["ops"]:
            if op.get("state") == "intent" and _adopt(mem, con, op, pkey):
                op["state"] = "done"
        for op in journal.doc["ops"]:
            if op.get("state") != "done":
                continue
            if op["op"] == "add":
                now, after = _digest(con, op["id"]), op.get("after")
                if now is None:
                    return t("tidy.undo.gone", record=op["id"])
                if now["status"] == "superseded" and marker_hint and now["superseded_by"] == marker_hint:
                    continue
                if now != after:
                    return t("tidy.undo.changed", record=op["id"])
            elif op["op"] == "supersede":
                old, new = _digest(con, op["old_id"]), _digest(con, op["new_id"])
                after = op.get("after") or {}
                if old is None or new is None:
                    return t("tidy.undo.either_gone", old=op["old_id"], new=op["new_id"])
                if old != after.get("old"):
                    return t("tidy.undo.changed", record=op["old_id"])
                if new != after.get("new") and not (marker_hint and new["status"] == "superseded"
                                                    and new["superseded_by"] == marker_hint):
                    return t("tidy.undo.changed", record=op["new_id"])
            elif op["op"] == "reinforce":
                now, after = _digest(con, op["id"]), op.get("after")
                if now is None or now != after:
                    return t("tidy.undo.changed", record=op["id"])
    finally:
        con.close()
    return None


def _restore_strength(mem, rid: str, before: dict) -> bool:
    con = mem.get_con()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT tier, scope, type FROM records WHERE id=?", (rid,)).fetchone()
        if row is None:
            con.rollback()
            return False
        con.execute("UPDATE records SET strength=?, last_accessed=? WHERE id=?",
                    (before["strength"], before["last_accessed"], rid))
        mem._capture_v2_operation(con, "put", post_ids=[rid], reason="tidy-undo-reinforce")
        con.commit()
        mem._append_write_event("tidy-undo-reinforce", rid, tier=row[0], scope=row[1], rtype=row[2], actor=ACTOR)
        return True
    finally:
        con.close()


def _takedown(mem, rid: str, marker: str) -> tuple:
    """Take a record out of use (superseded by the marker); idempotent."""
    con = mem.get_con()
    try:
        now = _digest(con, rid)
    finally:
        con.close()
    if now and now["status"] == "superseded" and now["superseded_by"] == marker:
        return True, ""
    return _quiet(mem.supersede, rid, marker)


def undo_batch(mem, batch_id: str) -> tuple:
    """Undo one batch under the caller's apply lock → (exit code, one line)."""
    if not BATCH_RE.match(batch_id or ""):
        raise ApplyError(f"invalid batch id: {batch_id!r}")
    directory = run_dir(batch_id)
    journal = Journal.load(directory)
    if journal is None:
        raise ApplyError(f"no undo record for batch {batch_id}")
    t = st._ui_translator()
    if journal.doc.get("undone"):
        return 0, _tidy_line(t, batch_id, t("tidy.undo.already"))
    cwd = journal.doc.get("cwd") or os.getcwd()
    if not Path(cwd).is_dir():
        raise ApplyError(f"project folder not found: {cwd}")
    os.chdir(cwd)
    pkey = journal.doc.get("project_key") or td.project_origin(mem, cwd)
    con = mem.get_con()
    try:
        marker = mem.find_by_source("working", "project", "tidy-undo", f"tidy-undo:{batch_id}", pkey, con)
    finally:
        con.close()
    problem = _preflight(mem, journal, marker, pkey)
    if problem:
        return 1, _tidy_line(t, batch_id, t("tidy.undo.refused", problem=problem))
    ops = [op for op in journal.doc["ops"] if op.get("state") == "done"]
    needs_marker = any(op["op"] in ("add", "supersede") for op in ops)
    marker = _marker(mem, batch_id, pkey, cwd) if needs_marker else None
    if needs_marker and not marker:
        return 1, _tidy_line(t, batch_id, t("tidy.undo.no_marker"))
    restored = 0
    for op in reversed(ops):
        if op["op"] == "reinforce":
            ok = _restore_strength(mem, op["id"], op["before"])
            fail = "" if ok else t("tidy.undo.reinforce_failed", record=op["id"])
        elif op["op"] == "supersede":
            ok, text = _takedown(mem, op["new_id"], marker)
            if ok:
                ok, text = _quiet(mem.activate, op["old_id"])
            fail = "" if ok else t("tidy.undo.restore_failed", record=op["old_id"], error=text[:100])
        else:
            ok, text = _takedown(mem, op["id"], marker)
            fail = "" if ok else t("tidy.undo.takedown_failed", record=op["id"], error=text[:100])
        if not ok:
            journal.flush()
            return 1, _tidy_line(t, batch_id, t("tidy.undo.stopped", count=restored, failure=fail, batch=batch_id))
        op["state"] = "undone"
        restored += 1
        journal.flush()
    journal.doc["undone"] = _now_iso()
    journal.flush()
    return 0, _tidy_line(t, batch_id, t("tidy.undo.done", count=restored))


def undo_command(mem, args) -> int:
    previous_cwd = os.getcwd()
    try:
        os.environ["MEM_ACTOR"] = ACTOR
        try:
            with td.apply_lock(timeout=APPLY_LOCK_WAIT_SEC):
                code, line = undo_batch(mem, args.batch)
        except td.LockBusy:
            raise ApplyError("another tidy is applying; try again shortly") from None
        except st.StateError as exc:
            raise ApplyError(str(exc)) from exc
    except ApplyError as exc:
        sys.stderr.write(f"[tidy] {exc}\n")
        return 2
    finally:
        with contextlib.suppress(OSError):
            os.chdir(previous_cwd)
    print(line)
    return code
