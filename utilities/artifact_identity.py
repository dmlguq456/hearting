from __future__ import annotations

"""D-4 stable-ID allocation: typed, opaque, >=128-bit identifiers.

Never derive an identity from a path, slug, title, capability, mtime, content
hash, or date -- see docstring on `RootIdentity` and the prefix table below.
`dispatch_contract.canonical_repository_identity()` is a path-derived value and
must never be used as a seed here.
"""

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

ID_BODY_BYTES = 16  # 128-bit

ID_KINDS: Dict[str, str] = {
    "repository": "repo_",
    "artifact_root": "root_",
    "campaign": "camp_",
    "cycle": "cyc_",
    "artifact": "art_",
    "artifact_revision": "arev_",
    "shared_reference": "ref_",
    "shared_reference_revision": "rrev_",
    "manifest": "man_",
    "manifest_revision": "mrev_",
    "event": "evt_",
    "stream": "strm_",
    "producer": "prod_",
    "evidence": "evd_",
}

_PREFIX_TO_KIND: Dict[str, str] = {prefix: kind for kind, prefix in ID_KINDS.items()}

_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class IdentityError(Exception):
    pass


def prefix_for(kind: str) -> str:
    try:
        return ID_KINDS[kind]
    except KeyError:
        raise IdentityError("unknown id kind: {0!r}".format(kind))


def kind_of(value: str) -> Optional[str]:
    if not isinstance(value, str):
        return None
    for prefix, kind in _PREFIX_TO_KIND.items():
        if value.startswith(prefix):
            rest = value[len(prefix):]
            if _ID_RE.match(rest):
                return kind
    return None


def is_well_formed(value: str, kind: Optional[str] = None) -> bool:
    if not isinstance(value, str):
        return False
    if kind is not None:
        prefix = prefix_for(kind)
        if not value.startswith(prefix):
            return False
        return bool(_ID_RE.match(value[len(prefix):]))
    return kind_of(value) is not None


class IdAllocator:
    def __init__(self, entropy: Callable[[int], bytes] = os.urandom) -> None:
        self._entropy = entropy

    def allocate(self, kind: str) -> str:
        prefix = prefix_for(kind)
        body = self._entropy(ID_BODY_BYTES)
        if len(body) != ID_BODY_BYTES:
            raise IdentityError(
                "entropy source returned {0} bytes, expected {1}".format(
                    len(body), ID_BODY_BYTES
                )
            )
        return prefix + body.hex()

    def allocate_many(self, kind: str, count: int) -> List[str]:
        return [self.allocate(kind) for _ in range(count)]


class FixedEntropy:
    """Deterministic entropy source for tests: cycles through a fixed seed."""

    def __init__(self, seed: bytes) -> None:
        if not seed:
            raise IdentityError("FixedEntropy seed must be non-empty")
        self._seed = seed
        self._offset = 0

    def __call__(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            out.append(self._seed[self._offset % len(self._seed)])
            self._offset += 1
        return bytes(out[:size])


def migration_namespace(namespace: str, key: str) -> str:
    """Interface seat for rollout step 6 (deterministic migration IDs).

    Not implemented in rollout step 1.
    """
    raise NotImplementedError("migration ID determinism is rollout step 6")


_ROOT_IDENTITY_KEYS = frozenset(
    {
        "schema_version",
        "artifact_root_id",
        "repository_id",
        "issued_at",
        "producer_contract_version",
    }
)


@dataclass(frozen=True)
class RootIdentity:
    schema_version: int
    artifact_root_id: str
    repository_id: str
    issued_at: str  # RFC3339 UTC, audit only -- never used as an id seed
    producer_contract_version: str

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> "RootIdentity":
        if not isinstance(payload, dict):
            raise IdentityError("root identity payload must be an object")
        extra = set(payload.keys()) - _ROOT_IDENTITY_KEYS
        if extra:
            raise IdentityError(
                "unknown root identity key(s): {0}".format(sorted(extra))
            )
        missing = _ROOT_IDENTITY_KEYS - set(payload.keys())
        if missing:
            raise IdentityError(
                "missing root identity key(s): {0}".format(sorted(missing))
            )
        if not is_well_formed(payload["artifact_root_id"], "artifact_root"):
            raise IdentityError("malformed artifact_root_id")
        if not is_well_formed(payload["repository_id"], "repository"):
            raise IdentityError("malformed repository_id")
        return cls(
            schema_version=payload["schema_version"],
            artifact_root_id=payload["artifact_root_id"],
            repository_id=payload["repository_id"],
            issued_at=payload["issued_at"],
            producer_contract_version=payload["producer_contract_version"],
        )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_root_id": self.artifact_root_id,
            "repository_id": self.repository_id,
            "issued_at": self.issued_at,
            "producer_contract_version": self.producer_contract_version,
        }
