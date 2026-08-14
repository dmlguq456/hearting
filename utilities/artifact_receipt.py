from __future__ import annotations

"""artifact-path-contract's D-12 canonical publication receipt decoder.

Owns the exact v1/v2/v3 receipt key tables, typed refusal classification,
canonical receipt digesting, v3 local lineage resolution against a registered
artifact root, and idempotency/conflict ledgering. `utilities/artifact-sink.sh`
(note-publication, D-13) is the only receipt producer; this module is the only
receipt consumer. It never writes to the step-1 admission index/root-identity
files and never calls `artifact_admission.load_index()`.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission  # noqa: E402  (ADMISSION_REL constant only -- never load_index())
import artifact_identity  # noqa: E402
import artifact_index  # noqa: E402
import artifact_lifecycle  # noqa: E402
import artifact_manifest  # noqa: E402

# ---------------------------------------------------------------------------
# exact key tables (D-12)
# ---------------------------------------------------------------------------

V1_KEYS: Tuple[str, ...] = (
    "schema_version",
    "event",
    "source_path",
    "source_capability",
    "project_root",
    "status",
    "completed_at",
)
V2_KEYS: Tuple[str, ...] = (
    "schema_version",
    "event",
    "status",
    "completed_at",
    "bundle_id",
    "version",
    "entrypoint",
)
V3_KEYS: Tuple[str, ...] = (
    "schema_version",
    "event",
    "status",
    "completed_at",
    "repository_id",
    "campaign_id",
    "cycle_id",
    "artifact_id",
    "artifact_revision_id",
    "manifest_id",
    "manifest_revision_id",
)

V1_KEY_SET = frozenset(V1_KEYS)
V2_KEY_SET = frozenset(V2_KEYS)
V3_KEY_SET = frozenset(V3_KEYS)

KEY_ORDER: Mapping[int, Tuple[str, ...]] = {1: V1_KEYS, 2: V2_KEYS, 3: V3_KEYS}
KEY_SETS: Mapping[int, frozenset] = {1: V1_KEY_SET, 2: V2_KEY_SET, 3: V3_KEY_SET}

V2_IDENTITY_KEYS: Tuple[str, ...] = ("bundle_id", "version", "entrypoint")
_V2_IDENTITY_KEY_SET = frozenset(V2_IDENTITY_KEYS)

V3_IDENTITY_KEYS: Tuple[str, ...] = (
    "repository_id",
    "campaign_id",
    "cycle_id",
    "artifact_id",
    "artifact_revision_id",
    "manifest_id",
    "manifest_revision_id",
)
_V3_IDENTITY_KEY_SET = frozenset(V3_IDENTITY_KEYS)

V3_ID_KINDS: Mapping[str, str] = {
    "repository_id": "repository",
    "campaign_id": "campaign",
    "cycle_id": "cycle",
    "artifact_id": "artifact",
    "artifact_revision_id": "artifact_revision",
    "manifest_id": "manifest",
    "manifest_revision_id": "manifest_revision",
}

# repository_id is not in the index; campaign_id is reusable and has no
# cycle_id/manifest_id row lineage. These are the indexed v3 IDs.
_INDEXED_V3_ID_KEYS: Tuple[str, ...] = (
    "cycle_id",
    "artifact_id",
    "artifact_revision_id",
    "manifest_id",
    "manifest_revision_id",
)

EVENT_LITERAL = "artifact.completed"
STATUS_LITERAL = "completed"
RECEIPT_LEDGER_REL = ".runtime/artifact-receipts/v1"
LEDGER_RECORD_SCHEMA_VERSION = 1

REASONS = frozenset(
    {
        "value-invalid",
        "unknown-schema-version",
        "key-set-mismatch",
        "partial-bundle-identity",
        "partial-manifest-identity",
        "mixed-version-fields",
        "unknown-field",
        "local-manifest-unregistered",
        "local-lineage-mismatch",
        "local-state-unreadable",
        "identity-conflict",
    }
)
STATES = frozenset({"accepted", "noop-idempotent", "rejected", "unavailable", "error"})

EXIT_OK = 0
EXIT_REFUSED = 64
EXIT_UNAVAILABLE = 69
EXIT_INTERNAL = 70

_LEDGER_README_TEXT = (
    "This directory holds the D-12 publication receipt idempotency ledger.\n"
    "It is owned by artifact-path-contract and is intentionally separate\n"
    "from the step-1 admission index (.runtime/artifact-admission/v1/).\n"
    "Each record file is write-once and named by sha256(identity_key).\n"
)


class ReceiptError(Exception):
    pass


@dataclass(frozen=True)
class Verdict:
    state: str
    reason: Optional[str] = None
    detail: Optional[str] = None
    schema_version: Optional[int] = None
    receipt: Optional[Mapping[str, Any]] = None
    digest: Optional[str] = None
    identity: Optional[Tuple[str, ...]] = None

    def ok(self) -> bool:
        return self.state in ("accepted", "noop-idempotent")

    def exit_code(self) -> int:
        assert self.state in STATES
        if self.state in ("accepted", "noop-idempotent"):
            return EXIT_OK
        if self.state == "rejected":
            return EXIT_REFUSED
        if self.state == "unavailable":
            return EXIT_UNAVAILABLE
        if self.state == "error":
            return EXIT_INTERNAL
        raise ReceiptError("unknown verdict state: {0!r}".format(self.state))

    def render(self) -> str:
        lines = ["state=" + self.state]
        if self.reason is not None:
            lines.append("reason=" + self.reason)
        if self.detail is not None:
            lines.append("detail=" + self.detail)
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# canonical bytes (see plan.md sec.1.3 -- declared key order, no sort_keys)
# ---------------------------------------------------------------------------


def canonical_bytes(receipt: Mapping[str, Any]) -> bytes:
    schema_version = receipt["schema_version"]
    order = KEY_ORDER[schema_version]
    ordered = {key: receipt[key] for key in order}
    return (
        json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(receipt)).hexdigest()


def identity_tuple(receipt: Mapping[str, Any]) -> Tuple[str, ...]:
    schema_version = receipt["schema_version"]
    if schema_version == 3:
        return tuple(receipt[key] for key in V3_IDENTITY_KEYS)
    if schema_version == 2:
        return tuple(receipt[key] for key in V2_IDENTITY_KEYS)
    if schema_version == 1:
        return (receipt_digest(receipt),)
    raise ReceiptError("identity_tuple: unknown schema_version {0!r}".format(schema_version))


# ---------------------------------------------------------------------------
# R3 value validators (declared key order per version; first failure wins)
# ---------------------------------------------------------------------------

_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_BUNDLE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _v_event(value: Any) -> bool:
    return value == EVENT_LITERAL


def _v_status(value: Any) -> bool:
    return value == STATUS_LITERAL


def _v_completed_at(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _v_abs_path_str(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0 and os.path.isabs(value)


def _is_utf8_encodable(value: Any) -> bool:
    """A lone surrogate is legal JSON syntax but is not encodable as UTF-8.

    `canonical_bytes()` would raise `UnicodeEncodeError` on such a value, and
    that is neither a refusal nor a member of the typed 0/64/69/70 vocabulary.
    D-12 requires every decoder to refuse non-conforming input, so R3 screens
    it as an invalid value instead of letting the digest step escape.
    """

    if not isinstance(value, str):
        return True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _v_capability(value: Any) -> bool:
    return isinstance(value, str) and bool(_CAPABILITY_RE.fullmatch(value))


def _v_bundle_id(value: Any) -> bool:
    if not isinstance(value, str) or "/" not in value:
        return False
    project, _sep, experiment = value.partition("/")
    return bool(_BUNDLE_TOKEN_RE.fullmatch(project)) and bool(
        _BUNDLE_TOKEN_RE.fullmatch(experiment)
    )


def _v_bundle_token(value: Any) -> bool:
    return isinstance(value, str) and bool(_BUNDLE_TOKEN_RE.fullmatch(value))


def _v_entrypoint(value: Any) -> bool:
    return value == "report/index.html"


def _v3_id_validator(kind: str):
    def _check(value: Any) -> bool:
        return artifact_identity.is_well_formed(value, kind)

    return _check


_VALUE_VALIDATORS: Mapping[int, Mapping[str, Any]] = {
    1: {
        "event": _v_event,
        "source_path": _v_abs_path_str,
        "source_capability": _v_capability,
        "project_root": _v_abs_path_str,
        "status": _v_status,
        "completed_at": _v_completed_at,
    },
    2: {
        "event": _v_event,
        "status": _v_status,
        "completed_at": _v_completed_at,
        "bundle_id": _v_bundle_id,
        "version": _v_bundle_token,
        "entrypoint": _v_entrypoint,
    },
    3: dict(
        {
            "event": _v_event,
            "status": _v_status,
            "completed_at": _v_completed_at,
        },
        **{key: _v3_id_validator(kind) for key, kind in V3_ID_KINDS.items()},
    ),
}


# ---------------------------------------------------------------------------
# decode() -- R0-R3
# ---------------------------------------------------------------------------


def decode(payload: Any) -> Verdict:
    if not isinstance(payload, dict):
        return Verdict(state="rejected", reason="value-invalid", detail="$")

    schema_version = payload.get("schema_version")
    if (
        "schema_version" not in payload
        or not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in KEY_ORDER
    ):
        return Verdict(state="rejected", reason="unknown-schema-version")

    key_set = KEY_SETS[schema_version]
    observed = set(payload.keys())
    missing = key_set - observed
    extra = observed - key_set
    foreign = (V1_KEY_SET | V2_KEY_SET | V3_KEY_SET) - key_set

    if missing and extra:
        reason = None
        for other_version in (1, 2, 3):
            if other_version == schema_version:
                continue
            if observed == KEY_SETS[other_version]:
                reason = "key-set-mismatch"
                break
        if reason is None:
            reason = "mixed-version-fields" if (extra & foreign) else "key-set-mismatch"
        return Verdict(state="rejected", reason=reason, schema_version=schema_version)

    if missing and not extra:
        if schema_version == 2 and missing <= _V2_IDENTITY_KEY_SET:
            reason = "partial-bundle-identity"
        elif schema_version == 3 and missing <= _V3_IDENTITY_KEY_SET:
            reason = "partial-manifest-identity"
        else:
            reason = "key-set-mismatch"
        return Verdict(state="rejected", reason=reason, schema_version=schema_version)

    if extra and not missing:
        reason = "mixed-version-fields" if (extra & foreign) else "unknown-field"
        return Verdict(state="rejected", reason=reason, schema_version=schema_version)

    validators = _VALUE_VALIDATORS[schema_version]
    for key in KEY_ORDER[schema_version]:
        value = payload[key]
        # Screened for every key, not only validated ones: an unvalidated key
        # still reaches canonical_bytes() through receipt_digest() below.
        if not _is_utf8_encodable(value):
            return Verdict(
                state="rejected",
                reason="value-invalid",
                detail="$." + key,
                schema_version=schema_version,
            )
        validator = validators.get(key)
        if validator is None:
            continue
        if not validator(value):
            return Verdict(
                state="rejected",
                reason="value-invalid",
                detail="$." + key,
                schema_version=schema_version,
            )

    receipt = dict(payload)
    return Verdict(
        state="accepted",
        schema_version=schema_version,
        receipt=receipt,
        digest=receipt_digest(receipt),
        identity=identity_tuple(receipt),
    )


def decode_bytes(data: bytes) -> Verdict:
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return Verdict(state="rejected", reason="value-invalid", detail="$")
    return decode(payload)


# ---------------------------------------------------------------------------
# resolve() -- R4 / R4' (v3 local lineage resolution; read-only)
# ---------------------------------------------------------------------------


def _rejected(reason: str, schema_version: Optional[int] = None) -> Verdict:
    return Verdict(state="rejected", reason=reason, schema_version=schema_version)


def _unavailable(reason: str, schema_version: Optional[int] = None) -> Verdict:
    return Verdict(state="unavailable", reason=reason, schema_version=schema_version)


def _ledger_error(schema_version: Optional[int] = None) -> Verdict:
    """OD-14: one exit class for every local receipt-book I/O failure.

    `resolve()` owns "the lineage cannot be read" -> `unavailable` / exit 69.
    `register()` owns "the receipt book cannot be trusted" -> `error` / exit 70.
    Both used `local-state-unreadable`, so splitting the exit split the D-10
    publication word (`skipped` vs `failed`) for one failure class.
    """

    return Verdict(
        state="error", reason="local-state-unreadable", schema_version=schema_version
    )


def resolve(artifact_root: Any, receipt: Mapping[str, Any]) -> Verdict:
    schema_version = receipt.get("schema_version")
    if schema_version != 3:
        return Verdict(
            state="accepted",
            schema_version=schema_version,
            receipt=receipt,
            digest=receipt_digest(receipt),
            identity=identity_tuple(receipt),
        )

    root = Path(artifact_root)

    # S1
    try:
        root_identity = artifact_lifecycle.read_root_identity(root)
    except artifact_lifecycle.LifecycleError:
        return _unavailable("local-state-unreadable", 3)
    if root_identity is None:
        return _rejected("local-manifest-unregistered", 3)

    # S2
    if root_identity.repository_id != receipt["repository_id"]:
        return _rejected("local-lineage-mismatch", 3)

    # S3
    index_path = root / artifact_admission.ADMISSION_REL / "index.json"
    try:
        raw_index = index_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _rejected("local-manifest-unregistered", 3)
    except OSError:
        return _unavailable("local-state-unreadable", 3)
    try:
        index_payload = json.loads(raw_index)
        index = artifact_index.parse(index_payload)
    except (ValueError, TypeError):
        return _unavailable("local-state-unreadable", 3)

    # S4
    if index.artifact_root_id != root_identity.artifact_root_id:
        return _rejected("local-lineage-mismatch", 3)

    # S5 + S6
    stable_id_rows = {}
    for key in _INDEXED_V3_ID_KEYS:
        kind = V3_ID_KINDS[key]
        row = index.stable_ids.get(receipt[key])
        if row is None:
            return _rejected("local-manifest-unregistered", 3)
        if row.get("kind") != kind:
            return _rejected("local-lineage-mismatch", 3)
        stable_id_rows[key] = row

    for key, row in stable_id_rows.items():
        if row.get("cycle_id") != receipt["cycle_id"] or row.get("manifest_id") != receipt["manifest_id"]:
            return _rejected("local-lineage-mismatch", 3)

    # S7
    cycle_row = index.cycles.get(receipt["cycle_id"])
    if cycle_row is None:
        return _rejected("local-manifest-unregistered", 3)
    if cycle_row.get("campaign_id") != receipt["campaign_id"]:
        return _rejected("local-lineage-mismatch", 3)

    # S8 -- manifests is keyed by idempotency_key; value scan required.
    matches = [
        row
        for row in index.manifests.values()
        if row.get("manifest_id") == receipt["manifest_id"]
        and row.get("manifest_revision_id") == receipt["manifest_revision_id"]
        and row.get("cycle_id") == receipt["cycle_id"]
    ]
    if not matches:
        return _rejected("local-manifest-unregistered", 3)
    if len(matches) > 1:
        return _rejected("local-lineage-mismatch", 3)
    manifest_row = matches[0]

    # S9
    if manifest_row.get("manifest_digest") != cycle_row.get("manifest_digest"):
        return _rejected("local-lineage-mismatch", 3)

    # S10
    cycle_path = cycle_row.get("cycle_path")
    if not isinstance(cycle_path, str):
        # S7 already found this cycle row, so "unregistered" would contradict
        # itself here; a row that is present but malformed is a lineage
        # contradiction.
        return _rejected("local-lineage-mismatch", 3)
    manifest_path = root / cycle_path / "manifest.json"
    try:
        raw_manifest = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _rejected("local-manifest-unregistered", 3)
    except OSError:
        return _unavailable("local-state-unreadable", 3)
    try:
        document = json.loads(raw_manifest)
    except ValueError:
        return _unavailable("local-state-unreadable", 3)

    # S11
    report = artifact_manifest.validate(document)
    if not report.ok:
        return _rejected("local-lineage-mismatch", 3)

    # S12
    if artifact_manifest.manifest_digest(document) != manifest_row.get("manifest_digest"):
        return _rejected("local-lineage-mismatch", 3)

    # S13
    campaign = document.get("campaign") if isinstance(document.get("campaign"), dict) else {}
    cycle = document.get("cycle") if isinstance(document.get("cycle"), dict) else {}
    if (
        document.get("repository_id") != receipt["repository_id"]
        or document.get("manifest_id") != receipt["manifest_id"]
        or document.get("manifest_revision_id") != receipt["manifest_revision_id"]
        or campaign.get("campaign_id") != receipt["campaign_id"]
        or cycle.get("cycle_id") != receipt["cycle_id"]
        or cycle.get("campaign_id") != receipt["campaign_id"]
    ):
        return _rejected("local-lineage-mismatch", 3)

    # S14
    artifact_row = next(
        (
            row
            for row in (document.get("artifacts") or [])
            if isinstance(row, dict)
            and row.get("artifact_id") == receipt["artifact_id"]
            and row.get("cycle_id") == receipt["cycle_id"]
        ),
        None,
    )
    if artifact_row is None:
        return _rejected("local-lineage-mismatch", 3)

    # S15
    revision_row = next(
        (
            row
            for row in (document.get("artifact_revisions") or [])
            if isinstance(row, dict)
            and row.get("artifact_revision_id") == receipt["artifact_revision_id"]
            and row.get("artifact_id") == receipt["artifact_id"]
        ),
        None,
    )
    if revision_row is None:
        return _rejected("local-lineage-mismatch", 3)

    return Verdict(
        state="accepted",
        schema_version=3,
        receipt=receipt,
        digest=receipt_digest(receipt),
        identity=identity_tuple(receipt),
    )


# ---------------------------------------------------------------------------
# register() -- R5 idempotency ledger
# ---------------------------------------------------------------------------

_LEDGER_RECORD_ORDER = ("schema_version", "receipt_schema_version", "identity", "receipt_digest")


def _ledger_record_bytes(record: Mapping[str, Any]) -> bytes:
    ordered = {key: record[key] for key in _LEDGER_RECORD_ORDER}
    return (
        json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def _identity_key(receipt: Mapping[str, Any]) -> str:
    schema_version = receipt["schema_version"]
    tup = identity_tuple(receipt)
    # Encode tuple boundaries structurally; a separator embedded in a public
    # v2 bundle_id must not alias a version or entrypoint boundary.
    return json.dumps((schema_version,) + tup, ensure_ascii=False, separators=(",", ":"))


def _ensure_ledger_dirs(ledger_root: Path, records_dir: Path) -> None:
    """Create the ledger tree, or raise OSError. No failure is swallowed here.

    OD-15: the 0700 mode is an owner-sealed invariant, so a chmod that fails
    silently would break it without any typed signal. The README is written in
    a directory a peer may have created first, so README creation is an atomic
    postcondition check: FileExistsError means the peer established it; every
    other OSError remains a real defect. This does not relax OD-15.
    """

    records_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(records_dir, 0o700)
    os.chmod(ledger_root, 0o700)
    readme_path = ledger_root / "README.md"
    try:
        fd = os.open(str(readme_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            os.write(fd, _LEDGER_README_TEXT.encode("utf-8"))
        finally:
            os.close(fd)


def _write_once(records_dir: Path, final_path: Path, data: bytes) -> bool:
    tmp_name = ".{0}-{1}.tmp".format(os.getpid(), uuid.uuid4().hex)
    tmp_path = records_dir / tmp_name
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(str(tmp_path), str(final_path))
            linked = True
        except FileExistsError:
            linked = False
    finally:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
    # OD-15: the directory fsync is the durability half of write-once, so its
    # failure is raised, not swallowed. It runs after a successful os.link, so
    # a raised error can leave the record on disk -- that is the honest result
    # ("cannot confirm durability"); a retry converges to noop-idempotent.
    dir_fd = os.open(str(records_dir), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return linked


def register(artifact_root: Any, receipt: Mapping[str, Any]) -> Verdict:
    root = Path(artifact_root)
    ledger_root = root / RECEIPT_LEDGER_REL
    records_dir = ledger_root / "records"
    try:
        _ensure_ledger_dirs(ledger_root, records_dir)
    except OSError:
        return _ledger_error(receipt.get("schema_version"))

    schema_version = receipt["schema_version"]
    digest = receipt_digest(receipt)
    identity = identity_tuple(receipt)
    identity_key = _identity_key(receipt)
    filename = hashlib.sha256(identity_key.encode("utf-8")).hexdigest() + ".json"
    final_path = records_dir / filename

    record = {
        "schema_version": LEDGER_RECORD_SCHEMA_VERSION,
        "receipt_schema_version": schema_version,
        "identity": list(identity),
        "receipt_digest": digest,
    }
    data = _ledger_record_bytes(record)
    try:
        linked = _write_once(records_dir, final_path, data)
    except OSError:
        return _ledger_error(schema_version)
    if linked:
        return Verdict(
            state="accepted",
            schema_version=schema_version,
            receipt=receipt,
            digest=digest,
            identity=identity,
        )

    try:
        existing_raw = final_path.read_text(encoding="utf-8")
        existing = json.loads(existing_raw)
    except (OSError, ValueError):
        return _ledger_error(schema_version)
    if not isinstance(existing, dict) or set(existing.keys()) != set(_LEDGER_RECORD_ORDER):
        return _ledger_error(schema_version)
    if existing.get("identity") != list(identity):
        return _ledger_error(schema_version)
    if existing.get("receipt_digest") == digest:
        return Verdict(
            state="noop-idempotent",
            schema_version=schema_version,
            receipt=receipt,
            digest=digest,
            identity=identity,
        )
    return Verdict(
        state="rejected",
        reason="identity-conflict",
        schema_version=schema_version,
        receipt=receipt,
        digest=digest,
        identity=identity,
    )


# ---------------------------------------------------------------------------
# publication result mapping (caller-side translation table; see
# artifact_lifecycle.PUBLICATION_RESULTS / artifact-sink.sh / note-publication
# AS-3 for the owning enums -- neither is modified here)
# ---------------------------------------------------------------------------

PUBLICATION_BY_SINK_EXIT: Mapping[Optional[int], str] = {
    None: "not-offered",
    69: "skipped",
    0: "succeeded",
}


def publication_result_for_sink_exit(exit_code: Optional[int]) -> str:
    """Translate a sink invocation's exit status to a publication result word.

    The four-word closed set is owned by `artifact_lifecycle.PUBLICATION_RESULTS`
    (step-2, unmodified here). Which sink exit maps to which condition is owned
    by `artifact-sink.sh` / note-publication AS-3. This function is only a
    caller-side lookup table, not a redefinition of either owning surface.
    """
    if exit_code in PUBLICATION_BY_SINK_EXIT:
        return PUBLICATION_BY_SINK_EXIT[exit_code]
    return "failed"


# ---------------------------------------------------------------------------
# build_v3 / CLI
# ---------------------------------------------------------------------------


def build_v3(
    *,
    completed_at: str,
    repository_id: str,
    campaign_id: str,
    cycle_id: str,
    artifact_id: str,
    artifact_revision_id: str,
    manifest_id: str,
    manifest_revision_id: str,
) -> Dict[str, Any]:
    return {
        "schema_version": 3,
        "event": EVENT_LITERAL,
        "status": STATUS_LITERAL,
        "completed_at": completed_at,
        "repository_id": repository_id,
        "campaign_id": campaign_id,
        "cycle_id": cycle_id,
        "artifact_id": artifact_id,
        "artifact_revision_id": artifact_revision_id,
        "manifest_id": manifest_id,
        "manifest_revision_id": manifest_revision_id,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="artifact_receipt.py", add_help=False)
    parser.add_argument("--decode")
    parser.add_argument("--emit-v3", action="store_true")
    parser.add_argument("--artifact-root")
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--completed-at")
    parser.add_argument("--repository-id")
    parser.add_argument("--campaign-id")
    parser.add_argument("--cycle-id")
    parser.add_argument("--artifact-id")
    parser.add_argument("--artifact-revision-id")
    parser.add_argument("--manifest-id")
    parser.add_argument("--manifest-revision-id")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_arg_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(raw_args)
    except SystemExit:
        sys.stdout.write("state=rejected\nreason=value-invalid\n")
        return EXIT_REFUSED

    if args.decode:
        try:
            data = Path(args.decode).read_bytes()
        except OSError:
            sys.stdout.write("state=unavailable\nreason=local-state-unreadable\n")
            return EXIT_UNAVAILABLE
        verdict = decode_bytes(data)
        if verdict.ok() and args.artifact_root:
            verdict = resolve(args.artifact_root, verdict.receipt)
        if verdict.ok() and args.register:
            if not args.artifact_root:
                sys.stdout.write("state=rejected\nreason=value-invalid\ndetail=$.artifact_root\n")
                return EXIT_REFUSED
            verdict = register(args.artifact_root, verdict.receipt)
        sys.stdout.write(verdict.render())
        return verdict.exit_code()

    if args.emit_v3:
        required = {
            "out": args.out,
            "artifact_root": args.artifact_root,
            "completed_at": args.completed_at,
            "repository_id": args.repository_id,
            "campaign_id": args.campaign_id,
            "cycle_id": args.cycle_id,
            "artifact_id": args.artifact_id,
            "artifact_revision_id": args.artifact_revision_id,
            "manifest_id": args.manifest_id,
            "manifest_revision_id": args.manifest_revision_id,
        }
        if any(value is None for value in required.values()):
            sys.stdout.write("state=rejected\nreason=value-invalid\n")
            return EXIT_REFUSED
        receipt = build_v3(
            completed_at=args.completed_at,
            repository_id=args.repository_id,
            campaign_id=args.campaign_id,
            cycle_id=args.cycle_id,
            artifact_id=args.artifact_id,
            artifact_revision_id=args.artifact_revision_id,
            manifest_id=args.manifest_id,
            manifest_revision_id=args.manifest_revision_id,
        )
        verdict = decode(receipt)
        if verdict.ok():
            verdict = resolve(args.artifact_root, verdict.receipt)
        if verdict.ok():
            verdict = register(args.artifact_root, verdict.receipt)
        if verdict.ok():
            # OD-14: the receipt file write is local state too, so its failure
            # stays inside the typed 0/64/69/70 vocabulary instead of escaping
            # as exit 1 and being misattributed to a handler failure.
            try:
                data = canonical_bytes(verdict.receipt)
                Path(args.out).write_bytes(data)
                os.chmod(args.out, 0o600)
            except OSError:
                verdict = _ledger_error(verdict.schema_version)
        sys.stdout.write(verdict.render())
        return verdict.exit_code()

    sys.stdout.write("state=rejected\nreason=value-invalid\n")
    return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
