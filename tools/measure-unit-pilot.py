#!/usr/bin/env python3
"""Count the six things the two-layer unit pilot is judged on. Read-only, no model, stdlib only.

The pilot units (qa/code-review, qa/test, dev/backend, editorial/report) were split into
`## 지킬 것` / `## 참고`. Whether that helps is read from records that already exist: the dispatch
registry (`jobs.log`) and the report each reviewer or stage wrote. Nothing here runs on a timer; the
supervisor runs it by hand once before the pilot (baseline) and once about two weeks after.

    python3 tools/measure-unit-pilot.py --before 2026-11-18T00:00:00Z [--json]   # the window before
    python3 tools/measure-unit-pilot.py --after  2026-11-18T00:00:00Z [--json]   # the window after

A window is, per unit, the attempts of that unit inside 14 days of the cut, at most 30, whichever is
reached first (`--before` takes the latest ones before the cut, `--after` the earliest after it). Output
carries counts and kinds only: unit ids, numbers, route ids; never a project name, title or report text.

The six items (per unit):
  1 roundtrips      review verdicts that sent the work back (note `completed-review-blocking`) in each route
                    the unit appears in: median and 90th percentile over those routes
  2 closed_fail     round >= 2 blocking verdicts whose report marks earlier red items closed and none open or
                    regressed (a FAIL for a new finding); the goal is 0
  3 place / repro   share of red items carrying a `file:line`, and a reproduction-condition cue
  4 unrun_red       red items that are really "could not run a check"; handed_over: `handed-over:` lines
  5 render_bytes    bytes of the worker text the runtime renders for the unit (`--render-ref REF` for another tree)
  6 invalid_envelope attempts that ended `dead-invalid-envelope`
Items 2-4 read report text and are heuristics (`reports_read` / `reports_missing` say how much was readable).
"""
from __future__ import annotations

import argparse
import datetime as dt
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
        "completion_marker", "model_profile"}


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


_FENCE = re.compile(r"```.*?```", re.S)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_RED_HEAD = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?🔴")
_OTHER_HEAD = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:🟡|🟢|✅)|^\s*#{1,6}\s")
_PLACE = re.compile(r"[\w./\-]+\.\w{1,8}:\d+")
_REPRO = re.compile(r"재현|repro|\b(?:when|if|given|whenever|once|after|with)\b|입력|조건|경우", re.I)
_UNRUN = re.compile(r"(?:could not|cannot|can't|unable to|not able to|did not|didn't)\s+(?:run|execute)|not run\b|"
                    r"미실행|실행하지 못|돌리지 못|실행 불가|확인하지 못", re.I)
_HANDED = re.compile(r"(?im)^\s*(?:[-*+]\s*)?(?:\*\*)?handed-over(?:\*\*)?:")
_STATUS_CLOSED = re.compile(r"\bclosed\b|닫힘|해소", re.I)
_STATUS_BAD = re.compile(r"\b(?:open|regressed)\b|열림|되돌아감", re.I)


def red_items(text: str) -> list[str]:
    """The blocking items of a review report: bullets under a red heading, else bullets that start with 🔴."""
    lines = _FENCE.sub("", text).splitlines()
    items, current, in_red = [], None, False

    def flush():
        nonlocal current
        if current:
            items.append(" ".join(current))
        current = None

    for line in lines:
        if _RED_HEAD.match(line) and not _BULLET.match(line):
            flush()
            in_red = True
            continue
        if _OTHER_HEAD.match(line) and not _RED_HEAD.match(line):
            flush()
            in_red = False
            continue
        starts_red = bool(_BULLET.match(line)) and "🔴" in line[:12]
        if starts_red or (in_red and _BULLET.match(line)):
            flush()
            current = [line.strip()]
        elif current is not None and line.strip():
            current.append(line.strip())
        elif not line.strip():
            flush()
    flush()
    return items


def analyse_report(text: str, round_index: int, blocking: bool) -> dict:
    # Red items exist only in a blocking verdict; a passing report's red heading lists what was cleared.
    items = red_items(text) if blocking else []
    closed_fail = round_index >= 1 and bool(_STATUS_CLOSED.search(text)) and not _STATUS_BAD.search(_FENCE.sub("", text))
    return {"red": len(items), "place": sum(bool(_PLACE.search(i)) for i in items),
            "repro": sum(bool(_REPRO.search(i)) for i in items),
            "unrun": sum(bool(_UNRUN.search(i)) for i in items),
            "handed": len(_HANDED.findall(text)), "closed_fail": closed_fail and bool(items)}


# -- rendering ------------------------------------------------------------------------------------

def render_bytes(root: Path) -> dict:
    sys.path.insert(0, str(ROOT / "utilities"))
    import worker_bootstrap as wb  # noqa: E402
    out = {}
    for unit in UNITS:
        try:
            out[unit] = len(wb.render_worker_bootstrap(root, WORKER_TYPE[unit], unit=unit).encode("utf-8"))
        except (OSError, ValueError):
            out[unit] = None
    return out


def render_bytes_of_ref(ref: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="unit-pilot-") as tmp:
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", ref, "roles"], capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout, check=True)
        return render_bytes(Path(tmp))


# -- the measurement ------------------------------------------------------------------------------

def measure(rows, root: Path, cut, side, rendered, cap=MAX_ATTEMPTS) -> dict:
    blocking_by_route: dict[str, int] = {}
    verdict_order: dict[tuple, list] = {}
    for when, meta in rows:
        route = meta.get("route_id")
        if route and meta.get("note") == BLOCKING and meta.get("dispatch_depth", "2") != "0":
            blocking_by_route[route] = blocking_by_route.get(route, 0) + 1
        if route and meta.get("note", "").startswith("completed-") and meta.get("unit") in REVIEW_UNITS:
            verdict_order.setdefault((route, meta.get("route_node"), meta.get("unit")), []).append((when, meta.get("attempt_id")))
    result = {}
    for unit in UNITS:
        picked = window(rows, unit, cut, side, cap)
        routes = sorted({m["route_id"] for _, m in picked if m.get("route_id")})
        trips = [blocking_by_route.get(r, 0) for r in routes]
        totals = {"red": 0, "place": 0, "repro": 0, "unrun": 0, "handed": 0, "closed_fail": 0}
        read = missing = 0
        for when, meta in picked:
            if not (meta.get("note", "").startswith("completed-") or meta.get("note") == INVALID):
                continue
            text = read_text(report_path(meta, root))
            if text is None:
                missing += 1
                continue
            read += 1
            order = sorted(verdict_order.get((meta.get("route_id"), meta.get("route_node"), unit), []))
            index = next((i for i, (_, a) in enumerate(order) if a == meta.get("attempt_id")), 0)
            for key, value in analyse_report(text, index, meta.get("note") == BLOCKING).items():
                totals[key] += int(value)
        result[unit] = {
            "attempts": len(picked),
            "window": [picked[0][0].isoformat(), picked[-1][0].isoformat()] if picked else None,
            "routes": len(routes),
            "roundtrips_median": median(trips), "roundtrips_p90": percentile(trips, 90),
            "closed_fail": totals["closed_fail"] if unit in REVIEW_UNITS else None,
            "red_items": totals["red"] if unit in REVIEW_UNITS else None,
            "red_with_place": totals["place"] if unit in REVIEW_UNITS else None,
            "red_with_repro_cue": totals["repro"] if unit in REVIEW_UNITS else None,
            "unrun_as_red": totals["unrun"] if unit in REVIEW_UNITS else None,
            "handed_over_lines": totals["handed"],
            "render_bytes": rendered.get(unit),
            "invalid_envelope": sum(m.get("note") == INVALID for _, m in picked),
            "reports_read": read, "reports_missing": missing,
        }
    return result


def format_table(result: dict) -> str:
    fields = ["attempts", "routes", "roundtrips_median", "roundtrips_p90", "closed_fail", "red_items", "red_with_place",
              "red_with_repro_cue", "unrun_as_red", "handed_over_lines", "render_bytes", "invalid_envelope",
              "reports_read", "reports_missing"]
    lines = ["field".ljust(20) + "".join(u.ljust(18) for u in UNITS)]
    for field in fields:
        lines.append(field.ljust(20) + "".join(str(result[u][field]).ljust(18) for u in UNITS))
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
