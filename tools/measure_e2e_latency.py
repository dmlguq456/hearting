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

import sys
sys.dont_write_bytecode = True  # before any other import: `live` must not write .pyc into the code checkout

import argparse
import importlib
import json
import os
from pathlib import Path
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
D1_LIMIT = 1.20            # d1: warm pass time with a doubled history stays under x1.20 (direction brief §4)
_VIEW_SEGMENT = "dispatch_registry_cache.RegistryView.__init__"
_REBUILD_SEGMENT = "dispatch_registry_cache._RegistrySegment.__init__"
_PREFIX_SEGMENT = "dispatch_registry_cache._reusable_prefix"


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
                "n", "n_total", "median_s", "p95_s", "max_s", "p95_basis")}})
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


def _full_parses(entry: dict) -> int:
    metrics = ((entry.get("after_row") or {}).get("metrics")) or {}
    return int(metrics.get("full_metadata_parses", 0) or 0)


def _segment_s(entry: dict, name: str):
    segment = (((entry.get("after_row") or {}).get("segments")) or {}).get(name)
    return segment.get("total_s") if isinstance(segment, dict) else None


def _ratio(small, large):
    return large / small if small and large is not None else None


def d1_rows(scale: dict) -> list[dict]:
    """One record per pair x role for the d1 criterion (history doubled, warm pass time).

    The verdict column is the whole pass time: large/small of the unchanged pass and of the pass
    right after a whole-registry replacement, each under ``D1_LIMIT``. The diagnostic columns
    (never part of the verdict) split the replacement pass into the floor (the new generation's
    read and digest: ``_reusable_prefix`` plus ``RegistryView.__init__`` minus segment
    reconstruction) and the rest.
    """
    rows = []
    for pair in scale.get("pairs", ()):
        for role in ("producer", "fleet"):
            small = pair["small"]["roles"].get(role, {})
            large = pair["large"]["roles"].get(role, {})
            if not small and not large:
                continue
            row = {"pair": pair["label"], "role": role,
                   "unchanged_small_s": small.get("warm_median_s"), "unchanged_large_s": large.get("warm_median_s"),
                   "after_small_s": small.get("after_replace_s"), "after_large_s": large.get("after_replace_s")}
            row["unchanged_ratio"] = _ratio(row["unchanged_small_s"], row["unchanged_large_s"])
            row["after_ratio"] = _ratio(row["after_small_s"], row["after_large_s"])
            floors = []
            for entry in (small, large):
                view, rebuild = _segment_s(entry, _VIEW_SEGMENT), _segment_s(entry, _REBUILD_SEGMENT)
                prefix = _segment_s(entry, _PREFIX_SEGMENT)
                floors.append(None if view is None else round(view - (rebuild or 0.0) + (prefix or 0.0), 4))
            row["floor_small_s"], row["floor_large_s"] = floors
            if None not in floors and row["after_small_s"] and row["after_large_s"] is not None:
                row["after_without_floor_ratio"] = _ratio(row["after_small_s"] - floors[0],
                                                          row["after_large_s"] - floors[1])
            else:
                row["after_without_floor_ratio"] = None
            ratios = (row["unchanged_ratio"], row["after_ratio"])
            row["met"] = None if None in ratios else all(value < D1_LIMIT for value in ratios)
            rows.append(row)
    return rows


def d1_lines(scale: dict) -> list[str]:
    rows = d1_rows(scale)
    if not rows:
        return []
    met = sum(1 for row in rows if row["met"])
    lines = [f"**d1 (history 2배, 판정 = 전체 시간 large/small < {D1_LIMIT:.2f}, 무변화·교체 직후 둘 다): "
             f"{met}/{len(rows)} 칸 충족**"]

    def fmt(value, spec=".3f"):
        return "-" if value is None else format(value, spec)

    for row in rows:
        verdict = {True: "충족", False: "미충족", None: "판정 불가"}[row["met"]]
        lines.append(
            f"- {row['pair']} {row['role']}: {verdict} — 무변화 {fmt(row['unchanged_small_s'])}s -> {fmt(row['unchanged_large_s'])}s "
            f"(x{fmt(row['unchanged_ratio'], '.2f')}), 교체 직후 {fmt(row['after_small_s'])}s -> {fmt(row['after_large_s'])}s "
            f"(x{fmt(row['after_ratio'], '.2f')}) | 진단: 바닥(읽기·digest) {fmt(row['floor_small_s'])}s -> {fmt(row['floor_large_s'])}s, "
            f"바닥을 뺀 직후 pass x{fmt(row['after_without_floor_ratio'], '.2f')}")
    return lines


def history_verdicts(scale: dict) -> list[str]:
    """The falsifier sentences, from the numbers (never from the expectation).

    The history verdict is read from the pass the user waits for: the pass right after the
    registry changed (one line's content differs), as the *extra* it costs over an unchanged
    pass. A role counts only when both sizes re-parsed the registry in that pass
    (``full_metadata_parses >= 1``); an unchanged pass reads nothing again, so its time says
    nothing about history and is listed only as the fixed cost.
    """
    lines = []
    changes = []
    for pair in scale.get("pairs", ()):
        for role in ("producer", "fleet"):
            small = pair["small"]["roles"].get(role, {})
            large = pair["large"]["roles"].get(role, {})
            extra_small, extra_large = small.get("extra_s"), large.get("extra_s")
            if extra_small is None or extra_large is None:
                continue
            parses = (_full_parses(small), _full_parses(large))
            if min(parses) < 1 or extra_small <= 0:
                lines.append(f"- {pair['label']} {role}: 교체 직후 추가분 판정 불가 (full parses {parses[0]}/{parses[1]}, "
                             f"추가분 {extra_small:+.3f}s -> {extra_large:+.3f}s)")
                continue
            change = _contrast(extra_small, extra_large)
            changes.append(change)
            lines.append(f"- {pair['label']} {role}: 교체 직후 추가분 {extra_small:.3f}s -> {extra_large:.3f}s "
                         f"({change:+.1%}, x{extra_large / extra_small:.2f}), full parses {parses[0]}/{parses[1]}")
    if changes:
        verdict = ("history 비례는 주원인 아님 (두 배에서 교체 직후 추가분 증가 < 20%)" if max(changes) < CONTRAST_LIMIT
                   else "history 비례가 주원인 후보 (두 배에서 교체 직후 추가분 증가 ≥ 20%)")
    else:
        verdict = "history 비례 판정 불가 (양쪽이 registry를 다시 해석한 교체 직후 pass가 없음)"
    lines.insert(0, f"**history 2배 대조 (바뀐 직후 pass의 추가분): {verdict}**")
    lines[1:1] = d1_lines(scale)
    fixed = []
    for pair in scale.get("pairs", ()):
        for role in ("producer", "fleet"):
            base = pair["small"]["roles"].get(role, {}).get("warm_median_s")
            big = pair["large"]["roles"].get(role, {}).get("warm_median_s")
            change = _contrast(base, big)
            if change is not None:
                fixed.append(f"- (참고, 판정 아님) {pair['label']} {role}: 무변화 pass {base:.3f}s -> {big:.3f}s "
                             f"({change:+.1%}) — registry를 다시 읽지 않는 pass의 고정 비용")
    shares = []
    detail = []
    for pair in scale.get("pairs", ()):
        for size in ("small", "large"):
            for role in ("producer", "fleet"):
                entry = pair[size]["roles"].get(role, {})
                if entry.get("after_replace_s"):
                    shares.append(entry["extra_s"] / entry["after_replace_s"])
                    detail.append(f"- {pair['label']} {size} {role}: unchanged {_fmt(entry['warm_median_s'])}s, "
                                  f"after replace {_fmt(entry['after_replace_s'])}s "
                                  f"(extra {entry['extra_s']:+.3f}s, full parses {_full_parses(entry)})")
    if shares:
        verdict = ("[다] 먼저 (교체 직후와 무변화의 차가 직후 pass의 20% 미만)" if max(shares) < CONTRAST_LIMIT
                   else "교체 직후 pass가 무변화보다 20% 이상 큼: '[다] 먼저' 조건 불충족")
        lines.insert(1, f"**통째 교체 대조: {verdict}**")
    return lines + detail + fixed


def _md_baseline(report: dict) -> list[str]:
    rows = []
    for item in report.get("results", ()):
        rows.extend(_t1_t2_rows([item]))
    out = ["## 기준 T1~T4", "", "| T | 사건 종류 | n (보임/전체) | 중앙값 s | p95 s | 최대 s | 목표 중앙/p95 |",
           "|---|---|---|---|---|---|---|"]
    seen = set()
    for row in rows:
        target = TARGETS.get(row["metric"], ("-", "-"))
        count = f"{row['n']}/{row['n_total']}" if row.get("n_total") is not None else f"{row['n']}"
        out.append(f"| {row['metric']} | {row['kind']} | {count} | {_fmt(row['median_s'])} | "
                   f"{_fmt(row['p95_s'])}{'*' if row.get('p95_basis') else ''} | {_fmt(row['max_s'])} | "
                   f"{target[0]} / {target[1]} |")
        seen.add(row["metric"])
    for metric in ("T1", "T2", "T3", "T4"):
        if metric not in seen:
            out.append(f"| {metric} | (측정 안 됨) | 0 | - | - | - | {TARGETS[metric][0]} / {TARGETS[metric][1]} |")
    out += ["", "`*` n < 20: p95는 nearest-rank로 n개 중 최대(`max-of-n`)다. "
            "n은 값이 실제로 보인 표본이고 전체는 넣은 사건 수다. 끝내 보이지 않은 표본(`not_visible`)은 시간을 적지 않는다.", ""]
    return out


def _md_outside(report: dict) -> list[str]:
    group = report.get("outside_registry")
    if not group:
        return []
    total = group["total_s"]
    out = ["## registry 밖 사건: producer 주기 대기 (한 기전)", "",
           f"{group['mechanism']}. 사건은 producer 주기({group['cadence_s']:g}s) 전체에 층화·seed 고정 위상으로 넣었다.", "",
           f"- 보임 {group['n']}/{group['n_total']}, 중앙 {_fmt(total['median_s'])}s, p95 {_fmt(total['p95_s'])}s"
           f"{'*' if total.get('p95_basis') else ''}, 최대 {_fmt(total['max_s'])}s",
           f"- 위상(마지막 게시 뒤 s) 분포(5초 칸): {json.dumps(group['phase_bins_5s'])}",
           f"- 주기 대기를 뺀 나머지(합계 − (주기 − 위상)): 중앙 {_fmt(group['residual_after_cadence_wait_s']['median_s'])}s, "
           f"최대 {_fmt(group['residual_after_cadence_wait_s']['max_s'])}s",
           f"- 다른 registry 변화가 producer를 일찍 깨운 표본: {group['woken_early']}개",
           f"- host panel 답이 값을 보인 표본(id·도구 이름이 있는 사건만): {json.dumps(group['host_shows'])}", "",
           "| 사건 | rep | 위상 s (게시 뒤) | 위상 s (pass 시작 뒤) | 합계 s | 주기 대기 s | 일찍 깸 |",
           "|---|---|---|---|---|---|---|"]
    for r in sorted(group["samples"], key=lambda r: (r["kind"], r["phase_s"] if r["phase_s"] is not None else -1)):
        out.append(f"| {r['kind']} | {r['rep']} | {_fmt(r['phase_s'])} | {_fmt(r['since_pass_start_s'])} | "
                   f"{_fmt(r['total_s'])} | {_fmt(r['cadence_wait_s'])} | {'예' if r['woken_early'] else '-'} |")
    return out + [""]


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
           f"- fixture shape (넣은 것): {json.dumps(report.get('fixture', {}).get('shape'))}",
           f"- fixture history shape (route·owner·BLOCKED·dead owner·stall): "
           f"{json.dumps(report.get('fixture', {}).get('history_shape'))}",
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
    for name, build in (("기준 T1~T4", _md_baseline), ("registry 밖 사건", _md_outside),
                        ("무변화 대 교체", _md_contrast),
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
        fixture = fixture_mod.build(probe=True, min_open_routes=2 * args.reps + 4, shape=True)
        report["fixture"] = {k: fixture[k] for k in ("bytes", "rows", "open_routes", "baseline", "marker")}
        shapes = _module("shapes")
        report["fixture"]["shape"] = shapes.describe(fixture) if shapes is not None else None
        report["fixture"]["history_shape"] = {k: v for k, v in (fixture.get("history_shape") or {}).items()
                                              if k != "blocked_attempts"}
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
                report["outside_registry"] = result.get("outside_registry")
                report["events"] = result["events"]
            if only & {"t1", "t2"} and only & {"t3", "t4"}:
                # T1/T2 leave the fixture changed (closed and killed routes, new launches, a replaced
                # registry); T3/T4 measure a fresh start, so they get an untouched fixture.
                cleanups.append({**fixture_mod.cleanup(fixture, keep_files=args.keep_scratch),
                                 "marker": fixture["marker"]})
                fixture = fixture_mod.build(probe=True, min_open_routes=2 * args.reps + 4, shape=True)
                scratch = Path(fixture["root"]) / "scratch"
                scratch.mkdir(exist_ok=True)
                report["notes"].append("T3/T4 ran on a second, untouched fixture of the same shape "
                                       "(T1/T2 change the first one).")
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
            if not any(c.get("marker") == fixture["marker"] for c in cleanups):
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
