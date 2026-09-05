#!/usr/bin/env python3
"""D-75 read-only fleet cutover gate. Owns no rule; consumes artifact_producer.

Never calls begin/activate/finalize/admit_shared/recover/resolve_output_dir --
classification and mutation stay owned by `artifact_producer.py` (Option A).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_cutover as C  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_resplit as RS  # noqa: E402
import artifact_relayout as RL  # noqa: E402
import artifact_residue as RES  # noqa: E402

SCHEMA_VERSION = 1
KIND = "fleet-cutover-gate/v1"
ROOT_RESOLVER = Path(__file__).resolve().parent / "artifact-root.sh"
RESOLVE_TIMEOUT_DEFAULT = 20.0
PROBE_REF = "ref_" + "0" * 32
PROBE_RREV = "rrev_" + "0" * 32
OK, INCOMPLETE, USAGE = 0, 65, 64


class GateError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


# ---------------------------------------------------------------------------
# roster
# ---------------------------------------------------------------------------


def load_roster(path: Path) -> Tuple[Dict[str, Any], str]:
    """-> (normalized_payload, digest). Raises GateError on any schema problem."""
    payload = P._read_json(Path(path))
    if payload is None:
        raise GateError("roster-unreadable", str(path))
    if payload.get("schema_version") != 1:
        raise GateError("roster-schema-unknown", str(payload.get("schema_version")))
    fleet_id = payload.get("fleet_id")
    if not isinstance(fleet_id, str) or not fleet_id:
        raise GateError("roster-fleet-id-missing")
    repos = payload.get("repos")
    if not isinstance(repos, list) or not repos:
        raise GateError("roster-empty")
    seen: set = set()
    normalized_repos: List[Dict[str, Any]] = []
    for row in repos:
        if not isinstance(row, dict):
            raise GateError("roster-repo-path-invalid", str(row))
        repo_path = row.get("repo_path")
        if not isinstance(repo_path, str) or not repo_path or not os.path.isabs(repo_path):
            raise GateError("roster-repo-path-invalid", str(repo_path))
        if repo_path in seen:
            raise GateError("roster-duplicate-repo", repo_path)
        seen.add(repo_path)
        entry: Dict[str, Any] = {"repo_path": repo_path}
        if "note" in row and row["note"] is not None:
            entry["note"] = row["note"]
        normalized_repos.append(entry)
    normalized = {
        "schema_version": 1,
        "fleet_id": fleet_id,
        "generated_at": payload.get("generated_at"),
        "repos": normalized_repos,
    }
    digest = P._digest(P._canonical(normalized))
    return normalized, digest


# ---------------------------------------------------------------------------
# root resolution -- the #1 failure mode
# ---------------------------------------------------------------------------


def resolve_repo_root(repo_path: str, *, timeout: float = RESOLVE_TIMEOUT_DEFAULT
                      ) -> Tuple[Optional[Path], Optional[str]]:
    env = {k: v for k, v in os.environ.items() if k != "AGENT_ARTIFACT_ROOT"}
    try:
        proc = subprocess.run([str(ROOT_RESOLVER), repo_path], env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "root-unresolved:timeout"
    except OSError as exc:
        return None, f"root-unresolved:oserror:{exc.errno}"
    if proc.returncode != 0:
        return None, f"root-unresolved:{proc.returncode}"
    out = proc.stdout.strip()
    if not out:
        return None, "root-unresolved:empty"
    return Path(out), None


# ---------------------------------------------------------------------------
# route bookkeeping (reported, never a classification input)
# ---------------------------------------------------------------------------


def route_bookkeeping(root: Path) -> Dict[str, int]:
    """D-72: reported as its own field; never an input to classification."""
    open_routes = 0
    non_canonical = 0
    try:
        routes_dir = root / ".runtime" / "routes"
        if routes_dir.is_dir():
            for entry in routes_dir.iterdir():
                if entry.name.endswith(".outcome.json"):
                    continue
                if entry.suffix == ".json":
                    outcome = routes_dir / (entry.stem + ".outcome.json")
                    if not outcome.exists():
                        open_routes += 1
    except OSError:
        pass
    try:
        for entry in root.iterdir():
            if entry.is_file() and "route" in entry.name and entry.suffix == ".json":
                non_canonical += 1
    except OSError:
        pass
    for sub in ("routes", "_routes", ".routes"):
        try:
            directory = root / sub
            if directory.is_dir():
                for current, _dirs, files in os.walk(str(directory)):
                    for name in files:
                        if name.endswith(".json"):
                            non_canonical += 1
        except OSError:
            pass
    return {"open_routes": open_routes, "non_canonical_route_records": non_canonical}


# ---------------------------------------------------------------------------
# negative probe (active rows only)
# ---------------------------------------------------------------------------


def negative_probe(root: Path) -> Dict[str, Any]:
    legacy = P.check_write(root, root / "plans" / "_fleet-gate-probe" / "probe.md")
    shared = P.check_write(root, root / "shared" / "spec" / PROBE_REF / "revisions" / PROBE_RREV / "probe.md")
    return {
        "legacy_top_level": {"verdict": legacy["verdict"], "reason": legacy["reason"]},
        "shared_revision": {"verdict": shared["verdict"], "reason": shared["reason"]},
        "passed": legacy["reason"] == "legacy-top-level-write-denied"
                  and shared["reason"] == "shared-revision-immutable"
                  and legacy["verdict"] == shared["verdict"] == "deny",
    }


# ---------------------------------------------------------------------------
# D-85 gate extension: resplit/retire predicates (read-only, mutation 0)
# ---------------------------------------------------------------------------


def _empty_resplit_fields() -> Dict[str, Any]:
    return {
        "lumped_cycles_remaining": None, "lump_index_state": None,
        "legacy_top_level_retired": False, "resplit_hold": None, "supersession_divergent": [],
        **_empty_relayout_fields(),
    }


def _empty_relayout_fields() -> Dict[str, Any]:
    return {"readable_layout": None, "legacy_campaign_dirs": None, "legacy_cycle_dirs": None,
            "relayout_hold": None, "transition_window": None, **_empty_residue_fields()}


def _empty_residue_fields() -> Dict[str, Any]:
    return {"legacy_top_level": None, "legacy_top_level_files": None, "residue_deferred": None,
            "residue_trash_pending": None, "residue_hold": None}


def _residue_fields(root: Path) -> Dict[str, Any]:
    """W7H read-only view: what is still at the legacy top level, typed."""
    view = RES.status(root)
    return {
        "legacy_top_level": view["legacy_top_level"],
        "legacy_top_level_files": view["legacy_top_level_files"],
        "residue_deferred": len(view["deferred"]),
        "residue_trash_pending": view["trash_pending"],
        "residue_hold": view["residue_hold"],
    }


def _relayout_fields(root: Path) -> Dict[str, Any]:
    """W7I A-17.8 read-only view: legacy-shaped directories left, the typed
    relayout hold if a run is nonterminal, and the D-91 transition window."""
    view = RL.status(root)
    return {
        "readable_layout": view["readable_layout"],
        "legacy_campaign_dirs": view["legacy_campaign_dirs"],
        "legacy_cycle_dirs": view["legacy_cycle_dirs"],
        "relayout_hold": view["relayout_hold"],
        "transition_window": view["transition_window"],
        **_residue_fields(root),
    }


def _retirement_run_dirs(root: Path) -> List[Path]:
    mdir = C.migrations_dir(root)
    if not mdir.is_dir():
        return []
    try:
        return sorted(p for p in mdir.iterdir() if p.is_dir() and p.name.endswith("-retirement"))
    except OSError:
        return []


def _sha256_key(value: Optional[str]) -> Optional[str]:
    """`artifact_cutover.retire`'s journal rows carry a bare hex digest while the
    resplit retire-inventory's rows carry a `sha256:`-prefixed one -- normalize
    both to the bare form before comparing."""
    if not isinstance(value, str):
        return value
    return value[len("sha256:"):] if value.startswith("sha256:") else value


def _r4_retired_rows(root: Path) -> Tuple[bool, set]:
    """(True, rows) once an R4 run *completed* (report.json written past approval
    validation -- `artifact_cutover.retire` raises before writing it otherwise).
    `rows` is every `(source_locator, sha256)` that run's journal committed."""
    approved = False
    retired: set = set()
    for run_dir in _retirement_run_dirs(root):
        report = P._read_json(run_dir / "report.json")
        if not isinstance(report, dict) or report.get("kind") != "w7c-source-retirement" or report.get("dry_run"):
            continue
        journal_path = run_dir / "journal.jsonl"
        if not journal_path.is_file():
            continue
        approved = True
        for jrow in C._read_jsonl(journal_path):
            if jrow.get("action") == "retire_source" and jrow.get("commit_state") == "committed":
                retired.add((jrow.get("source_locator"), _sha256_key(jrow.get("sha256"))))
    return approved, retired


def legacy_top_level_retired(root: Path) -> bool:
    """D-85: judged from the R1-sealed retire ordered inventory (D-84). Absent
    inventory is fail-closed `False`; an empty inventory is `True`; otherwise
    `True` only when every entry is accounted for -- by a completed R4 run's evidence,
    or (for the `plans/stage-sessions` rows D-79 assigns to `C-RT`) by a re-verified
    relocation disposition -- and none of the still-outstanding source paths remain as
    a regular file. Reading that second source is an extension of D-85's wording and is
    reported as spec-impact for the closing W7G transaction.
    """
    inventory = RS.sealed_retire_inventory(root)
    if inventory is None:
        return False
    entries = inventory.get("entries") or []
    if not entries:
        return True
    wanted = {(e.get("source_locator"), _sha256_key(e.get("sha256"))) for e in entries}
    # D-79 C-RT relocations are accounted for by their own re-verified disposition
    # record, not by an R4 deletion that can never happen for them. Every other entry
    # still needs R4's approval and evidence, so a partly-disposed inventory is not a
    # shortcut past the retirement gate.
    disposed = {(source, _sha256_key(sha)) for source, sha in RS.stage_sessions_disposed_rows(root)}
    outstanding = wanted - disposed
    if outstanding:
        approved, retired = _r4_retired_rows(root)
        if not approved or not outstanding <= retired:
            return False
    for entry in entries:
        key = (entry.get("source_locator"), _sha256_key(entry.get("sha256")))
        if key in disposed:
            continue
        path = root / entry["source_locator"]
        if path.is_file() and not path.is_symlink():
            return False
    return True


def _resplit_fields(root: Path) -> Dict[str, Any]:
    hold = RS.resplit_hold(root)
    retired = legacy_top_level_retired(root)
    relayout = _relayout_fields(root)
    if hold is not None:
        return {
            "lumped_cycles_remaining": None, "lump_index_state": "resplit-in-progress",
            "legacy_top_level_retired": retired, "resplit_hold": hold, "supersession_divergent": [],
            **relayout,
        }
    scan = RS.scan_lumps(root)
    if scan.get("invalid"):
        return {
            "lumped_cycles_remaining": None, "lump_index_state": "lump-report-invalid",
            "legacy_top_level_retired": retired, "resplit_hold": None, "supersession_divergent": [],
            **relayout,
        }
    display = RS.lump_display_state(root)
    return {
        "lumped_cycles_remaining": display["lumped_cycles_remaining"],
        "lump_index_state": display["lump_index_state"],
        "legacy_top_level_retired": retired, "resplit_hold": None,
        "supersession_divergent": display["divergent"],
        **relayout,
    }


# ---------------------------------------------------------------------------
# row assembly
# ---------------------------------------------------------------------------


def audit_row(repo: Mapping[str, Any], *, timeout: float) -> Dict[str, Any]:
    repo_path = repo["repo_path"]
    row: Dict[str, Any] = {"repo_path": repo_path}
    if "note" in repo:
        row["note"] = repo["note"]
    root, reason = resolve_repo_root(repo_path, timeout=timeout)
    if root is None:
        row.update({
            "resolved_root": None, "state": "malformed", "reason": reason,
            "cutover_state": None, "activation_kind": None, "identity": None,
            "legacy_top_level": [], "legacy_top_level_complete": False,
            "route_bookkeeping": {"open_routes": 0, "non_canonical_route_records": 0},
            "probe": {"legacy_top_level": None, "shared_revision": None, "passed": None},
        })
        row.update(_empty_resplit_fields())
        return row
    klass = P.classify_root(root, collect_legacy_top_level=True)
    row.update({
        "resolved_root": str(root),
        "state": klass["state"],
        "reason": klass["reason"],
        "cutover_state": klass["cutover_state"],
        "activation_kind": klass["activation_kind"],
        "identity": klass["identity"],
        "legacy_top_level": klass["legacy_top_level"],
        "legacy_top_level_complete": klass["legacy_top_level_complete"],
        "route_bookkeeping": route_bookkeeping(root),
    })
    if klass["state"] == "active":
        row["probe"] = negative_probe(root)
        row.update(_resplit_fields(root))
    else:
        row["probe"] = {"legacy_top_level": None, "shared_revision": None, "passed": None}
        row.update(_empty_resplit_fields())
    return row


# ---------------------------------------------------------------------------
# waivers
# ---------------------------------------------------------------------------


def load_waivers(path: Path) -> Tuple[Dict[str, Any], str]:
    payload = P._read_json(Path(path))
    if payload is None:
        raise GateError("waivers-unreadable", str(path))
    if payload.get("schema_version") != 1:
        raise GateError("waivers-schema-unknown", str(payload.get("schema_version")))
    waivers = payload.get("waivers")
    if not isinstance(waivers, list):
        raise GateError("waivers-invalid", "waivers must be a list")
    normalized = {"schema_version": 1, "waivers": waivers}
    digest = P._digest(P._canonical(normalized))
    return normalized, digest


def match_waiver(entry: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Identify the row a waiver targets.

    `repo_path` is the primary match key when present -- matching by
    `canonical_root` would compare against `row["resolved_root"]` using the
    exact same realpath equality the subsequent foreign-root check performs,
    so a canonical_root-matched entry could never be flagged foreign by
    construction. Matching by `repo_path` lets `validate_time_bounded_grant`
    independently confirm the entry's declared `canonical_root` (if any)
    against the matched row's resolved root.

    Consequence: a waiver entry that carries only `canonical_root` (no
    `repo_path`) can never trigger `waiver-foreign-root` -- its match key
    and the value the foreign-root check compares against are the same
    field compared to the same value. This is accepted, intended
    non-coverage, not a gap to close by reordering the match keys.
    """
    repo_path = entry.get("repo_path")
    canonical_root = entry.get("canonical_root")
    matches: List[Dict[str, Any]] = []
    if repo_path:
        target = os.path.realpath(str(repo_path))
        matches = [row for row in rows if row.get("repo_path")
                  and os.path.realpath(row["repo_path"]) == target]
    elif canonical_root:
        target = os.path.realpath(str(canonical_root))
        matches = [row for row in rows if row.get("resolved_root")
                  and os.path.realpath(row["resolved_root"]) == target]
    if not matches:
        return None
    return matches[0]


def _apply_waivers(rows: Sequence[Dict[str, Any]], waivers_payload: Optional[Mapping[str, Any]],
                   *, now: Optional[float]) -> Dict[str, Any]:
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    by_repo_path: Dict[str, Dict[str, Any]] = {}
    if waivers_payload is None:
        for row in rows:
            row["waiver"] = {"status": "absent", "reason": None, "expires_at": None}
        return {"accepted": accepted, "rejected": rejected, "unmatched": unmatched}
    matched_repo_paths: set = set()
    for index, entry in enumerate(waivers_payload.get("waivers", [])):
        if not isinstance(entry, dict):
            unmatched.append({"entry_index": index, "reason": "waiver-unmatched-root"})
            continue
        row = match_waiver(entry, rows)
        if row is None:
            unmatched.append({"entry_index": index, "reason": "waiver-unmatched-root"})
            continue
        if row["repo_path"] in matched_repo_paths:
            unmatched.append({"entry_index": index, "reason": "waiver-duplicate-root"})
            continue
        matched_repo_paths.add(row["repo_path"])
        canonical_root = Path(row["resolved_root"]) if row.get("resolved_root") else Path(row["repo_path"])
        verdict = P.validate_time_bounded_grant(
            entry, canonical_root=canonical_root, required_fields=P.WAIVER_FIELDS, now=now)
        reason = f"waiver-{verdict['reason']}" if verdict["reason"] else None
        block = {"status": verdict["status"], "reason": reason, "expires_at": verdict["expires_at"]}
        row["waiver"] = block
        by_repo_path[row["repo_path"]] = {"repo_path": row["repo_path"], **block}
        if block["status"] == "accepted":
            accepted.append(by_repo_path[row["repo_path"]])
        else:
            rejected.append(by_repo_path[row["repo_path"]])
    for row in rows:
        if "waiver" not in row:
            row["waiver"] = {"status": "absent", "reason": None, "expires_at": None}
    return {"accepted": accepted, "rejected": rejected, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------


def evaluate(rows: Sequence[Mapping[str, Any]], *, waived: bool,
            require_resplit: bool = False, require_relayout: bool = False,
            require_residue: bool = False) -> Tuple[str, List[Dict[str, str]]]:
    blocking: List[Dict[str, str]] = []
    for row in rows:
        base_passed = row.get("state") == "active" and row.get("probe", {}).get("passed") is True
        resplit_passed = True
        if require_resplit:
            resplit_passed = (row.get("lumped_cycles_remaining") == 0
                              and row.get("legacy_top_level_retired") is True)
        relayout_passed = True
        if require_relayout:
            # A-17: no legacy-shaped directory left, no nonterminal run, and the
            # D-91 transition window closed by a completed relayout.
            relayout_passed = (row.get("readable_layout") == "readable"
                               and row.get("relayout_hold") is None
                               and row.get("transition_window") == "closed")
        residue_passed = True
        if require_residue:
            # W7H: the legacy top level holds nothing but typed deferrals, no
            # nonterminal run, and no trash still awaiting its approval gate.
            residue_passed = (row.get("legacy_top_level") in {"empty", "deferred-only"}
                              and row.get("residue_hold") is None
                              and row.get("residue_trash_pending") == 0)
        if base_passed and resplit_passed and relayout_passed and residue_passed:
            continue
        waiver = row.get("waiver") or {}
        if waived and waiver.get("status") == "accepted":
            continue
        if row.get("state") != "active":
            reason = row.get("reason") or row.get("state")
        elif not base_passed:
            reason = "negative-probe-failed"
        elif not resplit_passed:
            reason = "resplit-incomplete"
        elif not relayout_passed:
            reason = "relayout-incomplete"
        else:
            reason = "residue-remaining"
        blocking.append({"repo_path": row["repo_path"], "state": row.get("state"), "reason": reason})
    return ("incomplete" if blocking else "complete"), blocking


# ---------------------------------------------------------------------------
# --output placement
# ---------------------------------------------------------------------------


def validate_output_path(output: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    target = Path(output).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target = Path(os.path.realpath(str(target.parent))) / target.name
    for row in rows:
        root = row.get("resolved_root")
        if not root:
            continue
        rp = Path(os.path.realpath(root))
        if target == rp or rp in target.parents:
            raise GateError("output-inside-audited-root", str(target))
    return target


def _write_output(target: Path, payload: Any) -> None:
    caller_root, _reason = resolve_repo_root(str(Path.cwd()))
    if caller_root is not None:
        verdict = P.check_write(caller_root, target)
        if verdict["verdict"] != "allow":
            raise GateError("output-write-denied", verdict["reason"])
    P._write_atomic(target, P._json_bytes(payload))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True))


def _build_payload(*, command: str, roster: Dict[str, Any], roster_digest: str,
                   roster_path: str, rows: List[Dict[str, Any]],
                   waivers_path: Optional[str], waivers_digest: Optional[str]) -> Dict[str, Any]:
    by_state: Dict[str, int] = {}
    probe_failed: List[str] = []
    for row in rows:
        by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        if row["state"] == "active" and row.get("probe", {}).get("passed") is False:
            probe_failed.append(row["repo_path"])
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": P.CONTRACT,
        "kind": KIND,
        "command": command,
        "generated_at": P._rfc3339(),
        "fleet_id": roster["fleet_id"],
        "roster_path": roster_path,
        "roster_digest": roster_digest,
        "waivers_path": waivers_path,
        "waivers_digest": waivers_digest,
        "roots": rows,
        "summary": {"total": len(rows), "by_state": by_state, "probe_failed": probe_failed},
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - argparse plumbing
        self.exit(USAGE, f"{self.prog}: error: {message}\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _Parser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("audit")
    p.add_argument("--roster", required=True)
    p.add_argument("--output")
    p.add_argument("--timeout", type=float, default=RESOLVE_TIMEOUT_DEFAULT)

    p = sub.add_parser("complete")
    p.add_argument("--roster", required=True)
    p.add_argument("--waivers")
    p.add_argument("--output")
    p.add_argument("--timeout", type=float, default=RESOLVE_TIMEOUT_DEFAULT)
    p.add_argument("--require-resplit", action="store_true")
    p.add_argument("--require-relayout", action="store_true")
    p.add_argument("--require-residue", action="store_true")

    args = parser.parse_args(argv)
    try:
        roster, roster_digest = load_roster(Path(args.roster))
        rows = [audit_row(repo, timeout=args.timeout) for repo in roster["repos"]]
        if args.command == "audit":
            payload = _build_payload(
                command="audit", roster=roster, roster_digest=roster_digest,
                roster_path=str(args.roster), rows=rows, waivers_path=None, waivers_digest=None,
            )
            if args.output:
                target = validate_output_path(Path(args.output), rows)
                _write_output(target, payload)
            else:
                _print(payload)
            return OK
        # complete
        waivers_payload = None
        waivers_digest = None
        if args.waivers:
            waivers_payload, waivers_digest = load_waivers(Path(args.waivers))
        waivers_result = _apply_waivers(rows, waivers_payload, now=None)
        verdict, blocking = evaluate(rows, waived=waivers_payload is not None,
                                     require_resplit=args.require_resplit,
                                     require_relayout=args.require_relayout,
                                     require_residue=args.require_residue)
        payload = _build_payload(
            command="complete", roster=roster, roster_digest=roster_digest,
            roster_path=str(args.roster), rows=rows,
            waivers_path=str(args.waivers) if args.waivers else None, waivers_digest=waivers_digest,
        )
        payload["verdict"] = verdict
        payload["blocking"] = blocking
        payload["waivers"] = waivers_result
        if args.output:
            target = validate_output_path(Path(args.output), rows)
            _write_output(target, payload)
        else:
            _print(payload)
        return OK if verdict == "complete" else INCOMPLETE
    except GateError as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return INCOMPLETE


if __name__ == "__main__":
    raise SystemExit(main())
