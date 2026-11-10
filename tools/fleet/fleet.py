#!/usr/bin/env python3
"""fleet — cross-harness live agent dashboard (entry point).

Zero external deps (stdlib curses/sqlite3/json/subprocess/re/os/time only). Pure external
observer: reads process table + on-disk state artifacts and injects nothing (PRD §0.5).
Summary production belongs to dispatch or the interactive runtime lifecycle; starting,
refreshing, or closing this TUI never invokes a model provider. Git telemetry remains
bounded background work. Every mode — ``--once`` and ``--json`` included — is a
snapshot in what it displays: no task is started, settled, or marked, no model
provider is invoked, and no route, registry, or authority state changes. "Snapshot"
still allows one bounded write: collection may create or refresh the two-hex
session-tag display reservations in
``~/.local/state/agent-fleet/tag-assignments.json`` (core/OPERATIONS §5.11). Existing
live assignments keep their tag, the stable (harness, session_id) identity remains
the authority, and display tags never settle or advance work.

Modes:
  (default)  curses full-screen, background re-collect every --interval seconds
  --once     single snapshot; plain stdout when not a TTY / curses unavailable
  --json     collectors' result as JSON to stdout (pipe / debug / test)
"""
import argparse
import json
import os
import sys
from dataclasses import replace


def _cap_malloc_arenas(environ=os.environ, platform=sys.platform):
    """Keep glibc from giving every concurrent reader thread its own heap arena.

    Fleet starts a fresh worker thread for each pass and a burst of short pane-read
    threads. With one arena per concurrent thread, each large collector pass lands in
    a different arena and the memory it frees stays mapped there, so RSS climbs pass
    after pass (measured: 0.6GB to 1.9GB in 20 rewrites of a 45MB registry, flat at
    0.9GB with two arenas). Set once, before any thread exists. An operator's own
    MALLOC_ARENA_MAX wins; other platforms and C libraries are left alone.
    """
    if not platform.startswith("linux") or environ.get("MALLOC_ARENA_MAX"):
        return False
    try:
        import ctypes
        mallopt = getattr(ctypes.CDLL(None), "mallopt", None)
        return bool(mallopt is not None and mallopt(-8, 2))   # M_ARENA_MAX
    except (OSError, AttributeError, TypeError, ValueError):
        return False


_cap_malloc_arenas()

# Support both `python3 fleet.py` (script) and `python3 -m fleet.fleet` (module).
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fleet.collectors import collect_all
    from fleet.collectors import compute_hosts
    from fleet.collectors import procscan
    from fleet import installinfo
    from fleet import install_follow
    from fleet import screen_guard
    from fleet.refresh import as_snapshot
else:
    from .collectors import collect_all
    from .collectors import compute_hosts
    from .collectors import procscan
    from . import installinfo
    from . import install_follow
    from . import screen_guard
    from .refresh import as_snapshot


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="fleet",
        description=(
            "Cross-harness live agent-session, dispatch, and lab-resource dashboard. "
            "The `fleet` command selects Explorer; `fleet-monitor` selects the "
            "group/process screen. Direct fleet.py defaults to group/process."
        ),
    )
    p.add_argument("--interval", type=float, default=2.0,
                   help="live tick interval in seconds (default 2)")
    p.add_argument("--once", action="store_true",
                   help="render one snapshot of the selected screen then exit (plain text if not a TTY)")
    p.add_argument("--no-tmux", action="store_true",
                   help="run the TUI directly (honored by the shell launchers, not fleet.py)")
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
    p.add_argument("--explorer", action="store_true",
                   help="use Fleet Explorer (selected by the public `fleet` and compatibility `fleet2` launchers); "
                        "--view does not apply to it.")
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
        return memcol.collect(include_summary=False)
    except Exception:
        return None


def _snapshot_json(sessions, jobs, resource_jobs=None, usage=None, disabled=None, show_all=False,
                   hearting=None, compute_host_snapshot=None, observations=None,
                   resource_diagnostics=None):
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
        "resource_diagnostics": list(resource_diagnostics or []),
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
    if observations is not None:
        out["observations"] = observations
    mem = _collect_memory()
    if mem is not None:
        out["memory"] = mem
    peer = getattr(collect_all, "last_peer_messages", None)
    if isinstance(peer, dict) and peer.get("records"):
        # Collector identity is a tuple internally; JSON keeps the same projection
        # readable without collapsing cross-harness equal session ids.
        out["peer_messages"] = dict(peer)
        out["peer_messages"]["by_session"] = {
            "%s:%s" % key: row for key, row in (peer.get("by_session") or {}).items()
        }
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
        # F-104: live GPU processes no registered run owns; session_owner tells
        # a consumer which of them a session line also shows.
        out["unregistered_gpu"] = compute_hosts.unregistered_gpu(
            compute_host_snapshot, resource_jobs)
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


def _arm_stall_dump():
    """`kill -USR1 <fleet pid>` writes every thread's stack to a file.

    When the TUI froze on 2026-09-09 there was no way to see where: this host sets
    `ptrace_scope=1`, so strace and gdb cannot attach to a process that is not their own
    child, and fleet had no way to answer for itself. Two freezes were diagnosed by
    elimination instead of by reading the stack. This closes that gap for the next one.

    The dump goes to a file rather than stderr because stderr is the curses screen.
    Best-effort: a platform without SIGUSR1 simply gets no dump.
    """
    try:
        import faulthandler
        import signal
        import tempfile
    except ImportError:
        return
    if not hasattr(signal, "SIGUSR1"):
        return
    path = os.path.join(tempfile.gettempdir(), "fleet-stall-%d.txt" % os.getpid())
    try:
        # Held open for the process lifetime: the handler must not allocate or open
        # anything while the process is wedged.
        handle = open(path, "w", buffering=1)
    except OSError:
        return
    try:
        faulthandler.register(signal.SIGUSR1, file=handle, all_threads=True, chain=False)
    except (OSError, RuntimeError, ValueError):
        handle.close()
        _discard_empty(path)
        return

    def _on_exit():
        # A dump is evidence and stays. A file nobody dumped into is only the
        # arming: left behind, every fleet run adds one more to the temp dir.
        _discard_empty(path)

    import atexit
    atexit.register(_on_exit)


def _discard_empty(path):
    try:
        if os.path.getsize(path) == 0:
            os.unlink(path)
    except OSError:
        pass


def _run_live(render, collector, hfilter, section, interval, *, follower=None, guard=None):
    """The live view on a guarded screen: a terminal that stops reading pauses the
    picture, not collection or install follow, and a new release waits for output."""
    follower = install_follow.InstallFollower() if follower is None else follower
    guard = screen_guard.ScreenGuard() if guard is None else guard
    follower.hold = guard.hold_reason
    if guard.note is None:
        guard.note = follower.waiting_note
    collector.install_follower = follower
    with guard.around(render):
        return render.run_live(collector, hfilter, section, interval)


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    args = parse_args(argv)
    viewer_state = install_follow.read_handoff()
    _arm_stall_dump()
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

        def collector(harness_filter=None, usage="cache-only", fast_first=False,
                      include_report_verification=True):      # LIVE real data + injected demo fixtures (merged)
            report_option = ({"include_report_verification": False}
                             if not include_report_verification else {})
            observed = as_snapshot(collect_all(harness_filter=harness_filter, usage="cache-only",
                                               **report_option,
                                               **({"fast_first": True} if fast_first else {})))
            rs, rj = observed
            ds, dj = demo.collect(harness_filter=harness_filter)
            # Real rows are projected by collect_all. Only the injected rows
            # still need projection; never reread the live routes/artifact tree.
            if __package__ in (None, ""):
                from fleet.projection import attach_projections
            else:
                from .projection import attach_projections
            ds, dj = attach_projections(ds, dj, **report_option)
            answer_waits = demo.merge_answer_waits(
                observed.answer_waits,
                demo.answer_wait_snapshot(harness_filter=harness_filter))
            return replace(observed, sessions=rs + ds, jobs=rj + dj,
                           answer_waits=answer_waits)

    def projected_collector(harness_filter=None, usage="cache-only", fast_first=False,
                            include_report_verification=True):
        fast_kw = {"fast_first": True} if fast_first else {}
        report_option = ({"include_report_verification": False}
                         if not include_report_verification else {})
        if usage == "cache-only":
            observed = as_snapshot(collector(harness_filter=harness_filter,
                                             **report_option, **fast_kw))
        else:
            observed = as_snapshot(collector(harness_filter=harness_filter, usage=usage,
                                            **report_option, **fast_kw))
        projected_collector.last_resource_jobs = list(
            observed.resources)
        projected_collector.last_resource_diagnostics = list(observed.resource_diagnostics)
        projected_collector.last_usage_snapshots = dict(
            observed.usage_snapshots)
        return observed

    projected_collector.last_resource_jobs = []
    projected_collector.last_resource_diagnostics = []
    projected_collector.last_usage_snapshots = {}

    def close_herdr_observation():
        try:
            if __package__ in (None, ""):
                from fleet.collectors import herdr as herdr_collector
            else:
                from .collectors import herdr as herdr_collector
            herdr_collector.close_observations()
        except Exception:
            pass

    projected_collector.close = close_herdr_observation

    if args.json:
        observed = projected_collector(harness_filter=hfilter)
        sessions, jobs = observed
        compute_host_snapshot = compute_hosts.collect()
        usage_json = dict(getattr(collect_all, "last_usage", {}))
        snapshots = [value for value in usage_json.values() if isinstance(value, dict)]
        freshnesses = [value.get("freshness") for value in snapshots]
        usage_json["freshness"] = ("fresh" if "fresh" in freshnesses else
                                    "stale" if "stale" in freshnesses else "unknown")
        usage_observed = [value.get("observed_at") for value in snapshots
                          if isinstance(value.get("observed_at"), (int, float))]
        usage_json["observed_at"] = max(usage_observed) if usage_observed else None
        usage_json["api_disabled"] = disabled["api_disabled"]
        print(_snapshot_json(sessions, jobs, observations=observed.observations,
                             resource_jobs=projected_collector.last_resource_jobs,
                             usage=usage_json,
                             disabled=disabled,
                             show_all=args.show_all,
                             hearting=hearting,
                             compute_host_snapshot=compute_host_snapshot,
                             resource_diagnostics=observed.resource_diagnostics))
        projected_collector.close()
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
    # Public `fleet` and compatibility `fleet2` select Explorer; `fleet-monitor`
    # and direct fleet.py retain the group/process default.
    render.set_explorer_mode(args.explorer)
    # F-30 (v10, plan §P3/§9): --view is additive and honors the SAME single _PROCESS_VIEW
    # state the `p` key flips — never a second decision path. FLEET_VIEW env is the reduction
    # fallback the plan reserves in case --view itself is judged out of scope later (§9 note 1);
    # both stay best-effort no-ops when unset (default = the pre-v10 group view).
    view = args.view or os.environ.get("FLEET_VIEW")
    if view and not args.explorer:
        render.set_process_view(view == "process")
    if args.once:
        try:
            return render.render_once(projected_collector, hfilter, args.section,
                                      compute_hosts_refresh=compute_hosts.collect)
        finally:
            projected_collector.close()
            render.set_explorer_mode(False)
    render.reset_scroll()   # fresh launch starts scrolled to top (belt-and-suspenders)
    if viewer_state is not None:
        render.restore_viewer_state(viewer_state)

    base_collector = projected_collector
    previous_sessions = []

    def live_collector(harness_filter=None, fast_first=False, include_report_verification=True):
        nonlocal previous_sessions
        effective = set(harness_filter) if harness_filter is not None else {"claude", "codex", "opencode"}
        live = set()
        if not disabled["api_disabled"] and args.section in ("fleet", "both"):
            live = render.live_harnesses(previous_sessions) & effective & {"claude", "codex"}
        usage = "refresh" if live else "cache-only"
        collector_options = {"harness_filter": harness_filter, "usage": usage,
                             **({"fast_first": True} if fast_first else {})}
        if not include_report_verification or args.explorer:
            collector_options["include_report_verification"] = False
        observed = base_collector(**collector_options)
        sessions, jobs = observed
        live_collector.last_resource_jobs = list(
            getattr(base_collector, "last_resource_jobs", []))
        live_collector.last_resource_diagnostics = list(observed.resource_diagnostics)
        live_collector.last_usage_snapshots = dict(
            getattr(base_collector, "last_usage_snapshots", {}))
        previous_sessions = list(sessions)
        return observed

    live_collector.last_resource_jobs = []
    live_collector.last_resource_diagnostics = []
    live_collector.last_usage_snapshots = {}
    live_collector.close = projected_collector.close
    # Live-only metadata refresh. render's snapshot pump invokes this off the curses
    # thread; --once/--json never opt into remote release discovery.
    # Passive detail collection reuses exact rows; never performs a second
    # procscan/classifier pass or delays the basic observation pump.
    if __package__ in (None, ""):
        from fleet import details
    else:
        from . import details
    live_collector.detail_refresh = lambda source: details.enrich(
        source, include_report_verification=not args.explorer)
    if args.explorer:
        live_collector.required_detail_refresh = lambda source: details.enrich(
            source, include_report_verification=False, required_only=True)
    live_collector.hearting_refresh = lambda: installinfo.collect(
        refresh_remote=True, fast_local=True)
    # F-83: SSH/GPU polling has its own slower pump in render. Never put it in
    # the 2-second process snapshot producer or on the curses thread.
    live_collector.compute_hosts_refresh = compute_hosts.collect
    # The real collector names its watchable inputs, so the live view can stay
    # quiet between their changes (FLEET_IDLE_REFRESH_INTERVAL=0 keeps periodic).
    if __package__ in (None, ""):
        from fleet.native_events import basic_event_sources
    else:
        from .native_events import basic_event_sources
    live_collector.basic_event_sources = basic_event_sources
    live_collector.restart_argv = argv

    try:
        return _run_live(render, live_collector, hfilter, args.section, args.interval)
    finally:
        render.set_explorer_mode(False)


if __name__ == "__main__":
    sys.exit(main())
