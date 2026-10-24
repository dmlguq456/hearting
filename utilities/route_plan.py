"""The framed route's decision record (`route_decision_v1`) and the framed-shape predicate.

A framed route (capability `route-frame`, shape `framed`) ends in one runtime terminal,
`route-decision`, that no model runs. The runtime fixes one record as that terminal's
evidence: an immutable `decision` part with its canonical digest, and a separate,
monotonic `first_leg` part that may be added once and never changed. The digest covers
the `decision` part only, so binding a first leg never invalidates the record.

It also reads a frame brief's section 8 (`route_proposal_v1`), validates the proposal
through a caller-supplied memory compile, reads a `--route-plan <record>#<index>` argument,
and projects `next_leg`. This module has no CLI, no gate and no recovery command; it only
builds, renders, reads and checks, and none of its functions write a file.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import stat
import sys
import textwrap
from pathlib import Path

SCHEMA = "route_decision_v1"
CAPABILITY = "route-frame"
SHAPE = "framed"
NODE_IDS = ("frame", "frame-alternative", "route-decision")
# A new framed route runs both legs by default. Explicit direct/quick may narrow to one
# deep leg when the request has no protected path, multi-adapter paths, or spec/ship hint.
# A recipe's own frame pair is always both legs.
ONE_LEG_NODE_IDS = ("frame", "route-decision")
FRAME_PAIR = ("frame", "frame-alternative")
_FRAME_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:\./|/|`)?(?:core|capabilities|roles|spec)/[^\s`'\"<>),;]+"
    r"|(?<![A-Za-z0-9_.-])(?:\./|~/|/|`)?(?:[A-Za-z0-9_.-]+/)*prd\.md(?![A-Za-z0-9_.-])",
    re.I,
)
_ADAPTER_PATH = re.compile(r"(?<![A-Za-z0-9_.-])adapters/(claude|codex|opencode)(?=/|\b)")
TERMINAL_NODE = "route-decision"
RECORD_RELATIVE = "shards/frame/route-decision.json"
MAX_RECORD_BYTES = 262144
NONE = "none"
NO_PROPOSAL_READ = "proposal-not-read"
_DECISION_KEYS = frozenset((
    "frame_route", "selected", "reason", "proposal", "proposals", "briefs", "intent",
    "approvals", "first_leg_compose"))
_FRAME_NODES = ("frame", "frame-alternative")
ROOT = Path(__file__).resolve().parents[1]

PROPOSAL_SCHEMA = "route_proposal_v1"
MAX_BRIEF_BYTES = 262144
MAX_SECTION_BYTES = 16384
MAX_LEGS = 4
MAX_APPROVALS = 4
MAX_TEXT = 300
MAX_YAML_EVENTS = 600
SHAPES = ("direct", "solo", "staged")
APPROVAL_KEYS = ("full-run", "deploy", "handback", "preview")
VALID = "valid"
_SLUG = re.compile(r"[a-z][a-z0-9-]{0,31}")
_CAPABILITY = re.compile(r"[a-z][a-z0-9-]{0,63}")
_MODE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_GRAPH_TOKEN = re.compile(r"[a-z][a-z0-9-]*(?::[a-z0-9][a-z0-9/_.-]*){0,2}")
_SECTION_HEAD = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:\*\*)?[ \t]*8[ \t]*[.):]?[ \t]*(?:\*\*)?[^\n]*경로 조립 제안", re.M)
# Section 8 under a title in any language: a heading or a numbered line that starts with 8.
_SECTION_NUMBER = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]*(?:\*\*)?[ \t]*8(?![0-9])|(?:\*\*)?[ \t]*8[ \t]*[.):])[^\n]*", re.M)
_NEXT_SECTION = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:\*\*)?[ \t]*(?:9|1[0-9])[ \t]*[.):]", re.M)
# The same two as markdown headings only: a numbered list line `8.` or `9.` is not a section.
_HEADING_NUMBER = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*(?:\*\*)?[ \t]*8(?![0-9])[^\n]*", re.M)
_NEXT_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*(?:\*\*)?[ \t]*(?:9|1[0-9])[ \t]*[.):]", re.M)
_NUMBERED_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]*(?:\*\*)?[ \t]*[0-9]+(?![0-9])[^\n]*", re.M)
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([A-Za-z0-9_-]*)[ \t]*$")


def is_framed_route(route) -> bool:
    """The exact framed route shape: nothing else may take the model-less terminal path."""
    if not isinstance(route, dict) or route.get("capability") != CAPABILITY:
        return False
    if (route.get("selection") or {}).get("shape") != SHAPE or route.get("effective_intensity") != "standard":
        return False
    nodes = route.get("nodes")
    return (isinstance(nodes, list) and tuple(n.get("id") for n in nodes) in (NODE_IDS, ONE_LEG_NODE_IDS)
            and nodes[-1].get("kind") == "runtime-terminal" and nodes[-1].get("terminal") is True)


def frame_legs(route) -> tuple:
    """The frame legs a route declares, in order: both legs of a pair, or the one leg of a framed
    route that runs one."""
    ids = {n.get("id") for n in (route or {}).get("nodes") or [] if isinstance(n, dict)
           and n.get("worker_type") == "frame" and n.get("dispatch_depth") == 1}
    return tuple(node for node in FRAME_PAIR if node in ids)


def valid_frame_legs(route, legs) -> bool:
    """Whether `legs` is a frame set this route may have: the pair, or `frame` alone on a framed route."""
    legs = set(legs)
    return legs == set(FRAME_PAIR) or (legs == {"frame"} and is_framed_route(route))


def frame_leg_decision(given_intensity, request_text="", routing_hints=None):
    """Return the sealed frame width, its source, and a stable human-readable reason."""
    if given_intensity is None:
        return 2, "default", "기본"
    if given_intensity not in {"direct", "quick", "standard", "strong", "thorough", "adversarial"}:
        raise ValueError("invalid intensity")
    if given_intensity not in {"direct", "quick"}:
        return 2, "explicit", f"--intensity {given_intensity} 표시"
    text = str(request_text or "")
    match = _FRAME_PATH.search(text)
    if match:
        reason = re.sub(r"[`\n\r]+", "", match.group(0))[:72]
        return 2, "automatic", f"자동 지킴: --intensity {given_intensity}이지만 과제에 {reason}"
    adapters = list(dict.fromkeys(_ADAPTER_PATH.findall(text)))
    if len(adapters) >= 2:
        return 2, "automatic", f"자동 지킴: --intensity {given_intensity}이지만 과제에 어댑터 둘 이상"
    hints = routing_hints or {}
    if hints.get("capability") in {"autopilot-spec", "autopilot-ship"}:
        return 2, "automatic", f"자동 지킴: --intensity {given_intensity}이지만 과제에 {hints['capability']} 작업"
    return 1, "explicit", f"간단 표시 --intensity {given_intensity}"


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decision_digest(decision) -> str:
    """`sha256:<hex>` of the decision part's canonical bytes; `first_leg` is never part of it."""
    return "sha256:" + hashlib.sha256(_canonical(decision)).hexdigest()


def file_digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_decision(*, frame_route, selected, reason, briefs, intent, proposal=None, proposals=None,
                   approvals=None, first_leg_compose=None) -> dict:
    """The immutable `decision` part. `selected` is an option label or `none`."""
    if not isinstance(selected, str) or not selected:
        raise ValueError("route-decision-invalid:selected")
    if not isinstance(reason, str) or (selected == NONE and not reason):
        raise ValueError("route-decision-invalid:reason")
    return {
        "frame_route": dict(frame_route),
        "selected": selected,
        "reason": reason,
        "proposal": proposal,
        "proposals": proposals if proposals is not None else [],
        "briefs": [dict(item) for item in briefs],
        "intent": dict(intent),
        "approvals": approvals if approvals is not None else {},
        "first_leg_compose": first_leg_compose,
    }


def none_decision(*, frame_route, briefs, intent, reason=NO_PROPOSAL_READ, legs=_FRAME_NODES) -> dict:
    """The minimal ending: no proposal was selected, so no leg starts and the main session composes next."""
    return build_decision(
        frame_route=frame_route, selected=NONE, reason=reason, briefs=briefs, intent=intent,
        proposals=[{"node": node, "proposal": None, "reason": reason} for node in legs])


def build_record(decision, first_leg=None) -> dict:
    record = {"schema": SCHEMA, "decision": decision, "digest": decision_digest(decision)}
    if first_leg is not None:
        record["first_leg"] = first_leg
    return record


def render(record) -> bytes:
    """The record's exact file bytes; the same record always renders the same bytes."""
    return (json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def validate_record(record) -> dict:
    """Return `record` when it is a well-formed `route_decision_v1`; else raise ValueError."""
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ValueError("route-decision-invalid:schema")
    if set(record) - {"schema", "decision", "digest", "first_leg"} or {"decision", "digest"} - set(record):
        raise ValueError("route-decision-invalid:fields")
    decision = record["decision"]
    if not isinstance(decision, dict) or set(decision) != _DECISION_KEYS:
        raise ValueError("route-decision-invalid:decision")
    if record["digest"] != decision_digest(decision):
        raise ValueError("route-decision-invalid:digest")
    frame_route = decision["frame_route"]
    if not isinstance(frame_route, dict) or set(frame_route) != {"route_id", "route_hash", "cycle_id"}:
        raise ValueError("route-decision-invalid:frame_route")
    if not isinstance(decision["selected"], str) or not decision["selected"]:
        raise ValueError("route-decision-invalid:selected")
    if decision["selected"] == NONE and (not decision["reason"] or decision["proposal"] is not None):
        raise ValueError("route-decision-invalid:none")
    if decision["selected"] != NONE and (not isinstance(decision["proposal"], dict)
                                         or not isinstance(decision["first_leg_compose"], dict)):
        raise ValueError("route-decision-invalid:selected")
    if "first_leg" in record and (decision["selected"] == NONE or not isinstance(record["first_leg"], dict)):
        raise ValueError("route-decision-invalid:first_leg")
    return record


def read_record(path) -> dict:
    """Read and validate a record file; it must be a small regular, non-symlink file."""
    path = Path(path)
    try:
        meta = os.lstat(path)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_RECORD_BYTES:
            raise ValueError("route-decision-invalid:file")
        return validate_record(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("route-decision-invalid:unreadable") from exc


def bind_first_leg(record, first_leg) -> dict:
    """Add the `first_leg` part once, or confirm the same value again; any other value conflicts.

    The decision part and its digest are never touched. S2 fills `first_leg`; a `none`
    decision has no first leg.
    """
    validate_record(record)
    if record["decision"]["selected"] == NONE:
        raise ValueError("route-decision-invalid:first_leg")
    current = record.get("first_leg")
    if current is None:
        return {**record, "first_leg": first_leg}
    merged = {**current, **first_leg}
    if any(current.get(key) != value for key, value in first_leg.items() if key in current):
        raise ValueError("route-decision-conflict:first_leg")
    return {**record, "first_leg": merged}



# --- Section 8: the route proposal ------------------------------------------------------

class ProposalError(ValueError):
    """A reason the brief holds no usable proposal; the caller shows `proposal:none(<reason>)`."""


def none_text(reason) -> str:
    return f"proposal:none({reason})"


def _section8(text: str):
    """Find section 8, preferring a Markdown heading over a title or plain number.

    Alternate endings tolerate numbered lists inside that section, but cannot
    cross another section heading to borrow its proposal. Return None when no
    section 8 header is present.
    """
    heads = []
    for pattern in (_HEADING_NUMBER, _SECTION_HEAD, _SECTION_NUMBER):
        heads = list(pattern.finditer(text))
        if heads:
            break
    readings = []
    for head in heads:
        rest = text[head.end():]
        heading = re.match(r"^[ \t]{0,3}(#{1,6})", head.group())
        boundary = next((match for match in _NUMBERED_HEADING.finditer(rest)
                         if heading is None or len(match[1]) <= len(heading[1])), None)
        if boundary:
            rest = rest[:boundary.start()]
        for ending in (_NEXT_SECTION, _NEXT_HEADING):
            tail = ending.search(rest)
            readings.append(rest[:tail.start()] if tail else rest)
    if not readings:
        return None
    section = next((reading for reading in readings if _proposal_blocks(reading)), readings[0])
    if len(section.encode("utf-8")) > MAX_SECTION_BYTES:
        raise ProposalError("section-too-large")
    return section


def _proposal_blocks(section: str) -> list:
    blocks, marker, info, buffer = [], None, "", []
    for line in section.splitlines():
        fence = _FENCE.match(line)
        if marker is None:
            if fence:
                marker, info, buffer = fence.group(1), fence.group(2).lower(), []
            continue
        if fence and not fence.group(2) and fence.group(1)[0] == marker[0] and len(fence.group(1)) >= len(marker):
            if info in ("yaml", "yml") and PROPOSAL_SCHEMA in "\n".join(buffer):
                blocks.append("\n".join(buffer))
            marker = None
            continue
        buffer.append(line)
    return blocks


def yaml_available() -> bool:
    """Whether this Python can read a proposal block at all: `_load_yaml` needs PyYAML."""
    return importlib.util.find_spec("yaml") is not None


def _load_yaml(text: str):
    try:
        import yaml
    except ImportError as exc:
        raise ProposalError("yaml-unavailable") from exc

    class Strict(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=True)
            if key in seen:
                raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        count = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            count += 1
            if count > MAX_YAML_EVENTS:
                raise ProposalError("yaml-too-large")
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise ProposalError("yaml-alias")
            tag = getattr(event, "tag", None)
            if tag and not tag.startswith("tag:yaml.org,2002:"):
                raise ProposalError("yaml-tag")
        return yaml.load(text, Loader=Strict)
    except yaml.YAMLError as exc:
        raise ProposalError("yaml-invalid") from exc


def _text(value, *, name, required=False):
    if value is None or value == "":
        if required:
            raise ProposalError(f"schema-invalid:{name}")
        return None
    if not isinstance(value, str) or len(value) > MAX_TEXT or not value.strip():
        raise ProposalError(f"schema-invalid:{name}")
    return value.strip()


_LEG_FIELDS = frozenset({"capability", "mode", "shape", "graph", "intensity", "cwd", "why",
                         "done_when", "verify", "hands_over", "parallel", "extra_stages"})
MAX_DONE_WHEN = 5
MAX_PLAN_NOTES = 8


def _plan_fields(raw, index, notes):
    """The leg's optional plan fields, normalized; a malformed one is left out and named in `notes`.

    `done_when`: 1-5 checks that the leg is finished, each a sentence or `{text, check}` (`check` is a
    command), sealed with stable ids `d1`, `d2`... in the order written. `verify`: one line saying
    what verification looks at. `hands_over`: what the next leg reads. `parallel`: work the owner may
    split through the plan's slices. `extra_stages`: `{id, unit, after, verify}` stages the compiler
    adds as `plan-<id>` nodes (`compose_subgraph_recipe`)."""
    fields = {}

    def bad(name):
        if notes is not None:
            notes.append(f"ignored:legs[{index}].{name}")

    def line(value, limit=MAX_TEXT):
        return value.strip() if isinstance(value, str) and value.strip() and len(value) <= limit else None
    if "done_when" in raw:
        items, rows = raw["done_when"], []
        if isinstance(items, (str, dict)):
            items = [items]
        if isinstance(items, list) and 1 <= len(items) <= MAX_DONE_WHEN:
            for item in items:
                text = line(item) if isinstance(item, str) else line(item.get("text")) if isinstance(item, dict) else None
                check = line(item.get("check")) if isinstance(item, dict) and item.get("check") is not None else None
                if text is None or (isinstance(item, dict) and item.get("check") is not None and check is None):
                    rows = None
                    break
                rows.append({"id": f"d{len(rows) + 1}", "text": text, **({"check": check} if check else {})})
        if rows:
            fields["done_when"] = rows
        else:
            bad("done_when")
    if "verify" in raw:
        if line(raw["verify"]):
            fields["verify"] = line(raw["verify"])
        else:
            bad("verify")
    for name in ("hands_over", "parallel"):
        if name in raw:
            values = raw[name] if isinstance(raw[name], list) else [raw[name]]
            kept = [line(v, 200) for v in values]
            if values and len(values) <= MAX_PLAN_NOTES and all(kept):
                fields[name] = kept
            else:
                bad(name)
    if "extra_stages" in raw:
        values, kept = raw["extra_stages"], []
        for value in values if isinstance(values, list) else []:
            row = {key: line(value.get(key), 200) for key in ("id", "unit", "after", "verify")
                   if isinstance(value, dict) and value.get(key) is not None}
            if not isinstance(value, dict) or not row.get("id") or len(row) != len(
                    [k for k in ("id", "unit", "after", "verify") if value.get(k) is not None]):
                kept = None
                break
            kept.append(row)
        if kept and len(kept) <= MAX_PLAN_NOTES:
            fields["extra_stages"] = kept
        else:
            bad("extra_stages")
    return fields
_DOCUMENT_FIELDS = frozenset({"summary", "legs", "entry_approvals", "execution_scope"})


def _normal_leg(raw, index, notes=None):
    if not isinstance(raw, dict):
        raise ProposalError(f"schema-invalid:legs[{index}]")
    if set(raw) - _LEG_FIELDS and notes is not None:
        notes.append(f"ignored:legs[{index}]:" + ",".join(sorted(map(str, set(raw) - _LEG_FIELDS))))
    capability, shape = raw.get("capability"), raw.get("shape")
    if not isinstance(capability, str) or not _CAPABILITY.fullmatch(capability):
        raise ProposalError(f"schema-invalid:legs[{index}].capability")
    if shape not in SHAPES:
        raise ProposalError(f"schema-invalid:legs[{index}].shape")
    mode, intensity = raw.get("mode"), raw.get("intensity")
    if mode is not None and (not isinstance(mode, str) or not _MODE.fullmatch(mode)):
        raise ProposalError(f"schema-invalid:legs[{index}].mode")
    if intensity is not None and (not isinstance(intensity, str) or not _MODE.fullmatch(intensity)):
        raise ProposalError(f"schema-invalid:legs[{index}].intensity")
    graph = raw.get("graph")
    if graph is not None:
        if shape != "staged":
            raise ProposalError(f"schema-invalid:legs[{index}].graph-only-staged")
        if not isinstance(graph, list) or not 1 <= len(graph) <= 24:
            raise ProposalError(f"schema-invalid:legs[{index}].graph")
        for token in graph:
            if not isinstance(token, str) or not _GRAPH_TOKEN.fullmatch(token):
                raise ProposalError(f"schema-invalid:legs[{index}].graph")
            if set(token.split(":")[:2]) & set(_FRAME_NODES):
                raise ProposalError(f"leg-invalid:{index}:frame-stage-not-allowed")
    return {"capability": capability, "mode": mode, "shape": shape, "graph": list(graph) if graph else None,
            "intensity": intensity, "why": _text(raw.get("why"), name=f"legs[{index}].why"),
            **({"cwd": _text(raw["cwd"], name=f"legs[{index}].cwd", required=True)} if "cwd" in raw else {}),
            **_plan_fields(raw, index, notes)}


def _question_id(original, row, used) -> str:
    """A `_SLUG` id for an approval question the brief wrote in some other shape: lower-cased, runs of
    other characters become `-`, a leading digit gets `q-`, 32 characters at most, `<key>-leg<leg>` when
    nothing is left (e.g. a Korean id), and `-2`, `-3`... when the proposal already used it
    (an id that is already a slug is kept as written, even when two rows share it)."""
    base = re.sub(r"[^a-z0-9]+", "-", original.lower()).strip("-")
    if base and base[0].isdigit():
        base = "q-" + base
    base = base[:32].rstrip("-") or f"{row['key']}-leg{row['leg']}"
    candidate, count = base, 1
    while candidate in used:
        count += 1
        suffix = f"-{count}"
        candidate = base[:32 - len(suffix)].rstrip("-") + suffix
    return candidate


def _approval_rows(approvals, leg_count) -> tuple:
    """`(rows, renames)` of a proposal's `entry_approvals`. A string `question` that is not a slug is
    normalized, never rejected; `renames` is `[{leg, key, question, original}]` for those rows."""
    rows, renames = [], []
    used = {raw["question"] for raw in approvals
            if isinstance(raw, dict) and isinstance(raw.get("question"), str) and _SLUG.fullmatch(raw["question"])}
    for raw in approvals:
        if (not isinstance(raw, dict) or set(raw) != {"key", "leg", "question"}
                or raw["key"] not in APPROVAL_KEYS or isinstance(raw["leg"], bool)
                or not isinstance(raw["leg"], int) or not 0 <= raw["leg"] < leg_count
                or not isinstance(raw["question"], str)):
            raise ProposalError("schema-invalid:entry_approvals")
        row = {"key": raw["key"], "leg": raw["leg"], "question": raw["question"]}
        if not _SLUG.fullmatch(raw["question"]):
            row["question"] = _question_id(raw["question"], row, used)
            used.add(row["question"])
            renames.append({**row, "original": raw["question"]})
        rows.append(row)
    return rows, renames


def question_renames(source) -> list:
    """The approval question ids `parse_proposal_with_source` normalized in a brief's proposal block
    (`[{leg, key, question, original}]`, display only); `[]` when none or the block does not parse."""
    try:
        body = _load_yaml(source)[PROPOSAL_SCHEMA]
        return _approval_rows(body.get("entry_approvals") or [], len(body["legs"]))[1]
    except (ProposalError, AttributeError, KeyError, TypeError, StopIteration):
        return []


def parse_proposal(text: str) -> dict:
    """The one `route_proposal_v1` of a brief's section 8, normalized; ProposalError when none."""
    return parse_proposal_with_source(text)[0]


def parse_proposal_with_source(text: str) -> tuple:
    """`(proposal, source)`: the normalized proposal and the fenced block's own text."""
    return parse_proposal_with_notes(text)[:2]


def _readable_block(blocks):
    """`(source, document, index)` of the first block whose YAML holds a `route_proposal_v1`
    mapping; the first block's own reason when none does."""
    first = None
    for index, block in enumerate(blocks):
        source = textwrap.dedent(block)
        try:
            document = _load_yaml(source)
        except ProposalError as exc:
            first = first or exc
            continue
        if isinstance(document, dict) and isinstance(document.get(PROPOSAL_SCHEMA), dict):
            return source, document, index
        first = first or ProposalError("schema-invalid:document")
    raise first


def parse_proposal_with_notes(text: str) -> tuple:
    """`(proposal, source, notes)`. What a brief wrote around its proposal is read, not refused:
    section 8 under a title in any language (the whole brief when it has no section 8 heading),
    the first readable block when there are several, and unknown fields ignored. `notes` says
    each time that happened, for the person who reads the review."""
    if len(text.encode("utf-8")) > MAX_BRIEF_BYTES:
        raise ProposalError("brief-too-large")
    notes = []
    section = _section8(text)
    if section is None:
        blocks = _proposal_blocks(text)
        if not blocks:
            raise ProposalError("section-missing")
        notes.append("section-8-heading-missing:read-whole-brief")
    else:
        blocks = _proposal_blocks(section)
        if not blocks:
            raise ProposalError("block-missing")
    source, document, index = _readable_block(blocks)
    if len(blocks) > 1:
        notes.append(f"blocks:{len(blocks)}:read-{index + 1}")
    if set(document) - {PROPOSAL_SCHEMA}:
        notes.append("ignored:" + ",".join(sorted(map(str, set(document) - {PROPOSAL_SCHEMA}))))
    body = document[PROPOSAL_SCHEMA]
    if set(body) - _DOCUMENT_FIELDS:
        notes.append("ignored:" + ",".join(sorted(map(str, set(body) - _DOCUMENT_FIELDS))))
    scope = body.get("execution_scope", "complete")
    if scope not in ("complete", "report"):
        raise ProposalError("schema-invalid:document")
    legs = body.get("legs")
    if not isinstance(legs, list) or not 1 <= len(legs) <= MAX_LEGS:
        raise ProposalError("schema-invalid:legs")
    approvals = body.get("entry_approvals") or []
    if not isinstance(approvals, list) or len(approvals) > MAX_APPROVALS:
        raise ProposalError("schema-invalid:entry_approvals")
    rows = _approval_rows(approvals, len(legs))[0]
    return ({"summary": _text(body.get("summary"), name="summary", required=True),
             "legs": [_normal_leg(raw, index, notes) for index, raw in enumerate(legs)],
             "entry_approvals": rows, "execution_scope": scope}, source, notes)


def leg_arguments(leg) -> dict:
    """The compose-shaped keyword arguments of one normalized proposal leg."""
    return {"capability": leg["capability"], "capability_mode": leg.get("mode"), "shape": leg["shape"],
            "graph": ",".join(leg["graph"]) if leg.get("graph") else None, "intensity": leg.get("intensity"),
            **({"cwd": leg["cwd"]} if leg.get("cwd") else {})}


def leg_cwd(leg, default_cwd, *, base_cwd=None) -> str:
    """A leg's chosen folder, relative to the original frame; otherwise inherit the current folder."""
    if not leg.get("cwd"):
        return str(default_cwd)
    path = Path(leg["cwd"]).expanduser()
    return str((path if path.is_absolute() else Path(base_cwd or default_cwd) / path).resolve())


def validate_proposal(proposal, *, compile_leg, start_approvals) -> dict:
    """Compile every leg through `compile_leg(leg, index) -> route` (a memory compile that writes,
    starts and records nothing) and reconcile `entry_approvals` with the parts the legs really carry.

    Returns `{"legs": [facts], "start_approvals": [{leg, key, part, node}], "entry_approvals": [rows],
    "notes": [...]}`. An invalid first leg raises ProposalError, since nothing could start. An invalid
    later leg ends the proposal before it (`leg-invalid:<i>:<reason>` in `notes`); the legs before it
    stay valid. An approval row for a part its leg does not carry, or for a leg that was cut, is
    left out and named in `notes`, as is an extra stage the compiler could not place
    (`extra-stage-not-compiled:<id>`).
    """
    scope = proposal.get("execution_scope", "complete")
    if scope not in ("complete", "report"):
        raise ProposalError("schema-invalid:document")
    facts, approvals, notes = [], [], []
    for index, leg in enumerate(proposal["legs"]):
        try:
            route = compile_leg(leg, index)
        except ValueError as exc:
            reason = f"leg-invalid:{index}:{str(exc).strip().splitlines()[0][:120] if str(exc).strip() else 'rejected'}"
            if index == 0:
                raise ProposalError(reason) from exc
            notes.append(reason)
            break
        composed = (route.get("composed_recipe") or {}).get("compose") or {}
        placed = {stage.get("id") for stage in composed.get("extra_stages") or ()}
        notes.extend(f"extra-stage-not-compiled:{stage['id']}" for stage in leg.get("extra_stages") or ()
                     if stage["id"] not in placed)
        facts.append({"capability": route["capability"], "mode": route["capability_mode"],
                      "shape": (route.get("selection") or {}).get("shape"),
                      "graph": list(composed.get("graph") or []) or None,
                      "intensity": route["effective_intensity"],
                      **({"cwd": route["cwd"]} if leg.get("cwd") else {})})
        approvals.extend({"leg": index, **row} for row in start_approvals(route))
    kept = []
    for row in proposal.get("entry_approvals", []):
        if any(item["leg"] == row["leg"] and item["start_approval"] == row["key"] for item in approvals):
            kept.append(row)
        else:
            notes.append(f"entry-approval-mismatch:{row['key']}@{row['leg']}")
    return {"legs": facts, "start_approvals": approvals, "execution_scope": scope,
            "entry_approvals": kept, "notes": notes}


def evaluate_brief(path, *, root, node, compile_leg, start_approvals) -> dict:
    """One brief's proposal row: `{node, proposal|None, reason, brief_path, sha256, facts?}`.

    `reason` is `valid` or the `proposal:none(<reason>)` reason. A missing or unreadable
    brief is none too; nothing here raises.
    """
    path = Path(path)
    row = {"node": node, "proposal": None, "reason": "", "brief_path": _relative(path, root), "sha256": ""}
    try:
        raw = path.read_bytes()
        row["sha256"] = hashlib.sha256(raw).hexdigest()
        if len(raw) > MAX_BRIEF_BYTES:
            raise ProposalError("brief-too-large")
        proposal, source, notes = parse_proposal_with_notes(raw.decode("utf-8"))
        facts = validate_proposal(proposal, compile_leg=compile_leg, start_approvals=start_approvals)
    except ProposalError as exc:
        row["reason"] = str(exc)
    except (OSError, UnicodeError):
        row["reason"] = "brief-unreadable"
    else:
        # The proposal is what can run: the legs that compiled and the approval rows that match them.
        proposal = {**proposal, "legs": proposal["legs"][:len(facts["legs"])],
                    "entry_approvals": facts["entry_approvals"]}
        row.update(proposal=proposal, reason=VALID, facts=facts, source=source)
        if notes + facts["notes"]:
            row["read_notes"] = notes + facts["notes"]
    return row


def _relative(path, root) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def scope_key(row) -> list:
    """The approval scope a valid proposal row carries, in a comparable form."""
    return sorted((item["leg"], item["start_approval"], item["part"]) for item in row["facts"]["start_approvals"])


def proposals_equal(rows) -> bool:
    """Plan decision D5: two valid proposals are equal when their normalized legs and their
    start-approval scope match. Summary and `why` wording is not part of it."""
    if len(rows) != 2 or any(row["proposal"] is None for row in rows):
        return False
    first, second = rows
    return first["facts"]["legs"] == second["facts"]["legs"] and scope_key(first) == scope_key(second)


def wording_differs(rows) -> bool:
    if len(rows) != 2 or any(row["proposal"] is None for row in rows):
        return False
    first, second = (row["proposal"] for row in rows)
    return (first["summary"] != second["summary"]
            or [leg["why"] for leg in first["legs"]] != [leg["why"] for leg in second["legs"]])


_LEG_KEYS = ("capability", "mode", "shape", "graph", "intensity", "cwd")


def same_proposal(left, right, resolved=None) -> bool:
    """Whether `right` (an interview's copy) names the legs of `left` (a proposal the runtime validated):
    each leg's (capability, mode, shape, graph, intensity) is, taken together, the proposal's own
    leg or, given `resolved` (that proposal's compiled legs, `facts["legs"]`, shown as `legs` in
    route_proposal_review), the leg its compile resolved it to -- never a per-key blend of the
    two. The copy's approval questions are the interview's own and are not compared."""
    try:
        own, copy = left["legs"], right["legs"]
        shown = own if resolved is None else resolved
        key_values = lambda leg: [leg.get(key) for key in _LEG_KEYS]
        return (len(own) == len(copy) == len(shown)
                and all(key_values(leg) in (key_values(mine), key_values(compiled))
                        for leg, mine, compiled in zip(copy, own, shown)))
    except (AttributeError, KeyError, TypeError):
        return False


# --- `--route-plan <record>#<index>` ----------------------------------------------------

def parse_route_plan_arg(text) -> tuple:
    path, separator, index = str(text or "").rpartition("#")
    if not separator or not path or not index.isdigit() or len(index) > 2:
        raise ValueError("route-plan-invalid:argument")
    return path, int(index)


def read_route_plan(text, artifact_root) -> dict:
    """Read a `<record>#<index>` argument against `artifact_root`.

    Returns `{decision, digest, index, leg, legs, record}`; raises ValueError when the record is
    unreadable, is not a selected decision, lies outside the root, or `index` is out of range.
    The caller seals only `decision`, `digest` and `index`.
    """
    arg_path, index = parse_route_plan_arg(text)
    root = Path(artifact_root).resolve()
    path = Path(arg_path)
    path = (path if path.is_absolute() else root / path)
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("route-plan-invalid:path")
    record = read_record(path.resolve())
    decision = record["decision"]
    proposal = decision["proposal"]
    if decision["selected"] == NONE or not isinstance(proposal, dict):
        raise ValueError("route-plan-invalid:none")
    legs = proposal.get("legs")
    if not isinstance(legs, list) or not 0 <= index < len(legs):
        raise ValueError("route-plan-invalid:index")
    return {"decision": path.resolve().relative_to(root).as_posix(), "digest": record["digest"],
            "index": index, "leg": legs[index], "legs": legs, "record": record}


def sealed_form(binding) -> dict:
    return {"decision": binding["decision"], "digest": binding["digest"], "index": binding["index"]}


def validate_sealed(value) -> dict:
    """The format of a route's own `route_plan` field, never the record it names."""
    if (not isinstance(value, dict) or set(value) != {"decision", "digest", "index"}
            or not isinstance(value["decision"], str) or not value["decision"] or value["decision"].startswith("/")
            or ".." in Path(value["decision"]).parts
            or not isinstance(value["digest"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["digest"])
            or isinstance(value["index"], bool) or not isinstance(value["index"], int)
            or not 0 <= value["index"] < MAX_LEGS):
        raise ValueError("route-plan-invalid:sealed")
    return value


def display_plan(legs) -> list:
    """Fleet's `--plan` display: the legs' capability order, adjacent repeats shown once."""
    names = []
    for leg in legs:
        name = leg["capability"]
        name = name[len("autopilot-"):] if name.startswith("autopilot-") else name
        if not names or names[-1] != name:
            names.append(name)
    return names


from route_authority import PIN_TARGETS as _PIN_TARGETS  # noqa: E402
from parent_next_directive import entrypoint  # noqa: E402


def pin_tokens(pins) -> list:
    """`target=harness[:model[@effort]]` for each pin of a sealed `selection_pins` map, in target order.

    What `compose --pin` reads back; `contract_version` and anything malformed are left out, so a
    route without pins gives `[]` and the printed command gains no token.
    """
    tokens = []
    for target in _PIN_TARGETS:
        pin = pins.get(target) if isinstance(pins, dict) else None
        if not isinstance(pin, dict) or not isinstance(pin.get("harness"), str):
            continue
        model, effort = pin.get("model"), pin.get("effort")
        token = f"{target}={pin['harness']}"
        if isinstance(model, str) and model:
            token += f":{model}" + (f"@{effort}" if isinstance(effort, str) and effort else "")
        tokens.append(token)
    return tokens


def frame_selection_pins(decision, artifact_root) -> dict:
    """The pins in force on the decision's verified frame route, or `{}`.

    Read from that frame's own route file and trusted only when its id and hash are the ones the
    decision recorded and its bytes still hash to them; nothing is written or rewritten.
    """
    try:
        import route_identity
        reference = decision["frame_route"]
        path = Path(artifact_root).resolve() / ".runtime" / "routes" / f"{reference['route_id']}.json"
        if path.is_symlink():
            return {}
        frame = json.loads(path.read_text(encoding="utf-8"))
        if ((frame.get("route_id"), frame.get("route_hash")) != (reference["route_id"], reference["route_hash"])
                or route_identity.route_hash(frame) != reference["route_hash"]):
            return {}
        from route_authority import selection_pin_rows
        return selection_pin_rows(frame)
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def compose_argv(leg, *, context, route_plan_arg, parent_cycle, slug, pins=None) -> list:
    """The `capability-route.py compose --start` argv for one leg: run as printed, it seals and starts."""
    argv = [sys.executable, entrypoint(ROOT, "utilities/capability-route.py"), "compose", "--start", "--slug", slug,
            "--shape", leg["shape"], "--capability", leg["capability"]]
    if leg.get("mode"):
        argv += ["--capability-mode", leg["mode"]]
    if leg.get("graph"):
        argv += ["--graph", ",".join(leg["graph"])]
    if leg.get("intensity"):
        argv += ["--intensity", leg["intensity"]]
    argv += ["--cwd", leg_cwd(leg, context["cwd"]), "--artifact-root", context["artifact_root"],
             "--prompt-file", context["prompt_file"]]
    if context.get("spec_read") and context["spec_read"] != "auto":
        argv += ["--spec-read", context["spec_read"]]
    argv += ["--route-plan", route_plan_arg]
    for token in pin_tokens(pins):
        argv += ["--pin", token]
    if context.get("campaign_key"):
        argv += ["--campaign-key", context["campaign_key"]]
    argv += ["--parent-cycle", parent_cycle]
    return argv


def project_next_leg(route, completed_cycle_id):
    """`{index, leg, compose_command}` for the leg after this route's, or None.

    Information only: it is never a launch and never enters `parent_next`, `parent_next_command`
    or `required_action`. None when the route carries no `route_plan`, the record is unreadable or
    no longer matches the sealed digest, the route was the last leg, or the reusable prompt file is gone.
    """
    try:
        sealed = validate_sealed(route.get("route_plan"))
        root = Path(route["artifact_root"]).resolve()
        binding = read_route_plan(f"{root / sealed['decision']}#{sealed['index']}", root)
        if binding["digest"] != sealed["digest"] or not completed_cycle_id:
            return None
        index = sealed["index"] + 1
        if index >= len(binding["legs"]):
            return None
        context = binding["record"]["decision"]["first_leg_compose"]["context"]
        if not Path(context["prompt_file"]).is_file():
            return None
        leg = binding["legs"][index]
        # The leg's pins in force; a leg sealed before pins reached legs takes its frame's.
        from route_authority import selection_pin_rows
        pins = selection_pin_rows(route) or frame_selection_pins(binding["record"]["decision"], root)
        argv = compose_argv(
            {**leg, "cwd": leg_cwd(leg, route["cwd"], base_cwd=context["cwd"])},
            context={**context, "campaign_key": route.get("campaign_key") or context.get("campaign_key")},
            route_plan_arg=f"{root / sealed['decision']}#{index}", parent_cycle=completed_cycle_id,
            slug=f"{context['slug']}-leg{index}", pins=pins)
        return {"index": index, "leg": leg, "compose_command": shlex.join(argv)}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def completed_cycle(route):
    """The sealed producer cycle this route ran in, or None.

    A continuation attaches to its source route's cycle, so the source chain is walked
    (bounded) through the canonical route files.
    """
    import artifact_producer as producer
    root = Path(route["artifact_root"]).resolve()
    ids, current = [route["route_id"]], route
    for _ in range(8):
        source = current.get("source_route_id")
        if not source or source in ids:
            break
        ids.append(source)
        try:
            current = json.loads((root / ".runtime" / "routes" / f"{source}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            break
    for record in producer.list_cycle_records(root):
        if record.get("route_id") in ids and record.get("state") == "sealed":
            return record
    return None


def latest_leg_route(route):
    """The furthest leg already started from the same decision record as `route`, or `route` itself.

    A started leg begins its cycle under the previous leg's cycle (`--parent-cycle`), so the walk
    follows cycle parents and accepts only a route that seals the same record and digest with the
    next index. Reads only; a leg that was composed but never started is not found.
    """
    try:
        import artifact_producer as producer
        sealed = validate_sealed(route.get("route_plan"))
        root = Path(route["artifact_root"]).resolve()
        records = producer.list_cycle_records(root)
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return route
    current = route
    for _ in range(MAX_LEGS):
        cycles = {rec.get("cycle_id") for rec in records if rec.get("route_id") == current.get("route_id")}
        index = current["route_plan"]["index"] + 1
        found = []
        for rec in records:
            if rec.get("parent_cycle_id") not in cycles or not rec.get("cycle_id"):
                continue
            try:
                candidate = json.loads((root / ".runtime" / "routes" / f"{rec.get('route_id')}.json")
                                       .read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if (isinstance(candidate, dict) and candidate.get("route_plan") == {**sealed, "index": index}
                    and candidate.get("route_id") == rec.get("route_id")):
                found.append((str(rec.get("started_on") or ""), candidate))
        if not found:
            return current
        current = max(found, key=lambda row: row[0])[1]
    return current


# --- The approved plan of one leg ---------------------------------------------------------

LEG_ITEMS_SCHEMA = "leg_items_v1"
LEG_ITEM_STATES = ("met", "unmet", "unknown")


def leg_plan(route):
    """What the approved plan says about this route's leg, or None for a route made from no plan.

    `{index, leg, legs, adopted_brief, handed_over}`: the sealed leg with its optional `done_when`,
    `verify`, `hands_over`, `parallel` and `extra_stages`; the brief whose proposal the person chose;
    and what the earlier legs said they hand over. Read from the sealed decision, never copied."""
    try:
        sealed = validate_sealed(route.get("route_plan"))
        root = Path(route["artifact_root"]).resolve()
        binding = read_route_plan(f"{root / sealed['decision']}#{sealed['index']}", root)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if binding["digest"] != sealed["digest"]:
        return None
    decision = binding["record"]["decision"]
    adopted = next((row.get("brief_path") for row in decision.get("proposals") or []
                    if isinstance(row, dict) and isinstance(row.get("proposal"), dict)
                    and same_proposal(row["proposal"], decision["proposal"])), None)
    legs, index = binding["legs"], binding["index"]
    return {"index": index, "leg": binding["leg"], "legs": legs, "adopted_brief": adopted,
            "handed_over": [{"leg": at, "items": legs[at]["hands_over"]} for at in range(index)
                            if isinstance(legs[at], dict) and legs[at].get("hands_over")]}


def leg_items_path(artifact) -> Path:
    """Where a verification stage records the state of each `done_when` item: beside its artifact."""
    return Path(str(artifact) + ".items.json")


def read_leg_items(route, artifact):
    """`{ids, unmet, node, attempt_id}` from the sidecar a verification stage wrote beside `artifact`,
    or None when it cannot be judged (no `done_when`, no file, another route, a malformed file, or an
    id the plan did not seal). `ids` follow the sealed `done_when` order; `unmet` is every id whose
    state is not `met`, an id the file leaves out included."""
    plan = leg_plan(route)
    ids = [item["id"] for item in ((plan or {}).get("leg") or {}).get("done_when") or []]
    if not ids:
        return None
    try:
        value = json.loads(leg_items_path(artifact).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = value.get("items") if isinstance(value, dict) else None
    if (value.get("schema") != LEG_ITEMS_SCHEMA if isinstance(value, dict) else True) \
            or value.get("route_id") != route.get("route_id") or not isinstance(items, list):
        return None
    states = {}
    for item in items:
        if (not isinstance(item, dict) or item.get("id") not in ids or item["id"] in states
                or item.get("state") not in LEG_ITEM_STATES):
            return None
        states[item["id"]] = item["state"]
    return {"ids": ids, "unmet": [i for i in ids if states.get(i) != "met"],
            "node": value.get("node"), "attempt_id": value.get("attempt_id")}


# --- The plan cursor -------------------------------------------------------------------
# One append-only row per leg the runtime started from an approved decision, beside the
# decision's frame route (the same shape as a route's recorded pin changes): which leg, the
# route sealed for it, who advanced the plan and when. A replayed start reads it, so the same
# leg is never compiled twice.

PLAN_CURSOR_SCHEMA = 1


def plan_cursor_path(root, frame_route_id) -> Path | None:
    if not isinstance(frame_route_id, str) or not re.fullmatch(r"rt-[0-9a-f]{8,64}", frame_route_id):
        return None
    return Path(root).resolve() / ".runtime" / "framed-decision" / f"{frame_route_id}.plan-cursor.jsonl"


def plan_cursor(root, frame_route_id, digest) -> list:
    """The started legs of one decision, oldest first; rows for another decision are ignored."""
    path = plan_cursor_path(root, frame_route_id)
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path else []
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("schema") == PLAN_CURSOR_SCHEMA and row.get("digest") == digest
                and type(row.get("index")) is int and isinstance(row.get("route_file"), str)):
            rows.append(row)
    return rows


def append_plan_cursor(root, frame_route_id, row) -> None:
    path = plan_cursor_path(root, frame_route_id)
    if path is None:
        raise ValueError("plan-cursor-unlocated")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"schema": PLAN_CURSOR_SCHEMA, **row}, sort_keys=True) + "\n")


def next_leg_for_route(route):
    """`project_next_leg` for a finished route, found from its own sealed cycle; None otherwise."""
    if not isinstance(route, dict) or route.get("route_plan") is None:
        return None
    try:
        record = completed_cycle(route)
    except (OSError, ValueError, KeyError):
        return None
    return project_next_leg(route, record["cycle_id"]) if record else None
