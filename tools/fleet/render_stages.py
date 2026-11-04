"""Fleet render: stage segments for routes, dispatch jobs and sessions.

Split out of `render.py`, which re-exports every name defined here.
"""
import math
import re
import time
from .display import label as _user_label
from .model import fmt_min
from . import titles
from .session_handle import sanitize_title as _sanitize_session_title
from .session_handle import current_session_subject as _current_session_subject

from .render_cells import (
    _clip_w, _dw, _INTERACTION_LABEL, _NAME_KEY_DIM, _PIPE_STAGES, _stage_role_label,
    _STAGE_ZONE_MAX, _TAG_W,
)


def _plugin_phase(j):
    """F-50e — the openai-codex plugin's own `phase` word, verbatim, or ''.

    The plugin's phase vocabulary is third-party internal ("investigating", "done", …). It is
    NEVER mapped onto a canonical stage, breadcrumb or WorkProjection (F-50a forbids inventing
    meaning for an unknown word); it is shown as-is, dim, in the row's micro-status slot, and
    an absent/blank phase simply shows nothing.
    """
    record = getattr(j, "plugin_job", None)
    phase = record.get("phase") if isinstance(record, dict) else None
    if not isinstance(phase, str) or not phase.strip():
        return ""
    return _clip_w(" ".join(phase.split()), _STAGE_ZONE_MAX)


def _is_plugin_agent(job):
    """F-73 semantic boundary: plugin storage is not dispatch identity."""
    return (getattr(job, "surface_kind", None) == "plugin-agent"
            or getattr(job, "source", None) == "plugin-queue")


_ROUTE_STATE_MARK = {"failed": ("✕", "lvl_r"), "degraded": ("◐", "lvl_y"),
                     "attention": ("!", "lvl_y"),
                     "reconciling": ("…", "lvl_y"), "recovering": ("…", "lvl_y"),
                     "done": ("✓", None)}


def _one_shot_route(route_seq):
    return bool(route_seq and len(route_seq) == 1 and route_seq[0][0] == "one-shot")


def _route_rides_the_rail(j, route_seq, in_card):
    """OWNER and depth-1 FRAME cards share a layout; worker status stays inline."""
    depth = max(1, int(getattr(j, "depth", 1) or 1))
    return bool(in_card and (_is_owner_mode_row(j)
                            or (depth == 1 and getattr(j, "worker_type", None) == "frame")
                            or (depth == 1 and not getattr(j, "worker_type", None))))


def _stage_zone_segs(bc):
    """Stage-zone lead-in (user 2026-07-20: "stages 앞에 콜론 등으로 구분감") — a dim ' : '
    separates the breadcrumb from the options dial. The leading space is load-bearing: a
    dial that overflows _OPTW leaves no pad, and a flush colon would read as the dial's own
    label ('layer2: pre'). Skipped when the breadcrumb already opens with its own
    'label: ' prefix (SD-F1 rows) — a second colon would stutter. An empty
    breadcrumb gets an explicit ``-`` instead of leaving the stages slot blank."""
    if not bc:
        return [(" : ", "dim"), ("-", "dim")]
    lead_text, lead_key = bc[0]
    own_prefix = lead_key == "name_dim" and lead_text.endswith(": ")
    return ([("  ", None)] if own_prefix else [(" : ", "dim")]) + bc


def _stall_attention_stage_segs(job, max_width=None):
    """Show a retained attempt advisory in the existing dispatch stage slot."""
    observation = getattr(job, "stall_observation", None)
    if not isinstance(observation, dict):
        return None
    if getattr(job, "liveness", None) == "stale":
        from . import route
        if route._is_reconciling_attempt(job):
            return None
    label = "! stalled" if observation.get("state") == "stalled" else "! check"
    try:
        age = max(0, int((float(observation["observed_at"])
                          - float(observation["last_progress_at"])) / 60))
        age_label = " " + fmt_min(age)
    except (KeyError, TypeError, ValueError, OverflowError):
        age_label = ""
    if max_width is not None:
        # `_stage_zone_segs` adds the existing ` : ` prefix outside this budget.
        content_width = max(0, int(max_width) - 3)
        if len(label + age_label) <= content_width:
            label += age_label
        elif len(label) <= content_width:
            pass
        elif len("! check") <= content_width:
            label = "! check"
        elif content_width >= 1:
            label = "!"
        else:
            return []
    else:
        label += age_label
    return _stage_zone_segs([(label, "lvl_y")])


def _session_name(s):
    """Keep explicit user names and successful Fleet subjects; leave gaps blank.

    Native titles, derived names, slugs and cwd basenames cannot replace a failed
    title refresh. Tags still identify sessions whose title has never succeeded.
    """
    return _current_session_subject(getattr(s, "runtime_name", None), s.title)


def _display_session_subject(s):
    if getattr(s, "runtime_name", None):
        return _session_name(s)
    return _subject_title(s)


def _text_age(timestamp, now=None):
    now = time.time() if now is None else now
    if (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
            or not math.isfinite(timestamp) or not 0 <= timestamp <= now):
        return "age unknown"
    minutes = int((now - timestamp) // 60)
    if minutes >= 1440:
        return "%dd ago" % (minutes // 1440)
    if minutes >= 60:
        return "%dh ago" % (minutes // 60)
    return "%dm ago" % minutes


def _aged_text(text, timestamp, room=None):
    """Reserve the observation age at the end, even when prose is clipped."""
    return _text_with_age(text, _text_age(timestamp), room)


def _text_with_age(text, age, room=None):
    if room is None:
        return str(text) + " · " + age
    suffix = " · " + age
    text_room = room - _dw(suffix)
    if text_room < 1:
        return age if _dw(age) <= room else ""
    return _clip_w(str(text), text_room) + suffix


def _clip_age_tail(text, room):
    if room is None:
        return text
    body, separator, age = text.rpartition(" · ")
    if separator and (age == "age unknown" or re.fullmatch(r"\d+[mhd] ago", age)):
        return _text_with_age(body, age, room)
    return _clip_w(text, room)


def _subject_name_key(entity, key):
    if (not getattr(entity, "runtime_name", None) and not _is_plugin_agent(entity)
            and titles.previous_title(entity, getattr(entity, "title_ts", None))):
        return _NAME_KEY_DIM.get(getattr(entity, "harness", None), "nmd_other")
    return key


def _subject_title(entity):
    title = _sanitize_session_title(getattr(entity, "title", None) or "")
    if not title:
        return ""
    if _is_plugin_agent(entity):
        return title
    return title


def _projection_stage_text(entity, max_width=24):
    """Render only the entity's attached projection, never a guessed child route."""
    projection = getattr(entity, "work_projection", None)
    if projection is None:
        return ""
    if getattr(projection, "ambiguity", None):
        return ""
    label = getattr(projection, "stage_label", None)
    if not label:
        return ""
    progress = getattr(projection, "progress", None)
    suffix = ""
    if progress is not None:
        suffix = " %d/%d" % (progress.done, progress.total)
    return _clip_w('stage %s%s' % (_user_label(label), suffix), max_width)


def _spec_phase_seq(entity):
    """``[(display, state), ...]`` for a spec-grounding projection's lit phase breadcrumb, else
    None. The sequence is attached by ``projection._spec_marker_projection`` in ``_route_view``."""
    cap = getattr(entity, "cap_grounding", None) or {}
    if cap.get("capability") != "autopilot-spec":
        return None
    projection = getattr(entity, "work_projection", None)
    if not projection or getattr(projection, "source", None) != "artifact-inferred":
        return None
    backing = getattr(projection, "_route_view", None) or {}
    seq = backing.get("spec_phases")
    if not seq:
        return None
    return [(str(a), str(b)) for a, b in seq]


def _untagged_parallel_groups(nodes):
    """Recover legs whose route record never carried `parallel_group`.

    F-79 (user 2026-08-14 "review가 3-way로 안뜨고 전부 직렬로 뜨는데"). The compiler seals
    the tag correctly — `_expand_parallel_groups` writes it on every leg — but a hand-built
    partial route (a recovery route naming only the nodes left to run) can reach the board
    without it, and the three `impl-review*` legs then drew as three serial stages while all
    three were `active` at once. Fleet was drawing the record faithfully; the record was
    short an attribute.

    Recovery uses the SHAPE the compiler itself produces: legs share one `depends_on` set,
    and their ids are the anchor's id plus a `-<suffix>` (`_expand_parallel_groups`:
    `leg["id"] = f"{base['id']}-{suffix}"`). Measured against every tagged group on the live
    board, this reproduces the tag exactly and finds no group the record disagrees with — so
    it is a read of the compiler's own convention, not a guess about what looks parallel.
    The tag stays authoritative: this only fills in where there is none.
    """
    by_deps = {}
    for node in nodes:
        if node.get("parallel_group") or node.get("replica_group"):
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            continue
        by_deps.setdefault(tuple(node.get("depends_on") or ()), []).append(node)
    recovered = {}
    for siblings in by_deps.values():
        if len(siblings) < 2:
            continue
        ids = [n["id"] for n in siblings]
        # The anchor keeps the bare group id; every other leg is `<anchor>-<suffix>`.
        anchors = [candidate for candidate in ids
                   if all(other == candidate or other.startswith(candidate + "-")
                          for other in ids)]
        if len(anchors) != 1:
            continue
        recovered[anchors[0]] = siblings
    return recovered


def _collapse_parallel_nodes(nodes):
    """Fold parallel legs into one ``<group>(N-way)`` node.

    ``parallel_group`` is canonical; ``replica_group`` remains a read-only
    one-window alias for prior route records.

    The individual legs already render as their own dispatch rows under the
    conductor (user 2026-07-24: "병렬 leg를 굳이 표현을 안해도 되잖아"), so every
    route-projection surface names the group once. State follows the F-41d precedence
    (failed > attention > active > degraded > recovery > pending); downstream
    ``depends_on`` references to a member are rewritten to the merged id. In particular,
    a completed leg plus a pending leg remains pending. Completion requires
    every member to be done; neither completion nor activity is inferred."""
    nodes = list(nodes or ())
    groups = {}
    for node in nodes:
        group = node.get("parallel_group") or node.get("replica_group")
        if isinstance(group, str) and group:
            groups.setdefault(group, []).append(node)
    groups = {gid: members for gid, members in groups.items() if len(members) > 1}
    for gid, members in _untagged_parallel_groups(nodes).items():
        groups.setdefault(gid, members)
    if not groups:
        return nodes
    merged_by_group, merged_id_of = {}, {}
    # Membership is resolved by node ID, not by re-reading the tag: a group recovered by
    # `_untagged_parallel_groups` has no tag on its members, and reading the field again
    # here would silently drop it back into serial nodes.
    group_of_id = {}
    for gid, members in groups.items():
        for member in members:
            if member.get("id"):
                group_of_id[member["id"]] = gid
    for gid, members in groups.items():
        states = [m.get("state") for m in members]
        state = next(
            (candidate for candidate in
             ("failed", "attention", "active", "degraded", "recovering", "reconciling", "pending")
             if candidate in states),
            "done" if all(state == "done" for state in states) else "pending",
        )
        member_ids = {m.get("id") for m in members}
        deps = []
        for m in members:
            for parent in m.get("depends_on") or ():
                if parent not in member_ids and parent not in deps:
                    deps.append(parent)
        elapsed = [m.get("elapsed_min") for m in members
                   if m.get("elapsed_min") is not None]
        rounds = [m.get("attempt_round") for m in members
                  if isinstance(m.get("attempt_round"), int)
                  and not isinstance(m.get("attempt_round"), bool)]
        route_rounds = [m.get("route_attempt_round") for m in members
                        if isinstance(m.get("route_attempt_round"), int)
                        and not isinstance(m.get("route_attempt_round"), bool)]
        revision = next((m.get("revision_of") for m in members
                         if m.get("id") == gid and m.get("revision_of") is not None), None)
        if revision is None:
            revision = next((m.get("revision_of") for m in members
                             if m.get("revision_of") is not None), None)
        merged = dict(members[0])
        # ``gate_passed`` is the single completion carrier for registered,
        # native, and inline attempts alike.  It is resolved from the exact
        # schema-2 completion marker before this pure projection step; a note or
        # generic ``done`` state must never manufacture a gate pass.
        member_gates = [bool(m.get("gate_passed")) for m in members]
        merged.update({
            "id": "%s(%d-way)" % (gid, len(members)), "state": state,
            "depends_on": deps,
            "level": min(m.get("level") or 0 for m in members),
            # Legs may sit on different harnesses/models by design — no single
            # unit/model represents the group.
            "unit": None, "unit_choices": [], "model": None, "effort": None,
            "elapsed_min": max(elapsed) if elapsed else None,
            "gate_passed": True if all(member_gates) else None,
            "attempt_round": max(rounds) if rounds else None,
            "route_attempt_round": max(route_rounds) if route_rounds else None,
            "revision_of": revision,
            "round_node_id": gid,
            "parallel_width": len(members),
        })
        merged_by_group[gid] = merged
        for m in members:
            if m.get("id"):
                merged_id_of[m["id"]] = merged["id"]
    out, emitted = [], set()
    for node in nodes:
        group = group_of_id.get(node.get("id"))
        if group in merged_by_group:
            if group not in emitted:
                emitted.add(group)
                out.append(merged_by_group[group])
            continue
        deps = []
        for parent in node.get("depends_on") or ():
            renamed = merged_id_of.get(parent, parent)
            if renamed not in deps:
                deps.append(renamed)
        if deps != list(node.get("depends_on") or ()):
            node = dict(node)
            node["depends_on"] = deps
        out.append(node)
    return out


# One-window callable compatibility for existing Fleet extensions/tests.
_collapse_replica_nodes = _collapse_parallel_nodes


def _projection_route_seq(entity):
    """Return assigned work in sealed order; the route overview is separate."""
    projection = getattr(entity, "work_projection", None)
    if not projection or getattr(projection, "source", None) != "route-exact":
        return None
    backing = getattr(projection, "_route_view", None) or {}
    view = backing.get("view") or {}
    from . import route
    nodes = view.get("nodes") or ()
    scope = getattr(projection, "scope_node_ids", None)
    if scope is not None:
        nodes = [node for node in nodes if node.get("id") in scope]
    return [(route.node_display_label(node), node.get("state"))
            for node in _collapse_parallel_nodes(nodes)]


_LEGACY_STAGE_COLOR_INDEX = {
    "code-plan": 0,
    "code-execute": 1,
    "code-test": 2,
    "code-report": 3,
    "stage-search": 0,
    "stage-analyze": 1,
    "stage-report": 2,
}


def _dispatch_stage_color_index(entity, key=None, stage=None):
    """F-42b — color a dispatch-depth-2 micro-status with its own stage.

    A validated route owns the index, including a collapsed parallel group.  Legacy rows fall
    back to their stage contract/key; zero is only the final no-evidence fallback.  The helper
    returns an index rather than a color key so blink only changes brightness, never hue.
    """
    projection = getattr(entity, "work_projection", None)
    if projection and getattr(projection, "source", None) == "route-exact":
        backing = getattr(projection, "_route_view", None) or {}
        view = backing.get("view") or {}
        raw_nodes = list(view.get("nodes") or ())
        selected_id = (getattr(projection, "route_node", None)
                       or getattr(entity, "route_node", None))
        selected = next((node for node in raw_nodes if node.get("id") == selected_id), None)
        selected_group = ((selected or {}).get("parallel_group")
                          or (selected or {}).get("replica_group"))
        for i, node in enumerate(_collapse_parallel_nodes(raw_nodes)):
            node_group = node.get("parallel_group") or node.get("replica_group")
            if node.get("id") == selected_id or (selected_group and node_group == selected_group):
                return i % 5

    identities = (
        getattr(entity, "assigned_contract", None),
        key,
        getattr(entity, "worker_role", None),
    )
    for identity in identities:
        base = str(identity or "").partition(":")[0]
        if base in _LEGACY_STAGE_COLOR_INDEX:
            return _LEGACY_STAGE_COLOR_INDEX[base] % 5

    label = _dispatch_stage_label(entity) or stage
    for seq in _PIPE_STAGES.values():
        if label in seq:
            return seq.index(label) % 5
    return 0


def _projection_stage_for_dispatch(entity):
    """Select a row stage from its projection with a bounded legacy fallback."""
    projection = getattr(entity, "work_projection", None)
    if projection is None:
        return getattr(entity, "stage", None) or ""
    if getattr(projection, "ambiguity", None) or getattr(projection, "source", None) == "registry-exact":
        return ""
    return getattr(projection, "stage_label", None) or (
        getattr(entity, "stage", None) if not getattr(projection, "route_id", None) else ""
    ) or ""


def _unused_badge(s, compact=False):
    """`unused <age>` — F-26's first-class signal (prd.md:248). Says the session was started
    and never prompted, and for how long it has sat that way. Age comes from the process, not
    the registry clock (the registry's updatedAt froze at startup — that IS the finding).

    compact drops the age. Two of F-26's requirements collide on a tight row: the badge is
    specified as `unused <경과>` (prd.md:248) but prd.md:247 also demands no anonymous rows,
    and the badge is what starves the name. The age is the recoverable half — every layout
    already carries elapsed time in its own time cell — so it yields first, and only when the
    name would otherwise be clipped."""
    if compact:
        return ' unused'
    return ' unused %s' % fmt_min(s.elapsed_min if s.elapsed_min is not None else None)


def _interaction_badge(s):
    state = getattr(s, "interaction_state", None)
    kinds = [state.get("kind")] if isinstance(state, dict) else []
    kinds.extend(row.get("kind") for row in (getattr(s, "interaction_observations", None) or [])
                 if isinstance(row, dict))
    labels = list(dict.fromkeys(_INTERACTION_LABEL[kind] for kind in kinds if kind in _INTERACTION_LABEL))
    return " " + " + ".join(labels) if labels else ""


def _interaction_chip(s):
    """F-60: the blocked session's interaction badge, drawn as a REVERSE-VIDEO chip.

    Returns `(segs, width)` — `([], 0)` when the session has no interaction kind to name.

    The chip is where F-60's emphasis lives. The separator space stays OUTSIDE the reversed
    run and the label is padded inside it, so the reversed cells form one solid block instead
    of a run of inverted letters butting against the title. The glyph and the context lead
    word deliberately do NOT get this treatment: they carry the same `g_blocked` red, and a
    second reversed cell on the row would read as two competing chips rather than one.
    """
    label = _interaction_badge(s).strip()
    if not label:
        return [], 0
    chip = " %s " % label
    return [(" ", None), (chip, "g_blocked_chip")], 1 + _dw(chip)


def _session_tag_chip(s, dim=False):
    """F-100a — the 2-hex session tag (`hearting-46` → `46`) as an outline badge in
    `_TAG_W` cells, then the steward flag when the session supervises others: dim `[` `]`
    around the tag in soft white (user 2026-09-03: the reverse chip read too heavy; white,
    not the harness hue, so it separates from the harness text).
    A session without a tag draws the same cells blank — the badge is a slot, not a suffix,
    so every harness label starts in the same column. This is what replaced the F-99c
    ` · hearting-46` companion: the title owns the whole name zone again and the identity
    token has its own fixed home."""
    return _session_tag_slot(s, dim) + _steward_flag(s, dim)


def _steward_flag(s, dim=False):
    """`[aa] ⚑ claude` — the flag sits between the tag and the harness word (user
    2026-10-14: on the row under the ID it hid the supervisor line whenever the first
    watched session was only one row away). Callers size the harness cell by the chip's
    real width, so the name column stays where it was."""
    if not getattr(s, "steward", False):
        return []
    return [(_ICON_STEWARD, "tag_dim" if dim else "tag_steward"), (" ", None)]


def _session_tag_slot(s, dim=False):
    tag = getattr(s, "session_tag", None)
    steward = bool(getattr(s, "steward", False))
    if not isinstance(tag, str) or not tag:
        if getattr(s, "_session_tag_unpaired", False) and not steward:
            # A Codex app-server row whose managed state dir paired with no tagged TUI
            # client (collectors/codex.share_managed_tags): say so instead of a blank slot.
            body = "--"[: _TAG_W - 3].ljust(_TAG_W - 3)
            return [("[", "dim"), (body, "tag_dim"), ("]", "dim"), (" ", None)]
        if not steward:
            return [(" " * _TAG_W, None)]
        # Unknown session ID stays blank; the model cell carries the role.
        return [(" " * _TAG_W, None)]
    body = tag[: _TAG_W - 3].ljust(_TAG_W - 3)
    key = "tag_dim" if dim else ("tag_steward" if steward else "tag")
    return [("[", "dim"), (body, key), ("]", "dim"), (" ", None)]


_DISPATCH_NAME_MAX = 18


def _compact_dispatch_name(name, max_width=_DISPATCH_NAME_MAX):
    return _clip_w(name or "", max_width)


def _is_owner_mode_row(j):
    worker_role = getattr(j, "worker_role", None) or ""
    return bool(
        getattr(j, "worker_type", None) == "owner"
        or getattr(j, "unit", None) == "_kernel/owner"
        or worker_role.endswith("orchestrator")
    )

def _dispatch_stage_label(j):
    """(label_prefix, is_stage_worker) — the dispatch-depth-2 stage-role label ('exec', 'plan'...) that
    now identifies a dispatch-depth-2 child in the NAME zone (F-15a P0-1: identity lives here, not in a
    duplicated breadcrumb). dispatch-depth-1 conductors/orphans have no such label — their identity is
    just their own slug."""
    if _is_owner_mode_row(j) or max(1, int(getattr(j, "depth", 1) or 1)) < 2:
        return None
    route_node = getattr(j, "route_node", None)
    if route_node:
        return _compact_dispatch_name(route_node, 14)
    label, suffix = _stage_role_label(getattr(j, "assigned_contract", None))
    if label is None:
        label, suffix = _stage_role_label(getattr(j, "key", None))
    if label is None:
        # Legacy rows may only carry the old overloaded worker_role.
        label, suffix = _stage_role_label(getattr(j, "worker_role", None))
    if label is None:
        return None
    return label + suffix
_ICON_STEWARD = "⚑"
