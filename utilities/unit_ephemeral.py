#!/usr/bin/env python3
"""The record-free form of a unit route (`compose --unit <u> --ephemeral`).

It is the same unit route: sealed, started, followed and closed by the same code. The one difference is
where its files go. A scratch artifact root under the project's runtime directory takes the route file,
the cycle and the report, so the project's artifact folder and its campaign records get nothing from it.
The worker's answer comes back with its completion: the sealed summary of an ephemeral route is the
answer itself (its report text), cut at ``ANSWER_CHARS`` with the place to read the rest.
"""
from __future__ import annotations

from pathlib import Path

SCRATCH_DIR = "ephemeral"
MARKER = ".ephemeral"
ANSWER_CHARS = 6000   # what one completion notice carries of an answer; the rest stays in the report file


PROJECT_KEY = "project_artifact_root: "


def scratch_root(project_root) -> Path:
    """The scratch artifact root beside the project's runtime directory (created when absent).

    A scratch root given back is returned as it is, so a worker that starts another keeps the one root.
    The marker names the project's artifact root: the scratch root's workers count under that project's
    model-worker governor, never under one of their own."""
    import artifact_admission
    import worker_bootstrap
    given = Path(project_root).resolve()
    if is_scratch_root(given):
        return given
    runtime = worker_bootstrap.runtime_dir_candidate(given) or str(given.parent / ".agent_runtime")
    root = Path(runtime) / SCRATCH_DIR / ".agent_reports"
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    if counted_root(root) == root:
        marker.write_text(f"{PROJECT_KEY}{given}\nscratch artifact root of ephemeral unit routes; nothing here is a record\n",
                          encoding="utf-8")
    artifact_admission.ensure_root_identity(root)
    return root


def is_scratch_root(root) -> bool:
    try:
        return (Path(root) / MARKER).is_file()
    except (OSError, TypeError, ValueError):
        return False


def counted_root(root) -> Path:
    """The artifact root whose model-worker governor counts work under `root`: the project's own root for a
    scratch root that names one, else `root` itself."""
    try:
        for line in (Path(root) / MARKER).read_text(encoding="utf-8").splitlines():
            if line.startswith(PROJECT_KEY) and line[len(PROJECT_KEY):].strip():
                return Path(line[len(PROJECT_KEY):].strip())
    except (OSError, UnicodeError, TypeError, ValueError):
        pass
    return Path(root)


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
