"""Hearting's block inside a user's Codex ``AGENTS.override.md``.

Codex reads one global instruction file from ``$CODEX_HOME``: the first
non-empty one of ``AGENTS.override.md`` and ``AGENTS.md``. A user who keeps
personal rules in the override therefore hides the Hearting bootstrap that
activation links as ``AGENTS.md``. Activation then appends the bootstrap to the
override as one marked block after the user's text. Install and update replace
only that block; uninstall removes only that block. Bytes outside the block are
never changed, and without a user override (or with an empty one) nothing is
written, because Codex already reads ``AGENTS.md``.

The begin marker records how many newlines were added in front of the block, so
removal gives back the user's exact bytes without any stored state.
"""

from __future__ import annotations

import re
from pathlib import Path

import safe_fs


OVERRIDE_NAME = "AGENTS.override.md"
_TOKEN = b"hearting:codex-bootstrap"
_END = b"<!-- hearting:codex-bootstrap end -->"
_BLOCK = re.compile(
    rb"(?ms)^<!-- hearting:codex-bootstrap begin sep=([012]) [^\n]*-->\n"
    rb".*?^<!-- hearting:codex-bootstrap end -->(?:\n|\Z)"
)


def _begin(sep: int) -> bytes:
    return (b"<!-- hearting:codex-bootstrap begin sep=%d (managed by Hearting: "
            b"install and update replace this block, uninstall removes it) -->\n" % sep)


def _block(sep: int, body: bytes) -> bytes:
    if _TOKEN in body:
        raise ValueError("bootstrap text contains the block marker")
    if not body.endswith(b"\n"):
        body += b"\n"
    return _begin(sep) + body + _END + b"\n"


def _parse(data: bytes):
    """Return ``(user_bytes, (start, end, sep))``; the span is None without a block.

    Raises ValueError when markers are present but not exactly one whole block.
    """
    matches = list(_BLOCK.finditer(data))
    if not matches:
        if _TOKEN in data:
            raise ValueError("damaged-markers")
        return data, None
    if len(matches) != 1 or data.count(_TOKEN) != 2:
        raise ValueError("damaged-markers")
    match = matches[0]
    start, end, sep = match.start(), match.end(), int(match.group(1))
    before = data[:start]
    if sep and before.endswith(b"\n" * sep):
        before = before[:-sep]
    return before + data[end:], (start, end, sep)


def _has_text(data: bytes) -> bool:
    # Codex decodes lossily and skips a file whose trimmed text is empty.
    return bool(data.decode("utf-8", "replace").strip())


def _separator(user: bytes) -> int:
    if user.endswith(b"\n\n"):
        return 0
    return 1 if user.endswith(b"\n") else 2


_LINES = {
    "added": "Codex {path}: added the Hearting bootstrap below your text "
             "(Codex reads this file instead of AGENTS.md)",
    "updated": "Codex {path}: refreshed the Hearting bootstrap block; your text is unchanged",
    "removed": "Codex {path}: removed the Hearting bootstrap block; your text is unchanged",
    "skipped-symlink": "Codex {path} is a symlink, so Hearting left it alone; Codex reads it "
                       "instead of AGENTS.md and will not load the Hearting bootstrap",
    "skipped-markers": "Codex {path} has a damaged Hearting block marker, so Hearting left it "
                       "alone; Codex will not load the Hearting bootstrap",
    "skipped-error": "Codex {path} was not updated ({detail}); Codex will not load the "
                     "Hearting bootstrap",
}


def _result(path: Path, status: str, *, changed=False, dry_run=False, detail="") -> dict:
    line = _LINES.get(status)
    if line:
        line = ("dry-run, not written: " if dry_run else "") + line.format(path=path, detail=detail)
    return {"action": "codex-agents-override", "path": str(path), "status": status,
            "changed": changed, "report_lines": [line] if line else []}


def _write(path: Path, current: safe_fs.PathState, payload: bytes) -> None:
    auth = safe_fs.authority(path, owner="codex-agents-override", allowed_paths=(path,),
                             allow_leaf_symlink=False, expected=current)
    safe_fs.atomic_write_bytes(auth, payload, current.mode, expected=current)


def _update(codex_home: Path, bootstrap: Path | None, *, dry_run: bool) -> dict:
    path = Path(codex_home) / OVERRIDE_NAME
    try:
        current = safe_fs.capture_state(path, include_payload=True)
        if current.kind == "symlink":
            return _result(path, "skipped-symlink")
        if current.kind != "file":
            return _result(path, "absent")  # Codex skips a non-file and reads AGENTS.md
        data = current.payload
        try:
            user, span = _parse(data)
        except ValueError:
            return _result(path, "skipped-markers")
        if bootstrap is None or not _has_text(user):
            if span is None:
                # Nothing of ours to remove, or an empty override Codex skips.
                return _result(path, "unchanged")
            status, after = "removed", user
        elif span is None:
            sep = _separator(user)
            status, after = "added", data + b"\n" * sep + _block(sep, bootstrap.read_bytes())
        else:
            start, end, sep = span
            status = "updated"
            after = data[:start] + _block(sep, bootstrap.read_bytes()) + data[end:]
        if after == data:
            return _result(path, "unchanged")
        if not dry_run:
            _write(path, current, after)
        return _result(path, status, changed=True, dry_run=dry_run)
    except (OSError, ValueError, safe_fs.SafetyError) as exc:
        return _result(path, "skipped-error", detail=type(exc).__name__)


def apply(codex_home: Path, bootstrap: Path, *, dry_run: bool = False) -> dict:
    """Add or refresh the block when the user's override has text of its own."""
    return _update(codex_home, Path(bootstrap), dry_run=dry_run)


def remove(codex_home: Path, *, dry_run: bool = False) -> dict:
    """Take the block out and leave every other byte as the user wrote it."""
    return _update(codex_home, None, dry_run=dry_run)
