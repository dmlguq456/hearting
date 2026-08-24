#!/usr/bin/env python3
"""Read-only bridge for ``apid:`` memory pointers.

The module intentionally has exactly three public CLI operations: ``parse``,
``resolve`` and ``duplication-check``.  It never returns artifact content.
"""
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, fields

SLOTS = ("root", "artifact", "cycle", "campaign", "shared-reference", "route")
TARGET_SLOTS = frozenset(SLOTS[1:])
SUBCOMMANDS = frozenset(("parse", "resolve", "duplication-check"))
MAX_TARGETS = 4
MAX_ROOTS = 1
MAX_TOKEN_CHARS = 160
MAX_ENTITIES = 24
INTEGRITY_ORDER = ("ok", "stale", "version-mismatch", "missing", "broken-locator",
                   "quarantined", "forbidden", "unavailable", "unknown-failure")
INTEGRITY_RANK = {value: i for i, value in enumerate(INTEGRITY_ORDER)}
ERROR_TO_INTEGRITY = {
    "AUTH_FAILED": "forbidden", "FORBIDDEN": "forbidden",
    "INVALID_REQUEST": "unknown-failure", "INVALID_CURSOR": "unknown-failure",
    "MISSING_TARGET": "missing", "UNREADABLE_TARGET": "broken-locator",
    "STALE_SNAPSHOT": "stale", "STALE_PROJECTION": "stale",
    "VERSION_MISMATCH": "version-mismatch", "DIGEST_MISMATCH": "version-mismatch",
    "BROKEN_POINTER": "broken-locator",
    "TIMEOUT": "unavailable", "UNAVAILABLE_PROJECTION": "unavailable",
    "LEGACY_MAPPING_MISSING": "missing",
    "LEGACY_MAPPING_MISMATCH": "version-mismatch",
    "CONFLICT_QUARANTINED": "quarantined", "INTERNAL_FAILURE": "unknown-failure",
}
TELEMETRY_KEYS = (
    "request_id", "occurred_at", "artifact_root_id", "namespace_id", "envelope_id",
    "snapshot_id", "retrieval_mode", "verify", "filter_key_set", "filter_digest",
    "query_digest", "query_token_count", "query_language_hint", "result_count",
    "zero_result", "degraded_row_count", "latency_ms", "deadline_ms", "timed_out",
    "error_code", "page_size", "page_ordinal", "pointer_bridge", "pointer_target_slot",
    "pointer_integrity_state",
)
FORBIDDEN_BODY_KEYS = frozenset(("body", "content", "text", "summary", "snippet", "prose"))


@dataclass(frozen=True, slots=True)
class PointerDereferenceResult:
    memory_record_id: object
    disposition: str
    identity: object
    integrity: object
    read_error_code: object
    detail: object
    retryable: bool
    canonical_raw_locator: object
    locator_source: str
    containment_ok: object
    fallback_locators: tuple
    observed_at: object


_RESULT_FIELDS = tuple(field.name for field in fields(PointerDereferenceResult))
assert not (set(_RESULT_FIELDS) & FORBIDDEN_BODY_KEYS)


class BridgeError(Exception):
    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def _tokens(value):
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def parse_token(token):
    """Parse only the first two separators; values remain opaque."""
    if not isinstance(token, str) or not token.startswith("apid:"):
        return None
    parts = token.split(":", 2)
    if len(parts) != 3 or parts[1] not in SLOTS or not parts[2]:
        return None
    value = parts[2]
    if len(token) > MAX_TOKEN_CHARS:
        raise BridgeError("apid-token-too-long", "apid token exceeds 160 characters")
    return {"token": token, "slot": parts[1], "value": value,
            "identity_class": "migration" if value.startswith("migration:") else "stable"}


def _record_parts(record):
    if not isinstance(record, dict):
        raise BridgeError("invalid-record", "record must be an object")
    entities = _tokens(record.get("entities", []))
    refs = _tokens(record.get("artifact_refs", []))
    if any(item.startswith("apid:") for item in refs):
        raise BridgeError("apid-in-artifact-refs", "apid tokens belong in entities")
    parsed = []
    for item in entities:
        value = parse_token(item)
        if value is not None:
            parsed.append(value)
    roots = [item for item in parsed if item["slot"] == "root"]
    targets = [item for item in parsed if item["slot"] in TARGET_SLOTS]
    if len(roots) > MAX_ROOTS:
        raise BridgeError("apid-root-capacity", "at most one root token is allowed")
    if len(targets) > MAX_TARGETS:
        raise BridgeError("apid-target-capacity", "at most four target tokens are allowed")
    return parsed, refs


def parse_record(record):
    parsed, refs = _record_parts(record)
    targets = [item for item in parsed if item["slot"] in TARGET_SLOTS]
    root = next((item for item in parsed if item["slot"] == "root"), None)
    return _serialize_result(PointerDereferenceResult(
        memory_record_id=record.get("id", record.get("memory_record_id")),
        disposition="attempted" if root and targets else "legacy-locator-only",
        identity=([{"slot": item["slot"], "identity_class": item["identity_class"]}
                      for item in parsed] or None),
        integrity=None, read_error_code=None, detail=None, retryable=False,
        canonical_raw_locator=None, locator_source="none", containment_ok=None,
        fallback_locators=tuple(refs), observed_at=None,
    ))


def _serialize_result(result):
    value = asdict(result)
    value["fallback_locators"] = list(value["fallback_locators"])
    assert tuple(value) == _RESULT_FIELDS
    assert not (set(value) & FORBIDDEN_BODY_KEYS)
    return value


def _runtime_codes():
    root = os.environ.get("CAIRN_ROOT")
    if not root:
        return None
    path = Path(root) / "lib" / "artifact-projection" / "read" / "errors.ts"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"READ_ERROR_CODES\s*=\s*\[([\s\S]*?)\]", text)
    if not match:
        return None
    return tuple(re.findall(r"['\"]([A-Z][A-Z_]+)['\"]", match.group(1)))


def _validate_error_domain():
    codes = _runtime_codes()
    if codes is None:
        raise BridgeError("cairn-unavailable", "Cairn read error domain is unavailable")
    if codes != tuple(ERROR_TO_INTEGRITY):
        raise BridgeError("mapping-domain-drift", "Cairn read error domain differs")
    return codes


def _request(root, targets):
    return {"artifact_root_id": root["value"], "resolve_active": True,
            "target_ids": [item["value"] for item in targets],
            "retrieval_mode": "path-only", "verify": "metadata", "order": "stable-id",
            "page_size": 20, "deadline_ms": 3000}


def valid_fallback_locator(root, ref):
    """Return (containment, exists) without ever following an escape target."""
    if not isinstance(root, (str, os.PathLike)) or not isinstance(ref, str) or not ref:
        return False, False
    root_path = Path(root).expanduser()
    candidate = Path(ref) if Path(ref).is_absolute() else root_path / ref
    try:
        root_real = root_path.resolve(strict=False)
        candidate_real = candidate.resolve(strict=False)
        candidate_real.relative_to(root_real)
    except (OSError, ValueError):
        return False, False
    # A symlink anywhere in the relative walk is not a safe raw locator.
    try:
        relative = candidate.relative_to(root_path)
        cursor = root_path
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                return False, False
    except ValueError:
        return False, False
    return True, candidate.exists()


def _read_cairn(request):
    _validate_error_domain()
    command = Path(__file__).with_name("cairn-artifact-read.sh")
    try:
        proc = subprocess.run([str(command)], input=json.dumps(request), text=True,
                              capture_output=True, check=False)
    except OSError as exc:
        raise BridgeError("cairn-unavailable", "Cairn read transport is unavailable") from exc
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BridgeError("cairn-unavailable", "Cairn returned invalid JSON") from exc
    return proc.returncode, payload


def _result_for_target(item, payload, code=None, *, fallback_locators=(), memory_record_id=None):
    error = payload.get("error") if isinstance(payload, dict) else None
    if error:
        code = error.get("code") or code or "INTERNAL_FAILURE"
        integrity = ERROR_TO_INTEGRITY.get(code, "unknown-failure")
        locator = error.get("canonical_raw_locator")
        ok, exists = valid_fallback_locator(os.environ.get("ARTIFACT_ROOT"), locator) if locator else (None, False)
        if locator and ok is False:
            integrity, locator = "broken-locator", None
        detail = error.get("detail")
        if isinstance(detail, str):
            detail = detail[:240]
        return _serialize_result(PointerDereferenceResult(memory_record_id, "attempted",
                {"slot": item["slot"], "identity_class": item["identity_class"]}, integrity,
                code, detail, bool(error.get("retryable", False)), locator,
                "error-payload" if locator else "none", ok, tuple(fallback_locators), None))
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    match = next((row for row in rows if isinstance(row, dict) and
                  (row.get("stable_id") == item["value"] or row.get("target_id") == item["value"])), None)
    if match is None:
        return _serialize_result(PointerDereferenceResult(memory_record_id, "attempted",
                {"slot": item["slot"], "identity_class": item["identity_class"]}, "missing", None,
                None, False, None, "none", None, tuple(fallback_locators), None))
    locator = match.get("canonical_raw_locator")
    ok, exists = valid_fallback_locator(os.environ.get("ARTIFACT_ROOT"), locator) if locator else (None, False)
    integrity = "ok"
    if locator and (ok is False or not exists):
        integrity, locator = "broken-locator", None
    return _serialize_result(PointerDereferenceResult(memory_record_id, "attempted",
            {"slot": item["slot"], "identity_class": item["identity_class"]}, integrity, None,
            None, False, locator, "read-row" if locator else "none", ok,
            tuple(fallback_locators), None))


def _fallback_diagnostic(refs):
    """Inspect memory hints without ever promoting them to transport input."""
    root = os.environ.get("ARTIFACT_ROOT")
    checked = [(ref, *valid_fallback_locator(root, ref)) for ref in refs]
    # Broken hints take precedence, while a valid hint is selected only when a
    # Cairn row did not provide its own canonical locator.  No hint is ever
    # supplied to _request or the Cairn transport.
    for ref, contained, exists in checked:
        if not contained or not exists:
            return ref, "memory-hint", contained, "broken-locator"
    if checked:
        ref, contained, _exists = checked[0]
        return ref, "memory-hint", contained, None
    return None, "none", None, None


def _telemetry(slot, root, results, target_values):
    state = max((item["integrity"] for item in results), key=lambda x: INTEGRITY_RANK[x])
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    path = state_home / "hearting" / "artifact-projection" / "read-telemetry.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"request_id": uuid.uuid4().hex, "occurred_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
           "artifact_root_id": root["value"], "namespace_id": None, "envelope_id": None,
           "snapshot_id": None, "retrieval_mode": "path-only", "verify": "metadata",
           "filter_key_set": [slot], "filter_digest": hashlib.sha256(",".join(sorted(target_values)).encode()).hexdigest(),
           "query_digest": hashlib.sha256(b"").hexdigest(), "query_token_count": 0,
           "query_language_hint": None, "result_count": sum(i["integrity"] == "ok" for i in results),
           "zero_result": all(i["integrity"] == "missing" for i in results), "degraded_row_count": 0,
           "latency_ms": 0, "deadline_ms": 3000, "timed_out": False,
           "error_code": next((i["read_error_code"] for i in results if i["read_error_code"]), None),
           "page_size": 20, "page_ordinal": 0, "pointer_bridge": True,
           "pointer_target_slot": slot, "pointer_integrity_state": state}
    if set(row) != set(TELEMETRY_KEYS):
        raise BridgeError("telemetry-schema", "telemetry key set is not closed")
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise BridgeError("telemetry-append-failed", "telemetry row could not be appended") from exc


def resolve(records):
    if not isinstance(records, list):
        records = [records]
    output = []
    for record in records:
        parsed_tokens, refs = _record_parts(record)
        targets = [item for item in parsed_tokens if item["slot"] in TARGET_SLOTS]
        root = next((item for item in parsed_tokens if item["slot"] == "root"), None)
        if not root or not targets:
            output.append(parse_record(record))
            continue
        grouped = {}
        for item in targets:
            grouped.setdefault(item["slot"], []).append(item)
        for slot, targets in grouped.items():
            try:
                code, payload = _read_cairn(_request(root, targets))
            except BridgeError as exc:
                if exc.code != "cairn-unavailable":
                    raise
                code, payload = 18, {"error": {"code": "INTERNAL_FAILURE",
                                                "detail": exc.detail,
                                                "retryable": False}}
            results = [_result_for_target(item, payload, code,
                                          fallback_locators=refs,
                                          memory_record_id=record.get("id", record.get("memory_record_id")))
                       for item in targets]
            diagnostic = _fallback_diagnostic(refs)
            if diagnostic[0] is not None:
                for index, result in enumerate(results):
                    if diagnostic[3] is not None or result["canonical_raw_locator"] is None:
                        result["canonical_raw_locator"] = diagnostic[0]
                        result["locator_source"] = diagnostic[1]
                        result["containment_ok"] = diagnostic[2]
                        if diagnostic[3] is not None:
                            result["integrity"] = diagnostic[3]
            _telemetry(slot, root, results, [item["value"] for item in targets])
            output.extend(results)
    return output


def normalize_duplication_text(text):
    return re.sub(r"\s+", " ", str(text or "").casefold()).strip()


NORMALIZE_SOURCE_DIGEST = hashlib.sha256(inspect.getsource(normalize_duplication_text).encode("utf-8")).hexdigest()
REPOSITORY_CORPUS = Path(__file__).with_name("fixtures") / "w5-duplication-labeled.json"
REPOSITORY_FREEZE = Path(__file__).with_name("fixtures") / "w5-duplication-freeze.json"


def _lcs(a, b):
    previous = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        current = [0]
        for j, cb in enumerate(b, 1):
            value = previous[j - 1] + 1 if ca == cb else 0
            current.append(value)
            best = max(best, value)
        previous = current
    return best


def _word_ngrams(text, n):
    words = text.split()
    return tuple(zip(*(words[i:] for i in range(n)))) if len(words) >= n else ()


def duplication_predicate(row, freeze):
    body = normalize_duplication_text(row.get("body", row.get("memory_body", "")))
    if row.get("target_readable") is False or row.get("undecidable"):
        return "undecidable"
    target = normalize_duplication_text(row.get("target_text", row.get("artifact_text", "")))
    if not target:
        return "undecidable"
    n = int(freeze.get("ngram_n", 2))
    body_ngrams = set(_word_ngrams(body, n))
    target_ngrams = set(_word_ngrams(target, n))
    overlap = len(body_ngrams & target_ngrams) / len(body_ngrams) if body_ngrams else 0.0
    return "violation" if (len(body.encode("utf-8")) > int(freeze["body_byte_cap"])
        or _lcs(body, target) >= int(freeze["lcs_threshold"])
        or overlap >= float(freeze["ngram_overlap_threshold"])) else "ok"


RULE_SOURCE_DIGEST = hashlib.sha256(inspect.getsource(duplication_predicate).encode("utf-8")).hexdigest()


def duplication_check(corpus, freeze):
    if not isinstance(corpus, list) or not corpus:
        raise BridgeError("no-corpus", "labeled corpus is required")
    if not isinstance(freeze, dict):
        raise BridgeError("no-freeze", "duplication freeze is required")
    required = ("body_byte_cap", "lcs_threshold", "ngram_n", "ngram_overlap_threshold",
                "duplication_rule_version", "normalize_version", "corpus_sha256",
                "corpus_count", "normalize_source_digest", "rule_source_digest",
                "calibration_source_digest")
    if any(key not in freeze for key in required):
        raise BridgeError("no-freeze", "freeze parameters are incomplete")
    fp = fn = undecidable = 0
    items = []
    for row in corpus:
        if not isinstance(row, dict) or "label" not in row:
            raise BridgeError("no-corpus", "corpus labels are required")
        verdict = duplication_predicate(row, freeze)
        expected = row.get("label") in ("positive", "violation", True, 1)
        if verdict == "violation" and not expected: fp += 1
        if verdict == "ok" and expected: fn += 1
        if verdict == "undecidable": undecidable += 1
        normalized_body = normalize_duplication_text(row.get("body", row.get("memory_body", "")))
        items.append({"id": row.get("id"), "label": row.get("label"), "verdict": verdict,
                      "ngram_denominator_count": len(set(_word_ngrams(
                          normalized_body, int(freeze.get("ngram_n", 2)))))} )
    if "false_positive" in freeze:
        if (fp, fn, undecidable) != (freeze["false_positive"], freeze["false_negative"], freeze["undecidable_count"]):
            raise BridgeError("freeze-count-mismatch", "measured calibration counts differ from freeze")
        if fp > freeze["false_positive_upper_bound"] or fn > freeze["false_negative_upper_bound"] \
                or not (freeze["undecidable_lower_bound"] <= undecidable <= freeze["undecidable_upper_bound"]):
            raise BridgeError("calibration-bounds-exceeded", "measured counts exceed declared bounds")
    return {"duplication_rule_version": freeze["duplication_rule_version"],
            "normalize_version": freeze.get("normalize_version", "w5-norm-v1"),
            "normalize_source_digest": NORMALIZE_SOURCE_DIGEST,
            **{key: freeze[key] for key in required}, "denominator": "memory-side",
            "false_positive": fp, "false_negative": fn, "undecidable_count": undecidable,
            "items": items,
            "ngram_denominator_count": sum(item["ngram_denominator_count"] for item in items),
            "model_calls": 0, "corpus_count": len(corpus),
            "corpus_sha256": freeze.get("corpus_sha256"),
            "calibration_source_digest": freeze["calibration_source_digest"],
            "rule_source_digest": freeze["rule_source_digest"],
            **({key: freeze[key] for key in ("false_positive_upper_bound", "false_negative_upper_bound",
                                              "undecidable_lower_bound", "undecidable_upper_bound")
               if key in freeze})}


def _load(value):
    return json.load(sys.stdin) if value is None else json.loads(value)


def _sealed_duplication_inputs(payload):
    corpus_path = REPOSITORY_CORPUS
    freeze_path = REPOSITORY_FREEZE
    if isinstance(payload, dict):
        if payload.get("freeze") not in (None, str(freeze_path), str(freeze_path.resolve())):
            raise BridgeError("freeze-path-forbidden", "only the repository freeze is accepted")
        if isinstance(payload.get("corpus"), str) and payload["corpus"] not in (str(corpus_path), str(corpus_path.resolve())):
            raise BridgeError("corpus-path-forbidden", "only the repository corpus is accepted")
        if isinstance(payload.get("corpus"), list):
            expected = json.loads(REPOSITORY_CORPUS.read_text(encoding="utf-8"))
            if payload["corpus"] != expected:
                raise BridgeError("corpus-override-forbidden", "only the repository corpus is accepted")
    try:
        corpus_bytes = corpus_path.read_bytes()
        freeze_bytes = freeze_path.read_bytes()
        corpus = json.loads(corpus_bytes)
        freeze = json.loads(freeze_bytes)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise BridgeError("freeze-unavailable", "repository corpus/freeze cannot be read") from exc
    if freeze.get("corpus_sha256") != hashlib.sha256(corpus_bytes).hexdigest():
        raise BridgeError("freeze-digest-mismatch", "corpus digest differs from freeze")
    if freeze.get("normalize_source_digest") != NORMALIZE_SOURCE_DIGEST:
        raise BridgeError("freeze-digest-mismatch", "normalizer digest differs from freeze")
    if freeze.get("rule_source_digest") != RULE_SOURCE_DIGEST:
        raise BridgeError("freeze-digest-mismatch", "rule digest differs from freeze")
    if freeze.get("corpus_count") != len(corpus):
        raise BridgeError("freeze-count-mismatch", "corpus count differs from freeze")
    for key in ("calibration_source_digest", "false_positive", "false_negative",
                "undecidable_count", "false_positive_upper_bound",
                "false_negative_upper_bound", "undecidable_lower_bound",
                "undecidable_upper_bound", "duplication_rule_version", "normalize_version"):
        if key not in freeze:
            raise BridgeError("freeze-provenance-missing", f"freeze field missing: {key}")
    return corpus, freeze, hashlib.sha256(freeze_bytes).hexdigest()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in SUBCOMMANDS:
        raise BridgeError("invalid-command", "command must be parse, resolve, or duplication-check")
    command = argv[0]
    if command == "parse":
        payload = _load(argv[1] if len(argv) > 1 else None)
        result = [parse_record(item) for item in payload] if isinstance(payload, list) else parse_record(payload)
    elif command == "resolve":
        result = resolve(_load(argv[1] if len(argv) > 1 else None))
    else:
        payload = _load(argv[1] if len(argv) > 1 else None)
        if isinstance(payload, list):
            payload = {"corpus": payload}
        if not isinstance(payload, dict):
            raise BridgeError("invalid-request", "duplication-check expects an object")
        corpus, freeze, freeze_digest_before = _sealed_duplication_inputs(payload)
        result = duplication_check(corpus, freeze)
        if hashlib.sha256(REPOSITORY_FREEZE.read_bytes()).hexdigest() != freeze_digest_before:
            raise BridgeError("freeze-mutated", "freeze changed during duplication check")
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except BridgeError as exc:
        json.dump({"error": {"code": exc.code, "detail": exc.detail}}, sys.stdout)
        sys.stdout.write("\n")
        sys.exit(2)
