#!/usr/bin/env python3
"""What each harness runtime can do, as its adapter declares it.

Every adapter ships ``adapters/<harness>/config/harness-capabilities.json``.
The shared layer reads these declarations where it would otherwise branch on a
harness name: an adapter translates execution and declares what its runtime
has, and the decision stays in the shared module that reads the declaration.

``parent_completion`` says how a parent session of that harness learns that a
direct registered dispatch-depth-1 child finished:

``carrier``
    The ``parent_completion_delivery`` its runtime carries to the parent
    without a model turn, or null when the parent has to wait for it itself.
``reason``
    The ``parent_completion_reason`` recorded when that carrier is selected.
``parent_proof``
    What must hold at launch for the carrier to reach the parent:
    ``native-session`` -- the calling session is the registered parent;
    ``runtime-hook`` -- the carrier binds the session itself;
    ``carrier-env`` -- the runtime running the carrier names, in the calling
    command's environment (``CARRIER_ENV``), the carrier and the exact
    session it carries as ``<carrier>:<session>``. A runtime that predates
    its carrier names nothing, so its parent keeps the fallback.
``without_carrier``
    What a direct registered launch does when the carrier cannot reach the
    parent: ``poll`` -- the receipt discloses a bounded wait; ``refuse`` -- the
    launch stops before spawn, unless an operator authorizes the bounded wait.

``session_identity`` says how a session of that harness is identified:

``env``
    The variables, current name first, that carry the native session id the
    runtime exports to the commands it runs (`session_identity` reads them).
``process_proof``
    The native source that proves which session a running process is on:
    ``session-registry`` (Claude ``sessions/<pid>.json``), ``open-rollout``
    (the Codex rollout a process holds), ``tui-selection`` (the OpenCode TUI's
    own selection record). A process without it proves only its harness.
``herdr_session_id``
    How far herdr's reported ``agent_session`` value may be taken as the
    session id: ``verified`` -- the publisher proved it from the process;
    ``claim`` -- a reported value that is not taken as proof.
``sub_session_root``
    Where a command run by a native sub-session (a subagent the session
    spawned) finds the root session it belongs to, so the sub-session's
    dispatches and reports name the session the user talks to:
    ``shared-id`` -- the runtime exports the root's id to sub-sessions too
    (a Claude subagent); ``spawn-edges`` -- the runtime's own spawn record
    (Codex ``thread_spawn_edges`` in its state DB); ``env:<VARIABLE>`` -- the
    adapter's plugin exports the root id in that variable beside the native
    id (OpenCode, walking ``parentID``).

``access`` says how the runtime realizes a portable execution access request
(read-only roots, writable roots, network) and how strongly:

``enforcement``
    ``os-sandbox`` -- an OS sandbox whose launch mode decides the guarantee
    (writes outside the writable roots and network are blocked under
    ``workspace-write``; ``read-only`` blocks every write; ``danger-full-access``
    blocks nothing), it has no host allowlist, and a child sandbox can nest
    inside a parent's; ``tool-permission`` -- the runtime's tool permission
    rules carry the request, the network is not enforced.
``means``
    What the adapter uses for each axis (``read``, ``write``, ``network``): the
    projection table, as the adapters realize it.

A declaration is data, never an approval step: reading it adds no gate.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ("claude", "codex", "opencode")
SCHEMA_VERSION = 1
PARENT_PROOFS = frozenset({"native-session", "runtime-hook", "carrier-env"})
CARRIER_ENV = "AGENT_PARENT_COMPLETION_CARRIER"
WITHOUT_CARRIER = frozenset({"poll", "refuse"})
PARENT_COMPLETION_KEYS = ("carrier", "reason", "parent_proof", "without_carrier")
SESSION_IDENTITY_KEYS = ("env", "process_proof", "herdr_session_id", "sub_session_root")
SUB_SESSION_ROOTS = frozenset({"shared-id", "spawn-edges"})
PROCESS_PROOFS = frozenset({"session-registry", "open-rollout", "tui-selection"})
HERDR_SESSION_IDS = frozenset({"verified", "claim"})
ACCESS_ENFORCEMENT = frozenset({"os-sandbox", "tool-permission"})
ACCESS_AXES = ("read", "write", "network")
# A parent named by no adapter has no runtime that could carry its completion.
NO_PARENT_CARRIER = {"carrier": None, "reason": None, "parent_proof": None,
                     "without_carrier": "poll"}


class HarnessCapabilityError(ValueError):
    pass


def declaration_path(harness: str, root: Path = ROOT) -> Path:
    return root / "adapters" / harness / "config" / "harness-capabilities.json"


def _validate(harness: str, value: object) -> dict:
    def refuse(detail: str):
        raise HarnessCapabilityError(f"harness-capabilities-invalid:{harness}:{detail}")

    if not isinstance(value, dict):
        refuse("not-an-object")
    if value.get("schema_version") != SCHEMA_VERSION:
        refuse("schema_version")
    if value.get("harness") != harness:
        refuse("harness")
    completion = value.get("parent_completion")
    if not isinstance(completion, dict) or sorted(completion) != sorted(PARENT_COMPLETION_KEYS):
        refuse("parent_completion")
    if completion["without_carrier"] not in WITHOUT_CARRIER:
        refuse("parent_completion.without_carrier")
    if completion["carrier"] is None:
        if completion["reason"] is not None or completion["parent_proof"] is not None:
            refuse("parent_completion.carrier")
    elif not (isinstance(completion["carrier"], str) and completion["carrier"]
              and isinstance(completion["reason"], str) and completion["reason"]
              and completion["parent_proof"] in PARENT_PROOFS):
        refuse("parent_completion.carrier")
    identity = value.get("session_identity")
    if not isinstance(identity, dict) or sorted(identity) != sorted(SESSION_IDENTITY_KEYS):
        refuse("session_identity")
    names = identity["env"]
    if (not isinstance(names, list) or not names
            or not all(isinstance(name, str) and name.isidentifier() for name in names)):
        refuse("session_identity.env")
    if identity["process_proof"] not in PROCESS_PROOFS:
        refuse("session_identity.process_proof")
    if identity["herdr_session_id"] not in HERDR_SESSION_IDS:
        refuse("session_identity.herdr_session_id")
    root = identity["sub_session_root"]
    if not (root in SUB_SESSION_ROOTS
            or (isinstance(root, str) and root.startswith("env:") and root[4:].isidentifier())):
        refuse("session_identity.sub_session_root")
    access = value.get("access")
    if (not isinstance(access, dict) or sorted(access) != ["enforcement", "means"]
            or access["enforcement"] not in ACCESS_ENFORCEMENT
            or not isinstance(access["means"], dict) or sorted(access["means"]) != sorted(ACCESS_AXES)
            or not all(isinstance(text, str) and text for text in access["means"].values())):
        refuse("access")
    return value


@functools.lru_cache(maxsize=None)
def capabilities(harness: str, root: Path = ROOT) -> dict:
    """The validated declaration of one harness; an unreadable one is an error."""
    if harness not in HARNESSES:
        raise HarnessCapabilityError(f"harness-unknown:{harness}")
    path = declaration_path(harness, root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessCapabilityError(f"harness-capabilities-unreadable:{harness}") from exc
    return _validate(harness, value)


def parent_completion(harness: str) -> dict:
    """How a parent of ``harness`` receives completion (see the module doc)."""
    if harness not in HARNESSES:
        return dict(NO_PARENT_CARRIER)
    return dict(capabilities(harness)["parent_completion"])


def declared_carriers() -> frozenset[str]:
    """Every parent completion carrier some adapter declares."""
    return frozenset(
        carrier for harness in HARNESSES
        if (carrier := capabilities(harness)["parent_completion"]["carrier"])
    )


def session_identity(harness: str) -> dict:
    """How a session of ``harness`` is identified (see the module doc)."""
    return dict(capabilities(harness)["session_identity"])


def _readable_identities() -> dict[str, dict]:
    """The session-identity declaration of every harness whose adapter is readable.

    Identity is read on every launch, label and probe, so one unreadable or invalid
    declaration (a packaged tree without that adapter) leaves only that harness
    unknown instead of failing everything that asks who a session is.
    """
    found = {}
    for harness in HARNESSES:
        try:
            found[harness] = capabilities(harness)["session_identity"]
        except HarnessCapabilityError:
            continue
    return found


def session_env() -> dict[str, tuple[str, ...]]:
    """``{harness: (variable, ...)}`` -- the session id variables the readable adapters declare."""
    return {harness: tuple(declared["env"]) for harness, declared in _readable_identities().items()}


def sub_session_roots() -> dict[str, str]:
    """``{harness: sub_session_root}`` for every readable adapter (see the module doc)."""
    return {harness: declared["sub_session_root"]
            for harness, declared in _readable_identities().items()}


def herdr_verified_harnesses() -> frozenset[str]:
    """Harnesses whose herdr ``agent_session`` value is a proven session id."""
    return frozenset(harness for harness, declared in _readable_identities().items()
                     if declared["herdr_session_id"] == "verified")


def access(harness: str) -> dict:
    """How ``harness`` realizes an execution access request (see the module doc)."""
    return json.loads(json.dumps(capabilities(harness)["access"]))
