#!/usr/bin/env python3
"""Rewind distill markers over an outage window and optionally re-harvest.

Background (2026-08-13). The Claude distillation worker died at boot from
2026-08-10 20:16 onward because a fixed three-hop parent walk resolved the wrong
harness root under the installed layout. The dispatcher discarded worker stderr
and advanced the marker unconditionally, so every delta in that window was
skipped while looking exactly like "nothing worth storing". The conversation
JSONL transcripts survive, so the deltas are recoverable: rewind each affected
marker to the last UUID that precedes the outage, then let the ordinary
increment path harvest them again.

Safety posture, in order of importance:

* Dry run is the default. Without ``--apply`` nothing on disk is written --
  not the marker files, not the database.
* ``--apply`` backs up each marker to ``<marker>.pre-recover`` before rewinding.
* ``--apply`` is refused unless the distillation worker actually boots. That is
  the whole point of the cycle: rewinding markers while the worker is still dead
  would re-lose the same deltas, silently, a second time.
* Re-harvest is sequential. It contends for the ordinary D-41 slots and never
  raises a concurrency limit, because the v18 incident (216 concurrent workers)
  came from exactly this kind of batch fan-out.
* Idempotent. A marker already at or behind its rewind target is left alone, so
  repeated runs converge instead of walking backwards.
"""

import argparse
import datetime as _dt
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mem  # noqa: E402  (path is set immediately above)

DEFAULT_SINCE = "2026-08-10T20:16:00"
BACKUP_SUFFIX = ".pre-recover"


def _parse_iso(text):
    """Accept ISO-8601 with or without a trailing Z, always return aware UTC."""
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    stamp = _dt.datetime.fromisoformat(raw)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.timezone.utc)
    return stamp.astimezone(_dt.timezone.utc)


def _msg_time(msg):
    stamp = getattr(msg, "ts", None) or getattr(msg, "timestamp", None)
    if not stamp:
        return None
    try:
        return _parse_iso(str(stamp))
    except Exception:
        return None


def affected_markers(since, store=None):
    """Marker files whose mtime falls inside the outage window."""
    store = store or mem.STORE
    if not store.exists():
        return []
    out = []
    for path in sorted(store.glob(".distill-state-*")):
        if path.name.endswith(BACKUP_SUFFIX):
            continue
        try:
            mtime = _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.timezone.utc)
        except OSError:
            continue
        if mtime >= since:
            out.append(path)
    return out


def _sid_of(path):
    return path.name[len(".distill-state-"):]


def rewind_target(sid, since, source=None):
    """Last UUID strictly before the outage, plus the delta count after it.

    Returns ``(uuid, skipped, located)``. ``uuid`` is ``""`` when no message
    predates the window -- meaning the whole session falls inside the outage and
    the marker should be cleared so the session is re-read from its start.
    """
    src = source or mem.ClaudeCodeJsonlSource(sid)
    if src.locate() is None:
        return None, 0, False
    last_before = ""
    skipped = 0
    for msg in src.messages():
        when = _msg_time(msg)
        if when is not None and when < since:
            if getattr(msg, "uuid", None):
                last_before = msg.uuid
        else:
            skipped += 1
    return last_before, skipped, True


def _cwd_of(sid, source=None):
    """Logical project cwd of a session, decoded from its transcript directory.

    The dispatcher's argument mode is ``distill <sid> <cwd>`` and working-tier
    scope comes from that cwd, so the re-harvest call must carry it. Fail open
    to None; the dispatcher then falls back to its own $PWD default."""
    src = source or mem.ClaudeCodeJsonlSource(sid)
    located = src.locate()
    if located is None:
        return None
    try:
        return mem._decode_enc_cwd(Path(located).parent.name)
    except Exception:
        return None


def plan_recovery(since, store=None):
    rows = []
    for path in affected_markers(since, store=store):
        sid = _sid_of(path)
        current = mem.read_marker(sid)
        target, skipped, located = rewind_target(sid, since)
        rows.append({
            "sid": sid,
            "marker": path,
            "current": current,
            "target": target,
            "skipped": skipped,
            "located": located,
            "cwd": _cwd_of(sid) if located else None,
            # Already at the target, or no transcript to rewind against.
            "actionable": located and target != current,
        })
    return rows


def worker_boots(worker=None):
    """Prove the distillation worker starts before we trust it with a rewind.

    A boot failure is exactly the fault this cycle repaired; refusing --apply
    here is what stops a rewind from re-losing the same deltas.
    """
    worker = worker or os.environ.get("MEM_DISTILL_WORKER")
    if not worker:
        root = os.environ.get("AGENT_HOME")
        if root:
            # Installed homes are flattened (`<home>/bin/...`); only a source
            # checkout keeps the adapters/ tree. Probing the repo shape alone
            # made --apply refuse on every installed deployment (2026-08-13).
            for cand in (
                Path(root) / "bin/mem-distill-worker.sh",
                Path(root) / "adapters/claude/bin/mem-distill-worker.sh",
            ):
                if cand.exists():
                    worker = str(cand)
                    break
    if not worker or not Path(worker).exists():
        return False, "worker not found"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("synthetic boot probe\n")
        prompt = fh.name
    try:
        proc = subprocess.run(
            [worker, "increment", "fast-distiller", prompt],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:                      # pragma: no cover - defensive
        return False, f"worker probe failed: {exc}"
    finally:
        os.unlink(prompt)
    err = proc.stderr or ""
    if proc.returncode != 0:
        return False, f"worker exited {proc.returncode}: {err.strip()[:200]}"
    for bad in ("unbound variable", "parameter not set", "No such file or directory"):
        if bad in err:
            return False, f"worker stderr shows boot failure: {bad}"
    return True, "ok"


def apply_rewind(row):
    path = row["marker"]
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        backup.write_text(row["current"] + "\n", encoding="utf-8")
    if row["target"]:
        mem.advance_marker(row["sid"], row["target"])
    else:
        # No message predates the window: clear so the session re-reads whole.
        path.write_text("", encoding="utf-8")
    return backup


def reharvest(rows, dispatcher=None):
    """Run the ordinary increment path once per session, strictly sequentially."""
    dispatcher = dispatcher or os.environ.get("MEM_RECOVER_DISPATCHER")
    if not dispatcher:
        root = os.environ.get("AGENT_HOME")
        if root:
            cand = Path(root) / "hooks/mem-distill-dispatch.sh"
            dispatcher = str(cand) if cand.exists() else None
    if not dispatcher or not Path(dispatcher).exists():
        print("reharvest: dispatcher not found; skipped", file=sys.stderr)
        return 0
    done = 0
    for row in rows:
        # The dispatcher's argument mode is `distill <sid> <cwd>`; an argv-less
        # call falls through to its stdin-JSON branch and exits as a silent
        # no-op (observed 2026-08-13 — the first recovery wave launched zero
        # workers). An empty cwd argument degrades to the dispatcher's own
        # $PWD default via `${3:-$PWD}`.
        try:
            subprocess.run(
                [dispatcher, "distill", row["sid"], row.get("cwd") or ""],
                timeout=900, capture_output=True, text=True)
            done += 1
        except Exception as exc:                  # pragma: no cover - defensive
            print(f"reharvest: {row['sid']}: {exc}", file=sys.stderr)
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Rewind distill markers over the worker-outage window.")
    ap.add_argument("--since", default=DEFAULT_SINCE,
                    help=f"ISO-8601 outage start (default {DEFAULT_SINCE})")
    ap.add_argument("--apply", action="store_true",
                    help="write markers (default is a dry run that writes nothing)")
    ap.add_argument("--reharvest", action="store_true",
                    help="after rewinding, run the increment path sequentially")
    ap.add_argument("--skip-worker-check", action="store_true",
                    help="testing only: bypass the worker boot probe")
    args = ap.parse_args(argv)

    since = _parse_iso(args.since)
    rows = plan_recovery(since)
    actionable = [r for r in rows if r["actionable"]]

    print(f"store: {mem.STORE}")
    print(f"since: {since.isoformat()}")
    print(f"markers in window: {len(rows)}  actionable: {len(actionable)}")
    for row in rows:
        state = "rewind" if row["actionable"] else (
            "no-transcript" if not row["located"] else "already-at-target")
        current = (row["current"] or "")[:8] or "-"
        target = (row["target"] or "")[:8] or "(start)"
        print(f"  {row['sid']}  {state}  "
              f"current={current} -> target={target}  "
              f"deltas~{row['skipped']}")

    if not args.apply:
        print("\ndry run: nothing written. re-run with --apply to rewind.")
        return 0

    if not args.skip_worker_check:
        ok, why = worker_boots()
        if not ok:
            print(f"\nrefusing --apply: {why}", file=sys.stderr)
            print("the worker must boot before rewinding, or the same deltas "
                  "are lost again.", file=sys.stderr)
            return 2

    for row in actionable:
        backup = apply_rewind(row)
        print(f"rewound {row['sid']} (backup {backup.name})")
    print(f"rewound {len(actionable)} marker(s)")

    if args.reharvest:
        # Re-trigger every located session, not only freshly rewound ones: a
        # marker rewound by an earlier --apply whose dispatch never launched
        # (start-budget denial, the argv no-op above) is `already-at-target`
        # yet still holds an unprocessed delta. The dispatcher skips empty
        # deltas deterministically, so this stays idempotent.
        count = reharvest([r for r in rows if r["located"]])
        print(f"re-harvested {count} session(s) sequentially")
    return 0


if __name__ == "__main__":
    sys.exit(main())
