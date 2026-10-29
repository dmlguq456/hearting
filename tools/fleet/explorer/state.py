"""Compact view-local state and reducer for the Fleet explorer."""

from dataclasses import dataclass, field, replace
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import json
import math


@dataclass(frozen=True)
class NavState:
    view: str = "OVERVIEW"
    scope: str = "-"
    opened: str = ""
    usage_open: bool = False
    scroll: tuple = ()
    title: str = "OVERVIEW"
    status: str = ""
    parents: tuple = ()
    folds: tuple = ()
    drag: tuple = ()
    # Private, process-local selected question detail. serialize() deliberately omits it.
    question_overlay: object = field(default=None, repr=False, compare=False)

    def fold_map(self):
        return dict(self.folds)

    def opened_for(self, key, default=""):
        return self.fold_map().get(str(key), default)

    @classmethod
    def from_value(cls, value=None):
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        names = set(cls.__dataclass_fields__)
        values = {key: value[key] for key in names if key in value}
        values.pop("question_overlay", None)
        for key in ("folds", "scroll", "drag"):
            values[key] = tuple(tuple(item) for item in values.get(key, ()))
        values["parents"] = tuple(values.get("parents", ()))
        return cls(**values)


VIEWS = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE", "MEMORY", "MODELS", "SYSTEM")


_MODEL_FIND_FOLDS = ("ui:mfind", "ui:mfocus", "ui:mcursor")


def models_finding(state):
    """The MODELS model list holds the keyboard: typed characters filter it instead of acting."""
    return (getattr(state, "view", None) == "MODELS" and getattr(state, "scope", "-") == "-"
            and state.fold_map().get("ui:mfocus") == "1")


def _models_find_hit(frame):
    return next((hit for hit in getattr(frame, "hits", ())
                 if hit.action[:1] == ("models.find",) and len(hit.action) >= 6), None)


def _models_find_key(state, key, hit):
    """One key while the model list is focused; ``None`` leaves the key to the shared handling."""
    _name, count, item, cursor, top, rows = hit.action[:6]
    count, cursor, top, rows = int(count), int(cursor), int(top), max(1, int(rows))
    folds = state.fold_map()
    offsets = dict(state.scroll)
    if key in ("escape", "esc"):
        folds.pop("ui:mfocus", None)
    elif key in ("enter", "\n"):
        if not item:
            return state
        folds["ui:model"] = str(item)
    elif key == "backspace" or (isinstance(key, str) and key.startswith("text:")):
        text = folds.get("ui:mfind", "")
        text = text[:-1] if key == "backspace" else (text + key[5:])[:80]
        folds["ui:mfind"] = text
        folds["ui:mcursor"] = "0"
        offsets["mpick"] = 0
    elif key in ("up", "down", "pageup", "pagedown", "home", "end"):
        if not count:
            return state
        cursor = {"up": cursor - 1, "down": cursor + 1, "pageup": cursor - rows,
                  "pagedown": cursor + rows, "home": 0, "end": count - 1}[key]
        cursor = max(0, min(count - 1, cursor))
        if cursor < top:
            top = cursor
        elif cursor >= top + rows:
            top = cursor - rows + 1
        folds["ui:mcursor"] = str(cursor)
        offsets["mpick"] = max(0, min(max(0, count - rows), top))
    else:
        return None
    return replace(state, folds=tuple(sorted(folds.items())), scroll=tuple(sorted(offsets.items())))


def _scroll_region(frame, name):
    region = getattr(frame, name, ()) if frame is not None else ()
    return tuple(region) if isinstance(region, (list, tuple)) else ()


def _tree_release_on_press(drag, x, y, frame):
    """An unmoved tree press released on what it grabbed: its own cell, or a hit of that identity.

    The press cell uses the motion threshold (same row, under two columns). Any other release
    position counts only on a hit whose action is the grabbed ``("scope", identity)``.
    """
    if y == int(drag[5]) and abs(x - int(drag[4])) < 2:
        return True
    hit = next((item for item in getattr(frame, "hits", ())
                if item.y == y and item.x0 <= x < item.x1), None)
    return hit is not None and tuple(hit.action[:2]) == ("scope", drag[3])


_QUESTION_REF_FIELDS = ("id", "harness", "session_id", "request_id", "turn_id",
                        "route_id", "question_id")
_QUESTION_DRAFT_PREFIX = "hearting-question-draft:v1:"


def _question_ref(value):
    if not isinstance(value, Mapping):
        return {}
    result = {key: value[key] for key in _QUESTION_REF_FIELDS
              if isinstance(value.get(key), str) and value.get(key)}
    if type(value.get("request_id")) is int:
        result["request_id"] = value["request_id"]
    return result


def _question_refs(event):
    refs = event.get("question_refs", ())
    return tuple(_question_ref(item) for item in refs if isinstance(item, Mapping))


def _question_binding_complete(ref):
    return all(ref.get(key) for key in ("id", "harness", "session_id"))


def _question_overlay(state):
    return state.question_overlay if isinstance(state.question_overlay, Mapping) else {}


def _question_supports_reply(detail):
    support = detail.get("support")
    if support is False:
        return False
    if isinstance(support, Mapping):
        if support.get("available") is False or support.get("supported") is False:
            return False
    if isinstance(support, str) and support.lower() not in ("supported", "available", "native"):
        return False
    return isinstance(detail.get("native_ref"), Mapping)


def _question_schema_items(detail):
    """Return every field from the exact selected schema, before UI visibility rules."""
    if not isinstance(detail, Mapping):
        return ()
    body = detail.get("detail")
    questions = detail.get("questions")
    if not isinstance(questions, (list, tuple)) and isinstance(body, Mapping):
        questions = body.get("questions")
    if isinstance(questions, (list, tuple)):
        result = []
        native_ref = detail.get("native_ref")
        for question in questions:
            if not isinstance(question, Mapping):
                continue
            question_id = question.get("id", question.get("question_id", question.get("key")))
            if not isinstance(question_id, (str, int)) or isinstance(question_id, bool):
                continue
            item = dict(question)
            item["id"] = str(question_id)
            if (detail.get("harness") == "opencode" and isinstance(native_ref, Mapping)
                    and len(questions) == 1):
                for source, target in (("field_type", "field_type"), ("custom", "custom"),
                                       ("min_items", "min_items"),
                                       ("max_items", "max_items"),
                                       ("option_values", "option_values")):
                    if target not in item and source in native_ref:
                        item[target] = native_ref[source]
            if (detail.get("harness") == "opencode" and item.get("field_type") == "boolean"
                    and not item.get("choices")):
                item["choices"] = [{"value": "true", "label": "Yes"},
                                    {"value": "false", "label": "No"}]
            result.append(item)
        return tuple(result)
    question_id = detail.get("question_id")
    if not isinstance(question_id, (str, int)) or isinstance(question_id, bool):
        return ()
    item = {"id": str(question_id), "question": body}
    choices = detail.get("choices")
    if not isinstance(choices, (list, tuple)) and isinstance(body, Mapping):
        choices = body.get("choices", body.get("options"))
    if isinstance(choices, (list, tuple)):
        item["choices"] = choices
    if isinstance(body, Mapping) and "is_other" in body:
        item["is_other"] = bool(body.get("is_other"))
    native_ref = detail.get("native_ref")
    if detail.get("harness") == "opencode" and isinstance(native_ref, Mapping):
        for key in ("field_type", "custom", "min_items", "max_items", "option_values"):
            if key not in item and key in native_ref:
                item[key] = native_ref[key]
    if detail.get("harness") == "opencode" and item.get("field_type") == "boolean":
        item["choices"] = [{"value": "true", "label": "Yes"},
                            {"value": "false", "label": "No"}]
    return (item,)


def question_items(detail):
    """Return visible fields, hiding hidden fields and unsatisfied conditions."""
    if not isinstance(detail, Mapping):
        return ()
    questions = _question_schema_items(detail)
    native = detail.get("native_ref") if isinstance(detail, Mapping) else None
    if (detail.get("harness") != "opencode" or not isinstance(native, Mapping)
            or native.get("transport") != "opencode-http-v2"):
        return questions
    active = detail.get("_fleet_active_field_keys")
    if not isinstance(active, (list, tuple)):
        active = _question_active_field_keys(detail, {})
    active = set(active)
    return tuple(question for question in questions
                 if question.get("id") in active and question.get("hidden") is not True)


def _question_empty_hidden_optional_v2_form(detail):
    """Allow explicit empty submission only for a complete optional hidden V2 schema."""
    if (not isinstance(detail, Mapping) or detail.get("kind") != "question"
            or detail.get("state") != "open"
            or detail.get("harness") != "opencode"):
        return False
    native = detail.get("native_ref")
    if (not isinstance(native, Mapping)
            or native.get("transport") != "opencode-http-v2"):
        return False
    fields = _question_schema_items(detail)
    field_keys = native.get("field_keys")
    if (not fields or not isinstance(field_keys, (list, tuple))
            or len(field_keys) != len(fields)
            or tuple(field_keys) != tuple(item["id"] for item in fields)
            or len(set(field_keys)) != len(field_keys)):
        return False
    if question_items(detail):
        return False
    supported_types = {"string", "number", "integer", "boolean", "multiselect"}
    return all(item.get("hidden") is True
               and item.get("required") is False
               and item.get("field_type") in supported_types
               for item in fields)


def question_answer_supported(detail):
    if not isinstance(detail, Mapping):
        return False
    if not _question_supports_reply(detail):
        return False
    questions = question_items(detail)
    if questions:
        if (detail.get("harness") == "opencode"
                and isinstance(detail.get("native_ref"), Mapping)
                and detail["native_ref"].get("transport") == "opencode-http-v2"
                and any(item.get("hidden") is True and item.get("required") is True
                        for item in _question_schema_items(detail))):
            return False
        return True
    return _question_empty_hidden_optional_v2_form(detail)


def question_draft_supported(detail):
    """Allow private drafts for an exact open question even without a reply transport."""
    return (isinstance(detail, Mapping) and detail.get("kind") == "question"
            and detail.get("state") == "open"
            and isinstance(detail.get("native_ref"), Mapping)
            and bool(question_items(detail)))


def question_action_supported(detail):
    return (_question_supports_reply(detail)
            and (_question_is_approval(detail) or question_answer_supported(detail)))


def _question_is_approval(detail):
    kind = str(detail.get("kind") or "").lower()
    return ("approval" in kind or "permission" in kind or
            "commandexecution" in kind or "filechange" in kind)


def _question_choice_value(choice):
    if isinstance(choice, Mapping):
        value = choice.get("value")
        if value is None:
            value = choice.get("label", choice.get("text"))
    else:
        value = choice
    return value if isinstance(value, str) else None


def _question_choice_allowed(detail, question_id, answer):
    question = next((item for item in question_items(detail) if item["id"] == question_id), None)
    if question is None:
        return False
    choices = question.get("choices", question.get("options", ()))
    if not isinstance(choices, (list, tuple)):
        return False
    return any(_question_choice_value(choice) == answer for choice in choices)


def _question_uses_workflow_transport(detail):
    native = detail.get("native_ref")
    return (isinstance(native, Mapping) and native.get("transport") in {
        "hearting-owner-input", "hearting-human-gate", "hearting-answer-marker"})


def question_free_text_supported(detail, question_id):
    question = next((item for item in question_items(detail) if item["id"] == question_id), None)
    if question is None:
        return False
    native_ref = detail.get("native_ref")
    if _question_uses_workflow_transport(detail):
        choices = question.get("choices", question.get("options"))
        return (question.get("is_other") is True
                or choices is None
                or isinstance(choices, (list, tuple)) and not choices)
    if detail.get("harness") == "opencode":
        field_type = question.get("field_type")
        if field_type == "string":
            return (question.get("options_present") is not True
                    or question.get("custom") is True)
        if field_type in {"number", "integer"}:
            return True
        if field_type == "multiselect":
            return question.get("custom") is True
        return False
    choices = question.get("choices", question.get("options"))
    if question.get("is_other") is True:
        return True
    return choices is None


def question_multiple_select(detail, question_id):
    question = next((item for item in question_items(detail) if item["id"] == question_id), None)
    if question is None or detail.get("harness") != "opencode":
        return False
    if question.get("field_type") != "multiselect":
        return False
    minimum, maximum = question.get("min_items"), question.get("max_items")
    return ((minimum is not None and type(minimum) is int and minimum > 1)
            or maximum is None or (type(maximum) is int and maximum > 1))


def question_values_valid(detail, question_id, values):
    question = next((item for item in _question_schema_items(detail)
                     if item["id"] == question_id), None)
    if question is None:
        return False
    workflow = _question_uses_workflow_transport(detail)
    if (detail.get("harness") == "opencode" and not workflow
            and isinstance(detail.get("native_ref"), Mapping)
            and detail["native_ref"].get("transport") == "opencode-http-v2"):
        return _question_values_valid_item(question, values)
    if (values is None or isinstance(values, (list, tuple)) and not values):
        return False
    if (not isinstance(values, (list, tuple))
            or any(not isinstance(value, str) or not value.strip() for value in values)):
        return False
    choices = question.get("choices", question.get("options"))
    if (isinstance(choices, (list, tuple)) and (choices or not workflow)
            and question.get("is_other") is not True):
        allowed = {_question_choice_value(choice) for choice in choices}
        return all(value in allowed for value in values)
    return True


def _question_decimal(value):
    if type(value) not in (int, float, str):
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    try:
        number = Decimal(value.strip() if isinstance(value, str) else str(value))
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return number if number.is_finite() else None


def _question_values_valid_item(question, values):
    if values is None or isinstance(values, (list, tuple)) and not values:
        return question.get("required") is not True
    if not isinstance(values, (list, tuple)):
        return False
    field_type = question.get("field_type")
    required = question.get("required") is True
    if field_type == "string":
        if len(values) != 1 or not isinstance(values[0], str):
            return False
        value = values[0]
        if value == "":
            return not required
        try:
            length = len(value.encode("utf-16-le")) // 2
        except UnicodeEncodeError:
            return False
        minimum, maximum = question.get("min_length"), question.get("max_length")
        if ((minimum is not None and length < minimum)
                or (maximum is not None and length > maximum)):
            return False
        if (question.get("options_present") and question.get("custom") is not True
                and value not in question.get("option_values", ())):
            return False
        return True
    if field_type in {"number", "integer"}:
        if len(values) != 1:
            return False
        raw = values[0]
        if isinstance(raw, str) and not raw.strip():
            return not required
        number = _question_decimal(raw)
        minimum = (_question_decimal(question.get("minimum"))
                   if question.get("minimum") is not None else None)
        maximum = (_question_decimal(question.get("maximum"))
                   if question.get("maximum") is not None else None)
        if ((question.get("minimum") is not None and minimum is None)
                or (question.get("maximum") is not None and maximum is None)
                or number is None):
            return False
        if ((minimum is not None and number < minimum)
                or (maximum is not None and number > maximum)):
            return False
        if field_type == "integer":
            return (-9007199254740991 <= number <= 9007199254740991
                    and number == number.to_integral_value())
        native = float(number)
        return math.isfinite(native) and Decimal(str(native)) == number
    if field_type == "boolean":
        if len(values) != 1:
            return False
        value = values[0]
        return (type(value) is bool or value in ("true", "false"))
    if field_type == "multiselect":
        if any(not isinstance(value, str) or not value for value in values):
            return False
        minimum, maximum = question.get("min_items"), question.get("max_items")
        if ((minimum is not None and (type(minimum) is not int or minimum < 0))
                or (maximum is not None and (type(maximum) is not int or maximum < 0))
                or (minimum is not None and maximum is not None and minimum > maximum)
                or (minimum is not None and len(values) < minimum)
                or (maximum is not None and len(values) > maximum)
                or len(set(values)) != len(values)):
            return False
        allowed = question.get("option_values", ())
        return question.get("custom") is True or all(value in allowed for value in values)
    return False


def _question_native_answer(question, values):
    if not values or not _question_values_valid_item(question, values):
        return None
    field_type = question.get("field_type")
    if field_type == "string":
        return values[0] if values[0] != "" else None
    if field_type in {"number", "integer"}:
        if isinstance(values[0], str) and not values[0].strip():
            return None
        number = _question_decimal(values[0])
        if number is None:
            return None
        return int(number) if field_type == "integer" else float(number)
    if field_type == "boolean":
        value = values[0]
        if type(value) is bool:
            return value
        if value == "true":
            return True
        if value == "false":
            return False
        return None
    if field_type == "multiselect":
        return list(values)
    return None


def _question_active_field_keys(detail, answers):
    fields = _question_schema_items(detail)
    active, native_answers = [], {}
    for question in fields:
        conditions = question.get("when", ())
        matches = True
        for condition in conditions if isinstance(conditions, (list, tuple)) else ():
            if not isinstance(condition, Mapping):
                matches = False
                break
            key = condition.get("key")
            if key not in native_answers or condition.get("op") not in {"eq", "neq"}:
                matches = False
                break
            value = native_answers[key]
            wanted = condition.get("value")
            hit = (any(item == wanted for item in value) if isinstance(value, list)
                   else value == wanted)
            if ((condition.get("op") == "eq" and not hit)
                    or (condition.get("op") == "neq" and hit)):
                matches = False
                break
        if not matches:
            continue
        question_id = question["id"]
        if question.get("hidden") is not True:
            active.append(question_id)
        values = answers.get(question_id) if isinstance(answers, Mapping) else None
        if (question.get("hidden") is not True and values is not None
                and _question_values_valid_item(question, values)):
            native_value = _question_native_answer(question, values)
            if native_value is not None:
                native_answers[question_id] = native_value
    return active


def question_ui_detail(detail, draft=None):
    """Attach only active field IDs to a private envelope for the question table."""
    if not isinstance(detail, Mapping):
        return detail
    native = detail.get("native_ref")
    if (detail.get("harness") != "opencode" or not isinstance(native, Mapping)
            or native.get("transport") != "opencode-http-v2"):
        return detail
    if isinstance(draft, str):
        draft = question_draft_from_backend(detail, draft)
    answers = draft.get("answers", {}) if isinstance(draft, Mapping) else {}
    result = dict(detail)
    result["_fleet_active_field_keys"] = _question_active_field_keys(detail, answers)
    return result


def question_free_text_available(detail, question_id, values=()):
    if not question_free_text_supported(detail, question_id):
        return False
    if not question_multiple_select(detail, question_id):
        return True
    question = next((item for item in question_items(detail) if item["id"] == question_id), None)
    maximum = question.get("max_items") if question else None
    if maximum is None:
        return True
    if type(maximum) is not int:
        return False
    allowed = set(question.get("option_values", ())) if question else set()
    has_custom = any(value not in allowed for value in values)
    return has_custom or len(values) < maximum


def question_choice_click_allowed(detail, question_id, answer, values=()):
    if not question_multiple_select(detail, question_id):
        return True
    question = next((item for item in question_items(detail) if item["id"] == question_id), None)
    if question is None:
        return False
    maximum = question.get("max_items")
    maximum = maximum if type(maximum) is int else None
    if answer in values:
        return True
    return maximum is None or len(values) < maximum


def question_decisions(detail):
    if not _question_is_approval(detail):
        return ()
    result = ["approve", "reject", "defer"]
    native_ref = detail.get("native_ref")
    request = native_ref.get("request") if isinstance(native_ref, Mapping) else None
    if (isinstance(request, Mapping)
            and request.get("method") == "item/commandExecution/requestApproval"):
        native_detail = detail.get("detail")
        available = (native_detail.get("availableDecisions")
                     if isinstance(native_detail, Mapping) else None)
        if available is not None:
            result = []
            if isinstance(available, list):
                if "accept" in available:
                    result.append("approve")
                if "decline" in available:
                    result.append("reject")
            result.append("defer")
    actions = detail.get("actions")
    if isinstance(actions, (list, tuple)):
        result = [decision for decision in result if decision in actions]
    return tuple(result)


def _question_action_ref(overlay):
    row_ref = _question_ref(overlay.get("ref"))
    envelope = overlay.get("detail")
    if not isinstance(envelope, Mapping):
        return row_ref
    result = dict(envelope)
    result.update(row_ref)
    result["_fleet_row_ref"] = dict(row_ref)
    questions = question_items(envelope)
    if len(questions) == 1:
        result.setdefault("question_id", questions[0]["id"])
    elif len(questions) > 1:
        result.setdefault("question_ids", [item["id"] for item in questions])
    return result


def _question_draft_for_backend(draft, detail=None):
    answers = draft.get("answers") if isinstance(draft, Mapping) else None
    normalized = {}
    if isinstance(answers, Mapping):
        for question_id, values in answers.items():
            if isinstance(values, (tuple, list)):
                normalized[str(question_id)] = [
                    value for value in values
                    if (isinstance(value, (str, bool, int))
                        or isinstance(value, float) and math.isfinite(value))]
    fields = _question_schema_items(detail)
    native = detail.get("native_ref") if isinstance(detail, Mapping) else None
    v2_form = isinstance(native, Mapping) and native.get("transport") == "opencode-http-v2"
    single_string = (not v2_form or bool(fields)
                     and fields[0].get("field_type") == "string")
    if (len(fields) == 1 and single_string
            and len(normalized) == 1 and len(next(iter(normalized.values()))) <= 1
            and all(isinstance(value, str) for value in next(iter(normalized.values())))):
        values = next(iter(normalized.values()))
        return values[0] if values else ""
    if not normalized:
        return ""
    return _QUESTION_DRAFT_PREFIX + json.dumps({"answers": normalized}, ensure_ascii=False,
                                                separators=(",", ":"))


def question_draft_from_backend(detail, draft):
    if not isinstance(draft, str):
        return None
    if draft.startswith(_QUESTION_DRAFT_PREFIX):
        try:
            value = json.loads(draft[len(_QUESTION_DRAFT_PREFIX):])
        except (TypeError, ValueError):
            return None
        answers = value.get("answers") if isinstance(value, Mapping) else None
        if not isinstance(answers, Mapping):
            return None
        return {"answers": {str(key): [
                            item for item in values
                            if (isinstance(item, (str, bool, int))
                                or isinstance(item, float) and math.isfinite(item))]
                            for key, values in answers.items()
                            if isinstance(values, (list, tuple))}}
    questions = _question_schema_items(detail)
    if not questions:
        return None
    return {"answers": {questions[0]["id"]: [draft]}}


def _question_selection(state, ref):
    ref = _question_ref(ref)
    if not ref:
        return state, ()
    folds = state.fold_map()
    selected = str(ref.get("id") or "")
    folds["qsel:QUESTIONS"] = selected
    previous = _question_overlay(state)
    if (folds.get("qsel:QUESTIONS") == state.opened_for("qsel:QUESTIONS")
            and previous.get("ref") == ref and previous.get("state") in
            ("loading", "submitted", "received", "applied", "cancelled", "expired")):
        return replace(state, folds=tuple(sorted(folds.items()))), ()
    if previous.get("ref") == ref and previous.get("editing"):
        return state, ()
    if _question_binding_complete(ref):
        retained = previous.get("draft") if previous.get("ref") == ref else None
        overlay = {"ref": ref, "state": "loading", "detail": None, "draft": retained,
                   "editing": False, "pending": False, "status": "Loading selected request…"}
        effects = (("questions.read", ref),)
    else:
        overlay = {"ref": ref, "state": "unknown", "detail": None, "draft": None,
                   "editing": False, "pending": False,
                   "status": "No exact session binding; use the native prompt."}
        effects = ()
    return replace(state, folds=tuple(sorted(folds.items())), question_overlay=overlay), effects


def _question_draft_response(overlay, *, require_complete=False):
    draft = overlay.get("draft")
    if not isinstance(draft, Mapping):
        return None
    answers = draft.get("answers")
    if not isinstance(answers, Mapping):
        return None
    questions = question_items(overlay.get("detail"))
    if not questions:
        return None
    normalized = {}
    missing_required = False
    for question in questions:
        question_id = question["id"]
        values = answers.get(question_id, ())
        if isinstance(values, (list, tuple)):
            clean = [value for value in values if (
                isinstance(value, str) and value != ""
                or type(value) is bool
                or isinstance(value, (int, float)) and not isinstance(value, bool)
                and (not isinstance(value, float) or math.isfinite(value)))]
            if clean:
                normalized[question_id] = clean
        if question.get("required") is True and question_id not in normalized:
            missing_required = True
    if require_complete and missing_required:
        return None
    return {"answers": normalized} if normalized else None


def _question_text_draft(overlay, question_id=None):
    response = _question_draft_response(overlay)
    questions = question_items(overlay.get("detail"))
    question_id = str(question_id or overlay.get("editing_question_id") or
                      (questions[0]["id"] if questions else ""))
    answers = response.get("answers", {}) if response else {}
    values = answers.get(question_id, ()) if question_id else ()
    if values and question_free_text_supported(overlay.get("detail"), question_id):
        question = next((item for item in questions if item["id"] == question_id), None)
        if question and question_multiple_select(overlay.get("detail"), question_id):
            allowed = set(question.get("option_values", ()))
            return str(next((value for value in values if value not in allowed), ""))
    return str(values[0]) if values else ""


def _question_store_answers(state, question_id, values, *, editing=False):
    overlay = dict(_question_overlay(state))
    detail = overlay.get("detail")
    draft = overlay.get("draft")
    draft = {key: dict(value) if isinstance(value, Mapping) else value
             for key, value in draft.items()} if isinstance(draft, Mapping) else {}
    answers = draft.get("answers")
    answers = {str(key): list(value) if isinstance(value, (list, tuple)) else []
               for key, value in answers.items()} if isinstance(answers, Mapping) else {}
    answers[question_id] = list(values)
    draft["answers"] = answers
    detail = question_ui_detail(detail, draft)
    supported = question_answer_supported(detail)
    overlay.update(detail=detail, draft=draft, editing=editing, editing_question_id=question_id,
                   status=("Draft updated; submit explicitly to send." if supported else
                           "Private draft saved; use the native prompt to respond."))
    ref = _question_action_ref(overlay)
    return replace(state, question_overlay=overlay), (("questions.draft", ref,
                                                        _question_draft_for_backend(draft, detail)),)


def _question_set_answer(state, question_id, answer):
    overlay = dict(_question_overlay(state))
    detail = overlay.get("detail")
    if (not isinstance(detail, Mapping) or overlay.get("state") != "open"
            or detail.get("state") != "open" or not question_draft_supported(detail)):
        return state, ()
    question_id = str(question_id or detail.get("question_id") or "")
    if (not question_id or question_id not in {item["id"] for item in question_items(detail)}
            or not isinstance(answer, str)
            or not question_free_text_available(detail, question_id,
                                                (_question_draft_response(overlay) or {}).get(
                                                    "answers", {}).get(question_id, ()))):
        return state, ()
    existing = ((_question_draft_response(overlay) or {}).get("answers", {}).get(question_id, ()))
    if question_multiple_select(detail, question_id):
        question = next(item for item in question_items(detail) if item["id"] == question_id)
        allowed = set(question.get("option_values", ()))
        values = [value for value in existing if value in allowed]
        maximum = question.get("max_items")
        if answer and type(maximum) is int and len(values) >= maximum:
            return state, ()
        if answer:
            values.append(answer)
    else:
        values = [answer]
    return _question_store_answers(state, question_id, values, editing=True)


def _question_toggle_choice(state, question_id, answer):
    overlay = _question_overlay(state)
    detail = overlay.get("detail")
    answers = ((_question_draft_response(overlay) or {}).get("answers", {}))
    current = list(answers.get(question_id, ()))
    if not question_choice_click_allowed(detail, question_id, answer, current):
        return state, ()
    if question_multiple_select(detail, question_id):
        values = [value for value in current if value != answer]
        if len(values) == len(current):
            values.append(answer)
    else:
        values = [answer]
    return _question_store_answers(state, question_id, values)


def _question_submit(state, response=None):
    overlay = dict(_question_overlay(state))
    detail = overlay.get("detail")
    if (not isinstance(detail, Mapping) or overlay.get("state") != "open"
            or overlay.get("pending")
            or detail.get("state") != "open" or not _question_supports_reply(detail)):
        return state, ()
    if not _question_is_approval(detail):
        native = detail.get("native_ref")
        opencode_form = (detail.get("harness") == "opencode" and isinstance(native, Mapping)
                         and native.get("transport") == "opencode-http-v2")
        if response is None:
            response = _question_draft_response(overlay, require_complete=True)
            if response is None and opencode_form:
                # Only this explicit submit action can send an optional-only empty form.
                response = {"answers": {}}
        questions = question_items(detail)
        if (not question_answer_supported(detail) or not isinstance(response, Mapping)
                or not isinstance(response.get("answers"), Mapping)):
            return state, ()
        answers = response["answers"]
        if opencode_form:
            question_ids = {item["id"] for item in questions}
            if set(answers).difference(question_ids):
                return state, ()
            typed_answers = {}
            for question in questions:
                question_id = question["id"]
                values = answers.get(question_id)
                if values is None or isinstance(values, (list, tuple)) and not values:
                    if question.get("required") is True:
                        return state, ()
                    continue
                if not question_values_valid(detail, question_id, values):
                    return state, ()
                value = _question_native_answer(question, values)
                if value is None:
                    if question.get("required") is True:
                        return state, ()
                    continue
                typed_answers[question_id] = value if question.get("field_type") == "multiselect" else [value]
            response = {"answers": typed_answers}
        else:
            question_ids = {item["id"] for item in questions}
            if (not answers or set(answers) != question_ids
                    or any(not question_values_valid(detail, question_id,
                                                     answers.get(question_id))
                           for question_id in question_ids)):
                return state, ()
    elif (not isinstance(response, Mapping)
          or response.get("decision") not in question_decisions(detail)
          or response.get("decision") == "defer"):
        return state, ()
    overlay.update(pending=True, editing=False, state="submitted",
                   status="Submitted; waiting for the runtime result.")
    return replace(state, question_overlay=overlay), (("questions.submit", _question_action_ref(
        overlay), dict(response)),)


def reduce(state, event, frame=None):
    """Apply one normalized input or hit-map action and preserve its order."""
    before = NavState.from_value(state)
    after, effects = _reduce(before, event, frame)
    folds = after.fold_map()
    if (after.scope, after.view) != (before.scope, before.view):
        # A generation pick belongs to the page it was made on.
        folds = {key: value for key, value in folds.items() if not key.startswith("ui:gen:")}
    # The open OVERVIEW session is a view-local fold; it is not cleared by moving away.
    after = replace(after, opened=str(folds.get("sopen:OVERVIEW") or ""),
                    folds=tuple(sorted(folds.items())))
    return after, effects


def _reduce(state, event, frame=None):
    if isinstance(event, str):
        event = {"type": "key", "key": event}
    if not isinstance(event, Mapping):
        return state, ()
    kind, key = event.get("type", "key"), event.get("key")
    if kind in ("click", "action"):
        action = tuple(event.get("action", ()))
        if not action:
            return state, ()
        if action[0] in ("view", "tab") and len(action) > 1:
            view = str(action[1])
            return replace(state, view=view, scope="-", opened="", title=view), ()
        if action[0] == "fold" and len(action) > 2:
            slot, identity = str(action[1]), str(action[2])
            folds = state.fold_map()
            folds[slot] = "" if folds.get(slot, "") == identity else identity
            opened = folds[slot] if slot == "sopen:OVERVIEW" else state.opened
            return replace(state, opened=opened, folds=tuple(sorted(folds.items()))), ()
        if action[0] == "question.older.toggle":
            folds = state.fold_map()
            key = "qolder:QUESTIONS"
            folds[key] = "" if folds.get(key) == "open" else "open"
            return replace(state, folds=tuple(sorted(folds.items()))), ()
        if action[0] == "question.select" and len(action) > 2:
            return _question_selection(state, action[2])
        if action[0] == "question.choice" and len(action) > 2:
            detail = _question_overlay(state).get("detail")
            question_id, answer = str(action[1]), str(action[2])
            answers = ((_question_draft_response(_question_overlay(state)) or {}).get(
                "answers", {}).get(question_id, ()))
            if (not isinstance(detail, Mapping) or detail.get("state") != "open"
                    or _question_overlay(state).get("state") != "open"
                    or _question_is_approval(detail)
                    or not question_draft_supported(detail)
                    or not _question_choice_allowed(detail, question_id, answer)
                    or not question_choice_click_allowed(detail, question_id, answer, answers)):
                return state, ()
            if question_multiple_select(detail, question_id):
                return _question_toggle_choice(state, question_id, answer)
            return _question_store_answers(state, question_id, [answer])
        if action[0] == "question.edit":
            overlay = dict(_question_overlay(state))
            detail = overlay.get("detail")
            if (isinstance(detail, Mapping) and overlay.get("state") == "open"
                    and detail.get("state") == "open"
                    and question_draft_supported(detail)):
                questions = question_items(detail)
                question_id = (str(action[1]) if len(action) > 1 else
                               (questions[0]["id"] if questions else ""))
                if question_id not in {item["id"] for item in questions}:
                    return state, ()
                answers = ((_question_draft_response(overlay) or {}).get("answers", {}))
                if not question_free_text_available(detail, question_id,
                                                    answers.get(question_id, ())):
                    return state, ()
                overlay.update(editing=True, editing_question_id=question_id,
                               status="Type an answer; Enter submits it.")
                return replace(state, question_overlay=overlay), ()
            return state, ()
        if action[0] == "question.submit":
            return _question_submit(state)
        if action[0] == "question.decision" and len(action) > 1:
            decision = str(action[1])
            if decision == "defer":
                overlay = dict(_question_overlay(state))
                detail = overlay.get("detail")
                if (isinstance(detail, Mapping) and overlay.get("state") == "open"
                        and detail.get("state") == "open"
                        and _question_is_approval(detail)
                        and _question_supports_reply(detail)
                        and decision in question_decisions(detail)):
                    overlay.update(state="open", pending=False, editing=False,
                                   status="Deferred; the request remains unresolved.")
                    return replace(state, question_overlay=overlay), ()
                return state, ()
            if decision in question_decisions(_question_overlay(state).get("detail")):
                return _question_submit(state, {"decision": decision})
            return state, ()
        if action[0] == "ui" and len(action) > 2:
            folds = state.fold_map()
            offsets = dict(state.scroll)
            if action[1] == "sel":
                folds = {key: value for key, value in folds.items()
                         if key not in ("ui:sel", "ui:model", "ui:effort") + _MODEL_FIND_FOLDS}
                offsets.pop("mpick", None)
            folds["ui:" + str(action[1])] = str(action[2])
            return replace(state, folds=tuple(sorted(folds.items())),
                           scroll=tuple(sorted(offsets.items()))), ()
        if action[0] == "models.find":
            folds = state.fold_map()
            folds["ui:mfocus"] = "1"
            return replace(state, folds=tuple(sorted(folds.items()))), ()
        if action[0] == "models.apply" and len(action) in (5, 7):
            return state, (action,)
        if action[0] == "alloc.apply" and len(action) == 3:
            return state, (action,)
        if action[0] == "usage":
            return replace(state, usage_open=not state.usage_open), ()
        if action[0] in ("page", "scope") and len(action) > 1:
            scope = str(action[1]) if action[0] == "scope" else f"{action[1]}:{action[2]}"
            if action[0] == "scope" and scope != "-" and ":" not in scope:
                scope = "session:" + scope
            parents = state.parents + (state.scope,)
            return replace(state, scope=scope, opened="", title=scope.split(":", 1)[-1],
                           parents=parents), ()
        if action[0] == "route" and len(action) > 1:
            offsets = dict(state.scroll)
            offsets["rawoff"] = 0
            return replace(state, scope=f"route:{action[1]}", opened="",
                           title=str(action[1]), parents=state.parents + (state.scope,),
                           scroll=tuple(sorted(offsets.items()))), ()
        if action[0] == "pane" and len(action) > 2:
            return state, (("focus", action[1], action[2]),)
        if action[0] == "root" and len(action) > 1:
            view = str(action[1])
            folds = state.fold_map()
            if len(action) > 2 and isinstance(action[2], Mapping):
                opened = str(action[2].get("ropen:" + view, ""))
                if opened:
                    folds["ropen:" + view] = opened
            return replace(state, view=view, scope="-", opened="",
                           title=view, folds=tuple(sorted(folds.items()))), ()
        return state, ()
    if kind != "key":
        if kind == "wheel":
            x, y = int(event.get("x", -1)), int(event.get("y", -1))
            hit = next((item for item in getattr(frame, "hits", ())
                        if item.y == y and item.x0 <= x < item.x1 and item.action[:1] == ("scroll",)), None)
            offsets = dict(state.scroll)
            if hit is not None:
                _name, slot, maximum, *sign = hit.action
                direction = int(event.get("direction", 0))
                amount = 3 * direction * (int(sign[0]) if sign else 1)
                offsets[slot] = max(0, min(int(maximum), int(offsets.get(slot, 0)) + amount))
                return replace(state, scroll=tuple(sorted(offsets.items()))), ()
            direction = int(event.get("direction", 0))
            for slot, name in (("tree", "tree_scroll"), ("pane", "pane_scroll")):
                region = _scroll_region(frame, name)
                if len(region) < 7:
                    continue
                x0, y0, x1, y1, painted, maximum, page = map(int, region[:7])
                if x0 <= x < x1 and y0 <= y < y1:
                    current = int(offsets.get(slot, painted))
                    offsets[slot] = max(0, min(maximum, current - 3 * direction))
                    return replace(state, scroll=tuple(sorted(offsets.items()))), ()
            return state, ()
        if kind == "mouse":
            phase = event.get("phase")
            x, y = int(event.get("x", -1)), int(event.get("y", -1))
            button = int(event.get("button", 0))
            if event.get("motion") and not state.drag:
                return state, ()
            offsets = dict(state.scroll)
            if phase == "release":
                if state.drag and state.drag[0] == "tree":
                    drag = state.drag
                    if drag[8]:
                        # Validate the actual release target against the painted frame, not
                        # just the previous motion packet's insertion preview.
                        moved, _ = _reduce(state, {**event, "phase": "press", "motion": True}, frame)
                        if moved.drag[6:8] != drag[6:8]:
                            return replace(state, drag=()), ()
                    if not drag[8] or (not drag[7] and _tree_release_on_press(drag, x, y, frame)):
                        # An unmoved press is a click on the identity it grabbed, wherever that
                        # row is drawn: the tree or a ("scope", …) row of the right pane. It is
                        # confirmed before the tree-only bounds below. Only motion packets set
                        # drag[8], so a release away from the press (motion never reported or not
                        # adopted) is a click only on a hit with the same identity.
                        if _tree_release_on_press(drag, x, y, frame):
                            return reduce(replace(state, drag=()),
                                          {"type": "click", "action": ("scope", drag[3])}, frame)
                        return replace(state, drag=()), ()
                    if x >= 32 or y < 0 or y >= int(getattr(frame, "rows", 0)):
                        return replace(state, drag=()), ()
                    if drag[7] and drag[8]:
                        ordering = event.get("ordering") or {}
                        source = (drag[2] if drag[1] == "repo" else
                                  getattr(ordering.get("tag_keys", {}), "get", lambda _key: None)(drag[2]))
                        target = drag[6]
                        if drag[1] == "session":
                            target = (getattr(ordering.get("tag_keys", {}), "get", lambda _key: None)(target)
                                      if target != "end" else None)
                        repo = (drag[2] if drag[1] == "repo" else
                                getattr(ordering.get("repo_by_tag", {}), "get", lambda _key: None)(drag[2]))
                        if source and repo is not None:
                            return replace(state, drag=()), (("reorder", drag[1], repo, source, target),)
                    return replace(state, drag=()), ()
                return replace(state, drag=()), ()
            if event.get("motion") and state.drag:
                if state.drag[0] == "tree":
                    drag = state.drag
                    moved = drag[8] or abs(y - drag[5]) >= 1 or abs(x - drag[4]) >= 2
                    if not moved:
                        return state, ()
                    hit = next((item for item in getattr(frame, "hits", ())
                                if item.y == y and item.x0 <= x < item.x1), None)
                    action = hit.action if hit is not None else ()
                    target_kind, target = None, None
                    valid = False
                    tree = _scroll_region(frame, "tree_scroll")
                    tree_end_visible = (len(tree) >= 8 and
                                        int(tree[4]) + int(tree[7]) >= int(tree[6]))
                    in_tree = (not tree or (len(tree) >= 4 and
                                            int(tree[0]) <= x < int(tree[2]) and
                                            int(tree[1]) <= y < int(tree[3])))
                    if action[:1] == ("scope",) and len(action) > 1:
                        value = str(action[1])
                        if value.startswith("repo:"):
                            target_kind, target = "repo", value[5:]
                        elif ":" not in value:
                            target_kind, target = "session", value
                    elif action[:1] == ("order-end",):
                        target, valid = "end", True
                    elif (hit is None and in_tree and (not tree or tree_end_visible) and x < 32 and
                          y > max((item.y for item in getattr(frame, "hits", ())
                                   if item.action[:1] == ("scope",) and len(item.action) > 1 and
                                   (str(item.action[1]).startswith("repo:") or
                                    ":" not in str(item.action[1]))), default=-1)):
                        # Empty rail cells below the last tree row are the end
                        # insertion point; they deliberately have no hit box.
                        target, valid = "end", True
                    if target_kind == drag[1]:
                        if target_kind == "repo":
                            valid = target != drag[2]
                        else:
                            ordering = event.get("ordering") or {}
                            repo_by_tag = ordering.get("repo_by_tag", {})
                            source_repo = repo_by_tag.get(drag[2])
                            valid = target != drag[2] and repo_by_tag.get(target) == source_repo
                    return replace(state, drag=("tree", drag[1], drag[2], drag[3], drag[4],
                                                 drag[5], target if valid else None, valid, moved, *drag[9:])), ()
                slot, maximum, origin_y, origin_offset, sign = state.drag
                offsets[slot] = max(0, min(int(maximum), int(origin_offset) +
                                           (y - int(origin_y)) * int(sign)))
                return replace(state, scroll=tuple(sorted(offsets.items()))), ()
            if phase != "press" or button != 0:
                return state, ()
            hit = next((item for item in getattr(frame, "hits", ())
                        if item.y == y and item.x0 <= x < item.x1), None)
            if hit is None:
                return state, ()
            if hit.action[:1] == ("scope",) and len(hit.action) > 1:
                scope = str(hit.action[1])
                if scope.startswith("repo:"):
                    return replace(state, drag=("tree", "repo", scope[5:], scope, x, y,
                                                None, False, False)), ()
                if ":" not in scope:
                    ordering = event.get("ordering") or {}
                    repo = ordering.get("repo_by_tag", {}).get(scope)
                    if repo is not None:
                        return replace(state, drag=("tree", "session", scope, scope, x, y,
                                                    None, False, False)), ()
            if hit.action[:1] == ("scroll",):
                _name, slot, maximum, *sign = hit.action
                return replace(state, drag=(slot, int(maximum), y,
                                            min(int(offsets.get(slot, 0)), int(maximum)),
                                            int(sign[0]) if sign else 1)), ()
            return reduce(state, {"type": "click", "action": hit.action}, frame)
        return state, ()
    if state.view == "QUESTIONS" and state.scope == "-":
        overlay = _question_overlay(state)
        refs = _question_refs(event)
        selected = state.opened_for("qsel:QUESTIONS", "")
        if key in ("up", "down") and refs:
            ids = [ref.get("id", "") for ref in refs]
            index = ids.index(selected) if selected in ids else (-1 if key == "down" else 0)
            step = -1 if key == "up" else 1
            return _question_selection(state, refs[(index + step) % len(refs)])
        if key in ("e", "E"):
            return _reduce(state, {"type": "click", "action": ("question.edit",)}, frame)
        if key in ("escape", "esc") and overlay.get("editing"):
            overlay = dict(overlay)
            overlay.update(editing=False, status="Editing paused; draft retained.")
            return replace(state, question_overlay=overlay), ()
        if overlay.get("editing") and key == "backspace":
            questions = question_items(overlay.get("detail"))
            question_id = overlay.get("editing_question_id") or (
                questions[0]["id"] if questions else None)
            text = _question_text_draft(overlay, question_id)
            if text and question_id:
                return _question_set_answer(state, question_id, text[:-1])
            return state, ()
        if overlay.get("editing") and isinstance(key, str) and key.startswith("text:"):
            questions = question_items(overlay.get("detail"))
            question_id = overlay.get("editing_question_id") or (
                questions[0]["id"] if questions else None)
            text = _question_text_draft(overlay, question_id)
            addition = key[5:]
            if question_id and addition and len(text) + len(addition) <= 16000:
                return _question_set_answer(state, question_id, text + addition)
            return state, ()
        if key in ("enter", "\n"):
            if overlay.get("editing"):
                if question_answer_supported(overlay.get("detail")):
                    return _question_submit(state)
                overlay = dict(overlay)
                overlay.update(editing=False,
                               status="Private draft saved; use the native prompt to respond.")
                return replace(state, question_overlay=overlay), ()
            if (_question_draft_response(overlay) is not None
                    and isinstance(overlay.get("detail"), Mapping)
                    and overlay["detail"].get("state") == "open"):
                return _question_submit(state)
            if selected:
                ref = next((item for item in refs if item.get("id") == selected), None)
                if ref:
                    return _question_selection(state, ref)
            if refs:
                return _question_selection(state, refs[0])
    if state.view == "MODELS" and state.scope == "-":
        found = _models_find_hit(frame)
        if found is not None and models_finding(state):
            handled = _models_find_key(state, key, found)
            if handled is not None:
                return handled, ()
        elif found is not None and key in ("e", "E"):
            return _reduce(state, {"type": "click", "action": found.action}, frame)
    if key in ("tab", "shift-tab", "left", "right"):
        if state.scope != "-":
            return replace(state, scope="-", opened=""), ()
        step = -1 if key in ("shift-tab", "left") else 1
        tabs = tuple(str(item) for item in event.get("tabs", VIEWS))
        if not tabs:
            return state, ()
        index = tabs.index(state.view) if state.view in tabs else 0
        view = tabs[(index + step) % len(tabs)]
        return replace(state, view=view, scope="-", opened="", title=view), ()
    if key in ("escape", "esc"):
        if state.drag:
            return replace(state, drag=()), ()
        if state.scope != "-":
            parent = event.get("parent")
            if parent is None:
                parent_hit = next((hit for hit in getattr(frame, "hits", ())
                                   if hit.y == -1 and hit.action[:1] == ("parent",)), None)
                parent = parent_hit.action[1] if parent_hit else (
                    state.parents[-1] if state.parents else "-")
            if parent not in (None, "-") and ":" not in str(parent):
                parent = "session:" + str(parent)
            parents = state.parents[:-1] if state.parents else ()
            return replace(state, scope=str(parent), opened="", parents=parents,
                           title=(str(parent).split(":", 1)[-1] if parent != "-" else state.view)), ()
        return state, ()
    if key in ("backspace", "delete"):
        return reduce(state, {"type": "key", "key": "escape"}, frame)
    if key in ("pageup", "pagedown", "home", "end"):
        region = _scroll_region(frame, "pane_scroll")
        if len(region) < 7:
            return state, ()
        _x0, _y0, _x1, _y1, painted, maximum, page = map(int, region[:7])
        offsets = dict(state.scroll)
        current = int(offsets.get("pane", painted))
        if key == "home":
            current = 0
        elif key == "end":
            current = int(event.get("max_scroll", maximum))
        elif key == "pageup":
            current = max(0, current - max(1, page))
        else:
            current = min(maximum, current + max(1, page))
        offsets["pane"] = current
        return replace(state, scroll=tuple(sorted(offsets.items()))), ()
    if key in ("up", "down"):
        order = tuple(str(item) for item in event.get("order", ()))
        if not order:
            return state, ()
        step = -1 if key == "up" else 1
        index = order.index(state.scope) + step if state.scope in order else (0 if step > 0 else -1)
        scope = order[index % len(order)]
        offsets = dict(state.scroll)
        tree = _scroll_region(frame, "tree_scroll")
        if len(tree) >= 9 and isinstance(tree[8], Mapping):
            row_index = tree[8].get(scope)
            if row_index is None and ":" not in scope:
                row_index = tree[8].get("session:" + scope)
            if row_index is not None:
                _x0, top, _x1, _bottom, painted, maximum, total, visible = map(int, tree[:8])
                offset = int(offsets.get("tree", painted))
                row_index = int(row_index)
                if row_index < offset:
                    offset = row_index
                elif visible and row_index >= offset + visible:
                    offset = row_index - visible + 1
                offset = max(0, min(maximum, offset))
                if offset or "tree" in offsets:
                    offsets["tree"] = offset
        return replace(state, scope=scope, opened="", title=scope.partition(":")[2] or scope,
                       scroll=tuple(sorted(offsets.items()))), ()
    if key in ("u", "usage"):
        return replace(state, usage_open=not state.usage_open), ()
    if key in ("/", "F"):
        return state, (("command", "filter" if key == "/" else "feed"),)
    if key in ("enter", "\n"):
        hit = event.get("hit")
        action = hit.action if hasattr(hit, "action") else hit
        if state.scope == "-" and not action:
            return state, ()
        if state.scope != "-":
            tag = state.scope.partition(":")[2]
            pane_hit = next((item for item in getattr(frame, "hits", ())
                             if item.action[:1] == ("pane",) and
                             len(item.action) > 2 and str(item.action[1]) == tag), None)
            pane = pane_hit.action[2] if pane_hit else ""
            return state, (("focus", tag, pane),)
        return reduce(state, {"type": "click", "action": action}, frame)
    return state, ()


def serialize(state):
    """Persist only explorer view state; old B cursors are ignored on read."""
    state = NavState.from_value(state)
    return {"schema_version": 3, "view": state.view, "scope": state.scope,
            "opened": state.opened, "usage_open": state.usage_open,
            "scroll": [list(item) for item in state.scroll], "title": state.title,
            "status": state.status, "parents": list(state.parents),
            "folds": [list(item) for item in state.folds],
            "frame_layout": {"schema_version": 3}}


def restore(value=None):
    return NavState.from_value(value)
