#!/usr/bin/env python3
"""Atomically admit and concurrently launch one immutable 2..4-way group."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    GOVERNOR_RESERVATION_ENV,
    attempt_process_quiescence,
    completion_attempt_readiness,
    completion_marker_gate,
    PRELAUNCH_PROCESS_BLOCK_REASONS,
    completion_marker_is_current,
    dispatch_state_roots,
    parse_registry_metadata,
    recover_unstarted_attempt,
    resolve_agent_home,
    resolve_dispatch_state_root,
    resolve_global_registry,
    resolve_live_parent_attempt,
    resolve_model_governor_root,
    validate_attempt_metadata,
    validate_dispatch_log_dir,
)
from dispatch_lifecycle import select_launch_lifecycle  # noqa: E402
from replica_batch_contract import DIGEST, build_manifest  # noqa: E402
from dispatch_degradation import record_degradation  # noqa: E402
from dispatch_allocation import (  # noqa: E402
    STRATEGY as ALLOCATION_STRATEGY,
    attempt_counts,
)

CAPACITY_SPEC = importlib.util.spec_from_file_location(
    "harness_capacity", ROOT / "utilities" / "harness-capacity.py"
)
if CAPACITY_SPEC is None or CAPACITY_SPEC.loader is None:
    raise RuntimeError("harness-capacity loader unavailable")
CAPACITY = importlib.util.module_from_spec(CAPACITY_SPEC)
CAPACITY_SPEC.loader.exec_module(CAPACITY)

NODE_SPEC = importlib.util.spec_from_file_location(
    "dispatch_node", ROOT / "utilities" / "dispatch-node.py"
)
if NODE_SPEC is None or NODE_SPEC.loader is None:  # pragma: no cover - install corruption
    raise RuntimeError("dispatch-node loader unavailable")
DISPATCH_NODE = importlib.util.module_from_spec(NODE_SPEC)
NODE_SPEC.loader.exec_module(DISPATCH_NODE)

# All three, in preference order. `DISPATCH_NODE.resolve_checked_tuple` filters the
# node's own compiled fallback_hops by child_harness and by the actual launching
# parent identity, so an adapter listed here is only ever selected when the route
# actually sealed a supported, this-parent candidate for it — this list widens what
# may be chosen, never what is authorized. OpenCode was absent without a recorded
# reason while `dispatch-defaults.DISPATCHABLE_HARNESSES` already authorized it as a
# relief target, and with only two entries the `>= 2 distinct harnesses` rule below
# had exactly one legal combination, leaving cross-family placement no slack at all.
SUPPORTED_BATCH_HARNESSES = ("codex", "claude", "opencode")
SAFE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
RESERVATION_TOKEN = re.compile(r"[0-9a-f]{32}")
OUTPUT_TAIL_BYTES = 65536
DEFAULT_PROMPT = "Execute the selected immutable parallel leg and emit its completion evidence."

# Closed degradation vocabulary. An unconstrained free-text reason reproduces
# the defect one layer down: dispatch_degradation.py clips `reason` to 160
# characters and enforces nothing.
DEGRADATION_CAUSES = frozenset({
    "single-usable-harness-family",   # exactly one usable family for the group
    "no-usable-harness-family",       # a leg had no usable adapter at all
})
# Compact ledger codes: `detail` clips at 512 chars, so full typed reasons do
# not fit for a three-family group.
EXCLUSION_CODES = {
    "dispatch-evidence-no-eligible-fallback": "no-candidate",
    "dispatch-evidence-candidate-unsupported": "unsupported",
    "dispatch-evidence-ambiguous-candidate": "ambiguous",
    "dispatch-evidence-no-top-level-counterpart": "no-counterpart",
    "dispatch-evidence-conflicting-counterparts": "conflicting-counterparts",
    "dispatch-evidence-parent-runtime-mismatch": "parent-mismatch",
}


class BatchError(RuntimeError):
    def __init__(
        self,
        reason: str,
        detail: str = "",
        *,
        degradation_reason: str | None = None,
        route_node: str | None = None,
    ):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        # Set only for the two assign_harnesses failures that carry typed
        # degradation evidence. They remain part of the failure receipt;
        # failures never write an outcome ledger row because no child launched.
        self.degradation_reason = degradation_reason
        self.route_node = route_node


def fail(reason: str, code: int, **fields: object) -> int:
    receipt = {"schema_version": 1, "state": "blocked", "reason": reason, **fields}
    print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    return code


def load_route(route_path: Path) -> dict[str, object]:
    try:
        route = json.loads(route_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BatchError("route-record-unreadable", str(exc)) from exc
    if not isinstance(route, dict):
        raise BatchError("route-record-invalid", "route root must be an object")
    verify = subprocess.run(
        [
            sys.executable,
            str(ROOT / "utilities" / "capability-route.py"),
            "verify",
            "--route",
            str(route_path),
            "--cwd",
            str(route.get("cwd", "")),
        ],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if verify.returncode:
        raise BatchError("route-record-invalid", verify.stderr.strip()[:512])
    return route


def parallel_nodes(route: dict[str, object], group: str) -> list[dict[str, object]]:
    nodes = [
        node
        for node in route.get("nodes", [])
        if isinstance(node, dict)
        and (node.get("parallel_group") or node.get("replica_group")) == group
    ]
    if not 2 <= len(nodes) <= 4:
        raise BatchError("parallel-group-cardinality", f"group={group} count={len(nodes)}")
    if any("parallel_leg_index" in node for node in nodes):
        nodes.sort(key=lambda node: int(node.get("parallel_leg_index", -1)))
        if [node.get("parallel_leg_index") for node in nodes] != list(range(len(nodes))):
            raise BatchError("parallel-group-leg-index-invalid", group)
        if any(node.get("parallel_leg_count") != len(nodes) for node in nodes):
            raise BatchError("parallel-group-width-mismatch", group)
    if any(node.get("dispatch_depth") != 2 for node in nodes):
        raise BatchError("parallel-group-depth-invalid", group)
    dependencies = {tuple(node.get("depends_on", [])) for node in nodes}
    if len(dependencies) != 1:
        raise BatchError("parallel-group-dependency-mismatch", group)
    summaries = [
        row for row in route.get("parallel_groups", [])
        if isinstance(row, dict) and row.get("id") == group
    ]
    if summaries:
        summary = summaries[0]
        if (len(summaries) != 1 or summary.get("width") != len(nodes)
                or summary.get("members") != [node.get("id") for node in nodes]):
            raise BatchError("parallel-group-summary-mismatch", group)
    return nodes


# One-window import compatibility. New callers and CLI diagnostics use parallel_nodes.
replica_nodes = parallel_nodes


def _exclusion_codes(exclusions: dict[str, set[str]], *, exclude: set[str] = frozenset()) -> str:
    """Render every reason for every excluded adapter, deterministically.

    `next(iter(set_of_str))` picks by string hash, which CPython randomises
    per process; it also silently drops every reason but one when an adapter
    carries more than one. Sort both levels instead so the same inputs always
    render the same string, and no reason is discarded.
    """
    return ";".join(
        f"{adapter}=" + "+".join(EXCLUSION_CODES.get(reason, reason) for reason in sorted(reasons))
        for adapter, reasons in sorted(exclusions.items())
        if adapter not in exclude
    )


def _persist_degradation(
    agent_home: Path,
    route: dict[str, object],
    *,
    route_node: str | None,
    reason: str,
    detail: str,
) -> None:
    record_degradation(
        route_id=route.get("route_id"), route_node=route_node,
        route_hash=route.get("route_hash"), dispatch_depth=2,
        fallback_hop=None, execution_surface="registered-headless",
        writer="dispatch-batch.py", kind="degradation",
        agent_home=agent_home,
        reason=reason, detail=detail[:512],
    )


def _persist_launched_degradation(
    agent_home: Path,
    route: dict[str, object],
    diagnostics: dict[str, object],
    results: list[dict[str, object]],
) -> None:
    """Persist one group degradation only after a validated new child start.

    `--start` is an intent, not launch evidence: parent validation, completion
    gates, governor admission, duplicate checks, or wrapper startup may still
    stop the batch. The ledger records realized dispatch outcomes, so retries
    that resolve entirely to existing attempts and prelaunch failures must not
    add rows either.
    """
    reason = diagnostics.get("degradation_cause")
    if not reason or not any(row.get("launch_state") == "started" for row in results):
        return
    _persist_degradation(
        agent_home,
        route,
        route_node=None,
        reason=str(reason),
        detail=str(diagnostics.get("degradation_detail", "")),
    )


def assign_harnesses(
    route: dict[str, object],
    nodes: list[dict[str, object]],
    *,
    allow_degraded: bool,
    parent_identity: dict[str, str] | None = None,
    jobs: Path | None = None,
) -> tuple[list[tuple[dict[str, object], str, str, int]], str, dict[str, object]]:
    """Pick a harness per node. Side-effect free: never writes the ledger.

    A caller that realizes a new child launch may persist this function's
    returned diagnostics (`degradation_cause`/`degradation_detail`). Raised
    BatchError fields remain typed failure-receipt evidence, never proof that a
    dispatch happened (see `_persist_launched_degradation`).
    """
    options: list[list[tuple[str, str, int]]] = []
    exclusions: dict[str, set[str]] = {}
    for node in nodes:
        choices = []
        # Scoped to this node only, so a later node's failure detail cannot
        # report an earlier (possibly already-succeeded) node's exclusions as
        # its own. `exclusions` keeps accumulating across nodes for the
        # group-level diagnostics/degradation evidence below, which
        # legitimately wants group-wide reasons.
        node_exclusions: dict[str, set[str]] = {}
        for adapter in SUPPORTED_BATCH_HARNESSES:
            try:
                selection = DISPATCH_NODE.resolve_checked_tuple(
                    route, node, adapter, parent_identity=parent_identity
                )
            except DISPATCH_NODE.DispatchNodeError as exc:
                exclusions.setdefault(adapter, set()).add(exc.reason)
                node_exclusions.setdefault(adapter, set()).add(exc.reason)
                continue
            choices.append((adapter, selection.fallback_hop, selection.ordinal))
        if not choices:
            detail = f"node={node.get('id', '-')}"
            codes = _exclusion_codes(node_exclusions)
            if codes:
                detail += f";{codes}"
            raise BatchError(
                "parallel-headless-unavailable", detail,
                degradation_reason="no-usable-harness-family",
                route_node=node.get("id"),
            )
        options.append(choices)

    usable = sorted({adapter for choices in options for adapter, _hop, _ord in choices})

    combinations = list(itertools.product(*options))
    distinct = [rows for rows in combinations if len({row[0] for row in rows}) >= 2]
    independence = "cross-harness"
    if len(usable) >= 2:
        # Group width is >= 2 and every leg holds >= 1 option, so two usable
        # families always admit a two-family assignment: `not distinct` is
        # exactly "fewer than two usable compatible harness families". The
        # blanket --allow-degraded-independence boolean is therefore never
        # consulted on this branch and cannot downgrade an achievable
        # cross-harness group.
        if not distinct:
            # defensive: unreachable -- the equivalence above guarantees
            # distinct != [] whenever len(usable) >= 2.
            raise BatchError(
                "cross-harness-equivalence-violated",
                f"usable={','.join(usable)}",
            )
        combinations = distinct
    elif allow_degraded:
        independence = "degraded-same-harness"
    else:
        detail = f"usable={','.join(usable) or '-'}"
        codes = _exclusion_codes(exclusions, exclude=set(usable))
        if codes:
            detail += f";{codes}"
        raise BatchError(
            "parallel-cross-harness-unavailable", detail,
            degradation_reason="single-usable-harness-family",
        )

    degradation_cause = "" if independence == "cross-harness" else "single-usable-harness-family"
    if degradation_cause and degradation_cause not in DEGRADATION_CAUSES:
        # defensive: unreachable -- degradation_cause is only ever assigned
        # the literal "single-usable-harness-family" two lines above.
        raise BatchError("degradation-cause-not-in-vocabulary", degradation_cause)

    allocation = route.get("dispatch_allocation")
    counts = {harness: 0 for harness in SUPPORTED_BATCH_HARNESSES}
    declared_order = list(SUPPORTED_BATCH_HARNESSES)
    if (
        jobs is not None
        and isinstance(allocation, dict)
        and allocation.get("strategy") in {ALLOCATION_STRATEGY, "capacity-aware", "balanced"}
    ):
        counts = attempt_counts(jobs, window=int(allocation["window"]))
        declared_order = allocation.get("harness_order") or declared_order
    order = {harness: index for index, harness in enumerate(declared_order)}
    capacity = (
        CAPACITY.capacity_scores()
        if isinstance(allocation, dict) and allocation.get("strategy") in {"capacity-aware", "balanced"}
        else {harness: None for harness in SUPPORTED_BATCH_HARNESSES}
    )

    def band_rank(node, harness):
        policy = node.get("harness_policy")
        if not isinstance(policy, dict):
            return 0
        bands = ["primary", "relief", "last_resort"]
        threshold = policy.get("promote_relief_below", 0)
        primary = [capacity.get(name) for name in policy.get("primary", [])]
        primary = [value for value in primary if value is not None]
        if (
            policy.get("relief")
            and threshold
            and primary
            and len(primary) == len(policy.get("primary", []))
            and max(primary) <= threshold
        ):
            bands = ["relief", "primary", "last_resort"]
        for index, band in enumerate(bands):
            if harness in policy.get(band, []):
                return index
        return len(bands) + 1

    def score(rows: tuple[tuple[str, str, int], ...]) -> tuple:
        affinity_misses = sum(
            1
            for node, row in zip(nodes, rows)
            if node.get("harness_affinity") not in {
                None, "", "unspecified", "diverse", row[0]
            }
        )
        if isinstance(allocation, dict) and allocation.get("strategy") == "balanced":
            gate = 100 - allocation.get("usage_gate_used_percent", 90)
            gated = [capacity.get(row[0]) is not None and capacity.get(row[0]) <= gate for row in rows]
            all_gated = bool(gated) and all(gated)
            allocation_order = (
                sum(0 if item else 1 for item in gated),
                -sum(float(capacity.get(row[0]) or 0) for row in rows) if all_gated else 0,
                sum(counts.get(row[0], 0) for row in rows),
            )
        else:
            allocation_order = (
                -sum(CAPACITY.ordering_score(capacity, row[0]) for row in rows),
                sum(counts.get(row[0], 0) for row in rows),
            )
        return (
            -len({row[0] for row in rows}),
            sum(band_rank(node, row[0]) for node, row in zip(nodes, rows)),
            affinity_misses,
            *allocation_order,
            sum(row[2] for row in rows),
            tuple(order.get(row[0], len(order)) for row in rows),
        )

    diagnostics = {
        "families_considered": list(SUPPORTED_BATCH_HARNESSES),
        "usable_families": usable,
        "family_exclusions": {
            adapter: sorted(reasons)
            for adapter, reasons in sorted(exclusions.items())
            if adapter not in usable
        },
        "capacity": {h: capacity.get(h) for h in SUPPORTED_BATCH_HARNESSES},
        "degradation_cause": degradation_cause,
    }
    if degradation_cause:
        detail = f"usable={','.join(usable) or '-'}"
        codes = _exclusion_codes(exclusions, exclude=set(usable))
        if codes:
            detail += f";{codes}"
        diagnostics["degradation_detail"] = detail[:512]

    chosen = min(combinations, key=score)
    return [
        (node, adapter, hop, ordinal)
        for node, (adapter, hop, ordinal) in zip(nodes, chosen)
    ], independence, diagnostics


def stable_attempt_id(
    route: dict[str, object], node: dict[str, object], slug: str, parent: str,
    parent_attempt_id: str, adapter: str, ordinal: int,
) -> str:
    # Display labels are deliberately excluded. One exact parent generation,
    # route node and selected fallback tuple must always resolve to the same
    # launch identity even when a caller varies --slug-prefix on a retry.
    del slug, parent
    payload = {
        "route_id": route["route_id"],
        "route_node": node["id"],
        "parent_attempt_id": parent_attempt_id,
        "target_harness": adapter,
        "fallback_ordinal": ordinal,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "att-" + digest[:48]


def parallel_slug(prefix: str, node_id: str) -> str:
    """Keep the node identity after truncation and avoid sanitized collisions."""

    safe_prefix = SAFE_SLUG.sub("-", prefix).strip("-") or "replica"
    safe_node = SAFE_SLUG.sub("-", node_id).strip("-") or "node"
    node_digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:8]
    node_component = f"{safe_node[:40]}-{node_digest}"
    prefix_limit = 120 - len(node_component) - 1
    return f"{safe_prefix[:prefix_limit]}-{node_component}"


# One-window callable compatibility.
replica_slug = parallel_slug


def reserve_batch(
    governor: Path,
    governor_root: Path,
    pending_legs: list[dict[str, object]],
    *,
    manifest: dict[str, object],
    manifest_digest: str,
    peers: list[dict[str, str]] | None = None,
) -> list[str]:
    count = len(pending_legs)
    encoded_manifest = json.dumps(manifest, separators=(",", ":"), sort_keys=True)
    result = subprocess.run(
        [
            sys.executable,
            str(governor),
            "--root",
            str(governor_root),
            "reserve",
            "--class",
            "dispatch",
            "--count",
            str(count),
            "--pid",
            str(os.getpid()),
            "--batch-manifest",
            encoded_manifest,
            *[
                value
                for leg in pending_legs
                for value in ("--batch-attempt-id", str(leg["attempt_id"]))
            ],
            *(
                ["--batch-peers-json", json.dumps(peers, separators=(",", ":"), sort_keys=True)]
                if peers is not None else []
            ),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        payload = {}
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    valid_payload = (
        isinstance(payload, dict)
        and payload.get("class") == "dispatch"
        and payload.get("count") == count
        and payload.get("owner_pid") == os.getpid()
        and payload.get("batch_manifest_sha256") == manifest_digest
        and isinstance(tokens, list)
        and len(tokens) == count
        and all(isinstance(token, str) and RESERVATION_TOKEN.fullmatch(token) for token in tokens)
        and len(set(tokens)) == count
    )
    if result.returncode or not valid_payload:
        detail = (result.stderr or result.stdout).strip()[:512]
        raise BatchError("model-worker-governor-denied", detail or "atomic-reserve-failed")
    return tokens


def cancel_unclaimed(governor: Path, governor_root: Path, token: str) -> None:
    try:
        check = subprocess.run(
            [
                sys.executable,
                str(governor),
                "--root",
                str(governor_root),
                "reservation-check",
                "--token",
                token,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return
    try:
        state = json.loads(check.stdout).get("state")
    except (AttributeError, ValueError):
        state = "invalid"
    if state != "unclaimed":
        return
    try:
        subprocess.run(
            [
                sys.executable,
                str(governor),
                "--root",
                str(governor_root),
                "cancel",
                "--token",
                token,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


class BatchSignalRelay:
    """Keep wrapper groups owned and forward cancellation to every live leg."""

    def __init__(self) -> None:
        self.processes: list[subprocess.Popen] = []
        self.received: list[int] = []
        self.previous: dict[int, object] = {}

    def __enter__(self) -> "BatchSignalRelay":
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._forward)
        return self

    def __exit__(self, *_exc: object) -> None:
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)

    def _forward(self, signum: int, _frame: object) -> None:
        self.received.append(signum)
        for proc in tuple(self.processes):
            if proc.poll() is not None:
                continue
            try:
                os.killpg(proc.pid, signum)
            except ProcessLookupError:
                pass

    def add(self, proc: subprocess.Popen) -> None:
        self.processes.append(proc)
        if self.received and proc.poll() is None:
            try:
                os.killpg(proc.pid, self.received[-1])
            except ProcessLookupError:
                pass


def stop_wrapper(proc: subprocess.Popen) -> None:
    """Boundedly stop only the wrapper whose collection contract failed."""

    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def output_fields(value: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in value.splitlines() if "=" in line)


def _append_tail(buffer: bytearray, chunk: bytes) -> None:
    buffer.extend(chunk)
    if len(buffer) > OUTPUT_TAIL_BYTES:
        del buffer[:-OUTPUT_TAIL_BYTES]


def bounded_process_output(proc: subprocess.Popen) -> tuple[str, str]:
    """Drain both wrapper pipes while retaining only fixed-size UTF-8 tails."""

    stdout_stream = getattr(proc, "stdout", None)
    stderr_stream = getattr(proc, "stderr", None)
    if stdout_stream is None or stderr_stream is None:
        # Unit-test doubles have no real file descriptors; production Popen
        # always supplies both PIPE streams above.
        stdout, stderr = proc.communicate()
        return stdout[-OUTPUT_TAIL_BYTES:], stderr[-OUTPUT_TAIL_BYTES:]

    tails = {"stdout": bytearray(), "stderr": bytearray()}
    with selectors.DefaultSelector() as selector:
        selector.register(stdout_stream, selectors.EVENT_READ, "stdout")
        selector.register(stderr_stream, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            for key, _ in selector.select():
                chunk = os.read(key.fd, 8192)
                if chunk:
                    _append_tail(tails[str(key.data)], chunk)
                    continue
                selector.unregister(key.fileobj)
                key.fileobj.close()
    proc.wait()
    return (
        bytes(tails["stdout"]).decode("utf-8", errors="replace"),
        bytes(tails["stderr"]).decode("utf-8", errors="replace"),
    )


def collect_wrapper(
    item: tuple[dict[str, object], str, subprocess.Popen],
) -> tuple[dict[str, object], str, subprocess.Popen, str, str]:
    leg, token, proc = item
    stdout, stderr = bounded_process_output(proc)
    return leg, token, proc, stdout, stderr


def wrapper_result(
    leg: dict[str, object], proc: subprocess.Popen, stdout: str, stderr: str
) -> dict[str, object]:
    fields = output_fields(stdout + stderr)
    started = fields.get("started", fields.get("child_spawned", "unknown"))
    duplicate = fields.get("duplicate_attempt", "unknown")
    runtime_failure = fields.get("worker_failure", "-") not in {"", "-"}
    early_death = fields.get("early_death", "-") not in {"", "-"}
    receipt_valid = (
        proc.returncode == 0
        and fields.get("check") == "ok"
        and fields.get("adapter") == leg["adapter"]
        and fields.get("status") == "start"
        and fields.get("attempt_id") == leg["attempt_id"]
        and started in {"0", "1"}
        and duplicate in {"0", "1"}
        and (started, duplicate) in {("1", "0"), ("0", "1")}
        and not runtime_failure
        and not early_death
    )
    if receipt_valid:
        launch_state = "started" if started == "1" else "existing"
        reason = "-"
    else:
        launch_state = "failed"
        reason = fields.get("reason", "invalid-wrapper-receipt")[:160]
    return {
        **leg,
        "exit_code": proc.returncode,
        "child_spawned": started,
        "duplicate_attempt": duplicate,
        "check": fields.get("check", "invalid"),
        "launch_state": launch_state,
        "reason": reason,
    }


def existing_leg_result(
    jobs: Path,
    leg: dict[str, object],
    route: dict[str, object],
    *,
    repo: str,
    parent: str,
    parent_attempt_id: str,
    parallel_group: str,
    declared_size: int,
    manifest_digest: str,
    leg_digest: str,
    agent_home: Path,
) -> dict[str, object] | None:
    """Classify one exact prior attempt before consuming governor capacity.

    ``None`` means the leg is absent or is an open registered-only row that still
    needs its one launch claim. An already claimed active leg and an exact
    completed-marker leg are safe idempotent results. Every other terminal or
    malformed exact row is a typed failure and can never be promoted to
    ``existing`` merely because the wrapper returned ``duplicate_attempt=1``.
    """

    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BatchError("batch-registry-unreadable", str(exc)) from exc
    matches: list[tuple[list[str], dict[str, str]]] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == leg["attempt_id"]:
            matches.append((fields, metadata))
    if not matches:
        return None
    if len(matches) != 1:
        raise BatchError(
            "batch-attempt-row-not-unique",
            f"attempt_id={leg['attempt_id']} rows={len(matches)}",
        )
    fields, metadata = matches[0]
    try:
        validate_attempt_metadata(metadata)
    except DispatchContractError as exc:
        raise BatchError("batch-attempt-row-invalid", exc.detail) from exc
    # The stable launch identity deliberately excludes the display prefix.  If
    # a retry uses a different --slug-prefix, keep the already registered
    # display slug so the wrapper also reuses the row-bound transcript paths.
    # The exact attempt id, route node, parent generation and fallback tuple
    # below remain the authority; a display alias must not turn that same
    # attempt into either a conflict or a second launch.
    leg["slug"] = fields[4]
    expected = {
        "attempt_id": str(leg["attempt_id"]),
        "route_id": str(route["route_id"]),
        "route_node": str(leg["node"]),
        "parent": parent,
        "parent_attempt_id": parent_attempt_id,
        "harness": str(leg["adapter"]),
        "child_harness": str(leg["adapter"]),
        "dispatch_depth": "2",
        "fallback_hop": str(leg["hop"]),
        "fallback_ordinal": str(leg["ordinal"]),
        "parallel_group": parallel_group,
        "replica_group": parallel_group,
        "reservation_kind": "parallel-batch",
        "batch_declared_size": str(declared_size),
        "batch_group": parallel_group,
        "batch_route_id": str(route["route_id"]),
        "batch_parent_attempt_id": parent_attempt_id,
        "batch_attempt_id": str(leg["attempt_id"]),
        "batch_route_node": str(leg["node"]),
        "batch_harness": str(leg["adapter"]),
        "batch_fallback_hop": str(leg["hop"]),
        "batch_fallback_ordinal": str(leg["ordinal"]),
        "batch_independence": str(leg["independence"]),
        "batch_assignment_sha256": str(leg["assignment_sha256"]),
        "batch_manifest_sha256": manifest_digest,
        "batch_leg_sha256": leg_digest,
    }
    mismatches = {
        key: (value, metadata.get(key, ""))
        for key, value in expected.items()
        if metadata.get(key) != value
    }
    if metadata.get("batch_admission_count") not in {"1", str(declared_size)}:
        mismatches["batch_admission_count"] = (
            f"1|{declared_size}", metadata.get("batch_admission_count", "")
        )
    if metadata.get("batch_admission_count") == "1":
        for key in ("batch_peer_count", "batch_peer_set_sha256"):
            if not metadata.get(key):
                mismatches[key] = ("partial-recovery-peer-proof", "")
        if metadata.get("batch_peer_count") != str(declared_size - 1):
            mismatches["batch_peer_count"] = (
                str(declared_size - 1), metadata.get("batch_peer_count", "")
            )
        proof = metadata.get("batch_peer_set_sha256", "")
        if not DIGEST.fullmatch(proof):
            mismatches["batch_peer_set_sha256"] = (
                "sha256:<64 lowercase hex>", proof
            )
    if (
        fields[2] != repo
        or os.path.realpath(fields[3]) != os.path.realpath(str(route["cwd"]))
        or mismatches
    ):
        detail = ";".join(
            f"{key}:expected={expected_value}:actual={actual_value}"
            for key, (expected_value, actual_value) in sorted(mismatches.items())
        )
        raise BatchError(
            "batch-attempt-identity-conflict",
            detail or f"attempt_id={leg['attempt_id']}",
        )

    claimed = metadata.get("launch_claimed")
    if fields[1] == "open" and claimed == "0":
        return None
    common = {
        **leg,
        "exit_code": 0,
        "child_spawned": "0",
        "duplicate_attempt": "1",
    }
    if fields[1] in {"open", "running"} and claimed == "1":
        process = attempt_process_quiescence(metadata)
        if (
            process.state == "quiescent"
            and metadata.get("launch_fence") == "registry-v1"
            and metadata.get("launch_started") != "1"
            and not metadata.get("launch_outcome")
            and recover_unstarted_attempt(jobs, str(leg["attempt_id"]))
        ):
            return None
        if process.state != "live":
            return {
                **common,
                "exit_code": 70,
                "check": "failed",
                "launch_state": "failed",
                "reason": "existing-active-attempt-" + process.reason,
            }
        return {
            **common,
            "check": "ok",
            "launch_state": "existing",
            "reason": "existing-active-attempt",
        }
    if (
        fields[1] == "done"
        and claimed == "1"
        and metadata.get("note") == "completed-marker"
    ):
        node = next(
            (
                candidate for candidate in route.get("nodes", [])
                if isinstance(candidate, dict) and candidate.get("id") == leg["node"]
            ),
            None,
        )
        marker_path = next(
            (
                candidate
                for candidate in (
                    root / "completion" / str(route["route_id"]) / f"{leg['node']}.json"
                    for root in dispatch_state_roots(agent_home, jobs)
                )
                if candidate.is_file()
            ),
            resolve_dispatch_state_root(agent_home, jobs)
            / "completion" / str(route["route_id"]) / f"{leg['node']}.json",
        )
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marker = None
        if (
            isinstance(node, dict)
            and isinstance(marker, dict)
            and completion_marker_is_current(route, node, marker_path, marker)
        ):
            readiness = completion_attempt_readiness(
                route, node, marker, jobs, registry_lines=lines
            )
            if readiness.state == "ready":
                return {
                    **common,
                    "check": "ok",
                    "launch_state": "existing",
                    "reason": "existing-completed-attempt",
                }
            terminal_reason = readiness.reason
        else:
            terminal_reason = "completion-marker-not-current"
        return {
            **common,
            "exit_code": 70,
            "check": "failed",
            "launch_state": "failed",
            "reason": "existing-completed-attempt-" + terminal_reason,
        }
    return {
        **common,
        "exit_code": 70,
        "check": "failed",
        "launch_state": "failed",
        "reason": "existing-terminal-attempt-" + (metadata.get("note") or fields[1]),
    }


def batch_receipt(
    *,
    args: argparse.Namespace,
    lifecycle: str,
    independence: str,
    required_axes: list[str],
    realized_axes: list[str],
    degradation_reason: str,
    legs: list[dict[str, object]],
    results: list[dict[str, object]],
    admitted: int,
    interrupted_signal: int = 0,
    selection_diagnostics: dict[str, object] | None = None,
) -> tuple[dict[str, object], bool]:
    order = {str(leg["attempt_id"]): index for index, leg in enumerate(legs)}
    results.sort(key=lambda leg: order[str(leg["attempt_id"])])
    started_count = sum(leg.get("launch_state") == "started" for leg in results)
    existing_count = sum(leg.get("launch_state") == "existing" for leg in results)
    success = (
        not interrupted_signal
        and len(results) == len(legs)
        and started_count + existing_count == len(legs)
    )
    if interrupted_signal:
        state = "interrupted"
    elif not success:
        state = "partial-failure"
    elif existing_count:
        state = "idempotent-existing" if not started_count else "idempotent-mixed"
    else:
        state = "launched"
    receipt = {
        "schema_version": 2,
        "state": state,
        "action": "start",
        "parallel_group": args.parallel_group,
        "replica_group": args.parallel_group,
        "independence": independence,
        "required_independence_axes": required_axes,
        "realized_independence_axes": realized_axes,
        "degradation_reason": degradation_reason,
        "concurrent_launch": int(started_count == len(legs)),
        "launch_lifecycle": lifecycle,
        "requested": len(legs),
        "admitted": admitted,
        "newly_started": started_count,
        "existing": existing_count,
        "legs": results,
    }
    if selection_diagnostics is not None:
        receipt["selection_diagnostics"] = selection_diagnostics
    if interrupted_signal:
        receipt["signal"] = interrupted_signal
    return receipt, success


def _record_failed_legs(route, results, agent_home):
    """Record failed receipt legs once, after receipt assembly, without changing it."""
    paths = []
    for leg in results:
        if leg.get("check") != "failed" and leg.get("launch_state") != "failed":
            continue
        path = record_degradation(
            route_id=route.get("route_id"), route_node=leg.get("node"),
            route_hash=route.get("route_hash"), dispatch_depth=2,
            fallback_hop=leg.get("hop"), execution_surface="registered-headless",
            writer="dispatch-batch.py", kind="leg-failure",
            parallel_group=leg.get("parallel_group"),
            parallel_leg_index=leg.get("parallel_leg_index"),
            parallel_leg_count=leg.get("parallel_leg_count") or len(results),
            attempt_id=leg.get("attempt_id"), exit_code=leg.get("exit_code"),
            launch_state=leg.get("launch_state"), harness=leg.get("adapter") or leg.get("harness"),
            reason=leg.get("reason") or "leg-failure",
        )
        if path:
            paths.append(path)
    return paths[-1] if paths else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", type=Path, required=True)
    parser.add_argument("--parallel-group")
    parser.add_argument("--replica-group")
    parser.add_argument("--action", choices=("dry-run", "start"), default="dry-run")
    parser.add_argument("--slug-prefix", required=True)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--qa", default="standard")
    parser.add_argument("--jobs", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument(
        "--prompt-text",
        default=DEFAULT_PROMPT,
    )
    parser.add_argument("--allow-degraded-independence", action="store_true")
    args = parser.parse_args(argv)
    if args.parallel_group and args.replica_group and args.parallel_group != args.replica_group:
        parser.error("--parallel-group and --replica-group aliases must match")
    args.parallel_group = args.parallel_group or args.replica_group
    if not args.parallel_group:
        parser.error("one of --parallel-group or --replica-group is required")
    args.replica_group = args.parallel_group

    agent_home = None
    route: dict[str, object] = {}
    try:
        route_path = args.route.resolve()
        route = load_route(route_path)
        nodes = parallel_nodes(route, args.parallel_group)
        parent_identity = DISPATCH_NODE.current_parent_identity()
        if parent_identity is None:
            raise BatchError("parent-runtime-identity-missing")
        agent_home = resolve_agent_home()
        jobs = resolve_global_registry(
            agent_home,
            str(args.jobs) if args.jobs else os.environ.get("AGENT_DISPATCH_JOBS"),
            2,
            args.action,
        ).path
        if args.log_dir is not None:
            args.log_dir = validate_dispatch_log_dir(jobs, args.log_dir)
        assignments, independence, diagnostics = assign_harnesses(
            route,
            nodes,
            allow_degraded=args.allow_degraded_independence,
            parent_identity=parent_identity,
            jobs=jobs,
        )
        self_slug = os.environ.get("AGENT_DISPATCH_SELF_SLUG", "")
        parent_attempt = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
        if not self_slug or args.parent != self_slug or not parent_attempt:
            raise BatchError("parent-identity-mismatch", f"parent={args.parent} self={self_slug or '-'}")
        repo = subprocess.check_output(
            ["git", "-C", str(route["cwd"]), "rev-parse", "--show-toplevel"],
            text=True,
        ).strip()
        resolve_live_parent_attempt(
            jobs,
            parent_slug=args.parent,
            repo=repo,
            worktree=str(route["cwd"]),
            expected_attempt_id=parent_attempt,
            expected_harness=parent_identity["parent_harness"],
            expected_transport=parent_identity["parent_transport"],
            expected_sandbox=parent_identity["parent_sandbox"],
        )
        for node, _, _, _ in assignments:
            completion_marker_gate(
                str(route_path), str(node["id"]), args.action, agent_home, jobs
            )
    except (
        BatchError,
        DispatchContractError,
        DISPATCH_NODE.DispatchNodeError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        reason = getattr(exc, "reason", "batch-validation-failed")
        detail = getattr(exc, "detail", str(exc))
        return fail(reason, 78 if reason in PRELAUNCH_PROCESS_BLOCK_REASONS else 65, detail=detail)

    lifecycle = select_launch_lifecycle()
    required_axes = list(nodes[0].get("parallel_independence_axes", ["cross-harness"]))
    realized_axes = []
    if len({adapter for _, adapter, _, _ in assignments}) >= 2:
        realized_axes.append("cross-harness")
    if len({str(node.get("model_profile")) for node, _, _, _ in assignments}) >= 2:
        realized_axes.append("model-profile")
    if len({str(node.get("perspective")) for node, _, _, _ in assignments}) == len(nodes):
        realized_axes.append("perspective")
    diagnostics["independence_axis_delta"] = [
        axis for axis in required_axes if axis not in realized_axes
    ]
    degradation_reason = (
        "" if independence == "cross-harness"
        else "cross-harness-unavailable-user-allowed"
    )
    assignment_digest = "sha256:" + hashlib.sha256(
        args.prompt_text.encode("utf-8")
    ).hexdigest()
    legs = []
    for node, adapter, hop, ordinal in assignments:
        node_id = str(node["id"])
        slug = parallel_slug(args.slug_prefix, node_id)
        attempt_id = stable_attempt_id(
            route,
            node,
            slug,
            args.parent,
            parent_attempt,
            adapter,
            ordinal,
        )
        legs.append(
            {
                "node": node_id,
                "adapter": adapter,
                "hop": hop,
                "ordinal": ordinal,
                "slug": slug,
                "attempt_id": attempt_id,
                "assignment_sha256": assignment_digest,
                "independence": independence,
                "model_profile": str(node.get("model_profile")),
                "perspective": str(node.get("perspective")),
                "parallel_leg_index": int(node.get("parallel_leg_index", len(legs))),
            }
        )
    manifest, manifest_digest, leg_digests = build_manifest(
        parallel_group=args.parallel_group,
        route_id=str(route["route_id"]),
        parent_attempt_id=parent_attempt,
        independence=independence,
        required_independence_axes=required_axes,
        realized_independence_axes=realized_axes,
        degradation_reason=degradation_reason,
        members=[
            {
                "assignment_sha256": assignment_digest,
                "attempt_id": str(leg["attempt_id"]),
                "route_node": str(leg["node"]),
                "harness": str(leg["adapter"]),
                "fallback_hop": str(leg["hop"]),
                "fallback_ordinal": int(leg["ordinal"]),
                "model_profile": str(leg["model_profile"]),
                "perspective": str(leg["perspective"]),
                "parallel_leg_index": int(leg["parallel_leg_index"]),
            }
            for leg in legs
        ],
    )

    if args.action != "start":
        print(json.dumps({
            "schema_version": 2,
            "state": "validated",
            "action": args.action,
            "parallel_group": args.parallel_group,
            "replica_group": args.parallel_group,
            "independence": independence,
            "required_independence_axes": required_axes,
            "realized_independence_axes": realized_axes,
            "degradation_reason": degradation_reason,
            "launch_lifecycle": lifecycle,
            "legs": legs,
            "selection_diagnostics": diagnostics,
        }, separators=(",", ":"), sort_keys=True))
        return 0

    governor = ROOT / "utilities" / "model-worker-governor.py"
    artifact_root = Path(
        os.environ.get("AGENT_ARTIFACT_ROOT", str(agent_home / ".agent_reports"))
    )
    try:
        governor_root = resolve_model_governor_root(artifact_root)
    except DispatchContractError as exc:
        return fail(exc.reason, 73, detail=exc.detail, admitted=0, spawned=0)
    results: list[dict[str, object]] = []
    pending_legs: list[dict[str, object]] = []
    try:
        for leg in legs:
            existing = existing_leg_result(
                jobs,
                leg,
                route,
                repo=repo,
                parent=args.parent,
                parent_attempt_id=parent_attempt,
                parallel_group=args.parallel_group,
                declared_size=len(legs),
                manifest_digest=manifest_digest,
                leg_digest=leg_digests[str(leg["attempt_id"])],
                agent_home=agent_home,
            )
            if existing is None:
                pending_legs.append(leg)
            else:
                results.append(existing)
    except BatchError as exc:
        return fail(exc.reason, 73, detail=exc.detail, admitted=0, spawned=0)

    # A stable failed attempt cannot be relaunched under the same identity. Do
    # not start an absent sibling and turn a prior terminal failure into a new
    # partial batch.
    if any(result.get("launch_state") == "failed" for result in results):
        for leg in pending_legs:
            results.append({
                **leg,
                "exit_code": 70,
                "child_spawned": "0",
                "duplicate_attempt": "0",
                "check": "failed",
                "launch_state": "failed",
                "reason": "batch-peer-terminal-attempt",
            })
        receipt, _ = batch_receipt(
            args=args,
            lifecycle=lifecycle,
            independence=independence,
            required_axes=required_axes,
            realized_axes=realized_axes,
            degradation_reason=degradation_reason,
            legs=legs,
            results=results,
            admitted=0,
            selection_diagnostics=diagnostics,
        )
        receipt["degradation_ledger"] = _record_failed_legs(route, results, agent_home) or "-"
        print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
        return 70

    tokens: list[str] = []
    processes: list[tuple[dict[str, object], str, subprocess.Popen]] = []
    with BatchSignalRelay() as relay:
        if pending_legs:
            try:
                peers = None
                if len(pending_legs) == 1:
                    existing_peers = [
                        result for result in results
                        if result.get("launch_state") == "existing"
                    ]
                    if len(existing_peers) != len(legs) - 1:
                        raise BatchError(
                            "parallel-partial-peer-set-missing",
                            f"existing={len(existing_peers)}",
                        )
                    peers = [
                        {
                            "agent_home": str(agent_home.resolve(strict=False)),
                            "attempt_id": str(existing_peer["attempt_id"]),
                            "jobs": str(jobs.resolve(strict=False)),
                            "route": str(route_path),
                        }
                        for existing_peer in existing_peers
                    ]
                tokens = reserve_batch(
                    governor,
                    governor_root,
                    pending_legs,
                    manifest=manifest,
                    manifest_digest=manifest_digest,
                    peers=peers,
                )
            except BatchError as exc:
                return fail(
                    exc.reason,
                    75,
                    detail=exc.detail,
                    admitted=0,
                    spawned=0,
                    existing=len(results),
                )

        for leg, token in zip(pending_legs, tokens):
            if relay.received:
                cancel_unclaimed(governor, governor_root, token)
                results.append({
                    **leg,
                    "exit_code": 128 + relay.received[-1],
                    "child_spawned": "0",
                    "duplicate_attempt": "0",
                    "check": "failed",
                    "launch_state": "failed",
                    "reason": "batch-interrupted-before-wrapper",
                })
                continue
            command = [
                sys.executable,
                str(ROOT / "utilities" / "dispatch-node.py"),
                "--route",
                str(route_path),
                "--node",
                str(leg["node"]),
                "--adapter",
                str(leg["adapter"]),
                "--action",
                "start",
                "--slug",
                str(leg["slug"]),
                "--qa",
                args.qa,
                "--parent",
                args.parent,
                "--prompt-text",
                args.prompt_text,
                "--attempt-id",
                str(leg["attempt_id"]),
                "--jobs",
                str(jobs),
                "--",
                "--parent-attempt-id",
                parent_attempt,
                *(
                    ["--log-dir", str(args.log_dir)]
                    if args.log_dir is not None
                    else []
                ),
                "--launch-lifecycle",
                lifecycle,
                "--fallback-hop",
                str(leg["hop"]),
                "--fallback-ordinal",
                str(leg["ordinal"]),
            ]
            env = {
                # This launches a depth-2 node (dispatch-node.py), which
                # always supplies its own --route via `command` above. An
                # inherited AGENT_OWNER_ROUTE_* triple (the calling owner's
                # own identity, per dispatch-owner.py) must not ride along:
                # the adapter wrapper rejects a node launch that carries an
                # owner route binding alongside an explicit route file.
                key: value
                for key, value in os.environ.items()
                if not key.startswith("AGENT_OWNER_ROUTE_")
            }
            env.update({
                GOVERNOR_RESERVATION_ENV: token,
                "AGENT_MODEL_GOVERNOR_ROOT": str(governor_root),
                "AGENT_DISPATCH_JOBS": str(jobs),
            })
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                relay.add(proc)
                processes.append((leg, token, proc))
            except OSError:
                cancel_unclaimed(governor, governor_root, token)
                results.append(
                    {
                        **leg,
                        "exit_code": 70,
                        "child_spawned": "0",
                        "duplicate_attempt": "0",
                        "check": "failed",
                        "launch_state": "failed",
                        "reason": "parallel-wrapper-spawn-failed",
                    }
                )

        if processes:
            with ThreadPoolExecutor(max_workers=len(processes)) as executor:
                futures = {
                    executor.submit(collect_wrapper, item): item
                    for item in processes
                }
                for future in as_completed(futures):
                    original_leg, token, proc = futures[future]
                    try:
                        leg, _, proc, stdout, stderr = future.result()
                        result = wrapper_result(leg, proc, stdout, stderr)
                        if result["launch_state"] == "existing":
                            checked = existing_leg_result(
                                jobs,
                                leg,
                                route,
                                repo=repo,
                                parent=args.parent,
                                parent_attempt_id=parent_attempt,
                                parallel_group=args.parallel_group,
                                declared_size=len(legs),
                                manifest_digest=manifest_digest,
                                leg_digest=leg_digests[str(leg["attempt_id"])],
                                agent_home=agent_home,
                            )
                            if checked is None:
                                result.update(
                                    check="failed",
                                    launch_state="failed",
                                    reason="duplicate-attempt-row-unclaimed",
                                )
                            else:
                                result = checked
                        results.append(result)
                    except Exception as exc:  # retain a typed batch receipt
                        stop_wrapper(proc)
                        results.append({
                            **original_leg,
                            "exit_code": proc.returncode if proc.returncode is not None else 70,
                            "child_spawned": "unknown",
                            "duplicate_attempt": "unknown",
                            "check": "failed",
                            "launch_state": "failed",
                            "reason": "parallel-wrapper-collect-failed:" + type(exc).__name__,
                        })
                    finally:
                        cancel_unclaimed(governor, governor_root, token)

        interrupted_signal = relay.received[-1] if relay.received else 0

    receipt, success = batch_receipt(
        args=args,
        lifecycle=lifecycle,
        independence=independence,
        required_axes=required_axes,
        realized_axes=realized_axes,
        degradation_reason=degradation_reason,
        legs=legs,
        results=results,
        admitted=len(tokens),
        interrupted_signal=interrupted_signal,
        selection_diagnostics=diagnostics,
    )
    _persist_launched_degradation(agent_home, route, diagnostics, results)
    receipt["degradation_ledger"] = _record_failed_legs(route, results, agent_home) or "-"
    print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    if interrupted_signal:
        return 128 + interrupted_signal
    return 0 if success else 70


if __name__ == "__main__":
    raise SystemExit(main())
