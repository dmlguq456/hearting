#!/usr/bin/env python3
"""End-to-end reaction time of the shell panel and Fleet, measured and written down.

Not part of CI and not a regression test: it times real processes. Its unit test
(``tools/measure_e2e_latency.test.py``) never measures anything.

Subcommands
-----------
``run``   Build one isolated, operating-size fixture (registry copy <= 50 MiB,
          fixture HOME/XDG under ``/tmp/hearting-e2e-*``) and measure

          T1  route / worker value change -> shell panel backend projection
          T2  the same change -> the running Fleet's screen model
          T3  new Fleet run -> first observed screen (PTY)
          T4  the three harness hook chains, one prompt and one tool call each

          as median and p95 per event kind, with the warm and cold pass broken into
          segments. The default is ``--reps 3`` (a few minutes). ``--history-scale``
          instead runs the contrast the report needs: doubling only the *finished*
          history (two pairs, 50 MiB each) and "unchanged" versus "right after the
          whole registry was replaced".
``live``  Read-only sampler of the operator's own state (stat, mtime and
          ``observed_at`` only; no write, no signal): the latency from a registry
          change to the cache publish, as counts and times.

Isolation: every child starts from a cleared environment with fixture HOME/XDG/TMPDIR and a PATH
holding only python3/node/sh/git/sleep (no herdr, tmux, codex, claude, opencode); production
session discovery is replaced by the fixture's sleeper processes. Every process carries a
marker; the run ends by killing only marked processes, deleting the fixture and recording
``cleanup: {scratch_removed, leftover_processes}``.

A required measurement target that left no record (the shim went blind) makes ``run`` report the
failure and exit 1 instead of publishing numbers.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

TARGETS = {"T1": ("1 s", "2 s"), "T2": ("1 s", "2 s"), "T3": ("3 s", "-"), "T4": ("재고 정함", "-")}
HISTORY_PAIRS = (
    ("A (rows x2, row bytes fixed)", (3861, 5500), (7722, 5500)),
    ("B (rows fixed x2 of A, row bytes 2,900)", (7722, 2900), (15444, 2900)),
)
CONTRAST_LIMIT = 0.20


def _module(name: str):
    """A measurement module of ``e2e_latency`` or None when this checkout does not have it."""
    try:
        return importlib.import_module(f"e2e_latency.{name}")
    except ModuleNotFoundError as exc:
        if exc.name in (f"e2e_latency.{name}", "e2e_latency"):
            return None
        raise


def tag_kinds(metric: str, passes: list[dict]) -> list[dict]:
    """Give every pass record a ``kind``: ``t3-<condition>``, ``t4-<event>`` (T1/T2 already have one)."""
    for record in passes:
        if isinstance(record, dict) and not record.get("kind"):
            what = record.get("condition") or record.get("event") or record.get("hook") or "pass"
            record["kind"] = f"{metric.lower()}-{what}"
    return passes


def _load() -> list[float]:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except OSError:
        return []


def _fmt(value) -> str:
    return "-" if value is None else f"{value:.3f}"


def _contrast(base, doubled):
    if not base or doubled is None:
        return None
    return (doubled - base) / base


def _t1_t2_rows(results: list[dict]) -> list[dict]:
    rows = []
    for item in results:
        for kind, stats in item.get("events", {}).items():
            label = kind if not stats.get("no_observed_within_s") else (
                f"{kind} (loading을 {stats['no_observed_within_s']:g}s 안에 벗어나지 못함)")
            rows.append({"metric": item["metric"], "kind": label, **{k: stats.get(k) for k in (
                "n", "median_s", "p95_s", "max_s", "p95_basis")}})
    return rows


def replace_contrast(passes: list[dict]) -> dict | None:
    """Warm 'unchanged' passes versus passes right after a whole-registry replace.

    Replace events are the ones whose registry write is ``_atomic_registry_replace``:
    route-start (launch identity) and stage-transition.
    """
    out = {}
    for role in ("producer", "fleet"):
        warm = [p["dur_s"] for p in passes if p.get("role") == role and p.get("kind") == "warm-unchanged"]
        replaced = [p["dur_s"] for p in passes if p.get("role") == role
                    and p.get("kind") in ("after-route-start", "after-stage-transition", "after-replace")]
        if warm and replaced:
            warm_m = sorted(warm)[len(warm) // 2]
            rep_m = sorted(replaced)[len(replaced) // 2]
            out[role] = {"warm_median_s": warm_m, "after_replace_median_s": rep_m,
                         "extra_s": round(rep_m - warm_m, 4),
                         "extra_share": round((rep_m - warm_m) / rep_m, 4) if rep_m else None}
    return out or None


def history_verdicts(scale: dict) -> list[str]:
    """The two falsifier sentences, from the numbers (never from the expectation)."""
    lines = []
    shares = []
    for pair in scale.get("pairs", ()):
        for role in ("producer", "fleet"):
            base = pair["small"]["roles"].get(role, {}).get("warm_median_s")
            big = pair["large"]["roles"].get(role, {}).get("warm_median_s")
            change = _contrast(base, big)
            if change is not None:
                shares.append(change)
                lines.append(f"- {pair['label']} {role}: warm {base:.3f}s -> {big:.3f}s ({change:+.1%})")
    if shares:
        verdict = ("history 비례는 주원인 아님 (두 배에서 pass 증가 < 20%)" if max(shares) < CONTRAST_LIMIT
                   else "history 비례가 주원인 후보 (두 배에서 pass 증가 ≥ 20%)")
        lines.insert(0, f"**history 2배 대조: {verdict}**")
    shares = []
    for pair in scale.get("pairs", ()):
        for size in ("small", "large"):
            for role in ("producer", "fleet"):
                entry = pair[size]["roles"].get(role, {})
                if entry.get("after_replace_s"):
                    shares.append(entry["extra_s"] / entry["after_replace_s"])
                    lines.append(f"- {pair['label']} {size} {role}: unchanged {_fmt(entry['warm_median_s'])}s, "
                                 f"after replace {_fmt(entry['after_replace_s'])}s "
                                 f"(extra {entry['extra_s']:+.3f}s)")
    if shares:
        verdict = ("[다] 먼저 (교체 직후와 무변화의 차가 직후 pass의 20% 미만)" if max(shares) < CONTRAST_LIMIT
                   else "교체 직후 pass가 무변화보다 20% 이상 큼: '[다] 먼저' 조건 불충족")
        lines.insert(1 if lines and lines[0].startswith("**history") else 0,
                     f"**통째 교체 대조: {verdict}**")
    return lines


def _md_baseline(report: dict) -> list[str]:
    rows = []
    for item in report.get("results", ()):
        rows.extend(_t1_t2_rows([item]))
    out = ["## 기준 T1~T4", "", "| T | 사건 종류 | n | 중앙값 s | p95 s | 최대 s | 목표 중앙/p95 |",
           "|---|---|---|---|---|---|---|"]
    seen = set()
    for row in rows:
        target = TARGETS.get(row["metric"], ("-", "-"))
        out.append(f"| {row['metric']} | {row['kind']} | {row['n']} | {_fmt(row['median_s'])} | "
                   f"{_fmt(row['p95_s'])}{'*' if row.get('p95_basis') else ''} | {_fmt(row['max_s'])} | "
                   f"{target[0]} / {target[1]} |")
        seen.add(row["metric"])
    for metric in ("T1", "T2", "T3", "T4"):
        if metric not in seen:
            out.append(f"| {metric} | (측정 안 됨) | 0 | - | - | - | {TARGETS[metric][0]} / {TARGETS[metric][1]} |")
    out += ["", "`*` n < 20: p95는 nearest-rank로 n개 중 최대(`max-of-n`)다.", ""]
    return out


def _md_contrast(report: dict) -> list[str]:
    contrast = report.get("replace_contrast")
    if not contrast:
        return []
    out = ["## 무변화 pass 대 통째 교체 직후 pass (사건 측정에서)", ""]
    for role, entry in contrast.items():
        out.append(f"- {role}: unchanged {_fmt(entry['warm_median_s'])}s, after replace "
                   f"{_fmt(entry['after_replace_median_s'])}s (extra {entry['extra_s']:+.3f}s, "
                   f"{(entry['extra_share'] or 0):.0%} of the pass)")
    return out + [""]


def _md_passes(report: dict) -> list[str]:
    """The T1/T2 producer and Fleet passes with their segments (a T3/T4 record has none)."""
    passes = [p for p in (report.get("passes") or ())
              if isinstance(p.get("segments"), dict) and p.get("role")]
    if not passes:
        return []
    out = ["## warm·cold pass 구간 내역", "",
           "| role | # | kind | pass s | 구간(합 s, 호출 수) | full parses | open |", "|---|---|---|---|---|---|---|"]
    interesting = [p for p in passes if p.get("kind") != "warm-unchanged"][:40]
    warm = [p for p in passes if p.get("kind") == "warm-unchanged"]
    for role in ("producer", "fleet"):
        group = [p for p in warm if p["role"] == role]
        if group:
            interesting.append(sorted(group, key=lambda p: p.get("dur_s") or 0)[len(group) // 2])
    for p in interesting:
        segs = ", ".join(f"{name} {s['total_s']:.3f}({s['n']})" for name, s in p["segments"].items()
                         if s["total_s"] >= 0.01)
        out.append(f"| {p['role']} | {p.get('index')} | {p.get('kind')} | {p.get('dur_s') or 0:.3f} | {segs} | "
                   f"{p.get('metrics', {}).get('full_metadata_parses', 0)} | {p.get('open_calls')} |")
    return out + [""]


def _md_launches(report: dict) -> list[str]:
    """T3 launches: one row per new Fleet run (no producer/Fleet segments)."""
    launches = [p for p in (report.get("passes") or ()) if p.get("condition")]
    if not launches:
        return []
    out = ["## T3 launch별 (새 실행 → 화면)", "",
           "| 조건 | # | 첫 byte s | 첫 비-loading s | 첫 관측 화면 s | 상세 완료 s | paints | 저장 |",
           "|---|---|---|---|---|---|---|---|"]
    for p in launches:
        out.append(f"| {p.get('condition')} | {p.get('rep')} | {_fmt(p.get('first_byte_s'))} | "
                   f"{_fmt(p.get('first_nonloading_s'))} | {_fmt(p.get('first_observed_s'))} | "
                   f"{_fmt(p.get('details_complete_s'))} | {p.get('paints')} | {p.get('persisted')} |")
    return out + [""]


def _seg_value(value) -> str:
    """A segment is a stats dict (median_s), a plain number (T4 opencode) or a small dict of those."""
    if isinstance(value, dict):
        if value.get("median_s") is not None:
            return f"{value['median_s']:.3f}"
        return "{" + ", ".join(f"{k} {_seg_value(v)}" for k, v in value.items()
                               if isinstance(v, (int, float, dict)) and not isinstance(v, bool)) + "}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.3f}"
    return str(value)


def _md_segments(report: dict) -> list[str]:
    out = []
    for item in report.get("results", ()):
        for kind, segs in (item.get("segments") or {}).items():
            if isinstance(segs, list):          # T4: one record per hook of the chain
                parts = [f"{str(h.get('hook', '?')).rsplit('/', 1)[-1]} {h.get('median_ms', 0):.0f}ms"
                         f"{' (하한)' if h.get('denied') or h.get('path_gaps') else ''}"
                         for h in segs if isinstance(h, dict)]
                if parts:
                    out.append(f"- {item['metric']} {kind}: {', '.join(parts)}")
                continue
            if isinstance(segs, dict):
                line = ", ".join(f"{name} {_seg_value(value)}" for name, value in segs.items())
                if line:
                    out.append(f"- {item['metric']} {kind}: {line}")
    return out


def _md_info(report: dict) -> list[str]:
    out = ["", "## 실행 정보", "",
           f"- elapsed_s: {report.get('elapsed_s')}",
           f"- load before/after: {report.get('load_before')} / {report.get('load_after')}",
           f"- fixture: registry {report.get('fixture', {}).get('bytes')} bytes, "
           f"rows {report.get('fixture', {}).get('rows')}, baseline {report.get('fixture', {}).get('baseline')}",
           f"- cleanup: {json.dumps(report.get('cleanup'))}"]
    failures = (report.get("self_check") or {}).get("failures") or []
    if failures:
        out += ["", f"**자체 점검 실패: {failures}** (이 지표는 측정값으로 내지 않았다)"]
    for entry in report.get("not_measured", ()):
        out.append(f"- not measured: {entry.get('what')} — {entry.get('reason')}")
    for note in report.get("notes", ()):
        out.append(f"- note: {note}")
    return out


def _guarded(name: str, build, report: dict) -> list[str]:
    """One section failing to render must not erase the others."""
    try:
        return build(report)
    except Exception as exc:  # noqa: BLE001
        return [f"(`{name}` 표를 그리지 못함: {type(exc).__name__}: {exc})", ""]


def render_markdown(report: dict) -> str:
    out = ["# 끝-끝 반응 속도 측정", ""]
    scale = report.get("history_scale")
    if scale and not report.get("results"):
        out += ["## history 2배 대조와 통째 교체", "", "(`--history-scale` 실행: T1~T4 사건 측정은 하지 않았다. "
                "기준 T1~T4 표는 기본 `run`의 보고서에 있다.)", ""] + history_verdicts(scale) + [""]
        for pair in scale["pairs"]:
            out.append(f"- fixture {pair['label']}: small {pair['small'].get('rows')} rows / "
                       f"{pair['small'].get('fixture_bytes')} bytes, large {pair['large'].get('rows')} rows / "
                       f"{pair['large'].get('fixture_bytes')} bytes")
        out += ["", "## 실행 정보", "", f"- elapsed_s: {report.get('elapsed_s')}",
                f"- load before/after: {report.get('load_before')} / {report.get('load_after')}",
                f"- cleanup: {json.dumps(report.get('cleanup'))}"]
        return "\n".join(out) + "\n"
    for name, build in (("기준 T1~T4", _md_baseline), ("무변화 대 교체", _md_contrast),
                        ("pass 구간", _md_passes), ("T3 launch", _md_launches),
                        ("구간 중앙값", _md_segments), ("실행 정보", _md_info)):
        out += _guarded(name, build, report)
    return "\n".join(out) + "\n"


def run_history_scale(fixture_mod, t12, scratch_root: Path, keep: bool) -> tuple[dict, list[dict]]:
    pairs, cleanups = [], []
    for label, small, large in HISTORY_PAIRS:
        pair = {"label": label}
        for name, (rows, row_bytes) in (("small", small), ("large", large)):
            fixture = fixture_mod.build(history_rows=rows, row_bytes=row_bytes, probe=False)
            scratch = Path(fixture["root"]) / "scratch"
            scratch.mkdir(exist_ok=True)
            try:
                pair[name] = t12.measure_replace(fixture, scratch=scratch)
                pair[name]["fixture_bytes"] = fixture["bytes"]
            finally:
                cleanups.append(fixture_mod.cleanup(fixture, keep_files=keep))
        pairs.append(pair)
    return {"pairs": pairs,
            "interpretation": "history = finished attempt rows; open routes and sessions fixed; "
                              "pair A doubles the row count at the live row size, pair B doubles "
                              "the rows again at a smaller row size so each registry stays <= 50 MiB."}, cleanups


def cmd_run(args) -> int:
    fixture_mod = _module("fixture")
    t12 = _module("t12")
    if fixture_mod is None or t12 is None:
        print("e2e_latency modules are not present", file=sys.stderr)
        return 2
    started = time.monotonic()
    report: dict = {"command": "run", "reps": args.reps, "load_before": _load(), "results": [],
                    "passes": [], "notes": [], "not_measured": [], "history_scale": None}
    cleanups = []
    code = 0
    if args.history_scale:
        scale, cleanups = run_history_scale(fixture_mod, t12, Path("/tmp"), args.keep_scratch)
        report["history_scale"] = scale
        for pair in scale["pairs"]:
            for size in ("small", "large"):
                for role, check in pair[size]["self_check"].items():
                    if check.get("failures"):
                        report.setdefault("self_check", {"failures": []})["failures"] += check["failures"]
    else:
        only = {item.strip().lower() for item in (args.only or "t1,t2,t3,t4").split(",") if item.strip()}
        fixture = fixture_mod.build(probe=True)
        report["fixture"] = {k: fixture[k] for k in ("bytes", "rows", "open_routes", "baseline", "marker")}
        scratch = Path(fixture["root"]) / "scratch"
        scratch.mkdir(exist_ok=True)
        try:
            if only & {"t1", "t2"}:
                result = t12.measure(fixture, reps=args.reps, scratch=scratch)
                failures = result["self_check"]["failures"]
                report["self_check"] = result["self_check"]
                if failures:
                    code = 1
                    report["not_measured"].append({"what": "T1/T2", "reason": f"self-check failed: {failures}"})
                else:
                    report["results"].extend(result["results"])
                    report["passes"].extend(result["passes"])
                    report["replace_contrast"] = replace_contrast(result["passes"])
                report["notes"].extend(result["notes"])
                report["not_measured"].extend(result["not_measured"])
                report["shim_mode"] = result.get("shim_mode")
                report["events"] = result["events"]
            for name, key in (("t3", "T3"), ("t4", "T4")):
                if name not in only:
                    continue
                module = _module(name)
                if module is None:
                    report["not_measured"].append({"what": key, "reason": f"e2e_latency.{name} is not present in this checkout"})
                    continue
                try:
                    sub = module.measure(fixture, reps=args.reps, scratch=scratch)
                except Exception as exc:  # one metric failing must not discard the others
                    report["not_measured"].append({"what": key, "reason": f"{type(exc).__name__}: {exc}"})
                    code = 1
                    continue
                report["results"].append({k: sub.get(k) for k in ("metric", "events", "segments")})
                report["passes"].extend(tag_kinds(key, sub.get("passes") or []))
                report["notes"].extend(sub.get("notes") or [])
                report["not_measured"].extend(sub.get("not_measured") or [])
                if sub.get("violations"):
                    code = 1
                    report.setdefault("violations", []).extend(sub["violations"])
        finally:
            cleanups.append(fixture_mod.cleanup(fixture, keep_files=args.keep_scratch))
    report["cleanup"] = {"scratch_removed": all(c["scratch_removed"] for c in cleanups) or args.keep_scratch,
                         "leftover_processes": sum(c["leftover_processes"] for c in cleanups),
                         "runs": cleanups}
    if report["cleanup"]["leftover_processes"]:
        code = 1
    report["load_after"] = _load()
    report["elapsed_s"] = round(time.monotonic() - started, 1)
    report["exit_code"] = code
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    try:
        markdown = render_markdown(report)
    except Exception as exc:  # noqa: BLE001 - the JSON is already written; never lose the run to its report
        markdown = f"# 끝-끝 반응 속도 측정\n\n(Markdown을 그리지 못함: {type(exc).__name__}: {exc}; JSON에는 전부 있다)\n"
        code = code or 1
        report["exit_code"] = code
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(markdown, encoding="utf-8")
    if not args.json and not args.markdown:
        print(markdown)
    return code


def cmd_live(args) -> int:
    live = _module("live")
    if live is None:
        print("e2e_latency.live is not present in this checkout", file=sys.stderr)
        return 2
    argv = ["--duration", str(args.duration), "--period", str(args.period)]
    if args.out:
        argv += ["--out", args.out]
    if args.markdown:
        argv += ["--markdown", args.markdown]
    return int(live.main(argv) or 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="isolated, operating-size measurement of T1-T4", allow_abbrev=False)
    run.add_argument("--only", default="", help="comma list of t1,t2,t3,t4 (t1 and t2 share one run)")
    run.add_argument("--reps", type=int, default=3, help="events per kind (default 3; 8 for baseline statistics)")
    run.add_argument("--history-scale", action="store_true",
                     help="measure only the history-doubling and whole-registry-replace contrasts")
    run.add_argument("--json", default="")
    run.add_argument("--markdown", default="")
    run.add_argument("--keep-scratch", action="store_true", help="keep fixture files (processes are still reaped)")
    live = sub.add_parser("live", help="read-only sampler of the operator's registry -> cache latency",
                          allow_abbrev=False)
    live.add_argument("--duration", type=float, default=600.0)
    live.add_argument("--period", type=float, default=0.1)
    live.add_argument("--out", default="")
    live.add_argument("--markdown", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        if args.reps < 1:
            print("--reps must be >= 1", file=sys.stderr)
            return 2
        return cmd_run(args)
    return cmd_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
