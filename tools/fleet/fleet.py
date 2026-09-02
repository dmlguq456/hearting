#!/usr/bin/env python3
"""fleet — cross-harness live agent dashboard (entry point).

Zero external deps (stdlib curses/sqlite3/json/subprocess/re/os/time only). Pure external
observer: reads process table + on-disk state artifacts and injects nothing (PRD §0.5).
Summary production belongs to dispatch or the interactive runtime lifecycle; starting,
refreshing, or closing this TUI never invokes a model provider. Git telemetry remains
bounded background work. ``--json`` and ``--once`` are side-effect-free snapshots.

Modes:
  (default)  curses full-screen, background re-collect every --interval seconds
  --once     single snapshot; plain stdout when not a TTY / curses unavailable
  --json     collectors' result as JSON to stdout (pipe / debug / test)
"""
import argparse
import json
import os
import sys

# Support both `python3 fleet.py` (script) and `python3 -m fleet.fleet` (module).
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fleet.collectors import collect_all
    from fleet.collectors import compute_hosts
    from fleet.collectors import procscan
    from fleet import installinfo
else:
    from .collectors import collect_all
    from .collectors import compute_hosts
    from .collectors import procscan
    from . import installinfo


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="fleet",
        description="Cross-harness live agent-session, dispatch, and lab-resource dashboard.",
    )
    p.add_argument("--interval", type=float, default=2.0,
                   help="live tick interval in seconds (default 2)")
    p.add_argument("--once", action="store_true",
                   help="render one snapshot then exit (plain text if not a TTY)")
    p.add_argument("--no-tmux", action="store_true",
                   help="run the TUI directly (this flag is honored by fleet.sh, not fleet.py)")
    p.add_argument("--section", choices=["fleet", "dispatch", "both"], default="both",
                   help="row-type filter within each project group: fleet=session rows only, "
                        "dispatch=job rows (dispatch + resource), both=full group (default both)")
    p.add_argument("--harness", default=None,
                   help="comma list to restrict harnesses, e.g. claude,codex")
    p.add_argument("--json", action="store_true",
                   help="emit collected state as JSON to stdout")
    p.add_argument("--no-usage-api", action="store_true",
                   help="disable live usage API refreshes")
    p.add_argument("--title-provider", choices=["opencode", "claude", "codex"], default=None,
                   help="pin the harness that runs the title/summary worker; default walks "
                        "the cascade (opencode, then claude, then codex) and takes the first "
                        "one installed. The model always comes from that adapter's mini tier.")
    p.add_argument("--all", dest="show_all", action="store_true",
                   help="include stale/dead sessions and exited/stale resources (hidden by default)")
    p.add_argument("--demo", action="store_true",
                   help="render synthetic fixture data (all harnesses + states) for rendering checks")
    p.add_argument("--view", choices=["group", "process"], default=None,
                   help="F-30 (v10): initial view — group (default, per-project) or process "
                        "(per-route pipeline cards). Additive; the live TUI's `p` key toggles "
                        "the same state either way (plan §P3 — this is --once's ONLY entry "
                        "point into the process view, since `p` is a curses-live-only key).")
    return p.parse_args(argv)   # argparse exits 2 on bad args (matches PRD §3 exit codes)


def _harness_filter(spec):
    if not spec:
        return None
    hs = set(h.strip() for h in spec.split(",") if h.strip())
    unknown = hs - set(procscan.HARNESSES)
    if unknown:
        sys.stderr.write("warning: unknown harness(es) ignored: %s\n" % ", ".join(sorted(unknown)))
    hs &= set(procscan.HARNESSES)
    return hs or None


def _disabled_tokens(value=None):
    raw = os.environ.get("FLEET_DISABLE", "") if value is None else value
    tokens = sorted(set(token.strip().lower() for token in str(raw).split(",") if token.strip()))
    recognized = sorted(token for token in tokens if token == "usage-api")
    return {"recognized": recognized,
            "ignored": sorted(token for token in tokens if token not in recognized),
            "api_disabled": "usage-api" in recognized}


def _collect_memory():
    # F-19: additive, best-effort — a collector import/read failure must never break --json.
    try:
        if __package__ in (None, ""):
            from fleet.collectors import memory as memcol
        else:
            from .collectors import memory as memcol
        return memcol.collect()
    except Exception:
        return None


def _snapshot_json(sessions, jobs, resource_jobs=None, usage=None, disabled=None, show_all=False,
                   hearting=None, compute_host_snapshot=None):
    resource_jobs = list(resource_jobs or [])
    visible_resources = resource_jobs if show_all else [
        row for row in resource_jobs if row.liveness == "working"
    ]
    counts = {}
    for s in sessions:
        counts[s.harness] = counts.get(s.harness, 0) + 1
    out = {
        "sessions": [s.to_dict() for s in sessions],
        "jobs": [j.to_dict() for j in jobs],
        "resource_jobs": [j.to_dict() for j in visible_resources],
        "summary": {
            "session_count": len(sessions),
            "by_harness": counts,
            "dispatch_count": len(jobs),
            "resource_count": len(visible_resources),
            "resource_working": sum(j.liveness == "working" for j in resource_jobs),
            "resource_stale": sum(j.liveness == "stale" for j in resource_jobs),
            "resource_exited": sum(j.liveness == "exited" for j in resource_jobs),
        },
    }
    mem = _collect_memory()
    if mem is not None:
        out["memory"] = mem
    peer = getattr(collect_all, "last_peer_messages", None)
    if isinstance(peer, dict) and peer.get("records"):
        out["peer_messages"] = peer      # summary(<=200) only; body never present
    out["route"] = _collect_route(list(sessions) + list(jobs))
    gov = _collect_governor()
    if gov is not None:
        out["governor"] = gov
    if usage is not None:
        out["usage"] = usage
    if disabled is not None:
        out["disabled"] = disabled
    if hearting is not None:
        out["hearting"] = dict(hearting)
    if compute_host_snapshot is not None:
        out["compute_hosts"] = compute_host_snapshot
    return json.dumps(out, ensure_ascii=False, indent=2)


def _collect_governor():
    """F-28c (prd.md:288/311) — best-effort, additive `governor` key. `None` (source absent) =
    key omitted entirely, same convention `memory` already uses just above."""
    try:
        if __package__ in (None, ""):
            from fleet.collectors import governor
        else:
            from .collectors import governor
        return governor.collect()
    except Exception:
        return None


def _collect_route(entities):
    """F-28a (prd.md:302) — best-effort, additive `route` key. `route.py` itself never raises,
    but this stays wrapped (the `mem` precedent just above) so a future regression there can
    never break `--json` (§3.4)."""
    try:
        if __package__ in (None, ""):
            from fleet.projection import route_summary_from_projections
        else:
            from .projection import route_summary_from_projections
        return route_summary_from_projections(entities)
    except Exception:
        return []


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    hfilter = _harness_filter(args.harness)
    hearting = (installinfo.collect() if args.json or args.once
                else installinfo.collect(fast_local=True))
    disabled = _disabled_tokens()
    disabled["api_disabled"] = bool(disabled["api_disabled"] or args.no_usage_api)
    if args.title_provider:
        # The refresher runs as a detached subprocess, so the pin travels by environment
        # rather than argv — one place to set it, and every worker this run spawns
        # inherits it. An already-set env value means the operator chose it too; the
        # explicit flag is the more specific instruction and wins.
        os.environ["FLEET_TITLE_PROVIDER"] = args.title_provider

    collector = collect_all
    if args.demo or os.environ.get("FLEET_DEMO"):   # flag OR env (env works through any launcher/alias)
        os.environ["FLEET_DEMO"] = "1"
        if __package__ in (None, ""):
            from fleet import demo
        else:
            from . import demo

        def collector(harness_filter=None, usage="cache-only"):      # LIVE real data + injected demo fixtures (merged)
            rs, rj = collect_all(harness_filter=harness_filter, usage="cache-only")
            ds, dj = demo.collect(harness_filter=harness_filter)
            return rs + ds, rj + dj

    def projected_collector(harness_filter=None, usage="cache-only"):
        if usage == "cache-only":
            sessions, jobs = collector(harness_filter=harness_filter)
        else:
            sessions, jobs = collector(harness_filter=harness_filter, usage=usage)
        try:
            if __package__ in (None, ""):
                from fleet.projection import attach_projections
                from fleet.collectors import dispatch as _dispatch
            else:
                from .projection import attach_projections
                from .collectors import dispatch as _dispatch
            # F-28a terminal node evidence (dispatch.py's _scan_route_nodes) must survive
            # into the projection or a node whose live job already went terminal silently
            # regresses to "pending" (see projection.py's resolve_work_projection).
            node_evidence = getattr(_dispatch.collect, "last_route_nodes", None)
            degradations = getattr(_dispatch.collect, "last_degradations", None)
            result = attach_projections(sessions, jobs, artifact_root=os.environ.get("AGENT_ARTIFACT_ROOT"),
                                        node_evidence=node_evidence, degradations=degradations)
        except Exception:
            result = (sessions, jobs)
        projected_collector.last_resource_jobs = list(
            getattr(collect_all, "last_resource_jobs", []))
        projected_collector.last_resource_malformed = getattr(
            collect_all, "last_resource_malformed", 0)
        projected_collector.last_usage_snapshots = dict(
            getattr(collect_all, "last_usage_snapshots", {}))
        return result

    projected_collector.last_resource_jobs = []
    projected_collector.last_resource_malformed = 0
    projected_collector.last_usage_snapshots = {}

    if args.json:
        sessions, jobs = projected_collector(harness_filter=hfilter)
        compute_host_snapshot = compute_hosts.collect()
        usage_json = dict(getattr(collect_all, "last_usage", {}))
        snapshots = [value for value in usage_json.values() if isinstance(value, dict)]
        freshnesses = [value.get("freshness") for value in snapshots]
        usage_json["freshness"] = ("fresh" if "fresh" in freshnesses else
                                    "stale" if "stale" in freshnesses else "unknown")
        observed = [value.get("observed_at") for value in snapshots
                    if isinstance(value.get("observed_at"), (int, float))]
        usage_json["observed_at"] = max(observed) if observed else None
        usage_json["api_disabled"] = disabled["api_disabled"]
        print(_snapshot_json(sessions, jobs,
                             resource_jobs=projected_collector.last_resource_jobs,
                             usage=usage_json,
                             disabled=disabled,
                             show_all=args.show_all,
                             hearting=hearting,
                             compute_host_snapshot=compute_host_snapshot))
        return 0

    # curses / --once path (render module) — resolved lazily so --json needs no curses.
    try:
        if __package__ in (None, ""):
            from fleet import render
        else:
            from . import render
    except Exception as e:  # pragma: no cover
        sys.stderr.write("render init failed: %s\n" % e)
        return 1

    render.set_show_all(args.show_all)
    render.set_api_disabled(disabled["api_disabled"])
    render.set_hearting(hearting)
    # F-30 (v10, plan §P3/§9): --view is additive and honors the SAME single _PROCESS_VIEW
    # state the `p` key flips — never a second decision path. FLEET_VIEW env is the reduction
    # fallback the plan reserves in case --view itself is judged out of scope later (§9 note 1);
    # both stay best-effort no-ops when unset (default = the pre-v10 group view).
    view = args.view or os.environ.get("FLEET_VIEW")
    if view:
        render.set_process_view(view == "process")
    if args.once:
        render.set_compute_hosts(compute_hosts.collect())
        return render.render_once(projected_collector, hfilter, args.section)
    render.reset_scroll()   # fresh launch starts scrolled to top (belt-and-suspenders)

    base_collector = projected_collector
    previous_sessions = []

    def live_collector(harness_filter=None):
        nonlocal previous_sessions
        effective = set(harness_filter) if harness_filter is not None else {"claude", "codex", "opencode"}
        live = set()
        if not disabled["api_disabled"] and args.section in ("fleet", "both"):
            live = render.live_harnesses(previous_sessions) & effective & {"claude", "codex"}
        usage = "refresh" if live else "cache-only"
        sessions, jobs = base_collector(harness_filter=harness_filter, usage=usage)
        live_collector.last_resource_jobs = list(
            getattr(base_collector, "last_resource_jobs", []))
        live_collector.last_resource_malformed = getattr(
            base_collector, "last_resource_malformed", 0)
        live_collector.last_usage_snapshots = dict(
            getattr(base_collector, "last_usage_snapshots", {}))
        previous_sessions = list(sessions)
        return sessions, jobs

    live_collector.last_resource_jobs = []
    live_collector.last_resource_malformed = 0
    live_collector.last_usage_snapshots = {}
    # Live-only metadata refresh. render's snapshot pump invokes this off the curses
    # thread; --once/--json never opt into remote release discovery.
    live_collector.hearting_refresh = lambda: installinfo.collect(
        refresh_remote=True, fast_local=True)
    # F-83: SSH/GPU polling has its own slower pump in render. Never put it in
    # the 2-second process snapshot producer or on the curses thread.
    live_collector.compute_hosts_refresh = compute_hosts.collect

    return render.run_live(live_collector, hfilter, args.section, args.interval)


if __name__ == "__main__":
    sys.exit(main())
