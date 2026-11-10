#!/usr/bin/env python3
"""The launch receipt states the parent's next action (one model-visible contract).

Completion delivery has several runtime realizations -- an interactive Claude
parent's `asyncRewake` carrier, a native Codex per-thread queue, a
registered owner's session supervisor, the legacy Codex Stop hook, and the
disclosed `poll-fallback`. Which one applies is a *runtime* selection made from
the parent runtime (`resolve_parent_completion_delivery` in each adapter
wrapper), and the parent reading the receipt cannot change it.

So the parent does not need to carry that taxonomy in its own instructions. The
receipt answers the only question the parent actually has -- "what do I do
now?" -- with two typed values:

    parent_next=end-turn        a runtime carrier owns this attempt; yield.
    parent_next=bounded-wait    no carrier owns it; run `parent_next_command`.

`parent_next_reason` names which realization decided it (diagnostics, not a
branch the parent takes), and `parent_next_command` carries the exact command
for the `bounded-wait` case only -- an `end-turn` receipt deliberately hands the
parent no wait command, because a model-owned wait there is exactly what the
carrier contract forbids.

Fail-closed rule: an unrecognized delivery value yields `bounded-wait`, never
`end-turn`. Ending the turn on an unproven carrier loses the completion; a
bounded wait only costs time.
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

NEXT_END_TURN = "end-turn"
NEXT_BOUNDED_WAIT = "bounded-wait"

NEXT_VALUES = frozenset({NEXT_END_TURN, NEXT_BOUNDED_WAIT})

# Every value the three adapter wrappers' `resolve_parent_completion_delivery`
# returns, plus the two legacy recipient kinds older rows still carry.
CARRIER_DELIVERIES = {
    "codex-native-queue": "carrier-codex-native-queue",
    "claude-parent-runtime": "carrier-claude-async-rewake",
    "codex-managed-gateway": "carrier-codex-managed-gateway",
    "codex-stop-hook": "carrier-codex-stop-hook",
    "opencode-turn": "carrier-opencode-turn",
    "parent-runtime-supervised": "carrier-session-supervisor",
}
WAIT_DELIVERIES = {
    "poll-fallback": "explicit-poll-fallback",
}

# The disclosed finite fallback (`core/DESIGN_PRINCIPLES.md`, OPERATIONS §5.10).
# Bounded on purpose: an unbounded wait is not a fallback, it is a hang.
WAIT_MAX_SECONDS = 600

# Margin so a join outlives the watch deadline it is joining (review round 5).
JOIN_SLACK_MS = 5_000

# The one steward wake mechanism that actually carries a watch to a wake. Every
# other value (`none`, or `auto` resolved with no Claude session) leaves the
# receipt on disk with nothing to deliver it -- ending the turn there loses it.
STEWARD_CARRIER_WAKE = "hook"
# A Codex steward's watch: the observer queues its receipt as an exact notice to
# the steward thread, and the steward's own App Server queue (the one that wakes
# an idle Codex for completions and peer reports) carries it into a turn. Chosen
# by `watch --wake auto` only while that queue socket exists.
STEWARD_QUEUE_WAKE = "queue"
STEWARD_WAKES = {STEWARD_CARRIER_WAKE: "carrier-steward-watch",
                 STEWARD_QUEUE_WAKE: "carrier-steward-watch-codex-queue"}


def command_home(agent_home) -> str:
    """The harness root a printed command names, or "" when the root is unknown.

    A printed command outlives the process that printed it: it sits in a
    receipt, reaches a successor session, or runs hours later after a capacity
    wait. A managed release (`<data>/hearting/releases/<v>`, also when reached
    through a runtime projection such as `~/.codex/hearting`) is therefore
    printed as its `<data>/hearting/current` pointer, so the command runs the
    release installed when it runs, not the one that happened to print it. A
    session's own pin (OPERATIONS §5.9a) is its exported `AGENT_HOME` and each
    row's sealed launch home, never a printed path. Any other root -- a
    checkout, a test tree -- is printed as given.
    """
    root = str(agent_home or "").strip().rstrip("/")
    if not root:
        return ""
    releases = os.path.dirname(os.path.realpath(root))
    if os.path.basename(releases) == "releases":
        pointer = os.path.join(os.path.dirname(releases), "current")
        if os.path.isfile(os.path.join(pointer, "core", "CORE.md")):
            return pointer
    return root


def release_moved(agent_home) -> bool:
    """Whether a command printed now would run another release than `agent_home`.

    The same rule as `command_home`, asked by a long-lived helper about itself:
    once the installer's `current` pointer names another release, work that
    starts now runs there, and a helper still running older code hands its work
    over at its own safe point (`peer_obligations.follow_installed_release`,
    which makes the validated decision). Read-only and cheap enough for every
    idle pass; checkouts and other roots never move.
    """
    root = os.path.realpath(str(agent_home or "")) if agent_home else ""
    pointer = command_home(root) if root else ""
    return bool(pointer) and pointer != root and os.path.realpath(pointer) != root


def entrypoint(agent_home, relative: str) -> str:
    """Absolute path to a checked entry point, or "" when the root is unknown.

    A bare `dispatch-wait` is not on any PATH here; printing one would hand the
    parent a command that fails with command-not-found exactly when it is the
    only thing standing between the work and a lost completion.
    """
    root = command_home(agent_home)
    return f"{root}/{relative}" if root else ""


def registry_args(jobs) -> list[str]:
    """`--jobs <registry>` unless it is the stable default a bare call uses here and in a new session.

    A bare `start` reads `AGENT_DISPATCH_JOBS` first, so the registry is left
    out only when that variable is unset or names this same registry.
    """
    if not jobs:
        return []
    try:
        from dispatch_contract import stable_state_root
        named = Path(jobs).resolve()
        inherited = os.environ.get("AGENT_DISPATCH_JOBS")
        if (named == (stable_state_root(os.environ) / "jobs.log").resolve()
                and (not inherited or Path(inherited).resolve() == named)):
            return []
    except (ImportError, OSError, ValueError, RuntimeError):
        pass
    return ["--jobs", str(jobs)]


def route_reference(route_file) -> str:
    """A canonical route record (`.runtime/routes/rt-<16 hex>.json`) by its ID, any other by its path.

    `start --route <id>` finds the record again from the route's checkout, the registry or the
    session's route-chain ledger (`capability-route.resolve_route_argument`)."""
    path = Path(route_file)
    if (re.fullmatch(r"rt-[0-9a-f]{16}\.json", path.name) and path.parent.name == "routes"
            and path.parent.parent.name == ".runtime"):
        return path.stem
    return str(route_file)


def resume_command(route_file, jobs=None, *, agent_home) -> str:
    """The command that continues one sealed route; the route is its whole handle."""
    return shlex.join([sys.executable, entrypoint(agent_home, "utilities/capability-route.py"),
                       "start", "--route", route_reference(route_file), *registry_args(jobs)])


def correction_command(attempt_id, jobs=None, *, agent_home) -> str:
    """The command that answers or corrects one existing owner, by its attempt id."""
    return shlex.join([sys.executable, entrypoint(agent_home, "utilities/capability-route.py"),
                       "correct", *registry_args(jobs), "--attempt-id", str(attempt_id)])


def wait_command(attempt_id: str, *, agent_home=None) -> str:
    """The exact bounded wait a `bounded-wait` receipt authorizes."""
    entry = entrypoint(agent_home, "utilities/dispatch-wait.sh")
    if not entry:
        return ""
    return f"{entry} --attempt-id {attempt_id} --max {WAIT_MAX_SECONDS}"


def parent_next(
    delivery: str, attempt_id: str | None, *, agent_home=None
) -> tuple[str, str, str]:
    """Return `(next, reason, command)` for one launch receipt.

    `command` is the empty string whenever the parent must not, or cannot, run
    one -- and an empty command never converts a wait into `end-turn`.
    """
    delivery = (delivery or "").strip()
    if delivery in CARRIER_DELIVERIES:
        return NEXT_END_TURN, CARRIER_DELIVERIES[delivery], ""
    reason = WAIT_DELIVERIES.get(delivery, "delivery-unrecognized")
    if not attempt_id or attempt_id == "-":
        # Nothing to wait on by id: say so rather than print a command that
        # cannot run. The parent still does not end the turn on this branch.
        return NEXT_BOUNDED_WAIT, f"{reason}-attempt-unknown", ""
    command = wait_command(attempt_id, agent_home=agent_home)
    if not command:
        return NEXT_BOUNDED_WAIT, f"{reason}-entrypoint-unknown", ""
    return NEXT_BOUNDED_WAIT, reason, command


def steward_next(
    wake: str | None,
    watch_id: str | None,
    *,
    agent_home=None,
    arms_hook: bool = True,
    timeout_ms: int | None = None,
) -> tuple[str, str, str]:
    """Same contract for a steward watch line.

    Two things must both hold before this line may say `end-turn`:

    * `arms_hook` -- *this line* is one the rewake hook arms from. The hook only
      arms on a `watch` command printing `state=armed`; a line the session
      prints from `rearm` reaches no hook even when the watch behind it is
      perfectly healthy, so it must not claim a carrier.
    * `wake` names a carrier: Claude's rewake hook (`hook`) or a Codex
      steward's native thread queue (`queue`). A watch armed with `wake=none`
      (or `auto` resolved where neither exists) leaves its receipt on disk with
      nothing to deliver it.

    Otherwise this is a `bounded-wait` on the steward's own foreground surface.
    That surface is `join <watch_id>`, not `wait <target>`: join waits on *this
    watch*, reads its receipt, and exits with the receipt's state, while a wait
    on the target only watches herdr's own state, ignores the watch's `until`
    set, and opens a second ledger row. (Neither acks: the next prompt's sweep
    surfaces the receipt once more either way.) Never a dispatch attempt wait,
    which does not take a watch at all. The bound is explicit, because both
    surfaces wait forever without one.
    """
    if arms_hook and (wake or "").strip() in STEWARD_WAKES:
        return NEXT_END_TURN, STEWARD_WAKES[(wake or "").strip()], ""
    if not arms_hook:
        reason = "steward-line-does-not-arm"
    else:
        reason = f"steward-wake-{(wake or 'unset').strip() or 'unset'}"
    entry = entrypoint(agent_home, "utilities/peer-steward.py")
    if not entry or not watch_id or watch_id == "-":
        return NEXT_BOUNDED_WAIT, f"{reason}-entrypoint-unknown", ""
    # A watch's own deadline may be hours (the hook budget is ~6 h). Waiting that
    # long in the foreground is not "bounded": it outlives the caller's own tool
    # limit and would be cut mid-wait, so the printed bound is the smaller of the
    # two. `0` is a real value (expire at once), not a missing one.
    # The watch expires at its own `timeout`, then writes its receipt and only
    # then releases the lock. Joining with exactly that deadline loses the race
    # by milliseconds and reports `join-timeout` for a watch that just finished,
    # so the join outlives it by a small margin -- still capped, because a bound
    # the caller cannot actually run is not a bound.
    bound = (
        min(int(timeout_ms) + JOIN_SLACK_MS, WAIT_MAX_SECONDS * 1000)
        if timeout_ms is not None
        else WAIT_MAX_SECONDS * 1000
    )
    return (
        NEXT_BOUNDED_WAIT,
        reason,
        f"python3 {entry} join {watch_id} --timeout {bound}",
    )


def _lines(triple: tuple[str, str, str]) -> list[str]:
    next_action, reason, command = triple
    return [
        f"parent_next={next_action}",
        f"parent_next_reason={reason}",
        f"parent_next_command={command or '-'}",
    ]


def receipt_lines(
    delivery: str, attempt_id: str | None, *, agent_home=None
) -> list[str]:
    """The three receipt lines, in the order every adapter prints them."""
    return _lines(parent_next(delivery, attempt_id, agent_home=agent_home))


def steward_fields(
    wake: str | None,
    watch_id: str | None,
    *,
    agent_home=None,
    arms_hook: bool = True,
    timeout_ms: int | None = None,
) -> str:
    """The same three fields as one directive line for steward output.

    The steward prints this on its *own* line, after the watch line: the wait
    command contains spaces, and a whitespace-splitting reader of the watch line
    would otherwise take only its first token. `parent_next_command` is last, so
    even such a reader can take it as the rest of the line.
    """
    return " ".join(
        _lines(
            steward_next(
                wake, watch_id, agent_home=agent_home,
                arms_hook=arms_hook, timeout_ms=timeout_ms,
            )
        )
    )
