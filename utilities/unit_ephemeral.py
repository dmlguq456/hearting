#!/usr/bin/env python3
"""The record-free form of a unit route (`compose --unit <u> --ephemeral`).

It is the same unit route: sealed, started, followed and closed by the same code. The one difference is
where its files go. A scratch artifact root under the project's runtime directory takes the route file,
the cycle and the report, so the project's artifact folder and its campaign records get nothing from it.
The worker's answer comes back with its completion: the sealed summary of an ephemeral route is the
answer itself (its report text), cut at ``ANSWER_CHARS`` with the place to read the rest.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

SCRATCH_DIR = "ephemeral"
MARKER = ".ephemeral"
ANSWER_CHARS = 6000   # what one completion notice carries of an answer; the rest stays in the report file


PROJECT_KEY = "project_artifact_root: "
KEY_CHARS = 12


def project_key(project_root) -> str:
    """The folder name of a project's scratch root: the first 12 hex digits of its artifact root path's sha256."""
    return hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[:KEY_CHARS]


def _shaped_like_a_scratch_root(root: Path) -> bool:
    """`<...>/ephemeral/<12 hex>/.agent_reports`: the place a project's scratch root is made."""
    key = root.parent.name
    return (root.name == ".agent_reports" and root.parent.parent.name == SCRATCH_DIR
            and len(key) == KEY_CHARS and all(c in "0123456789abcdef" for c in key))


def scratch_root(project_root) -> Path:
    """The project's scratch artifact root beside its runtime directory (created when absent).

    One folder per project, `<runtime>/ephemeral/<key>/.agent_reports`, so projects sharing a runtime directory
    never share a root or a marker. A scratch root given back is returned as it is, so a worker that starts
    another keeps the one root. The caller of a project's artifact root is the authority for the marker: it is
    written when absent or when it does not verify for this folder. A worker never writes it.
    The marker names the project's artifact root: the scratch root's workers count under that project's
    model-worker governor, never under one of their own."""
    import artifact_admission
    import worker_bootstrap
    given = Path(project_root).resolve()
    if is_scratch_root(given):
        return given
    runtime = worker_bootstrap.runtime_dir_candidate(given) or str(given.parent / ".agent_runtime")
    root = Path(runtime) / SCRATCH_DIR / project_key(given) / ".agent_reports"
    root.mkdir(parents=True, exist_ok=True)
    if counted_root(root) != given:
        (root / MARKER).write_text(f"{PROJECT_KEY}{given}\nscratch artifact root of ephemeral unit routes; nothing here is a record\n",
                                   encoding="utf-8")
    artifact_admission.ensure_root_identity(root)
    return root


def is_scratch_root(root) -> bool:
    try:
        root = Path(root)
        return _shaped_like_a_scratch_root(root) and (root / MARKER).is_file()
    except (OSError, TypeError, ValueError):
        return False


def counted_root(root) -> Path:
    """The artifact root whose model-worker governor counts work under `root`: the project's own root for a
    scratch root whose marker names a project that hashes to the folder's own name, else `root` itself.
    A marker that is missing, unreadable or names another project makes no correspondence."""
    root = Path(root)
    try:
        if _shaped_like_a_scratch_root(root):
            for line in (root / MARKER).read_text(encoding="utf-8").splitlines():
                named = line[len(PROJECT_KEY):].strip() if line.startswith(PROJECT_KEY) else ""
                if named and project_key(named) == root.parent.name:
                    return Path(named)
    except (OSError, UnicodeError, TypeError, ValueError):
        pass
    return root


def answer_text(path) -> str:
    """The report's text with blank lines folded away; "" when it cannot be read or has no text."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def answer_summary(route, primary, *, head: str) -> str:
    """The sealed summary of an ephemeral route: the answer, cut at `ANSWER_CHARS` with where to read it all.

    One line per report line, no blank line (a sealed summary is one block). An answer with no text is
    no answer: nothing is sealed, so the end reads as unfinished."""
    text = answer_text(primary)
    if not text:
        return ""
    lines = [f"{head} {route.get('capability') or '-'} · route {route.get('route_id') or '-'} · ephemeral", "Answer:"]
    if len(text) > ANSWER_CHARS:
        lines.append(text[:ANSWER_CHARS].rstrip())
        lines.append(f"[answer cut at {ANSWER_CHARS} of {len(text)} characters; read the rest in {primary}]")
    else:
        lines.append(text)
    return "\n".join(lines)
