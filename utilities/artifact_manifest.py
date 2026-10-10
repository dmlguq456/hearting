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
from typing import Any, Collection, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from artifact_identity import is_well_formed, kind_of

CONTRACT_VERSION = "artifact-cycle-manifest/v2"
SCHEMA_VERSION = 2
MANIFEST_KIND = "artifact.cycle"

_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
# A leading underscore is the harness's own cycle-internal convention
# (`_internal/`, CORE.md §3 C-INT) and is therefore a valid locator component;
# Legacy cycle-relative names retain this grammar. The explicit artifacts/
# payload namespace also admits UTF-8 and dot-prefixed names, spaces and the
# punctuation research outputs commonly carry (`~ + ( ) @ , =`), up to the
# 255-byte NAME_MAX of Linux file systems. Quotes, `$`, backquotes, `;`, `|`,
# `&`, redirections and glob characters stay invalid, as do dot segments and
# control characters.
_LOCATOR_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_PAYLOAD_COMPONENT_RE = re.compile(r"^(?:[A-Za-z0-9_.~+()@,= -]|[^\x00-\x7f])+$")
_PAYLOAD_COMPONENT_MAX_BYTES = 255
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_MAX_LOCATOR_COMPONENTS = 32
_MAX_LOCATOR_LENGTH = 1024
_MAX_PAYLOAD_DEPTH = 16
_MAX_PAYLOAD_BYTES = 64 * 1024

_EVENT_TYPES = frozenset(
    {
        "campaign.satisfied",
        "campaign.reopened",
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
CAMPAIGN_CLOSURE_EVENTS = ("campaign.satisfied", "campaign.reopened")
SATISFIED_ACTOR_KINDS = frozenset({"user", "producer"})


class CampaignClosureFold(NamedTuple):
    state: str
    last_satisfied: Optional[Mapping[str, Any]]
    error: Optional[Tuple[str, str]]


def fold_campaign_closure(events: Sequence[Mapping[str, Any]]) -> CampaignClosureFold:
    """Fold campaign close/reopen transitions in stream order."""
    state = "active"
    last_satisfied = None
    for event in events:
        event_type = event.get("event_type")
        actor = event.get("actor")
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if event_type == "campaign.satisfied":
            if state in {"satisfied", "superseded"}:
                return CampaignClosureFold(state, last_satisfied,
                                           ("campaign-event-transition-invalid", "satisfied-while-satisfied"))
            if not isinstance(actor, Mapping) or actor.get("kind") not in SATISFIED_ACTOR_KINDS:
                return CampaignClosureFold(state, last_satisfied,
                                           ("campaign-event-transition-invalid", "satisfied-actor-kind"))
            state, last_satisfied = "satisfied", event
        elif event_type == "campaign.reopened":
            if state != "satisfied":
                return CampaignClosureFold(state, last_satisfied,
                                           ("campaign-event-transition-invalid", "reopened-while-active"))
            if payload.get("reopens_event_id") != last_satisfied.get("event_id"):
                return CampaignClosureFold(state, last_satisfied,
                                           ("campaign-event-transition-invalid", "reopens-event-id-mismatch"))
            if "supersedes_event_id" in event or "revokes_event_id" in event:
                return CampaignClosureFold(state, last_satisfied,
                                           ("campaign-event-transition-invalid", "reopen-correction-field"))
            if not isinstance(actor, Mapping) or actor.get("kind") != "producer":
                return CampaignClosureFold(state, last_satisfied,
                                           ("campaign-event-transition-invalid", "reopened-actor-kind"))
            state = "active"
        elif event_type == "campaign.superseded":
            if state == "superseded" or not isinstance(actor, Mapping) or actor.get("kind") != "producer":
                return CampaignClosureFold(state, last_satisfied,
                                           ("campaign-event-transition-invalid", "superseded-transition"))
            state = "superseded"
        else:
            return CampaignClosureFold(state, last_satisfied,
                                       ("campaign-event-transition-invalid", "unexpected-event-type"))
    return CampaignClosureFold(state, last_satisfied, None)
_CYCLE_TERMINAL_EVENTS = {
    "completed": "cycle.completed",
    "abandoned": "cycle.abandoned",
    "superseded": "cycle.superseded",
}

# D-6-b display relocation only.  Live payload classification is handled by
# classify_artifact_path below; keep this value stable for W7H compatibility.
_RESERVED_LOCATOR_NAMES = frozenset({"manifest.json"})
_LEGACY_RESERVED_LOCATOR_NAMES = _RESERVED_LOCATOR_NAMES


@dataclass(frozen=True)
class ArtifactPathClassification:
    """Typed result of the shared artifact path classifier."""

    namespace: str
    allowed: bool
    reason: Optional[str] = None


def _locator_error(path_value: Any) -> Optional[Tuple[str, str]]:
    """Return the stable locator error for a relative path, without I/O."""
    if not isinstance(path_value, str):
        return None
    if path_value == "":
        return "locator-empty", "empty locator path"
    if path_value.startswith("/"):
        return "locator-absolute", "absolute path"
    if re.match(r"^[A-Za-z]:", path_value) or path_value.startswith("\\\\"):
        return "locator-absolute", "drive/UNC path"
    if "\\" in path_value:
        return "locator-backslash", "backslash in path"
    if _CONTROL_CHAR_RE.search(path_value):
        return "locator-control-char", "control character in path"
    try:
        path_value.encode("utf-8")
    except UnicodeEncodeError:
        return "locator-invalid-unicode", "path is not valid UTF-8"
    if path_value.endswith("/"):
        return "locator-trailing-slash", "trailing slash"
    if len(path_value) > _MAX_LOCATOR_LENGTH:
        return "locator-too-long", "path too long"
    components = path_value.split("/")
    payload = len(components) > 1 and components[0] == "artifacts"
    if len(components) > _MAX_LOCATOR_COMPONENTS:
        return "locator-too-many-components", "too many components"
    component_re = _PAYLOAD_COMPONENT_RE if payload else _LOCATOR_COMPONENT_RE
    for comp in components:
        if comp == "":
            return "locator-empty-component", "empty component"
        if comp in (".", ".."):
            return "locator-dot-segment", "dot segment"
        if comp.startswith(".") and not payload:
            return "locator-hidden-component", "hidden component"
        if not component_re.fullmatch(comp) or (
                payload and len(comp.encode("utf-8")) > _PAYLOAD_COMPONENT_MAX_BYTES):
            return "locator-invalid-component", "invalid component"
    if components[-1] in _LEGACY_RESERVED_LOCATOR_NAMES and not payload:
        return "locator-reserved-name", "reserved locator name"
    return None


def classify_artifact_path(
    artifact_root: Optional[str],
    campaign_binding: Optional[str],
    cycle_binding: Optional[str],
    namespace: str,
    root_relative_path: str,
    node_kind: str,
    *,
    prospective: bool = False,
) -> ArtifactPathClassification:
    """Classify exact controls and cycle payloads from their bound context.

    Binding paths are root-relative POSIX paths.  No filesystem lookup occurs;
    callers that open a path must separately enforce containment, lstat/no-follow,
    and regular-file checks.  ``root_relative_path`` is also the manifest
    locator for payloads, preserving the existing D-6 grammar and typed errors.
    """
    if namespace not in {"control", "payload", "locator"}:
        return ArtifactPathClassification("invalid", False, "namespace-invalid")
    if namespace == "locator":
        error = _locator_error(root_relative_path)
        return ArtifactPathClassification("payload", error is None,
                                          error[0] if error else None)

    if not isinstance(artifact_root, str) or not artifact_root:
        return ArtifactPathClassification("unclassified", False, "artifact-root-binding-required")
    campaign = campaign_binding.rstrip("/") if campaign_binding else None
    cycle = cycle_binding.rstrip("/") if cycle_binding else None
    if campaign and not campaign.startswith("campaigns/"):
        return ArtifactPathClassification("unclassified", False, "campaign-binding-invalid")
    if cycle and (not campaign or not cycle.startswith(campaign + "/")):
        return ArtifactPathClassification("unclassified", False, "cycle-binding-invalid")

    controls = {".runtime/"}
    if campaign:
        controls.update({campaign + "/campaign.json", campaign + "/campaign.satisfied.json"})
        if root_relative_path.startswith(campaign + "/campaign.events/"):
            tail = root_relative_path[len(campaign + "/campaign.events/"):]
            if re.fullmatch(r"[0-9]{6}\.json", tail):
                controls.add(root_relative_path)
            elif namespace == "control":
                return ArtifactPathClassification("reserved", False, "campaign-event-path-invalid")
    if cycle:
        controls.update({cycle + "/.cycle.json", cycle + "/manifest.json"})
    elif cycle_binding == "" and root_relative_path == "manifest.json":
        # Admission's private staging directory is itself the exact cycle root;
        # its single root manifest is control until the directory is published.
        controls.add("manifest.json")
    if namespace == "control":
        if root_relative_path in controls or root_relative_path.startswith(".runtime/"):
            allowed_kind = node_kind in {"regular", "prospective"} or (prospective and node_kind == "missing")
            return ArtifactPathClassification("control", allowed_kind,
                                              None if allowed_kind else "control-node-not-regular")
        return ArtifactPathClassification("unclassified", False, "control-path-unrecognized")
    if namespace != "payload":
        return ArtifactPathClassification("invalid", False, "namespace-invalid")
    prefix = (cycle + "/artifacts/") if cycle else "artifacts/"
    if not root_relative_path.startswith(prefix):
        return ArtifactPathClassification("unclassified", False, "outside-cycle-artifacts")
    locator = root_relative_path[len(cycle) + 1:] if cycle else root_relative_path
    error = _locator_error(locator)
    if error:
        return ArtifactPathClassification("payload", False, error[0])
    if node_kind not in {"regular", "prospective"} and not (prospective and node_kind == "missing"):
        return ArtifactPathClassification("payload", False, "payload-node-not-regular")
    return ArtifactPathClassification("payload", True, None)


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
    warnings: Tuple[Violation, ...] = ()

    def to_payload(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": [v.to_payload() for v in self.violations],
            "warnings": [v.to_payload() for v in self.warnings],
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
        # An unhashable candidate (list/dict) can never be a member; membership
        # against a frozenset would raise TypeError instead of rejecting, so the
        # closed schema must classify it as a wrong-value violation itself.
        try:
            member = value in allowed
        except TypeError:
            member = False
        if not member:
            violations.append(
                Violation("wrong-value", path, "expected one of {0}".format(sorted(allowed)))
            )

    return validator


# ---------------------------------------------------------------------------
# nested object shapes
# ---------------------------------------------------------------------------


def _v_completion_criterion(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(value, path, {"statement": _v_str}, {}, violations)


def _v_legacy_merged_from(value: Any, path: str, violations: List[Violation]) -> None:
    _check_closed_object(
        value,
        path,
        {
            "campaign_id": _v_typed_id("campaign"),
            "goal": _v_str,
            "key": _v_str,
            "locator": _v_str,
            "title": _v_str,
        },
        {},
        violations,
    )


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
        {
            # Legacy read-only campaign-merge metadata that predates the closure
            # of this schema. It is produced only by the campaign-merge migration
            # and must be accepted so sealed manifests stay immutable while index
            # rebuild/verify can still validate them. Arbitrary extra keys remain
            # rejected.
            "key": _v_str,
            "merged_from": _v_legacy_merged_from,
        },
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


def _v_relocation_provenance(value, path, violations):
    # Campaign supersession is not a manifest close; an all-open stream has
    # no source manifest. Its automatic journal is the actual provenance.
    _check_closed_object(value, path, {
        "source_root": _v_nonempty_str, "operation_id": _v_nonempty_str,
        "algorithm_version": _v_str, "schema_version": _v_int, "source_digest": _v_digest,
    }, {}, violations)


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
            "provenance": (_v_relocation_provenance if isinstance(value, dict)
                           and value.get("event_type") == "campaign.superseded"
                           and isinstance(value.get("payload"), dict)
                           and value["payload"].get("contract") == "artifact-campaign-closure/v2"
                           else _v_provenance),
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


def _v_manifest_relocation(value, path, violations):
    _check_closed_object(value, path, {
        "operation_id": _v_nonempty_str, "source_root": _v_nonempty_str,
        "source_repository_id": _v_typed_id("repository"),
        "source_artifact_root_id": _v_typed_id("artifact_root"),
        "historical_root_ids": _v_array(_v_typed_id("artifact_root")),
    }, {}, violations)


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
    _check_closed_object(document, "$", _TOP_REQUIRED, {"relocation": _v_manifest_relocation}, violations)
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
    result = classify_artifact_path(None, None, None, "locator", path_value, "regular")
    if result.reason:
        detail = _locator_error(path_value)
        violations.append(_locator_violation(result.reason, vpath,
                                             detail[1] if detail else "invalid locator"))


def validate_locator_path(path_value: str) -> ValidationReport:
    """One grammar for producer collection and manifest/reader consumption."""
    violations: List[Violation] = []
    _check_locator_path(path_value, path_value, violations)
    return _report(violations)


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


def validate_lineage(
    document: Mapping[str, Any],
    *,
    resolvable_ids: Optional[Collection[str]] = None,
    first_close: bool = True,
) -> ValidationReport:
    """`resolvable_ids` are IDs declared by this cycle's earlier documents: a
    refreshed document (§45 D-124) keeps its earlier events, which may name an
    artifact or revision that is no longer a current row, and reads those names
    from the preserved copies.  `first_close=False` skips the checks that only
    the first closing document answers to (the required artifact roles)."""
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
        if (row.get("artifact_root_id") is not None and row.get("artifact_root_id") != top_root_id
                and row.get("artifact_root_id") not in (document.get("relocation") or {}).get("historical_root_ids", [])):
            violations.append(
                Violation(
                    "route-root-id-mismatch",
                    "$.routes[{0}].artifact_root_id".format(i),
                    "does not match top-level artifact_root_id",
                )
            )

    all_ids = set(declared_ids(document).keys()) | set(resolvable_ids or ())
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
        if et is not None and (not isinstance(et, str) or et not in _EVENT_TYPES):
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
    closure_events = sorted(
        (row for row in campaign_events if row.get("event_type") in CAMPAIGN_CLOSURE_EVENTS),
        key=lambda row: row.get("stream_sequence", 0),
    )
    closure_fold = fold_campaign_closure(closure_events)
    if closure_fold.error:
        violations.append(Violation(closure_fold.error[0], "$.campaign.state", closure_fold.error[1]))
    if campaign_state == "satisfied" and not closure_fold.error and closure_fold.state != "satisfied":
        violations.append(Violation(
            "campaign-satisfaction-unauthorized", "$.campaign.state",
            "the last campaign closure transition is not campaign.satisfied",
        ))
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
            for role in required_roles if first_close else ():
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
            if (label == "campaign" and seen_terminal[stream_id] == "campaign.satisfied"
                    and row.get("event_type") == "campaign.reopened"):
                del seen_terminal[stream_id]
                continue
            violations.append(Violation(
                "illegal-transition",
                "$.events[?event_id={0!r}]".format(row.get("event_id")),
                "transition out of terminal state",
            ))
        if row.get("event_type") in terminal_types:
            seen_terminal[stream_id] = row.get("event_type")
        elif label == "campaign" and row.get("event_type") == "campaign.reopened":
            seen_terminal.pop(stream_id, None)


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
            if not _authorized_supersession(row, target_row):
                violations.append(
                    Violation(
                        "event-supersession-unauthorized",
                        "$.events[?event_id={0!r}]".format(eid),
                        "only a later user event may {0} a user event".format(kind),
                    )
                )
            if (
                isinstance(target_row, dict)
                and row.get("stream_id") == target_row.get("stream_id")
                and isinstance(row.get("stream_sequence"), int)
                and isinstance(target_row.get("stream_sequence"), int)
                and not isinstance(row.get("stream_sequence"), bool)
                and not isinstance(target_row.get("stream_sequence"), bool)
                and row["stream_sequence"] <= target_row["stream_sequence"]
            ):
                violations.append(
                    Violation(
                        "event-supersession-not-later",
                        "$.events[?event_id={0!r}]".format(eid),
                        "a same-stream {0} must come after its target".format(kind),
                    )
                )

    return _report(violations)


# ---------------------------------------------------------------------------
# fold_events
# ---------------------------------------------------------------------------


def _authorized_supersession(row: Any, target_row: Any) -> bool:
    """D-11 user-correction precedence.

    A user event may only be superseded or revoked by a later authorized user
    event. Every other actor kind (producer, system, curator-proposal-accepted)
    lacks that authority. Non-user targets keep the existing permissive rule.
    """
    if not isinstance(target_row, dict):
        return True  # dangling target is reported separately
    target_actor = target_row.get("actor")
    if not isinstance(target_actor, dict) or target_actor.get("kind") != "user":
        return True
    actor = row.get("actor") if isinstance(row, dict) else None
    return isinstance(actor, dict) and actor.get("kind") == "user"


def fold_events(document: Mapping[str, Any]) -> Tuple[Dict[str, Any], ...]:
    events = [r for r in document.get("events", []) or [] if isinstance(r, dict)]

    events_by_id = {row.get("event_id"): row for row in events}
    superseded_or_revoked = set()
    for row in events:
        for key in ("supersedes_event_id", "revokes_event_id"):
            target = row.get(key)
            if target is None:
                continue
            # Fold defends user-correction precedence even standalone: an
            # unauthorized supersession link never removes the user event.
            if _authorized_supersession(row, events_by_id.get(target)):
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


# ---------------------------------------------------------------------------
# interim (open-cycle) manifest
# ---------------------------------------------------------------------------

# An open cycle's checkpoint publishes this same closed document with one
# difference: `cycle.state` is `open`, a value no sealed manifest may carry.
# It claims no lineage commit and no completion, so it has no cycle.* or
# route.terminal.recorded event; every other rule is the sealed rule.
INTERIM_CYCLE_STATE = "open"


def validate_update(
    document: Any,
    *,
    preserved: Sequence[Mapping[str, Any]] = (),
    previous: Optional[Mapping[str, Any]] = None,
    published: bool = False,
    changeable_cycle_fields: Sequence[str] = (),
) -> ValidationReport:
    """§45 D-124: a document the runtime published after the cycle closed.

    It is the first-close document's own D-6 check with two differences: a
    reference to an artifact or revision that is no longer a current row is
    resolved from `preserved` (the cycle's earlier documents; an ID declared
    nowhere in them stays a violation), and the required artifact roles are not
    asked again, so removing a required or every file leaves a valid document.
    With `previous` the document must also keep that document's events, and
    its routes, exactly as they were: only a revision record (or the terminal
    record of a later route) may follow; `changeable_cycle_fields` names the cycle
    fields a move (`campaign_id`) or a parent change (`parent_cycle_id`) may differ in.  `published=True` reads a document
    that is already out (a rebuild, a proof): a reference no preserved copy
    resolves any more is then accepted, because losing a copy must not turn a
    finished cycle into a broken one -- only publishing a new document asks for
    every reference to resolve."""
    shape = validate_shape(document)
    locators = validate_locators(document)
    earlier: set = set()
    for old in preserved:
        earlier |= set(declared_ids(old).keys())
    lineage = validate_lineage(document, resolvable_ids=earlier, first_close=False)
    events = validate_events(document)
    report = shape.merged(locators).merged(lineage).merged(events)
    if published:
        kept = tuple(v for v in report.violations
                     if v.code not in ("event-target-id-not-declared", "unresolvable-evidence-id"))
        report = ValidationReport(ok=not kept, violations=kept)
    if previous is None or not isinstance(document, dict):
        return report
    violations: List[Violation] = []
    before_cycle = previous.get("cycle") if isinstance(previous.get("cycle"), dict) else {}
    after_cycle = document.get("cycle") if isinstance(document.get("cycle"), dict) else {}
    provisional_completion = before_cycle.get("state") == "active" and after_cycle.get("state") == "completed"
    for key in ("events", "routes"):
        before = [r for r in previous.get(key, []) or []]
        after = [r for r in document.get(key, []) or []]
        if after[: len(before)] != before and not (key == "routes" and provisional_completion):
            violations.append(Violation(
                "update-earlier-%s-changed" % key, "$.%s" % key,
                "a refreshed document keeps every earlier %s exactly" % key))
        elif key == "events":
            allowed = {"artifact.revision.recorded", "route.terminal.recorded"}
            if provisional_completion:
                allowed.add("cycle.completed")
            for row in after[len(before):]:
                if not isinstance(row, dict) or row.get("event_type") not in allowed:
                    violations.append(Violation(
                        "update-event-type-not-allowed", "$.events",
                        "only a revision or the exact provisional-completion terminal events may follow"))
    for key in ("cycle_id", "campaign_id", "state", "parent_cycle_id", "started_on", "input_digest"):
        if key in changeable_cycle_fields and key != "state":
            continue  # a move or a new parent (§45 D-126) says which fields it may change
        if before_cycle.get(key) != after_cycle.get(key):
            if not (key == "state" and provisional_completion):
                violations.append(Violation(
                    "update-cycle-field-changed", "$.cycle.%s" % key, "a refresh does not change the cycle"))
    if provisional_completion:
        before_routes = [row for row in previous.get("routes", []) or [] if isinstance(row, dict)]
        after_routes = [row for row in document.get("routes", []) or [] if isinstance(row, dict)]
        if len(before_routes) != len(after_routes):
            violations.append(Violation("update-completion-route-count-changed", "$.routes",
                                        "provisional completion keeps the exact route set"))
        else:
            changed = []
            for index, (old, new_row) in enumerate(zip(before_routes, after_routes)):
                if old == new_row:
                    continue
                allowed_row = dict(old)
                allowed_row["terminal_marker"] = new_row.get("terminal_marker")
                allowed_row["terminal_evidence_id"] = new_row.get("terminal_evidence_id")
                if (old.get("terminal_marker") != "pending" or old.get("terminal_evidence_id")
                        or not new_row.get("terminal_marker") or not new_row.get("terminal_evidence_id")
                        or allowed_row != new_row):
                    violations.append(Violation("update-completion-route-identity-changed",
                                                "$.routes[%d]" % index,
                                                "only pending terminal evidence on the same route may bind"))
                changed.append(new_row)
            suffix = [row for row in (document.get("events") or [])[len(previous.get("events") or []):]
                      if isinstance(row, dict)]
            cycle_events = [row for row in suffix if row.get("event_type") == "cycle.completed"
                            and row.get("target_id") == after_cycle.get("cycle_id")]
            terminal_events = [row for row in suffix if row.get("event_type") == "route.terminal.recorded"
                               and row.get("target_id") == after_cycle.get("cycle_id")]
            if len(changed) != 1 or len(cycle_events) != 1 or len(terminal_events) != 1:
                violations.append(Violation("update-completion-event-binding-invalid", "$.events",
                                            "one exact route and one pair of terminal events are required"))
            elif changed[0].get("terminal_evidence_id") != terminal_events[0].get("event_id"):
                violations.append(Violation("update-completion-event-binding-invalid", "$.routes",
                                            "the terminal route row must name its appended event"))
    relocation = document.get("relocation") or {}
    moving_root = (relocation.get("source_artifact_root_id") == previous.get("artifact_root_id")
                   and relocation.get("source_repository_id") == previous.get("repository_id")
                   and previous.get("artifact_root_id") in relocation.get("historical_root_ids", []))
    for key in ("manifest_id", "artifact_root_id", "repository_id"):
        if previous.get(key) != document.get(key) and not (key != "manifest_id" and moving_root):
            violations.append(Violation(
                "update-identity-changed", "$.%s" % key, "a refresh keeps the manifest identity"))
    if violations:
        return report.merged(_report(violations))
    return report


def validate_interim(document: Any) -> ValidationReport:
    if not isinstance(document, dict) or not isinstance(document.get("cycle"), dict):
        return validate(document)
    if document["cycle"].get("state") != INTERIM_CYCLE_STATE:
        return _report([Violation("interim-state-invalid", "$.cycle.state",
                                  "interim manifest must declare cycle.state 'open'")])
    violations: List[Violation] = []
    for index, row in enumerate(document.get("events") or []):
        event_type = row.get("event_type") if isinstance(row, dict) else None
        if isinstance(event_type, str) and (event_type.startswith("cycle.")
                                            or event_type == "route.terminal.recorded"):
            violations.append(Violation("interim-terminal-event", "$.events[{0}]".format(index),
                                        "an open cycle records no terminal event"))
    for index, row in enumerate(document.get("routes") or []):
        if isinstance(row, dict) and (row.get("terminal_marker") != "pending"
                                      or row.get("terminal_evidence_id") != ""):
            violations.append(Violation("interim-terminal-event", "$.routes[{0}]".format(index),
                                        "an open cycle's route has no terminal marker"))
    probe = dict(document)
    probe["cycle"] = {**document["cycle"], "state": "active"}
    return _report(violations).merged(validate(probe))
