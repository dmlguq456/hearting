#!/usr/bin/env python3
"""Runtime-neutral ``harness`` installer CLI.

Subcommand tree:
  install [claude|codex|opencode|all]
  verify  [runtime]
  update  [--reapply]
  status
  uninstall [runtime]

The installer PRD is the source of truth. This module owns command parsing,
exit codes, and JSON output while helper modules own projection and drift logic.
It depends only on the Python standard library.
"""
import sys
import os
import json
import argparse
import shutil
import subprocess
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import paths
import projector
import manifest
import verifier
import bootstrap
import runtime_activation
import extensions
import distribution
import codex_launcher
import routing_config
import report_bundle_config
import memory_sync_config
import compute_hosts_config
import user_config
import host_probes
import node_runtime
import safe_fs
from drivers import get_driver, RUNTIMES

# Exit codes map one-to-one to the PRD CLI table.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_VERIFY_FAIL = 2
EXIT_BLOCKED = 3
EXIT_DRIFT = 4
EXIT_USAGE = 64


class _UsageExitParser(argparse.ArgumentParser):
    """Use the PRD usage exit code 64 instead of argparse's default 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser():
    p = _UsageExitParser(
        prog="harness",
        description=(
            "hearting installer — immutable local snapshots by default; "
            "linked checkouts are an explicit maintainer debug mode."
        ),
    )
    # Common options inherited by all subcommands.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--runtime", action="append", choices=RUNTIMES, dest="runtimes",
        help="Target runtime (repeatable); defaults to the positional target or all runtimes.",
    )
    common.add_argument("--scope", choices=["global", "project"], default="global")
    common.add_argument("--dry-run", action="store_true", help="Print the plan without applying it")
    common.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    common.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    common.add_argument("--plugin", action="store_true", help="Use the plugin-channel marketplace wrapper")

    sub = p.add_subparsers(dest="command", required=True, parser_class=_UsageExitParser)

    p_install = sub.add_parser("install", parents=[common], help="Install projections, runtime-owned surfaces, and manifests")
    p_install.add_argument("target", nargs="?", choices=[*RUNTIMES, "all"], default="all")
    p_install.add_argument(
        "--report-bundle-root",
        help="Absolute report bundle store; initialized once and then user-owned",
    )

    p_verify = sub.add_parser("verify", parents=[common], help="Run the automated Migration Order checks")
    p_verify.add_argument("target", nargs="?", choices=RUNTIMES, default=None)

    p_update = sub.add_parser(
        "update",
        parents=[common],
        help="Update a managed release or inspect/reapply local projection drift",
    )
    p_update.add_argument("--reapply", action="store_true", help="Reapply local patches to new files")
    p_update.add_argument(
        "--version",
        default=None,
        help="Managed release tag, or latest to leave a pin",
    )
    p_update.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)
    p_update.add_argument(
        "--force-prune-unproven",
        action="store_true",
        help="Delete superseded releases whose dispatch-state migration could not be "
        "proven, after committing a non-recoverable gap record for each (SD-115)",
    )

    sub.add_parser("status", parents=[common], help="Summarize installation channels, versions, and drift")

    p_uninstall = sub.add_parser("uninstall", parents=[common], help="Remove only manifest-owned files")
    p_uninstall.add_argument("target", nargs="?", choices=[*RUNTIMES, "all"], default=None)

    # Memory exchange policy: one portable file every adapter reads, plus the
    # one-shot join that turns a fresh store into a member of that exchange.
    p_memory = sub.add_parser(
        "memory", help="Configure and join the shared memory exchange")
    memory_sub = p_memory.add_subparsers(
        dest="memory_command", required=True, parser_class=_UsageExitParser)
    p_memory_join = memory_sub.add_parser(
        "join", help="Record the exchange policy and join this store to it")
    p_memory_join.add_argument("--remote-url", required=True,
                               help="Git remote holding the protected ref")
    p_memory_join.add_argument(
        "--ref", default=memory_sync_config.DEFAULT_REF,
        help=f"Protected ref (default: {memory_sync_config.DEFAULT_REF})")
    p_memory_join.add_argument(
        "--exchange-dir",
        help="Private per-host exchange repository (default: XDG state home)")
    p_memory_join.add_argument("--dry-run", action="store_true",
                               help="Show the policy without writing or joining")
    p_memory_join.add_argument("--json", action="store_true")
    p_memory_status = memory_sub.add_parser(
        "status", help="Show the recorded exchange policy")
    p_memory_status.add_argument("--json", action="store_true")

    # User-owned configuration surfaces: one read-only status view over the
    # registry that install seeds and verify checks.
    p_config = sub.add_parser(
        "config", help="Show every user-owned config file, its state, and how to set it")
    config_sub = p_config.add_subparsers(
        dest="config_command", required=True, parser_class=_UsageExitParser)
    p_config_status = config_sub.add_parser(
        "status", help="List config surfaces with path, state, and next step")
    p_config_status.add_argument("--verbose", action="store_true",
                                 help="Show seeding and absence notes for every surface")
    p_config_status.add_argument("--json", action="store_true")

    # Source → active runtime truth.  These parsers intentionally do not inherit
    # the legacy install channel's --plugin option: linked/packaged are mutually
    # exclusive and both are fully local/offline.
    p_runtime = sub.add_parser("runtime", help="Manage the active source, revision, and projection")
    runtime_sub = p_runtime.add_subparsers(
        dest="runtime_command", required=True, parser_class=_UsageExitParser
    )

    def runtime_common(parser, *, require_runtime=False):
        parser.add_argument(
            "--runtime",
            action="append" if require_runtime else "store",
            choices=[*RUNTIMES, "all"],
            required=require_runtime,
            default=None if require_runtime else "all",
        )
        parser.add_argument("--scope", choices=["global", "project"], default="global")
        parser.add_argument("--json", action="store_true")

    p_runtime_status = runtime_sub.add_parser("status", help="Show the active source and freshness")
    runtime_common(p_runtime_status)

    p_runtime_activate = runtime_sub.add_parser(
        "activate",
        help="Activate an immutable packaged snapshot (default) or explicit linked debug source",
    )
    runtime_common(p_runtime_activate, require_runtime=True)
    p_runtime_activate.add_argument(
        "--mode",
        choices=runtime_activation.MODES,
        default="packaged",
        help="packaged (default, session-stable) or linked (explicit live/debug projection)",
    )
    p_runtime_activate.add_argument("--source", help="local canonical repo (default: AGENT_HOME)")
    p_runtime_activate.add_argument(
        "--report-bundle-root",
        help="Absolute report bundle store; initialized once and then user-owned",
    )

    p_runtime_refresh = runtime_sub.add_parser("refresh", help="Refresh the current mode from its local source")
    runtime_common(p_runtime_refresh, require_runtime=True)

    p_runtime_doctor = runtime_sub.add_parser("doctor", help="Diagnose projections, duplicates, and freshness")
    runtime_common(p_runtime_doctor)
    p_runtime_doctor.add_argument("--strict", action="store_true")

    p_extension = sub.add_parser(
        "extension", help="offline instruction-only external extension lifecycle"
    )
    extension_sub = p_extension.add_subparsers(
        dest="extension_command", required=True, parser_class=_UsageExitParser
    )
    p_extension_inspect = extension_sub.add_parser(
        "inspect", help="inspect a local extension source without mutation"
    )
    p_extension_inspect.add_argument("source")
    p_extension_inspect.add_argument("--json", action="store_true")

    p_extension_add = extension_sub.add_parser(
        "add", help="inspect, snapshot, and project a local extension"
    )
    p_extension_add.add_argument("source")
    p_extension_add.add_argument(
        "--runtime",
        action="append",
        choices=[*RUNTIMES, "all"],
        dest="extension_runtimes",
    )
    p_extension_add.add_argument("--json", action="store_true")

    p_extension_update = extension_sub.add_parser(
        "update", help="refresh an installed extension from a local source"
    )
    p_extension_update.add_argument("canonical_id")
    p_extension_update.add_argument("--source")
    p_extension_update.add_argument("--json", action="store_true")

    p_extension_remove = extension_sub.add_parser(
        "remove", help="remove only registry-owned extension projections"
    )
    p_extension_remove.add_argument("canonical_id")
    p_extension_remove.add_argument("--json", action="store_true")

    p_auto_update = sub.add_parser(
        "auto-update", help="Manage the managed-release user scheduler"
    )
    p_auto_update.add_argument("operation", choices=["status", "enable", "disable"])
    p_auto_update.add_argument("--json", action="store_true")

    return p


def resolve_runtimes(args):
    """Combine a positional target and repeated ``--runtime`` options."""
    if args.runtimes:
        return list(args.runtimes)
    target = getattr(args, "target", None)
    if target in (None, "all"):
        return list(RUNTIMES)
    return [target]


def emit(result, as_json):
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for line in result.get("lines", []):
            print(line)
    return result["exit"]


def cmd_install(args):
    runtimes = resolve_runtimes(args)
    lines = [f"install: runtime={r} scope={args.scope} plugin={args.plugin} dry_run={args.dry_run}" for r in runtimes]
    checks = []
    results = []
    bootstrap_failed = False

    for rt in runtimes:
        driver = get_driver(rt)
        result = driver.install(scope=args.scope, plugin=args.plugin, dry_run=args.dry_run)
        results.append(result)

        for a in result["actions"]:
            dest = a.get("dest", "")
            detail = a.get("detail")
            line = f"{rt}: {a['action']} {dest} -> {a['status']}"
            if detail and a["status"] not in ("created", "unchanged"):
                line += f" ({detail})"
            lines.append(line)

            check_name = Path(dest).name if dest else "x"
            checks.append(
                {
                    "id": f"{rt}.{a['action']}.{check_name}",
                    "ok": a["status"] != "blocked",
                    "detail": detail if detail else a["status"],
                }
            )

    any_blocked = any(result["blocked"] for result in results)

    if not any_blocked:
        routing = routing_config.ensure(runtimes, dry_run=args.dry_run)
        lines.append(
            "routing-config: " + routing["status"] + " " + routing["path"]
            + " enabled=" + ",".join(routing["enabled"])
        )
        checks.append(
            {
                "id": "routing-config.user-policy",
                "ok": True,
                "detail": routing["status"],
            }
        )
        # A preserved file is never rewritten (DP-23), so install is the one
        # moment to say out loud when it has drifted from the shipped policy.
        if routing["status"] == "preserved":
            routing_state = routing_config.validate()
            if routing_state.get("status") == "drift":
                lines.append("routing-config: drift — " + routing_state["detail"])
        try:
            bundle_config = report_bundle_config.ensure(
                args.report_bundle_root, dry_run=args.dry_run
            )
        except ValueError as exc:
            any_blocked = True
            lines.append(f"report-bundle-config: blocked ({exc})")
            checks.append({"id": "report-bundle-config.root", "ok": False, "detail": str(exc)})
        else:
            lines.append(
                "report-bundle-config: " + bundle_config["status"] + " "
                + bundle_config["path"] + " root=" + bundle_config["root"]
            )
            checks.append({
                "id": "report-bundle-config.root",
                "ok": True,
                "detail": bundle_config["status"],
            })
        inventory = compute_hosts_config.ensure(dry_run=args.dry_run)
        lines.append("compute-hosts-config: " + inventory["status"] + " " + inventory["path"])
        checks.append({
            "id": "compute-hosts-config.inventory",
            "ok": True,
            "detail": inventory["status"],
        })
        if args.dry_run:
            launcher_results = bootstrap.install_launchers(dry_run=True)
            lines.append("bootstrap: mem-import skipped (dry-run — no dry-run mode for restore_memory)")
            for lr in launcher_results:
                lines.append(f"bootstrap: launcher {lr['name']} -> {lr['status']} (dry-run)")
                checks.append(
                    {
                        "id": f"bootstrap.launcher.{lr['name']}",
                        "ok": lr["status"] != "skipped-collision",
                        "detail": lr.get("detail", lr["status"]),
                    }
                )
            bootstrap_failed = any(
                lr["status"] == "skipped-collision" for lr in launcher_results
            )
        else:
            mem_result = bootstrap.restore_memory()
            lines.append(f"bootstrap: mem-import -> {mem_result['action']} ({mem_result['detail']})")
            checks.append(
                {
                    "id": "bootstrap.mem-import",
                    "ok": mem_result["action"] != "failed",
                    "detail": mem_result["detail"],
                }
            )
            bootstrap_failed = mem_result["action"] == "failed"

            launcher_results = bootstrap.install_launchers(dry_run=False)
            for lr in launcher_results:
                lines.append(f"bootstrap: launcher {lr['name']} -> {lr['status']}")
                checks.append(
                    {
                        "id": f"bootstrap.launcher.{lr['name']}",
                        "ok": lr["status"] != "skipped-collision",
                        "detail": lr.get("detail", lr["status"]),
                    }
                )
            bootstrap_failed = bootstrap_failed or any(
                lr["status"] == "skipped-collision" for lr in launcher_results
            )

    environment = [node_runtime.ensure_node()]
    environment.extend(host_probes.run())
    for probe in environment:
        lines.append(f"environment: {probe['id']} -> {probe['status']} ({probe['detail']})")

    user_config_rows = [] if args.dry_run else user_config.status()
    if user_config_rows:
        lines.append("user config (edit any time; `harness config status` repeats this):")
        lines.extend("  " + line for line in user_config.lines(user_config_rows))

    exit_code = EXIT_BLOCKED if any_blocked else EXIT_FAIL if bootstrap_failed else EXIT_OK
    return {"runtime": runtimes, "channel": "plugin" if args.plugin else "dev", "checks": checks,
            "drift": [], "exit": exit_code, "lines": lines, "environment": environment,
            "user_config": user_config_rows}


def cmd_verify(args):
    runtimes = resolve_runtimes(args)
    all_checks = []
    ok = True
    for rt in runtimes:
        activation_state = paths.harness_state_dir(rt, args.scope) / "activation.json"
        if activation_state.exists() or activation_state.is_symlink():
            try:
                report = runtime_activation.doctor(rt, strict=True, scope=args.scope)
                status = report["status"]
                rt_checks = [
                    {
                        "id": f"{rt}.runtime-activation",
                        "ok": report["ok"],
                        "detail": (
                            f"freshness={report['freshness']} "
                            f"next={report['next_action']}"
                        ),
                    }
                ]
            except (runtime_activation.ActivationError, OSError, ValueError) as exc:
                rt_checks = [
                    {
                        "id": f"{rt}.runtime-activation",
                        "ok": False,
                        "detail": f"activation state invalid: {exc}",
                    }
                ]
        else:
            driver = get_driver(rt)
            rt_checks = verifier.run(rt, driver)
        all_checks.extend(rt_checks)
        if any(not c["ok"] for c in rt_checks):
            ok = False
    routing = routing_config.validate()
    all_checks.append({
        "id": "routing-config.user-policy",
        "ok": routing["ok"],
        "detail": routing["status"] + ": " + routing["path"] + (
            " — " + routing["detail"] if routing["status"] == "drift" else ""
        ),
    })
    if not routing["ok"]:
        ok = False
    bundle_config = report_bundle_config.validate()
    all_checks.append({
        "id": "report-bundle-config.root",
        "ok": bundle_config["ok"],
        "detail": bundle_config["status"] + ": " + bundle_config["path"],
    })
    if not bundle_config["ok"]:
        ok = False
    for surface_id, check_id in (("compute-hosts", "compute-hosts-config.inventory"),
                                 ("memory-sync", "memory-sync-config.policy")):
        row = user_config.status([surface_id])[0]
        all_checks.append({
            "id": check_id,
            "ok": row["ok"],
            "detail": row["reading"] + ": " + row["path"],
        })
        if not row["ok"]:
            ok = False
    launcher = bootstrap.compute_hosts_status()
    launcher_expected = bootstrap.compute_hosts_expected()
    launcher_skipped = launcher["status"] == "missing" and not launcher_expected
    launcher_ok = launcher["status"] == "healthy" or launcher_skipped
    launcher_detail = launcher["status"] + ": " + launcher["target"]
    if launcher_skipped:
        launcher_detail = (
            "SKIP: common PATH launchers were not installed by this "
            "runtime-activation-only channel"
        )
    all_checks.append({
        "id": "bootstrap.launcher.compute-hosts",
        "ok": launcher_ok,
        "detail": launcher_detail,
    })
    if launcher["status"] == "healthy":
        try:
            smoke = subprocess.run(
                [launcher["target"], "--help"],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "COMPUTE_HOSTS_CONFIG": str(Path(os.devnull))},
            )
            smoke_ok = smoke.returncode == 0
            smoke_detail = "forwarding smoke passed" if smoke_ok else (smoke.stderr or "smoke failed").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            smoke_ok, smoke_detail = False, str(exc)
        all_checks.append({"id": "bootstrap.launcher.compute-hosts-smoke", "ok": smoke_ok, "detail": smoke_detail})
        ok = ok and smoke_ok
    elif not launcher_skipped:
        ok = False
    lines = [("✓" if c["ok"] else "✗") + f" {c['id']} {c['detail']}" for c in all_checks]
    return {"runtime": runtimes, "channel": "plugin" if args.plugin else "dev", "checks": all_checks,
            "drift": [], "exit": EXIT_OK if ok else EXIT_VERIFY_FAIL, "lines": lines}


def cmd_update(args):
    try:
        managed = distribution.is_managed()
    except distribution.DistributionError as exc:
        return {
            "runtime": args.runtimes or [],
            "channel": "managed-release",
            "checks": [{"id": "update.state", "ok": False, "detail": str(exc)}],
            "drift": [],
            "exit": EXIT_FAIL,
            "lines": [f"update: invalid managed release state: {exc}"],
        }
    if managed:
        incompatible = []
        if args.dry_run:
            incompatible.append("--dry-run")
        if args.scope != "global":
            incompatible.append(f"--scope {args.scope}")
        if args.plugin:
            incompatible.append("--plugin")
        if incompatible:
            detail = "unsupported for managed releases: " + ", ".join(incompatible)
            return {
                "runtime": args.runtimes or [],
                "channel": "managed-release",
                "checks": [{"id": "update.options", "ok": False, "detail": detail}],
                "drift": [],
                "exit": EXIT_BLOCKED,
                "lines": [f"update: blocked: {detail}"],
            }
        if args.reapply:
            return {
                "runtime": args.runtimes or [],
                "channel": "managed-release",
                "checks": [
                    {
                        "id": "update.mode",
                        "ok": False,
                        "detail": "--reapply is for checkout copy-once drift, not managed releases",
                    }
                ],
                "drift": [],
                "exit": EXIT_BLOCKED,
                "lines": [
                    "update: blocked: --reapply is unavailable for managed releases"
                ],
            }
        try:
            result = distribution.update(
                version=args.version,
                runtimes=args.runtimes,
                automatic=args.auto,
                force_prune_unproven=getattr(args, "force_prune_unproven", False),
            )
        except distribution.DistributionError as exc:
            return {
                "runtime": args.runtimes or [],
                "channel": "managed-release",
                "checks": [{"id": "update.release", "ok": False, "detail": str(exc)}],
                "drift": [],
                "exit": EXIT_FAIL,
                "lines": [f"update: managed release failed: {exc}"],
            }
        lines = [
            f"update: managed release {result['status']} version={result['version']}"
        ]
        for runtime in result.get("runtimes", []):
            action = result.get("session_action", {}).get(runtime)
            lines.append(f"updated: {runtime} session_action={action}")
        for runtime, reason in result.get("skipped", {}).items():
            lines.append(f"skipped: {runtime} ({reason})")
        return {
            "runtime": result.get("runtimes", []),
            "channel": "managed-release",
            "release": result,
            "checks": [
                {
                    "id": "update.release",
                    "ok": True,
                    "detail": f"{result['status']} {result['version']}",
                }
            ],
            "drift": [],
            "exit": EXIT_OK,
            "lines": lines,
        }

    runtimes = resolve_runtimes(args)
    drift = manifest.check_drift(runtimes, scope=args.scope)
    lines = [f"update: runtime={r} reapply={args.reapply}" for r in runtimes]
    checks = []

    if not args.reapply:
        if drift:
            for d in drift:
                lines.append(f"drift: {d['runtime']}/{d['path']} ({d['detail']})")
            checks.append(
                {
                    "id": "update.drift",
                    "ok": False,
                    "detail": f"found {len(drift)} drift item(s); use --reapply or inspect manually",
                }
            )
            exit_code = EXIT_DRIFT
        else:
            checks.append({"id": "update.drift", "ok": True, "detail": "no drift"})
            exit_code = EXIT_OK
        launcher_results = bootstrap.install_launchers(dry_run=args.dry_run)
        for launcher in launcher_results:
            if launcher["name"] != "compute-hosts":
                continue
            status = launcher["status"]
            lines.append(f"bootstrap: launcher compute-hosts -> {status}" +
                         (f" ({launcher['detail']})" if launcher.get("detail") else ""))
            launcher_ok = status != "skipped-collision"
            checks.append({
                "id": "bootstrap.launcher.compute-hosts",
                "ok": launcher_ok,
                "detail": launcher.get("detail", status),
            })
            if not launcher_ok:
                exit_code = EXIT_FAIL
        return {"runtime": runtimes, "channel": "plugin" if args.plugin else "dev", "checks": checks,
                "drift": drift, "exit": exit_code, "lines": lines}

    # --reapply: build sources dict from current projector plan's copy_once entries.
    sources = {rt: {} for rt in runtimes}
    for rt in runtimes:
        entries = projector.plan([rt], scope=args.scope)[rt]
        for entry in entries:
            if entry["action"] == "copy_once":
                relpath = Path(entry["dest"]).name
                sources[rt][relpath] = entry["source"]

    result = manifest.reapply(runtimes, scope=args.scope, sources=sources)

    for r in result["reapplied"]:
        lines.append(f"reapplied: {r['runtime']}/{r['path']}")
    for c in result["conflicts"]:
        lines.append(f"conflict: {c['runtime']}/{c['path']} ({c.get('status')})")
    for v in result["verify_failed"]:
        lines.append(f"verify_failed: {v['runtime']}/{v['path']} ({v.get('status')})")
    for m in result["missing"]:
        lines.append(f"missing: {m['runtime']}/{m['path']}")

    checks.append({"id": "update.reapplied", "ok": True, "detail": f"reapplied {len(result['reapplied'])} file(s)"})
    checks.append(
        {
            "id": "update.conflicts",
            "ok": not result["conflicts"],
            "detail": f"{len(result['conflicts'])} conflict(s)",
        }
    )
    checks.append(
        {
            "id": "update.verify_failed",
            "ok": not result["verify_failed"],
            "detail": f"{len(result['verify_failed'])} verification failure(s)",
        }
    )
    checks.append({"id": "update.missing", "ok": True, "detail": f"{len(result['missing'])} missing file(s)"})

    exit_code = EXIT_DRIFT if (result["conflicts"] or result["verify_failed"]) else EXIT_OK
    return {"runtime": runtimes, "channel": "plugin" if args.plugin else "dev", "checks": checks,
            "drift": drift, "exit": exit_code, "lines": lines}


def cmd_status(args):
    runtimes = resolve_runtimes(args)
    lines = []
    checks = []
    managed = None
    if args.scope == "global" and not args.plugin:
        try:
            managed = distribution.managed_status()
        except distribution.DistributionError as exc:
            return {
                "runtime": runtimes,
                "channel": "managed-release",
                "checks": [
                    {"id": "distribution.status", "ok": False, "detail": str(exc)}
                ],
                "drift": [],
                "exit": EXIT_FAIL,
                "lines": [f"managed release: invalid state: {exc}"],
            }
    for rt in runtimes:
        try:
            activation = runtime_activation.status(rt, scope=args.scope)
            freshness = activation.get("freshness", "unknown")
            if activation.get("mode") is None:
                freshness = "not-activated"
        except runtime_activation.ActivationError as exc:
            activation = None
            freshness = f"error:{exc}"
        managed_runtime = False
        if managed and activation and rt in managed["runtimes"]:
            source = activation.get("source_root")
            try:
                managed_runtime = (
                    activation.get("mode") == "packaged"
                    and source is not None
                    and Path(source).expanduser().resolve(strict=False)
                    == Path(managed["release_root"]).expanduser().resolve(strict=False)
                )
            except (OSError, TypeError, ValueError):
                managed_runtime = False
        if managed_runtime:
            channel = "managed-release"
            version = managed["version"]
            drift_count = 0
        else:
            status = get_driver(rt).status(scope=args.scope)
            channel = status["channel"]
            version = status["version"]
            drift_count = status["drift_count"]
        detail = (
            f"channel={channel} version={version} drift={drift_count} "
            f"projection={freshness}"
        )
        healthy = freshness in ("fresh", "not-activated")
        checks.append({"id": f"{rt}.status", "ok": healthy, "detail": detail})
        lines.append(f"{rt}: {detail}")
    channel = managed["channel"] if managed else ("plugin" if args.plugin else "dev")
    result = {
        "runtime": runtimes,
        "channel": channel,
        "checks": checks,
        "drift": [],
        "exit": EXIT_OK,
        "lines": lines,
    }
    if managed:
        result["release"] = managed
    return result


def cmd_uninstall(args):
    runtimes = resolve_runtimes(args)
    lines = []
    checks = []

    try:
        for runtime in runtimes:
            runtime_activation.validate_scope(runtime, args.scope)
    except runtime_activation.ActivationError as exc:
        return {
            "runtime": runtimes,
            "channel": "dev",
            "checks": [{"id": "uninstall.preflight", "ok": False, "detail": f"invalid-before-mutation: {exc}"}],
            "drift": [],
            "exit": EXIT_BLOCKED,
            "lines": [f"uninstall: blocked before mutation: {exc}"],
        }

    for rt in runtimes:
        launcher_snapshot = None
        if rt == "codex" and not args.dry_run:
            launcher_snapshot = codex_launcher.capture_snapshot(
                operation="uninstall"
            )
        try:
            if rt == "codex":
                try:
                    launcher_result = codex_launcher.uninstall(
                        dry_run=args.dry_run,
                        transaction_snapshot=launcher_snapshot,
                    )
                    lines.append(
                        "uninstall: codex — managed launcher "
                        + launcher_result["status"]
                    )
                    checks.append(
                        {
                            "id": "codex.managed-launcher",
                            "ok": True,
                            "detail": launcher_result["status"],
                        }
                    )
                except codex_launcher.CodexLauncherError as exc:
                    lines.append(f"uninstall: codex — managed launcher blocked: {exc}")
                    checks.append(
                        {"id": "codex.managed-launcher", "ok": False, "detail": str(exc)}
                    )
                    if launcher_snapshot is not None:
                        if "postimages" not in launcher_snapshot:
                            codex_launcher.seal_snapshot(launcher_snapshot)
                        codex_launcher.restore_snapshot(launcher_snapshot)
                    return {
                        "runtime": runtimes,
                        "channel": "dev",
                        "checks": checks,
                        "drift": [],
                        "exit": EXIT_BLOCKED,
                        "lines": lines,
                    }
                if (
                    not args.dry_run
                    and os.environ.get("HARNESS_INSTALLER_FAIL_AFTER_UNINSTALL_LAUNCHER") == "1"
                ):
                    # Private-fixture fault injection only: proves a failure
                    # immediately after the managed launcher's uninstall commit
                    # boundary — before any remaining uninstall step for this
                    # runtime runs — restores the launcher to its exact
                    # pre-uninstall bytes/state instead of leaving a partial
                    # uninstall with the protected ingress already removed.
                    raise RuntimeError(
                        "injected failure after uninstall launcher commit boundary"
                    )
            try:
                deactivated = runtime_activation.deactivate(
                    rt, scope=args.scope, dry_run=args.dry_run
                )
            except runtime_activation.ActivationError as exc:
                deactivated = None
                lines.append(f"uninstall: {rt} — projection deactivation blocked: {exc}")
                checks.append({"id": f"{rt}.deactivate", "ok": False, "detail": str(exc)})
            if deactivated is not None and deactivated["status"] != "not-active":
                verb = "would remove" if args.dry_run else "removed"
                lines.append(
                    f"uninstall: {rt} — projection {deactivated['status']}; "
                    f"{verb} {len(deactivated['removed'])} owned path(s)"
                )
                checks.append(
                    {
                        "id": f"{rt}.deactivate",
                        "ok": True,
                        "detail": f"{deactivated['status']}: {len(deactivated['removed'])} path(s)",
                    }
                )

            manifest_path = manifest._manifest_path(rt, args.scope)
            manifest_data, loaded_manifest_state = manifest.load_ownership_manifest(
                manifest_path, rt, args.scope
            )

            if manifest_data is None:
                lines.append(f"uninstall: {rt} — no manifest; nothing to remove")
                checks.append({"id": f"{rt}.uninstall", "ok": True, "detail": "no manifest, nothing to uninstall"})
                if launcher_snapshot is not None:
                    codex_launcher.discard_snapshot(launcher_snapshot)
                continue

            runtime_home = paths.runtime_home(rt, args.scope)

            copy_once_dests = [runtime_home / relpath for relpath in manifest_data.get("files", {})]

            entries = projector.plan([rt], scope=args.scope)[rt]
            symlink_entries = [e for e in entries if e["action"] == "symlink"]
            symlink_dests = [Path(e["dest"]) for e in symlink_entries]

            if args.dry_run:
                for d in copy_once_dests:
                    lines.append(f"uninstall(dry-run): {rt} — remove copy-once file {d}")
                for d in symlink_dests:
                    lines.append(f"uninstall(dry-run): {rt} — remove symlink {d}")
                lines.append(f"uninstall(dry-run): {rt} — remove manifest {manifest_path}")
                checks.append(
                    {
                        "id": f"{rt}.uninstall",
                        "ok": True,
                        "detail": f"dry-run: would remove {len(copy_once_dests)} copy-once file(s) and {len(symlink_dests)} symlink(s)",
                    }
                )
                continue

            deletion_targets = [*symlink_dests, *copy_once_dests, manifest_path]
            with safe_fs.TargetLocks(deletion_targets):
                symlink_states = []
                for entry in symlink_entries:
                    d = Path(entry["dest"])
                    current = safe_fs.capture_state(d)
                    if current.kind == "missing":
                        continue
                    if current.kind != "symlink":
                        raise RuntimeError(
                            f"ownership-unproved: projection is no longer a symlink: {d}"
                        )
                    raw_target = Path(os.readlink(d))
                    resolved = (
                        raw_target if raw_target.is_absolute() else d.parent / raw_target
                    ).resolve(strict=False)
                    expected_target = Path(entry["source"]).resolve(strict=False)
                    if resolved != expected_target:
                        raise RuntimeError(
                            f"expected-state-mismatch: repointed projection preserved: {d}"
                        )
                    symlink_states.append((d, current))

                copy_states = []
                for relpath, d in zip(
                    manifest_data.get("files", {}), copy_once_dests
                ):
                    manifest._safe_relpath(relpath)
                    current = safe_fs.capture_state(d)
                    if current.kind == "missing":
                        continue
                    expected_hash = manifest_data["files"][relpath]
                    if current.kind != "file" or current.digest != expected_hash:
                        raise RuntimeError(
                            f"expected-state-mismatch: modified copy-once file preserved: {d}"
                        )
                    copy_states.append((relpath, d, current))

                manifest_state = safe_fs.capture_state(manifest_path)
                if manifest_state != loaded_manifest_state:
                    raise RuntimeError(
                        f"expected-state-mismatch: manifest changed while preparing uninstall: {manifest_path}"
                    )
                if manifest_state.kind not in {"file", "missing"}:
                    raise RuntimeError(
                        f"ownership-unproved: manifest path has unsafe kind: {manifest_path}"
                    )

                # 1) Remove only exact, still-owned symlink projections.
                for d, current in symlink_states:
                    auth = safe_fs.authority(
                        d,
                        owner=f"installer-manifest:{rt}:symlink",
                        allowed_paths=(d,),
                        expected=current,
                    )
                    safe_fs.remove_exact(auth)
                    lines.append(f"uninstall: {rt} — removed symlink {d}")

                # 2) Back up and remove byte-exact copy-once files.
                for relpath, d, current in copy_states:
                    backup_path = (
                        paths.harness_state_dir(rt, args.scope)
                        / "local-patches"
                        / relpath
                    )
                    manifest._atomic_write_bytes(backup_path, d.read_bytes())
                    auth = safe_fs.authority(
                        d,
                        owner=f"installer-manifest:{rt}:copy-once:{relpath}",
                        allowed_paths=(d,),
                        expected=current,
                    )
                    safe_fs.remove_exact(auth)
                    lines.append(
                        f"uninstall: {rt} — removed copy-once file {d} "
                        f"(backed up to {backup_path})"
                    )

                # 3) Remove the exact manifest object last.
                if manifest_state.kind == "file":
                    auth = safe_fs.authority(
                        manifest_path,
                        owner=f"installer-manifest:{rt}:registry",
                        allowed_paths=(manifest_path,),
                        expected=manifest_state,
                    )
                    safe_fs.remove_exact(auth)
                    lines.append(f"uninstall: {rt} — removed manifest {manifest_path}")

            checks.append(
                {
                    "id": f"{rt}.uninstall",
                    "ok": True,
                    "detail": f"removed {len(copy_once_dests)} copy-once file(s) and {len(symlink_dests)} symlink(s)",
                }
            )
        except Exception as exc:
            if launcher_snapshot is not None:
                if "postimages" not in launcher_snapshot:
                    codex_launcher.seal_snapshot(launcher_snapshot)
                codex_launcher.restore_snapshot(launcher_snapshot)
            lines.append(
                f"uninstall: {rt} — blocked after managed-launcher commit; "
                f"launcher restored to its exact pre-uninstall state: {exc}"
            )
            checks.append({"id": f"{rt}.uninstall", "ok": False, "detail": str(exc)})
            return {
                "runtime": runtimes,
                "channel": "dev",
                "checks": checks,
                "drift": [],
                "exit": EXIT_BLOCKED,
                "lines": lines,
            }
        if launcher_snapshot is not None:
            codex_launcher.discard_snapshot(launcher_snapshot)

    full = (
        args.scope == "global"
        and (
            getattr(args, "target", None) == "all"
            or "all" in (args.runtimes or [])
            or set(runtimes) == set(RUNTIMES)
        )
    )
    if full and args.scope == "global":
        launcher_result = bootstrap.uninstall_compute_hosts(dry_run=args.dry_run)
        lines.append("uninstall: common compute-hosts launcher — " + launcher_result["status"])
        checks.append({
            "id": "bootstrap.launcher.compute-hosts",
            "ok": launcher_result["status"] != "preserved-foreign",
            "detail": launcher_result["status"],
        })
    else:
        lines.append("uninstall: common compute-hosts launcher — retained for other runtimes")
        checks.append({
            "id": "bootstrap.launcher.compute-hosts",
            "ok": True,
            "detail": "retained for partial runtime uninstall",
        })
    return {"runtime": runtimes, "channel": "dev", "checks": checks, "drift": [], "exit": EXIT_OK, "lines": lines}


def _runtime_targets(value):
    values = value if isinstance(value, list) else [value]
    if not values or "all" in values:
        return list(runtime_activation.RUNTIMES)
    result = []
    for runtime in values:
        if runtime not in result:
            result.append(runtime)
    return result


def _runtime_emit_shape(command, reports, exit_code, lines):
    if len(reports) == 1:
        result = dict(reports[0])
        result.update({"command": command, "exit": exit_code, "lines": lines})
        return result
    return {
        "command": command,
        "runtimes": reports,
        "exit": exit_code,
        "lines": lines,
    }


def cmd_runtime(args):
    targets = _runtime_targets(args.runtime)
    reports = []
    lines = []
    exit_code = EXIT_OK
    snapshots = []
    snapshots_by_runtime = {}
    launcher_snapshot = None

    try:
        # Validate the complete multi-runtime request before any snapshot,
        # lock-file creation, directory creation, or state write.
        for runtime in targets:
            runtime_activation.validate_request(
                runtime,
                args.runtime_command,
                mode=args.mode if args.runtime_command == "activate" else None,
                source=args.source if args.runtime_command == "activate" else None,
                scope=args.scope,
            )
        if args.runtime_command in {"activate", "refresh"}:
            source = args.source if args.runtime_command == "activate" else None
            if "codex" in targets:
                launcher_snapshot = codex_launcher.capture_snapshot(
                    operation="activation"
                )
            for runtime in targets:
                snapshot = runtime_activation.capture_runtime_state(
                    runtime, source, args.scope
                )
                snapshots.append(snapshot)
                snapshots_by_runtime[runtime] = snapshot
        for runtime in targets:
            if args.runtime_command == "status":
                report = runtime_activation.status(runtime, args.scope)
                if report["freshness"] in {
                    "missing", "cache-stale", "duplicate", "unsupported"
                }:
                    exit_code = EXIT_VERIFY_FAIL
            elif args.runtime_command == "activate":
                report = runtime_activation.activate(
                    runtime,
                    args.mode,
                    args.source,
                    args.scope,
                )
            elif args.runtime_command == "refresh":
                report = runtime_activation.refresh(runtime, args.scope)
            elif args.runtime_command == "doctor":
                report = runtime_activation.doctor(runtime, args.strict, args.scope)
                if not report["ok"]:
                    exit_code = EXIT_VERIFY_FAIL
            else:
                raise runtime_activation.ActivationError(
                    f"unknown runtime command: {args.runtime_command}"
                )
            if args.runtime_command in {"activate", "refresh"}:
                runtime_activation.seal_runtime_state(
                    snapshots_by_runtime[runtime]
                )
            if runtime == "codex" and args.runtime_command in {"status", "doctor"}:
                launcher_report = codex_launcher.status()
                report["managed_launcher"] = launcher_report
                if args.runtime_command == "doctor" and args.strict and launcher_report.get("installed"):
                    if launcher_report.get("protected") and launcher_report.get("path_precedence") != "first":
                        report["ok"] = False
                        exit_code = EXIT_VERIFY_FAIL
                        report["next_action"] = "put the protected Codex ingress first on PATH"
            reports.append(report)
            freshness = report.get("freshness")
            if freshness is None and isinstance(report.get("status"), dict):
                freshness = report["status"].get("freshness")
            lines.append(
                f"{runtime}: {args.runtime_command} "
                f"freshness={freshness} "
                f"next={report.get('next_action', 'none')}"
            )
            for state_path in report.get("bundle_runtime_state", []):
                lines.append(
                    f"{runtime}: bundle-runtime-state {state_path} "
                    "(runtime state inside the immutable release bundle; "
                    "remove it, nothing may write there)"
                )
        if args.runtime_command == "doctor":
            # Cross-surface skew is a property of the INSTALLATION, not of any one runtime,
            # so it is reported once after the per-runtime loop. The managed release root
            # is passed in rather than imported inside runtime_activation, which keeps that
            # module free of the distribution/network layer.
            skew = runtime_activation.surface_skew(
                distribution.current_path(), scope=args.scope
            )
            for surface in skew["surfaces"]:
                lines.append(
                    "surface %s: %s label=%s root=%s" % (
                        surface["name"],
                        "present" if surface["present"] else "absent",
                        surface["label"] or "-",
                        surface["root"] or "-",
                    )
                )
            if skew["ok"]:
                lines.append(
                    "surface-skew: none compared=" + ",".join(skew["compared"])
                )
            else:
                for entry in skew["skewed"]:
                    lines.append(
                        "surface-skew: %s differs across %s" % (
                            entry["subtree"],
                            " | ".join("+".join(group) for group in entry["groups"]),
                        )
                    )
                lines.append(
                    "next: harness update --version latest (release) and "
                    "harness runtime refresh --runtime all (runtimes)"
                )
                exit_code = EXIT_VERIFY_FAIL
            for report in reports:
                report["surface_skew"] = skew
        if args.runtime_command in {"activate", "refresh"} and "codex" in targets:
            try:
                launcher_result = codex_launcher.install(
                    transaction_snapshot=launcher_snapshot
                )
            except codex_launcher.CodexUnavailableError as exc:
                launcher_result = {
                    "action": "managed-launcher",
                    "status": "skipped-unavailable",
                    "target": str(codex_launcher.wrapper_path(codex_launcher.default_bin_dir())),
                    "detail": str(exc),
                }
                if launcher_snapshot is not None:
                    codex_launcher.seal_snapshot(launcher_snapshot)
            except codex_launcher.CodexLauncherError as exc:
                raise runtime_activation.ActivationError(
                    f"Codex managed launcher installation failed: {exc}"
                ) from exc
            if os.environ.get("HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER") == "1":
                # Private-fixture fault injection only: proves rollback restores
                # both the just-committed launcher transaction and every runtime
                # snapshot when a failure lands immediately after the launcher
                # commit boundary, the narrowest possible post-commit window.
                raise runtime_activation.ActivationError(
                    "injected failure after launcher commit boundary"
                )
            lines.append(
                "codex: managed-launcher "
                f"status={launcher_result['status']} target={launcher_result['target']}"
            )
            for report in reports:
                if report.get("runtime") == "codex":
                    report["managed_launcher"] = launcher_result
        if args.runtime_command in {"activate", "refresh"}:
            routing = routing_config.ensure(targets)
            lines.append(
                "routing-config: " + routing["status"] + " " + routing["path"]
                + " enabled=" + ",".join(routing["enabled"])
            )
            for report in reports:
                report["routing_config"] = routing
            requested_root = (
                args.report_bundle_root if args.runtime_command == "activate" else None
            )
            bundle_config = report_bundle_config.ensure(requested_root)
            lines.append(
                "report-bundle-config: " + bundle_config["status"] + " "
                + bundle_config["path"] + " root=" + bundle_config["root"]
            )
            for report in reports:
                report["report_bundle_config"] = bundle_config
    except runtime_activation.ActivationError as exc:
        rollback_errors = []
        for snapshot in reversed(snapshots):
            try:
                if not snapshot.get("_sealed"):
                    runtime_activation.seal_runtime_state(snapshot)
                runtime_activation.restore_runtime_state(snapshot)
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if launcher_snapshot is not None:
            try:
                if "postimages" not in launcher_snapshot:
                    codex_launcher.seal_snapshot(launcher_snapshot)
                codex_launcher.restore_snapshot(launcher_snapshot)
            except Exception as rollback_exc:
                rollback_errors.append(f"launcher: {rollback_exc}")
        for report in reports:
            report["rolled_back"] = True
        if snapshots:
            lines.append("runtime invocation rolled back across all selected runtimes")
        if rollback_errors:
            lines.append("rollback errors: " + "; ".join(rollback_errors))
        lines.append(f"runtime {args.runtime_command}: blocked: {exc}")
        for snapshot in snapshots:
            runtime_activation.discard_runtime_state(snapshot)
        return _runtime_emit_shape(
            args.runtime_command,
            reports + [{"error": str(exc)}],
            EXIT_BLOCKED,
            lines,
        )
    except Exception:
        for snapshot in reversed(snapshots):
            if not snapshot.get("_sealed"):
                runtime_activation.seal_runtime_state(snapshot)
            runtime_activation.restore_runtime_state(snapshot)
        if launcher_snapshot is not None:
            if "postimages" not in launcher_snapshot:
                codex_launcher.seal_snapshot(launcher_snapshot)
            codex_launcher.restore_snapshot(launcher_snapshot)
        for snapshot in snapshots:
            runtime_activation.discard_runtime_state(snapshot)
        raise

    for snapshot in snapshots:
        runtime_activation.discard_runtime_state(snapshot)
    if launcher_snapshot is not None:
        codex_launcher.discard_snapshot(launcher_snapshot)

    return _runtime_emit_shape(args.runtime_command, reports, exit_code, lines)


def cmd_extension(args):
    operation = args.extension_command
    try:
        if operation == "inspect":
            report = extensions.inspect_source(args.source)
            blocked = report["status"] == "blocked"
            return {
                "operation": operation,
                "extension": report,
                "checks": report["findings"],
                "drift": [],
                "exit": EXIT_VERIFY_FAIL if blocked else EXIT_OK,
                "lines": [
                    f"extension inspect: {report['status']}: {report['canonical_id']}",
                    f"checksum: {report['source_checksum']}",
                    (
                        "parity: "
                        + ", ".join(
                            f"{runtime}={value['status']}"
                            for runtime, value in report["parity"].items()
                        )
                    ),
                    (
                        "inactive: "
                        + (",".join(report["inactive_surfaces"]) or "none")
                    ),
                    f"findings: {len(report['findings'])}",
                    (
                        f"next: extension add {report['source']}"
                        if not blocked
                        else "next: resolve blocking findings and inspect again"
                    ),
                ],
            }
        if operation == "add":
            result = extensions.add(args.source, args.extension_runtimes)
        elif operation == "update":
            result = extensions.update(args.canonical_id, args.source)
        elif operation == "remove":
            result = extensions.remove(args.canonical_id)
        else:
            raise extensions.ExtensionError(
                "unsupported-operation", f"unsupported extension operation: {operation}"
            )
        changed = "changed" if result["changed"] else "unchanged"
        return {
            "operation": operation,
            "extension": result,
            "checks": [],
            "drift": [],
            "exit": EXIT_OK,
            "lines": [
                f"extension {operation}: {changed}: {result['canonical_id']}",
                f"checksum: {result['snapshot_key']}",
                (
                    "projection: "
                    + ", ".join(
                        f"{runtime}={destination}"
                        for runtime, destination in result["runtime_projection"].items()
                    )
                ),
                f"inactive: {','.join(result['inactive_surfaces']) or 'none'}",
                f"snapshot: {result['snapshot'] or 'removed'}",
                (
                    "next-session: "
                    + ", ".join(
                        f"{runtime}={action}"
                        for runtime, action in result["next_session_action"].items()
                    )
                ),
            ],
        }
    except extensions.ExtensionError as exc:
        exit_code = EXIT_VERIFY_FAIL if operation == "inspect" else EXIT_BLOCKED
        return {
            "operation": operation,
            "extension": None,
            "checks": [{"id": exc.reason, "ok": False, "detail": str(exc)}],
            "drift": [],
            "exit": exit_code,
            "reason": exc.reason,
            "lines": [f"extension {operation}: blocked ({exc.reason}): {exc}"],
        }


def cmd_auto_update(args):
    try:
        result = distribution.auto_update(args.operation)
    except distribution.DistributionError as exc:
        return {
            "operation": args.operation,
            "scheduler": None,
            "checks": [{"id": "auto-update", "ok": False, "detail": str(exc)}],
            "drift": [],
            "exit": EXIT_FAIL,
            "lines": [f"auto-update {args.operation}: failed: {exc}"],
        }
    detail = result["status"]
    lines = [f"auto-update {args.operation}: {result['status']} ({result['kind']})"]
    if args.operation == "status":
        detail = f"{detail} ({result['kind']}) health={result['health']}"
        scheduler = result["scheduler"]
        display_state = {True: "yes", False: "no", None: "unknown"}
        lines.extend([
            f"managed release: version={result.get('version') or '-'} channel={result.get('channel') or '-'}",
            f"scheduler health: {result['health']} ({scheduler.get('detail') or scheduler['probe']})",
            "scheduler state: "
            f"loaded={display_state.get(scheduler.get('loaded'), 'unknown')} "
            f"active={display_state.get(scheduler.get('active'), 'unknown')} "
            f"enabled={display_state.get(scheduler.get('enabled'), 'unknown')}",
        ])
        if scheduler.get("last_trigger") is not None:
            lines.append(f"last trigger: {scheduler['last_trigger']}")
        if scheduler.get("last_result") is not None:
            exit_status = scheduler.get("exit_status")
            exit_detail = f" (exit={exit_status})" if exit_status is not None else ""
            lines.append(f"last result: {scheduler['last_result']}{exit_detail}")
    return {
        "operation": args.operation,
        "scheduler": result,
        "checks": [{"id": "auto-update", "ok": True, "detail": detail}],
        "drift": [],
        "exit": EXIT_OK,
        "lines": lines,
    }


def cmd_config(args):
    """Read-only view of the user-owned configuration registry."""
    rows = user_config.status()
    ok = all(row["ok"] for row in rows)
    return {
        "operation": f"config {args.config_command}",
        "user_config": rows,
        "checks": [],
        "drift": [],
        "exit": EXIT_OK if ok else EXIT_VERIFY_FAIL,
        "lines": user_config.lines(rows, verbose=args.verbose),
    }


def cmd_memory(args):
    """Record the shared exchange policy, then join this store to it."""
    operation = args.memory_command
    if operation == "status":
        report = memory_sync_config.validate()
        return {
            "operation": f"memory {operation}",
            "memory_sync": report,
            "checks": [],
            "drift": [],
            "exit": EXIT_OK if report["status"] in {"ok", "absent"} else EXIT_VERIFY_FAIL,
            "lines": [f"memory sync config: {report['status']}: {report['path']}"]
                     + ([f"remote: {report.get('remote_url')}",
                         f"ref: {report.get('ref')}",
                         f"exchange: {report.get('exchange_dir') or '(default)'}"]
                        if report["status"] == "ok" else []),
        }

    try:
        written = memory_sync_config.write(
            remote_url=args.remote_url, ref=args.ref,
            exchange_dir=args.exchange_dir, dry_run=args.dry_run)
    except ValueError as exc:
        return {"operation": f"memory {operation}", "checks": [], "drift": [],
                "exit": EXIT_FAIL, "lines": [f"memory join: invalid policy: {exc}"]}

    lines = [f"memory join: policy {written['status']}: {written['path']}",
             f"remote: {written['remote_url']}",
             f"ref: {written['ref']}",
             f"exchange: {written.get('exchange_dir') or '(default)'}"]
    if args.dry_run:
        lines.append("next: rerun without --dry-run to join")
        return {"operation": f"memory {operation}", "memory_sync": written,
                "checks": [], "drift": [], "exit": EXIT_OK, "lines": lines}

    # `mem` owns every store mutation; this command only supplies the policy.
    mem = paths.agent_home() / "tools" / "memory" / "mem.py"
    completed = subprocess.run(
        [sys.executable, str(mem), "migration", "join", "--apply", "--sync", "--json"],
        text=True, capture_output=True)
    payload = {}
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            payload = {}
    ok = completed.returncode == 0 and payload.get("status") != "hard-failure"
    lines.append(
        f"join: {payload.get('migration_state', 'failed')}"
        f" ({payload.get('reason', completed.stderr.strip()[:120] or 'no output')})")
    if ok:
        lines.append(f"sync exit: {payload.get('sync_exit_code', 'n/a')}")
    return {"operation": f"memory {operation}", "memory_sync": written,
            "join": payload, "checks": [], "drift": [],
            "exit": EXIT_OK if ok else EXIT_FAIL, "lines": lines}


COMMANDS = {
    "install": cmd_install,
    "memory": cmd_memory,
    "config": cmd_config,
    "verify": cmd_verify,
    "update": cmd_update,
    "status": cmd_status,
    "uninstall": cmd_uninstall,
    "runtime": cmd_runtime,
    "extension": cmd_extension,
    "auto-update": cmd_auto_update,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    result = handler(args)
    return emit(result, args.json)


if __name__ == "__main__":
    sys.exit(main())
