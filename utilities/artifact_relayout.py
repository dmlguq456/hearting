#!/usr/bin/env python3
"""W7I Cycle B: relayout ID-named campaign/cycle directories into readable locators.

PRD artifact-path-contract v14 §33 (D-88..D-93), acceptance A-17.6..A-17.8.

Before (W7C/W7G layout)                         After (D-88 layout)
  campaigns/camp_<id>/campaign.json               campaigns/<date>_<slug>/campaign.json
  campaigns/camp_<id>/cycles/cyc_<id>/manifest    campaigns/<date>_<slug>/<date>_<slug>/manifest.json
  campaigns/camp_<id>/cycles/cyc_<id>/artifacts   campaigns/<date>_<slug>/<date>_<slug>/artifacts/

Contract summary
- The move is a same-filesystem directory rename only (D-93). Nothing is copied
  and nothing is deleted; the empty ``cycles/`` container is removed with
  ``rmdir`` and re-created on rollback.
- Records are authoritative (D-88/D-89). Every campaign/cycle record receives its
  display fields (``slug``, ``title``, ``slug_source``, ``slug_truncated``,
  ``locator``, ``locator_suffix``) *before* the directory is renamed, and the
  directory name is derived from the record, never the reverse (D-92).
- Retroactive naming follows the D-92 priority list and journals which priority
  fired. Auto-generated titles (``"<capability> campaign"``, ``"<capability>
  <intensity> cycle"``, ``"<capability> cycle output"``) count as absent.
- The run is journaled under ``.runtime/artifact-producer/v1/migrations/
  <stamp>-relayout/`` with a monotone phase, per-operation inverse rows, and a
  witness (inventory, optionally content digests). A crash before the rename
  commit point rolls back; after it rolls forward (A-17.8). A nonterminal
  journal is a typed ``relayout-in-progress`` hold for readers and gates.
- The old ID locators keep resolving through the D-82 append-only compat map
  chain: one ``directory`` row per moved campaign/cycle, one ``file`` row per
  moved file, and re-targeted rows for every older map whose target moved.
- Sealed manifests are never rewritten (D-6/D-11/D-71). Their bytes are part of
  the witness.
- Completion closes the D-91 transition window for the root: from then on
  ``artifact_producer.begin`` refuses a slugless route with a typed error.
"""
from __future__ import annotations

import argparse
import base64
import collections
import dataclasses
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission  # noqa: E402
import artifact_cutover as C  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_resplit as RS  # noqa: E402

RELAYOUT_ALGORITHM_VERSION = "w7i-relayout/v1"
RUN_SUFFIX = "-relayout"
JOURNAL_NAME = "journal-relayout.json"
INVERSE_NAME = "inverse.jsonl"
INVENTORY_NAME = "inventory.json"
PLAN_NAME = "plan.json"
REPORT_NAME = "report.json"
MAP_NAME = "compatibility-map.jsonl"
INVERSE_SCHEMA = "artifact-relayout-inverse-row/v1"
PLAN_KIND = "w7i-relayout-plan"
REPORT_KIND = "w7i-relayout-report"
HOLD_EXIT = 65

# Monotone phases. Everything up to and including ``renaming`` rolls back on
# a crash; ``renamed`` is the commit point after which a re-run rolls forward.
PHASES = ("planned", "records-written", "renaming", "renamed", "witnessed",
          "compat-reissued", "indexed", "complete")
ROLLBACK_PHASES = frozenset({"planned", "records-written", "renaming"})
ROLL_FORWARD_PHASES = frozenset({"renamed", "witnessed", "compat-reissued", "indexed"})
TERMINAL_PHASES = frozenset({"complete", "rolled-back", "no-op"})

NAME_PRIORITIES = ("record", "attempt-slug", "artifacts-top-dir", "campaign-goal", "unnamed")
UNNAMED = "unnamed"
_AUTO_TITLE = re.compile(
    r"^[a-z][a-z0-9-]*\s+(campaign|cycle output|(?:direct|quick|standard|strong|deep|max)\s+cycle|cycle)$",
    re.IGNORECASE,
)
_AUTO_KEY = re.compile(r"^(?:[a-z][a-z0-9-]*:rt-[0-9a-f]{16}|adopted:camp_[0-9a-f]{32})$")
_LEGACY_KEY = re.compile(r"^legacy:[^:]+:(?P<name>.+)$")
_DATE_TOKEN = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[_\-\s]+|$)")
_CAMPAIGN_ID = re.compile(r"^camp_[0-9a-f]{32}$")
_CYCLE_ID = re.compile(r"^cyc_[0-9a-f]{32}$")


class RelayoutError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return C._now()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    return "sha256:" + C._sha(path)


def _canonical_digest(body: Mapping[str, Any]) -> str:
    return RS._canonical_digest(dict(body))


def _rel(root: Path, path: Path) -> str:
    return os.path.relpath(str(path), str(root))


def run_dirs(root: Path) -> List[Path]:
    mdir = C.migrations_dir(Path(root).resolve())
    if not mdir.is_dir():
        return []
    return sorted((p for p in mdir.iterdir() if p.is_dir() and p.name.endswith(RUN_SUFFIX)),
                  key=lambda p: p.name)


def state_path(root: Path) -> Path:
    return P.relayout_state_path(Path(root))


def relayout_state(root: Path) -> Dict[str, Any]:
    """Pure read of the per-root relayout state (transition window, D-91)."""
    return P.read_relayout_state(root)


def transition_window_closed(root: Path) -> bool:
    return P.transition_window_closed(root)


def relayout_hold(root: Path) -> Optional[Dict[str, Any]]:
    """A nonterminal relayout journal on disk is a typed hold (A-17.8)."""
    holds = []
    for run_dir in run_dirs(root):
        journal = P._read_json(run_dir / JOURNAL_NAME)
        if journal is None:
            continue
        phase = journal.get("phase")
        if phase not in TERMINAL_PHASES:
            holds.append({
                "code": "relayout-in-progress", "journal": str(run_dir / JOURNAL_NAME),
                "phase": phase, "gate": "relayout", "started_at": journal.get("started_at"),
            })
    if not holds:
        return None
    holds.sort(key=lambda h: h["journal"])
    return holds[0]


def migration_hold(root: Path) -> Optional[Dict[str, Any]]:
    """Any W7G resplit or W7I relayout hold, resplit first (older contract)."""
    return RS.resplit_hold(Path(root)) or relayout_hold(root)


# ---------------------------------------------------------------------------
# legacy layout discovery (records first, names only as a compatibility hint)
# ---------------------------------------------------------------------------


def _manifest(path: Path) -> Optional[Dict[str, Any]]:
    return P._read_json(path / "manifest.json")


def _cycle_identity(root: Path, campaign_id: str, cycle_path: Path) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Stable cycle ID for a legacy ``cycles/<name>`` directory.

    A sealed cycle names itself in ``manifest.json``. An open cycle has no
    manifest yet; the historical layout created its directory with the record
    ID as its name, so the record with that ID and this campaign is the only
    admissible binding (the same compatibility rule ``artifact_locator`` uses).
    """
    manifest = _manifest(cycle_path)
    manifest_id = None
    if manifest is not None:
        cycle = manifest.get("cycle") if isinstance(manifest.get("cycle"), dict) else {}
        manifest_id = cycle.get("cycle_id")
        if not isinstance(manifest_id, str) or _CYCLE_ID.fullmatch(manifest_id) is None:
            raise RelayoutError("relayout-cycle-id-invalid", str(cycle_path))
        campaign = manifest.get("campaign") if isinstance(manifest.get("campaign"), dict) else {}
        if campaign.get("campaign_id") != campaign_id:
            raise RelayoutError("relayout-cycle-campaign-mismatch", str(cycle_path))
    binding = artifact_locator.read_cycle_binding(cycle_path)
    if binding is not None:
        if binding.get("campaign_id") != campaign_id:
            raise RelayoutError("relayout-cycle-campaign-mismatch", str(cycle_path))
        if manifest_id is not None and binding.get("cycle_id") != manifest_id:
            raise RelayoutError("relayout-cycle-binding-mismatch", str(cycle_path))
        manifest_id = manifest_id or binding.get("cycle_id")
    if manifest_id is not None:
        record = P.read_cycle_record(root, manifest_id)
        if record is not None and record.get("campaign_id") != campaign_id:
            raise RelayoutError("relayout-cycle-campaign-mismatch", manifest_id)
        return manifest_id, record
    if _CYCLE_ID.fullmatch(cycle_path.name) is None:
        raise RelayoutError("relayout-orphan-cycle-dir", str(cycle_path))
    record = P.read_cycle_record(root, cycle_path.name)
    if record is None or record.get("campaign_id") != campaign_id:
        raise RelayoutError("relayout-orphan-cycle-dir", str(cycle_path))
    return cycle_path.name, record


def iter_legacy_units(root: Path) -> Iterator[Dict[str, Any]]:
    """Yield one unit per campaign directory that still has legacy shape.

    A campaign is legacy-shaped when its directory is the stable ID itself, its
    record lacks a persisted ``locator``, or it still holds a ``cycles/``
    container (a hybrid left by the W7I transition window). Cycles under
    ``cycles/`` are always legacy; direct children are already readable.
    """
    root = Path(root).resolve()
    base = root / "campaigns"
    if not base.is_dir() or base.is_symlink():
        return
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        record = P._read_json(entry / "campaign.json")
        if record is None:
            manifest_view = artifact_locator._campaign_from_manifests(entry)
            if manifest_view is None:
                continue
            raise RelayoutError("relayout-campaign-record-missing", str(entry))
        campaign_id = record.get("campaign_id")
        if not isinstance(campaign_id, str) or _CAMPAIGN_ID.fullmatch(campaign_id) is None:
            raise RelayoutError("relayout-campaign-id-invalid", str(entry))
        legacy_dir = entry.name == campaign_id or not record.get("locator")
        cycles_dir = entry / "cycles"
        cycles: List[Dict[str, Any]] = []
        if cycles_dir.is_dir() and not cycles_dir.is_symlink():
            for child in sorted(cycles_dir.iterdir(), key=lambda p: p.name):
                if child.name.startswith("."):
                    continue
                if child.is_symlink() or not child.is_dir():
                    raise RelayoutError("relayout-cycles-container-foreign-entry", str(child))
                cycle_id, cycle_record = _cycle_identity(root, campaign_id, child)
                cycles.append({"cycle_id": cycle_id, "path": child, "record": cycle_record,
                               "manifest": _manifest(child)})
        elif cycles_dir.exists() or cycles_dir.is_symlink():
            raise RelayoutError("relayout-cycles-container-foreign-entry", str(cycles_dir))
        if not legacy_dir and not cycles:
            continue
        yield {"campaign_id": campaign_id, "path": entry, "record": record,
               "legacy_dir": legacy_dir, "cycles": cycles}


# ---------------------------------------------------------------------------
# D-92 retroactive naming
# ---------------------------------------------------------------------------


def is_auto_title(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip() or _AUTO_TITLE.fullmatch(value.strip()) is not None


def _key_name(key: Any) -> Optional[str]:
    if not isinstance(key, str) or not key.strip() or _AUTO_KEY.fullmatch(key.strip()):
        return None
    match = _LEGACY_KEY.match(key.strip())
    return match.group("name") if match else key.strip()


def strip_leading_date(value: str) -> Tuple[str, bool]:
    """Drop one leading ``YYYY-MM-DD`` token from a *slug input* only.

    The D-88 locator already starts with the record date, so a title that begins
    with the same date (every W7G resplit title does) would otherwise read
    ``2026-08-24_2026-08-24-...``. The record title itself stays verbatim.
    """
    stripped = _DATE_TOKEN.sub("", value, count=1)
    if stripped != value and stripped.strip():
        return stripped, True
    return value, False


def parse_attempt_slugs(jobs_path: Optional[Path]) -> Dict[str, str]:
    """``route_id -> slug`` from the dispatch registry (latest row wins).

    The registry is a TSV whose fifth column is the attempt slug and whose
    sixth column carries ``key=value`` metadata including ``route_id``.
    """
    result: Dict[str, str] = {}
    if jobs_path is None or not Path(jobs_path).is_file():
        return result
    with open(jobs_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 6:
                continue
            slug = fields[4].strip()
            if not slug:
                continue
            for pair in fields[5].split(","):
                key, sep, value = pair.partition("=")
                if sep and key.strip() == "route_id" and value.strip():
                    result[value.strip()] = slug
                    break
    return result


def _top_artifacts_dir(paths: Sequence[Path]) -> Optional[str]:
    counts: collections.Counter = collections.Counter()
    for base in paths:
        artifacts = Path(base) / "artifacts"
        if not artifacts.is_dir():
            continue
        for entry in artifacts.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_dir() and not entry.is_symlink():
                count = 0
                for _current, _dirs, files in os.walk(str(entry), followlinks=False):
                    count += len(files)
                counts[entry.name] += count
    if not counts:
        return None
    best = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return best[0] if best[1] > 0 else None


def _finish_name(raw: str, title: str, source: str, priority: int) -> Dict[str, Any]:
    slug_input, date_stripped = strip_leading_date(str(raw))
    slug, truncated = artifact_locator.slugify(slug_input, fallback=UNNAMED)
    return {"slug": slug, "title": str(title), "slug_source": source, "priority": priority,
            "slug_truncated": truncated, "title_date_stripped": date_stripped}


def derive_cycle_name(record: Optional[Mapping[str, Any]], manifest: Optional[Mapping[str, Any]],
                      cycle_path: Path, campaign: Mapping[str, Any],
                      attempt_slugs: Mapping[str, str]) -> Dict[str, Any]:
    """D-92 priority walk for one cycle."""
    record = record or {}
    if isinstance(record.get("slug"), str) and record["slug"].strip() and not is_auto_title(record["slug"]):
        return _finish_name(record["slug"], record.get("title") or record["slug"], "record", 1)
    if not is_auto_title(record.get("title")):
        return _finish_name(record["title"], record["title"], "record", 1)
    route_id = record.get("route_id")
    if isinstance(route_id, str) and attempt_slugs.get(route_id):
        return _finish_name(attempt_slugs[route_id], attempt_slugs[route_id], "attempt-slug", 2)
    top = _top_artifacts_dir([cycle_path])
    if top:
        return _finish_name(top, top, "artifacts-top-dir", 3)
    if not is_auto_title(campaign.get("goal")):
        return _finish_name(campaign["goal"], campaign["goal"], "campaign-goal", 4)
    return _finish_name(UNNAMED, UNNAMED, UNNAMED, 5)


def derive_campaign_name(record: Mapping[str, Any], cycle_paths: Sequence[Path],
                         cycle_records: Sequence[Mapping[str, Any]],
                         attempt_slugs: Mapping[str, str]) -> Dict[str, Any]:
    if isinstance(record.get("slug"), str) and record["slug"].strip() and not is_auto_title(record["slug"]):
        return _finish_name(record["slug"], record.get("title") or record["slug"], "record", 1)
    if not is_auto_title(record.get("title")):
        return _finish_name(record["title"], record["title"], "record", 1)
    key_name = _key_name(record.get("key"))
    if key_name:
        return _finish_name(key_name, key_name, "record", 1)
    votes: collections.Counter = collections.Counter()
    for cycle in cycle_records:
        route_id = cycle.get("route_id") if isinstance(cycle, dict) else None
        if isinstance(route_id, str) and attempt_slugs.get(route_id):
            votes[attempt_slugs[route_id]] += 1
    if votes:
        best = sorted(votes.items(), key=lambda item: (-item[1], item[0]))[0][0]
        return _finish_name(best, best, "attempt-slug", 2)
    top = _top_artifacts_dir(cycle_paths)
    if top:
        return _finish_name(top, top, "artifacts-top-dir", 3)
    if not is_auto_title(record.get("goal")):
        return _finish_name(record["goal"], record["goal"], "campaign-goal", 4)
    return _finish_name(UNNAMED, UNNAMED, UNNAMED, 5)


def _cycle_date(record: Optional[Mapping[str, Any]], manifest: Optional[Mapping[str, Any]]) -> str:
    """D-88 date = the cycle's own start; a W7G resplit cycle declares the
    work's date in ``resplit_started_on`` (D-79), which is what a reader wants."""
    record = record or {}
    for candidate in (record.get("resplit_started_on"), record.get("started_on"),
                      ((manifest or {}).get("cycle") or {}).get("started_on")):
        if isinstance(candidate, str) and candidate:
            return artifact_locator.date_part(candidate)
    raise RelayoutError("relayout-cycle-date-missing", str(record.get("cycle_id")))


def _allocate(parent: Path, reserved: set, timestamp: str, slug: str) -> Tuple[str, str]:
    """D-90 smallest unused suffix against disk *and* this plan's reservations."""
    base = artifact_locator.locator_base(timestamp, slug)
    ordinal = 1
    locator, suffix = base, ""
    while True:
        try:
            (parent / locator).lstat()
            occupied = True
        except FileNotFoundError:
            occupied = False
        if not occupied and locator not in reserved:
            break
        ordinal += 1
        suffix = f"-{ordinal}"
        locator = base + suffix
    reserved.add(locator)
    return locator, suffix


# ---------------------------------------------------------------------------
# inventory witness (D-78 dualization: inventory always, digests on request)
# ---------------------------------------------------------------------------


def _inventory(directory: Path, *, digests: bool) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    total = 0
    for path in sorted(P._walk_files(directory), key=lambda p: p.as_posix()):
        rel = _rel(directory, path)
        if rel == artifact_locator.CYCLE_BINDING:
            # Machine-owned locator binding written by this very run; never
            # user output, never manifest data, never part of the witness.
            continue
        st = path.lstat()
        row: Dict[str, Any] = {"path": rel, "size": st.st_size, "inode": st.st_ino,
                               "symlink": path.is_symlink()}
        if digests and not path.is_symlink():
            row["sha256"] = _sha_file(path)
        rows.append(row)
        total += st.st_size
    body = {"files": len(rows), "bytes": total, "rows": rows}
    body["digest"] = _sha_bytes(json.dumps(rows, sort_keys=True).encode("utf-8"))
    return body


def _manifest_digests(manifest: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """``artifacts/<rel> -> content_digest`` from a sealed manifest, if any."""
    out: Dict[str, str] = {}
    for row in (manifest or {}).get("artifact_revisions") or []:
        locator = row.get("locator") if isinstance(row, dict) else None
        digest = row.get("content_digest") if isinstance(row, dict) else None
        if isinstance(locator, dict) and isinstance(locator.get("path"), str) and isinstance(digest, str):
            out[locator["path"]] = digest
    return out


# ---------------------------------------------------------------------------
# plan (pure read)
# ---------------------------------------------------------------------------


def _route_closed(root: Path, route_id: Any) -> bool:
    if not isinstance(route_id, str) or not route_id:
        return False
    routes = Path(root) / ".runtime" / "routes"
    return (routes / f"{route_id}.outcome.json").is_file()


def build_plan(root: Path, *, jobs_path: Optional[Path] = None, digests: bool = False) -> Dict[str, Any]:
    root = Path(root).resolve()
    identity = P.artifact_lifecycle.read_root_identity(root)
    attempt_slugs = parse_attempt_slugs(jobs_path)
    reserved_campaigns: set = set()
    campaigns_out: List[Dict[str, Any]] = []
    live_open: List[Dict[str, Any]] = []
    priorities: collections.Counter = collections.Counter()
    campaign_priorities: collections.Counter = collections.Counter()
    totals = {"campaigns": 0, "campaign_dirs_renamed": 0, "cycles": 0, "files": 0, "bytes": 0,
              "unnamed_cycles": 0, "unnamed_campaigns": 0}
    for unit in iter_legacy_units(root):
        record = unit["record"]
        cycle_paths = [c["path"] for c in unit["cycles"]]
        cycle_records = [c["record"] for c in unit["cycles"] if c["record"]]
        if unit["legacy_dir"]:
            name = derive_campaign_name(record, cycle_paths, cycle_records, attempt_slugs)
            created_on = record.get("created_on")
            if not isinstance(created_on, str) or not created_on:
                raise RelayoutError("relayout-campaign-date-missing", unit["campaign_id"])
            locator, suffix = _allocate(root / "campaigns", reserved_campaigns,
                                        artifact_locator.date_part(created_on), name["slug"])
            campaign_priorities[name["priority"]] += 1
            totals["campaign_dirs_renamed"] += 1
            if name["slug_source"] == UNNAMED:
                totals["unnamed_campaigns"] += 1
        else:
            name = {"slug": record.get("slug"), "title": record.get("title"),
                    "slug_source": record.get("slug_source"), "priority": None,
                    "slug_truncated": bool(record.get("slug_truncated")), "title_date_stripped": False}
            # Display locators may be renamed by hand (D-89); the directory that
            # exists is the parent the cycles move into, not the recorded name.
            locator, suffix = unit["path"].name, record.get("locator_suffix", "")
        listed = record.get("cycles") if isinstance(record.get("cycles"), list) else []
        campaign_row = {
            "campaign_id": unit["campaign_id"], "source_dir": _rel(root, unit["path"]),
            "target_dir": f"campaigns/{locator}", "rename": unit["legacy_dir"],
            "locator": locator, "locator_suffix": suffix, **name, "cycles": [],
            "unlisted_cycle_ids": [c["cycle_id"] for c in unit["cycles"] if c["cycle_id"] not in listed],
        }
        reserved_cycles: set = set()
        for child, layout in artifact_locator.iter_cycle_dirs(unit["path"]):
            if layout == "readable":
                reserved_cycles.add(child.name)
        for cycle in unit["cycles"]:
            crec = cycle["record"]
            cname = derive_cycle_name(crec, cycle["manifest"], cycle["path"], record, attempt_slugs)
            cdate = _cycle_date(crec, cycle["manifest"])
            clocator, csuffix = _allocate(unit["path"], reserved_cycles, cdate, cname["slug"])
            inventory = _inventory(cycle["path"], digests=digests)
            state = (crec or {}).get("state") or ("sealed" if cycle["manifest"] else "unknown")
            if state == "open" and not _route_closed(root, (crec or {}).get("route_id")):
                live_open.append({"cycle_id": cycle["cycle_id"], "route_id": (crec or {}).get("route_id"),
                                  "source_dir": _rel(root, cycle["path"])})
            priorities[cname["priority"]] += 1
            if cname["slug_source"] == UNNAMED:
                totals["unnamed_cycles"] += 1
            totals["cycles"] += 1
            totals["files"] += inventory["files"]
            totals["bytes"] += inventory["bytes"]
            campaign_row["cycles"].append({
                "cycle_id": cycle["cycle_id"], "state": state, "has_record": crec is not None,
                "source_dir": _rel(root, cycle["path"]),
                "target_dir": f"campaigns/{locator}/{clocator}",
                "locator": clocator, "locator_suffix": csuffix, "date": cdate, **cname,
                "manifest_digest": (crec or {}).get("manifest_digest"),
                "witness": {"files": inventory["files"], "bytes": inventory["bytes"],
                            "digest": inventory["digest"], "content_digests": digests},
                "_inventory_rows": inventory["rows"],
            })
        totals["campaigns"] += 1
        campaigns_out.append(campaign_row)
    return {
        "schema_version": 1, "kind": PLAN_KIND, "algorithm_version": RELAYOUT_ALGORITHM_VERSION,
        "artifact_root": str(root), "artifact_root_id": identity.artifact_root_id if identity else None,
        "generated_at": _now(), "jobs_path": str(jobs_path) if jobs_path else None,
        "totals": totals,
        "cycle_priority_histogram": {str(k): v for k, v in sorted(priorities.items())},
        "campaign_priority_histogram": {str(k): v for k, v in sorted(campaign_priorities.items())},
        "unnamed_ratio": (totals["unnamed_cycles"] / totals["cycles"]) if totals["cycles"] else 0.0,
        "live_open_cycles": live_open,
        "campaigns": campaigns_out,
    }


def check_preconditions(root: Path) -> Dict[str, Any]:
    """Everything a run needs *after* the rename commit point, checked before
    the first record write: the compat chain must exist, be complete, and be
    undrifted, or the D-82 append at phase `witnessed` would fail with the
    directories already moved and no rollback left."""
    root = Path(root).resolve()
    compat_path = C.compat_path(root)
    if not compat_path.is_file():
        raise RelayoutError("compat-map-missing", str(compat_path))
    state = C.load_map_state(root)
    if state["missing"]:
        raise RelayoutError("compat-map-missing", state["missing"][0])
    if state["drifted"]:
        raise RelayoutError("compat-map-drifted", state["drifted"][0])
    return {"compat_maps": len(state["maps"]), "missing": [], "drifted": []}


def _strip_private(plan: Mapping[str, Any]) -> Dict[str, Any]:
    body = json.loads(json.dumps(plan))
    body.pop("generated_at", None)
    for campaign in body["campaigns"]:
        for cycle in campaign["cycles"]:
            cycle.pop("_inventory_rows", None)
    return body


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _journal_path(run_dir: Path) -> Path:
    return run_dir / JOURNAL_NAME


def _write_journal(run_dir: Path, journal: Dict[str, Any]) -> None:
    P._write_atomic(_journal_path(run_dir), P._json_bytes(journal))


def _inverse_rows(run_dir: Path) -> List[Dict[str, Any]]:
    path = run_dir / INVERSE_NAME
    return C._read_jsonl(path) if path.is_file() else []


def _append_inverse(run_dir: Path, rows: List[Dict[str, Any]], row: Dict[str, Any]) -> None:
    row = {"schema_version": 1, "kind": INVERSE_SCHEMA, "ordinal": len(rows), **row}
    rows.append(row)
    C._write_jsonl(run_dir / INVERSE_NAME, rows)


def _crash(journal: Mapping[str, Any], point: str) -> None:
    if journal.get("crash_at") == point:
        raise RelayoutError("crash-fixture", point)


def _crash_after(journal: Mapping[str, Any], phase: str) -> None:
    if journal.get("crash_after_phase") == phase:
        raise RelayoutError("crash-fixture", phase)


def _updated_record(record: Mapping[str, Any], name: Mapping[str, Any], *, locator: str, suffix: str,
                    legacy_locator: str, run_at: str) -> Dict[str, Any]:
    updated = dict(record)
    if record.get("title") is not None and is_auto_title(record.get("title")):
        updated["legacy_title"] = record["title"]
    updated["slug"] = name["slug"]
    updated["title"] = record["title"] if not is_auto_title(record.get("title")) else name["title"]
    updated["slug_source"] = f"relayout:{name['slug_source']}"
    updated["slug_truncated"] = bool(name["slug_truncated"])
    updated["locator"] = locator
    updated["locator_suffix"] = suffix
    updated["legacy_locator"] = legacy_locator
    updated["relayout_run_at"] = run_at
    if name.get("title_date_stripped"):
        updated["slug_date_stripped"] = True
    return updated


def _phase_records(root: Path, run_dir: Path, journal: Dict[str, Any], plan: Mapping[str, Any]) -> None:
    """Persist display fields on every record before any directory moves."""
    pre_image = journal.setdefault("pre_image", {})
    pre_campaigns: List[Dict[str, Any]] = pre_image.setdefault("campaign_records", [])
    pre_cycles: List[Dict[str, Any]] = pre_image.setdefault("cycle_records", [])
    bindings: List[str] = pre_image.setdefault("created_bindings", [])
    run_at = journal["started_at"]
    done = {row["path"] for row in pre_campaigns} | {row["path"] for row in pre_cycles}
    for campaign in plan["campaigns"]:
        cpath = root / campaign["source_dir"] / "campaign.json"
        crel = _rel(root, cpath)
        if (campaign["rename"] or campaign.get("unlisted_cycle_ids")) and crel not in done:
            original = cpath.read_bytes()
            pre_campaigns.append({"path": crel, "bytes_b64": _b64(original), "sha256": _sha_bytes(original)})
            _write_journal(run_dir, journal)
            record = json.loads(original.decode("utf-8"))
            if campaign["rename"]:
                updated = _updated_record(record, campaign, locator=campaign["locator"],
                                          suffix=campaign["locator_suffix"],
                                          legacy_locator=Path(campaign["source_dir"]).name, run_at=run_at)
            else:
                updated = dict(record)
            # A legacy cycle directory whose id the campaign record never listed
            # was tolerated by the ID layout; the readable layout verifies the
            # binding against `cycles[]`, so the record is repaired first.
            listed = list(updated.get("cycles") or [])
            for cycle_id in campaign.get("unlisted_cycle_ids") or []:
                if cycle_id not in listed:
                    listed.append(cycle_id)
            updated["cycles"] = listed
            P._write_atomic(cpath, P._json_bytes(updated))
        for cycle in campaign["cycles"]:
            if cycle["has_record"]:
                rpath = P.cycle_record_path(root, cycle["cycle_id"])
                rrel = _rel(root, rpath)
                if rrel not in done:
                    original = rpath.read_bytes()
                    pre_cycles.append({"path": rrel, "bytes_b64": _b64(original), "sha256": _sha_bytes(original)})
                    _write_journal(run_dir, journal)
                    record = json.loads(original.decode("utf-8"))
                    updated = _updated_record(record, cycle, locator=cycle["locator"],
                                              suffix=cycle["locator_suffix"],
                                              legacy_locator=Path(cycle["source_dir"]).name, run_at=run_at)
                    P._write_atomic(rpath, P._json_bytes(updated), 0o600)
            marker = root / cycle["source_dir"] / artifact_locator.CYCLE_BINDING
            if not marker.exists() and not marker.is_symlink():
                # Journal the intent first so a crash between the two leaves a
                # marker the rollback knows about.
                bindings.append(_rel(root, marker))
                _write_journal(run_dir, journal)
                P._write_cycle_binding(root / cycle["source_dir"], campaign["campaign_id"], cycle["cycle_id"])
            _crash(journal, "records:after-first-cycle")


def _rename(root: Path, run_dir: Path, rows: List[Dict[str, Any]], source: Path, target: Path) -> None:
    if target.exists() and not source.exists():
        return  # completed on an earlier attempt
    if target.exists():
        raise RelayoutError("relayout-target-exists", _rel(root, target))
    if not source.is_dir():
        raise RelayoutError("relayout-source-missing", _rel(root, source))
    os.rename(source, target)
    _append_inverse(run_dir, rows, {"action": "rename_back", "source": _rel(root, source),
                                    "target": _rel(root, target)})


def _phase_rename(root: Path, run_dir: Path, journal: Dict[str, Any], plan: Mapping[str, Any]) -> None:
    rows = _inverse_rows(run_dir)
    first = True
    for campaign in plan["campaigns"]:
        campaign_src = root / campaign["source_dir"]
        campaign_dst = root / campaign["target_dir"]
        # Cycles move while the campaign still has its old name so an inverse
        # replay in reverse order meets the same paths it recorded.
        current_campaign = campaign_src if campaign_src.is_dir() else campaign_dst
        for cycle in campaign["cycles"]:
            source = current_campaign / "cycles" / Path(cycle["source_dir"]).name
            target = current_campaign / cycle["locator"]
            _rename(root, run_dir, rows, source, target)
            if first:
                first = False
                _crash(journal, "rename:after-first-cycle")
        container = current_campaign / "cycles"
        if container.is_dir():
            if any(container.iterdir()):
                raise RelayoutError("relayout-cycles-container-not-empty", _rel(root, container))
            container.rmdir()
            _append_inverse(run_dir, rows, {"action": "mkdir", "target": _rel(root, container)})
        if campaign["rename"]:
            _rename(root, run_dir, rows, campaign_src, campaign_dst)
        _crash(journal, "rename:after-first-campaign")


def _phase_witness(root: Path, plan: Mapping[str, Any], *, digests: bool) -> Dict[str, Any]:
    mismatches = []
    checked = 0
    files = 0
    total = 0
    for campaign in plan["campaigns"]:
        for cycle in campaign["cycles"]:
            after = _inventory(root / cycle["target_dir"], digests=digests)
            expected_rows = cycle["_inventory_rows"]
            actual_rows = after["rows"]
            if digests and not cycle["witness"].get("content_digests"):
                actual_rows = [{k: v for k, v in row.items() if k != "sha256"} for row in actual_rows]
            if cycle["state"] == "open":
                # An open cycle has no manifest and may gain files from its own
                # producer while we hold the lock only against the producer CLI,
                # not against shell writers. Every file we inventoried must still
                # be there unchanged; new files are reported, not fatal.
                actual_by_path = {row["path"]: row for row in actual_rows}
                missing = [row for row in expected_rows if actual_by_path.get(row["path"]) != row]
                if missing:
                    mismatches.append({"cycle_id": cycle["cycle_id"], "expected_files": len(expected_rows),
                                       "actual_files": len(actual_rows), "changed": [r["path"] for r in missing][:5]})
                extra = len(actual_rows) - len(expected_rows)
                if extra:
                    cycle["witness"]["extra_files_after_plan"] = extra
            elif expected_rows != actual_rows:
                mismatches.append({"cycle_id": cycle["cycle_id"], "expected_files": len(expected_rows),
                                   "actual_files": len(actual_rows)})
            checked += 1
            files += after["files"]
            total += after["bytes"]
    if mismatches:
        raise RelayoutError("relayout-witness-mismatch", json.dumps(mismatches[:3], sort_keys=True))
    return {"cycles_checked": checked, "files": files, "bytes": total, "content_digests": digests}


def _map_rows(root: Path, plan: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], List[str], int]:
    rows: List[Dict[str, Any]] = []
    by_target: Dict[str, str] = {}
    moved_dirs: Dict[str, str] = {}
    for campaign in plan["campaigns"]:
        if campaign["rename"]:
            moved_dirs[campaign["source_dir"]] = campaign["target_dir"]
            rows.append({"schema_version": C.MAP_SCHEMA, "kind": "directory",
                         "source_locator": campaign["source_dir"], "target_locator": campaign["target_dir"],
                         "sha256": _sha_bytes(campaign["campaign_id"].encode("ascii")),
                         "identity_refs": [campaign["campaign_id"]]})
        for cycle in campaign["cycles"]:
            moved_dirs[cycle["source_dir"]] = cycle["target_dir"]
            digest = cycle.get("manifest_digest") or cycle["witness"]["digest"]
            rows.append({"schema_version": C.MAP_SCHEMA, "kind": "directory",
                         "source_locator": cycle["source_dir"], "target_locator": cycle["target_dir"],
                         "sha256": digest, "identity_refs": [cycle["cycle_id"]]})
            manifest_digests = _manifest_digests(_manifest(root / cycle["target_dir"]))
            for file_row in cycle["_inventory_rows"]:
                if file_row.get("symlink"):
                    continue
                rel = file_row["path"]
                sha = file_row.get("sha256") or manifest_digests.get(rel)
                if sha is None:
                    sha = _sha_file(root / cycle["target_dir"] / rel)
                target = f"{cycle['target_dir']}/{rel}"
                by_target[target] = sha
                rows.append({"schema_version": C.MAP_SCHEMA, "kind": "file",
                             "source_locator": f"{cycle['source_dir']}/{rel}",
                             "target_locator": target, "sha256": sha, "identity_refs": [cycle["cycle_id"]]})
    # D-82 lane 2: every older map row whose *target* moved gets a fresh row so a
    # one-hop `resolve_legacy` from the original legacy locator still lands.
    retargeted = 0
    supersedes: List[str] = []
    sorted_dirs = sorted(moved_dirs.items(), key=lambda item: -len(item[0]))
    for map_path, table in C._load_maps(root):
        hit = False
        for source, target in table.items():
            for old_dir, new_dir in sorted_dirs:
                if target == old_dir or target.startswith(old_dir + "/"):
                    new_target = new_dir + target[len(old_dir):]
                    sha = by_target.get(new_target)
                    candidate = root / new_target
                    if sha is None:
                        sha = _sha_file(candidate) if candidate.is_file() else _sha_bytes(new_target.encode())
                    rows.append({"schema_version": C.MAP_SCHEMA,
                                 "kind": "file" if candidate.is_file() else "directory",
                                 "source_locator": source, "target_locator": new_target,
                                 "sha256": sha, "identity_refs": []})
                    retargeted += 1
                    hit = True
                    break
        if hit:
            supersedes.append(map_path)
    return rows, supersedes, retargeted


def _phase_compat(root: Path, run_dir: Path, journal: Dict[str, Any], plan: Mapping[str, Any]) -> None:
    pre_image = journal.setdefault("pre_image", {})
    if pre_image.get("written_map_files"):
        return
    compat_path = C.compat_path(root)
    if not compat_path.is_file():
        raise RelayoutError("compat-map-missing", str(compat_path))
    state = C.load_map_state(root)
    map_path = run_dir / MAP_NAME
    if any(entry["path"] == str(map_path) for entry in state["maps"]):
        # A crash between `compat_append` and the journal write: the chain
        # already carries this run's map, so re-appending would double it.
        pre_image["written_map_files"] = [_rel(root, map_path)]
        _write_journal(run_dir, journal)
        return
    if state["missing"]:
        raise RelayoutError("compat-map-missing", state["missing"][0])
    if state["drifted"]:
        raise RelayoutError("compat-map-drifted", state["drifted"][0])
    pre_image["compat_json"] = {"bytes_b64": _b64(compat_path.read_bytes())}
    _write_journal(run_dir, journal)
    rows, supersedes, retargeted = _map_rows(root, plan)
    C._write_jsonl(map_path, rows)
    C.compat_append(root, maps=[map_path], supersedes=supersedes)
    pre_image["written_map_files"] = [_rel(root, map_path)]
    pre_image["superseded_map_files"] = supersedes
    journal["compat"] = {"rows": len(rows), "retargeted_rows": retargeted, "superseded_maps": len(supersedes)}
    _write_journal(run_dir, journal)


def _phase_index(root: Path, plan: Mapping[str, Any]) -> Dict[str, Any]:
    artifact_locator.rebuild_indexes(root)
    index_path = artifact_admission._index_path(root)
    patched = 0
    if index_path.is_file():
        index = artifact_admission.load_index(root)
        new_paths = {c["cycle_id"]: c["target_dir"] for camp in plan["campaigns"] for c in camp["cycles"]}
        cycles = {}
        for cycle_id, row in index.cycles.items():
            row = dict(row)
            if cycle_id in new_paths and row.get("cycle_path") != new_paths[cycle_id]:
                row["cycle_path"] = new_paths[cycle_id]
                patched += 1
            cycles[cycle_id] = row
        if patched:
            artifact_admission._write_index(root, dataclasses.replace(index, cycles=cycles))
    return {"locator_index": "rebuilt", "admission_index_cycle_paths_patched": patched}


def _write_state(root: Path, run_dir: Path, journal: Mapping[str, Any]) -> Dict[str, Any]:
    body = {
        "schema_version": 1, "contract": P.CONTRACT, "algorithm_version": RELAYOUT_ALGORITHM_VERSION,
        "state": "complete", "transition_window": "closed", "completed_at": _now(),
        "run_dir": str(run_dir), "plan_digest": journal.get("plan_digest"),
    }
    P._write_atomic(state_path(root), P._json_bytes(body), 0o600)
    return body


def _report(root: Path, run_dir: Optional[Path], journal: Mapping[str, Any], plan: Mapping[str, Any],
            *, dry_run: bool) -> Dict[str, Any]:
    body = {
        "schema_version": 1, "kind": REPORT_KIND, "algorithm_version": RELAYOUT_ALGORITHM_VERSION,
        "artifact_root": str(root), "artifact_root_id": plan.get("artifact_root_id"),
        "dry_run": dry_run, "run_dir": None if run_dir is None else str(run_dir), "phase": journal.get("phase"),
        "plan_digest": journal.get("plan_digest"), "totals": plan["totals"],
        "unnamed_ratio": plan["unnamed_ratio"],
        "cycle_priority_histogram": plan["cycle_priority_histogram"],
        "campaign_priority_histogram": plan["campaign_priority_histogram"],
        "witness": journal.get("witness"), "compat": journal.get("compat"),
        "index": journal.get("index"), "live_open_cycles": plan["live_open_cycles"],
        "moves": [
            {"cycle_id": c["cycle_id"], "from": c["source_dir"], "to": c["target_dir"],
             "slug_source": c["slug_source"], "priority": c["priority"]}
            for camp in plan["campaigns"] for c in camp["cycles"]
        ] + [
            {"campaign_id": camp["campaign_id"], "from": camp["source_dir"], "to": camp["target_dir"],
             "slug_source": camp["slug_source"], "priority": camp["priority"]}
            for camp in plan["campaigns"] if camp["rename"]
        ],
    }
    body["digest"] = _canonical_digest(body)
    if run_dir is not None and not dry_run:
        P._write_atomic(run_dir / REPORT_NAME, P._json_bytes(body))
    return body


def _execute(root: Path, run_dir: Path, journal: Dict[str, Any], plan: Mapping[str, Any], *, digests: bool) -> None:
    phase = journal["phase"]
    if phase == "planned":
        journal["phase"] = "records-written"
        _phase_records(root, run_dir, journal, plan)
        _write_journal(run_dir, journal)
        _crash_after(journal, "records-written")
        phase = journal["phase"]
    if phase == "records-written":
        journal["phase"] = "renaming"
        _write_journal(run_dir, journal)
        phase = "renaming"
    if phase == "renaming":
        _phase_rename(root, run_dir, journal, plan)
        journal["phase"] = "renamed"
        _write_journal(run_dir, journal)
        _crash_after(journal, "renamed")
        phase = "renamed"
    if phase == "renamed":
        journal["witness"] = _phase_witness(root, plan, digests=digests)
        journal["phase"] = "witnessed"
        _write_journal(run_dir, journal)
        _crash_after(journal, "witnessed")
        phase = "witnessed"
    if phase == "witnessed":
        _phase_compat(root, run_dir, journal, plan)
        journal["phase"] = "compat-reissued"
        _write_journal(run_dir, journal)
        _crash_after(journal, "compat-reissued")
        phase = "compat-reissued"
    if phase == "compat-reissued":
        journal["index"] = _phase_index(root, plan)
        journal["phase"] = "indexed"
        _write_journal(run_dir, journal)
        _crash_after(journal, "indexed")
        phase = "indexed"
    if phase == "indexed":
        journal["state"] = _write_state(root, run_dir, journal)
        journal["phase"] = "complete"
        journal["completed_at"] = _now()
        _write_journal(run_dir, journal)


def _rollback(root: Path, run_dir: Path, journal: Dict[str, Any]) -> None:
    rows = sorted(_inverse_rows(run_dir), key=lambda r: -r["ordinal"])
    for row in rows:
        if row["action"] == "rename_back":
            target = root / row["target"]
            source = root / row["source"]
            if target.is_dir() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.rename(target, source)
        elif row["action"] == "mkdir":
            (root / row["target"]).mkdir(parents=True, exist_ok=True)
    pre_image = journal.get("pre_image") or {}
    for rel in pre_image.get("created_bindings", []):
        marker = root / rel
        if marker.is_file() and not marker.is_symlink():
            marker.unlink()
    for entry in pre_image.get("cycle_records", []):
        P._write_atomic(root / entry["path"], base64.b64decode(entry["bytes_b64"]), 0o600)
    for entry in pre_image.get("campaign_records", []):
        P._write_atomic(root / entry["path"], base64.b64decode(entry["bytes_b64"]))
    compat_pre = pre_image.get("compat_json")
    if compat_pre is not None:
        P._write_atomic(C.compat_path(root), base64.b64decode(compat_pre["bytes_b64"]), 0o600)
    for rel in pre_image.get("written_map_files", []):
        path = root / rel
        if path.is_file():
            path.unlink()
    inverse = run_dir / INVERSE_NAME
    if inverse.is_file():
        inverse.unlink()
    artifact_locator.rebuild_indexes(root)
    journal["phase"] = "rolled-back"
    journal["rolled_back_at"] = _now()
    _write_journal(run_dir, journal)


def _open_run(root: Path) -> Optional[Path]:
    for run_dir in run_dirs(root):
        journal = P._read_json(run_dir / JOURNAL_NAME)
        if journal is not None and journal.get("phase") not in TERMINAL_PHASES:
            return run_dir
    return None


def _new_run_dir(root: Path) -> Path:
    base = C.migrations_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    stamp = C._stamp()
    run_dir = base / f"{stamp}{RUN_SUFFIX}"
    ordinal = 1
    while run_dir.exists():
        ordinal += 1
        run_dir = base / f"{stamp}-{ordinal}{RUN_SUFFIX}"
    run_dir.mkdir(parents=False, exist_ok=False)
    return run_dir


def apply(root: Path, *, jobs_path: Optional[Path] = None, dry_run: bool = False, digests: bool = False,
          crash_at: Optional[str] = None, crash_after_phase: Optional[str] = None,
          now: Optional[float] = None) -> Dict[str, Any]:
    """Plan and execute the relayout of one artifact root, or resume its open run."""
    root = Path(root).resolve()
    C._require_active(root)
    resplit = RS.resplit_hold(root)
    if resplit is not None:
        raise RelayoutError("resplit-in-progress", resplit["journal"])
    open_run = _open_run(root)
    if open_run is not None:
        if dry_run:
            journal = P._read_json(open_run / JOURNAL_NAME) or {}
            return {"status": "hold", "code": "relayout-in-progress", "run_dir": str(open_run),
                    "phase": journal.get("phase"), "dry_run": True}
        return resume(root, run_dir=open_run, digests=digests, crash_at=crash_at,
                      crash_after_phase=crash_after_phase, now=now)
    preconditions = check_preconditions(root)
    # The expensive walk (inventory, optional digests) runs before the admission
    # lock so concurrent producers are excluded only for the short mutation
    # window; the lock-held re-plan below proves nothing changed meanwhile.
    plan = build_plan(root, jobs_path=jobs_path, digests=digests)
    public_plan = _strip_private(plan)
    plan_digest = _canonical_digest(public_plan)
    if plan["live_open_cycles"]:
        raise RelayoutError("relayout-live-open-cycle",
                            json.dumps(plan["live_open_cycles"][:3], sort_keys=True))
    if dry_run and plan["campaigns"]:
        report = _report(root, None, {"phase": "dry-run", "plan_digest": plan_digest}, plan, dry_run=True)
        report["preconditions"] = preconditions
        return {"status": "dry-run", "plan_digest": plan_digest, "report": report, "plan": public_plan}
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        if plan["campaigns"] and not dry_run:
            locked = build_plan(root, jobs_path=jobs_path, digests=False)
            if _shape_digest(_strip_private(locked)) != _shape_digest(public_plan):
                raise RelayoutError("relayout-plan-drift", "root changed between plan and lock; re-run")
            if locked["live_open_cycles"]:
                raise RelayoutError("relayout-live-open-cycle",
                                    json.dumps(locked["live_open_cycles"][:3], sort_keys=True))
        if not plan["campaigns"]:
            window = relayout_state(root).get("transition_window")
            if not dry_run and window != "closed":
                run_dir = _new_run_dir(root)
                journal = {"schema_version": 1, "algorithm_version": RELAYOUT_ALGORITHM_VERSION,
                           "phase": "no-op", "started_at": _now(), "plan_digest": plan_digest}
                journal["state"] = _write_state(root, run_dir, journal)
                _write_journal(run_dir, journal)
                P._write_atomic(run_dir / PLAN_NAME, P._json_bytes({**public_plan, "digest": plan_digest}))
                window = "closed"
            return {"status": "no-op", "dry_run": dry_run, "plan_digest": plan_digest,
                    "transition_window": window, "plan": public_plan}
        run_dir = _new_run_dir(root)
        P._write_atomic(run_dir / PLAN_NAME, P._json_bytes({**public_plan, "digest": plan_digest}))
        # The private inventory rows are what the witness and the map need; they
        # are kept beside the plan so a resumed run reads the same expectation.
        P._write_atomic(run_dir / INVENTORY_NAME, P._json_bytes(
            {c["cycle_id"]: c["_inventory_rows"] for camp in plan["campaigns"] for c in camp["cycles"]}))
        journal = {
            "schema_version": 1, "algorithm_version": RELAYOUT_ALGORITHM_VERSION, "phase": "planned",
            "started_at": _now(), "plan_digest": plan_digest, "content_digests": digests,
            "crash_at": crash_at, "crash_after_phase": crash_after_phase, "pre_image": {},
        }
        _write_journal(run_dir, journal)
        try:
            _execute(root, run_dir, journal, plan, digests=digests)
        except RelayoutError as exc:
            if exc.code == "crash-fixture":
                raise
            journal["error"] = {"code": exc.code, "detail": exc.detail}
            _write_journal(run_dir, journal)
            if journal["phase"] in ROLLBACK_PHASES:
                _rollback(root, run_dir, journal)
            raise
        report = _report(root, run_dir, journal, plan, dry_run=False)
        return {"status": "complete", "run_dir": str(run_dir), "report": report}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _shape_digest(public_plan: Mapping[str, Any]) -> str:
    """The plan minus per-file witness data: which directories move where, under
    which names, with which records. Equal shapes mean the locked re-plan saw the
    same root the unlocked plan did."""
    body = json.loads(json.dumps(public_plan))
    for campaign in body["campaigns"]:
        for cycle in campaign["cycles"]:
            cycle.pop("witness", None)
    body.pop("totals", None)
    body.pop("jobs_path", None)
    return _canonical_digest(body)


def _load_run(root: Path, run_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    journal = P._read_json(run_dir / JOURNAL_NAME)
    plan = P._read_json(run_dir / PLAN_NAME)
    inventory = P._read_json(run_dir / INVENTORY_NAME) or {}
    if journal is None or plan is None:
        raise RelayoutError("relayout-run-unreadable", str(run_dir))
    for campaign in plan["campaigns"]:
        for cycle in campaign["cycles"]:
            cycle["_inventory_rows"] = inventory.get(cycle["cycle_id"], [])
    return journal, plan


def resume(root: Path, *, run_dir: Optional[Path] = None, digests: bool = False,
           crash_at: Optional[str] = None, crash_after_phase: Optional[str] = None,
           now: Optional[float] = None) -> Dict[str, Any]:
    """Roll an interrupted run back (before the rename commit) or forward (after)."""
    root = Path(root).resolve()
    run_dir = Path(run_dir) if run_dir is not None else _open_run(root)
    if run_dir is None:
        return {"status": "no-op", "reason": "no-open-run"}
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        journal, plan = _load_run(root, run_dir)
        journal["crash_at"] = crash_at
        journal["crash_after_phase"] = crash_after_phase
        phase = journal.get("phase")
        if phase in TERMINAL_PHASES:
            return {"status": phase, "run_dir": str(run_dir)}
        if phase in ROLLBACK_PHASES:
            _rollback(root, run_dir, journal)
            return {"status": "rolled-back", "run_dir": str(run_dir), "resumed_from": phase}
        if phase not in ROLL_FORWARD_PHASES:
            raise RelayoutError("relayout-phase-unknown", str(phase))
        _execute(root, run_dir, journal, plan, digests=bool(journal.get("content_digests")) or digests)
        report = _report(root, run_dir, journal, plan, dry_run=False)
        return {"status": "complete", "run_dir": str(run_dir), "resumed_from": phase, "report": report}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def rollback(root: Path, *, run_dir: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Explicit inverse replay of an open run that has not passed the commit point."""
    root = Path(root).resolve()
    run_dir = Path(run_dir) if run_dir is not None else _open_run(root)
    if run_dir is None:
        return {"status": "no-op", "reason": "no-open-run"}
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        journal, _plan = _load_run(root, run_dir)
        if journal.get("phase") not in ROLLBACK_PHASES:
            raise RelayoutError("relayout-past-commit-point", str(journal.get("phase")))
        _rollback(root, run_dir, journal)
        return {"status": "rolled-back", "run_dir": str(run_dir)}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def status(root: Path) -> Dict[str, Any]:
    """Read-only relayout view for gates: legacy shape counts, hold, window."""
    root = Path(root).resolve()
    legacy_campaign_dirs = 0
    legacy_cycle_dirs = 0
    error = None
    try:
        for unit in iter_legacy_units(root):
            legacy_campaign_dirs += 1 if unit["legacy_dir"] else 0
            legacy_cycle_dirs += len(unit["cycles"])
    except RelayoutError as exc:
        error = {"code": exc.code, "detail": exc.detail}
    except artifact_locator.LocatorError as exc:
        error = {"code": exc.code, "detail": exc.detail}
    except (OSError, ValueError) as exc:
        error = {"code": "relayout-scan-failed", "detail": str(exc)}
    hold = relayout_hold(root)
    state = relayout_state(root)
    if hold is not None:
        layout = "in-progress"
    elif error is not None:
        layout = "invalid"
    elif legacy_campaign_dirs == 0 and legacy_cycle_dirs == 0:
        layout = "readable"
    else:
        layout = "legacy"
    return {
        "readable_layout": layout, "legacy_campaign_dirs": legacy_campaign_dirs,
        "legacy_cycle_dirs": legacy_cycle_dirs, "relayout_hold": hold,
        "transition_window": state.get("transition_window"), "relayout_state": state.get("state"),
        "error": error,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _default_jobs() -> Optional[Path]:
    explicit = os.environ.get("AGENT_DISPATCH_JOBS")
    if explicit:
        return Path(explicit)
    try:
        import dispatch_contract  # noqa: WPS433
        return dispatch_contract.stable_state_root(os.environ) / "jobs.log"
    except Exception:  # pragma: no cover - registry resolution is best effort here
        return None


def _summary(plan: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in plan.items() if k != "campaigns"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--artifact-root", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_p = sub.add_parser("plan", help="pure read: the relayout plan and D-92 naming for this root")
    plan_p.add_argument("--jobs", help="dispatch registry for D-92 priority 2 (default: canonical)")
    plan_p.add_argument("--full", action="store_true", help="include per-cycle rows")
    apply_p = sub.add_parser("apply", help="journaled relayout (or resume of an open run)")
    apply_p.add_argument("--jobs")
    apply_p.add_argument("--dry-run", action="store_true")
    apply_p.add_argument("--content-digests", action="store_true",
                         help="witness with sha256 of every file (slow on large roots)")
    apply_p.add_argument("--crash-at")
    apply_p.add_argument("--crash-after-phase")
    resume_p = sub.add_parser("resume", help="roll an open run back or forward")
    resume_p.add_argument("--run-dir")
    rollback_p = sub.add_parser("rollback", help="inverse replay of an open run before its commit point")
    rollback_p.add_argument("--run-dir")
    sub.add_parser("hold", help="typed relayout hold, if any")
    sub.add_parser("status", help="read-only layout state for gates")
    args = parser.parse_args(argv)
    root = Path(args.artifact_root).resolve()
    try:
        if args.command == "plan":
            jobs = Path(args.jobs) if args.jobs else _default_jobs()
            plan = _strip_private(build_plan(root, jobs_path=jobs))
            try:
                plan["preconditions"] = check_preconditions(root)
            except RelayoutError as exc:
                plan["preconditions"] = {"blocked": exc.code, "detail": exc.detail}
            _print(plan if args.full else _summary(plan))
        elif args.command == "apply":
            jobs = Path(args.jobs) if args.jobs else _default_jobs()
            result = apply(root, jobs_path=jobs, dry_run=args.dry_run, digests=args.content_digests,
                           crash_at=args.crash_at, crash_after_phase=args.crash_after_phase)
            if "plan" in result:
                result["plan"] = _summary(result["plan"])
            _print(result)
            return 0 if result.get("status") in {"complete", "dry-run", "no-op"} else HOLD_EXIT
        elif args.command == "resume":
            _print(resume(root, run_dir=Path(args.run_dir) if args.run_dir else None))
        elif args.command == "rollback":
            _print(rollback(root, run_dir=Path(args.run_dir) if args.run_dir else None))
        elif args.command == "hold":
            hold = relayout_hold(root)
            _print({"hold": hold})
            return 0 if hold is None else HOLD_EXIT
        elif args.command == "status":
            _print(status(root))
    except RelayoutError as exc:
        _print({"status": "blocked", "code": exc.code, "detail": exc.detail})
        return 2
    except C.CutoverError as exc:
        _print({"status": "blocked", "code": str(exc.args[0]) if exc.args else "cutover-error",
                "detail": str(exc.args[1]) if len(exc.args) > 1 else ""})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
