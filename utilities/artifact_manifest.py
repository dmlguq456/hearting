from __future__ import annotations

"""artifact-cycle-manifest/v2 closed schema validation (D-6), event envelope
(D-11), locator safety, lineage/completeness/transition checks, and canonical
byte serialization.

All functions here are pure: no filesystem access, no clock reads except
values the caller passes in the document itself (which are audit-only and
never used for sorting or identity).
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from artifact_identity import is_well_formed, kind_of

CONTRACT_VERSION = "artifact-cycle-manifest/v2"
SCHEMA_VERSION = 2
MANIFEST_KIND = "artifact.cycle"

_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
_LOCATOR_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

_MAX_LOCATOR_COMPONENTS = 32
_MAX_LOCATOR_LENGTH = 1024
_MAX_PAYLOAD_DEPTH = 16
_MAX_PAYLOAD_BYTES = 64 * 1024

_EVENT_TYPES = frozenset(
    {
        "campaign.satisfied",
        "campaign.abandoned",
        "campaign.superseded",
        "cycle.completed",
        "cycle.abandoned",
        "cycle.superseded",
        "artifact.revision.recorded",
        "shared_reference.revision.selected",
        "route.terminal.recorded",
        "decision.recorded",
        "evidence.recorded",
        "user.correction.recorded",
    }
)

_ACTOR_KINDS = frozenset({"user", "producer", "system", "curator-proposal-accepted"})

_SHARED_REFERENCE_KINDS = frozenset(
    {"shared-spec", "cumulative-analysis", "shared-research"}
)

_CAMPAIGN_TERMINAL_EVENTS = {
    "satisfied": "campaign.satisfied",
    "abandoned": "campaign.abandoned",
    "superseded": "campaign.superseded",
}
_CYCLE_TERMINAL_EVENTS = {
    "completed": "cycle.completed",
    "abandoned": "cycle.abandoned",
    "superseded": "cycle.superseded",
}

_RESERVED_LOCATOR_NAMES = frozenset({"manifest.json"})


# ---------------------------------------------------------------------------
# Violation / ValidationReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    code: str
    path: str
    detail: str

    def to_payload(self) -> Dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    violations: Tuple[Violation, ...]

    def to_payload(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": [v.to_payload() for v in self.violations],
        }

    def merged(self, other: "ValidationReport") -> "ValidationReport":
        merged_violations = _sort_violations(self.violations + other.violations)
        return ValidationReport(
            ok=self.ok and other.ok, violations=merged_violations
        )


def _sort_violations(violations: Tuple[Violation, ...]) -> Tuple[Violation, ...]:
    return tuple(sorted(violations, key=lambda v: (v.code, v.path, v.detail)))


def _report(violations: List[Violation]) -> ValidationReport:
    sorted_v = _sort_violations(tuple(violations))
    return ValidationReport(ok=(len(sorted_v) == 0), violations=sorted_v)


def _ok() -> ValidationReport:
    return ValidationReport(ok=True, violations=())


# ---------------------------------------------------------------------------
# Canonical bytes
# ---------------------------------------------------------------------------


def canonical_bytes(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def manifest_digest(document: Mapping[str, Any]) -> str:
    return digest_bytes(canonical_bytes(document))


# ---------------------------------------------------------------------------
# shape helpers
# ---------------------------------------------------------------------------


def _has_float(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_has_float(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_float(v) for v in value)
    return False


def _check_no_float(value: Any, path: str, violations: List[Violation]) -> None:
    if _has_float(value):
        violations.append(
            Violation("value-float-forbidden", path, "float value is forbidden")
        )


def _check_closed_object(
    obj: Any,
    path: str,
    required: Mapping[str, Any],
    optional: Mapping[str, Any],
    violations: List[Violation],
) -> bool:
    """required/optional map key -> a validator callable(value, path, violations).

    Returns True if obj had the right shape (dict) to continue nested checks.
    """
    if not isinstance(obj, dict):
        violations.append(Violation("shape-not-object", path, "expected an object"))
        return False
    allowed = set(required.keys()) | set(optional.keys())
    extra = set(obj.keys()) - allowed
    for key in sorted(extra):
        violations.append(
            Violation("unknown-key", "{0}.{1}".format(path, key), "unknown key")
        )
    missing = set(required.keys()) - set(obj.keys())
    for key in sorted(missing):
        violations.append(
            Violation("missing-key", "{0}.{1}".format(path, key), "missing required key")
        )
    for key, validator in required.items():
        if key in obj:
            validator(obj[key], "{0}.{1}".format(path, key), violations)
    for key, validator in optional.items():
        if key in obj:
            validator(obj[key], "{0}.{1}".format(path, key), violations)
    return True


def _v_str(value: Any, path: str, violations: List[Violation]) -> None:
    if not isinstance(value, str):
        violations.append(Violation("wrong-type", path, "expected string"))


def _v_nonempty_str(value: Any, path: str, violations: List[Violation]) -> None:
    if not isinstance(value, str) or not value:
        violations.append(Violation("wrong-type", path, "expected non-empty string"))


def _v_bool(value: Any, path: str, violations: List[Violation]) -> None:
    if not isinstance(value, bool):
        violations.append(Violation("wrong-type", path, "expected boolean"))


def _v_int(value: Any, path: str, violations: List[Violation]) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        violations.append(Violation("wrong-type", path, "expected integer"))


def _v_nonneg_int(value: Any, path: str, violations: List[Violation]) -> None:
    _v_int(value, path, violations)
    if isinstance(value, int) and not isinstance(value, bool) and value < 0:
        violations.append(Violation("wrong-value", path, "expected >= 0"))


def _v_pos_int(value: Any, path: str, violations: List[Violation]) -> None:
    _v_int(value, path, violations)
    if isinstance(value, int) and not isinstance(value, bool) and value < 1:
        violations.append(Violation("wrong-value", path, "expected >= 1"))


def _v_rfc3339(value: Any, path: str, violations: List[Violation]) -> None:
    if not isinstance(value, str) or not _RFC3339_RE.match(value):
        violations.append(Violation("malformed-timestamp", path, "expected RFC3339 UTC"))


def _v_digest(value: Any, path: str, violations: List[Violation]) -> None:
    if not isinstance(value, str) or not _DIGEST_RE.match(value):
        violations.append(Violation("malformed-digest", path, "expected sha256:<64 hex>"))


def _v_media_type(value: Any, path: str, violations: List[Violation]) -> None:
    if not isinstance(value, str) or not _MEDIA_TYPE_RE.match(value):
        violations.append(Violation("malformed-media-type", path, "expected media type"))


def _v_typed_id(kind: str):
    def validator(value: Any, path: str, violations: List[Violation]) -> None:
        if not isinstance(value, str) or not is_well_formed(value, kind):
            violations.append(
                Violation("malformed-typed-id", path, "expected {0} id".format(kind))
            )

    return validator


def _v_typed_id_or_null(kind: str):
    def validator(value: Any, path: str, violations: List[Violation]) -> None:
        if value is None:
            return
        if not isinstance(value, str) or not is_well_formed(value, kind):
            violations.append(
                Violation(
                    "malformed-typed-id", path, "expected {0} id or null".format(kind)
                )
            )

    return validator


def _v_list_of_str_no_dup(value: Any, path: str, violations: List[Violation]) -> None:
    if not isinstance(value, list):
        violations.append(Violation("wrong-type", path, "expected list"))
        return
    seen = set()
    for i, item in enumerate(value):
        item_path = "{0}[{1}]".format(path, i)
        if not isinstance(item, str):
            violations.append(Violation("wrong-type", item_path, "expected string"))
            continue
        if item in seen:
            violations.append(Violation("duplicate-item", item_path, "duplicate item"))
        seen.add(item)


def _v_list_of_str(value: Any, path: str, violations: List[Violation]) -> None:
    if not isinstance(value, list):
        violations.append(Violation("wrong-type", path, "expected list"))
        return
    for i, item in enumerate(value):
        item_path = "{0}[{1}]".format(path, i)
        if not isinstance(item, str):
            violations.append(Violation("wrong-type", item_path, "expected string"))


def _v_literal(expected: Any):
    def validator(value: Any, path: str, violations: List[Violation]) -> None:
        if value != expected:
            violations.append(
                Violation("wrong-literal", path, "expected {0!r}".format(expected))
            )

    return validator


def _v_enum(allowed: frozenset):
    def validator(value: Any, path: str, violations: List[Violation]) -> None:
        if value not in allowed:
            violations.append(
                Violation("wrong-value", path, "expected one of {0}".format(sorted(allowed)))
            )

    return validator


# ---------------------------------------------------------------------------
# nested object shapes
# ---------------------------------------------------------------------------


def _v_completion_criterion(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(value, path, {"statement": _v_str}, {}, violations)


def _v_campaign(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {
            "campaign_id": _v_typed_id("campaign"),
            "goal": _v_str,
            "completion_criterion": _v_completion_criterion,
            "title": _v_str,
            "state": _v_str,
        },
        {},
        violations,
    )


def _v_outcome_criterion(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {
            "required_artifact_roles": _v_list_of_str_no_dup,
            "decision_required": _v_bool,
        },
        {},
        violations,
    )


def _v_cycle(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {
            "cycle_id": _v_typed_id("cycle"),
            "campaign_id": _v_typed_id("campaign"),
            "parent_cycle_id": _v_typed_id_or_null("cycle"),
            "started_on": _v_rfc3339,
            "input_digest": _v_digest,
            "outcome_criterion": _v_outcome_criterion,
            "state": _v_str,
        },
        {},
        violations,
    )


def _v_artifact_row(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {
            "artifact_id": _v_typed_id("artifact"),
            "cycle_id": _v_typed_id("cycle"),
            "role": _v_str,
            "type": _v_str,
            "capability": _v_str,
            "title": _v_str,
        },
        {},
        violations,
    )


def _v_locator(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {"kind": _v_literal("cycle-relative"), "path": _v_str},
        {},
        violations,
    )


def _v_provenance(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {
            "source_manifest_id": _v_typed_id("manifest"),
            "source_revision_id": _v_typed_id("manifest_revision"),
            "producer_route_id": _v_str,
            "algorithm_version": _v_str,
            "schema_version": _v_int,
            "source_digest": _v_digest,
        },
        {},
        violations,
    )


def _v_artifact_revision_row(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {
            "artifact_revision_id": _v_typed_id("artifact_revision"),
            "artifact_id": _v_typed_id("artifact"),
            "revision_sequence": _v_pos_int,
            "content_digest": _v_digest,
            "byte_size": _v_nonneg_int,
            "media_type": _v_media_type,
            "locator": _v_locator,
            "provenance": _v_provenance,
        },
        {},
        violations,
    )


def _v_shared_reference_row(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {
            "shared_reference_id": _v_typed_id("shared_reference"),
            "kind": _v_enum(_SHARED_REFERENCE_KINDS),
            "title": _v_str,
        },
        {},
        violations,
    )


def _v_shared_reference_revision_row(
    value: Any, path: str, violations: List[Violation]
) -> None:
    _check_closed_object(
        value,
        path,
        {
            "shared_reference_revision_id": _v_typed_id("shared_reference_revision"),
            "shared_reference_id": _v_typed_id("shared_reference"),
            "content_digest": _v_digest,
            "updated_at": _v_rfc3339,
            "provenance": _v_provenance,
        },
        {},
        violations,
    )


def _v_route_row(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {
            "artifact_root_id": _v_typed_id("artifact_root"),
            "route_id": _v_nonempty_str,
            "route_hash": _v_digest,
            "terminal_marker": _v_str,
            "terminal_evidence_id": _v_str,
        },
        {},
        violations,
    )


def _v_event_actor(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {"kind": _v_enum(_ACTOR_KINDS), "id": _v_nonempty_str},
        {},
        violations,
    )


def _v_event_payload(value: Any, path: str, violations: List[Violation]) -> None:
    if not isinstance(value, dict):
        violations.append(Violation("wrong-type", path, "expected object"))
        return
    _check_no_float(value, path, violations)
    depth = _max_depth(value)
    if depth > _MAX_PAYLOAD_DEPTH:
        violations.append(
            Violation("payload-too-deep", path, "depth exceeds {0}".format(_MAX_PAYLOAD_DEPTH))
        )
    if not _all_keys_str(value):
        violations.append(Violation("wrong-type", path, "all payload keys must be strings"))
    try:
        size = len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            .encode("utf-8")
        )
    except (TypeError, ValueError):
        size = _MAX_PAYLOAD_BYTES + 1
    if size > _MAX_PAYLOAD_BYTES:
        violations.append(
            Violation("oversized-payload", path, "serialized payload exceeds 64 KiB")
        )


def _max_depth(value: Any) -> int:
    if isinstance(value, dict):
        if not value:
            return 1
        return 1 + max(_max_depth(v) for v in value.values())
    if isinstance(value, list):
        if not value:
            return 1
        return 1 + max(_max_depth(v) for v in value)
    return 0


def _all_keys_str(value: Any) -> bool:
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                return False
            if not _all_keys_str(v):
                return False
    elif isinstance(value, list):
        for v in value:
            if not _all_keys_str(v):
                return False
    return True


def _v_event_row(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {
            "event_id": _v_typed_id("event"),
            "stream_id": _v_typed_id("stream"),
            "stream_sequence": _v_pos_int,
            "event_type": _v_enum(_EVENT_TYPES),
            "target_id": _v_nonempty_str,
            "actor": _v_event_actor,
            "recorded_at": _v_rfc3339,
            "provenance": _v_provenance,
            "evidence_ids": _v_list_of_str,
            "payload": _v_event_payload,
        },
        {
            "supersedes_event_id": _v_typed_id("event"),
            "revokes_event_id": _v_typed_id("event"),
        },
        violations,
    )


def _v_producer(value: Any, path: str, violations: List[Violation]) -> None:
    def _v_source_revision(v, p, viols):
        if not isinstance(v, str) or not (1 <= len(v) <= 200):
            viols.append(Violation("wrong-value", p, "expected string of length 1..200"))

    _check_closed_object(
        value,
        path,
        {
            "producer_id": _v_typed_id("producer"),
            "contract_version": _v_literal(CONTRACT_VERSION),
            "source_revision": _v_source_revision,
        },
        {},
        violations,
    )


def _v_array(item_validator):
    def validator(value: Any, path: str, violations: List[Violation]) -> None:
        if not isinstance(value, list):
            violations.append(Violation("wrong-type", path, "expected array"))
            return
        for i, item in enumerate(value):
            item_validator(item, "{0}[{1}]".format(path, i), violations)

    return validator


_TOP_REQUIRED = {
    "schema_version": _v_literal(SCHEMA_VERSION),
    "manifest_kind": _v_literal(MANIFEST_KIND),
    "manifest_id": _v_typed_id("manifest"),
    "manifest_revision_id": _v_typed_id("manifest_revision"),
    "repository_id": _v_typed_id("repository"),
    "artifact_root_id": _v_typed_id("artifact_root"),
    "campaign": _v_campaign,
    "cycle": _v_cycle,
    "artifacts": _v_array(_v_artifact_row),
    "artifact_revisions": _v_array(_v_artifact_revision_row),
    "shared_references": _v_array(_v_shared_reference_row),
    "shared_reference_revisions": _v_array(_v_shared_reference_revision_row),
    "routes": _v_array(_v_route_row),
    "events": _v_array(_v_event_row),
    "producer": _v_producer,
}


def validate_shape(document: Any) -> ValidationReport:
    violations: List[Violation] = []
    if not isinstance(document, dict):
        return _report([Violation("shape-not-object", "$", "document must be an object")])
    _check_closed_object(document, "$", _TOP_REQUIRED, {}, violations)
    _check_no_float(document, "$", violations)
    return _report(violations)


# ---------------------------------------------------------------------------
# locator safety
# ---------------------------------------------------------------------------


def _locator_violation(code: str, path: str, detail: str) -> Violation:
    return Violation(code, path, detail)


def _check_locator_path(path_value: Any, vpath: str, violations: List[Violation]) -> None:
    if not isinstance(path_value, str):
        return  # already reported by shape validation
    if path_value == "":
        violations.append(_locator_violation("locator-empty", vpath, "empty locator path"))
        return
    if path_value.startswith("/"):
        violations.append(_locator_violation("locator-absolute", vpath, "absolute path"))
        return
    if re.match(r"^[A-Za-z]:", path_value) or path_value.startswith("\\\\"):
        violations.append(_locator_violation("locator-absolute", vpath, "drive/UNC path"))
        return
    if "\\" in path_value:
        violations.append(_locator_violation("locator-backslash", vpath, "backslash in path"))
        return
    if _CONTROL_CHAR_RE.search(path_value):
        violations.append(
            _locator_violation("locator-control-char", vpath, "control character in path")
        )
        return
    if path_value.endswith("/"):
        violations.append(_locator_violation("locator-trailing-slash", vpath, "trailing slash"))
        return
    if len(path_value) > _MAX_LOCATOR_LENGTH:
        violations.append(_locator_violation("locator-too-long", vpath, "path too long"))
        return
    components = path_value.split("/")
    if len(components) > _MAX_LOCATOR_COMPONENTS:
        violations.append(
            _locator_violation("locator-too-many-components", vpath, "too many components")
        )
        return
    for comp in components:
        if comp == "":
            violations.append(
                _locator_violation("locator-empty-component", vpath, "empty component")
            )
            return
        if comp in (".", ".."):
            violations.append(
                _locator_violation("locator-dot-segment", vpath, "dot segment")
            )
            return
        if comp.startswith("."):
            violations.append(
                _locator_violation("locator-hidden-component", vpath, "hidden component")
            )
            return
        if not _LOCATOR_COMPONENT_RE.match(comp):
            violations.append(
                _locator_violation("locator-invalid-component", vpath, "invalid component")
            )
            return
    if components[-1] in _RESERVED_LOCATOR_NAMES:
        violations.append(
            _locator_violation("locator-reserved-name", vpath, "reserved locator name")
        )


def validate_locators(document: Mapping[str, Any]) -> ValidationReport:
    violations: List[Violation] = []
    if not isinstance(document, dict):
        return _ok()
    revisions = document.get("artifact_revisions")
    if not isinstance(revisions, list):
        return _report(violations)
    seen_paths: Dict[str, int] = {}
    for i, rev in enumerate(revisions):
        if not isinstance(rev, dict):
            continue
        locator = rev.get("locator")
        if not isinstance(locator, dict):
            continue
        path_value = locator.get("path")
        vpath = "$.artifact_revisions[{0}].locator.path".format(i)
        _check_locator_path(path_value, vpath, violations)
        if isinstance(path_value, str) and path_value:
            if path_value in seen_paths:
                violations.append(
                    Violation("locator-duplicate-path", vpath, "duplicate locator path")
                )
            else:
                seen_paths[path_value] = i
    return _report(violations)


# ---------------------------------------------------------------------------
# lineage / completeness / transition
# ---------------------------------------------------------------------------


def declared_ids(document: Mapping[str, Any]) -> Dict[str, str]:
    ids: Dict[str, str] = {}

    def add(value: Any, kind: str) -> None:
        if isinstance(value, str):
            ids[value] = kind

    campaign = document.get("campaign")
    if isinstance(campaign, dict):
        add(campaign.get("campaign_id"), "campaign")
    cycle = document.get("cycle")
    if isinstance(cycle, dict):
        add(cycle.get("cycle_id"), "cycle")
    for row in document.get("artifacts", []) or []:
        if isinstance(row, dict):
            add(row.get("artifact_id"), "artifact")
    for row in document.get("artifact_revisions", []) or []:
        if isinstance(row, dict):
            add(row.get("artifact_revision_id"), "artifact_revision")
    for row in document.get("shared_references", []) or []:
        if isinstance(row, dict):
            add(row.get("shared_reference_id"), "shared_reference")
    for row in document.get("shared_reference_revisions", []) or []:
        if isinstance(row, dict):
            add(row.get("shared_reference_revision_id"), "shared_reference_revision")
    for row in document.get("events", []) or []:
        if isinstance(row, dict):
            add(row.get("event_id"), "event")
    manifest_id = document.get("manifest_id")
    if isinstance(manifest_id, str):
        ids[manifest_id] = "manifest"
    manifest_revision_id = document.get("manifest_revision_id")
    if isinstance(manifest_revision_id, str):
        ids[manifest_revision_id] = "manifest_revision"
    return ids


def declared_routes(document: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
    out = []
    for row in document.get("routes", []) or []:
        if isinstance(row, dict):
            out.append((row.get("artifact_root_id"), row.get("route_id")))
    return tuple(out)


def declared_streams(document: Mapping[str, Any]) -> Dict[str, Tuple[int, int]]:
    result: Dict[str, Tuple[int, int]] = {}
    for row in document.get("events", []) or []:
        if not isinstance(row, dict):
            continue
        stream_id = row.get("stream_id")
        seq = row.get("stream_sequence")
        if not isinstance(stream_id, str) or not isinstance(seq, int) or isinstance(seq, bool):
            continue
        if stream_id not in result:
            result[stream_id] = (seq, seq)
        else:
            lo, hi = result[stream_id]
            result[stream_id] = (min(lo, seq), max(hi, seq))
    return result


def declared_locators(
    document: Mapping[str, Any],
) -> Tuple[Tuple[str, str, int, str], ...]:
    out = []
    for row in document.get("artifact_revisions", []) or []:
        if not isinstance(row, dict):
            continue
        locator = row.get("locator")
        if not isinstance(locator, dict):
            continue
        out.append(
            (
                locator.get("path"),
                row.get("content_digest"),
                row.get("byte_size"),
                row.get("media_type"),
            )
        )
    return tuple(out)


def _dup_check(
    values, code: str, path_prefix: str, violations: List[Violation]
) -> None:
    seen: Dict[Any, int] = {}
    for i, v in enumerate(values):
        if v is None:
            continue
        if v in seen:
            violations.append(
                Violation(code, "{0}[{1}]".format(path_prefix, i), "duplicate: {0!r}".format(v))
            )
        else:
            seen[v] = i


def validate_lineage(document: Mapping[str, Any]) -> ValidationReport:
    violations: List[Violation] = []
    if not isinstance(document, dict):
        return _ok()

    campaign = document.get("campaign") if isinstance(document.get("campaign"), dict) else {}
    cycle = document.get("cycle") if isinstance(document.get("cycle"), dict) else {}
    artifacts = [r for r in document.get("artifacts", []) or [] if isinstance(r, dict)]
    artifact_revisions = [
        r for r in document.get("artifact_revisions", []) or [] if isinstance(r, dict)
    ]
    shared_references = [
        r for r in document.get("shared_references", []) or [] if isinstance(r, dict)
    ]
    shared_reference_revisions = [
        r for r in document.get("shared_reference_revisions", []) or [] if isinstance(r, dict)
    ]
    routes = [r for r in document.get("routes", []) or [] if isinstance(r, dict)]
    events = [r for r in document.get("events", []) or [] if isinstance(r, dict)]

    campaign_id = campaign.get("campaign_id")
    cycle_id = cycle.get("cycle_id")
    top_root_id = document.get("artifact_root_id")

    if cycle.get("campaign_id") is not None and cycle.get("campaign_id") != campaign_id:
        violations.append(
            Violation("cycle-campaign-id-mismatch", "$.cycle.campaign_id", "does not match campaign.campaign_id")
        )

    artifact_ids = set()
    for i, row in enumerate(artifacts):
        artifact_ids.add(row.get("artifact_id"))
        if row.get("cycle_id") is not None and row.get("cycle_id") != cycle_id:
            violations.append(
                Violation(
                    "artifact-cycle-id-mismatch",
                    "$.artifacts[{0}].cycle_id".format(i),
                    "does not match cycle.cycle_id",
                )
            )

    for i, row in enumerate(artifact_revisions):
        if row.get("artifact_id") not in artifact_ids:
            violations.append(
                Violation(
                    "orphan-artifact-revision",
                    "$.artifact_revisions[{0}].artifact_id".format(i),
                    "artifact_id not declared in artifacts[]",
                )
            )

    # D-8.3 -- the minimum full lineage includes the FIRST revision of every
    # declared artifact. An artifacts[] row with no revision_sequence == 1 row
    # is partial lineage and is refused before anything is committed.
    first_revision_owners = {
        row.get("artifact_id")
        for row in artifact_revisions
        if row.get("revision_sequence") == 1
    }
    for i, row in enumerate(artifacts):
        artifact_id = row.get("artifact_id")
        if artifact_id is not None and artifact_id not in first_revision_owners:
            violations.append(
                Violation(
                    "partial-lineage-missing-first-revision",
                    "$.artifacts[{0}].artifact_id".format(i),
                    "artifact has no artifact_revisions[] row with revision_sequence 1",
                )
            )

    shared_reference_ids = {row.get("shared_reference_id") for row in shared_references}
    for i, row in enumerate(shared_reference_revisions):
        if row.get("shared_reference_id") not in shared_reference_ids:
            violations.append(
                Violation(
                    "orphan-shared-reference-revision",
                    "$.shared_reference_revisions[{0}].shared_reference_id".format(i),
                    "shared_reference_id not declared in shared_references[]",
                )
            )

    for i, row in enumerate(routes):
        if row.get("artifact_root_id") is not None and row.get("artifact_root_id") != top_root_id:
            violations.append(
                Violation(
                    "route-root-id-mismatch",
                    "$.routes[{0}].artifact_root_id".format(i),
                    "does not match top-level artifact_root_id",
                )
            )

    all_ids = set(declared_ids(document).keys())
    for i, row in enumerate(events):
        target_id = row.get("target_id")
        if target_id is not None and target_id not in all_ids:
            violations.append(
                Violation(
                    "event-target-id-not-declared",
                    "$.events[{0}].target_id".format(i),
                    "target_id not declared anywhere in this manifest",
                )
            )
        evidence_ids = row.get("evidence_ids")
        if isinstance(evidence_ids, list):
            for j, evid in enumerate(evidence_ids):
                if isinstance(evid, str) and evid not in all_ids:
                    violations.append(
                        Violation(
                            "unresolvable-evidence-id",
                            "$.events[{0}].evidence_ids[{1}]".format(i, j),
                            "evidence id not resolvable within this manifest",
                        )
                    )

    # revision sequence continuity per artifact_id
    by_artifact: Dict[Any, List[int]] = {}
    for row in artifact_revisions:
        seq = row.get("revision_sequence")
        if isinstance(seq, int) and not isinstance(seq, bool):
            by_artifact.setdefault(row.get("artifact_id"), []).append(seq)
    for artifact_id, seqs in by_artifact.items():
        ordered = sorted(seqs)
        if ordered and ordered[0] != 1:
            violations.append(
                Violation(
                    "revision-append-out-of-scope",
                    "$.artifact_revisions[?artifact_id={0!r}]".format(artifact_id),
                    "revision_sequence does not start at 1",
                )
            )
        for a, b in zip(ordered, ordered[1:]):
            if b == a:
                violations.append(
                    Violation(
                        "reused-revision-sequence",
                        "$.artifact_revisions[?artifact_id={0!r}]".format(artifact_id),
                        "duplicate revision_sequence",
                    )
                )
            elif b != a + 1:
                violations.append(
                    Violation(
                        "revision-sequence-gap",
                        "$.artifact_revisions[?artifact_id={0!r}]".format(artifact_id),
                        "gap in revision_sequence",
                    )
                )

    # duplicate stable IDs within manifest
    all_typed_ids: List[Any] = []
    if campaign_id is not None:
        all_typed_ids.append(campaign_id)
    if cycle_id is not None:
        all_typed_ids.append(cycle_id)
    all_typed_ids.extend(row.get("artifact_id") for row in artifacts)
    all_typed_ids.extend(row.get("artifact_revision_id") for row in artifact_revisions)
    all_typed_ids.extend(row.get("shared_reference_id") for row in shared_references)
    all_typed_ids.extend(
        row.get("shared_reference_revision_id") for row in shared_reference_revisions
    )
    _dup_check(all_typed_ids, "duplicate-stable-id", "$.<stable-ids>", violations)

    revision_ids = [row.get("artifact_revision_id") for row in artifact_revisions]
    _dup_check(revision_ids, "reused-revision-id", "$.artifact_revisions", violations)

    event_ids = [row.get("event_id") for row in events]
    _dup_check(event_ids, "reused-event-id", "$.events", violations)

    route_composites = [(row.get("artifact_root_id"), row.get("route_id")) for row in routes]
    _dup_check(route_composites, "duplicate-route-composite", "$.routes", violations)

    # transition legality
    event_type_by_id = {}
    for row in events:
        et = row.get("event_type")
        if et is not None and et not in _EVENT_TYPES:
            violations.append(
                Violation(
                    "unknown-event-type",
                    "$.events[?event_id={0!r}]".format(row.get("event_id")),
                    "unknown event_type",
                )
            )
        event_type_by_id[row.get("event_id")] = et

    campaign_events = [row for row in events if row.get("target_id") == campaign_id]
    cycle_events = [row for row in events if row.get("target_id") == cycle_id]

    campaign_state = campaign.get("state")
    if campaign_state in _CAMPAIGN_TERMINAL_EVENTS:
        needed = _CAMPAIGN_TERMINAL_EVENTS[campaign_state]
        matching = [row for row in campaign_events if row.get("event_type") == needed]
        if not matching:
            violations.append(
                Violation(
                    "illegal-transition",
                    "$.campaign.state",
                    "declared state {0!r} unreachable from events".format(campaign_state),
                )
            )
        if campaign_state == "satisfied":
            authorized = [
                row for row in matching if isinstance(row.get("actor"), dict)
                and row["actor"].get("kind") == "user"
            ]
            if not authorized:
                violations.append(
                    Violation(
                        "campaign-satisfaction-unauthorized",
                        "$.campaign.state",
                        "campaign.satisfied event must have actor.kind == 'user'",
                    )
                )
    elif campaign_state is not None and campaign_state != "active":
        violations.append(
            Violation("unknown-event-type", "$.campaign.state", "unrecognised campaign state")
        )

    cycle_state = cycle.get("state")
    if cycle_state in _CYCLE_TERMINAL_EVENTS:
        needed = _CYCLE_TERMINAL_EVENTS[cycle_state]
        matching = [row for row in cycle_events if row.get("event_type") == needed]
        if not matching:
            violations.append(
                Violation(
                    "illegal-transition",
                    "$.cycle.state",
                    "declared state {0!r} unreachable from events".format(cycle_state),
                )
            )
        if cycle_state == "completed":
            outcome = cycle.get("outcome_criterion")
            outcome = outcome if isinstance(outcome, dict) else {}
            required_roles = outcome.get("required_artifact_roles") or []
            roles_present = set()
            for rev in artifact_revisions:
                art = next(
                    (a for a in artifacts if a.get("artifact_id") == rev.get("artifact_id")),
                    None,
                )
                if art is not None:
                    roles_present.add(art.get("role"))
            for role in required_roles:
                if role not in roles_present:
                    violations.append(
                        Violation(
                            "cycle-completion-incomplete",
                            "$.cycle.outcome_criterion.required_artifact_roles",
                            "missing-required-artifact: role {0!r} has no revision".format(role),
                        )
                    )
            terminal_route_events = [
                row for row in cycle_events if row.get("event_type") == "route.terminal.recorded"
            ]
            if not terminal_route_events:
                violations.append(
                    Violation(
                        "cycle-completion-incomplete",
                        "$.cycle.state",
                        "completed cycle missing route.terminal.recorded event",
                    )
                )
            if outcome.get("decision_required"):
                decision_events = [
                    row for row in cycle_events if row.get("event_type") == "decision.recorded"
                ]
                if not decision_events:
                    violations.append(
                        Violation(
                            "cycle-completion-incomplete",
                            "$.cycle.state",
                            "completed cycle missing decision.recorded event",
                        )
                    )
    elif cycle_state is not None and cycle_state != "active":
        violations.append(
            Violation("unknown-event-type", "$.cycle.state", "unrecognised cycle state")
        )

    # out-of-terminal transitions: any event whose target already reached a
    # terminal state via an earlier (lower stream_sequence) event of the same
    # stream, and that is itself a *different* terminal event, is illegal.
    _check_no_transition_out_of_terminal(campaign_events, "campaign", violations)
    _check_no_transition_out_of_terminal(cycle_events, "cycle", violations)

    return _report(violations)


def _check_no_transition_out_of_terminal(events, label, violations) -> None:
    terminal_types = set(_CAMPAIGN_TERMINAL_EVENTS.values()) | set(
        _CYCLE_TERMINAL_EVENTS.values()
    )
    ordered = sorted(
        (row for row in events if isinstance(row.get("stream_sequence"), int)),
        key=lambda r: (r.get("stream_id"), r.get("stream_sequence")),
    )
    seen_terminal = {}
    for row in ordered:
        stream_id = row.get("stream_id")
        if stream_id in seen_terminal:
            violations.append(
                Violation(
                    "illegal-transition",
                    "$.events[?event_id={0!r}]".format(row.get("event_id")),
                    "transition out of terminal state",
                )
            )
        if row.get("event_type") in terminal_types:
            seen_terminal[stream_id] = row.get("event_type")


# ---------------------------------------------------------------------------
# event envelope (D-11)
# ---------------------------------------------------------------------------


def validate_events(document: Mapping[str, Any]) -> ValidationReport:
    violations: List[Violation] = []
    if not isinstance(document, dict):
        return _ok()
    events = [r for r in document.get("events", []) or [] if isinstance(r, dict)]

    by_stream: Dict[Any, List[Dict[str, Any]]] = {}
    for row in events:
        by_stream.setdefault(row.get("stream_id"), []).append(row)

    for stream_id, rows in by_stream.items():
        seqs = [r.get("stream_sequence") for r in rows]
        int_seqs = [s for s in seqs if isinstance(s, int) and not isinstance(s, bool)]
        counts: Dict[int, int] = {}
        for s in int_seqs:
            counts[s] = counts.get(s, 0) + 1
        for s, c in counts.items():
            if c > 1:
                violations.append(
                    Violation(
                        "event-sequence-duplicate",
                        "$.events[?stream_id={0!r}][seq={1}]".format(stream_id, s),
                        "duplicate stream_sequence",
                    )
                )
        distinct_sorted = sorted(set(int_seqs))
        if distinct_sorted:
            if distinct_sorted[0] != 1:
                violations.append(
                    Violation(
                        "event-sequence-gap",
                        "$.events[?stream_id={0!r}]".format(stream_id),
                        "stream_sequence does not start at 1",
                    )
                )
            for a, b in zip(distinct_sorted, distinct_sorted[1:]):
                if b != a + 1:
                    violations.append(
                        Violation(
                            "event-sequence-gap",
                            "$.events[?stream_id={0!r}]".format(stream_id),
                            "gap between {0} and {1}".format(a, b),
                        )
                    )
        # nonmonotonic detection is implied by dup/gap checks above given a
        # sort; explicit check retained for documents whose declared order
        # (list order) is not already sorted by sequence.
        raw_order = [s for s in seqs if isinstance(s, int) and not isinstance(s, bool)]
        if raw_order != sorted(raw_order) and len(set(raw_order)) == len(raw_order):
            violations.append(
                Violation(
                    "event-sequence-nonmonotonic",
                    "$.events[?stream_id={0!r}]".format(stream_id),
                    "declared order is not monotonic by stream_sequence",
                )
            )

    event_ids = [row.get("event_id") for row in events]
    _dup_check(event_ids, "event-id-reused", "$.events", violations)

    events_by_id = {row.get("event_id"): row for row in events}
    superseded_targets: Dict[str, str] = {}
    revoked_targets: Dict[str, str] = {}
    for row in events:
        eid = row.get("event_id")
        supersedes = row.get("supersedes_event_id")
        revokes = row.get("revokes_event_id")
        if supersedes is not None and revokes is not None:
            violations.append(
                Violation(
                    "event-supersede-and-revoke",
                    "$.events[?event_id={0!r}]".format(eid),
                    "supersedes_event_id and revokes_event_id both set",
                )
            )
        for target, kind, bucket in (
            (supersedes, "supersedes", superseded_targets),
            (revokes, "revokes", revoked_targets),
        ):
            if target is None:
                continue
            if target == eid:
                violations.append(
                    Violation(
                        "event-self-supersession",
                        "$.events[?event_id={0!r}]".format(eid),
                        "{0} targets itself".format(kind),
                    )
                )
                continue
            if target not in events_by_id:
                violations.append(
                    Violation(
                        "event-dangling-supersession-target",
                        "$.events[?event_id={0!r}]".format(eid),
                        "{0} target {1!r} not present in manifest".format(kind, target),
                    )
                )
                continue
            if target in bucket:
                violations.append(
                    Violation(
                        "event-double-supersession",
                        "$.events[?event_id={0!r}]".format(eid),
                        "target {0!r} already {1} by another event".format(target, kind),
                    )
                )
            else:
                bucket[target] = eid
            target_row = events_by_id.get(target)
            if (
                isinstance(target_row, dict)
                and isinstance(target_row.get("actor"), dict)
                and target_row["actor"].get("kind") == "user"
                and isinstance(row.get("actor"), dict)
                and row["actor"].get("kind") == "curator-proposal-accepted"
            ):
                violations.append(
                    Violation(
                        "event-supersession-unauthorized",
                        "$.events[?event_id={0!r}]".format(eid),
                        "curator-proposal-accepted actor cannot {0} a user event".format(kind),
                    )
                )

    return _report(violations)


# ---------------------------------------------------------------------------
# fold_events
# ---------------------------------------------------------------------------


def fold_events(document: Mapping[str, Any]) -> Tuple[Dict[str, Any], ...]:
    events = [r for r in document.get("events", []) or [] if isinstance(r, dict)]

    superseded_or_revoked = set()
    for row in events:
        for key in ("supersedes_event_id", "revokes_event_id"):
            target = row.get(key)
            if target is not None:
                superseded_or_revoked.add(target)

    live = [row for row in events if row.get("event_id") not in superseded_or_revoked]
    ordered = sorted(
        live,
        key=lambda r: (
            r.get("stream_id") or "",
            r.get("stream_sequence") if isinstance(r.get("stream_sequence"), int) else 0,
        ),
    )
    return tuple(dict(row) for row in ordered)


# ---------------------------------------------------------------------------
# validate() = deterministic union
# ---------------------------------------------------------------------------


def validate(document: Any) -> ValidationReport:
    shape = validate_shape(document)
    if not shape.ok:
        # Downstream validators assume shape correctness for dict access;
        # still run them defensively since they guard with isinstance checks,
        # producing a deterministic, larger violation set is acceptable.
        pass
    locators = validate_locators(document)
    lineage = validate_lineage(document)
    events = validate_events(document)
    return shape.merged(locators).merged(lineage).merged(events)
