#!/usr/bin/env python3
"""Campaign/cycle metadata and the project vocabulary: one read model, one write path, one command.

Public files (Cairn reads them):
  campaigns/<locator>/meta.json                          contract `artifact-meta/v1`
  .runtime/artifact-producer/v1/project-meta.json        contract `artifact-project-meta/v1`
Internal (never a Cairn input):
  .runtime/artifact-producer/v1/artifact-meta-state.json   number high-water, issued IDs, project sources
  .runtime/artifact-producer/v1/artifact-meta-transactions/  at most one write-ahead intent per write

Every writer -- the background judgement, a person, an agent -- goes through `run_write`: one
admission lock, one validation, one recorder (`artifact_history`).  The intent file is the
commit point of a multi-file write: it is written once, whole, before the first replacement,
and `recover_locked` finishes it (replace what is still at its old digest, publish the events
once) at the next write.  Reads, `show`, and `--dry-run` never recover and never write.
Nothing here is a gate, an input, or an obligation for an agent or a user.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_admission as admission  # noqa: E402
import artifact_history as H  # noqa: E402
import artifact_identity as identity  # noqa: E402
import artifact_lifecycle as lifecycle  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_producer as producer  # noqa: E402
import artifact_workflow_groups as W  # noqa: E402

META_CONTRACT = "artifact-meta/v1"
PROJECT_CONTRACT = "artifact-project-meta/v1"
STATE_CONTRACT = "artifact-meta-state/v1"
INTENT_SCHEMA = "artifact-meta-intent/v1"
META_NAME = "meta.json"
PROJECT_REL = ".runtime/artifact-producer/v1/project-meta.json"
STATE_REL = ".runtime/artifact-producer/v1/artifact-meta-state.json"
DISPLAY_TITLES_REL = ".runtime/artifact-producer/v1/campaign-display-titles.json"

KINDS = ("학습", "데이터", "평가", "문서", "운영", "조사", "배포")
FIELDS = ("short_id", "title", "summary", "branches", "kinds")
ENTRY_ORDER = ("short_id", "aliases", "title", "summary", "branches", "kinds", "source")
PRESENTATION_KINDS = ("archive_bundle",)
ACTORS = ("rule", "model", "human", "agent")
PROTECTING = frozenset(("human", "agent"))
ETC = "ETC"
ETC_LABEL = "기타"
ETC_NOTE = "기존 갈래에 맞지 않는 작업"
GENERAL_BRANCH_MAX = 12  # ETC is not counted
LIST_MAX = 16
TITLE_MAX = 120
SUMMARY_MAX = 400
LABEL_MAX = 40
NOTE_MAX = 120
SIZE_CAP = 8 * 1024 * 1024
RFC3339 = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
BRANCH_CODE = re.compile(r"[A-Z]{2,5}\Z")
CAMPAIGN_SHORT = re.compile(r"([A-Z]{2,5})-(\d{2,})\Z")
CYCLE_SHORT = re.compile(r"([A-Z]{2,5})-(\d{2,})\.(\d+)\Z")
TITLE_DISABLE_ENV = "HEARTING_CAMPAIGN_TITLE_AUTO"


class MetaError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


def title_auto_disabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(TITLE_DISABLE_ENV, "")).strip().lower() in {"off", "0", "false", "no", "disabled"}


# ---------------------------------------------------------------------------
# value rules (the public contract's limits)
# ---------------------------------------------------------------------------


def check_text(value: Any, maximum: int, code: str, *, allow_empty: bool = False) -> str:
    if (not isinstance(value, str) or (not value and not allow_empty) or len(value) > maximum
            or value != value.strip() or unicodedata.normalize("NFC", value) != value
            or any(unicodedata.category(char) in ("Cc", "Zl", "Zp") for char in value)):
        raise MetaError(code)
    return value


def check_title(value: Any) -> str:
    return check_text(value, TITLE_MAX, "title-invalid")


def check_summary(value: Any) -> str:
    return check_text(value, SUMMARY_MAX, "summary-invalid", allow_empty=True)


def check_code(value: Any) -> str:
    if not isinstance(value, str) or not BRANCH_CODE.fullmatch(value):
        raise MetaError("branch-code-invalid", str(value)[:20])
    return value


def _unique_list(value: Any, code: str) -> List[Any]:
    if not isinstance(value, list) or len(value) > LIST_MAX or len(set(map(str, value))) != len(value):
        raise MetaError(code)
    return value


def check_branches(value: Any) -> List[str]:
    return [check_code(item) for item in _unique_list(value, "branches-invalid")]


def check_kinds_item(value: Any) -> str:
    if value not in KINDS:
        raise MetaError("kind-unknown")
    return value


def check_kinds(value: Any) -> List[str]:
    items = _unique_list(value, "kinds-invalid")
    if any(item not in KINDS for item in items):
        raise MetaError("kind-unknown")
    return list(items)


def check_short_id(value: Any, *, cycle: bool) -> str:
    if not isinstance(value, str) or not (CYCLE_SHORT if cycle else CAMPAIGN_SHORT).fullmatch(value):
        raise MetaError("short-id-invalid", str(value)[:30])
    return value


def check_aliases(value: Any, *, cycle: bool) -> List[str]:
    return [check_short_id(item, cycle=cycle) for item in _unique_list(value, "aliases-invalid")]


def check_branch_def(item: Any) -> Dict[str, str]:
    if not isinstance(item, dict) or set(item) != {"code", "label", "note"}:
        raise MetaError("branch-def-invalid")
    return {"code": check_code(item["code"]), "label": check_text(item["label"], LABEL_MAX, "branch-label-invalid"),
            "note": check_text(item["note"], NOTE_MAX, "branch-note-invalid", allow_empty=True)}


def _is_time(value: Any) -> bool:
    return isinstance(value, str) and bool(RFC3339.fullmatch(value))


def _check_source(value: Any) -> None:
    if not isinstance(value, dict):
        raise MetaError("source-invalid")
    for name in FIELDS:
        row = value.get(name)
        if row is None:
            continue
        if not isinstance(row, dict) or row.get("by") not in ACTORS or not _is_time(row.get("at")):
            raise MetaError("source-invalid", name)


def validate_entry(entry: Any, *, cycle: bool) -> None:
    """Known fields only: a type or limit violation raises; unknown fields are ignored."""
    if not isinstance(entry, dict):
        raise MetaError("entry-invalid")
    if "presentation_kind" in entry:
        if cycle:
            raise MetaError("presentation-cycle-not-allowed")
        if entry["presentation_kind"] not in PRESENTATION_KINDS:
            raise MetaError("presentation-kind-invalid", str(entry["presentation_kind"])[:40])
    if "short_id" in entry:
        check_short_id(entry["short_id"], cycle=cycle)
    if "aliases" in entry:
        check_aliases(entry["aliases"], cycle=cycle)
    if "title" in entry:
        check_title(entry["title"])
    if "summary" in entry:
        check_summary(entry["summary"])
    if "branches" in entry:
        check_branches(entry["branches"])
    if "kinds" in entry:
        check_kinds(entry["kinds"])
    if "source" in entry:
        _check_source(entry["source"])


# ---------------------------------------------------------------------------
# reading (no lock, no write, no directory creation)
# ---------------------------------------------------------------------------


@dataclass
class FileRead:
    status: str  # missing | ok | invalid
    rel: str
    raw: Optional[bytes] = None
    doc: Optional[Dict[str, Any]] = None
    code: str = ""
    foreign: List[str] = field(default_factory=list)  # cycle keys whose current campaign is another


def _root_ids(root: Path) -> Tuple[str, str]:
    try:
        ident = lifecycle.read_root_identity(root)
    except lifecycle.LifecycleError as exc:
        raise MetaError("root-identity-unreadable", exc.args[0] if exc.args else "") from exc
    if ident is None:
        raise MetaError("root-identity-missing")
    return ident.artifact_root_id, ident.repository_id


def _read_raw(root: Path, rel: str) -> Optional[bytes]:
    try:
        W._no_symlink(root, Path(rel))
        return W._regular(root / rel, missing=True, cap=SIZE_CAP)
    except W.WorkflowGroupError as exc:
        raise MetaError("path-unsafe", exc.code) from exc


def _parse(raw: bytes) -> Dict[str, Any]:
    try:
        return W._json(raw)
    except W.WorkflowGroupError as exc:
        raise MetaError("json-invalid", exc.code) from exc


def _membership(root: Path) -> Dict[str, Dict[str, Any]]:
    return {row["cycle_id"]: row for row in producer.list_cycle_records(root)
            if isinstance(row.get("cycle_id"), str)}


def _validate_meta_doc(doc: Mapping[str, Any], root_id: str, campaign_id: str,
                       members: Mapping[str, Mapping[str, Any]], repository_id: Optional[str] = None) -> List[str]:
    """Raises MetaError on any known-field violation; returns the cycle keys owned by another campaign."""
    if (type(doc.get("schema_version")) is not int or doc.get("schema_version") != 1
            or doc.get("contract") != META_CONTRACT):
        raise MetaError("contract-unknown")
    if doc.get("artifact_root_id") != root_id or doc.get("campaign_id") != campaign_id:
        raise MetaError("identity-mismatch")
    if "campaign" in doc:
        validate_entry(doc["campaign"], cycle=False)
    # Presentation mark binds the current repository/root/campaign identities
    # exactly; any mismatch is a presentation-* error that skips only this
    # meta while lifecycle keeps folding. Lifecycle codes stay distinct.
    presentation = (doc.get("campaign") or {}).get("presentation_kind") if isinstance(doc.get("campaign"), dict) else None
    if isinstance(doc.get("campaign"), dict) and "presentation_kind" in doc["campaign"]:
        if repository_id is None:
            raise MetaError("presentation-identity-missing")
        if doc.get("repository_id") != repository_id:
            raise MetaError("presentation-repository-mismatch", str(doc.get("repository_id"))[:40])
    cycles = doc.get("cycles", {})
    if not isinstance(cycles, dict):
        raise MetaError("cycles-invalid")
    foreign = []
    for cycle_id, entry in cycles.items():
        if not identity.is_well_formed(cycle_id, "cycle"):
            raise MetaError("cycle-id-invalid", str(cycle_id)[:40])
        validate_entry(entry, cycle=True)
        if (members.get(cycle_id) or {}).get("campaign_id") != campaign_id:
            foreign.append(cycle_id)
    return foreign


def presentation_view(raw: Optional[bytes], *, root_id: str, repository_id: str, campaign_id: str,
                      members: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """Pure bytes -> (kind, status, reason) for the archive mark; never raises.

    `absent` when no mark is present, `valid` when the mark binds exactly,
    `invalid` with a `presentation-*`/identity reason otherwise. Lifecycle
    errors never surface here; unknown fields are preserved elsewhere.
    """
    if raw is None:
        return {"presentation_kind": None, "presentation_status": "absent", "presentation_reason": None}
    try:
        doc = _parse(raw)
    except MetaError as exc:
        return {"presentation_kind": None, "presentation_status": "invalid", "presentation_reason": exc.code if exc.code.startswith("presentation-") else "presentation-" + exc.code}
    entry = doc.get("campaign")
    kind = entry.get("presentation_kind") if isinstance(entry, dict) else None
    # Validate known metadata even when the mark is absent: a null mark or
    # a mark on a cycle is invalid, never silently interpreted as absence.
    try:
        if not root_id or not repository_id or not identity.is_well_formed(campaign_id, "campaign"):
            raise MetaError("presentation-identity-missing")
        _validate_meta_doc(doc, root_id, campaign_id, members or {}, repository_id=repository_id)
    except MetaError as exc:
        return {"presentation_kind": kind if isinstance(kind, str) else None,
                "presentation_status": "invalid", "presentation_reason": exc.code if exc.code.startswith("presentation-") else "presentation-" + exc.code}
    return {"presentation_kind": kind, "presentation_status": "valid" if kind is not None else "absent",
            "presentation_reason": None}


def _campaign_location(root: Path, campaign_id: str) -> Tuple[Dict[str, Any], str]:
    try:
        campaign, directory, _root_id, _repo_id = W._context(root, campaign_id)
    except W.WorkflowGroupError as exc:
        raise MetaError(exc.code, exc.detail) from exc
    return campaign, (directory / META_NAME).relative_to(root).as_posix()


def read_campaign_meta(root: Path, campaign_id: str, *,
                       members: Optional[Mapping[str, Mapping[str, Any]]] = None) -> FileRead:
    """The campaign's meta.json as a reader sees it: `invalid` (never an exception) on any violation."""
    root = Path(root).resolve()
    try:
        root_id, repo_id = _root_ids(root)
        _campaign, rel = _campaign_location(root, campaign_id)
        raw = _read_raw(root, rel)
    except MetaError as exc:
        return FileRead("invalid", "", code=exc.code)
    if raw is None:
        return FileRead("missing", rel)
    try:
        doc = _parse(raw)
        foreign = _validate_meta_doc(doc, root_id, campaign_id, members if members is not None else _membership(root),
                                     repository_id=repo_id)
    except MetaError as exc:
        return FileRead("invalid", rel, raw=raw, code=exc.code)
    if foreign:
        return FileRead("invalid", rel, raw=raw, doc=doc, code="cycle-foreign", foreign=foreign)
    return FileRead("ok", rel, raw=raw, doc=doc)


def read_project(root: Path) -> FileRead:
    root = Path(root).resolve()
    try:
        root_id, _repo = _root_ids(root)
        raw = _read_raw(root, PROJECT_REL)
    except MetaError as exc:
        return FileRead("invalid", PROJECT_REL, code=exc.code)
    if raw is None:
        return FileRead("missing", PROJECT_REL)
    try:
        doc = _parse(raw)
        _validate_project_doc(doc, root_id)
    except MetaError as exc:
        return FileRead("invalid", PROJECT_REL, raw=raw, code=exc.code)
    return FileRead("ok", PROJECT_REL, raw=raw, doc=doc)


def _validate_project_doc(doc: Mapping[str, Any], root_id: str) -> None:
    if (doc.get("schema_version") != 1 or isinstance(doc.get("schema_version"), bool)
            or doc.get("contract") != PROJECT_CONTRACT):
        raise MetaError("contract-unknown")
    if doc.get("artifact_root_id") != root_id:
        raise MetaError("identity-mismatch")
    check_text(doc.get("display_name"), TITLE_MAX, "display-name-invalid")
    branches = doc.get("branches")
    if not isinstance(branches, list) or len(branches) > LIST_MAX:
        raise MetaError("branches-invalid")
    seen = set()
    for item in branches:
        if (not isinstance(item, dict) or not isinstance(item.get("label"), str)
                or not isinstance(item.get("note"), str)):
            raise MetaError("branches-invalid")
        code = check_code(item.get("code"))
        if code in seen:
            raise MetaError("branches-invalid", "duplicate")
        seen.add(code)


def new_state(root_id: str) -> Dict[str, Any]:
    return {"schema_version": 1, "contract": STATE_CONTRACT, "artifact_root_id": root_id,
            "branch_high_water": {}, "cycle_high_water": {}, "project_source": {}, "issued": {}}


def _validate_state(doc: Mapping[str, Any], root_id: str) -> None:
    def counters(value: Any) -> bool:
        return isinstance(value, dict) and all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in value.values())

    if (doc.get("schema_version") != 1 or doc.get("contract") != STATE_CONTRACT
            or doc.get("artifact_root_id") != root_id or not counters(doc.get("branch_high_water"))
            or not counters(doc.get("cycle_high_water")) or not isinstance(doc.get("project_source"), dict)
            or not isinstance(doc.get("issued"), dict)):
        raise MetaError("state-invalid")
    for key, row in doc["issued"].items():
        if (not isinstance(key, str) or not isinstance(row, dict) or row.get("kind") not in ("campaign", "cycle")
                or not isinstance(row.get("id"), str)):
            raise MetaError("state-invalid", "issued")


def effective_title(root: Path, campaign_id: str, *, locator: str = "") -> str:
    """meta.json title, else the old display declaration, else the folder name."""
    read = read_campaign_meta(root, campaign_id)
    title = ((read.doc or {}).get("campaign") or {}).get("title") if read.status == "ok" else None
    if isinstance(title, str) and title:
        return title
    legacy, _present = legacy_title(root, campaign_id)
    return legacy or locator


def legacy_title(root: Path, campaign_id: str) -> Tuple[Optional[str], bool]:
    """(title usable as a meta title, whether the old declaration protects this campaign's title).

    The old declaration is only read.  Its titles carry no source, so a person's title is assumed;
    a declaration that exists but cannot be read protects every campaign's title the same way."""
    try:
        raw = _read_raw(Path(root).resolve(), DISPLAY_TITLES_REL)
        doc = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (MetaError, ValueError, UnicodeError):
        return None, True  # a declaration that cannot be read may hold a person's title: keep it safe
    if raw is None:
        return None, False
    entries = doc.get("entries") if isinstance(doc, dict) else None
    try:
        foreign_root = isinstance(doc, dict) and doc.get("artifact_root_id") != _root_ids(Path(root).resolve())[0]
    except MetaError:
        foreign_root = True
    if not isinstance(entries, list) or foreign_root:
        return None, True
    for row in entries:
        if isinstance(row, dict) and row.get("campaign_id") == campaign_id and isinstance(row.get("display_title"), str):
            value = unicodedata.normalize("NFC", row["display_title"]).strip()
            try:
                return check_title(value), True
            except MetaError:
                return None, True
    return None, False


def legacy_title_replaceable(entry: Optional[Mapping[str, Any]], legacy: Optional[str]) -> bool:
    """Whether an explicit backfill may renew this campaign title: none yet, or the person-sourced
    copy of the old declaration title that an earlier review imported (a title a person set
    through this tool differs from it and stays protected)."""
    if not legacy:
        return False
    entry = entry or {}
    if "title" not in entry:
        return True
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    by = (source.get("title") or {}).get("by") if isinstance(source.get("title"), dict) else None
    return entry.get("title") == legacy and by in (None, "human")


def protected_fields(entry: Optional[Mapping[str, Any]]) -> List[str]:
    """Fields a model never writes: a value whose source is human/agent, or whose source is unknown."""
    if not entry:
        return []
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    out = []
    for name in FIELDS:
        if name in entry:
            by = (source.get(name) or {}).get("by") if isinstance(source.get(name), dict) else None
            if by is None or by in PROTECTING:
                out.append(name)
    return out


# ---------------------------------------------------------------------------
# workspace: everything one write reads, changes in memory, and then commits once
# ---------------------------------------------------------------------------


def _bytes(doc: Any) -> bytes:
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _ordered_entry(entry: Mapping[str, Any]) -> Dict[str, Any]:
    out = {name: entry[name] for name in ENTRY_ORDER if name in entry}
    if isinstance(out.get("source"), dict):
        known = {name: out["source"][name] for name in FIELDS if name in out["source"]}
        out["source"] = {**known, **{k: v for k, v in out["source"].items() if k not in known}}
    out.update({k: v for k, v in entry.items() if k not in out})
    return out


def _ordered_meta(doc: Mapping[str, Any]) -> Dict[str, Any]:
    head = ("schema_version", "contract", "artifact_root_id", "repository_id", "campaign_id")
    out = {name: doc[name] for name in head if name in doc}
    out["campaign"] = _ordered_entry(doc.get("campaign") or {})
    out["cycles"] = {cid: _ordered_entry(entry) for cid, entry in sorted((doc.get("cycles") or {}).items())}
    out.update({k: v for k, v in doc.items() if k not in out})
    return out


@dataclass
class MetaDoc:
    campaign_id: str
    rel: str
    doc: Dict[str, Any]
    before_raw: Optional[bytes]
    changed: bool = False
    foreign: List[str] = field(default_factory=list)


class Workspace:
    def __init__(self, root: Path, *, now: Optional[float], actor_by: str, session: Optional[str],
                 reason: str, locked: bool) -> None:
        self.root = Path(root).resolve()
        self.now = now
        self.actor_by = actor_by
        self.session = session
        self.reason = reason
        self.locked = locked
        self.root_id, self.repo_id = _root_ids(self.root)
        self.txn = H.new_transaction_id()
        self.stamp = producer._rfc3339(now)
        self.members = _membership(self.root)
        self.events: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self.metas: Dict[str, MetaDoc] = {}
        self.invalid: Dict[str, str] = {}
        self.extra_targets: List[Tuple[str, Optional[bytes], bytes]] = []  # (rel, before, after)
        self.load_project()
        self.load_state()
        self.load_metas()
        self.reconcile_membership()
        self.reconcile_state()

    # -- loading ---------------------------------------------------------
    def load_project(self) -> None:
        read = read_project(self.root)
        if read.status == "invalid":
            raise MetaError("project-meta-invalid", read.code)
        self.project_raw = read.raw
        self.project_exists = read.status == "ok"
        self.project = json.loads(json.dumps(read.doc)) if read.doc else {
            "schema_version": 1, "contract": PROJECT_CONTRACT, "artifact_root_id": self.root_id,
            "display_name": self.default_display_name(), "branches": []}
        self.project_changed = False

    def default_display_name(self) -> str:
        name = self.root.name
        if name in (".agent_reports", ".claude_reports"):
            name = self.root.parent.name
        try:
            return check_title(unicodedata.normalize("NFC", name).strip())
        except MetaError:
            return "project"

    def load_state(self) -> None:
        raw = _read_raw(self.root, STATE_REL)
        self.state_raw = raw
        self.state_missing = raw is None
        if raw is None:
            self.state = new_state(self.root_id)
        else:
            self.state = _parse(raw)
            _validate_state(self.state, self.root_id)
            self.state = json.loads(json.dumps(self.state))

    def load_metas(self) -> None:
        self.campaign_dirs: Dict[str, Path] = {}
        base = artifact_locator.campaigns_dir(self.root)
        if not base.is_dir():
            return
        for directory in artifact_locator.iter_campaign_dirs(self.root):
            record = producer._read_json(directory / "campaign.json")
            campaign_id = record.get("campaign_id") if record else None
            if not isinstance(campaign_id, str) or not identity.is_well_formed(campaign_id, "campaign"):
                continue
            self.campaign_dirs[campaign_id] = directory
            self.load_meta(campaign_id, directory)

    def load_meta(self, campaign_id: str, directory: Optional[Path] = None) -> Optional[MetaDoc]:
        if campaign_id in self.metas:
            return self.metas[campaign_id]
        if directory is not None:
            rel = (directory / META_NAME).relative_to(self.root).as_posix()
        else:
            rel = self.campaign_location(campaign_id)
        raw = _read_raw(self.root, rel)
        if raw is None:
            return None
        try:
            doc = _parse(raw)
            foreign = _validate_meta_doc(doc, self.root_id, campaign_id, self.members,
                                         repository_id=self.repo_id)
        except MetaError as exc:
            self.warnings.append(f"{rel}: {exc.code}")
            self.invalid[campaign_id] = exc.code
            return None
        meta = MetaDoc(campaign_id, rel, json.loads(json.dumps(doc)), raw, foreign=foreign)
        self.metas[campaign_id] = meta
        return meta

    def campaign_location(self, campaign_id: str) -> str:
        return _campaign_location(self.root, campaign_id)[1]

    def require_meta(self, campaign_id: str, *, create: bool) -> MetaDoc:
        """The campaign's writable meta document; an invalid existing file is never replaced."""
        if campaign_id in self.invalid:
            raise MetaError("meta-invalid", self.invalid[campaign_id])
        meta = self.load_meta(campaign_id)
        if meta is None:
            if not create:
                raise MetaError("meta-missing", campaign_id)
            _campaign, rel = _campaign_location(self.root, campaign_id)
            doc = {"schema_version": 1, "contract": META_CONTRACT, "artifact_root_id": self.root_id,
                   "campaign_id": campaign_id, "campaign": {}, "cycles": {}}
            meta = MetaDoc(campaign_id, rel, doc, None)
            self.metas[campaign_id] = meta
        return meta

    def require_all_readable(self) -> None:
        if self.invalid:
            raise MetaError("meta-unreadable", ",".join(sorted(self.invalid)))

    # -- events ----------------------------------------------------------
    def event(self, *, kind: str, target_type: str, target_id: str, target_path: str, operation: str, field_path: str,
              before: Mapping[str, Any], after: Mapping[str, Any], reason: Optional[str] = None,
              actor_by: Optional[str] = None) -> None:
        self.events.append(H.make_event(
            kind=kind, target_type=target_type, target_id=target_id, target_path=target_path, operation=operation,
            field=field_path, before=before, after=after, reason=reason or self.reason,
            actor_by=actor_by or self.actor_by, transaction_id=self.txn, session=self.session, now=self.now))

    # -- generic entry access -------------------------------------------
    def entry(self, meta: MetaDoc, cycle_id: Optional[str], *, create: bool) -> Optional[Dict[str, Any]]:
        if cycle_id is None:
            return meta.doc.setdefault("campaign", {})
        cycles = meta.doc.setdefault("cycles", {})
        if cycle_id not in cycles and create:
            cycles[cycle_id] = {}
        return cycles.get(cycle_id)

    def put(self, meta: MetaDoc, cycle_id: Optional[str], name: str, value: Any, *, by: Optional[str],
            reason: Optional[str] = None, event_by: Optional[str] = None) -> bool:
        """Set one field and its source; no change (no source, no event) when the value is the same.
        `by=None` leaves the source untouched (a value rewritten by a vocabulary rename)."""
        entry = self.entry(meta, cycle_id, create=True)
        assert entry is not None
        before = entry.get(name)
        if name in entry and before == value:
            return False
        entry[name] = value
        if by is not None:
            entry.setdefault("source", {})[name] = {"by": by, "at": self.stamp}
        meta.changed = True
        path = "campaign." + name if cycle_id is None else f"cycles.{cycle_id}.{name}"
        self.event(kind="meta", target_type="campaign" if cycle_id is None else "cycle",
                   target_id=meta.campaign_id if cycle_id is None else cycle_id, target_path=meta.rel,
                   operation="add" if before is None else "update", field_path=path,
                   before=H.value_ref(before), after=H.value_ref(value), reason=reason, actor_by=event_by)
        return True

    def put_source_only(self, meta: MetaDoc, cycle_id: Optional[str], name: str, by: str,
                        reason: Optional[str] = None) -> bool:
        entry = self.entry(meta, cycle_id, create=False)
        if entry is None or name not in entry:
            return False
        source = entry.setdefault("source", {})
        before = source.get(name)
        after = {"by": by, "at": self.stamp}
        if before is not None and before.get("by") == by:
            return False
        source[name] = after
        meta.changed = True
        path = f"campaign.source.{name}" if cycle_id is None else f"cycles.{cycle_id}.source.{name}"
        self.event(kind="meta", target_type="campaign" if cycle_id is None else "cycle",
                   target_id=meta.campaign_id if cycle_id is None else cycle_id, target_path=meta.rel,
                   operation="update", field_path=path, before=H.value_ref(before), after=H.value_ref(after),
                   reason=reason)
        return True

    # -- vocabulary ------------------------------------------------------
    @property
    def branches(self) -> List[Dict[str, str]]:
        return self.project["branches"]

    def vocab_codes(self) -> List[str]:
        return [item["code"] for item in self.branches]

    def general_count(self) -> int:
        return sum(1 for item in self.branches if item["code"] != ETC)

    def branch(self, code: str) -> Optional[Dict[str, str]]:
        return next((item for item in self.branches if item["code"] == code), None)

    def project_event(self, operation: str, name: str, before: Any, after: Any, reason: Optional[str] = None,
                      actor_by: Optional[str] = None) -> None:
        self.event(kind="meta", target_type="project", target_id=self.root_id, target_path=PROJECT_REL,
                   operation=operation, field_path=name, before=H.value_ref(before), after=H.value_ref(after),
                   reason=reason, actor_by=actor_by)

    def mark_project_source(self, key: str, by: str) -> None:
        self.state["project_source"][key] = {"by": by, "at": self.stamp}

    def add_branch(self, item: Mapping[str, str], *, by: str, reason: Optional[str] = None) -> bool:
        """Add a vocabulary entry; the same code with the same label is a no-op, another label a conflict."""
        item = check_branch_def(dict(item))
        existing = self.branch(item["code"])
        if existing is not None:
            if existing["label"] != item["label"]:
                raise MetaError("branch-code-conflict", item["code"])
            return False
        if item["code"] != ETC and self.general_count() >= GENERAL_BRANCH_MAX:
            raise MetaError("branch-limit", str(GENERAL_BRANCH_MAX))
        self.branches.append(item)
        self.project_changed = True
        self.mark_project_source("branches." + item["code"], by)
        self.project_event("add", "branches." + item["code"], None, item, reason)
        return True

    def ensure_etc(self) -> None:
        if self.branch(ETC) is None:
            self.add_branch({"code": ETC, "label": ETC_LABEL, "note": ETC_NOTE}, by="rule",
                            reason="어휘 상한 또는 맞는 갈래 없음에 따른 예약 갈래")

    # -- ID numbers ------------------------------------------------------
    def issue(self, short_id: str, kind: str, owner: str, campaign_id: str) -> None:
        """Record an ID as spent for good; another owner's ID is refused."""
        row = self.state["issued"].get(short_id)
        if row is not None and (row["kind"], row["id"]) != (kind, owner):
            raise MetaError("id-reserved", short_id)
        self.state["issued"][short_id] = {"kind": kind, "id": owner}
        self._raise_water(short_id, kind, campaign_id)

    def _raise_water(self, short_id: str, kind: str, campaign_id: Optional[str]) -> None:
        if kind == "campaign":
            code, number = CAMPAIGN_SHORT.fullmatch(short_id).group(1, 2)  # type: ignore[union-attr]
            water = self.state["branch_high_water"]
            water[code] = max(water.get(code, 0), int(number))
        elif campaign_id:
            water = self.state["cycle_high_water"]
            water[campaign_id] = max(water.get(campaign_id, 0),
                                     int(CYCLE_SHORT.fullmatch(short_id).group(3)))  # type: ignore[union-attr]

    def next_campaign_number(self, code: str) -> int:
        number = self.state["branch_high_water"].get(code, 0) + 1
        while f"{code}-{number:02d}" in self.state["issued"]:
            number += 1
        return number

    def next_cycle_number(self, campaign_id: str) -> int:
        return self.state["cycle_high_water"].get(campaign_id, 0) + 1

    # -- reconciliation (idempotent convergence) -------------------------
    def reconcile_membership(self) -> None:
        """A cycle entry whose cycle now belongs to another campaign moves with it; one with no record is dropped."""
        self.moved: List[Tuple[str, str, str]] = []
        for campaign_id, meta in list(self.metas.items()):
            for cycle_id in list(meta.foreign):
                current = (self.members.get(cycle_id) or {}).get("campaign_id")
                entry = (meta.doc.get("cycles") or {}).pop(cycle_id)
                meta.changed = True
                if current in self.campaign_dirs and current != campaign_id:
                    self.moved.append((campaign_id, current, cycle_id))
                    self._receive_cycle(current, cycle_id, entry, campaign_id, meta)
                else:
                    self.event(kind="meta", target_type="cycle", target_id=cycle_id, target_path=meta.rel,
                               operation="delete", field_path=f"cycles.{cycle_id}", before=H.value_ref(entry),
                               after=H.value_ref(None), reason="사이클 기록이 없어 메타데이터 항목을 정리함",
                               actor_by="rule")
            meta.foreign = []

    def _receive_cycle(self, campaign_id: str, cycle_id: str, entry: Dict[str, Any], from_campaign: str,
                       from_meta: MetaDoc) -> None:
        meta = self.require_meta(campaign_id, create=True)
        moved = {k: v for k, v in entry.items() if k not in ("short_id", "aliases")}
        aliases = list(entry.get("aliases") or [])
        if entry.get("short_id"):
            aliases.append(entry["short_id"])
        moved["aliases"] = aliases[-LIST_MAX:]
        meta.doc.setdefault("cycles", {})[cycle_id] = moved
        meta.changed = True
        self.event(kind="meta", target_type="cycle", target_id=cycle_id, target_path=meta.rel, operation="move",
                   field_path=f"cycles.{cycle_id}", before=H.value_ref({"campaign_id": from_campaign}),
                   after=H.value_ref({"campaign_id": campaign_id}),
                   reason="사이클 소속이 바뀌어 메타데이터 항목을 옮김", actor_by="rule")
        self.normalize_ids(campaign_id)

    def reconcile_state(self) -> None:
        """Make the internal state cover every ID the public files already carry.

        A state file that is missing is rebuilt from those files and, for IDs no longer in
        any file, from the history; a state that disagrees with the files only ever grows."""
        issued = self.state["issued"]
        if self.state_missing:
            for event in H.iter_events(self.root):
                kind = {"campaign": "campaign", "cycle": "cycle"}.get(event["target"]["type"])
                if kind is None or event["kind"] != "meta" or not re.search(r"(^|\.)(short_id|aliases)$", event["field"]):
                    continue
                pattern = CYCLE_SHORT if kind == "cycle" else CAMPAIGN_SHORT
                for ref in (event["before"], event["after"]):
                    value = ref.get("value")
                    for item in value if isinstance(value, list) else [value]:
                        if isinstance(item, str) and pattern.fullmatch(item):
                            issued.setdefault(item, {"kind": kind, "id": event["target"]["id"]})
                            self._raise_water(item, kind, (self.members.get(event["target"]["id"]) or {}).get("campaign_id"))
        for campaign_id, meta in self.metas.items():
            for cycle_id, entry in [(None, meta.doc.get("campaign") or {}),
                                    *sorted((meta.doc.get("cycles") or {}).items())]:
                kind, owner = ("campaign", campaign_id) if cycle_id is None else ("cycle", cycle_id)
                for short_id in [entry.get("short_id"), *(entry.get("aliases") or [])]:
                    if not short_id:
                        continue
                    row = issued.get(short_id)
                    if row is not None and (row["kind"], row["id"]) != (kind, owner):
                        raise MetaError("id-conflict", short_id)
                    issued[short_id] = {"kind": kind, "id": owner}
                    self._raise_water(short_id, kind, campaign_id)

    # -- ID derivation ---------------------------------------------------
    def change_short_id(self, meta: MetaDoc, cycle_id: Optional[str], new: str, *, by: str,
                        reason: Optional[str] = None, event_by: Optional[str] = None) -> bool:
        """Move an entry to a new short ID; the old one becomes an alias and is never reissued."""
        entry = self.entry(meta, cycle_id, create=True)
        assert entry is not None
        old = entry.get("short_id")
        if old == new:
            return False
        owner_kind, owner = ("campaign", meta.campaign_id) if cycle_id is None else ("cycle", cycle_id)
        self.issue(new, owner_kind, owner, meta.campaign_id)
        aliases = [item for item in entry.get("aliases", []) if item != new]
        if old and old not in aliases:
            aliases.append(old)
        aliases = aliases[-LIST_MAX:]
        self.put(meta, cycle_id, "short_id", new, by=by, reason=reason, event_by=event_by)
        if aliases != entry.get("aliases", []):
            self.put(meta, cycle_id, "aliases", aliases, by=None, reason=reason, event_by=event_by)
        return True

    def normalize_ids(self, campaign_id: str, *, force: bool = False) -> None:
        """Give the campaign and its cycles the IDs the representative branch implies."""
        meta = self.metas.get(campaign_id)
        if meta is None:
            return
        entry = meta.doc.get("campaign") or {}
        branches = entry.get("branches") or []
        if branches:
            code = branches[0]
            current = entry.get("short_id")
            locked = "short_id" in protected_fields(entry)
            if current is None or (CAMPAIGN_SHORT.fullmatch(current).group(1) != code  # type: ignore[union-attr]
                                   and (force or not locked)):
                new = f"{code}-{self.next_campaign_number(code):02d}"
                self.change_short_id(meta, None, new, by="rule", reason="대표 갈래에 맞춘 짧은 ID 부여",
                                     event_by="rule")
        campaign_short = (meta.doc.get("campaign") or {}).get("short_id")
        if not campaign_short:
            return
        cycles = meta.doc.get("cycles") or {}
        fresh = sorted((cid for cid, e in cycles.items() if not e.get("short_id")),
                       key=lambda cid: (str((self.members.get(cid) or {}).get("started_on") or ""), cid))
        for cycle_id, cycle_entry in sorted(cycles.items()):
            short = cycle_entry.get("short_id")
            if short and short.rsplit(".", 1)[0] != campaign_short:
                suffix = short.rsplit(".", 1)[1]
                self.change_short_id(meta, cycle_id, f"{campaign_short}.{suffix}", by="rule",
                                     reason="캠페인 ID 변경에 맞춘 사이클 ID 갱신", event_by="rule")
        for cycle_id in fresh:
            number = self.next_cycle_number(campaign_id)
            self.change_short_id(meta, cycle_id, f"{campaign_short}.{number}", by="rule",
                                 reason="사이클 짧은 ID 부여", event_by="rule")

    # -- result ----------------------------------------------------------
    def diff(self) -> List[Dict[str, Any]]:
        return [{"kind": e["kind"], "target": e["target"], "operation": e["operation"], "field": e["field"],
                 "before": e["before"], "after": e["after"], "actor": e["actor"]["by"]} for e in self.events]


# ---------------------------------------------------------------------------
# commit: one write-ahead intent, then the replacements, then the events
# ---------------------------------------------------------------------------


def _digest(raw: Optional[bytes]) -> Optional[str]:
    return None if raw is None else "sha256:" + hashlib.sha256(raw).hexdigest()


def _intent_path(root: Path, txn: str) -> Path:
    return root / H.STAGING_REL / f"{txn}.json"


def pending_intents(root: Path) -> List[str]:
    try:
        return sorted(p.name for p in (Path(root) / H.STAGING_REL).glob("htxn_*.json"))
    except OSError:
        return []


def _targets(ws: Workspace) -> List[Tuple[str, Optional[bytes], bytes, int]]:
    rows: List[Tuple[str, Optional[bytes], bytes, int]] = []
    for meta in ws.metas.values():
        if meta.changed:
            rows.append((meta.rel, meta.before_raw, _bytes(_ordered_meta(meta.doc)), 0o644))
    if ws.project_changed:
        project = {k: ws.project[k] for k in ("schema_version", "contract", "artifact_root_id", "display_name", "branches")}
        project.update({k: v for k, v in ws.project.items() if k not in project})
        rows.append((PROJECT_REL, ws.project_raw, _bytes(project), 0o644))
    for rel, before, after in ws.extra_targets:
        rows.append((rel, before, after, 0o644))
    state_after = _bytes(ws.state)
    if ws.state_raw != state_after and (rows or ws.state["issued"] or ws.state["project_source"]):
        rows.append((STATE_REL, ws.state_raw, state_after, 0o600))
    return sorted(rows, key=lambda row: (row[0] == STATE_REL, row[0]))


def commit(ws: Workspace) -> Dict[str, Any]:
    """Write every changed file and publish the events once; the caller holds the admission lock."""
    rows = _targets(ws)
    if not rows:
        return {"status": "no-change", "files": [], "events": 0, "history": "none"}
    modes = {rel: mode for rel, _before, _after, mode in rows}
    intent = {"schema": INTENT_SCHEMA, "transaction_id": ws.txn, "events": ws.events,
              "targets": [{"path": rel, "mode": modes[rel], "before": _digest(before),
                           "after": base64.b64encode(after).decode("ascii")} for rel, before, after, _m in rows]}
    path = _intent_path(ws.root, ws.txn)
    path.parent.mkdir(parents=True, exist_ok=True)
    producer._write_atomic(path, _bytes(intent), 0o600)  # the commit point
    outcome = _finish_intent(ws.root, path, intent)
    return {"status": "applied", "files": [rel for rel, *_ in rows], "events": len(ws.events),
            "history": outcome["history"]}


def _finish_intent(root: Path, path: Path, intent: Mapping[str, Any]) -> Dict[str, str]:
    """Replace what is still at its old digest, then publish the events and drop the intent.

    Every target is first checked to be at its old or new digest; anything else is somebody
    else's file, so nothing is replaced and the intent is dropped (`intent-conflict`).
    Re-running a finished step changes nothing."""
    plan = []
    for target in intent["targets"]:
        after = base64.b64decode(target["after"])
        current = _digest(_read_raw(root, target["path"]))
        if current not in (target["before"], _digest(after)):
            _drop_intent(path)
            raise MetaError("intent-conflict", target["path"])
        plan.append((target, after, current))
    for target, after, current in plan:
        if current != _digest(after):
            full = root / target["path"]
            full.parent.mkdir(parents=True, exist_ok=True)
            producer._write_atomic(full, after, target.get("mode", 0o644))
    try:
        H.publish_events_locked(root, intent["events"])
    except (H.HistoryError, OSError):
        return {"history": "pending"}  # the next write publishes them; nothing is rolled back
    _drop_intent(path)
    return {"history": "published"}


def _drop_intent(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def recover_locked(root: Path) -> List[Dict[str, str]]:
    """Finish every intent a stopped write left behind; the caller holds the admission lock."""
    results = []
    for name in pending_intents(root):
        path = Path(root) / H.STAGING_REL / name
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
            if intent.get("schema") != INTENT_SCHEMA:
                raise ValueError("schema")
            results.append({"intent": name, **_finish_intent(root, path, intent)})
        except MetaError as exc:
            results.append({"intent": name, "history": "none", "conflict": exc.code})
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            results.append({"intent": name, "history": "none", "conflict": "intent-unreadable"})
    return results


def run_write(root: Path, mutate: Callable[[Workspace], Any], *, dry_run: bool = False, actor_by: str = "human",
              session: Optional[str] = None, reason: str = "요청에 따른 메타데이터 수정", now: Optional[float] = None,
              lock_timeout: Optional[float] = None) -> Dict[str, Any]:
    """The one write path.  `--dry-run` takes no lock, recovers nothing, and writes nothing."""
    root = Path(root).resolve()
    if dry_run:
        ws = Workspace(root, now=now, actor_by=actor_by, session=session, reason=reason, locked=False)
        result = mutate(ws)
        return {"status": "dry-run", "files": [row[0] for row in _targets(ws)], "changes": ws.diff(),
                "pending_recovery": len(pending_intents(root)), "warnings": ws.warnings, "result": result}
    try:
        lock = admission._acquire_lock(
            root, admission.LOCK_TIMEOUT_DEFAULT if lock_timeout is None else lock_timeout)
    except admission.AdmissionBusy as exc:
        raise MetaError("admission-busy") from exc
    try:
        recovered = recover_locked(root)
        ws = Workspace(root, now=now, actor_by=actor_by, session=session, reason=reason, locked=True)
        result = mutate(ws)
        outcome = commit(ws)
    finally:
        admission._release_lock(root, lock)
    return {**outcome, "recovered": recovered, "warnings": ws.warnings, "result": result,
            "changes": ws.diff()}


# ---------------------------------------------------------------------------
# operations (each runs inside `run_write`)
# ---------------------------------------------------------------------------


def resolve(ws: Workspace, token: str, kind: str) -> str:
    """A stable ID, or a short ID / alias that the issued table maps to exactly one owner."""
    if identity.is_well_formed(token, kind):
        return token
    row = ws.state["issued"].get(token)
    if row is None or row["kind"] != kind:
        raise MetaError(f"{kind}-unknown", str(token)[:40])
    return row["id"]


def _target(ws: Workspace, campaign: str, cycle: Optional[str]) -> Tuple[str, Optional[str]]:
    campaign_id = resolve(ws, campaign, "campaign")
    cycle_id = None if cycle is None else resolve(ws, cycle, "cycle")
    if campaign_id not in ws.campaign_dirs:
        raise MetaError("campaign-unknown", campaign_id)
    if cycle_id is not None and (ws.members.get(cycle_id) or {}).get("campaign_id") != campaign_id:
        raise MetaError("cycle-not-member", cycle_id)
    return campaign_id, cycle_id


def op_set(ws: Workspace, campaign: str, cycle: Optional[str], values: Mapping[str, Any]) -> Dict[str, Any]:
    if not values:
        raise MetaError("nothing-to-set")
    campaign_id, cycle_id = _target(ws, campaign, cycle)
    if "presentation_kind" in values and cycle_id is not None:
        raise MetaError("presentation-cycle-not-allowed")
    if "presentation_kind" in values and ws.actor_by not in PROTECTING:
        # The background model judgement never creates the archive mark; only
        # an explicit human/agent `set --presentation-kind` with evidence may.
        raise MetaError("presentation-model-forbidden")
    if "presentation_kind" in values and values["presentation_kind"] not in PRESENTATION_KINDS:
        raise MetaError("presentation-kind-invalid", str(values["presentation_kind"])[:40])
    meta = ws.require_meta(campaign_id, create=True)
    by = ws.actor_by if ws.actor_by in PROTECTING else "human"
    if "presentation_kind" in values:
        # Exact binding: the mark always carries this root/repository/campaign.
        existing_repo = meta.doc.get("repository_id")
        if existing_repo is None:
            meta.doc["repository_id"] = ws.repo_id
            meta.changed = True
        elif existing_repo != ws.repo_id:
            raise MetaError("presentation-repository-mismatch", str(existing_repo)[:40])
        ws.put(meta, None, "presentation_kind", values["presentation_kind"], by=by)
    if "title" in values:
        check_title(values["title"])
    if "summary" in values:
        check_summary(values["summary"])
    if "kinds" in values:
        check_kinds(values["kinds"])
    if "title" in values:
        check_title(values["title"])
    if "summary" in values:
        check_summary(values["summary"])
    if "kinds" in values:
        check_kinds(values["kinds"])
    if "branches" in values:
        known = set(ws.vocab_codes()) | {ETC}
        for code in check_branches(values["branches"]):
            if code not in known:
                raise MetaError("branch-unknown", code)
        if not values["branches"]:
            raise MetaError("branches-invalid", "empty")
    if "branches" in values and ETC in values["branches"]:
        ws.ensure_etc()
    for name in ("title", "summary", "kinds"):
        if name in values:
            if not ws.put(meta, cycle_id, name, values[name], by=by) and by in PROTECTING:
                ws.put_source_only(meta, cycle_id, name, by)
    if "branches" in values:
        before = (ws.entry(meta, cycle_id, create=True) or {}).get("branches")
        if not ws.put(meta, cycle_id, "branches", values["branches"], by=by) and by in PROTECTING:
            ws.put_source_only(meta, cycle_id, "branches", by)
        if cycle_id is None and "short_id" not in values and before != values["branches"]:
            ws.normalize_ids(campaign_id, force=True)
    if "short_id" in values:
        _manual_short_id(ws, meta, cycle_id, values["short_id"], by)
    ws.normalize_ids(campaign_id)
    return {"campaign_id": campaign_id, "cycle_id": cycle_id}


def _manual_short_id(ws: Workspace, meta: MetaDoc, cycle_id: Optional[str], value: Any, by: str) -> None:
    short = check_short_id(value, cycle=cycle_id is not None)
    campaign_entry = meta.doc.get("campaign") or {}
    if cycle_id is None:
        branches = campaign_entry.get("branches") or []
        if not branches or CAMPAIGN_SHORT.fullmatch(short).group(1) != branches[0]:  # type: ignore[union-attr]
            raise MetaError("short-id-branch-mismatch", short)
    else:
        parent = campaign_entry.get("short_id")
        if not parent or short.rsplit(".", 1)[0] != parent:
            raise MetaError("short-id-parent-mismatch", short)
    owner_kind, owner = ("campaign", meta.campaign_id) if cycle_id is None else ("cycle", cycle_id)
    row = ws.state["issued"].get(short)
    if row is not None and (row["kind"], row["id"]) != (owner_kind, owner):
        raise MetaError("id-reserved", short)
    if not ws.change_short_id(meta, cycle_id, short, by=by):
        ws.put_source_only(meta, cycle_id, "short_id", by)
        return
    if cycle_id is None:
        ws.normalize_ids(meta.campaign_id)


def op_release(ws: Workspace, campaign: str, cycle: Optional[str], names: Sequence[str]) -> Dict[str, Any]:
    if not names or any(name not in FIELDS for name in names):
        raise MetaError("field-unknown")
    campaign_id, cycle_id = _target(ws, campaign, cycle)
    meta = ws.require_meta(campaign_id, create=False)
    released = []
    for name in names:
        by = "rule" if name == "short_id" else "model"
        if ws.put_source_only(meta, cycle_id, name, by, reason="모델에게 다시 맡김(사람 지정 해제)"):
            released.append(name)
    return {"campaign_id": campaign_id, "cycle_id": cycle_id, "released": released}


def op_set_project(ws: Workspace, display_name: str) -> Dict[str, Any]:
    check_title(display_name)
    if ws.project_exists and ws.project["display_name"] == display_name:
        return {"display_name": display_name}
    before = ws.project["display_name"] if ws.project_exists else None
    ws.project["display_name"] = display_name
    ws.project_changed = True
    ws.mark_project_source("display_name", ws.actor_by if ws.actor_by in PROTECTING else "human")
    ws.project_event("update" if before is not None else "add", "display_name", before, display_name)
    return {"display_name": display_name}


def op_branch_add(ws: Workspace, code: str, label: str, note: str) -> Dict[str, Any]:
    by = ws.actor_by if ws.actor_by in PROTECTING else "human"
    added = ws.add_branch({"code": code, "label": label, "note": note}, by=by)
    return {"code": code, "added": added}


def op_branch_import(ws: Workspace, seed: Any) -> Dict[str, Any]:
    if not isinstance(seed, dict) or set(seed) != {"branches"} or not isinstance(seed["branches"], list):
        raise MetaError("seed-invalid")
    items = [check_branch_def(item) for item in seed["branches"]]
    if len({item["code"] for item in items}) != len(items):
        raise MetaError("seed-invalid", "duplicate")
    by = ws.actor_by if ws.actor_by in PROTECTING else "human"
    return {"added": [item["code"] for item in items if ws.add_branch(item, by=by)]}


def _rewrite_branches(ws: Workspace, mapping: Mapping[str, str], reason: str) -> List[str]:
    """Replace or drop branch codes in every campaign and cycle entry; sources stay as they were."""
    ws.require_all_readable()
    touched = []
    for campaign_id, meta in ws.metas.items():
        rep_before = ((meta.doc.get("campaign") or {}).get("branches") or [None])[0]
        for cycle_id, entry in [(None, meta.doc.get("campaign") or {}), *sorted((meta.doc.get("cycles") or {}).items())]:
            branches = entry.get("branches")
            if not branches or not any(code in mapping for code in branches):
                continue
            new: List[str] = []
            for code in branches:
                code = mapping.get(code, code)
                if code not in new:
                    new.append(code)
            ws.put(meta, cycle_id, "branches", new, by=None, reason=reason)
        if rep_before is not None and rep_before in mapping:
            touched.append(campaign_id)
    for campaign_id in touched:
        ws.normalize_ids(campaign_id, force=True)
    return touched


def op_branch_rename(ws: Workspace, code: str, new_code: Optional[str], label: Optional[str],
                     note: Optional[str]) -> Dict[str, Any]:
    check_code(code)
    item = ws.branch(code)
    if item is None:
        raise MetaError("branch-unknown", code)
    if code == ETC and new_code:
        raise MetaError("branch-reserved", ETC)
    if label is None and note is None and not new_code:
        raise MetaError("nothing-to-set")
    by = ws.actor_by if ws.actor_by in PROTECTING else "human"
    before = dict(item)
    updated = dict(item)
    if label is not None:
        updated["label"] = check_text(label, LABEL_MAX, "branch-label-invalid")
    if note is not None:
        updated["note"] = check_text(note, NOTE_MAX, "branch-note-invalid", allow_empty=True)
    if new_code:
        check_code(new_code)
        if new_code == ETC or ws.branch(new_code) is not None:
            raise MetaError("branch-code-conflict", new_code)
        updated["code"] = new_code
    if updated == before:
        return {"code": code, "changed": False}
    item.update(updated)
    ws.project_changed = True
    if new_code:
        ws.mark_project_source("branches." + new_code, by)
        ws.state["project_source"].pop("branches." + code, None)
        ws.project_event("move", "branches." + new_code, before, updated)
        _rewrite_branches(ws, {code: new_code}, f"갈래 코드 {code}를 {new_code}로 바꿈")
    else:
        ws.mark_project_source("branches." + code, by)
        ws.project_event("update", "branches." + code, before, updated)
    return {"code": new_code or code, "changed": True}


def op_branch_merge(ws: Workspace, old: str, into: str) -> Dict[str, Any]:
    check_code(old)
    check_code(into)
    if old == into or old == ETC:
        raise MetaError("branch-merge-invalid")
    item = ws.branch(old)
    if item is None or ws.branch(into) is None:
        raise MetaError("branch-unknown", old if item is None else into)
    ws.branches.remove(item)
    ws.project_changed = True
    ws.state["project_source"].pop("branches." + old, None)
    ws.project_event("delete", "branches." + old, item, None, f"갈래 {old}를 {into}로 합침")
    touched = _rewrite_branches(ws, {old: into}, f"갈래 {old}를 {into}로 합침")
    return {"merged": old, "into": into, "campaigns_reidentified": touched}


def op_branch_remove(ws: Workspace, code: str) -> Dict[str, Any]:
    check_code(code)
    if code == ETC:
        raise MetaError("branch-reserved", ETC)
    item = ws.branch(code)
    if item is None:
        raise MetaError("branch-unknown", code)
    ws.require_all_readable()
    for meta in ws.metas.values():
        entries = [meta.doc.get("campaign") or {}, *(meta.doc.get("cycles") or {}).values()]
        if any(code in (entry.get("branches") or []) for entry in entries):
            raise MetaError("branch-in-use", code)
    ws.branches.remove(item)
    ws.project_changed = True
    ws.state["project_source"].pop("branches." + code, None)
    ws.project_event("delete", "branches." + code, item, None)
    return {"removed": code}


# -- the background judgement's write ---------------------------------------


def _group_events(ws: Workspace, ready: Mapping[str, Any]) -> None:
    def groups(raw: Optional[bytes]) -> Dict[str, Any]:
        doc = json.loads(raw.decode("utf-8")) if raw else {}
        return {row["group_id"]: row for row in doc.get("groups", [])}

    rel = Path(ready["path"]).relative_to(ws.root).as_posix()
    old, new = groups(ready["before_raw"]), groups(ready["after_raw"])
    for group_id, row in new.items():
        if old.get(group_id) != row:
            ws.event(kind="group", target_type="group", target_id=group_id, target_path=rel,
                     operation="add" if group_id not in old else "update", field_path=f"groups.{group_id}",
                     before=H.value_ref(old.get(group_id)), after=H.value_ref(row),
                     reason="새 사이클 묶음 판정", actor_by="model")
    ws.extra_targets.append((rel, ready["before_raw"], ready["after_raw"]))


def op_judgement(ws: Workspace, campaign_id: str, *, campaign: Mapping[str, Any], cycles: Mapping[str, Mapping[str, Any]],
                 new_branches: Sequence[Mapping[str, str]], group_plan: Optional[Mapping[str, Any]],
                 protect_title: bool, replace_legacy_titles: bool = False) -> Dict[str, Any]:
    """Merge one validated model answer into the current files: protected fields, the vocabulary,
    and the 12-branch cap are judged here, against what is on disk now."""
    if campaign_id not in ws.campaign_dirs:
        raise MetaError("campaign-unknown", campaign_id)
    for cycle_id in cycles:
        if (ws.members.get(cycle_id) or {}).get("campaign_id") != campaign_id:
            raise MetaError("cycle-not-member", cycle_id)
    meta = ws.require_meta(campaign_id, create=True)
    allowed = 1 if ws.general_count() else GENERAL_BRANCH_MAX
    added = 0
    to_etc: set = set()  # a proposed code the cap kept out of the vocabulary
    for raw in new_branches:
        item = check_branch_def(dict(raw))
        if item["code"] == ETC or ws.branch(item["code"]) is not None:
            continue
        if added >= allowed or ws.general_count() >= GENERAL_BRANCH_MAX:
            to_etc.add(item["code"])
            continue
        ws.add_branch(item, by="model", reason="모델이 제안한 새 갈래")
        added += 1
    known = set(ws.vocab_codes()) | {ETC}

    def branches_of(codes: Sequence[str]) -> List[str]:
        out: List[str] = []
        for code in codes:
            code = ETC if code in to_etc or code not in known else code
            if code not in out:
                out.append(code)
        return out

    legacy, legacy_present = legacy_title(ws.root, campaign_id)
    campaign_entry = ws.entry(meta, None, create=True) or {}
    legacy_start = replace_legacy_titles and bool(legacy) and legacy_title_replaceable(campaign_entry, legacy)
    if "title" not in campaign_entry and legacy and not protect_title:
        if replace_legacy_titles:
            # An explicit, supervised backfill renews old declaration titles (DESIGN §10): start from
            # the old title so the history keeps it, and let the model's plain title replace it.
            ws.put(meta, None, "title", legacy, by="model", reason="기존 표시 선언의 제목에서 시작함(소급 실행)",
                   event_by="rule")
        else:
            ws.put(meta, None, "title", legacy, by="human", reason="기존 표시 선언의 제목을 사람 값으로 보존함",
                   event_by="rule")
    elif legacy_start and not protect_title:
        ws.put_source_only(meta, None, "title", "model", reason="기존 표시 선언의 제목에서 시작함(소급 실행)")
    for cycle_id, proposal in [(None, campaign), *sorted(cycles.items())]:
        current = ws.entry(meta, cycle_id, create=cycle_id is not None) or {}
        locked = set(protected_fields(current))
        if cycle_id is None and (protect_title or (legacy_present and "title" not in current
                                                   and not legacy_start)):
            locked.add("title")
        wanted = {"title": proposal["title"], "summary": proposal["summary"],
                  "branches": branches_of(proposal["branches"]), "kinds": list(proposal["kinds"])}
        if cycle_id is None and "branches" not in locked and current.get("branches") \
                and wanted["branches"][0] != current["branches"][0]:
            dependents = list((meta.doc.get("cycles") or {}).values())
            if "short_id" in locked or any("short_id" in protected_fields(e) for e in dependents):
                locked.add("branches")  # the representative branch would renumber an ID a person fixed
        if ETC in wanted["branches"] and "branches" not in locked:
            ws.ensure_etc()
        for name in ("title", "summary", "branches", "kinds"):
            if name not in locked:
                ws.put(meta, cycle_id, name, wanted[name], by="model")
    ws.normalize_ids(campaign_id)
    group: Dict[str, Any] = {"status": "none"}
    if group_plan is not None:
        if not ws.locked:
            raise MetaError("admission-lock-required")
        ready = W._validated_apply_locked(ws.root, group_plan)
        group = {"status": ready["status"], "sha256": ready["sha256"]}
        if ready["status"] == "ready":
            _group_events(ws, ready)
    return {"campaign_id": campaign_id, "group": group}


def apply_judgement(root: Path, campaign_id: str, *, campaign: Mapping[str, Any],
                    cycles: Mapping[str, Mapping[str, Any]], new_branches: Sequence[Mapping[str, str]] = (),
                    group_plan: Optional[Mapping[str, Any]] = None, protect_title: bool = False,
                    replace_legacy_titles: bool = False,
                    dry_run: bool = False, now: Optional[float] = None, lock_timeout: Optional[float] = None
                    ) -> Dict[str, Any]:
    def mutate(ws: Workspace) -> Dict[str, Any]:
        return op_judgement(ws, campaign_id, campaign=campaign, cycles=cycles, new_branches=new_branches,
                            group_plan=None if dry_run else group_plan, protect_title=protect_title,
                            replace_legacy_titles=replace_legacy_titles)

    return run_write(root, mutate, dry_run=dry_run, actor_by="model", now=now, lock_timeout=lock_timeout,
                     reason="백그라운드 판정")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # JSON typed code, never a traceback
        print(json.dumps({"status": "blocked", "code": "usage", "detail": message}, ensure_ascii=False))
        raise SystemExit(65)


def _csv(value: str) -> List[str]:
    return [item for item in value.split(",") if item] if value else []


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Read and change campaign/cycle metadata and the project vocabulary.",
                     allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def common(p: argparse.ArgumentParser, *, write: bool) -> None:
        p.add_argument("--artifact-root", required=True)
        if write:
            p.add_argument("--by", choices=("human", "agent"), default="human")
            p.add_argument("--reason")
            p.add_argument("--session")
            p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("show", allow_abbrev=False)
    common(p, write=False)
    p.add_argument("--project", action="store_true")
    p.add_argument("--campaign")
    p.add_argument("--cycle")
    p = sub.add_parser("set", allow_abbrev=False)
    common(p, write=True)
    p.add_argument("--project", action="store_true")
    p.add_argument("--display-name")
    p.add_argument("--campaign")
    p.add_argument("--cycle")
    p.add_argument("--title")
    p.add_argument("--summary")
    p.add_argument("--branches")
    p.add_argument("--kinds")
    p.add_argument("--short-id")
    p.add_argument("--presentation-kind", choices=("archive_bundle",))
    p = sub.add_parser("release", allow_abbrev=False)
    common(p, write=True)
    p.add_argument("--campaign", required=True)
    p.add_argument("--cycle")
    p.add_argument("--field", action="append", required=True)
    p = sub.add_parser("branches", allow_abbrev=False)
    action = p.add_subparsers(dest="action", required=True, parser_class=_Parser)
    q = action.add_parser("list", allow_abbrev=False)
    common(q, write=False)
    q = action.add_parser("add", allow_abbrev=False)
    common(q, write=True)
    q.add_argument("--code", required=True)
    q.add_argument("--label", required=True)
    q.add_argument("--note", default="")
    q = action.add_parser("import", allow_abbrev=False)
    common(q, write=True)
    q.add_argument("--input", required=True)
    q = action.add_parser("rename", allow_abbrev=False)
    common(q, write=True)
    q.add_argument("--code", required=True)
    q.add_argument("--new-code")
    q.add_argument("--label")
    q.add_argument("--note")
    q = action.add_parser("merge", allow_abbrev=False)
    common(q, write=True)
    q.add_argument("--from", dest="old", required=True)
    q.add_argument("--into", required=True)
    q = action.add_parser("remove", allow_abbrev=False)
    common(q, write=True)
    q.add_argument("--code", required=True)
    return parser


def _show(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    ws = Workspace(root, now=None, actor_by="human", session=None, reason="show", locked=False)
    pending = len(pending_intents(root))
    if getattr(args, "project", False) or args.command == "branches":
        sources = ws.state["project_source"]
        return {"status": "ok", "display_name": ws.project["display_name"], "exists": ws.project_exists,
                "branches": [{**item, "source": sources.get("branches." + item["code"])}
                             for item in ws.branches],
                "reserved": {"code": ETC, "label": ETC_LABEL}, "kinds": list(KINDS),
                "general_limit": GENERAL_BRANCH_MAX, "display_name_source": sources.get("display_name"),
                "pending_recovery": pending, "warnings": ws.warnings}
    if not args.campaign:
        raise MetaError("usage", "--campaign or --project is required")
    campaign_id, cycle_id = _target(ws, args.campaign, args.cycle)
    meta = ws.require_meta(campaign_id, create=False)
    entry = ws.entry(meta, cycle_id, create=False)
    if entry is None:
        raise MetaError("meta-missing", cycle_id or campaign_id)
    return {"status": "ok", "campaign_id": campaign_id, "cycle_id": cycle_id, "entry": _ordered_entry(entry),
            "pending_recovery": pending, "warnings": ws.warnings}


def _dispatch(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "show" or (args.command == "branches" and args.action == "list"):
        return _show(root, args)
    session = args.session or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or None
    options = dict(dry_run=args.dry_run, actor_by=args.by, session=session,
                   reason=args.reason or "요청에 따른 메타데이터 수정")
    if args.command == "release":
        return run_write(root, lambda ws: op_release(ws, args.campaign, args.cycle, args.field), **options)
    if args.command == "set":
        if args.project:
            if args.display_name is None:
                raise MetaError("nothing-to-set")
            return run_write(root, lambda ws: op_set_project(ws, args.display_name), **options)
        if not args.campaign:
            raise MetaError("usage", "--campaign or --project is required")
        values: Dict[str, Any] = {}
        for name in ("title", "summary", "short_id"):
            if getattr(args, name) is not None:
                values[name] = getattr(args, name)
        if args.branches is not None:
            values["branches"] = _csv(args.branches)
        if args.kinds is not None:
            values["kinds"] = _csv(args.kinds)
        if getattr(args, "presentation_kind", None) is not None:
            values["presentation_kind"] = args.presentation_kind
        return run_write(root, lambda ws: op_set(ws, args.campaign, args.cycle, values), **options)
    if args.action == "add":
        return run_write(root, lambda ws: op_branch_add(ws, args.code, args.label, args.note), **options)
    if args.action == "import":
        try:
            seed = json.loads(Path(args.input).read_text(encoding="utf-8"), object_pairs_hook=W._unique_pairs)
        except (OSError, ValueError, UnicodeError, W.WorkflowGroupError) as exc:
            raise MetaError("seed-unreadable", type(exc).__name__) from exc
        return run_write(root, lambda ws: op_branch_import(ws, seed), **options)
    if args.action == "rename":
        return run_write(root, lambda ws: op_branch_rename(ws, args.code, args.new_code, args.label, args.note),
                         **options)
    if args.action == "merge":
        return run_write(root, lambda ws: op_branch_merge(ws, args.old, args.into), **options)
    return run_write(root, lambda ws: op_branch_remove(ws, args.code), **options)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # a usage error already printed its JSON; --help exits 0
        return int(exc.code or 0)
    root = Path(args.artifact_root)
    try:
        if not root.is_dir():
            raise MetaError("root-invalid")
        result = _dispatch(root.resolve(), args)
    except MetaError as exc:
        print(json.dumps({"status": "blocked", "code": exc.code, "detail": exc.detail}, ensure_ascii=False,
                         sort_keys=True))
        return 65
    except (H.HistoryError, W.WorkflowGroupError, producer.ProducerError, OSError) as exc:
        print(json.dumps({"status": "blocked", "code": getattr(exc, "code", "io-error"),
                          "detail": str(exc)[:200]}, ensure_ascii=False, sort_keys=True))
        return 65
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
