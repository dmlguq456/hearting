#!/usr/bin/env python3
"""Count the six things the two-layer unit pilot is judged on. Read-only, no model, stdlib only.

The pilot units (qa/code-review, qa/test, dev/backend, editorial/report) were split into
`## 지킬 것` / `## 참고`. Whether that helps is read from records that already exist: the dispatch
registry (`jobs.log`) and the report each reviewer or stage wrote. Nothing here runs on a timer; the
supervisor runs it by hand once before the pilot (baseline) and once about two weeks after. The cut is the
time the new unit text was INSTALLED (workers render from the installed release), not the merge time.

    python3 tools/measure-unit-pilot.py --before <install time ISO> [--json]   # the window before
    python3 tools/measure-unit-pilot.py --after  <install time ISO> [--json]   # the window after

A window is, per unit, the attempts of that unit inside 14 days of the cut, at most 30, whichever is
reached first (`--before` takes the latest ones before the cut, `--after` the earliest after it). Output
carries counts and kinds only: unit ids, numbers, route ids; never a project name, title or report text.

The six items (per unit):
  1 roundtrips      execution <-> review send-backs in each route the unit appears in: median and 90th percentile
                    over those routes. A send-back is a blocking review verdict (`completed-review-blocking`) or a
                    FAIL of a stage-form gate (`dead-worker-fail`, failure_class `fail`, a `qa/*` unit). Plan review
                    (`qa/plan-review`, `research/plan-review`) is left out. `roundtrips_by_unit` splits the sum by the
                    unit that judged, so a reader can see what is mixed in.
  2 closed_fail     round >= 2 blocking verdicts whose report marks earlier red items closed and none open or
                    regressed (a FAIL for a new finding); the goal is 0. Status words count only on a line that also
                    carries an earlier item id.
  3 place / repro   share of red items carrying a `file:line`, and a reproduction-condition cue
  4 unrun_red       red items that are really "could not run a check"; handed_over: `handed-over:` lines
  5 render_bytes    bytes of the worker text the runtime renders for the unit (`--render-ref REF` for another tree);
                    qa/test also reports `render_bytes_stage` (it runs in both the review and the stage form)
  6 invalid_envelope attempts that ended `dead-invalid-envelope`
Items 2-4 read report text and are heuristics (`reports_read` / `reports_missing` say how much was readable).
Reports of attempts that ended FAIL or BLOCKED (`dead-worker-fail`, `dead-worker-blocked`) are read too. A measure the
unit's reports cannot carry by shape (a unit with no red-item list) is None (shown as a dash) with the reason in `notes`;
None is "not measurable", never "zero".
A report file is read as the report of the attempt that wrote it LAST. Attempts of one node (a send-back, a fix, a
re-review) write the same file, so an earlier attempt's report is gone from it: that attempt is not read and is counted
in `reports_overwritten`. The last writer is the latest row, with this unit's rows outside the window included, that
names the file; a `completed-marker` row whose recorded evidence hash equals the file's hash today is its writer.
So items 2-4 are measured from the last round of each node only; `red_reports` (`a/b`) says in how many of the b
blocking reports read a red item was found, and a note says when the measure is partial. Limits: an earlier round's
report is not recovered from the attempt log; a closure check started as a new unit route is always round 1 (the round
number is counted within one route and node), so it is left out of item 2.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

UNITS = ("qa/code-review", "qa/test", "dev/backend", "editorial/report")
WORKER_TYPE = {"qa/code-review": "review", "qa/test": "review", "dev/backend": "stage", "editorial/report": "stage"}
REVIEW_UNITS = ("qa/code-review", "qa/test")
MAX_ATTEMPTS, MAX_DAYS = 30, 14
BLOCKING = "completed-review-blocking"
INVALID = "dead-invalid-envelope"
STAGE_FAIL, STAGE_BLOCKED = "dead-worker-fail", "dead-worker-blocked"
PLAN_UNITS = ("qa/plan-review", "research/plan-review")   # plan review is not an execution <-> review send-back
READ_NOTES = (INVALID, STAGE_FAIL, STAGE_BLOCKED)
ROOT = Path(__file__).resolve().parents[1]


def state_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("AGENT_DISPATCH_JOBS")
    if env:
        return Path(env).parent if env.endswith("jobs.log") else Path(env)
    return Path.home() / ".local" / "state" / "hearting" / "dispatch"


def parse_ts(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def read_rows(jobs: Path):
    """The registry rows as (time, meta). Unreadable lines are skipped."""
    rows = []
    try:
        handle = jobs.open(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 6:
                continue
            try:
                when = parse_ts(parts[0])
            except ValueError:
                continue
            meta = {}
            for pair in parts[5].split(","):
                key, sep, value = pair.partition("=")
                if sep and key in KEEP:
                    meta[key] = value
            meta["_state"] = parts[1]
            rows.append((when, meta))
    return rows


KEEP = {"unit", "route_id", "route_node", "note", "attempt_id", "dispatch_depth", "log_file",
        "completion_marker", "model_profile", "failure_class"}


def window(rows, unit, cut, side, cap=MAX_ATTEMPTS):
    mine = [(w, m) for w, m in rows if m.get("unit") == unit]
    if side == "before":
        pool = [x for x in mine if cut - dt.timedelta(days=MAX_DAYS) <= x[0] <= cut]
        return sorted(pool, key=lambda x: x[0])[-cap:]
    pool = [x for x in mine if cut <= x[0] <= cut + dt.timedelta(days=MAX_DAYS)]
    return sorted(pool, key=lambda x: x[0])[:cap]


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, -(-len(ordered) * q // 100) - 1))]


def median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


# -- report location and reading -----------------------------------------------------------------

_ENVELOPE = re.compile(r"artifact: ([^\s\\\"]+)(?:\\n|\n)verdict: (?:PASS|FAIL|BLOCKED)")


def report_path(meta: dict, root: Path) -> Path | None:
    """The file the worker named in its closing lines (read off the tail of its log), else the marker's evidence."""
    log = meta.get("log_file")
    if log:
        try:
            with open(log, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 65536))
                tail = handle.read().decode("utf-8", errors="replace")
            found = _ENVELOPE.findall(tail)
            if found and found[-1] != "-":
                return Path(found[-1])
        except OSError:
            pass
    route, attempt = meta.get("route_id"), meta.get("attempt_id")
    folder = root / "completion" / route if route else None
    if folder and folder.is_dir():
        for name in sorted(os.listdir(folder)):
            if name.endswith(".json") and ".attempt." not in name:
                try:
                    data = json.loads((folder / name).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if data.get("attempt_id") == attempt and isinstance(data.get("evidence"), dict):
                    return Path(data["evidence"].get("path") or "")
    return None


def read_text(path: Path | None) -> str | None:
    if not path or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def recorded_sha(meta: dict, root: Path) -> str | None:
    """The evidence hash the runtime recorded when this attempt completed (`completion/<route>/<node>.json`)."""
    route, attempt = meta.get("route_id"), meta.get("attempt_id")
    folder = root / "completion" / route if route else None
    if not (folder and attempt and folder.is_dir()):
        return None
    for name in sorted(os.listdir(folder)):
        if name.endswith(".json") and ".attempt." not in name:
            try:
                data = json.loads((folder / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            evidence = data.get("evidence")
            if data.get("attempt_id") == attempt and isinstance(evidence, dict) and evidence.get("sha256"):
                return str(evidence["sha256"])
    return None


def file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def readable(meta: dict) -> bool:
    note = meta.get("note", "")
    return note.startswith("completed-") or note in READ_NOTES


def last_writers(rows, unit, root: Path) -> dict:
    """path -> attempt_id of the latest row of this unit (inside the window or not) that names that report file."""
    latest: dict[Path, tuple] = {}
    for number, (when, meta) in enumerate(rows):
        if meta.get("unit") != unit or not readable(meta):
            continue
        path = report_path(meta, root)
        if path and (path not in latest or (when, number) >= latest[path][:2]):
            latest[path] = (when, number, meta.get("attempt_id"))
    return {path: value[2] for path, value in latest.items()}


def overwritten(meta: dict, path: Path | None, root: Path, writers: dict) -> bool:
    """True when a later attempt wrote this file after the attempt in `meta`, so the file no longer holds its report."""
    if path is None:
        return False
    if meta.get("note") == "completed-marker":
        recorded, now = recorded_sha(meta, root), file_sha(path)
        if recorded and now:
            return recorded != now
    return writers.get(path, meta.get("attempt_id")) != meta.get("attempt_id")


_FENCE = re.compile(r"```.*?```", re.S)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_RED_HEAD = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?🔴")
_RED_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\*\*)?🔴")
_END_MARK = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:🟡|🟢|✅)")
_HEADING = re.compile(r"^\s*(#{1,6})(?=\s|\*|🔴|🟡|🟢|✅)")
_PLACE = re.compile(r"[\w./\-]+\.\w{1,8}:\d+")
_REPRO = re.compile(r"재현|repro|\b(?:when|if|given|whenever|once|after|with)\b|입력|조건|경우", re.I)
_UNRUN = re.compile(r"(?:could not|cannot|can't|unable to|not able to|did not|didn't)\s+(?:run|execute)|not run\b|"
                    r"미실행|실행하지 못|돌리지 못|실행 불가|확인하지 못", re.I)
_HANDED = re.compile(r"(?im)^\s*(?:[-*+]\s*)?(?:\*\*)?handed-over(?:\*\*)?:")
# An earlier item is named by a red marker + number, an r<N> or capital+number tag (B1), a file:line, or #<N>.
_ITEM_ID = re.compile(r"🔴\s*#?\d+|\b[Rr]\d+\b|\b[A-Z]\d+\b|[\w./\-]+\.\w{1,8}:\d+|#\d+")
_STATUS_BAD = re.compile(r"\b(?:open|reopened|re-opened|regressed|unclosed)\b|\b(?:not|never)\s+(?:yet\s+)?closed\b|"
                         r"열림|열린|열려|되돌아(?:옴|온|감|간|왔|갔)|해소되지|해소하지|해소 안|미해소|닫히지|닫지 못", re.I)
_STATUS_CLOSED = re.compile(r"\bclosed\b|닫힘|닫혔|닫음|해소(?!되지|하지|\s*안|\s*못)", re.I)


def _level(line: str) -> int:
    match = _HEADING.match(line)
    return len(match.group(1)) if match else 0


def _parse_red(text: str):
    """(items, consumed line indexes) of a review report's red section.

    The red section runs from a red heading to a heading of the same or a shallower depth, or to a yellow/green/check
    head. A deeper heading inside it is one item (with what follows up to the next heading of its own depth or
    shallower); otherwise an item is a bullet, or a bullet that itself starts with the red marker anywhere."""
    lines = _FENCE.sub("", text).splitlines()
    items, used = [], set()
    current, item_level, in_red, red_level = None, 0, False, 0

    def flush():
        nonlocal current, item_level
        if current:
            items.append(" ".join(current))
        current, item_level = None, 0

    for number, line in enumerate(lines):
        level = _level(line)
        if _END_MARK.match(line):
            flush()
            in_red = False
            continue
        if level or (_RED_HEAD.match(line) and not _BULLET.match(line)):
            is_red = bool(_RED_HEAD.match(line))
            if in_red and level and red_level and level > red_level:
                used.add(number)
                if current is not None and item_level and level > item_level:
                    current.append(line.strip())      # a sub-heading inside a heading item
                else:
                    flush()
                    current, item_level = [line.strip()], level
                continue
            flush()
            in_red, red_level = is_red, level
            if is_red:
                used.add(number)
            continue
        starts_red = bool(_RED_BULLET.match(line))
        if in_red or starts_red:
            used.add(number)
        if (starts_red or (in_red and _BULLET.match(line))) and not item_level:   # inside a heading item a red bullet belongs to it
            flush()
            current = [line.strip()]
        elif current is not None and line.strip():
            current.append(line.strip())
        elif not line.strip() and not item_level:
            flush()
    flush()
    return items, used


def red_items(text: str) -> list[str]:
    return _parse_red(text)[0]


def _prior_status(text: str, used: set) -> tuple[bool, bool]:
    """(some earlier item marked closed, some earlier item marked open/regressed): only a line outside the red section
    that names an item id counts."""
    closed = bad = False
    for number, line in enumerate(_FENCE.sub("", text).splitlines()):
        if number in used or not _ITEM_ID.search(line):
            continue
        closed = closed or bool(_STATUS_CLOSED.search(line))
        bad = bad or bool(_STATUS_BAD.search(line))
    return closed, bad


def analyse_report(text: str, round_index: int, blocking: bool) -> dict:
    # Red items exist only in a blocking verdict; a passing report's red heading lists what was cleared.
    items, used = _parse_red(text) if blocking else ([], set())
    closed, bad = _prior_status(text, used) if round_index >= 1 and items else (False, False)
    return {"red": len(items), "place": sum(bool(_PLACE.search(i)) for i in items),
            "repro": sum(bool(_REPRO.search(i)) for i in items),
            "unrun": sum(bool(_UNRUN.search(i)) for i in items),
            "handed": len(_HANDED.findall(text)), "closed_fail": closed and not bad and bool(items)}


# -- rendering ------------------------------------------------------------------------------------

def render_bytes(root: Path) -> dict:
    """Rendered worker-text bytes per unit in its usual form; qa/test (it runs in both) also in the stage form."""
    sys.path.insert(0, str(ROOT / "utilities"))
    import worker_bootstrap as wb  # noqa: E402
    out = {}
    jobs = [(unit, unit, WORKER_TYPE[unit]) for unit in UNITS] + [("qa/test@stage", "qa/test", "stage")]
    for key, unit, worker_type in jobs:
        try:
            out[key] = len(wb.render_worker_bootstrap(root, worker_type, unit=unit).encode("utf-8"))
        except (OSError, ValueError):
            out[key] = None
    return out


def render_bytes_of_ref(ref: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="unit-pilot-") as tmp:
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", ref, "roles"], capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout, check=True)
        return render_bytes(Path(tmp))


# -- the measurement ------------------------------------------------------------------------------

def sent_back(meta: dict) -> bool:
    """An execution <-> review send-back: a blocking review verdict, or the FAIL of a stage-form `qa/*` gate.
    Plan review never counts."""
    if meta.get("unit") in PLAN_UNITS or meta.get("dispatch_depth", "2") == "0":
        return False
    if meta.get("note") == BLOCKING:
        return True
    return (meta.get("note") == STAGE_FAIL and meta.get("failure_class") == "fail"
            and meta.get("unit", "").startswith("qa/"))


def measure(rows, root: Path, cut, side, rendered, cap=MAX_ATTEMPTS) -> dict:
    sent_by_route: dict[str, dict[str, int]] = {}
    verdict_order: dict[tuple, list] = {}
    for when, meta in rows:
        route = meta.get("route_id")
        if route and sent_back(meta) and not (side == "before" and when > cut):   # a send-back after the cut is not the baseline's
            by_unit = sent_by_route.setdefault(route, {})
            by_unit[meta.get("unit", "?")] = by_unit.get(meta.get("unit", "?"), 0) + 1
        if route and meta.get("unit") in REVIEW_UNITS and (meta.get("note", "").startswith("completed-") or meta.get("note") == STAGE_FAIL):
            verdict_order.setdefault((route, meta.get("route_node"), meta.get("unit")), []).append((when, meta.get("attempt_id")))
    result = {}
    for unit in UNITS:
        picked = window(rows, unit, cut, side, cap)
        routes = sorted({m["route_id"] for _, m in picked if m.get("route_id")})
        trips = [sum(sent_by_route.get(r, {}).values()) for r in routes]
        split: dict[str, int] = {}
        for r in routes:
            for judged, count in sent_by_route.get(r, {}).items():
                split[judged] = split.get(judged, 0) + count
        totals = {"red": 0, "place": 0, "repro": 0, "unrun": 0, "handed": 0, "closed_fail": 0}
        read = missing = lost = failing_read = failing_with_red = failing_rows = failing_lost = 0
        writers = last_writers(rows, unit, root)
        for when, meta in picked:
            note = meta.get("note", "")
            if not readable(meta):
                continue
            failing = note in (BLOCKING, STAGE_FAIL)   # a verdict that sent the work back; BLOCKED reached no verdict
            failing_rows += failing
            path = report_path(meta, root)
            if overwritten(meta, path, root, writers):
                lost += 1
                failing_lost += failing
                continue
            text = read_text(path)
            if text is None:
                missing += 1
                continue
            read += 1
            order = sorted(verdict_order.get((meta.get("route_id"), meta.get("route_node"), unit), []))
            index = next((i for i, (_, a) in enumerate(order) if a == meta.get("attempt_id")), 0)
            found = analyse_report(text, index, failing)
            failing_read += failing
            failing_with_red += failing and found["red"] > 0
            for key, value in found.items():
                totals[key] += int(value)
        notes = []
        red_measured = unit in REVIEW_UNITS and not (failing_rows and not failing_with_red)
        if unit not in REVIEW_UNITS:
            notes.append("items 2-4 (red items): not a review unit, its reports carry no red-item list")
        elif not red_measured:
            why = (f"{failing_read} FAIL/blocking reports read and none carries a red-item list" if failing_read
                   else "no FAIL/blocking report was left to read")
            notes.append(f"items 2-4 (red items): not measurable here, {why} ({failing_lost} of {failing_rows} blocking attempts, {lost} reports in all, overwritten by a later attempt)")
        elif lost or failing_with_red < failing_read:
            notes.append(f"items 2-4 (red items): partial, from the last attempt of each node only; {lost} reports ({failing_lost} of the "
                         f"{failing_rows} blocking ones) were overwritten by a later attempt and not read, a red item was read in "
                         f"{failing_with_red} of {failing_read} blocking reports read")
        result[unit] = {
            "attempts": len(picked),
            "window": [picked[0][0].isoformat(), picked[-1][0].isoformat()] if picked else None,
            "routes": len(routes),
            "roundtrips_median": median(trips), "roundtrips_p90": percentile(trips, 90),
            "roundtrips_by_unit": dict(sorted(split.items())),
            "closed_fail": totals["closed_fail"] if red_measured else None,
            "red_items": totals["red"] if red_measured else None,
            "red_with_place": totals["place"] if red_measured else None,
            "red_with_repro_cue": totals["repro"] if red_measured else None,
            "unrun_as_red": totals["unrun"] if red_measured else None,
            "handed_over_lines": totals["handed"],
            "render_bytes": rendered.get(unit),
            "render_bytes_stage": rendered.get("qa/test@stage") if unit == "qa/test" else None,
            "invalid_envelope": sum(m.get("note") == INVALID for _, m in picked),
            "reports_read": read, "reports_missing": missing, "reports_overwritten": lost,
            "red_reports": f"{failing_with_red}/{failing_read}" if unit in REVIEW_UNITS else None,
            "notes": notes,
        }
    return result


def format_table(result: dict) -> str:
    fields = ["attempts", "routes", "roundtrips_median", "roundtrips_p90", "closed_fail", "red_items", "red_with_place",
              "red_with_repro_cue", "unrun_as_red", "handed_over_lines", "render_bytes", "render_bytes_stage",
              "invalid_envelope", "reports_read", "reports_missing", "reports_overwritten", "red_reports"]
    lines = ["field".ljust(20) + "".join(u.ljust(18) for u in UNITS)]
    for field in fields:
        lines.append(field.ljust(20) + "".join(("—" if result[u][field] is None else str(result[u][field])).ljust(18) for u in UNITS))
    for unit in UNITS:
        lines.append(f"roundtrips_by_unit {unit}: " + (", ".join(f"{k}={v}" for k, v in result[unit]["roundtrips_by_unit"].items()) or "none"))
    lines.extend(f"note {unit}: {note}" for unit in UNITS for note in result[unit]["notes"])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    side = parser.add_mutually_exclusive_group(required=True)
    side.add_argument("--before", metavar="ISO_TIME", help="the window ending at this time (the baseline)")
    side.add_argument("--after", metavar="ISO_TIME", help="the window starting at this time (the pilot)")
    parser.add_argument("--state-root", help="dispatch state folder holding jobs.log and completion/ (default: the user's)")
    parser.add_argument("--render-ref", help="git ref whose roles/ the render bytes come from (default: this tree)")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS,
                        help="attempts per unit in a window (default 30; a larger value widens the window to the full 14 days)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = state_root(args.state_root)
    cut = parse_ts(args.before or args.after)
    rendered = render_bytes_of_ref(args.render_ref) if args.render_ref else render_bytes(ROOT)
    result = measure(read_rows(root / "jobs.log"), root, cut, "before" if args.before else "after", rendered, args.max_attempts)
    print(json.dumps(result, indent=1, sort_keys=True) if args.json else format_table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
