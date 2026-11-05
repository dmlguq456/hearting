#!/usr/bin/env python3
"""Frame interview: the questions a person answers at the `frame-review` gate,
and the intent document their answers produce (SD-129).

Depth-0 writes `shards/frame/interview.json` after both frame legs join and
raises the gate with it as the reviewable artifact. Depth-0 puts the questions
to the user one topic at a time, records the answers with
`workflow-supervisor.py release --decision proceed --answers <file>`, and
renders `shards/frame/intent.md` from interview + answers before the owner is
launched. `plan` reads the intent document as its brief.

The interview asks only for decisions the person owns. Validation checks the
schema, route and answer bindings, choice structure, approval meaning, actor
provenance, question cap, and a finite serialized-input limit. Writing
guidance recommends clear wording and one decision per question without
rejecting a valid answer for style. Facts a tool can establish are not
questions -- the owner investigates them -- so every question carries `why`:
the reason only the person can decide it.

    frame_interview.py validate      --interview F --intensity I
    frame_interview.py answers-template --interview F
    frame_interview.py validate-answers --interview F --answers A
    frame_interview.py render-intent --interview F --answers A --out intent.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "frame_interview_v1"
ANSWERS_SCHEMA = "frame_interview_answers_v1"
ANSWER_ACTOR_KINDS = ("user", "supervisor", "automatic", "headless-owner", "unknown")
ANSWER_SOURCE_GUIDANCE = ('Set "actor_kind": "user" only for the person\'s actual reply, or '
                          '"actor_kind": "supervisor" when the depth-0 supervisor answers on the person\'s behalf '
                          '(including adopting a recommendation). The template stays "unknown" until the source '
                          'is explicitly recorded; unmarked answers remain unknown. Registered workers cannot '
                          'claim user or supervisor authority. Supervisor answers release frame-review only; '
                          'person-only start approvals keep their gates.')


def path_conflicts(interview, cwd):
    """Advisory only: one explicitly named output file in two different folders."""
    pattern = re.compile(r"(?<![\w:])(?:/|\./|[\w.-]+/)[\w./-]+\.[\w-]+")
    brief = interview.get("brief") or {}
    wording = [brief.get("outcome", "")] + [q.get("question", "") for q in interview.get("questions", [])]
    spoken = {p for text in wording if isinstance(text, str) for p in pattern.findall(text)}
    proposals = (interview.get("route_proposals") or {}).get("by_option") or {}
    def unique_paths(paths, base):
        grouped = {}
        for raw in paths:
            path = (Path(raw) if Path(raw).is_absolute() else base / raw).resolve()
            grouped.setdefault(path.name, set()).add(path)
        # Multiple folders with the same filename can describe inputs, copies or
        # alternatives. Their meaning is ambiguous, so there is no conflict claim.
        return {name: next(iter(values)) for name, values in grouped.items() if len(values) == 1}
    found = []
    for proposal in proposals.values():
        for index, leg in enumerate(proposal.get("legs", [])):
            import route_plan
            base = Path(route_plan.leg_cwd(leg, cwd or "."))
            checked = set()
            for item in leg.get("done_when", []):
                text = " ".join(str(item.get(k) or "") for k in ("text", "check")) if isinstance(item, dict) else str(item)
                checked.update(pattern.findall(text))
            expected, described = unique_paths(checked, base), unique_paths(spoken, base)
            for name in expected.keys() & described.keys():
                if expected[name] != described[name]:
                    row = {"leg": index, "interview_path": str(described[name]), "done_when_path": str(expected[name])}
                    if row not in found:
                        found.append(row)
    return found[:16]


ANSWERS_SHAPE = ('answers file: {"actor_kind": "supervisor", "understanding_confirmed": true, '
                 '"answers": {"<question id>": {"choice": 0}}} '
                 '-- choice is an option index or label ("none" plus a "note" when no option fits); '
                 'add "correction" when understanding_confirmed is false. ' + ANSWER_SOURCE_GUIDANCE)
# A decision question stays open until the person answers (roles/response-policy.md).
PENDING_ANSWER_RULE = ("Until the person answers, do not proceed and do not ask again; if the turn "
                       "ends first, end it with the question and its options restated, and use the "
                       "person's next reply, structured or typed, as the answer. Timeout, empty "
                       "answers, and async accepted are not decisions; keep the same question pending.")

# Questions per raise, by intensity. `quick` now carries the `frame-review`
# gate too (entry-bound at its `one-shot` node), so its cap is machine-checked
# at the raise exactly like `standard+`. Only `direct` has no gate; its cap
# governs the inline interview the depth-0 session runs inside the §0.4 card.
QUESTION_CAP = {
    "direct": 1, "quick": 3,
    "standard": 7, "strong": 7, "thorough": 7, "adversarial": 7,
}
MAX_ROUNDS = 2                 # raises per route that may carry an interview
MAX_QUESTION_CHARS = 160
MAX_SENTENCES = 2
MAX_OPTION_LABEL_CHARS = 40
MAX_OPTION_MEANS_CHARS = 120
MAX_TOPIC_CHARS = 40
MAX_WHY_CHARS = 140
MAX_UNDERSTANDING_CHARS = 220
MAX_BRIEF_FIELD_CHARS = 500
MIN_OPTIONS, MAX_OPTIONS = 2, 4
KINDS = ("yes-no", "choice")
BRIEF_FIELDS = ("problem", "outcome", "affected", "constraints", "open")

# Terms that writing guidance can flag for an author. They are not validation
# errors; technical names may help a person understand a particular choice.
JARGON = (
    "route", "routes", "dispatch", "dispatched", "owner", "attempt", "attempts",
    "worker", "workers", "gate", "gates", "ledger", "supervisor", "node", "nodes",
    "shard", "shards", "intensity", "harness", "carrier", "marker", "receipt",
    "registry", "depth", "topology", "recipe", "frame-review", "plan-check",
    "impl-review", "parallel group", "worktree", "hook", "hooks", "sidecar",
    "quiescent", "asyncrewake", "sweep", "envelope", "pipeline", "conductor",
    "라우트", "디스패치", "오너", "어템프트", "워커", "게이트", "레저", "슈퍼바이저",
    "노드", "샤드", "하네스", "캐리어", "마커", "리시트", "레지스트리", "토폴로지",
    "워크트리", "사이드카", "파이프라인", "컨덕터",
)
# 훅 is not in the Korean list: as an adverb ("suddenly") it is ordinary
# Korean, and the English `hook` still catches the harness sense.
_KOREAN_PARTICLES = ("을", "를", "이", "가", "은", "는", "의", "에", "에서", "로", "으로", "와", "과",
                     "도", "만", "까지", "부터", "처럼", "마다", "보다", "에게", "께", "한테", "이라",
                     "라고", "이나", "나", "이며", "며", "이고", "고", "든", "이든", "이란", "란")
JARGON_IDS = re.compile(r"(?<![A-Za-z0-9])(?:SD-\d+|rt-[0-9a-f]{6,}|att-[0-9a-f]{6,}|cyc_[0-9a-f]{6,}|camp_[0-9a-f]{6,}|rrev_[0-9a-f]{6,})(?![A-Za-z0-9])", re.I)


def _jargon_pattern(term: str) -> "re.Pattern[str]":
    """English terms are bounded by ASCII letters only, so `route_id`, `owner-side`
    and `sub-node` are hits while `gateway` is not. Korean terms are plain
    substrings: Korean is agglutinative, so a term almost always carries a
    particle (`게이트를`, `오너가`) and a word boundary would never fire."""

    if re.search(r"[가-힣]", term):
        # A harness term followed by a particle (`게이트를`, `오너가`) or by a
        # non-Hangul character; a term that continues into another Hangul
        # syllable is a different word (`게이트볼`, `마커펜` -- review round 2, N2).
        particles = "|".join(sorted(map(re.escape, _KOREAN_PARTICLES), key=len, reverse=True))
        return re.compile(re.escape(term) + r"(?:(?![가-힣])|(?:" + particles + r")(?![가-힣]))")
    return re.compile(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", re.I)


_JARGON_PATTERNS = [_jargon_pattern(term) for term in JARGON]
# A name written as code (`capability_route.test.py`) is shown to the person as
# a name, not as a word to decode, so harness terms inside backticks are not
# hits. Internal ids stay hits anywhere: a person never needs `rt-…` to decide.
_CODE_SPAN = re.compile(r"`[^`\n]+`")
_ABBREVIATIONS = re.compile(r"\b(?:e\.g|i\.e|etc|vs|cf|Mr|Mrs|Ms|Dr|No)\.", re.I)
_SENTENCE_END = re.compile(r"[.!?。？！]+(?:\s|$)")
MAX_NOTE_CHARS = 500
# An off-menu answer's note IS the whole decision -- there is no label carrying
# any of it -- so it gets more room than an ordinary aside (2026-09-10: the
# `landing-scope` answer of this cycle needed a scope, an exclusion list and a
# defect callout, and did not fit in 500). `MAX_ANSWERS_BYTES` below is still
# the real ceiling for the payload as a whole.
MAX_OFFMENU_NOTE_CHARS = 1200
MAX_CORRECTION_CHARS = 500
# The payload ceiling is DERIVED from the per-field caps, never set beside
# them. It used to be a free-standing 8192 bytes next to caps counted in
# characters: a Korean character is 3 UTF-8 bytes, so three valid off-menu
# answers already broke the total, and "answer every field within its cap" no
# longer implied "the answers are accepted". 4 bytes is the UTF-8 maximum per
# character; 256 per question covers keys, choice and JSON punctuation.
MAX_ANSWERS_BYTES = (
    max(QUESTION_CAP.values()) * (MAX_OFFMENU_NOTE_CHARS * 4 + 256)
    + MAX_CORRECTION_CHARS * 4 + 1024
)
MAX_INTERVIEW_BYTES = 64 * 1024
# `choice` value meaning "answered, but none of the printed options apply".
# Kept strictly distinct from `None` (the template default, "unanswered"):
# collapsing the two would rebuild the very defect this sentinel closes.
NONE_SENTINEL = "none"


# One complete question as the validator accepts it, shown beside the empty interview
# template so the shape is visible. It is an example only and never part of an interview.
QUESTION_EXAMPLE = {
    "id": "scope", "topic": "How much to change",
    "question": "Should the fix cover only the approval step, or also the wording of the questions?",
    "kind": "choice",
    "options": [{"label": "Both", "means": "Fix the approval step and rewrite the questions in plain words."},
                {"label": "Approval step only", "means": "Leave the question wording as it is for now."}],
    "recommended": 0,
    "why": "Only you can say whether the wording matters enough to be in scope.",
}

# An approval question marks its approving option with `"approves": true` (exactly one of its two
# options); choosing the other option declines. Position carries no meaning.
ROUTE_PROPOSAL_KEYS = frozenset({"question", "by_option"})
# The runtime builds a framed interview's route question and its start-approval questions.
# A route option names the frame brief whose proposal it selects with `"proposal": <frame node>`;
# the runtime turns those marks into `route_proposals` when the interview is submitted.
ROUTE_QUESTION_ID = "route-choice"
PROPOSAL_MARK = "proposal"


# The numbered sections of a frame's direction brief (roles/units/plan/frame.md, Direction-Brief
# Schema) that draft the interview brief. The owner rewrites the draft in the person's words.
BRIEF_DRAFT_SECTIONS = {"problem": 1, "outcome": 4, "constraints": 5, "open": 6}
_HEADING = r"^[ \t]{{0,3}}#{{1,6}}[ \t]*(?:\*\*)?[ \t]*{}(?![0-9])[^\n]*$"


def _first_paragraph(text: str, limit: int) -> str:
    paragraph = " ".join(next((block for block in re.split(r"\n[ \t]*\n", text.strip()) if block.strip()), "").split())
    if len(paragraph) <= limit:
        return paragraph
    cut = paragraph[:limit]
    end = max(cut.rfind(". "), cut.rfind("다. "))
    return cut[:end + 1] if end > 0 else cut[:limit - 1].rstrip() + "…"


def brief_draft(text: str) -> dict:
    """The interview brief drafted from a frame's direction brief: each field the first paragraph
    of its numbered section, within the field's length; "" where the brief has no such section."""
    draft = {field: "" for field in BRIEF_FIELDS}
    for field, number in BRIEF_DRAFT_SECTIONS.items():
        head = re.search(_HEADING.format(number), text, re.M)
        if head is None:
            continue
        rest = text[head.end():]
        tail = re.search(_HEADING.format(r"[0-9]{1,2}"), rest, re.M)
        draft[field] = _first_paragraph(rest[:tail.start()] if tail else rest, MAX_BRIEF_FIELD_CHARS)
    return draft


_NO_USER_QUESTIONS = re.compile(
    r"^[\W_]*(?:없음|없다|없습니다|0\s*(?:개|건)|none|no questions?|n/?a|nothing)(?![가-힣A-Za-z])", re.I)


def user_questions_left(text: str) -> bool:
    """Whether a direction brief's section 7 (questions only the user can answer) lists any.

    A brief without that section is treated as listing some: the person is asked as before."""
    head = re.search(_HEADING.format(7), text or "", re.M)
    if head is None:
        return True
    rest = text[head.end():]
    tail = re.search(_HEADING.format(r"[0-9]{1,2}"), rest, re.M)
    body = (rest[:tail.start()] if tail else rest).strip()
    return bool(body) and not _NO_USER_QUESTIONS.match(body)


def understanding_draft(request: str) -> str:
    """The restatement drafted from the work request's first sentence, within its length."""
    first = re.split(r"(?<=[.!?。])\s|\n", (request or "").strip(), maxsplit=1)[0]
    return _first_paragraph(first, MAX_UNDERSTANDING_CHARS)


NATIVE_ANSWERS_NAME = "answers.native.json"
NATIVE_PARTIAL_NAME = "answers.native.partial.json"   # never read as answers: the rows of an unfinished interview
NATIVE_PARTIAL_SCHEMA = "frame_interview_native_partial_v1"
UNDERSTANDING_SLOT = "_understanding"                  # question ids match [a-z][a-z0-9-]{0,31}, so no clash
MAX_NATIVE_PARTIAL_BYTES = MAX_INTERVIEW_BYTES + MAX_ANSWERS_BYTES
_RECOMMENDED = re.compile(r"\s*[(（](?:권장|recommended)[)）]\s*$", re.I)


def _plain(text) -> str:
    value = _text(text)
    while True:
        cleaned = _RECOMMENDED.sub("", value)
        if cleaned == value:
            break
        value = cleaned
    return " ".join(value.split())


def _native_option_index(value, labels):
    """Match native display text without choosing the first normalized collision."""
    raw = _text(value).strip()
    exact = [index for index, label in enumerate(labels) if raw == _text(label).strip()]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    normalized = _plain(raw)
    matches = [index for index, label in enumerate(labels) if normalized and normalized == _plain(label)]
    return matches[0] if len(matches) == 1 else None


def _restatement_outcome(row):
    """`(confirmed, correction)` of the restatement's reply, or None when it is not an answer."""
    options = list(row.get("options") or [])
    reply = _text(row.get("answer")).strip()
    if not _plain(reply):
        return None
    index = _native_option_index(reply, options)
    if options and index == 0:
        return True, ""
    if index is not None:
        return None                                   # "no" without saying what is wrong: ask for it
    return False, reply


def _question_outcome(question: dict, row):
    """One registered question's answer entry from its native reply row, or None."""
    if not _plain(row.get("answer")):
        return None
    labels = _labels(question)
    picked = _text(row.get("answer")).strip()
    option_index = _native_option_index(picked, labels)
    if option_index is None and any(_plain(picked) == _plain(label) for label in labels):
        return None
    if option_index is None:
        return {"choice": NONE_SENTINEL, "note": picked}
    # The words the person added beside the option they picked; a reply is never asked again
    # for its length, so an over-long note keeps its head and says it was cut.
    note = _text(row.get("note")).strip()
    if len(note) > MAX_NOTE_CHARS:
        mark = _truncated_mark()
        note = note[:MAX_NOTE_CHARS - len(mark)] + mark
    return {"choice": option_index, "note": note}


def _truncated_mark() -> str:
    """The visible cut marker, in the shared UI-text catalog's language."""
    try:
        import work_intent
        return work_intent.ui_translator()("frame.note_truncated")
    except Exception:  # noqa: BLE001 -- the catalog is optional here; English stands in
        return "…(truncated)"


def native_slots(interview: dict, asked) -> dict | None:
    """The registered rows one native call answers: `{UNDERSTANDING_SLOT | question id: row}`.

    `asked` lists what the tool showed and got back: `{question, options: [label], answer}`.
    The restatement is asked as its own question (the understanding sentence, verbatim);
    every registered question is matched by its own text. A call may carry only some of
    them. None when a shown registered row is not an answer (empty, "no" without a
    correction, an ambiguous label, a note over its cap): that call is asked again whole."""
    shown = [row for row in asked or [] if isinstance(row, dict) and _plain(row.get("question"))]
    find = lambda text: next((row for row in shown if _plain(text) and _plain(text) in _plain(row["question"])), None)
    keep = lambda row: {"question": _text(row.get("question")), "options": list(row.get("options") or []),
                        "answer": _text(row.get("answer")),
                        **({"note": _text(row.get("note"))} if _plain(row.get("note")) else {})}
    slots = {}
    row = find(interview.get("understanding"))
    if row is not None:
        outcome = _restatement_outcome(row)
        if outcome is None or len(outcome[1]) > MAX_CORRECTION_CHARS:
            return None
        slots[UNDERSTANDING_SLOT] = keep(row)
    for question in interview.get("questions") or []:
        row = find(question.get("question"))
        if row is None:
            continue
        outcome = _question_outcome(question, row)
        if outcome is None or len(outcome["note"]) > MAX_OFFMENU_NOTE_CHARS:
            return None
        slots[_text(question.get("id"))] = keep(row)
    return slots


def missing_native_slots(interview: dict, slots) -> list[str]:
    """The slots (`UNDERSTANDING_SLOT`, then question ids in interview order) with no valid row yet."""
    slots = slots if isinstance(slots, dict) else {}
    row = slots.get(UNDERSTANDING_SLOT)
    missing = [] if isinstance(row, dict) and _restatement_outcome(row) is not None else [UNDERSTANDING_SLOT]
    for question in interview.get("questions") or []:
        row = slots.get(_text(question.get("id")))
        if not (isinstance(row, dict) and _question_outcome(question, row) is not None):
            missing.append(_text(question.get("id")))
    return missing


def answers_from_native_slots(interview: dict, slots) -> dict | None:
    """The answers file once `slots` cover the restatement and every registered question, else None."""
    if missing_native_slots(interview, slots):
        return None
    confirmed, correction = _restatement_outcome(slots[UNDERSTANDING_SLOT])
    given = {_text(question.get("id")): _question_outcome(question, slots[_text(question.get("id"))])
             for question in interview.get("questions") or []}
    return {"schema": ANSWERS_SCHEMA, "route_id": interview.get("route_id"), "round": interview.get("round", 1),
            "actor_kind": "user", "understanding_confirmed": confirmed, "correction": correction, "answers": given}


def answers_from_native(interview: dict, asked) -> dict | None:
    """The answers file one native question call makes, or None when that call is not a complete
    answer to this interview (a call that carries only some rows is combined by `work_start`).

    The restatement's first option confirms it and typed text is the correction. A printed
    label picks that option and typed text is an off-menu answer with that text as its note.
    The reply came from the person's native question box, so the source is `user`."""
    slots = native_slots(interview, asked)
    return None if slots is None else answers_from_native_slots(interview, slots)


def approval_question_id(leg, key) -> str:
    """The id of the yes/no question that approves the `key` parts of leg `leg`."""
    return f"{key}-leg{leg}"


# Who answers a frame-review interview (user decision 2026-10-07): the person, or a supervisor
# session answering on the person's behalf. The supervisor's answer keeps its source. It does not
# approve a start that changes the person's real artifact or something outside the workspace:
# those parts keep their gate for the person.
FRAME_REVIEW_SOURCES = ("user", "supervisor")
PERSON_ONLY_START_APPROVALS = ("deploy", "handback", "preview")
# A supervisor answer the runtime records for the depth-0 session when both frame legs agree, no
# start approval is in scope and no question is left (WORKFLOW §0.4): the existing on-behalf
# authority, labelled so it is never mistaken for a session's own judgement.
AUTOMATIC_BASIS = "automatic-agreement"


def answer_releases_gate(actor_kind, gate) -> bool:
    """Whether an answer from `actor_kind` releases `gate` (any gate for the person)."""
    return actor_kind == "user" or (gate == "frame-review" and actor_kind in FRAME_REVIEW_SOURCES)


def answer_approves_start(actor_kind, key) -> bool:
    """Whether an answer from `actor_kind` approves starting the `key` parts now."""
    return actor_kind == "user" or (actor_kind == "supervisor" and key not in PERSON_ONLY_START_APPROVALS)
APPROVAL_FIELDS = frozenset({"key", "leg", "question"})


class InterviewError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def load_json_file(path, *, max_bytes=MAX_INTERVIEW_BYTES, kind="interview", return_bytes=False):
    """Read one bounded interview/answer JSON document before parsing it."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise InterviewError(f"{kind}-unreadable", f"{path}: {exc}") from exc
    if len(raw) > max_bytes:
        raise InterviewError(f"{kind}-too-large", f"{len(raw)} bytes > {max_bytes}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InterviewError(f"{kind}-unreadable", f"{path}: {exc}") from exc
    return (value, raw) if return_bytes else value


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def jargon_hits(text: str) -> list[str]:
    words = _CODE_SPAN.sub(" ", text)
    hits = [term for term, pattern in zip(JARGON, _JARGON_PATTERNS) if pattern.search(words)]
    hits += [m.group(0) for m in JARGON_IDS.finditer(text)]
    return hits


def writing_guidance() -> tuple[str, ...]:
    """Optional drafting advice; none of these style preferences gate an answer."""
    return (
        "Use clear wording that fits the reader and the decision.",
        "Keep each question centered on one decision when practical.",
        "Use technical names when they make the choice clearer, and explain them when useful.",
        "Prefer concise wording while giving the reader enough context to answer.",
    )


def _sentences(text: str) -> int:
    cleaned = _ABBREVIATIONS.sub(lambda m: m.group(0).replace(".", ""), text.strip())
    parts = [p for p in _SENTENCE_END.split(cleaned) if p.strip()]
    return max(1, len(parts)) if cleaned else 0


def is_interview(value) -> bool:
    return isinstance(value, dict) and value.get("schema") == SCHEMA


def foreign_interview_schema(value) -> str | None:
    """The schema string of something that calls itself an interview but is not
    `frame_interview_v1` (e.g. `cairn-frame-interview/v1`), else None. A frame
    summary or any other artifact with an unrelated schema is not "foreign"."""
    if not isinstance(value, dict) or is_interview(value):
        return None
    schema = value.get("schema")
    if isinstance(schema, str) and "interview" in schema.lower():
        return schema
    return None


def question_cap(intensity: str) -> int:
    return QUESTION_CAP.get(str(intensity or "").strip(), QUESTION_CAP["standard"])


def validate(interview: dict, *, intensity: str = "standard") -> list[str]:
    """Every reason this interview may not be put in front of a person. Empty
    means it may. Never raises on shape -- the reasons are the output."""

    errors: list[str] = []
    if not is_interview(interview):
        return [f"schema: expected {SCHEMA!r}"]
    try:
        size = len(json.dumps(interview, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return ["interview: not JSON-serializable"]
    if size > MAX_INTERVIEW_BYTES:
        return [f"interview: {size} bytes > {MAX_INTERVIEW_BYTES}"]
    if not _text(interview.get("route_id")):
        errors.append("route_id: missing")
    if not _text(interview.get("summary")):
        errors.append("summary: path to frame-summary.json missing")
    understanding = _text(interview.get("understanding")).strip()
    if not understanding:
        errors.append("understanding: the owner's restatement is missing")
    brief = interview.get("brief")
    if not isinstance(brief, dict):
        errors.append("brief: missing (problem/outcome/affected/constraints/open)")
    else:
        for field in BRIEF_FIELDS:
            text = _text(brief.get(field)).strip()
            # Direction briefs have no dedicated affected-users/systems section.
            # Accept an empty draft rather than require invented scope.
            if not text and field not in {"affected", "open"}:
                errors.append(f"brief.{field}: missing")
    questions = interview.get("questions")
    if not isinstance(questions, list):
        errors.append("questions: must be a list (empty is allowed)")
        questions = []
    cap = question_cap(intensity)
    if len(questions) > cap:
        errors.append(f"questions: {len(questions)} > cap {cap} for intensity {intensity!r}")
    seen: set[str] = set()
    topics: set[str] = set()
    for index, question in enumerate(questions):
        where = f"questions[{index}]"
        if not isinstance(question, dict):
            errors.append(f"{where}: not an object")
            continue
        qid = _text(question.get("id")).strip()
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", qid or ""):
            errors.append(f"{where}.id: missing or not a short slug")
        elif qid in seen:
            errors.append(f"{where}.id: duplicate {qid!r}")
        seen.add(qid)
        topic = _text(question.get("topic")).strip()
        if not topic:
            errors.append(f"{where}.topic: missing")
        elif topic.lower() in topics:
            errors.append(f"{where}.topic: {topic!r} already asked -- one topic, one question")
        topics.add(topic.lower())
        text = _text(question.get("question")).strip()
        if not text:
            errors.append(f"{where}.question: missing")
        kind = _text(question.get("kind"))
        if kind not in KINDS:
            errors.append(f"{where}.kind: {kind!r} not in {KINDS}")
        options = question.get("options")
        if not isinstance(options, list) or not (MIN_OPTIONS <= len(options) <= MAX_OPTIONS):
            errors.append(f"{where}.options: need {MIN_OPTIONS}-{MAX_OPTIONS} options")
            options = options if isinstance(options, list) else []
        elif kind == "yes-no" and len(options) != 2:
            errors.append(f"{where}.options: a yes-no question has exactly 2 options")
        for oindex, option in enumerate(options):
            owhere = f"{where}.options[{oindex}]"
            if not isinstance(option, dict):
                errors.append(f"{owhere}: not an object")
                continue
            label = _text(option.get("label")).strip()
            means = _text(option.get("means")).strip()
            if not label:
                errors.append(f"{owhere}.label: missing")
            if not means:
                errors.append(f"{owhere}.means: say in one line what choosing it does")
        recommended = question.get("recommended")
        if not isinstance(recommended, int) or isinstance(recommended, bool) \
                or not (0 <= recommended < max(len(options), 1)):
            errors.append(f"{where}.recommended: must index one option -- every question carries a recommended answer")
        why = _text(question.get("why")).strip()
        if not why:
            errors.append(f"{where}.why: say why only the user can decide this (a fact a tool can find is not a question)")
    if "route_proposals" in interview:
        errors += _route_proposal_errors(interview, questions)
    round_no = interview.get("round", 1)
    if not isinstance(round_no, int) or isinstance(round_no, bool) or not (1 <= round_no <= MAX_ROUNDS):
        errors.append(f"round: must be 1..{MAX_ROUNDS}")
    return errors


def _question_table(questions) -> dict:
    return {_text(q.get("id")): q for q in questions if isinstance(q, dict) and _text(q.get("id"))}


def _labels(question) -> list:
    options = question.get("options") if isinstance(question.get("options"), list) else []
    return [_text(o.get("label")) for o in options if isinstance(o, dict)]


def _route_proposal_errors(interview: dict, questions: list) -> list[str]:
    """Reference checks only: the question id, the option labels and the approval questions a
    proposal names must exist. What the answers mean for the route stays with the runtime."""
    field = interview["route_proposals"]
    if not isinstance(field, dict) or set(field) != ROUTE_PROPOSAL_KEYS:
        return ["route_proposals: expected exactly {question, by_option}"]
    table = _question_table(questions)
    qid, by_option = field["question"], field["by_option"]
    if not isinstance(qid, str) or qid not in table:
        return [f"route_proposals.question: no such question {qid!r}"]
    if not isinstance(by_option, dict) or not by_option:
        return ["route_proposals.by_option: must map at least one option label to a proposal"]
    errors: list[str] = []
    approval_ids: set[str] = set()
    labels = _labels(table[qid])
    for label, proposal in by_option.items():
        where = f"route_proposals.by_option[{label!r}]"
        if labels.count(label) != 1:
            errors.append(f"{where}: {'ambiguous' if label in labels else 'not an option of'} question {qid!r}")
        if not isinstance(proposal, dict) or not isinstance(proposal.get("legs"), list) or not proposal["legs"]:
            errors.append(f"{where}: not a route proposal")
            continue
        scope = proposal.get("execution_scope", "complete")
        if scope not in ("complete", "report"):
            errors.append(f"{where}.execution_scope: expected complete or report")
        approvals = proposal.get("entry_approvals") or []
        if not isinstance(approvals, list):
            errors.append(f"{where}.entry_approvals: must be a list")
            continue
        for index, approval in enumerate(approvals):
            at = f"{where}.entry_approvals[{index}]"
            if not isinstance(approval, dict) or set(approval) != APPROVAL_FIELDS:
                errors.append(f"{at}: expected exactly {sorted(APPROVAL_FIELDS)}")
                continue
            leg = approval["leg"]
            if isinstance(leg, bool) or not isinstance(leg, int) or not 0 <= leg < len(proposal["legs"]):
                errors.append(f"{at}.leg: outside the proposal's legs")
            asked = table.get(approval["question"]) if isinstance(approval["question"], str) else None
            if asked is None:
                errors.append(f"{at}.question: no such approval question {approval['question']!r}")
            elif approval["question"] == qid:
                if _text(proposal.get("execution_scope", "complete")) not in ("complete", "report"):
                    errors.append(f"{at}.question: the route question needs an execution scope")
            elif _text(asked.get("kind")) != "yes-no":
                errors.append(f"{at}.question: an approval question is yes-no")
            else:
                approval_ids.add(approval["question"])
    for asked_id in sorted(approval_ids):
        errors += _approves_errors(asked_id, table[asked_id])
    return errors


def _approves_errors(qid: str, question: dict) -> list[str]:
    """An approval question marks the one option that approves with `"approves": true`."""
    options = [o for o in question.get("options", []) if isinstance(o, dict)] \
        if isinstance(question.get("options"), list) else []
    errors = [f"approval question {qid!r}: option {_text(o.get('label'))!r} has a non-boolean `approves`"
              for o in options if "approves" in o and not isinstance(o["approves"], bool)]
    marked = sum(1 for o in options if o.get("approves") is True)
    if marked != 1:
        errors.append(f"approval question {qid!r}: mark exactly one option `\"approves\": true` "
                      f"(found {marked})")
    return errors


def resolve_answer(question: dict, entry) -> dict:
    """What one answer chose: `{state: chosen|off-menu|unanswered, label, index}`."""
    labels = _labels(question)
    choice = entry.get("choice") if isinstance(entry, dict) else None
    if choice == NONE_SENTINEL and NONE_SENTINEL not in labels:
        return {"state": "off-menu", "label": None, "index": None}
    if isinstance(choice, str) and choice in labels:
        choice = labels.index(choice)
    if isinstance(choice, int) and not isinstance(choice, bool) and 0 <= choice < len(labels):
        return {"state": "chosen", "label": labels[choice], "index": choice}
    return {"state": "unanswered", "label": None, "index": None}


def route_choice(interview: dict, answers: dict):
    """The route question's answer against `route_proposals`; None when the field is absent.

    `{state: selected|declined|off-menu|unanswered, label, proposal|None}`. A declined option or an
    off-menu answer is a valid answer that selects no proposal.
    """
    field = interview.get("route_proposals") if isinstance(interview, dict) else None
    if not isinstance(field, dict):
        return None
    table = _question_table(interview.get("questions") or [])
    question = table.get(field.get("question"))
    given = answers.get("answers") if isinstance(answers, dict) and isinstance(answers.get("answers"), dict) else {}
    if question is None:
        return {"state": "unanswered", "label": None, "proposal": None}
    chosen = resolve_answer(question, given.get(field["question"]))
    if chosen["state"] == "chosen":
        proposal = (field.get("by_option") or {}).get(chosen["label"])
        return {"state": "selected" if proposal is not None else "declined", "label": chosen["label"],
                "proposal": proposal, "execution_scope": proposal.get("execution_scope", "complete")
                if isinstance(proposal, dict) else None}
    return {"state": chosen["state"], "label": None, "proposal": None}


def _approves(question, index) -> bool:
    """True only for the one option marked `approves: true`; a question with no mark or two marks approves nothing."""
    options = question.get("options") if isinstance(question, dict) else None
    if not isinstance(options, list) or not isinstance(index, int) or not 0 <= index < len(options):
        return False
    marked = [at for at, option in enumerate(options) if isinstance(option, dict) and option.get("approves") is True]
    return marked == [index]


def answer_actor_kind(answers: dict, *, registered_worker=False) -> str:
    """Declared answer provenance; an unmarked file never implies a person.

    The caller supplies its known worker identity separately. A worker cannot
    claim a user reply or a supervisor's reply for the person, and an unmarked
    worker answer retains that known source. This is not authentication of an
    interactive caller's declaration.
    """
    kind = answers.get("actor_kind", "unknown")
    if not isinstance(kind, str) or kind not in ANSWER_ACTOR_KINDS:
        raise ValueError("actor_kind: expected one of " + ", ".join(ANSWER_ACTOR_KINDS))
    if registered_worker and kind in FRAME_REVIEW_SOURCES:
        raise ValueError(f"gate-release-actor-refused: a registered worker cannot claim {kind} answers")
    return "headless-owner" if registered_worker and kind == "unknown" else kind


def recorded_answer_context(answers, actor_kind):
    """Read an old unmarked release with its existing journal provenance.

    No file or historical release is rewritten. Explicit answer provenance wins;
    a missing/unsupported journal source remains unknown.
    """
    if isinstance(answers, dict) and "actor_kind" not in answers:
        return {**answers, "actor_kind": actor_kind if actor_kind in ANSWER_ACTOR_KINDS else "unknown"}
    return answers


def approvals_given(interview: dict, answers: dict, proposal) -> list:
    """Each `entry_approvals` row of the selected proposal with the answer to its question:
    `{key, leg, question, label, accepted}`. Only the option marked `approves: true` accepts.
    A supervisor's yes to a part only the person approves is `held_for_person`: the leg may
    start, and that part keeps its gate."""
    if not isinstance(proposal, dict):
        return []
    table = _question_table(interview.get("questions") or [])
    given = answers.get("answers") if isinstance(answers.get("answers"), dict) else {}
    kind = answer_actor_kind(answers)
    rows = []
    for approval in proposal.get("entry_approvals") or []:
        question = table.get(approval.get("question"))
        chosen = resolve_answer(question, given.get(approval.get("question"))) if question else {"state": "unanswered", "label": None, "index": None}
        same_route_question = (isinstance(interview.get("route_proposals"), dict)
                               and approval.get("question") == interview["route_proposals"].get("question"))
        said_yes = (chosen["state"] == "chosen" if same_route_question else
                    chosen["state"] == "chosen" and _approves(question, chosen["index"]))
        accepted = said_yes and answer_approves_start(kind, approval.get("key"))
        row = {"key": approval.get("key"), "leg": approval.get("leg"), "question": approval.get("question"),
               "label": chosen["label"], "accepted": accepted}
        if said_yes and not accepted and kind in FRAME_REVIEW_SOURCES:
            row["held_for_person"] = True
        rows.append(row)
    return rows


def answers_template(interview: dict) -> dict:
    """What the depth-0 session fills in after asking: one entry per question,
    plus whether the owner's restatement was confirmed."""

    return {
        "schema": ANSWERS_SCHEMA,
        "route_id": interview.get("route_id"),
        "round": interview.get("round", 1),
        "actor_kind": "unknown",
        "understanding_confirmed": None,
        "correction": "",
        "answers": {
            _text(q.get("id")): {"choice": None, "note": ""}
            for q in interview.get("questions", []) if isinstance(q, dict)
        },
    }


def pending_answer_response(interview: dict, response) -> bool:
    """Recognize only an unreceived answer, never a malformed or foreign decision.

    Native Default may return an empty answer map or an async acknowledgement.
    These do not belong in answers.json and cannot release a gate. The caller
    retains the existing interview and awaits a real answer in conversation.
    """
    if response is None:
        return True
    if not isinstance(response, dict):
        return False
    allowed = {"schema", "route_id", "round", "understanding_confirmed", "correction",
               "answers", "accepted", "timeout", "timed_out", "actor_kind"}
    if set(response) - allowed:
        return False
    try:
        answer_actor_kind(response)
    except ValueError:
        return False
    for key, expected in (("schema", ANSWERS_SCHEMA), ("route_id", interview.get("route_id")),
                          ("round", interview.get("round", 1))):
        if key in response and (response[key] != expected or
                                (key == "round" and isinstance(response[key], bool))):
            return False
    try:
        if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > MAX_ANSWERS_BYTES:
            return False
    except (TypeError, ValueError):
        return False
    if response.get("understanding_confirmed") is not None or response.get("correction", "") != "":
        return False
    if any(key in response and response[key] is not True
           for key in ("accepted", "timeout", "timed_out")):
        return False
    given = response.get("answers", {})
    if not isinstance(given, dict):
        return False
    questions = _question_table(interview.get("questions") or [])
    if set(given) - set(questions):
        return False
    for entry in given.values():
        if entry is None:
            continue
        if not isinstance(entry, dict) or set(entry) - {"choice", "note", "answers"}:
            return False
        if entry.get("choice") is not None or entry.get("note", "") != "":
            return False
        if "answers" in entry and entry["answers"] != []:
            return False
    return True


def pending_question_block(interview: dict) -> str:
    """The registered words and choices the parent leaves in ordinary conversation."""
    lines = [_text(interview.get("understanding"))]
    for question in interview.get("questions") or []:
        lines.extend(["", _text(question.get("question"))])
        for option in question.get("options") or []:
            lines.append(f"- {_text(option.get('label'))}: {_text(option.get('means'))}")
    return "\n".join(lines)


def validate_answers(interview: dict, answers: dict) -> list[str]:
    errors: list[str] = []
    if not is_interview(interview):
        # SD-OPEN-48 (#12): checked before the per-question walk, which used to
        # report every real answer as `no such question` against a foreign
        # interview schema.
        return [f"interview.schema: expected {SCHEMA!r}, got "
                f"{(interview or {}).get('schema') if isinstance(interview, dict) else None!r}"]
    if not isinstance(answers, dict):
        return [f"answers: expected a JSON object; {ANSWERS_SHAPE}"]
    if any(key in answers for key in ("accepted", "timeout", "timed_out")):
        errors.append("response: acknowledgement or timeout is not the person's answer")
    if answers.get("schema", ANSWERS_SCHEMA) != ANSWERS_SCHEMA:
        return [f"schema: expected {ANSWERS_SCHEMA!r} or no schema field; {ANSWERS_SHAPE}"]
    if answers.get("route_id", interview.get("route_id")) != interview.get("route_id"):
        errors.append("route_id: answers do not belong to this interview (omit route_id to answer this one)")
    if answers.get("round", interview.get("round", 1)) != interview.get("round", 1):
        errors.append("round: answers belong to a different round (omit round to answer this one)")
    try:
        answer_actor_kind(answers)
    except ValueError as exc:
        errors.append(str(exc))
    try:
        size = len(json.dumps(answers, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return ["answers: not JSON-serializable"]
    if size > MAX_ANSWERS_BYTES:
        # The answers are copied into the append-only journal and the
        # gate-release sidecar, and re-parsed on every await/fence read.
        errors.append(f"answers: {size} bytes > {MAX_ANSWERS_BYTES}")
    confirmed = answers.get("understanding_confirmed")
    if not isinstance(confirmed, bool):
        errors.append("understanding_confirmed: must be true or false -- the user confirms the restatement")
    elif confirmed is False and not _text(answers.get("correction")).strip():
        errors.append("correction: say in the user's words what the owner got wrong")
    if len(_text(answers.get("correction"))) > MAX_CORRECTION_CHARS:
        errors.append(f"correction: {len(_text(answers.get('correction')))} chars > {MAX_CORRECTION_CHARS}")
    given = answers.get("answers")
    if not isinstance(given, dict):
        return errors + [f"answers: must map question id -> {{choice, note}}; {ANSWERS_SHAPE}"]
    questions = {_text(q.get("id")): q for q in interview.get("questions", []) if isinstance(q, dict)}
    for qid in given:
        if qid not in questions:
            errors.append(f"answers.{qid}: no such question")
    for qid, question in questions.items():
        entry = given.get(qid)
        if not isinstance(entry, dict):
            errors.append(f"answers.{qid}: missing")
            continue
        choice = entry.get("choice")
        note = _text(entry.get("note"))
        options = question.get("options") if isinstance(question.get("options"), list) else []
        labels = [_text(o.get("label")) for o in options if isinstance(o, dict)]
        # 2026-09-10, this cycle's own `landing-scope` question: the user's real
        # answer was neither printed option, the schema had no way to say so,
        # and the ledger recorded option 1 as if the user had picked it. An
        # off-menu answer now says so in as many words. A label that is itself
        # literally `"none"` is a real option, so index-conversion wins there
        # and the sentinel reading is refused typed below -- one value never
        # means two different things depending on the option list.
        offmenu = choice == NONE_SENTINEL and NONE_SENTINEL not in labels
        if offmenu:
            if not note.strip():
                # Off-menu means the note carries the entire decision; an empty
                # one records no decision at all.
                errors.append(f"answers.{qid}.note: required when no printed option applies")
            if len(note) > MAX_OFFMENU_NOTE_CHARS:
                errors.append(f"answers.{qid}.note: {len(note)} chars > {MAX_OFFMENU_NOTE_CHARS}")
            continue
        if len(note) > MAX_NOTE_CHARS:
            errors.append(f"answers.{qid}.note: {len(note)} chars > {MAX_NOTE_CHARS}")
        if isinstance(choice, bool) or not isinstance(choice, int) or not (0 <= choice < len(labels)):
            if isinstance(choice, str) and choice in labels:
                entry["choice"] = labels.index(choice)
                if choice == NONE_SENTINEL:
                    errors.append(
                        f"answers.{qid}.choice: option label collides with the none sentinel")
            else:
                errors.append(
                    f"answers.{qid}.choice: must index one of {labels}, "
                    f"or be {NONE_SENTINEL!r} when no printed option applies")
    return errors


def _inline(text: str) -> str:
    """User free text rendered as one line of prose: newlines collapse, so a
    pasted `---` or `## Heading` can never open a new section of the intent."""

    return " ".join(_text(text).split())


def render_intent(interview: dict, answers: dict, *, now: str | None = None, approval_scope=None) -> str:
    """The agreed intent document `plan` reads first. Plain sections in the
    order intent.md uses (Problem, Proposed Outcome, Affected, Constraints,
    Decisions, Open Questions), every decision traceable to its question."""

    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    brief = interview.get("brief") if isinstance(interview.get("brief"), dict) else {}
    given = answers.get("answers") if isinstance(answers.get("answers"), dict) else {}
    confirmed = answers.get("understanding_confirmed") is True
    actor_kind = answer_actor_kind(answers)
    human = actor_kind == "user"
    on_behalf = actor_kind in FRAME_REVIEW_SOURCES and not human
    speaker = "User" if human else actor_kind.capitalize()
    agreed = "agreed-on-behalf" if on_behalf else "agreed"
    lines = [
        "---",
        (f"status: {agreed}" if confirmed else f"status: {agreed}-with-correction") if human or on_behalf
        else "status: recorded",
        f"actor_kind: {actor_kind}",
        f"created: {stamp}",
        f"route_id: {interview.get('route_id', '-')}",
        f"round: {interview.get('round', 1)}",
        f"schema: frame_intent_v1",
        "---",
        "",
        "# Intent",
        "",
        "## Confirmed understanding" if human else
        "## Confirmed understanding (on the person's behalf)" if on_behalf else "## Recorded understanding",
        "",
        _inline(interview.get("understanding")) or "-",
    ]
    if not confirmed:
        lines += ["", f"**{speaker}'s correction:** " + (_inline(answers.get("correction")) or "-")]
    section = {
        "problem": "Problem", "outcome": "Proposed Outcome",
        "affected": "Affected Users / Systems", "constraints": "Constraints",
    }
    for field, title in section.items():
        lines += ["", f"## {title}", "", _inline(brief.get(field)) or "-"]
    lines += ["", "## Decisions", ""]
    questions = [q for q in interview.get("questions", []) if isinstance(q, dict)]
    if not questions:
        lines.append("No question needed a decision from the user; the direction above stands as proposed." if human else
                     f"No question recorded a decision from {actor_kind}; the direction above stands as proposed.")
    for question in questions:
        qid = _text(question.get("id"))
        entry = given.get(qid) if isinstance(given.get(qid), dict) else {}
        options = [o for o in question.get("options", []) if isinstance(o, dict)]
        choice = entry.get("choice")
        labels = [_text(o.get("label")) for o in options]
        offmenu = choice == NONE_SENTINEL and NONE_SENTINEL not in labels
        chosen = options[choice] if isinstance(choice, int) and 0 <= choice < len(options) else None
        recommended = question.get("recommended")
        followed = isinstance(choice, int) and choice == recommended
        lines.append(f"- **{_text(question.get('topic'))}** (`{qid}`): {_text(question.get('question')).strip()}")
        if offmenu:
            # No printed option was chosen, so neither `recommended` nor
            # `user's own choice` can be said -- both would name a label the
            # user never picked. The note below carries the actual decision.
            lines.append("  - Decision: **제시된 선택지 없음** (off-menu)")
        elif chosen is not None:
            tag = "recommended" if followed else ("user's own choice" if human else f"{actor_kind} choice")
            lines.append(f"  - Decision: **{_text(chosen.get('label'))}** ({tag}) — {_text(chosen.get('means')).strip()}")
        else:
            lines.append("  - Decision: unanswered")
        note = _inline(entry.get("note"))
        if note:
            lines.append(f"  - {speaker}'s note: {note}")
    if "route_proposals" in interview:
        lines += _route_lines(interview, answers, approval_scope or {})
    lines += ["", "## Open Questions", "", _inline(brief.get("open")) or "None recorded."]
    lines += ["", "## Sources", "", f"- interview: {interview.get('self_path', 'shards/frame/interview.json')}",
              f"- summary: {_text(interview.get('summary')) or '-'}", ""]
    return "\n".join(lines)


def _route_lines(interview: dict, answers: dict, scope: dict) -> list[str]:
    """The selected route and the approvals actually given, written only for an interview that
    carries `route_proposals`. `scope` maps `(key, leg)` to the part ids shown for that approval."""
    choice = route_choice(interview, answers) or {"state": "unanswered", "proposal": None}
    lines = ["", "## Route", ""]
    proposal = choice["proposal"]
    if proposal is None:
        reason = {"declined": "the proposed route was declined", "off-menu": "a different direction was chosen",
                  "unanswered": "the route question was not answered"}.get(choice["state"], "no route was proposed")
        return lines + [f"No route was selected ({reason}); no step starts automatically and the next route is chosen separately."]
    lines.append(f"Selected route: {_inline(proposal.get('summary')) or '-'}")
    lines.append(f"Execution scope: {proposal.get('execution_scope', 'complete')}")
    for index, leg in enumerate(proposal.get("legs") or []):
        graph = ",".join(leg.get("graph") or []) or "whole recipe"
        size = f" (size {leg['size']})" if leg.get("size") else ""
        lines.append(f"- Leg {index}: {leg.get('capability')} / {leg.get('mode') or 'default mode'} / "
                     f"{leg.get('shape')}{size} / {graph}" + ("  (starts now)" if index == 0 else "  (next step, decided separately)"))
    given = approvals_given(interview, answers, proposal)
    if given:
        lines += ["", "Start approvals:"]
    for row in given:
        parts = ", ".join(scope.get((row["key"], row["leg"])) or []) or "the steps named in the question"
        verdict = ("approved" if row["accepted"] else "held for the person" if row.get("held_for_person")
                   else "declined" if row["label"] is not None else "not answered")
        lines.append(f"- {row['key']} for leg {row['leg']} ({parts}) — question `{row['question']}`: {verdict}")
    return lines


def _load(path: str, *, max_bytes=MAX_INTERVIEW_BYTES, kind="interview") -> dict:
    try:
        value = load_json_file(path, max_bytes=max_bytes, kind=kind)
    except InterviewError:
        raise
    if not isinstance(value, dict):
        raise InterviewError("interview-unreadable", f"{path}: not an object")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="frame_interview")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate"); v.add_argument("--interview", required=True); v.add_argument("--intensity", default="standard")
    t = sub.add_parser("answers-template"); t.add_argument("--interview", required=True)
    va = sub.add_parser("validate-answers"); va.add_argument("--interview", required=True); va.add_argument("--answers", required=True)
    r = sub.add_parser("render-intent"); r.add_argument("--interview", required=True); r.add_argument("--answers", required=True); r.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    interview = _load(args.interview)
    interview.setdefault("self_path", str(Path(args.interview).resolve()))
    if args.command != "validate" and not is_interview(interview):
        # `validate` reports the schema as a reason; the answer-side commands
        # cannot do anything with a foreign interview, so they refuse typed.
        raise InterviewError(
            "interview-schema-unsupported",
            f"{args.interview}: schema {interview.get('schema')!r}, expected {SCHEMA!r}")
    if args.command == "validate":
        errors = validate(interview, intensity=args.intensity)
        print(json.dumps({"valid": not errors, "errors": errors,
                          "questions": len(interview.get("questions") or []),
                          "cap": question_cap(args.intensity)}, ensure_ascii=False))
        return 0 if not errors else 65
    if args.command == "answers-template":
        print(json.dumps(answers_template(interview), ensure_ascii=False, indent=2))
        return 0
    answers = _load(args.answers, max_bytes=MAX_ANSWERS_BYTES, kind="answers")
    errors = validate_answers(interview, answers)
    if args.command == "validate-answers":
        print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False))
        return 0 if not errors else 65
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False), file=sys.stderr)
        return 65
    interview_errors = validate(interview, intensity=str(interview.get("intensity") or "standard"))
    if interview_errors:
        print(json.dumps({"valid": False, "errors": interview_errors}, ensure_ascii=False), file=sys.stderr)
        return 65
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(render_intent(interview, answers), encoding="utf-8")
    tmp.replace(out)
    print(json.dumps({"intent": str(out), "questions": len(interview.get("questions") or [])}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InterviewError as exc:
        print(f"frame_interview: {exc}", file=sys.stderr)
        raise SystemExit(64)
