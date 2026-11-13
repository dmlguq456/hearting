#!/usr/bin/env python3
"""T4: what the three harnesses' hook chains add to one prompt and one tool call.

No model call. Claude and Codex run the real hook commands from their settings
through ``tools/measure_hook_before_after.py --chain`` machinery (Python hooks
refused outside the fixture, shell hooks noticed by before/after fingerprints).
OpenCode runs its plugin callbacks directly through ``opencode_chain.mjs``.

``measure(fixture, *, reps, scratch)`` follows the shared result shape: ``events``
by ``harness:event``, ``segments`` per hook (or per plugin callback), ``notes``,
``not_measured`` and ``violations``. A fingerprint violation discards the numbers.

Run alone for a quick look (the fixture must be a disposable ``hook_perf_fixture``
base)::

    python3 tools/e2e_latency/t4.py --state-root <fixture base> --reps 3
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OPENCODE_SCRIPT = HERE / "opencode_chain.mjs"
CHAIN_SPECS = ("claude:prompt", "claude:tool", "claude:stop",
               "codex:prompt", "codex:tool", "codex:stop")
OPENCODE_PROMPT = "chat.message"
OPENCODE_TOOL = ("tool.execute.before", "tool.execute.after")


def _load_measure_module():
    path = ROOT / "tools" / "measure_hook_before_after.py"
    spec = importlib.util.spec_from_file_location("measure_hook_before_after", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("measure_hook_before_after", module)
    spec.loader.exec_module(module)
    return module


def percentile(values, fraction: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def event_stats(samples_s: list[float], **extra) -> dict:
    stats = {
        "n": len(samples_s),
        "median_s": round(statistics.median(samples_s), 6) if samples_s else 0.0,
        "p95_s": round(percentile(samples_s, 0.95), 6),
        "max_s": round(max(samples_s), 6) if samples_s else 0.0,
        "samples_s": [round(value, 6) for value in samples_s],
    }
    if len(samples_s) < 20:
        stats["p95_basis"] = "max-of-n"
    stats.update(extra)
    return stats


def chain_rep_samples(chain: dict) -> list[float]:
    """Per-repeat sequential sum (s) of the blocking hooks of one chain result."""
    blocking = [hook for hook in chain["hooks"] if hook["blocking"]]
    if not blocking:
        return []
    reps = min(len(hook["runs_ms"]) for hook in blocking)
    return [sum(hook["runs_ms"][rep] for hook in blocking) / 1000 for rep in range(reps)]


def chain_events(chain_result: dict) -> tuple[dict, dict]:
    """Turn ``measure_chain`` output into ``events`` and per-hook ``segments``."""
    events: dict[str, dict] = {}
    segments: dict[str, list] = {}
    for spec, chain in chain_result["chains"].items():
        events[spec] = event_stats(
            chain_rep_samples(chain),
            sequential_upper_bound_s=chain["sum_s"],
            parallel_lower_bound_s=chain["max_s"],
            sum_s_incl_async=chain["sum_s_incl_async"],
            sum_s_lower_bound_hooks=chain["sum_s_lower_bound_hooks"],
            n_hooks=chain["n_hooks"], n_blocking=chain["n_blocking"])
        segments[spec] = [
            {"hook": hook["hook"], "event": hook["event"], "blocking": hook["blocking"],
             "median_ms": hook["median_ms"], "first_ms": hook["first_ms"],
             "returncode": hook["returncode"], "denied": hook["denied"],
             "path_gaps": hook["path_gaps"]}
            for hook in chain["hooks"]]
    return events, segments


def opencode_events(payload: dict) -> tuple[dict, dict]:
    """Turn ``opencode_chain.mjs`` output into ``events`` and per-callback ``segments``."""
    runs = {name: payload["events"][name]["runs"] for name in payload["events"]}
    events: dict[str, dict] = {}
    prompt = runs.get(OPENCODE_PROMPT, [])
    events["opencode:prompt"] = event_stats(
        [run["ms"] / 1000 for run in prompt],
        sync_children_median=payload["events"].get(OPENCODE_PROMPT, {}).get("sync_children_median"),
        detached_children_median=payload["events"].get(OPENCODE_PROMPT, {}).get("detached_children_median"))
    before, after = (runs.get(name, []) for name in OPENCODE_TOOL)
    tool = [(b["ms"] + a["ms"]) / 1000 for b, a in zip(before, after)]
    events["opencode:tool"] = event_stats(
        tool,
        sync_children_median=statistics.median(
            [b["sync_children"] + a["sync_children"] for b, a in zip(before, after)] or [0]),
        detached_children_median=statistics.median(
            [b["detached_children"] + a["detached_children"] for b, a in zip(before, after)] or [0]))
    segments = {
        name: {
            "median_ms": round(info["median_ms"], 3),
            "sync_children_median": info["sync_children_median"],
            "detached_children_median": info["detached_children_median"],
            "threw": info["threw"],
            "commands": sorted({cmd for run in info["runs"] for cmd in run["commands"]}),
        } for name, info in payload["events"].items()}
    segments["plugin_import_ms"] = round(payload["import_ms"], 3)
    return events, segments


def run_opencode(mh, code_root: Path, state_root: Path, *, reps: int, session: str,
                 fixture_env: dict, ops_roots, markers: list[str], settle_s: float,
                 timeout: float = 300.0) -> dict:
    """One ``node opencode_chain.mjs`` run in the fixture env, fingerprinted before and after."""
    node = shutil.which("node")
    if not node:
        return {"skipped": "node not on PATH"}
    ws = mh._chain_workspace(code_root, state_root)
    env = mh.chain_env(ws, state_root, fixture_env)
    audit_base = str(ws["tmp"] / "opencode-audit")
    env["HOOK_AUDIT_LOG"] = audit_base
    before = mh._snapshot(code_root, ops_roots, markers, str(ws["gitconfig"]))
    started = time.monotonic()
    completed = subprocess.run(
        [node, str(OPENCODE_SCRIPT), "--code-root", str(code_root), "--directory", str(ws["project"]),
         "--reps", str(reps), "--session", session],
        cwd=str(ws["project"]), env=env, capture_output=True, text=True, timeout=timeout)
    if settle_s:
        time.sleep(settle_s)
    after = mh._snapshot(code_root, ops_roots, markers, str(ws["gitconfig"]))
    violation = mh._snapshot_violation({"chain": "opencode", "hook": "<plugin>"}, before, after)
    result = {"returncode": completed.returncode, "elapsed_s": round(time.monotonic() - started, 3),
              "denied": sorted(set(mh._gather_audit(audit_base))), "violation": violation,
              "stderr_tail": completed.stderr[-400:]}
    try:
        result["payload"] = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result["error"] = "no JSON from opencode_chain.mjs"
    return result


def measure(fixture: dict, *, reps: int = 5, scratch: Path | None = None,
            code_root: Path | None = None, ops_roots=None, settle_s: float = 0.5,
            specs: tuple[str, ...] = CHAIN_SPECS, include_opencode: bool = True) -> dict:
    """T4 over one disposable fixture (``hook_perf_fixture.build`` output or the orchestrator dict)."""
    mh = _load_measure_module()
    code_root = Path(code_root or fixture.get("code_root") or ROOT).resolve()
    state_root = Path(fixture.get("root") or fixture.get("base") or "").resolve()
    if not state_root.is_dir():
        raise ValueError("fixture has no root/base directory")
    ops_roots = [Path(item) for item in (ops_roots if ops_roots is not None
                                          else mh.default_ops_roots(code_root))]
    marker = str(fixture.get("marker") or state_root.name)
    fixture_env = fixture.get("env") if isinstance(fixture.get("env"), dict) else None
    chain = mh.measure_chain(list(specs), code_root, state_root, repeat=reps, ops_roots=ops_roots,
                             fixture_env=fixture_env, marker=marker, settle_s=settle_s)
    result: dict = {
        "metric": "T4", "contract": chain["contract"], "contract_note": chain["contract_note"],
        "events": {}, "segments": {}, "passes": [], "violations": list(chain["violations"]),
        "notes": list(chain["notes"]), "not_measured": [],
        "session": chain["session"], "reps": chain["repeat"],
    }
    if chain["violations"]:
        result["not_measured"].append({
            "what": "T4 all chains",
            "reason": "a hook changed the code root or an operational root; numbers discarded"})
        return result
    events, segments = chain_events(chain)
    result["events"].update(events)
    result["segments"].update(segments)
    result["not_measured"].extend([
        {"what": "claude hook execution order (parallel vs sequential)",
         "reason": "no in-repo document or code decides it; both bounds are reported"},
        {"what": "codex hook execution order (parallel vs sequential)",
         "reason": "no in-repo document or code decides it; both bounds are reported"},
        {"what": "opencode stop/idle chain",
         "reason": "plan covers chat.message and tool.execute.before/after only"},
    ])
    if not include_opencode:
        result["not_measured"].append({"what": "opencode chain", "reason": "disabled by caller"})
        return result
    manifest_session = chain["session"]
    oc = run_opencode(mh, code_root, state_root, reps=reps, session=manifest_session,
                      fixture_env=fixture_env or {}, ops_roots=ops_roots,
                      markers=[marker, manifest_session], settle_s=settle_s)
    if oc.get("violation"):
        result["violations"].append(oc["violation"])
        result["events"] = {}
        result["segments"] = {}
        result["not_measured"].append({
            "what": "T4 all chains",
            "reason": "the OpenCode plugin changed the code root or an operational root; discarded"})
        return result
    if "payload" not in oc:
        result["not_measured"].append({
            "what": "opencode chain",
            "reason": oc.get("skipped") or oc.get("error") or f"node exit {oc.get('returncode')}: "
                      f"{oc.get('stderr_tail', '')[-200:]}"})
        return result
    oc_events, oc_segments = opencode_events(oc["payload"])
    result["events"].update(oc_events)
    result["segments"]["opencode"] = oc_segments
    if oc["denied"]:
        result["notes"].append("opencode python children were refused: " + ", ".join(oc["denied"][:5]))
    result["notes"].append(
        "opencode: callbacks are awaited one event at a time; spawnSync children block the "
        "callback (counted as sync_children), spawn children run beside it (detached_children)")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-root", required=True, help="disposable hook_perf_fixture base")
    parser.add_argument("--code-root", default=str(ROOT))
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--no-opencode", action="store_true")
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    result = measure({"base": args.state_root}, reps=args.reps, code_root=Path(args.code_root),
                     include_opencode=not args.no_opencode)
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if result["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
