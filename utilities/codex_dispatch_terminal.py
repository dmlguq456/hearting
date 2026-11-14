#!/usr/bin/env python3
"""Safe, exact-attempt inspection of shared dispatch JSONL terminal handoffs.

The compatibility parser returns the historical internal dictionary.  The
structured inspector and CLI expose only closed decision enums, bounded
failure-only excerpts, and a validated base64 path reference.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from collections.abc import Mapping
from pathlib import Path
import re
import subprocess
import sys

from dispatch_supervisor_terminal import (
    classify_session_result, opencode_terminal_boundary, supervisor_terminal_from_event,
)

# OPERATIONS §5.10 "Review verdict is a result, not a worker death": a review
# worker whose FAIL handoff names a readable in-root review artifact finished
# its contract -- it recorded blocking findings. Its row closes with this typed
# completion note instead of `dead-worker-fail`; the verdict axis
# (`failure_class=fail`) is unchanged. Only a `worker_type=review` row with a
# readable artifact earns it; every other FAIL keeps the dead-worker note.
from dispatch_attempt_policy import REVIEW_BLOCKING_NOTE
from route_authority import HANDOFF_RE, handoff_search, pass_blocker_note, pass_blocker_violation
REVIEW_WORKER_TYPE = "review"


ROOT = Path(__file__).resolve().parents[1]
# The handoff is the trailing three lines of the final message. Anchoring at the
# end rather than across the whole message keeps every guarantee the strict form
# had — only the last block counts, so an earlier decoy never wins, and nothing
# outside the captured groups reaches a result — while surviving the one failure
# a prompt cannot prevent: a worker that prepends a summary sentence to an
# otherwise exact handoff. Two 2026-07-28 pipelines died that way, each with a
# correct artifact already on disk (plans/2026-07-29_handoff-tail-anchor).
_HANDOFF_RE = HANDOFF_RE  # the one envelope pattern (route_authority)
_SANDBOX_INIT_RE = re.compile(
    r"bwrap: Can't bind mount [^\n]+ on [^\n]+/\.codex:"
    r"[^\n]*Unable to mount source on destination: No such file or directory",
    re.I,
)
# Telemetry the codex app-server supervisor can physically emit between a
# turn's final `agent_message` and that turn's `turn.completed`. Producer:
# `utilities/codex-app-server-supervisor.py` `run_turn`, the
# `thread/tokenUsage/updated` branch -- it emits after the `item/completed`
# that set `final_text`, so a valid handoff sits two rows before the terminal
# instead of one. The checked `dispatch.supervisor.turn.completed` boundary
# can now occur there too; the walkback below validates its completed status
# and runtime identity. Other `dispatch.supervisor.*` emits on that path
# (`owner-boundary`, `launch-settled`, `parked`, `reparked`, `resumed`,
# `redelivery-suppressed`, `reconciled`, `join-observed`) is followed by a
# `continue` that starts a new turn, and `delivery-timing` is emitted after
# `turn.completed`; none of them can land inside this window. Keep this an
# A completed input receipt can also follow that checked boundary. It is
# validated against the boundary below, not treated as generic telemetry.
# Keep an exact allowlist rather than a `dispatch.supervisor.` prefix rule: a prefix
# would also skip a future observation row that is emitted for a reason we have
# not checked, and hide a real work item behind it.
_TERMINAL_TELEMETRY_TYPES = frozenset({"dispatch.supervisor.token_usage"})
_MAX_TAIL_BYTES = 1024 * 1024
_DETAIL_LIMIT = 512

ARTIFACT_STATES = frozenset(
    {"unchecked", "none", "readable", "missing", "outside-root", "unsafe-root"}
)
_TERMINAL_SOURCES = (
    "exact-turn-completed",
    "exact-claude-result",
    "exact-opencode-result",
    "exact-step-finish-stop",
)
# Which inspector sources mark each harness's own exact turn boundary, and the
# name a person reads for it. Every recovery and liveness reader asks these
# instead of naming one harness's event.
EXACT_BOUNDARY_SOURCES = {
    "codex": frozenset({"exact-turn-completed"}),
    "claude": frozenset({"exact-claude-result"}),
    "opencode": frozenset({"exact-step-finish-stop", "exact-opencode-result"}),
}
BOUNDARY_LABELS = {
    "exact-turn-completed": "turn.completed",
    "exact-claude-result": "Claude result",
    "exact-step-finish-stop": "OpenCode step finish",
    "exact-opencode-result": "OpenCode result",
}


def exact_boundary(terminal: dict, harness: str) -> bool:
    """The inspected log ends on `harness`'s own exact turn boundary.

    Claude's `result` row counts only with its native completion proof, as before.
    """
    source = terminal.get("source")
    if source not in EXACT_BOUNDARY_SOURCES.get(harness, ()):
        return False
    return harness != "claude" or terminal.get("native_completed") is True


def boundary_label(source) -> str:
    return BOUNDARY_LABELS.get(source, str(source or "-"))
_LEGAL_WIRE = frozenset(
    {
        *(
            (0, "valid", source, "PASS", artifact, "none")
            for source in _TERMINAL_SOURCES
            for artifact in ("none", "readable")
        ),
        *(
            (0, "valid", source, verdict, artifact, blocker)
            for source in _TERMINAL_SOURCES
            for verdict in ("FAIL", "BLOCKED")
            for artifact in ("none", "readable")
            for blocker in ("none", "worker-reported")
        ),
        (2, "absent", "none", "-", "unchecked", "-"),
        *(
            (3, "invalid", source, "-", artifact, "contract-violation")
            for source in _TERMINAL_SOURCES
            for artifact in ("unchecked", "missing", "outside-root")
        ),
        (4, "error", "runtime-error", "-", "unchecked", "contract-violation"),
        (4, "error", "runtime-error", "-", "unsafe-root", "contract-violation"),
    }
)


def _result(
    exit_code: int,
    state: str,
    source: str,
    verdict: str,
    artifact_state: str,
    blocker_reason: str,
    *,
    reason: str,
    **extra: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "exit_code": exit_code,
        "state": state,
        "source": source,
        "verdict": verdict,
        "artifact_state": artifact_state,
        "blocker_reason": blocker_reason,
        "reason": reason,
    }
    row.update(extra)
    return row


def _tail_lines(path: Path) -> list[str]:
    size = path.stat().st_size
    start = max(0, size - _MAX_TAIL_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    lines = data.splitlines()
    if start and lines:
        lines = lines[1:]
    return [line.decode("utf-8", "strict") for line in lines]


def _codex_final_agent_message(
    rows: list[dict], terminal_index: int
) -> str | None:
    """Walk back from `turn.completed` to this turn's final `agent_message`.

    Only `_TERMINAL_TELEMETRY_TYPES` rows are stepped over. Anything else stops
    the walk fail-closed -- a real work item (`item.started`/`item.completed`
    for a command or tool) means the message was not final, a
    `dispatch.supervisor.turn.started` is the previous turn's boundary, and an
    unknown, malformed, or text-less row is unread evidence. Stopping at the
    first such row is what keeps an older turn's stale handoff from standing in
    for a missing final one.
    """

    completed_inputs: list[dict] = []
    for index in range(terminal_index - 1, -1, -1):
        row = rows[index]
        if row.get("type") == "dispatch.supervisor.input":
            requests = row.get("request_ids")
            if (row.get("state") != "turn-completed"
                    or not isinstance(row.get("attempt_id"), str) or not row["attempt_id"]
                    or not isinstance(row.get("turn_id"), str) or not row["turn_id"]
                    or not isinstance(requests, list) or not requests
                    or any(not isinstance(value, str) or not value for value in requests)
                    or len(set(requests)) != len(requests)):
                return None
            completed_inputs.append(row)
            continue
        if row.get("type") == "dispatch.supervisor.turn.completed":
            # The runtime boundary is distinct from the owner's terminal
            # envelope. Failed/malformed boundaries cannot bridge a handoff.
            if (row.get("status") == "completed"
                    and isinstance(row.get("thread_id"), str) and row["thread_id"]
                    and isinstance(row.get("turn_id"), str) and row["turn_id"]):
                if completed_inputs:
                    # The caller selects the exact attempt log. Within it this
                    # acknowledgement must belong to the completed turn, not
                    # merely resemble a supervisor observation.
                    terminal_thread = rows[terminal_index].get("thread_id")
                    if (terminal_thread is not None and terminal_thread != row["thread_id"]
                            or any(receipt["turn_id"] != row["turn_id"]
                                   or receipt.get("thread_id", row["thread_id"]) != row["thread_id"]
                                   for receipt in completed_inputs)
                            or len({receipt["attempt_id"] for receipt in completed_inputs}) != 1):
                        return None
                    completed_inputs.clear()
                continue
            return None
        if row.get("type") == "dispatch.supervisor.terminal":
            # This is the writer input logged just before registry settlement.
            # It carries no final text: the native agent_message still supplies
            # and validates the handoff below.
            if supervisor_terminal_from_event(row) is None:
                return None
            continue
        if row.get("type") in _TERMINAL_TELEMETRY_TYPES:
            continue
        if completed_inputs or row.get("type") != "item.completed":
            return None
        item = row.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            return None
        text = item.get("text")
        return text if isinstance(text, str) else None
    return None


def _claude_completion_tail(lines: list[str], session_id: object) -> bool:
    """Only the published native prompt-suggestion event may follow a result.

    SDKPromptSuggestionMessage (Agent SDK 0.3.289) is informational: it does
    not start another turn. Raw lines still have to parse, so an ignored partial
    record cannot disappear through the general inspector's stderr filtering.
    """
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            return False
        if (not isinstance(row, dict)
                or set(row) != {"type", "suggestion", "uuid", "session_id"}
                or row.get("type") != "prompt_suggestion"
                or not isinstance(row.get("suggestion"), str)
                or not isinstance(row.get("uuid"), str) or not row["uuid"]
                or not isinstance(session_id, str) or not session_id
                or row.get("session_id") != session_id):
            return False
    return True


def _read_terminal(path: str | Path | None) -> dict[str, object]:
    if not path:
        return _result(
            2, "absent", "none", "-", "unchecked", "-", reason="log-file-absent"
        )
    log_path = Path(path)
    try:
        lines = _tail_lines(log_path)
    except FileNotFoundError:
        return _result(
            2, "absent", "none", "-", "unchecked", "-", reason="log-file-absent"
        )
    except (OSError, UnicodeDecodeError):
        return _result(
            4,
            "error",
            "runtime-error",
            "-",
            "unchecked",
            "contract-violation",
            reason="log-unreadable",
        )

    rows: list[dict] = []
    row_line_indices: list[int] = []
    for line_index, line in enumerate(lines):
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            # Codex stderr is intentionally retained in this same exact attempt
            # log.  Non-JSON lines are private debugging data, never handoff data.
            continue
        if isinstance(value, dict):
            rows.append(value)
            row_line_indices.append(line_index)

    terminal_index = next(
        (
            index
            for index in range(len(rows) - 1, -1, -1)
            if rows[index].get("type") in {"turn.completed", "result"}
        ),
        None,
    )
    opencode_final_message: str | None = None
    if terminal_index is None:
        terminal_index, opencode_final_message = opencode_terminal_boundary(rows)
    if terminal_index is None:
        return _result(
            2, "absent", "none", "-", "unchecked", "-", reason="terminal-event-absent"
        )

    terminal_row = rows[terminal_index]
    terminal_event = str(terminal_row.get("type"))
    if terminal_event == "turn.completed":
        terminal_source = "exact-turn-completed"
    elif terminal_event == "step_finish":
        terminal_source = "exact-step-finish-stop"
    else:
        terminal_source = ("exact-opencode-result" if terminal_row.get("runtime") == "opencode"
                           else "exact-claude-result")
    final_message: str | None = None
    if terminal_event == "result":
        subtype = terminal_row.get("subtype")
        if terminal_row.get("is_error") is True or subtype not in {None, "success"}:
            # The live supervisor already separates a rate-limit or auth death
            # from a broken envelope (`classify_claude_result`). This post-hoc
            # reader used to call all three "claude-result-runtime-error", so
            # the same 429 read as `dead-capacity` while the supervisor watched
            # and as an envelope contract violation once it was gone -- which
            # is how a `top` review that simply ran out of Fable quota was
            # reported as a malformed handoff (2026-09-10, att-2164dce2).
            # One classifier, both readers.
            runtime = "opencode" if terminal_row.get("runtime") == "opencode" else "claude"
            supervised = classify_session_result(terminal_row, 1, runtime=runtime)
            from dispatch_capacity_evidence import native_quota
            quota = native_quota(rows[:terminal_index + 1], observed_at=log_path.stat().st_mtime)
            quota_fields = ({"quota_window": quota["window"], "quota_reset_epoch": str(quota["reset_epoch"]),
                             "quota_model_scope": quota["model_scope"]} if quota else {})
            return _result(
                3,
                "invalid",
                terminal_source,
                "-",
                "unchecked",
                supervised.failure_class if supervised.failure_class in {"capacity", "auth"} else "contract-violation",
                # the runtime case keeps its long-standing string; only the
                # two it used to swallow get their own name
                reason=(f"{runtime}-result-runtime-error"
                        if supervised.failure_class == "runtime"
                        else f"{runtime}-result-{supervised.failure_class}"),
                failure_note=supervised.note,
                failure_class=supervised.failure_class,
                **quota_fields,
            )
        text = terminal_row.get("result")
        final_message = text if isinstance(text, str) else None
    elif terminal_event == "step_finish":
        final_message = opencode_final_message
    else:
        final_message = _codex_final_agent_message(rows, terminal_index)
    if final_message is None:
        return _result(
            3,
            "invalid",
            terminal_source,
            "-",
            "unchecked",
            "contract-violation",
            reason="missing-final-agent-message",
        )
    # search, not fullmatch: the pattern itself anchors the block to the end of
    # the message, so anything before it is ignored rather than fatal. A
    # trailing fence or up to two sentences are tolerated the same way (G1).
    handoff = handoff_search(final_message)
    if handoff is None:
        return _result(
            3,
            "invalid",
            terminal_source,
            "-",
            "unchecked",
            "contract-violation",
            reason="malformed-handoff",
        )

    if pass_blocker_violation(handoff["verdict"], handoff["blocker"]):
        return _result(
            3,
            "invalid",
            terminal_source,
            "-",
            "unchecked",
            "contract-violation",
            reason="pass-blocker-not-none",
        )

    sandbox_init = False
    diagnostic = None
    diagnostic_start = 0
    for index, row in enumerate(rows[:terminal_index]):
        if row.get("type") == "dispatch.supervisor.turn.started":
            diagnostic_start = index + 1
    for row in rows[diagnostic_start:terminal_index]:
        if row.get("type") != "item.completed":
            continue
        item = row.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        output = item.get("aggregated_output")
        failed = item.get("exit_code") not in (None, 0)
        if failed and isinstance(output, str):
            diagnostic = output
            if _SANDBOX_INIT_RE.search(output):
                sandbox_init = True

    verdict = handoff["verdict"]
    # RA-8: a PASS whose blocker is `none (note)` is a PASS; the note is kept.
    note = pass_blocker_note(handoff["blocker"]) if verdict == "PASS" else None
    normalized = {"envelope_normalized": "pass-blocker-note",
                  "pass_note_excerpt": _escape_and_bound(note)[0]} if note else {}
    if handoff.get("disposition"):
        # A closed disposition word read as the verdict it names (route_authority.handoff_search).
        normalized["disposition"] = _escape_and_bound(handoff["disposition"])[0]
        normalized["envelope_normalized"] = ",".join(
            filter(None, (normalized.get("envelope_normalized"), "verdict-disposition")))
    failure_note = (
        "dead-sandbox-init"
        if verdict == "BLOCKED" and sandbox_init
        else "dead-worker-blocked"
        if verdict == "BLOCKED"
        else "dead-worker-fail"
        if verdict == "FAIL"
        else ""
    )
    return _result(
        0,
        "valid",
        terminal_source,
        verdict,
        "unchecked",
        "none" if handoff["blocker"] == "none" or note is not None else "worker-reported",
        reason="none",
        **normalized,
        artifact=handoff["artifact"],
        blocker=handoff["blocker"],
        diagnostic=diagnostic,
        failure_note=failure_note,
        failure_class="sandbox-init" if sandbox_init else verdict.lower(),
        terminal_event=terminal_event,
        native_completed=(
            terminal_source == "exact-claude-result"
            and terminal_row.get("subtype") in (None, "success")
            and (terminal_row.get("is_error") is False or terminal_row.get("is_error") is None)
            and terminal_row.get("terminal_reason") in (None, "completed")
            and terminal_row.get("stop_reason") in (None, "end_turn")
            and sum(row.get("type") == "result" for row in rows) == 1
            and _claude_completion_tail(
                lines[row_line_indices[terminal_index] + 1:], terminal_row.get("session_id")
            )
        ),
        log_file=str(log_path),
    )


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


# Main worktree per repository (its common git directory), for this process.
_PRIMARY_WORKTREES: dict[str, Path] = {}


def _common_git_dir(worktree: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return None
    common = result.stdout.strip()
    return common if not result.returncode and os.path.isabs(common) else None


def _primary_worktree(worktree: Path) -> Path | None:
    """The repository's main worktree, asked of `git worktree list` once per repository.

    That listing visits every linked worktree (0.6 s on a NAS repository with
    many of them) and one status pass inspects several attempts of the same
    repository; the common git directory names the repository cheaply. The
    main worktree of a common directory does not move while it stays there.
    Only an answer is kept: a failure is asked again next time.
    """
    common = _common_git_dir(worktree)
    if common is not None and common in _PRIMARY_WORKTREES:
        return _PRIMARY_WORKTREES[common]
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "worktree", "list", "--porcelain"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if result.returncode:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            try:
                primary = Path(line[9:]).resolve(strict=True)
            except OSError:
                return None
            if common is not None:
                _PRIMARY_WORKTREES[common] = primary
            return primary
    return None


def _resolve_safe_root(
    worktree: str | Path | None, artifact_root_metadata: str | Path | None, *,
    use_metadata_root: bool = False,
) -> tuple[Path | None, str]:
    if worktree is None:
        return None, "worktree-missing"
    worktree_path = Path(worktree)
    if not worktree_path.is_absolute() or _has_control(str(worktree_path)):
        return None, "worktree-unsafe"
    try:
        worktree_resolved = worktree_path.resolve(strict=True)
    except OSError:
        return None, "worktree-unavailable"
    if not worktree_resolved.is_dir():
        return None, "worktree-unavailable"

    override = (str(artifact_root_metadata) if use_metadata_root and artifact_root_metadata
                else os.environ.get("AGENT_ARTIFACT_ROOT"))
    if override:
        override_path = Path(override)
        if not override_path.is_absolute() or _has_control(override):
            return None, "artifact-root-relative"
        try:
            override_resolved = override_path.resolve(strict=True)
        except OSError:
            override_resolved = None
        if override_resolved is not None and override_resolved != Path(os.path.abspath(override_path)):
            return None, "artifact-root-symlink-escaped"

    try:
        child_env = os.environ.copy()
        if use_metadata_root and artifact_root_metadata:
            child_env["AGENT_ARTIFACT_ROOT"] = str(artifact_root_metadata)
        resolved = subprocess.run(
            [str(ROOT / "utilities" / "artifact-root.sh"), str(worktree_resolved)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=child_env,
        )
    except OSError:
        return None, "artifact-root-resolver-failed"
    raw = resolved.stdout.rstrip("\n")
    if resolved.returncode or not raw or "\n" in raw or _has_control(raw):
        return None, "artifact-root-resolver-failed"
    root_path = Path(raw)
    if not root_path.is_absolute():
        return None, "artifact-root-relative"
    try:
        root = root_path.resolve(strict=True)
    except OSError:
        return None, "artifact-root-missing"
    if root != Path(os.path.abspath(root_path)):
        return None, "artifact-root-symlink-escaped"
    if not root.is_dir():
        return None, "artifact-root-not-directory"
    primary = _primary_worktree(worktree_resolved)
    if (
        root == Path("/")
        or root == worktree_resolved
        or root in worktree_resolved.parents
        or primary is not None
        and (root == primary or root in primary.parents)
    ):
        return None, "artifact-root-over-broad"

    shadow_candidates = {
        worktree_resolved / ".agent_reports",
        worktree_resolved / ".claude_reports",
    }
    if primary is not None and primary != worktree_resolved and root in {
        candidate.resolve(strict=False) for candidate in shadow_candidates
    }:
        return None, "artifact-root-shadow"

    if artifact_root_metadata is None or str(artifact_root_metadata) in {"", "-"}:
        return None, "artifact-root-metadata-missing"
    metadata_path = Path(artifact_root_metadata)
    if not metadata_path.is_absolute() or _has_control(str(metadata_path)):
        return None, "artifact-root-metadata-unsafe"
    try:
        metadata_root = metadata_path.resolve(strict=True)
    except OSError:
        return None, "artifact-root-metadata-missing"
    if metadata_root != root:
        return None, "artifact-root-mismatch"
    return root, "none"


ARTIFACT_MAX_ENTRIES = 20000
ARTIFACT_MAX_BYTES = 2 * 1024**3


def directory_artifact_reason(artifact_path: Path, root: Path | None = None) -> str:
    """Return a typed refusal for a directory that cannot stand as a deliverable.

    ONE definition, used by both the envelope inspector and
    `capability-route.evidence_digest`. Splitting it left the automated path
    guarded and the documented manual `capability-route.py complete --evidence`
    path open — the same fail-open, one door over (review R2).

    An existing, readable directory is not by itself evidence of work. The
    harness pre-creates the cycle's `artifacts/` directory before a worker's
    first write (`artifact_producer.begin`) and exports that exact path as
    `AGENT_ARTIFACT_OUTPUT_DIR`, so accepting any directory would let a worker
    that produced nothing name the path it was handed and pass. Require at least
    one readable regular file somewhere in the tree.

    `root` is optional: the inspector has an artifact root to contain members
    against, the digest does not. Everything except containment therefore holds
    at both doors — size, entry count, emptiness and unreadable members all
    refuse identically, so a deliverable cannot pass classification and then be
    refused at completion. **Member containment is the one inspector-only rule**:
    a marker written through `complete --evidence` can still attest a tree with a
    member pointing outside the root. Nothing leaks (the digest records link
    target text and never follows), but the two doors are not identical there.
    """

    # Access first: an unreadable directory yields nothing from `rglob` (it
    # swallows PermissionError), which would read as "empty" instead of "cannot
    # be read" and send an operator after the wrong problem.
    if not os.access(artifact_path, os.R_OK | os.X_OK):
        return "artifact-unreadable"
    entries = 0
    total = 0
    has_file = False
    unreadable_member = False
    try:
        for child in artifact_path.rglob("*"):
            entries += 1
            if entries > ARTIFACT_MAX_ENTRIES:
                return "artifact-directory-too-large"
            if root is not None:
                try:
                    child.resolve(strict=False).relative_to(root)
                except (OSError, ValueError):
                    return "artifact-member-outside-root"
            if child.is_symlink() or not child.is_file():
                continue
            if not os.access(child, os.R_OK):
                unreadable_member = True
                continue
            try:
                total += child.stat().st_size
            except OSError:
                unreadable_member = True
                continue
            if total > ARTIFACT_MAX_BYTES:
                return "artifact-directory-too-large"
            has_file = True
    except OSError:
        return "artifact-unreadable"
    # ANY unreadable member, not just "no readable file at all": the digest
    # cannot hash what it cannot read, so a tree that classifies valid here and
    # then refuses at completion would be the disagreement this shared rule
    # exists to remove.
    if unreadable_member:
        return "artifact-unreadable-member"
    if has_file:
        return ""
    if entries:
        # Not empty — it holds only links, directories or non-regular members,
        # none of which the digest can attest as content.
        return "artifact-no-regular-file"
    return "artifact-empty-directory"


def same_tail_artifact(candidate: Path, root: Path) -> Path | None:
    """The one existing path under ``root`` that a missing absolute artifact path
    names by the same tail after the root's own directory name -- a mistyped
    parent such as ``/home/Project_Zoo/...`` for ``/home/example/Project_Zoo/...``
    (RA-8). None when no such path exists or more than one does; the caller
    still checks that the result stays inside ``root``.
    """
    found = set()
    parts = candidate.parts
    for index, part in enumerate(parts):
        rest = parts[index + 1:]
        if part != root.name or not rest or any(item in {"", ".", ".."} for item in rest):
            continue
        target = root.joinpath(*rest)
        if os.path.lexists(target):
            found.add(target)
    return next(iter(found)) if len(found) == 1 else None


def relative_artifact(
    artifact: str, root: Path, worktree: str | Path | None, *, bound_output: Path | None = None,
) -> dict[str, object]:
    """Where a relative ``artifact:`` value points: the artifact root first, then the worktree.

    Returns ``{"path": <resolved in-root path> | None, "reason", "tried", "artifact_state"}``.
    One existing in-root target is the artifact. None existing (`missing`), a
    target outside the root (`outside-root`), or two different targets
    (`unchecked`) leave the envelope invalid with the same
    `artifact-outside-root` reason a relative path always had, so every reader
    that holds or refuses on it keeps doing so; ``tried`` names every path that
    was looked at so the next attempt can report the right one.
    """
    relative = Path(artifact)
    tried = ([bound_output] if bound_output is not None else []) + [root / relative] + (
        [Path(worktree) / relative] if worktree else [])
    if any(part in {"", ".", ".."} for part in relative.parts):
        return {"path": None, "reason": "artifact-outside-root", "tried": tried, "artifact_state": "outside-root"}
    found: list[Path] = []
    outside = False
    for path in tried:
        if not os.path.lexists(path):
            continue
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            outside = True
            continue
        if resolved not in found:
            found.append(resolved)
    if len(found) == 1:
        return {"path": found[0], "reason": "none", "tried": tried, "artifact_state": "readable"}
    if found:
        return {"path": None, "reason": "artifact-outside-root", "tried": found, "artifact_state": "unchecked"}
    if outside:
        return {"path": None, "reason": "artifact-outside-root", "tried": tried, "artifact_state": "outside-root"}
    return {"path": None, "reason": "artifact-outside-root", "tried": tried, "artifact_state": "missing"}


def _bound_terminal_output(
    artifact: str, binding: Mapping[str, object] | None, *, root: Path,
    worktree: str | Path | None,
) -> tuple[Path | None, str | None]:
    """Use the exact route lineage, with owner binding when the row has one."""
    if not isinstance(binding, Mapping):
        return None, None
    route_file = binding.get("route_file")
    route_id = binding.get("route_id")
    route_hash = binding.get("route_hash")
    node_id = binding.get("route_node")
    attempt_id = binding.get("attempt_id")
    worker_type = binding.get("worker_type")
    has_parent_binding = "parent_attempt_id" in binding
    parent_attempt = binding.get("parent_attempt_id")
    if worker_type == "owner":
        producer_attempt_id = attempt_id
        require_producer_binding = True
    elif has_parent_binding:
        # A supplied parent is authoritative. Missing, malformed, or conflicting
        # producer bindings must not be reinterpreted as a pre-owner frame.
        if not isinstance(parent_attempt, str) or not parent_attempt:
            return None, None
        producer_attempt_id = parent_attempt
        require_producer_binding = True
    else:
        # The one legacy shape without a parent owner is a registered depth-1
        # frame. Its exact sealed route and admitted cycle are its authority.
        if (worker_type != "frame"
                or str(binding.get("dispatch_depth", "")) != "1"):
            return None, None
        producer_attempt_id = None
        require_producer_binding = False
    if not all(isinstance(value, str) and value for value in
               (route_file, route_id, route_hash, node_id, attempt_id)):
        return None, None
    try:
        from artifact_context import ArtifactContextError, resolve_context
        context = resolve_context(
            environ={}, route=str(route_file), node_id=str(node_id),
            parent_attempt_id=(str(producer_attempt_id) if producer_attempt_id is not None else None),
            explicit={"artifact_root": str(root), "source_worktree": str(worktree) if worktree else None},
            require_producer_binding=require_producer_binding,
        )
        if (not context.route or context.route.get("route_id") != route_id
                or context.route.get("route_hash") != route_hash
                or context.node_id != node_id):
            return None, None
        if not require_producer_binding and (
                not context.node or context.node.get("worker_type") != "frame"
                or str(context.node.get("dispatch_depth", "")) != "1"):
            return None, None
        return context.resolve_terminal_artifact(artifact), None
    except ArtifactContextError as exc:
        if exc.code in {"artifact-terminal-output-symlink", "artifact-output-outside-root",
                        "artifact-output-outside-node-scope"}:
            return None, exc.code
        return None, None


ARTIFACT_HINT_KEYS = ("artifact_base_root_b64", "artifact_candidates_b64")


def artifact_hint(root: Path, tried) -> dict[str, str]:
    """The base root and the exact paths an invalid artifact was checked against."""
    return {"artifact_base_root_b64": _encode_path(root),
            "artifact_candidates_b64": _encode_path(Path("\n".join(str(path) for path in tried)))}


def _encode_path(path: Path) -> str:
    return base64.urlsafe_b64encode(str(path).encode("utf-8")).decode("ascii").rstrip("=")


def _escape_and_bound(value: str) -> tuple[str, int]:
    chunks: list[str] = []
    size = 0
    truncated = 0
    for char in value:
        code = ord(char)
        if char == "\\":
            chunk = "\\\\"
        elif char == "\n":
            chunk = "\\n"
        elif char == "\r":
            chunk = "\\r"
        elif char == "\t":
            chunk = "\\t"
        elif code < 32 or code == 127:
            chunk = f"\\x{code:02x}"
        elif code in (0x2028, 0x2029):
            chunk = f"\\u{code:04x}"
        else:
            chunk = char
        encoded = chunk.encode("utf-8")
        if size + len(encoded) > _DETAIL_LIMIT:
            truncated = 1
            break
        chunks.append(chunk)
        size += len(encoded)
    return "".join(chunks), truncated


def inspect_terminal_attempt(
    path: str | Path | None,
    *,
    worktree: str | Path | None,
    artifact_root_metadata: str | Path | None,
    include_failure_detail: bool = False,
    worker_type: str | None = None,
    artifact_binding: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return the normalized, root-bound result for one exact attempt log.

    ``artifact_binding`` is the exact attempt row when the caller has it; it
    supplies the sealed route/node/producer context for declared cycle-relative
    artifacts. ``worker_type`` is the registry row's `worker_type`; passing `review` lets a
    FAIL handoff with a readable in-root artifact carry ``failure_note``
    ``completed-review-blocking`` (a finished review) instead of
    ``dead-worker-fail`` (a dead worker). Callers that do not know the worker
    type keep the compatibility note.
    """

    return inspect_terminal_value(_read_terminal(path), worktree=worktree,
        artifact_root_metadata=artifact_root_metadata, include_failure_detail=include_failure_detail,
        worker_type=worker_type, artifact_binding=artifact_binding)


def inspect_terminal_value(parsed, *, worktree, artifact_root_metadata,
                           include_failure_detail=False, worker_type=None, artifact_binding=None):
    """The same root/placement checks for a live final result and its later log."""
    if parsed["state"] != "valid":
        return parsed

    root, root_reason = _resolve_safe_root(
        worktree, artifact_root_metadata, use_metadata_root=artifact_binding is not None)
    if root is None:
        return _result(
            4,
            "error",
            "runtime-error",
            "-",
            "unsafe-root",
            "contract-violation",
            reason=root_reason,
        )

    artifact = str(parsed["artifact"])
    artifact_path = None
    if artifact == "-":
        parsed["artifact_state"] = "none"
    else:
        candidate = Path(artifact)
        if _has_control(artifact):
            return _result(
                3,
                "invalid",
                str(parsed["source"]),
                "-",
                "outside-root",
                "contract-violation",
                reason="artifact-outside-root",
            )
        if not candidate.is_absolute():
            # A route-bound node may name its exact declared cycle output. The
            # exact attempt metadata supplies the sealed context; inherited
            # process environment never participates in this post-exit lookup.
            bound_output, bound_error = _bound_terminal_output(
                artifact, artifact_binding, root=root, worktree=worktree)
            if bound_error:
                return _result(
                    3, "invalid", str(parsed["source"]), "-", "outside-root",
                    "contract-violation", reason="artifact-outside-root",
                    **artifact_hint(root, [root / candidate]),
                )
            # Preserve the established root/worktree-relative compatibility
            # paths, including its refusal when more than one target exists.
            relative = relative_artifact(artifact, root, worktree, bound_output=bound_output)
            if relative["path"] is None:
                return _result(
                    3, "invalid", str(parsed["source"]), "-", str(relative["artifact_state"]),
                    "contract-violation", reason=str(relative["reason"]),
                    **artifact_hint(root, relative["tried"]),
                )
            parsed["artifact_named_path_b64"] = _encode_path(candidate)
            parsed["envelope_normalized"] = ",".join(
                filter(None, (parsed.get("envelope_normalized"), "artifact-relative")))
            candidate = relative["path"]
        # RA-8: a missing path that names one existing file under the root by
        # the same tail is that file; the path the worker wrote is kept.
        named = same_tail_artifact(candidate, root) if not os.path.lexists(candidate) else None
        try:
            artifact_path = (named or candidate).resolve(strict=False)
            artifact_path.relative_to(root)
        except (OSError, ValueError):
            return _result(
                3,
                "invalid",
                str(parsed["source"]),
                "-",
                "outside-root",
                "contract-violation",
                reason="artifact-outside-root",
                **artifact_hint(root, [candidate]),
            )
        if named is not None:
            parsed["artifact_named_path_b64"] = _encode_path(candidate)
            parsed["envelope_normalized"] = ",".join(
                filter(None, (parsed.get("envelope_normalized"), "artifact-same-tail")))
        elif not os.path.lexists(candidate):
            # An earlier release's checkpoint may have moved a cycle's loose output
            # into its declared bucket after the worker wrote its final handoff.
            # Follow only the producer's recorded same-cycle move, then recheck the
            # root. This keeps the exact handoff useful without accepting an
            # arbitrary replacement file at the old or new path.
            from artifact_producer import resolve_placed_output
            try:
                artifact_path = resolve_placed_output(candidate).resolve(strict=False)
                artifact_path.relative_to(root)
            except (OSError, ValueError):
                return _result(
                    3, "invalid", str(parsed["source"]), "-", "outside-root",
                    "contract-violation", reason="artifact-outside-root",
                )
            if artifact_path != candidate.resolve(strict=False):
                parsed["artifact_origin_path_b64"] = _encode_path(candidate)
        # A worker's artifact is legitimately either one file or one directory of
        # them: a cycle's document bucket, a plans directory, an evidence tree. The
        # contract says "an in-root artifact", never "a regular file", and the
        # worker prompt asks for a path. Requiring `is_file()` silently converted
        # every directory-shaped deliverable into `dead-invalid-envelope` — three
        # of the five owners lost on 2026-09-04 (att-8864fd04, att-93a8e494,
        # att-836c2026) had emitted a well-formed `verdict: PASS` envelope and a
        # readable directory holding their output.
        # The artifact root itself is not a deliverable — naming it says no more
        # than naming nothing, and it would let any run "produce" the whole tree.
        # An artifact must be a real member of the root.
        names_root = artifact_path == root
        directory_reason = ""
        if not names_root and artifact_path.is_dir():
            directory_reason = directory_artifact_reason(artifact_path, root)
        readable = not names_root and not directory_reason and (
            (artifact_path.is_file() and os.access(artifact_path, os.R_OK))
            or (artifact_path.is_dir() and os.access(artifact_path, os.R_OK | os.X_OK))
        )
        if directory_reason:
            return _result(
                3,
                "invalid",
                str(parsed["source"]),
                "-",
                "missing",
                "contract-violation",
                reason=directory_reason,
            )
        if not readable:
            return _result(
                3,
                "invalid",
                str(parsed["source"]),
                "-",
                "missing",
                "contract-violation",
                # Name the shape so an operator can tell "the path is not there" from
                # "the path is there but this reader would not take it".
                reason=(
                    "artifact-is-root"
                    if names_root
                    else "artifact-unreadable"
                    if artifact_path.exists()
                    else "artifact-missing"
                ),
            )
        parsed["artifact_state"] = "readable"
        parsed["artifact_shape"] = "directory" if artifact_path.is_dir() else "file"
        parsed["artifact_path_b64"] = _encode_path(artifact_path)

    if review_blocking_handoff(parsed, worker_type):
        parsed["failure_note"] = REVIEW_BLOCKING_NOTE

    if completed_audit_assessment(parsed, worker_type, artifact_binding, artifact_path):
        parsed.update(verdict="PASS", assessment_verdict="FAIL", assessment_blocker=parsed.get("blocker"),
                      blocker="none", blocker_reason="none", failure_class="pass", failure_note="",
                      envelope_normalized=",".join(filter(None, (parsed.get("envelope_normalized"), "completed-audit-with-findings"))))

    if (
        worker_type == REVIEW_WORKER_TYPE
        and parsed.get("artifact_state") == "readable"
        and parsed.get("artifact_shape") == "file"
    ):
        declared = artifact_declared_verdict(artifact_path)
        parsed["review_artifact_verdict"] = declared
        conflict = review_artifact_conflict(str(parsed.get("verdict")), declared)
        if conflict:
            parsed["review_verdict_conflict"] = conflict

    if include_failure_detail and parsed["verdict"] in {"FAIL", "BLOCKED"}:
        blocker = str(parsed.get("blocker", ""))
        if blocker != "none":
            excerpt, truncated = _escape_and_bound(blocker)
            parsed["blocker_detail_excerpt"] = excerpt
            parsed["blocker_detail_truncated"] = truncated
        diagnostic = parsed.get("diagnostic")
        if isinstance(diagnostic, str) and diagnostic:
            excerpt, truncated = _escape_and_bound(diagnostic)
            parsed["failure_diagnostic_excerpt"] = excerpt
            parsed["failure_diagnostic_truncated"] = truncated
    if include_failure_detail and parsed.get("assessment_blocker") not in {None, "none"}:
        excerpt, truncated = _escape_and_bound(str(parsed["assessment_blocker"]))
        parsed["assessment_detail_excerpt"] = excerpt
        parsed["assessment_detail_truncated"] = truncated

    # Raw values stay internal and are removed from the normalized public view.
    for key in ("artifact", "blocker", "diagnostic", "assessment_blocker"):
        parsed.pop(key, None)
    return parsed


def completed_audit_assessment(parsed, worker_type, binding, artifact_path=None):
    """A negative subject judgment is not a failed audit execution.

    Only an exact registered audit owner with a completed negative review report
    qualifies. Native success or a readable file alone cannot prove that review
    work happened. Stage reviews and ordinary code work do not qualify.
    """
    if (worker_type != "owner" or not isinstance(binding, Mapping)
            or binding.get("worker_type") != "owner" or binding.get("dispatch_depth") != "1"
            or binding.get("workflow_completion") != "runtime-v1"
            or str(binding.get("note", "")).startswith("dead-")
            or parsed.get("state") != "valid" or parsed.get("verdict") != "FAIL"
            or parsed.get("artifact_state") != "readable"
            or parsed.get("artifact_shape") != "file"):
        return False
    if parsed.get("disposition"):
        # The owner wrote a closed review disposition (e.g. `수정 후 병합`) as its verdict:
        # a finished review of any capability only when its report opens on a closed
        # negative review verdict and nothing blocks it (route_authority.handoff_search).
        return (pass_blocker_note(parsed.get("blocker")) is not None
                and closed_review_kind(artifact_path) in {"fix", "hold"})
    try:
        import route_identity
        path = binding.get("owner_route_file") or binding.get("route_file")
        route = json.loads(Path(path).read_text())
        digest = binding.get("owner_route_hash") or binding.get("route_hash")
        rid = binding.get("owner_route_id") or binding.get("route_id")
        return (route.get("capability") == "audit" and route.get("route_id") == rid
                and bool(digest) and route.get("route_hash") == digest
                and route_identity.route_hash(route) == digest
                and _completed_negative_audit_report(artifact_path))
    except (OSError, ValueError, TypeError):
        return False


# Same whole-line forms/normalization as pr-chain.py (PR #1430). Only the
# negative forms reconcile a legacy audit FAIL. Free prose is never evidence.
# A held review (`보류`/`hold`) is a closed judgment too; only a verdict-word
# handoff reads it (`closed_review_kind`), an audit FAIL still needs `fix`.
_AUDIT_OPENING_BYTES = 64 * 1024
_AUDIT_VERDICT_FORMS = (
    ("merge", "최종 판정: 병합 가능"),
    ("merge", "Final verdict: mergeable"),
    ("fix", "최종 판정: 수정 후 병합"),
    ("fix", "Final verdict: changes requested"),
    ("hold", "최종 판정: 보류"),
    ("hold", "Final verdict: hold"),
)


def _audit_form_pattern(label):
    words = [re.escape(word) for word in label.replace(":", " : ").split()]
    return r"\s*".join(words).replace(r"\s*:\s*", r"\s*[:：]\s*")


_AUDIT_CLOSED = re.compile(
    r"^(?P<label>" + "|".join(f"(?:{_audit_form_pattern(label)})" for _, label in _AUDIT_VERDICT_FORMS)
    + r")\s+—\s+head\s+`?(?P<head>[0-9a-f]{7,40})`?$", re.I)
_AUDIT_KINDS = {re.sub(r"\s+", "", label).replace("：", ":").lower(): kind
                for kind, label in _AUDIT_VERDICT_FORMS}


def closed_review_kind(path):
    """``merge``, ``fix`` or ``hold`` when the report opens on a closed review verdict.

    Reads only the first nonblank, non-heading line, like pr-chain.py. The body is
    not an authority source: no prose, fence, history or conflicting later verdict
    interpretation. A missing, unreadable or differently shaped opening is ``None``,
    as is anything but a regular file (a pipe or device is never opened).
    """
    if path is None:
        return None
    try:
        if not Path(path).is_file():
            return None
        with Path(path).open("rb") as handle:
            remaining = _AUDIT_OPENING_BYTES
            while remaining > 0:
                raw = handle.readline(remaining + 1)
                if not raw or len(raw) > remaining:
                    return None
                remaining -= len(raw)
                for line in raw.decode("utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    line = line.replace("**", "").replace("__", "").strip()
                    line = line[:-1].rstrip() if line.endswith(".") else line
                    match = _AUDIT_CLOSED.fullmatch(line)
                    if not match:
                        return None
                    label = re.sub(r"\s+", "", match["label"]).replace("：", ":").lower()
                    return _AUDIT_KINDS[label]
    except (OSError, UnicodeError):
        pass
    return None


def _completed_negative_audit_report(path):
    """A legacy audit FAIL reconciles only on a report that opens on `수정 후 병합`."""
    return closed_review_kind(path) == "fix"


# P-4 (2026-09-10, top-tier review round 2 att-0be411a9): a review closed
# `verdict: PASS` in its terminal envelope while its own artifact opened with
# `## 평결: FAIL`, and the owner was told the round passed. The envelope stays
# authoritative -- §5.10 forbids manufacturing a verdict out of artifacts --
# so this reads ONE defined field and only ever *names* the contradiction.
# Deliberately narrow:
#   * review workers only, and only an artifact that is a single file;
#   * only the opening heading, inside the first 4KB, matched by an anchored
#     pattern -- this is a declared field, not a scan of review prose
#     (prose scanning is how the Bash-text hook kept losing);
#   * an artifact that declares nothing reports `unstated` and changes
#     nothing, so reviews that never adopted the heading are unaffected;
#   * only the harmful direction is a conflict (a PASS envelope over a
#     FAIL/BLOCKED artifact). FAIL over PASS already blocks on its own.
_ARTIFACT_VERDICT_RE = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]*(?:평결|verdict)[ \t]*[:\uff1a][ \t]*\**[ \t]*(PASS|FAIL|BLOCKED)\b",
    re.IGNORECASE | re.MULTILINE,
)
_ARTIFACT_VERDICT_HEAD_BYTES = 4096


def artifact_declared_verdict(path: Path) -> str:
    """The verdict declared by structured evidence or a report opening heading.

    Returns `PASS`/`FAIL`/`BLOCKED`, `unstated` when the artifact declares no
    such heading, or `unreadable` when the head cannot be read.
    """

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(_ARTIFACT_VERDICT_HEAD_BYTES)
    except OSError:
        return "unreadable"
    if path.suffix.lower() == ".json":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return "unreadable"
        if isinstance(value, dict):
            declarations = [value, value.get("io"), value.get("typed_handoff")]
            verdicts = {str(item.get("verdict", "")).upper()
                        for item in declarations if isinstance(item, dict)}
            # Contradictory structured declarations never manufacture PASS.
            for verdict in ("BLOCKED", "FAIL", "PASS"):
                if verdict in verdicts:
                    return verdict
    match = _ARTIFACT_VERDICT_RE.search(head)
    return match.group(1).upper() if match else "unstated"


def review_artifact_conflict(envelope_verdict: str, declared: str) -> str:
    """`artifact-<v>-envelope-pass` when a PASS envelope contradicts its artifact."""

    if envelope_verdict == "PASS" and declared in {"FAIL", "BLOCKED"}:
        return f"artifact-{declared.lower()}-envelope-pass"
    return ""


def review_blocking_handoff(
    terminal: dict[str, object], worker_type: str | None
) -> bool:
    """True when a valid FAIL handoff is a finished review with a readable artifact.

    The three conditions are deliberately conjunctive: a non-review worker's
    FAIL, a review FAIL that names no artifact (or one outside the root), and
    any invalid envelope all stay on the dead-worker path.
    """
    return (
        worker_type == REVIEW_WORKER_TYPE
        and terminal.get("state") == "valid"
        and str(terminal.get("verdict")) == "FAIL"
        and terminal.get("artifact_state") == "readable"
    )


def carrier_terminal_note(
    path: str | Path | None,
    *,
    worktree: str | Path | None,
    artifact_root_metadata: str | Path | None,
    worker_type: str | None,
    artifact_binding: Mapping[str, object] | None = None,
) -> tuple[dict[str, str] | None, dict[str, str]]:
    """The one terminal classification every post-hoc carrier shares.

    Reconcile (`dispatch-registry.py`), the progress watchdog
    (`dispatch-progress.py`), and any future carrier that closes an open row
    from its exact log call this instead of `inspect_terminal_log` directly,
    so the review conjunction (OPERATIONS §5.10) cannot drift between them:
    the compatibility view is upgraded to ``completed-review-blocking`` only
    when the row is a review worker AND the root-bound re-inspection proves a
    valid FAIL handoff naming a readable in-root artifact. Returns
    ``(terminal_view, extra_evidence)``; ``extra_evidence`` carries
    ``review_artifact_b64`` for a finished review and is empty otherwise.
    """
    compat = inspect_terminal_log(path)
    if not compat:
        return None, {}
    extra: dict[str, str] = {}
    if compat.get("failure_note") == "dead-worker-fail" and worker_type == "owner":
        view = inspect_terminal_attempt(path, worktree=worktree,
            artifact_root_metadata=artifact_root_metadata, worker_type=worker_type,
            artifact_binding=artifact_binding)
        if view.get("assessment_verdict") == "FAIL" and view.get("verdict") == "PASS":
            compat = {**compat, "verdict": "PASS", "blocker": "none", "failure_note": "",
                      "failure_class": "pass"}
            extra["reconcile_reason"] = "completed-audit-with-findings"
    if compat.get("failure_note") == "dead-worker-fail" and worker_type == REVIEW_WORKER_TYPE:
        view = inspect_terminal_attempt(
            path,
            worktree=worktree,
            artifact_root_metadata=artifact_root_metadata,
            worker_type=REVIEW_WORKER_TYPE,
            artifact_binding=artifact_binding,
        )
        if review_blocking_handoff(view, REVIEW_WORKER_TYPE):
            compat = {**compat, "failure_note": REVIEW_BLOCKING_NOTE}
            encoded = str(view.get("artifact_path_b64") or "")
            if encoded:
                extra["review_artifact_b64"] = encoded
    return compat, extra


def inspect_terminal_log(path: str | Path | None) -> dict[str, str] | None:
    """Historical compatibility view used by registry failure reconciliation."""

    parsed = _read_terminal(path)
    if parsed.get("state") != "valid":
        return None
    return {
        "artifact": str(parsed["artifact"]),
        "verdict": str(parsed["verdict"]),
        "blocker": str(parsed["blocker"]),
        "failure_note": str(parsed["failure_note"]),
        "failure_class": str(parsed["failure_class"]),
        "terminal_event": str(parsed["terminal_event"]),
        "log_file": str(parsed["log_file"]),
    }


def terminal_envelope_observed(path: str | Path | None) -> bool:
    """Return whether an exact log contains a final runtime/supervisor envelope."""

    if not path:
        return False
    try:
        lines = _tail_lines(Path(path))
    except (OSError, UnicodeDecodeError):
        return False
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("type") in {"turn.completed", "result", "dispatch.supervisor.error"}:
            return True
        if value.get("type") == "step_finish":
            part = value.get("part")
            if isinstance(part, dict) and part.get("reason") == "stop":
                return True
    return False


def wire_record(result: dict[str, object]) -> str:
    row = (
        int(result["exit_code"]),
        str(result["state"]),
        str(result["source"]),
        str(result["verdict"]),
        str(result["artifact_state"]),
        str(result["blocker_reason"]),
    )
    if row not in _LEGAL_WIRE:
        row = (4, "error", "runtime-error", "-", "unchecked", "contract-violation")
    return "codex-terminal-v1\t" + "\t".join(row[1:]) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--artifact-root-metadata", required=True)
    parser.add_argument("log_file")
    return parser


def main(argv: list[str]) -> int:
    try:
        args = _parser().parse_args(argv[1:])
    except SystemExit as exc:
        return 0 if exc.code == 0 else 64
    result = inspect_terminal_attempt(
        args.log_file,
        worktree=args.worktree,
        artifact_root_metadata=args.artifact_root_metadata,
    )
    sys.stdout.write(wire_record(result))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
