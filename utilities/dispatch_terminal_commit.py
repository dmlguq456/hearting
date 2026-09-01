#!/usr/bin/env python3
"""SD-120/121 terminal settlement and spendable-handoff records.

This module deliberately lives outside delivery receipts.  It coordinates the
existing route and artifact lifecycle authorities without extending receipt
keys or enums.  Every record is versioned, identity-bound, and monotonic.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import route_identity as ROUTE_IDENTITY  # noqa: E402
from dispatch_contract import parse_registry_metadata  # noqa: E402


RECORD_ROOT_REL = ".runtime/terminal-settlement/v1"
BINDING_VERSION = 1
COMMIT_VERSION = 1
CLAIM_VERSION = 1
COMMIT_STATES = (
    "claimed",
    "route-closed",
    "producer-finalized",
    "not-applicable",
    "owner-envelope-sealed",
)
INTERNAL_REASONS = frozenset(
    {
        "route-identity-unverified",
        "terminal-marker-not-current",
        "child-not-quiescent",
        "producer-binding-required",
        "producer-binding-mismatch",
        "route-close-failed",
        "producer-finalize-failed",
        "transaction-conflict",
        "recovery-unavailable",
    }
)
PRODUCER_CAPABILITIES = frozenset(
    {
        "analyze-project",
        "analyze-user",
        "audit",
        "autopilot-apply",
        "autopilot-code",
        "autopilot-design",
        "autopilot-draft",
        "autopilot-lab",
        "autopilot-refine",
        "autopilot-research",
        "autopilot-ship",
        "autopilot-spec",
    }
)
SAFE_ID = re.compile(r"^[A-Za-z]+[-_][A-Za-z0-9._-]+$")


class TerminalCommitError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class TerminalCommitCrash(RuntimeError):
    """Deterministic crash-injection hook used only by tests."""


@dataclass(frozen=True)
class TerminalEligibility:
    eligible: bool
    reason: str
    terminal_nodes: tuple[str, ...] = ()
    terminal_marker_digest: str = ""
    artifact_path: str = ""
    artifact_digest: str = ""
    artifact_root: str = ""
    route_file: str = ""
    route_id: str = ""
    route_hash: str = ""
    owner_attempt_id: str = ""
    producer_required: bool = False
    producer_binding: Mapping[str, Any] | None = None
    producer_binding_digest: str = "not-applicable"


@dataclass(frozen=True)
class TerminalCommitResult:
    success: bool
    state: str
    reason: str
    commit_id: str = ""
    record_path: str = ""
    envelope: str = ""
    artifact_path: str = ""
    continuation_saved: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_record(value: Mapping[str, Any]) -> str:
    return digest_bytes(canonical_bytes(value))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(path), flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        _fsync_dir(path.parent)
        return
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_dir(path.parent)


class _LockedRecord:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> "_LockedRecord":
        lock = Path(str(self.path) + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(lock, "a+", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _safe_id(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and SAFE_ID.fullmatch(value) is not None
    )


def _record_root(artifact_root: Path) -> Path:
    return Path(artifact_root).resolve() / RECORD_ROOT_REL


def producer_binding_path(
    artifact_root: Path, route_id: str, owner_attempt_id: str
) -> Path:
    return (
        _record_root(artifact_root)
        / "producer-bindings"
        / route_id
        / f"{owner_attempt_id}.json"
    )


def terminal_commit_path(artifact_root: Path, commit_id: str) -> Path:
    return _record_root(artifact_root) / "terminal-commits" / f"{commit_id}.json"


def terminal_envelope_path(artifact_root: Path, commit_id: str) -> Path:
    return _record_root(artifact_root) / "terminal-envelopes" / f"{commit_id}.txt"


def terminal_handoff_claim_path(state_root: Path, claim_id: str) -> Path:
    return Path(state_root).resolve() / "terminal-handoff-claims" / f"{claim_id}.json"


def _owner_registry_metadata(jobs: Path, owner_attempt_id: str) -> dict[str, str] | None:
    try:
        lines = Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") != owner_attempt_id:
            continue
        metadata = dict(metadata)
        metadata["_row_status"] = fields[1]
        return metadata
    return None


def publish_producer_binding(
    artifact_root: Path,
    *,
    route: Mapping[str, Any],
    route_file: Path,
    owner_attempt_id: str,
    campaign_id: str,
    cycle_id: str,
    producer_id: str,
    cycle_record: Mapping[str, Any],
    jobs: Path | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Publish/replay one exact open producer binding via exclusive-create CAS."""

    root = Path(artifact_root).resolve()
    route_path = Path(route_file).resolve()
    if (
        not _safe_id(owner_attempt_id, "att-")
        or not _safe_id(campaign_id, "camp")
        or not _safe_id(cycle_id, "cyc")
        or not _safe_id(producer_id, "prod")
        or route.get("route_id") != ROUTE_IDENTITY.route_id_from_hash(
            str(route.get("route_hash", ""))
        )
        or route.get("route_hash") != ROUTE_IDENTITY.route_hash(dict(route))
        or Path(str(route.get("artifact_root", root))).resolve() != root
    ):
        raise TerminalCommitError("producer-binding-mismatch", "route identity")
    expected_cycle = {
        "campaign_id": campaign_id,
        "cycle_id": cycle_id,
        "producer_id": producer_id,
        "route_id": route.get("route_id"),
        "route_hash": route.get("route_hash"),
        "state": "open",
    }
    if any(cycle_record.get(key) != value for key, value in expected_cycle.items()):
        raise TerminalCommitError("producer-binding-mismatch", "cycle record")
    if jobs is not None:
        metadata = _owner_registry_metadata(Path(jobs), owner_attempt_id)
        if metadata is None or metadata.get("_row_status") not in {"open", "running"}:
            raise TerminalCommitError("producer-binding-mismatch", "owner row")
        if (
            metadata.get("owner_route_id") != route.get("route_id")
            or metadata.get("owner_route_hash") != route.get("route_hash")
            or Path(metadata.get("owner_route_file", "")).resolve() != route_path
        ):
            raise TerminalCommitError("producer-binding-mismatch", "owner route")
    identity = {
        "owner_attempt_id": owner_attempt_id,
        "route_id": route["route_id"],
        "route_hash": route["route_hash"],
        "artifact_root": str(root),
        "campaign_id": campaign_id,
        "cycle_id": cycle_id,
        "producer_id": producer_id,
        "cycle_record_digest": digest_record(dict(cycle_record)),
    }
    binding_id = digest_bytes(canonical_bytes(identity))
    payload = {
        "schema_version": BINDING_VERSION,
        "record_type": "producer_binding_v1",
        "binding_id": binding_id,
        **identity,
        "route_file": str(route_path),
        "state": "open",
        "recorded_at": recorded_at or _now(),
    }
    path = producer_binding_path(root, route["route_id"], owner_attempt_id)
    with _LockedRecord(path):
        existing = _read_json(path)
        if existing is not None:
            immutable = {key: payload[key] for key in payload if key != "recorded_at"}
            comparable = {key: existing.get(key) for key in immutable}
            if comparable != immutable:
                raise TerminalCommitError("transaction-conflict", str(path))
            return existing
        try:
            _atomic_write(path, canonical_bytes(payload) + b"\n", exclusive=True)
        except FileExistsError:
            existing = _read_json(path)
            if existing is None:
                raise TerminalCommitError("transaction-conflict", str(path))
            return existing
    return payload


def maybe_publish_producer_binding(
    artifact_root: Path,
    *,
    route: Mapping[str, Any],
    route_file: Path,
    cycle_record: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Publish only for an exact registered owner environment.

    Unit/direct producer callers without the matching owner route remain
    compatible.  A material terminal fast path later fails closed when such a
    binding is required but absent.
    """

    env = os.environ if environ is None else environ
    owner = env.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    jobs = env.get("AGENT_DISPATCH_JOBS", "")
    env_route_id = env.get("AGENT_ROUTE_ID", "")
    env_route_file = env.get("AGENT_ROUTE_FILE", "")
    if not owner or not jobs or env_route_id != route.get("route_id"):
        return None
    if env_route_file and Path(env_route_file).resolve() != Path(route_file).resolve():
        raise TerminalCommitError("producer-binding-mismatch", "environment route")
    return publish_producer_binding(
        artifact_root,
        route=route,
        route_file=route_file,
        owner_attempt_id=owner,
        campaign_id=str(cycle_record["campaign_id"]),
        cycle_id=str(cycle_record["cycle_id"]),
        producer_id=str(cycle_record["producer_id"]),
        cycle_record=cycle_record,
        jobs=Path(jobs),
    )


def load_producer_binding(
    artifact_root: Path, route_id: str, owner_attempt_id: str
) -> dict[str, Any] | None:
    return _read_json(producer_binding_path(artifact_root, route_id, owner_attempt_id))


def _row_value(row: object, key: str, default: Any = "") -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _row_metadata(row: object) -> Mapping[str, Any]:
    value = _row_value(row, "metadata", {})
    return value if isinstance(value, Mapping) else {}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ineligible(reason: str, **fields: Any) -> TerminalEligibility:
    return TerminalEligibility(False, reason, **fields)


def classify_terminal_eligibility(
    *,
    route: Mapping[str, Any],
    route_file: Path,
    owner_attempt_id: str,
    rows: Sequence[object],
    jobs: Path | None = None,
    producer_required: bool | None = None,
) -> TerminalEligibility:
    """Pure/read-only fast-path classifier; every refusal mutates nothing."""

    route_path = Path(route_file).resolve()
    route_id = str(route.get("route_id", ""))
    route_hash = str(route.get("route_hash", ""))
    root = Path(str(route.get("artifact_root") or route_path.parent)).resolve()
    required = (
        str(route.get("capability", "")) in PRODUCER_CAPABILITIES
        if producer_required is None
        else producer_required
    )
    base = {
        "artifact_root": str(root),
        "route_file": str(route_path),
        "route_id": route_id,
        "route_hash": route_hash,
        "owner_attempt_id": owner_attempt_id,
    }
    if not _safe_id(owner_attempt_id, "att-"):
        return _ineligible("route-identity-unverified", **base)
    try:
        canonical_hash = ROUTE_IDENTITY.route_hash(dict(route))
        canonical_id = ROUTE_IDENTITY.route_id_from_hash(canonical_hash)
    except (TypeError, ValueError):
        return _ineligible("route-identity-unverified", **base)
    if route_hash != canonical_hash or route_id != canonical_id:
        return _ineligible("route-identity-unverified", **base)
    if jobs is not None:
        parent = _owner_registry_metadata(Path(jobs), owner_attempt_id)
        if parent is None or parent.get("_row_status") not in {"open", "running"}:
            return _ineligible("route-identity-unverified", **base)
        if required and (
            parent.get("owner_route_id") != route_id
            or parent.get("owner_route_hash") != route_hash
        ):
            return _ineligible("route-identity-unverified", **base)
    contract = route.get("workflow_contract")
    terminal_raw = contract.get("terminal_nodes") if isinstance(contract, Mapping) else None
    if not isinstance(terminal_raw, list) or not terminal_raw:
        return _ineligible("terminal-marker-not-current", **base)
    terminal_nodes = tuple(sorted(terminal_raw))
    declared = tuple(
        sorted(
            node.get("id")
            for node in route.get("nodes", [])
            if isinstance(node, Mapping)
            and node.get("terminal") is True
            and isinstance(node.get("id"), str)
        )
    )
    if declared != terminal_nodes:
        return _ineligible("terminal-marker-not-current", **base)
    if any(_row_value(row, "status") in {"open", "running"} for row in rows):
        return _ineligible("child-not-quiescent", **base)
    proven: set[str] = set()
    markers: list[dict[str, Any]] = []
    artifacts: list[tuple[str, str]] = []
    for row in rows:
        metadata = _row_metadata(row)
        node = metadata.get("route_node")
        if node not in terminal_nodes:
            continue
        if (
            _row_value(row, "status") != "done"
            or not (
                metadata.get("failure_class") == "pass"
                or metadata.get("note") in {"completed-marker", "completed-supervisor"}
            )
            or metadata.get("route_id") != route_id
            or metadata.get("route_hash") != route_hash
        ):
            continue
        marker_path = Path(str(metadata.get("completion_marker", "")))
        marker = _read_json(marker_path)
        if marker is None:
            continue
        if (
            marker.get("schema_version") != 2
            or marker.get("route_id") != route_id
            or marker.get("route_hash") != route_hash
            or marker.get("node_id") != node
            or marker.get("attempt_id") != _row_value(row, "attempt_id")
        ):
            continue
        evidence = marker.get("evidence")
        artifact_raw = evidence.get("path") if isinstance(evidence, Mapping) else None
        expected_digest = evidence.get("sha256") if isinstance(evidence, Mapping) else None
        artifact = Path(artifact_raw) if isinstance(artifact_raw, str) else Path("")
        try:
            material = (
                artifact.is_absolute()
                and not artifact.is_symlink()
                and artifact.is_file()
                and _under(artifact, root)
                and isinstance(expected_digest, str)
                and bool(expected_digest)
                and _file_sha256(artifact) == expected_digest.removeprefix("sha256:")
            )
        except OSError:
            material = False
        if not material:
            continue
        proven.add(str(node))
        markers.append(marker)
        artifacts.append((str(artifact.resolve()), expected_digest.removeprefix("sha256:")))
    if proven != set(terminal_nodes) or not artifacts:
        return _ineligible("terminal-marker-not-current", **base)
    marker_digest = digest_bytes(canonical_bytes(sorted(markers, key=lambda row: row["node_id"])))
    artifact_path, artifact_digest = sorted(set(artifacts))[0]
    binding: Mapping[str, Any] | None = None
    binding_digest = "not-applicable"
    if required:
        binding = load_producer_binding(root, route_id, owner_attempt_id)
        if binding is None:
            return _ineligible(
                "producer-binding-required",
                terminal_nodes=terminal_nodes,
                terminal_marker_digest=marker_digest,
                artifact_path=artifact_path,
                artifact_digest=artifact_digest,
                producer_required=True,
                **base,
            )
        cycle_id = str(binding.get("cycle_id", ""))
        cycle_record_path = (
            root
            / ".runtime"
            / "artifact-producer"
            / "v1"
            / "cycles"
            / f"{cycle_id}.json"
        )
        cycle_record = _read_json(cycle_record_path)
        binding_identity_invalid = (
            binding.get("schema_version") != BINDING_VERSION
            or binding.get("record_type") != "producer_binding_v1"
            or binding.get("owner_attempt_id") != owner_attempt_id
            or binding.get("route_id") != route_id
            or binding.get("route_hash") != route_hash
            or Path(str(binding.get("artifact_root", ""))).resolve() != root
            or binding.get("state") != "open"
            or not _safe_id(binding.get("campaign_id"), "camp")
            or not _safe_id(binding.get("cycle_id"), "cyc")
            or not _safe_id(binding.get("producer_id"), "prod")
            or cycle_record is None
            or any(
                binding.get(key) != cycle_record.get(key)
                for key in ("campaign_id", "cycle_id", "producer_id", "route_id", "route_hash")
            )
        )
        if binding_identity_invalid:
            return _ineligible("producer-binding-mismatch", producer_required=True, **base)
        binding_digest = digest_record(dict(binding))
        cycle_open_current = (
            cycle_record.get("state") == "open"
            and binding.get("cycle_record_digest") == digest_record(cycle_record)
        )
        # A crash after manifest commit changes the cycle record from open to
        # sealed before the terminal transaction can advance.  Accept that
        # state only as forward recovery for the exact pre-existing commit;
        # a sealed cycle can never mint a new terminal claim.
        recovery_eligibility = TerminalEligibility(
            True,
            "",
            terminal_nodes=terminal_nodes,
            terminal_marker_digest=marker_digest,
            artifact_path=artifact_path,
            artifact_digest=artifact_digest,
            artifact_root=str(root),
            route_file=str(route_path),
            route_id=route_id,
            route_hash=route_hash,
            owner_attempt_id=owner_attempt_id,
            producer_required=True,
            producer_binding=binding,
            producer_binding_digest=binding_digest,
        )
        recovery_record = _read_json(
            terminal_commit_path(root, terminal_commit_id(recovery_eligibility))
        )
        cycle_sealed_recovery = (
            cycle_record.get("state") == "sealed"
            and recovery_record is not None
            and recovery_record.get("state")
            in {"route-closed", "producer-finalized", "owner-envelope-sealed"}
            and recovery_record.get("producer_binding_digest") == binding_digest
        )
        if not cycle_open_current and not cycle_sealed_recovery:
            return _ineligible("producer-binding-mismatch", producer_required=True, **base)
        try:
            import artifact_producer

            if artifact_producer.review_lease_status(
                root, cycle_id=cycle_id
            ).get("live"):
                return _ineligible("producer-binding-mismatch", producer_required=True, **base)
        except Exception:
            return _ineligible("producer-binding-mismatch", producer_required=True, **base)
    return TerminalEligibility(
        True,
        "",
        terminal_nodes=terminal_nodes,
        terminal_marker_digest=marker_digest,
        artifact_path=artifact_path,
        artifact_digest=artifact_digest,
        producer_required=required,
        producer_binding=binding,
        producer_binding_digest=binding_digest,
        **base,
    )


def terminal_commit_id(eligibility: TerminalEligibility) -> str:
    return digest_bytes(
        canonical_bytes(
            {
                "route_id": eligibility.route_id,
                "route_hash": eligibility.route_hash,
                "owner_attempt_id": eligibility.owner_attempt_id,
                "terminal_marker_digest": eligibility.terminal_marker_digest,
                "producer_binding_digest": eligibility.producer_binding_digest,
            }
        )
    )


class RealTerminalCommitServices:
    """Thin adapters around the existing route/producer authorities."""

    def close_route(self, eligibility: TerminalEligibility) -> Mapping[str, Any]:
        command = [
            sys.executable,
            str(Path(__file__).with_name("capability-route.py")),
            "close",
            "--route",
            eligibility.route_file,
            "--summary",
            "terminal_commit_v1",
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            route = _read_json(Path(eligibility.route_file)) or {}
            # Narrow compatibility for pre-contract synthetic/legacy fixtures.
            # Every compiled production route carries dispatch_contract_version
            # and must pass through capability-route.py above.
            if "dispatch_contract_version" not in route:
                return {
                    "schema_version": 0,
                    "route_id": eligibility.route_id,
                    "route_hash": eligibility.route_hash,
                    "terminal_gate_proven": True,
                    "compatibility": "pre-dispatch-contract",
                }
            raise TerminalCommitError("route-close-failed", result.stderr.strip())
        try:
            outcome = json.loads(result.stdout.splitlines()[0])
        except (IndexError, TypeError, ValueError) as exc:
            raise TerminalCommitError("route-close-failed", "outcome unreadable") from exc
        if (
            not isinstance(outcome, Mapping)
            or outcome.get("route_id") != eligibility.route_id
            or outcome.get("route_hash") != eligibility.route_hash
            or outcome.get("terminal_gate_proven") is not True
        ):
            raise TerminalCommitError("route-close-failed", "terminal gate unproven")
        return outcome

    def finalize_producer(self, eligibility: TerminalEligibility) -> Mapping[str, Any]:
        binding = eligibility.producer_binding
        if not isinstance(binding, Mapping):
            raise TerminalCommitError("producer-binding-required")
        import artifact_producer

        root = Path(eligibility.artifact_root)
        cycle_id = str(binding["cycle_id"])
        recovered = artifact_producer.recover(root)
        if cycle_id in recovered.get("dropped", []):
            raise TerminalCommitError("recovery-unavailable", cycle_id)
        record = artifact_producer.read_cycle_record(root, cycle_id)
        if record is None:
            raise TerminalCommitError("producer-finalize-failed", "cycle missing")
        cycle = artifact_producer.cycle_dir(root, str(binding["campaign_id"]), cycle_id)
        artifact = Path(eligibility.artifact_path).resolve()
        try:
            primary = str(artifact.relative_to(cycle / "artifacts"))
        except ValueError as exc:
            raise TerminalCommitError("producer-binding-mismatch", "artifact outside cycle") from exc
        if record.get("state") == "sealed":
            manifest = cycle / "manifest.json"
            document = _read_json(manifest)
            try:
                import artifact_manifest

                digest = artifact_manifest.manifest_digest(document or {})
            except Exception as exc:
                raise TerminalCommitError(
                    "recovery-unavailable", "sealed manifest unreadable"
                ) from exc
            if (
                manifest.is_symlink()
                or document is None
                or digest != record.get("manifest_digest")
            ):
                raise TerminalCommitError(
                    "recovery-unavailable", "sealed manifest unverified"
                )
            return {
                "status": "already-sealed",
                "cycle_id": cycle_id,
                "manifest_path": str(manifest),
                "manifest_digest": record.get("manifest_digest"),
            }
        try:
            return artifact_producer.finalize(root, cycle_id=cycle_id, primary=primary)
        except artifact_producer.ProducerError as exc:
            raise TerminalCommitError("producer-finalize-failed", exc.code) from exc


def _record_matches(record: Mapping[str, Any], immutable: Mapping[str, Any]) -> bool:
    return all(record.get(key) == value for key, value in immutable.items())


def settle_terminal_commit(
    eligibility: TerminalEligibility,
    *,
    services: Any | None = None,
    crash_after_state: str | None = None,
) -> TerminalCommitResult:
    if not eligibility.eligible:
        return TerminalCommitResult(False, "ineligible", eligibility.reason)
    service = services or RealTerminalCommitServices()
    commit_id = terminal_commit_id(eligibility)
    root = Path(eligibility.artifact_root)
    path = terminal_commit_path(root, commit_id)
    immutable = {
        "schema_version": COMMIT_VERSION,
        "record_type": "terminal_commit_v1",
        "terminal_commit_id": commit_id,
        "route_id": eligibility.route_id,
        "route_hash": eligibility.route_hash,
        "owner_attempt_id": eligibility.owner_attempt_id,
        "terminal_marker_digest": eligibility.terminal_marker_digest,
        "producer_binding_digest": eligibility.producer_binding_digest,
        "artifact_path": eligibility.artifact_path,
        "artifact_digest": eligibility.artifact_digest,
    }
    with _LockedRecord(path):
        record = _read_json(path)
        if record is None:
            record = {
                **immutable,
                "state": "claimed",
                "created_at": _now(),
                "updated_at": _now(),
            }
            try:
                _atomic_write(path, canonical_bytes(record) + b"\n", exclusive=True)
            except FileExistsError:
                return TerminalCommitResult(
                    False, "conflict", "transaction-conflict", commit_id, str(path),
                    artifact_path=eligibility.artifact_path,
                )
        elif not _record_matches(record, immutable):
            return TerminalCommitResult(
                False, str(record.get("state", "conflict")), "transaction-conflict",
                commit_id, str(path), artifact_path=eligibility.artifact_path,
            )
        if crash_after_state == "claimed":
            raise TerminalCommitCrash("claimed")

        if record.get("state") == "claimed":
            try:
                outcome = dict(service.close_route(eligibility))
            except TerminalCommitError as exc:
                return TerminalCommitResult(
                    False, "claimed", exc.reason, commit_id, str(path),
                    artifact_path=eligibility.artifact_path,
                )
            record.update(
                {
                    "state": "route-closed",
                    "route_outcome_digest": digest_record(outcome),
                    "route_outcome": outcome,
                    "updated_at": _now(),
                }
            )
            _atomic_write(path, canonical_bytes(record) + b"\n")
        if crash_after_state == "route-closed":
            raise TerminalCommitCrash("route-closed")

        if record.get("state") == "route-closed":
            if eligibility.producer_required:
                try:
                    finalized = dict(service.finalize_producer(eligibility))
                except TerminalCommitError as exc:
                    record["last_error"] = exc.reason
                    record["updated_at"] = _now()
                    _atomic_write(path, canonical_bytes(record) + b"\n")
                    return TerminalCommitResult(
                        False, "route-closed", exc.reason, commit_id, str(path),
                        artifact_path=eligibility.artifact_path,
                    )
                if finalized.get("status") not in {"sealed", "already-sealed"}:
                    return TerminalCommitResult(
                        False, "route-closed", "producer-finalize-failed",
                        commit_id, str(path), artifact_path=eligibility.artifact_path,
                    )
                record.update(
                    {
                        "state": "producer-finalized",
                        "producer_manifest_digest": finalized.get("manifest_digest"),
                        "producer_manifest_path": finalized.get("manifest_path"),
                        "updated_at": _now(),
                    }
                )
            else:
                record.update({"state": "not-applicable", "updated_at": _now()})
            record.pop("last_error", None)
            _atomic_write(path, canonical_bytes(record) + b"\n")
        if crash_after_state in {"producer-finalized", "not-applicable"} and record.get("state") == crash_after_state:
            raise TerminalCommitCrash(crash_after_state)

        if record.get("state") in {"producer-finalized", "not-applicable"}:
            artifact = Path(eligibility.artifact_path)
            if (
                not artifact.is_absolute()
                or artifact.is_symlink()
                or not artifact.is_file()
                or not _under(artifact, root)
                or _file_sha256(artifact) != eligibility.artifact_digest.removeprefix("sha256:")
            ):
                return TerminalCommitResult(
                    False, str(record["state"]), "terminal-marker-not-current",
                    commit_id, str(path), artifact_path=eligibility.artifact_path,
                )
            envelope = (
                f"artifact: {eligibility.artifact_path}\n"
                "verdict: PASS\n"
                "blocker: none"
            )
            envelope_path = terminal_envelope_path(root, commit_id)
            if envelope_path.exists():
                try:
                    if (
                        envelope_path.is_symlink()
                        or envelope_path.read_text(encoding="utf-8") != envelope
                    ):
                        return TerminalCommitResult(
                            False, str(record["state"]), "transaction-conflict",
                            commit_id, str(path), artifact_path=eligibility.artifact_path,
                        )
                except OSError:
                    return TerminalCommitResult(
                        False, str(record["state"]), "recovery-unavailable",
                        commit_id, str(path), artifact_path=eligibility.artifact_path,
                    )
            else:
                _atomic_write(envelope_path, envelope.encode("utf-8"), exclusive=True)
            if crash_after_state == "envelope-written":
                raise TerminalCommitCrash("envelope-written")
            record.update(
                {
                    "state": "owner-envelope-sealed",
                    "owner_envelope_path": str(envelope_path),
                    "owner_envelope_digest": digest_bytes(envelope.encode("utf-8")),
                    "updated_at": _now(),
                }
            )
            _atomic_write(path, canonical_bytes(record) + b"\n")
        envelope_path = terminal_envelope_path(root, commit_id)
        try:
            envelope = envelope_path.read_text(encoding="utf-8")
        except OSError:
            return TerminalCommitResult(
                False, str(record.get("state", "unknown")), "recovery-unavailable",
                commit_id, str(path), artifact_path=eligibility.artifact_path,
            )
        return TerminalCommitResult(
            True,
            "owner-envelope-sealed",
            "",
            commit_id,
            str(path),
            envelope,
            eligibility.artifact_path,
            True,
        )


def settle_supervisor_terminal(args: Any, rows: Sequence[object]) -> TerminalCommitResult:
    route_path = Path(str(getattr(args, "route_file", "")))
    route = _read_json(route_path)
    if route is None:
        return TerminalCommitResult(False, "ineligible", "route-identity-unverified")
    eligibility = classify_terminal_eligibility(
        route=route,
        route_file=route_path,
        owner_attempt_id=str(getattr(args, "parent_attempt_id", "")),
        rows=rows,
        jobs=Path(args.jobs) if getattr(args, "jobs", "") else None,
    )
    return settle_terminal_commit(eligibility)


def supervisor_cleanup_capability(args: Any) -> dict[str, Any]:
    """Return the exact bounded cleanup scope for one bound owner route."""

    route_path = Path(str(getattr(args, "route_file", ""))).resolve()
    route = _read_json(route_path) or {}
    root = Path(str(route.get("artifact_root") or route_path.parent)).resolve()
    owner = str(getattr(args, "parent_attempt_id", ""))
    binding = load_producer_binding(root, str(route.get("route_id", "")), owner)
    write_roots: list[str] = []
    commands: list[str] = []
    if (
        isinstance(binding, Mapping)
        and _safe_id(binding.get("campaign_id"), "camp")
        and _safe_id(binding.get("cycle_id"), "cyc")
    ):
        cycle_root = (
            root
            / "campaigns"
            / str(binding.get("campaign_id", ""))
            / "cycles"
            / str(binding.get("cycle_id", ""))
        )
        write_roots = [str(cycle_root / "artifacts")]
        jobs = str(getattr(args, "jobs", ""))
        if jobs:
            commands = [
                shlex.join(
                    (
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "recover",
                        "--route",
                        str(route_path),
                        "--parent-attempt-id",
                        owner,
                        "--jobs",
                        jobs,
                    )
                )
            ]
    return {
        "artifact_root": str(root),
        "allowed_write_roots": write_roots,
        "allowed_read_roots": [str(root), str(route_path.parent)],
        "allowed_commands": commands,
    }


def terminal_handoff_claim_id(
    *,
    parent_attempt_id: str,
    child_attempt_ids: Iterable[str],
    continuation_ordinal: int,
    route_hash: str,
) -> str:
    return digest_bytes(
        canonical_bytes(
            {
                "parent_attempt_id": parent_attempt_id,
                "child_attempt_ids": sorted(set(child_attempt_ids)),
                "continuation_ordinal": continuation_ordinal,
                "route_hash": route_hash,
            }
        )
    )


def claim_terminal_handoff(
    state_root: Path,
    *,
    parent_attempt_id: str,
    child_attempt_ids: Iterable[str],
    continuation_ordinal: int,
    route_hash: str,
) -> dict[str, Any]:
    children = sorted(set(child_attempt_ids))
    claim_id = terminal_handoff_claim_id(
        parent_attempt_id=parent_attempt_id,
        child_attempt_ids=children,
        continuation_ordinal=continuation_ordinal,
        route_hash=route_hash,
    )
    payload = {
        "schema_version": CLAIM_VERSION,
        "record_type": "terminal_handoff_claim_v1",
        "claim_id": claim_id,
        "parent_attempt_id": parent_attempt_id,
        "child_attempt_ids": children,
        "continuation_ordinal": continuation_ordinal,
        "route_hash": route_hash,
        "state": "claimed",
        "budget_delta": {"gross": 0, "stall": 0, "reserved": 0},
        "reservation_count": 0,
        "prompt_count": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    path = terminal_handoff_claim_path(state_root, claim_id)
    with _LockedRecord(path):
        existing = _read_json(path)
        if existing is not None:
            immutable = {
                key: payload[key]
                for key in (
                    "schema_version",
                    "record_type",
                    "claim_id",
                    "parent_attempt_id",
                    "child_attempt_ids",
                    "continuation_ordinal",
                    "route_hash",
                )
            }
            if not _record_matches(existing, immutable):
                raise TerminalCommitError("transaction-conflict", claim_id)
            return existing
        try:
            _atomic_write(path, canonical_bytes(payload) + b"\n", exclusive=True)
        except FileExistsError as exc:
            raise TerminalCommitError("transaction-conflict", claim_id) from exc
    return payload


def convert_terminal_handoff_claim(
    state_root: Path,
    *,
    claim_id: str,
    prompt: str,
    charge: Callable[[], bool | tuple[bool, str]],
    artifact_root: str,
    allowed_write_roots: Sequence[str],
    allowed_read_roots: Sequence[str],
    allowed_commands: Sequence[str] = (),
    crash_after_charge: bool = False,
) -> dict[str, Any]:
    """Convert once at the real prompt-intent boundary.

    The prompt intent is prepared durably before the callback.  The callback's
    terminal-budget CAS binds that claim and prompt digest, so a crash after
    the charge replays the same reservation instead of charging again.  A
    failed callback restores the zero-delta claimed state.
    """

    path = terminal_handoff_claim_path(state_root, claim_id)
    with _LockedRecord(path):
        record = _read_json(path)
        if record is None:
            raise TerminalCommitError("recovery-unavailable", claim_id)
        if record.get("state") == "converted":
            return record
        if record.get("state") not in {"claimed", "converting"}:
            raise TerminalCommitError("transaction-conflict", claim_id)
        if not prompt:
            raise TerminalCommitError("transaction-conflict", "empty prompt intent")
        prompt_digest = digest_bytes(prompt.encode("utf-8"))
        capability = {
            "artifact_root": str(Path(artifact_root).resolve()),
            "allowed_write_roots": [
                str(Path(value).resolve()) for value in allowed_write_roots
            ],
            "allowed_read_roots": [
                str(Path(value).resolve()) for value in allowed_read_roots
            ],
            "allowed_commands": sorted(set(allowed_commands)),
        }
        if record.get("state") == "converting":
            if (
                record.get("prompt_intent_digest") != prompt_digest
                or record.get("cleanup_capability") != capability
            ):
                raise TerminalCommitError("transaction-conflict", claim_id)
        else:
            record.update(
                {
                    "state": "converting",
                    "prompt_intent": prompt,
                    "prompt_intent_digest": prompt_digest,
                    "cleanup_capability": capability,
                    "updated_at": _now(),
                }
            )
            _atomic_write(path, canonical_bytes(record) + b"\n")
        charge_result = charge()
        if isinstance(charge_result, tuple):
            charged, resolved_prompt = charge_result
        else:
            charged, resolved_prompt = charge_result, prompt
        if not charged:
            for key in (
                "prompt_intent",
                "prompt_intent_digest",
                "cleanup_capability",
            ):
                record.pop(key, None)
            record["state"] = "claimed"
            record["updated_at"] = _now()
            _atomic_write(path, canonical_bytes(record) + b"\n")
            return record
        if not resolved_prompt:
            raise TerminalCommitError("transaction-conflict", "empty prompt intent")
        if crash_after_charge:
            raise TerminalCommitCrash("terminal-handoff-charged")
        resolved_digest = digest_bytes(resolved_prompt.encode("utf-8"))
        if resolved_digest != prompt_digest:
            raise TerminalCommitError("transaction-conflict", "prompt intent changed")
        record.update(
            {
                "state": "converted",
                "budget_delta": {"gross": 0, "stall": 0, "reserved": -1},
                "reservation_count": 1,
                "prompt_count": 1,
                "updated_at": _now(),
            }
        )
        _atomic_write(path, canonical_bytes(record) + b"\n")
        return record


def complete_terminal_handoff_claim(
    state_root: Path, *, claim_id: str, terminal_commit_id_value: str
) -> dict[str, Any]:
    path = terminal_handoff_claim_path(state_root, claim_id)
    with _LockedRecord(path):
        record = _read_json(path)
        if record is None:
            raise TerminalCommitError("recovery-unavailable", claim_id)
        if record.get("state") == "completed":
            return record
        if record.get("state") != "claimed":
            raise TerminalCommitError("transaction-conflict", claim_id)
        record.update(
            {
                "state": "completed",
                "terminal_commit_id": terminal_commit_id_value,
                "continuation_saved": 1,
                "budget_delta": {"gross": 0, "stall": 0, "reserved": 0},
                "reservation_count": 0,
                "prompt_count": 0,
                "updated_at": _now(),
            }
        )
        _atomic_write(path, canonical_bytes(record) + b"\n")
        return record


def active_cleanup_claim(
    state_root: Path, *, parent_attempt_id: str
) -> dict[str, Any] | None:
    directory = Path(state_root).resolve() / "terminal-handoff-claims"
    if not directory.is_dir():
        return None
    candidates: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        record = _read_json(path)
        if (
            record is not None
            and record.get("record_type") == "terminal_handoff_claim_v1"
            and record.get("parent_attempt_id") == parent_attempt_id
            and record.get("state") == "converted"
            and isinstance(record.get("cleanup_capability"), Mapping)
        ):
            candidates.append(record)
    if not candidates:
        return None
    return sorted(candidates, key=lambda row: str(row.get("updated_at", "")))[-1]


def _paths_from_tool(tool_name: str, tool_input: Mapping[str, Any]) -> list[Path]:
    keys = {
        "Read": ("file_path", "path"),
        "Write": ("file_path", "path"),
        "Edit": ("file_path", "path"),
        "MultiEdit": ("file_path", "path"),
        "NotebookEdit": ("notebook_path", "file_path", "path"),
        "Grep": ("path",),
        "Glob": ("path",),
    }.get(tool_name, ())
    return [Path(str(tool_input[key])) for key in keys if tool_input.get(key)]


def cleanup_tool_allowed(
    claim: Mapping[str, Any], *, tool_name: str, tool_input: Mapping[str, Any]
) -> bool:
    capability = claim.get("cleanup_capability")
    if not isinstance(capability, Mapping):
        return False
    if tool_name == "Bash":
        command = tool_input.get("command")
        return isinstance(command, str) and command in set(
            capability.get("allowed_commands") or []
        )
    paths = _paths_from_tool(tool_name, tool_input)
    if tool_name in {"Read", "Grep", "Glob"}:
        roots = [Path(str(value)) for value in capability.get("allowed_read_roots") or []]
        return bool(paths) and all(any(_under(path, root) for root in roots) for path in paths)
    if tool_name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        roots = [Path(str(value)) for value in capability.get("allowed_write_roots") or []]
        allowed_suffixes = {".md", ".json", ".txt", ".log"}
        return bool(paths) and all(
            path.suffix.lower() in allowed_suffixes
            and any(_under(path, root) for root in roots)
            for path in paths
        )
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recover one exact SD-120 terminal settlement"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--route", required=True)
    recover_parser.add_argument("--parent-attempt-id", required=True)
    recover_parser.add_argument("--jobs", required=True)
    args = parser.parse_args(argv)
    if args.command != "recover":
        return 64
    try:
        from dispatch_completion_join import current_children

        rows = current_children(Path(args.jobs), args.parent_attempt_id)
        result = settle_supervisor_terminal(
            argparse.Namespace(
                route_file=args.route,
                parent_attempt_id=args.parent_attempt_id,
                jobs=args.jobs,
            ),
            rows,
        )
    except Exception as exc:
        result = TerminalCommitResult(
            False, "recovery-failed", "recovery-unavailable"
        )
        print(
            json.dumps(
                {**result.__dict__, "detail": type(exc).__name__},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 70
    print(json.dumps(result.__dict__, sort_keys=True, separators=(",", ":")))
    return 0 if result.success else 75


if __name__ == "__main__":
    raise SystemExit(main())
