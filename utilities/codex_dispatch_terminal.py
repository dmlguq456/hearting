#!/usr/bin/env python3
"""Safe, exact-attempt inspection of a Codex dispatch JSONL terminal handoff.

The compatibility parser returns the historical internal dictionary.  The
structured inspector and CLI expose only closed decision enums, bounded
failure-only excerpts, and a validated base64 path reference.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from dispatch_supervisor_terminal import opencode_terminal_boundary

# OPERATIONS §5.10 "Review verdict is a result, not a worker death": a review
# worker whose FAIL handoff names a readable in-root review artifact finished
# its contract -- it recorded blocking findings. Its row closes with this typed
# completion note instead of `dead-worker-fail`; the verdict axis
# (`failure_class=fail`) is unchanged. Only a `worker_type=review` row with a
# readable artifact earns it; every other FAIL keeps the dead-worker note.
REVIEW_BLOCKING_NOTE = "completed-review-blocking"
REVIEW_WORKER_TYPE = "review"


ROOT = Path(__file__).resolve().parents[1]
# The handoff is the trailing three lines of the final message. Anchoring at the
# end rather than across the whole message keeps every guarantee the strict form
# had — only the last block counts, so an earlier decoy never wins, and nothing
# outside the captured groups reaches a result — while surviving the one failure
# a prompt cannot prevent: a worker that prepends a summary sentence to an
# otherwise exact handoff. Two 2026-07-28 pipelines died that way, each with a
# correct artifact already on disk (plans/2026-07-29_handoff-tail-anchor).
_HANDOFF_RE = re.compile(
    r"(?:\A|\n)artifact: (?P<artifact>[^\n]+)\n"
    r"verdict: (?P<verdict>PASS|FAIL|BLOCKED)\n"
    r"blocker: (?P<blocker>[^\n]+)\Z"
)
_SANDBOX_INIT_RE = re.compile(
    r"bwrap: Can't bind mount [^\n]+ on [^\n]+/\.codex:"
    r"[^\n]*Unable to mount source on destination: No such file or directory",
    re.I,
)
_MAX_TAIL_BYTES = 1024 * 1024
_DETAIL_LIMIT = 512

ARTIFACT_STATES = frozenset(
    {"unchecked", "none", "readable", "missing", "outside-root", "unsafe-root"}
)
_TERMINAL_SOURCES = (
    "exact-turn-completed",
    "exact-claude-result",
    "exact-step-finish-stop",
)
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


def _read_terminal(path: str | Path | None) -> dict[str, object]:
    if not path:
        return _result(
            2, "absent", "none", "-", "unchecked", "-", reason="log-file-absent"
        )
    log_path = Path(path)
    try:
        lines = _tail_lines(log_path)
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
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            # Codex stderr is intentionally retained in this same exact attempt
            # log.  Non-JSON lines are private debugging data, never handoff data.
            continue
        if isinstance(value, dict):
            rows.append(value)

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
        terminal_source = "exact-claude-result"
    final_message: str | None = None
    if terminal_event == "result":
        subtype = terminal_row.get("subtype")
        if terminal_row.get("is_error") is True or subtype not in {None, "success"}:
            return _result(
                3,
                "invalid",
                terminal_source,
                "-",
                "unchecked",
                "contract-violation",
                reason="claude-result-runtime-error",
            )
        text = terminal_row.get("result")
        final_message = text if isinstance(text, str) else None
    elif terminal_event == "step_finish":
        final_message = opencode_final_message
    else:
        final_row = rows[terminal_index - 1] if terminal_index > 0 else None
        if isinstance(final_row, dict) and final_row.get("type") == "item.completed":
            item = final_row.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                final_message = text if isinstance(text, str) else None
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
    # the message, so anything before it is ignored rather than fatal.
    match = _HANDOFF_RE.search(final_message.strip())
    if match is None:
        return _result(
            3,
            "invalid",
            terminal_source,
            "-",
            "unchecked",
            "contract-violation",
            reason="malformed-handoff",
        )

    handoff = match.groupdict()
    if handoff["verdict"] == "PASS" and handoff["blocker"] != "none":
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
        "none" if handoff["blocker"] == "none" else "worker-reported",
        reason="none",
        artifact=handoff["artifact"],
        blocker=handoff["blocker"],
        diagnostic=diagnostic,
        failure_note=failure_note,
        failure_class="sandbox-init" if sandbox_init else verdict.lower(),
        terminal_event=terminal_event,
        log_file=str(log_path),
    )


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _primary_worktree(worktree: Path) -> Path | None:
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
                return Path(line[9:]).resolve(strict=True)
            except OSError:
                return None
    return None


def _resolve_safe_root(
    worktree: str | Path | None, artifact_root_metadata: str | Path | None
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

    override = os.environ.get("AGENT_ARTIFACT_ROOT")
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
        resolved = subprocess.run(
            [str(ROOT / "utilities" / "artifact-root.sh"), str(worktree_resolved)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
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


_ARTIFACT_MAX_ENTRIES = 20000


def _directory_artifact_reason(artifact_path: Path, root: Path) -> str:
    """Return a typed refusal for a directory that cannot stand as a deliverable.

    An existing, readable directory is not by itself evidence of work. The
    harness pre-creates the cycle's `artifacts/` directory before a worker's
    first write (`artifact_producer.begin`) and exports that exact path as
    `AGENT_ARTIFACT_OUTPUT_DIR`, so accepting any directory would let a worker
    that produced nothing name the path it was handed and pass. Require at least
    one readable regular file somewhere in the tree.

    Members must also stay inside the artifact root. The completion marker
    attests this tree, and a consumer that later copies or publishes it would
    follow a symlink pointing out of the root.
    """

    # Access first: an unreadable directory yields nothing from `rglob` (it
    # swallows PermissionError), which would read as "empty" instead of "cannot
    # be read" and send an operator after the wrong problem.
    if not os.access(artifact_path, os.R_OK | os.X_OK):
        return "artifact-unreadable"
    entries = 0
    has_file = False
    try:
        for child in artifact_path.rglob("*"):
            entries += 1
            if entries > _ARTIFACT_MAX_ENTRIES:
                return "artifact-directory-too-large"
            try:
                child.resolve(strict=False).relative_to(root)
            except (OSError, ValueError):
                return "artifact-member-outside-root"
            if not has_file and child.is_file() and os.access(child, os.R_OK):
                has_file = True
    except OSError:
        return "artifact-unreadable"
    return "" if has_file else "artifact-empty-directory"


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
) -> dict[str, object]:
    """Return the normalized, root-bound result for one exact attempt log.

    ``worker_type`` is the registry row's `worker_type`; passing `review` lets a
    FAIL handoff with a readable in-root artifact carry ``failure_note``
    ``completed-review-blocking`` (a finished review) instead of
    ``dead-worker-fail`` (a dead worker). Callers that do not know the worker
    type keep the compatibility note.
    """

    parsed = _read_terminal(path)
    if parsed["state"] != "valid":
        return parsed

    root, root_reason = _resolve_safe_root(worktree, artifact_root_metadata)
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
    if artifact == "-":
        parsed["artifact_state"] = "none"
    else:
        candidate = Path(artifact)
        if not candidate.is_absolute() or _has_control(artifact):
            return _result(
                3,
                "invalid",
                str(parsed["source"]),
                "-",
                "outside-root",
                "contract-violation",
                reason="artifact-outside-root",
            )
        try:
            artifact_path = candidate.resolve(strict=False)
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
            )
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
            directory_reason = _directory_artifact_reason(artifact_path, root)
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

    # Raw values stay internal and are removed from the normalized public view.
    for key in ("artifact", "blocker", "diagnostic"):
        parsed.pop(key, None)
    return parsed


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
    if compat.get("failure_note") == "dead-worker-fail" and worker_type == REVIEW_WORKER_TYPE:
        view = inspect_terminal_attempt(
            path,
            worktree=worktree,
            artifact_root_metadata=artifact_root_metadata,
            worker_type=REVIEW_WORKER_TYPE,
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
