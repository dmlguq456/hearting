#!/usr/bin/env python3
"""`peer-steward.py request | report | show`: the commands over a peer request.

They send through the same sealed message and carrier as `prompt`
(`cmd_prompt` with a `request` seal), so delivery, obligations and wake-ups are
the existing ones. This module only decides what a request's work state records
and what its one-line message says. ``steward`` is the peer-steward module.

Order of record and seal (the request lock is outermost and is never held while
a message is sealed or delivered): a new request first takes an id as
`preparing` (in no index); the message is sealed outside any request lock; the
`on_prepared` callback commits the record once the seal exists and before any
delivery attempt. A failed seal removes the `preparing` record.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import peer_request as pr  # noqa: E402

_ID = re.compile(r"rq-[0-9a-f]{6}\Z")
_PROMPT_RESULT = re.compile(r"^prompted=(\S+)(.*)$", re.MULTILINE)


def _field(text, key):
    match = re.search(r"(?:^|\s)%s=(\S+)" % re.escape(key), text)
    return match.group(1) if match else ""


def _me(steward):
    sid, harness = steward._current_session_identity()
    if harness not in steward._PROMPT_HARNESSES or not steward.peer_message.usable_session_id(sid):
        return None
    return {"harness": harness, "session_id": sid}


def _endpoint(steward, who):
    alias = steward.peer_message.peer_alias(who["harness"], who["session_id"])
    return {**who, "name": "" if alias == "[?]" else alias.strip("[]")}


def _read_body(args):
    if getattr(args, "body_file", None):
        with open(args.body_file, encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read()
    if getattr(args, "body_stdin", False):
        return sys.stdin.read()
    return getattr(args, "text", None) or ""


def _refuse(head, reason, code=1, **extra):
    fields = " ".join("%s=%s" % item for item in extra.items())
    print("%s delivery=refused reason=%s%s" % (head, reason, (" " + fields) if fields else ""))
    return code


def _send(steward, label, body, recipient, seal, line, refs, on_prepared):
    """One sealed send through `cmd_prompt`. Returns ``(rc, outcome, ref, via, reason)``."""
    ns = argparse.Namespace(target=label, text=body, body_file=None, body_stdin=False,
                            no_trailer=False, ref=list(refs))
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        rc = steward.cmd_prompt(ns, resolved_recipient=recipient, on_prepared=on_prepared,
                                request={"seal": seal, "line": line})
    output = captured.getvalue()
    match = None
    for match in _PROMPT_RESULT.finditer(output):
        pass
    outcome = match.group(1) if match else "unverified"
    rest = match.group(2) if match else ""
    return rc, outcome, _field(rest, "ref"), _field(rest, "via"), _field(rest, "reason")


def _label(steward, recipient):
    """The display address a sealed send carries: the recipient's tag, never a window address."""
    alias = steward.peer_message.peer_alias(recipient["harness"], recipient["session_id"])
    return alias if alias != "[?]" else recipient["session_id"][:8]


def _delivery(outcome):
    return "delivered" if outcome == "true" else "queued-not-delivered"


def _next_hint(via, reason):
    if via:
        return via
    return reason or "native-callback"


def _rows(steward, record):
    rows = {}
    refs = []
    for revision in record["revisions"]:
        refs.append(revision["transfer_ref"])
        refs.extend(revision.get("followups") or ())
    refs.extend(report.get("transfer_ref") for report in record["reports"])
    for ref in refs:
        if not ref:
            continue
        try:
            rows[ref] = steward.peer_message._read_pending(ref)
        except (OSError, ValueError):
            rows[ref] = None
    return rows


def _received_by(row, sessions):
    """True when the revision's transfer row was received by one of ``sessions`` (exact receipt)."""
    if not row or row.get("state") != "received":
        return False
    to = row.get("to") or {}
    return (to.get("harness"), to.get("session_id")) in sessions


def _delivered_to(row, sessions):
    """True when the transfer row was handed to one of ``sessions`` (queued or received)."""
    if not row or row.get("state") not in ("queued", "received"):
        return False
    to = row.get("to") or {}
    return (to.get("harness"), to.get("session_id")) in sessions


def _delivered_anywhere(steward, record):
    """True when some revision or follow-up of the request may have reached the receiver."""
    for row in _rows(steward, record).values():
        if row and row.get("state") in ("queued", "received", "unverified"):
            return True
    return False


# --- request ---------------------------------------------------------------

def request(args, steward):
    me = _me(steward)
    if me is None:
        return _refuse("request=-", "sender-unknown")
    if _ID.fullmatch(args.target or ""):
        if getattr(args, "cancel", False):
            return _cancel(args, steward, me)
        return _revise(args, steward, me)
    if getattr(args, "cancel", False):
        return _refuse("request=-", "cancel-needs-request-id")
    body = _read_body(args).rstrip("\n")
    if not body.strip():
        return _refuse("request=-", "empty-body")
    import peer_address
    identity, reason = peer_address.resolve(args.target, server=steward._HERDR_SESSION)
    sid = steward.peer_message.usable_session_id((identity or {}).get("session_id"))
    if (identity is None or reason or identity.get("harness") not in steward._PROMPT_HARNESSES
            or not sid):
        # A request is bound to one exact session. An address Hearting cannot
        # resolve is refused, never kept pending under a guessed recipient.
        return _refuse("request=-", reason or "target-session-unresolved", 5)
    recipient = {"harness": identity["harness"], "session_id": sid}
    if recipient == me:
        return _refuse("request=-", "request-to-self")
    title = args.title or body.strip().splitlines()[0]
    label = args.rev or ""
    request_id = pr.reserve(_endpoint(steward, me), _endpoint(steward, recipient),
                            title=title, address=args.target)
    head = "request=%s" % request_id
    record = pr.load(request_id)
    line = pr.render_line(record, "request", revision=1, label=label,
                          body_lines=len(body.splitlines()))
    seal = pr.request_seal(request_id, 1, "request")
    committed = []

    def commit(ref):
        pr.commit_open(request_id, ref, label=label)
        committed.append(ref)

    refs = [pr.ref_for(request_id, 1, "request")]
    rc, outcome, ref, via, why = _send(steward, _label(steward, recipient), body, recipient, seal,
                                       line, refs, commit)
    if not committed:
        pr.abort_preparing(request_id)
        return _refuse("request=-", why or "seal-failed", rc or 5)
    print("%s delivery=%s to=%s harness=%s via=%s reason=%s next=%s" % (
        head, _delivery(outcome), steward.peer_message.peer_alias(recipient["harness"], sid),
        recipient["harness"], via or "-", why or "-", _next_hint(via, why)))
    return 0 if outcome in ("true", "queued") else rc


def _revise(args, steward, me):
    request_id = args.target
    head = "request=%s" % request_id
    record = pr.load(request_id)
    if record is None or record["state"] == "preparing":
        return _refuse(head, "request-unknown")
    if not pr._same(pr.current_requester(record), me):
        return _refuse(head, "not-the-requester")
    if record["state"] not in pr._OPEN_STATES:
        return _refuse(head, "request-closed")
    body = _read_body(args).rstrip("\n")
    if not body.strip():
        return _refuse(head, "empty-body")
    recipient = pr.current_recipient(record)
    current = pr.current_revision(record)
    new_revision = args.rev is not None and args.rev != current.get("label", "")
    number = len(record["revisions"]) + 1 if new_revision else current["n"]
    role = "request" if new_revision else "followup"
    label = args.rev if new_revision else current.get("label", "")
    seal = pr.request_seal(request_id, number, role)
    line = pr.render_line(record, role, revision=number, label=label,
                          body_lines=len(body.splitlines()))
    serial = len(current["followups"]) + 1
    refs = [pr.ref_for(request_id, number, role)]
    if role == "followup":
        refs.append("peer-request-seq:%d" % serial)
    state = {}

    def commit(ref):
        if new_revision:
            _record, state["n"] = pr.add_revision(request_id, ref, label=label)
        else:
            _record, state["n"] = pr.add_followup(request_id, ref)

    rc, outcome, ref, via, why = _send(steward, _label(steward, recipient), body,
                                       recipient, seal, line, refs, commit)
    if "n" not in state:
        return _refuse(head, why or "seal-failed", rc or 5)
    extra = " revision=%d supersedes=%d" % (number, number - 1) if new_revision else " followup=%d" % serial
    print("%s delivery=%s%s to=%s harness=%s via=%s reason=%s next=%s" % (
        head, _delivery(outcome), extra,
        steward.peer_message.peer_alias(recipient["harness"], recipient["session_id"]),
        recipient["harness"], via or "-", why or "-", _next_hint(via, why)))
    return 0 if outcome in ("true", "queued") else rc


def _cancel(args, steward, me):
    request_id = args.target
    head = "request=%s" % request_id
    record = pr.load(request_id)
    if record is None or record["state"] == "preparing":
        return _refuse(head, "request-unknown")
    if not pr._same(pr.current_requester(record), me):
        return _refuse(head, "not-the-requester")
    if record["state"] not in pr._OPEN_STATES:
        return _refuse(head, "request-closed")
    notify = _delivered_anywhere(steward, record)
    reason = args.reason or ""
    try:
        record = pr.cancel(request_id, reason=reason)
    except ValueError as exc:
        return _refuse(head, str(exc))
    if not notify:
        print("%s state=cancelled notice=not-needed" % head)
        return 0
    recipient = pr.current_recipient(record)
    revision = record["revisions"][-1]["n"]
    body = "요청 %s 취소: %s" % (request_id, reason or record.get("title", ""))
    line = pr.render_line(record, "cancel")
    sent = {}

    def commit(ref):
        pr.set_cancel_notice(request_id, ref)
        sent["ref"] = ref

    rc, outcome, ref, via, why = _send(
        steward, _label(steward, recipient), body, recipient,
        pr.request_seal(request_id, revision, "cancel"), line,
        [pr.ref_for(request_id, revision, "cancel")], commit)
    if "ref" not in sent:
        pr.set_cancel_notice(request_id, "", failed=why or "seal-failed")
        print("%s state=cancelled notice=refused reason=%s" % (head, why or "seal-failed"))
        return 0
    print("%s state=cancelled notice=%s" % (head, _delivery(outcome)))
    return 0


# --- report ----------------------------------------------------------------

def _open_received(me):
    return [record for record in pr.session_requests(me["harness"], me["session_id"])["in"]
            if record["state"] in pr._OPEN_STATES]


def report(args, steward):
    me = _me(steward)
    if me is None:
        return _refuse("report=-", "sender-unknown")
    if args.partial and args.reject is not None:
        return _refuse("report=-", "partial-and-reject")
    if args.rq:
        record = pr.load(args.rq)
        if record is None:
            return _refuse("report=%s" % args.rq, "request-unknown")
        if record["state"] == "preparing":
            return _refuse("report=%s" % args.rq, "request-incomplete")
        if not pr._same(pr.current_recipient(record), me):
            return _refuse("report=%s" % args.rq, "reporter-not-recipient")
    else:
        candidates = _open_received(me)
        if not candidates:
            return _refuse("report=-", "no-open-request")
        if len(candidates) > 1:
            print("report=- delivery=refused reason=request-ambiguous candidates=%s" % ",".join(
                record["id"] for record in candidates))
            return 1
        record = candidates[0]
    request_id = record["id"]
    head = "report=%s" % request_id
    kind = "partial" if args.partial else "reject" if args.reject is not None else "final"
    result = args.reject if kind == "reject" else (args.result or "")
    body = ""
    path = args.file or args.body_file
    if path:
        with open(path, encoding="utf-8", errors="replace", newline="") as handle:
            body = handle.read()
    elif args.body_stdin:
        body = sys.stdin.read()
    if not body.strip():
        pieces = [result, ("남은 것: " + args.remaining) if args.remaining else ""]
        body = "\n".join(piece for piece in pieces if piece)
    if not body.strip():
        return _refuse(head, "report-empty")
    if not result:
        result = body.strip().splitlines()[0]
    rows = _rows(steward, record)
    if args.rev is not None:
        revision = args.rev
    else:
        # The newest revision this session (or its confirmed predecessor) received; failing
        # that, the newest one delivered to it. A convenience only: the guidance's explicit
        # --rev is what separates a late report of an older revision.
        mine = [(me["harness"], me["session_id"])] + [
            (me["harness"], sid) for sid in pr._predecessors(me["harness"], me["session_id"])]
        received = [r["n"] for r in record["revisions"]
                    if _received_by(rows.get(r["transfer_ref"]), mine)]
        delivered = [r["n"] for r in record["revisions"]
                     if _delivered_to(rows.get(r["transfer_ref"]), mine)]
        if not received and not delivered:
            return _refuse(head, "report-revision-unreceived",
                           revisions=",".join(str(r["n"]) for r in record["revisions"]),
                           next="report %s --rev N" % request_id)
        revision = max(received or delivered)
    target = pr.revision_of(record, revision)
    if target is None:
        return _refuse(head, "revision-unknown")
    late = target.get("state") != "current" or record["state"] in pr._CLOSED_STATES
    number = len(record["reports"]) + 1
    import time as _time
    synthetic = {"n": number, "revision": revision, "kind": kind, "result": pr.clean(result, 120),
                 "late": late, "elapsed": max(0.0, _time.time() - (target.get("created") or _time.time()))}
    requester = pr.current_requester(record)
    role = "late-report" if late else "report"
    line = pr.render_line(record, role, report=synthetic)
    seal = pr.request_seal(request_id, revision, role)
    refs = [pr.ref_for(request_id, revision, role), "peer-request-seq:%d" % number]
    state = {}

    def commit(ref):
        _record, state["report"] = pr.record_report(
            request_id, kind=kind, revision=revision, result=result, remaining=args.remaining or "",
            artifact=args.artifact or "", transfer_ref=ref, reporter=_endpoint(steward, me))

    rc, outcome, ref, via, why = _send(steward, _label(steward, requester), body,
                                       requester, seal, line, refs, commit)
    if "report" not in state:
        return _refuse(head, why or "seal-failed", rc or 5)
    after = pr.load(request_id)
    print("%s kind=%s revision=%d late=%s state=%s delivery=%s via=%s reason=%s next=%s" % (
        head, kind, revision, str(state["report"]["late"]).lower(), after["state"],
        _delivery(outcome), via or "-", why or "-", _next_hint(via, why)))
    return 0 if outcome in ("true", "queued") else rc


# --- show ------------------------------------------------------------------

def _party(record, me):
    return any(pr._same(who, me) for who in (
        record["requester"], record["recipient"], pr.current_requester(record),
        pr.current_recipient(record)))


def show(args, steward):
    me = _me(steward)
    if me is None:
        print("show reason=sender-unknown")
        return 1
    if not args.rq:
        listing = pr.session_requests(me["harness"], me["session_id"])
        count = 0
        for side, mark in (("in", "◂"), ("out", "→")):
            for record in listing[side]:
                if record["state"] in pr._OPEN_STATES or args.all:
                    other = record["requester" if side == "in" else "recipient"]
                    print("%s %s %s %s │ %s" % (mark, record["id"], record["state"],
                                                pr._name(other), record.get("title", "")))
                    if side == "in" and record["state"] in pr._OPEN_STATES:
                        print("  %s" % pr.report_commands(
                            record["id"], pr.current_revision(record)["n"])["final"])
                    count += 1
        if not count:
            print("requests=none")
        return 0
    record = pr.load(args.rq)
    if record is None:
        print("request=%s reason=request-unknown" % args.rq)
        return 1
    if record["state"] == "preparing":
        print("request=%s reason=request-incomplete" % args.rq)
        return 1
    if not _party(record, me):
        print("request=%s reason=not-a-party" % args.rq)
        return 1
    rows = _rows(steward, record)
    for line in pr.describe(record, rows=rows):
        print(line)
    if args.report is not None:
        report = next((item for item in record["reports"] if item["n"] == args.report), None)
        if report is None:
            print("body=unreadable reason=report-unknown")
            return 1
        refs = [("report %d" % report["n"], report["transfer_ref"])]
    else:
        revision = pr.revision_of(record, args.rev) if args.rev else pr.current_revision(record)
        if revision is None:
            print("body=unreadable reason=revision-unknown")
            return 1
        refs = [("revision %d" % revision["n"], revision["transfer_ref"])] + [
            ("followup %d" % (index + 1), ref) for index, ref in enumerate(revision.get("followups") or ())]
    if args.report is None and pr._same(pr.current_recipient(record), me) \
            and record["state"] in pr._OPEN_STATES:
        for kind, command in pr.report_commands(record["id"], revision["n"]).items():
            print("report-%s: %s" % (kind, command))
    status = 0
    for label, ref in refs:
        try:
            payload = steward.peer_message.read_peer_payload(ref)
            body = (payload or {}).get("body")
            if not isinstance(body, str):
                raise ValueError("peer-payload-missing")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            pr.note_body_unreadable(record["id"], str(exc))
            print("body=unreadable reason=%s part=%s" % (exc, label))
            status = 1
            continue
        print("--- %s (sealed body) ---" % label)
        print(body.rstrip("\n"))
        print("--- end ---")
    return status
