"""Fleet render: explorer remote-host observation and question bindings.

Split out of `render.py`, which re-exports every name defined here.
"""
import math
import re
import time
from collections.abc import Mapping

from .render_cells import (
    curses,
)

from .render_rows import (
    _project_for_display,
)

from .render_gpu import (
    _ExplorerView,
)


def _explorer_remote_host_observation(value):
    """Translate one completed compute-host pump result into readiness evidence."""
    if not isinstance(value, Mapping):
        return {"state": "failed", "attempted": True, "reason": "invalid-host-observation",
                "observed_at": time.time()}
    stamp = value.get("observed_at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
        stamp = time.time()
    if value.get("error"):
        return {"state": "failed", "attempted": True,
                "reason": "remote-host-probe-failed", "observed_at": stamp}
    if value.get("configured") is False:
        return {"state": "absent", "attempted": True,
                "reason": "remote-hosts-not-configured", "observed_at": stamp}
    hosts = value.get("hosts")
    if not isinstance(hosts, (list, tuple)):
        return {"state": "failed", "attempted": True,
                "reason": "invalid-host-list", "observed_at": stamp}
    if any(not isinstance(host, Mapping) for host in hosts):
        return {"state": "failed", "attempted": True,
                "reason": "invalid-host-row", "observed_at": stamp}
    remote = [host for host in hosts if isinstance(host, Mapping) and host.get("self") is not True]
    if not remote:
        return {"state": "absent", "attempted": True,
                "reason": "no-remote-hosts-configured", "observed_at": stamp}
    if all(host.get("reachable") is True for host in remote):
        return {"state": "success", "attempted": True, "observed_at": stamp}
    return {"state": "failed", "attempted": True,
            "reason": "remote-host-unreachable", "observed_at": stamp}


def _explorer_attach_remote_observation(snapshot, observation):
    if snapshot is None or observation is None:
        return snapshot
    from dataclasses import replace as _replace_snapshot
    observations = dict(getattr(snapshot, "observations", None) or {})
    observations["remote-host"] = dict(observation)
    return _replace_snapshot(snapshot, observations=observations)


def _explorer_output_reader():
    """The raw-output reader of the live view: the shared viewer, with the registry home as its fallback."""
    from .explorer.output import OutputReader
    try:
        from .collectors import dispatch
        return OutputReader(fallback_home=dispatch._registry_home())
    except Exception:
        return OutputReader()


_EXPLORER_INTERVALS = {"session": None, "seats": 20.0, "models": 60.0, "panes": 5.0}


def _explorer_lite_view(snapshot):
    """The snapshot with the explorer's pure project resolver and nothing else added (no host work)."""
    display = getattr(snapshot, "display_snapshot", None)
    if display is None or getattr(snapshot, "project_of", None) is not None:
        return snapshot
    return _ExplorerView(snapshot, project_of=lambda cwd: _project_for_display(cwd, display))


def _explorer_state_for(state, cols):
    from .explorer.state import NavState
    return NavState.from_value(state)


def _explorer_gesture_binding(model, tag):
    ordering = getattr(model, "ordering", None) or {}
    records = getattr(model, "source_records", None) or {}
    ref = next((ref for ref, value in records.get("_detail_tags", {}).items() if value == tag), None)
    binding = records.get("_detail_bindings", {}).get(ref)
    # Detail bindings also include child routes and attempts. Those change while
    # the same process is being painted and must not cancel a pending click.
    # Keep the native identity and process incarnation in the gesture fence.
    incarnation = tuple(binding[:4]) if isinstance(binding, (tuple, list)) and len(binding) >= 4 else None
    return ordering.get("tag_keys", {}).get(tag), incarnation


def _explorer_key_name(ch, searching=False):
    """The reducer key a curses code stands for; ``None`` when the explorer does not use it.
    While the ``/`` search is open, printable characters are text, never commands."""
    named = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left",
             curses.KEY_RIGHT: "right", curses.KEY_PPAGE: "pageup", curses.KEY_NPAGE: "pagedown",
             curses.KEY_HOME: "home", curses.KEY_END: "end", curses.KEY_ENTER: "enter", 10: "enter",
             13: "enter", 9: "tab", 29: "\x1d", _ESC: "escape",
             curses.KEY_BACKSPACE: "backspace", 127: "backspace", 8: "backspace"}
    if hasattr(curses, "KEY_BTAB"):
        named[curses.KEY_BTAB] = "btab"
    if ch in named:
        return named[ch]
    if searching:
        return "text:" + chr(ch) if isinstance(ch, int) and 32 <= ch < 127 else None
    plain = {ord("/"): "/", ord("e"): "e", ord("E"): "e", ord("F"): "F", ord("f"): "F",
             ord("u"): "u",
             ord("k"): "up", ord("j"): "down"}
    return plain.get(ch)


def _explorer_row_y(frame, entry):
    """Screen row of a target's navigation row, or None when it is folded away or scrolled out."""
    nav_rect = frame.region("nav")
    if nav_rect is None or not nav_rect.w:
        return None
    index = next((i for i, row in enumerate(frame.nav_rows) if row.identity == entry.get("nav_id")), None)
    if index is None:
        return None
    slot = index - frame.nav_offset
    if 0 <= slot < max(0, nav_rect.h - 3):
        return nav_rect.y + 2 + slot
    return None


def _explorer_question_status(value, fallback):
    if (isinstance(value, Mapping) and isinstance(value.get("native_ref"), Mapping)
            and value["native_ref"].get("transport") == "hearting-message-answer"):
        result = value.get("result") or {}
        if result.get("status") == "answered-elsewhere":
            if result.get("transport") == "not-sent":
                return "Answered in the original session; queued answer not sent."
            return "Answered in the original session; delivery received."
        return {"submitted": "Queued; waiting for the original session to receive it.",
                "received": "Received by the original session.",
                "applied": "Answer accepted.",
                "unknown": "Delivery is unconfirmed; your draft is kept."}.get(value.get("state"), fallback)
    reason = value.get("reason") if isinstance(value, Mapping) else None
    if isinstance(reason, str) and reason.strip():
        return " ".join(reason.split())[:220]
    return fallback


def _explorer_same_question_request(left, right):
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    for key in ("id", "harness", "session_id", "request_id", "turn_id", "route_id",
                "question_id", "gate_epoch"):
        a, b = left.get(key), right.get(key)
        if a != b or (key == "request_id" and type(a) is not type(b)):
            return False
    return left.get("native_ref") == right.get("native_ref")


def _explorer_question_has_workflow_binding(ref, envelope):
    """Workflow requests have their own exact identity, rather than a native turn."""
    native = envelope.get("native_ref")
    selected = envelope.get("selected_id")
    if ("turn_id" not in envelope or envelope["turn_id"] is not None
            or not isinstance(native, Mapping) or not isinstance(selected, str)
            or selected != ref.get("id") or envelope.get("request_id") != selected):
        return False
    transport = native.get("transport")
    if transport == "hearting-message-answer":
        return (envelope.get("source") == "S4" and selected.startswith("S4:")
                and native.get("signal_id") == selected
                and isinstance(native.get("seat_key"), str) and bool(native["seat_key"])
                and type(native.get("sequence")) is int and native["sequence"] >= 0
                and envelope.get("question_id") == "message-response")
    if transport == "hearting-answer-marker":
        return (selected.startswith("S4:") and native.get("signal_id") == selected
                and bool(native.get("seat_key"))
                and envelope.get("question_id") == "marker-response")
    if (native.get("source_id") != selected or not native.get("jobs_path")
            or native.get("route_id") != envelope.get("route_id")
            or not envelope.get("route_id")
            or not isinstance(native.get("route_hash"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", native["route_hash"])
            or not native.get("attempt_id")):
        return False
    if transport == "hearting-owner-input":
        return (selected.startswith("S3:")
                and native["attempt_id"] == envelope.get("item_id")
                and isinstance(native.get("owner_binding"), Mapping)
                and native["owner_binding"].get("attempt_id") == native["attempt_id"]
                and envelope.get("question_id") == "owner-correction")
    if transport == "hearting-human-gate":
        raised = native.get("raised_at")
        return (selected.startswith("S2:")
                and envelope.get("source") == "S2"
                and native.get("gate_id") == envelope.get("question_id")
                and bool(native.get("gate_id"))
                and type(native.get("gate_epoch")) is int and native["gate_epoch"] > 0
                and envelope.get("gate_epoch") == native.get("gate_epoch")
                and type(raised) in (int, float) and math.isfinite(raised))
    return False


def _explorer_opencode_form_binding(ref, envelope):
    """Accept a turnless native form only with its exact OpenCode HTTP binding."""
    row_ref = ref.get("_fleet_row_ref") if isinstance(ref, Mapping) else None
    row_ref = row_ref if isinstance(row_ref, Mapping) else ref
    native = envelope.get("native_ref")
    request_id = envelope.get("request_id")
    selected_id = envelope.get("selected_id")
    if (not isinstance(row_ref, Mapping) or row_ref.get("harness") != "opencode"
            or not isinstance(row_ref.get("id"), str)
            or not row_ref["id"].startswith("S1:")
            or selected_id != row_ref.get("id")
            or type(request_id) is not str or not request_id.startswith("frm_")
            or row_ref.get("request_id") != request_id
            or not isinstance(native, Mapping)
            or native.get("transport") != "opencode-http-v2"
            or native.get("request_id") != request_id
            or type(native.get("generation")) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", native["generation"])
            or type(native.get("server_pid")) is not int or native["server_pid"] <= 0
            or type(native.get("proc_start")) is not str
            or not native["proc_start"].isdigit()
            or not isinstance(native.get("socket_inodes"), list)
            or not native["socket_inodes"]
            or any(type(inode) is not str or not inode.isdigit()
                   for inode in native["socket_inodes"])):
        return False
    if ((row_ref.get("route_id") is not None or envelope.get("route_id") is not None)
            and row_ref.get("route_id") != envelope.get("route_id")):
        return False
    if (native.get("session_id") is not None
            and native.get("session_id") != envelope.get("session_id")):
        return False
    detail = envelope.get("detail")
    fields = detail.get("questions") if isinstance(detail, Mapping) else None
    if fields is None and envelope.get("state") in {
            "submitted", "received", "applied", "cancelled", "expired", "unknown"}:
        return True
    if (type(native.get("schema_binding")) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", native["schema_binding"])):
        return False
    field_keys = native.get("field_keys")
    if (not isinstance(field_keys, list) or not field_keys
            or any(type(key) is not str or not key for key in field_keys)
            or len(set(field_keys)) != len(field_keys)):
        return False
    if not isinstance(fields, list) or not fields:
        return False
    seen = set()
    fields_by_id = {}
    supported_types = {"string", "number", "integer", "boolean", "multiselect"}
    for field in fields:
        if not isinstance(field, Mapping):
            return False
        field_id = field.get("id", field.get("key"))
        field_type = field.get("field_type", field.get("type"))
        if (type(field_id) is not str or not field_id or field_id in seen
                or field_type not in supported_types
                or type(field.get("required")) is not bool
                or type(field.get("hidden")) is not bool
                or field.get("hidden") and field.get("required")):
            return False
        conditions = field.get("when", [])
        if not isinstance(conditions, list):
            return False
        for condition in conditions:
            if (not isinstance(condition, Mapping)
                    or condition.get("op") not in {"eq", "neq"}
                    or condition.get("key") not in fields_by_id
                    or "value" not in condition):
                return False
            target = fields_by_id[condition["key"]]
            target_type = target.get("field_type", target.get("type"))
            value = condition["value"]
            if target_type == "boolean":
                valid_condition = type(value) is bool
            elif target_type in {"number", "integer"}:
                try:
                    valid_condition = (type(value) in (int, float, str)
                                       and math.isfinite(float(value)))
                except (TypeError, ValueError, OverflowError):
                    valid_condition = False
            else:
                valid_condition = isinstance(value, str)
                options = target.get("option_values", ())
                if (valid_condition and target.get("options_present")
                        and target.get("custom") is not True):
                    valid_condition = value in options
            if not valid_condition:
                return False
        seen.add(field_id)
        fields_by_id[field_id] = field
        choices = field.get("choices", field.get("options"))
        if choices is not None:
            if not isinstance(choices, list):
                return False
            values = []
            for choice in choices:
                if (not isinstance(choice, Mapping)
                        or type(choice.get("value")) is not str
                        or not choice["value"]
                        or type(choice.get("label")) is not str):
                    return False
                values.append(choice["value"])
            if len(set(values)) != len(values):
                return False
        if field_type == "multiselect":
            minimum, maximum = field.get("min_items"), field.get("max_items")
            if ((minimum is not None and (type(minimum) is not int or minimum < 0))
                    or (maximum is not None and (type(maximum) is not int or maximum < 0))
                    or (minimum is not None and maximum is not None and minimum > maximum)):
                return False
        elif field_type in {"number", "integer"}:
            for bound in ("minimum", "maximum"):
                value = field.get(bound)
                if (value is not None and (type(value) not in (int, float)
                                           or not math.isfinite(value))):
                    return False
            if (field.get("minimum") is not None and field.get("maximum") is not None
                    and field["minimum"] > field["maximum"]):
                return False
        elif field_type == "boolean" and (
                "default" in field and type(field.get("default")) is not bool):
            return False
    return field_keys == [field.get("id", field.get("key")) for field in fields]


def _explorer_opencode_form_ui_supported(envelope):
    """Accept only backend-approved native schemas the private form editor can express."""
    detail = envelope.get("detail") if isinstance(envelope, Mapping) else None
    native = envelope.get("native_ref") if isinstance(envelope, Mapping) else None
    questions = detail.get("questions") if isinstance(detail, Mapping) else None
    support = envelope.get("support") if isinstance(envelope, Mapping) else None
    supported = (support is True or isinstance(support, Mapping)
                 and support.get("supported") is True)
    if (envelope.get("state") != "open" or not supported
            or not isinstance(questions, list) or not questions
            or not isinstance(native, Mapping)
            or native.get("transport") != "opencode-http-v2"):
        return False
    return native.get("field_keys") == [question.get("id") for question in questions
                                          if isinstance(question, Mapping)]


def _explorer_question_envelope(ref, value):
    if not isinstance(value, Mapping):
        return None
    envelope = value.get("request") if isinstance(value.get("request"), Mapping) else value
    state = envelope.get("state")
    if state not in ("open", "submitted", "received", "applied", "cancelled", "expired", "unknown"):
        return None
    expected = dict(ref)
    row_ref = expected.get("_fleet_row_ref")
    if isinstance(row_ref, Mapping):
        expected.update({key: value for key, value in row_ref.items() if key not in expected})
    if (expected.get("selected_id") and envelope.get("selected_id")
            and expected["selected_id"] != envelope["selected_id"]):
        return None
    expected_native = ref.get("native_ref")
    actual_native = envelope.get("native_ref")
    if (isinstance(expected_native, Mapping) and actual_native is not None
            and dict(expected_native) != actual_native):
        return None
    for key in ("request_id", "turn_id", "route_id", "question_id", "gate_epoch"):
        expected_value, actual_value = expected.get(key), envelope.get(key)
        if (expected_value is not None and actual_value is not None
                and (expected_value != actual_value
                     or (key == "request_id" and type(expected_value) is not type(actual_value)))):
            return None
        if state != "unknown" and expected_value is not None and actual_value != expected_value:
            return None
    if state != "unknown":
        workflow = isinstance(ref.get("id"), str) and ref["id"].startswith(("S2:", "S3:", "S4:"))
        if workflow:
            turn_bound = _explorer_question_has_workflow_binding(ref, envelope)
        elif envelope.get("turn_id") is None:
            turn_bound = (envelope.get("harness") == "opencode"
                          and _explorer_opencode_form_binding(ref, envelope))
        else:
            turn_bound = isinstance(envelope.get("turn_id"), str) and bool(envelope["turn_id"])
        if (envelope.get("v") != 1 or envelope.get("harness") != ref.get("harness")
                or envelope.get("session_id") != ref.get("session_id")
                or type(envelope.get("request_id")) not in (str, int)
                or envelope.get("request_id") == ""
                or not turn_bound
                or not envelope.get("kind") or "detail" not in envelope):
            return None
    return dict(envelope)


def _explorer_reveal(state, rows):
    """Open the folds that hide the selected row, so the kill cursor is never on an unseen row."""
    from dataclasses import replace
    by_id = {row.identity: row for row in rows}
    row, opened = by_id.get(state.selection), set()
    while row is not None and row.parent in by_id:
        parent = by_id[row.parent]
        if parent.open is False:
            opened.add(parent.identity)
        row = parent
    if not opened:
        return state
    return replace(state, nav_open=tuple(sorted(set(state.nav_open) | opened)),
                   nav_fold=tuple(x for x in state.nav_fold if x not in opened))


_EXPLORER_SPIN_KEYS = frozenset(("spin", "g_spin", "g_spin_dim"))


_ESC = 27
