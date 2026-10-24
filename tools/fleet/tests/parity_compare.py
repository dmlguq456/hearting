"""Cell, hit, VM, and ledger comparisons for reference replay."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.fleet.tests.freeze_explorer import capture, digest, load_reference, sanitize_reference
from tools.fleet.tests.reference_injection import extract_view_model, inject_view_model


def diff_view_models(expected, actual):
    """Return ordered path-level differences; lists remain ordered and are never key-sorted."""
    differences = []
    def walk(path, left, right):
        if type(left) is not type(right):
            differences.append({"path": path, "expected": left, "actual": right})
        elif isinstance(left, dict):
            for key in sorted(set(left) | set(right)):
                if key not in left or key not in right:
                    differences.append({"path": f"{path}/{key}", "expected": left.get(key),
                                        "actual": right.get(key)})
                else:
                    walk(f"{path}/{key}", left[key], right[key])
        elif isinstance(left, (list, tuple)):
            if len(left) != len(right):
                differences.append({"path": path + "/length", "expected": len(left), "actual": len(right)})
            for index, (a, b) in enumerate(zip(left, right)):
                walk(f"{path}/{index}", a, b)
        elif left != right:
            differences.append({"path": path, "expected": left, "actual": right})
    walk("", expected, actual)
    return differences


_MISSING = object()

# Which views draw each view-model section; used to label ledger rows.
SECTION_STATES = {
    "sessions": ["OVERVIEW", "session-page"], "routes": ["ROUTES", "route-page"],
    "nodes": ["route-page"], "repos": ["OVERVIEW", "repo-page"], "questions": ["QUESTIONS"],
    "answered": ["QUESTIONS"], "seats": ["HISTORY"], "generations": ["HISTORY"],
    "cards": ["HISTORY"], "peer_nodes": ["PEERS"], "peer_edges": ["PEERS"],
    "compute": ["COMPUTE"], "memory": ["MEMORY"], "system": ["SYSTEM"], "events": ["OVERVIEW"],
    "usage": ["OVERVIEW"], "hosts": ["OVERVIEW"], "models": ["MODELS"], "attempts": ["session-page"],
}


def _ident(row, *names):
    for name in names:
        if isinstance(row, dict) and row.get(name) is not None:
            return str(row[name])
    return None


def _ordinal_keys(pairs):
    """Disambiguate repeated keys with an occurrence suffix so no row is silently merged."""
    seen, out = {}, []
    for key, value in pairs:
        count = seen.get(key, 0)
        seen[key] = count + 1
        out.append((key if count == 0 else f"{key}#{count}", value))
    return out


def _alias_route_rows(expected_rows, actual_rows):
    """Resolve the reference's hand-chosen route aliases to route ids through source facts.

    A reference row matches a live row on the same session tag plus overlapping stage names;
    each live row is claimed once. The mapping and its evidence are returned for the ledger.
    """
    def stages(row):
        return [str(item.get("name")) for item in row.get("stages") or () if isinstance(item, dict)]
    crosswalk, claimed = {}, set()
    scored = []
    for exp in expected_rows:
        for act in actual_rows:
            if exp.get("session_tag") != act.get("session_tag"):
                continue
            left, right = set(stages(exp)), set(stages(act))
            if not left or not right:
                continue
            score = len(left & right) / len(left | right)
            if score >= 0.5:
                scored.append((score, exp["id"], act["id"]))
    for score, alias, route_id in sorted(scored, key=lambda item: (-item[0], item[1], item[2])):
        if alias in crosswalk or route_id in claimed:
            continue
        crosswalk[alias] = {"route_id": route_id, "stage_overlap": round(score, 3),
                            "evidence": "same session tag; shared stage names"}
        claimed.add(route_id)
    return crosswalk


def _section_pairs(section, expected, actual, crosswalk):
    """Align one section's rows by identity. Returns (ordered key pairs, left, right)."""
    def pairs(rows, keyfn):
        return _ordinal_keys([(keyfn(row), row) for row in rows])
    if section == "routes":
        alias = {value["route_id"]: key for key, value in crosswalk.items()}
        left = pairs(expected, lambda r: r["id"])
        right = _ordinal_keys([(alias.get(r["id"], r["id"]), r) for r in actual])
        return left, right
    if section == "nodes":
        left = list(expected.items())
        alias = {value["route_id"]: key for key, value in crosswalk.items()}
        right = [(alias.get(key, key), value) for key, value in actual.items()]
        return left, right
    keyers = {
        "sessions": lambda r: r["tag"], "repos": lambda r: r["id"], "hosts": lambda r: r["name"],
        "usage": lambda r: r["key"], "questions": lambda r: r["tag"],
        "answered": lambda r: r[0], "seats": lambda r: r[0], "peer_nodes": lambda r: r[0],
        "compute": lambda r: r[0], "memory": lambda r: r[0],
        "events": lambda r: f"{r[2]}/{r[3]}",
    }
    keyer = keyers.get(section)
    if keyer is not None:
        return pairs(expected, keyer), pairs(actual, keyer)
    return None


def _walk_keyed(path, left, right, out):
    if left is _MISSING or right is _MISSING:
        out.append((path, left, right))
        return
    if isinstance(left, dict) and isinstance(right, dict):
        for key in left:
            _walk_keyed(f"{path}/{key}", left[key], right.get(key, _MISSING), out)
        for key in right:
            if key not in left:
                _walk_keyed(f"{path}/{key}", _MISSING, right[key], out)
        return
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        keyed = (left and right and all(isinstance(i, dict) for i in (*left, *right))
                 and all(_ident(i, "name", "n", "id") is not None for i in (*left, *right)))
        if keyed:
            lp = _ordinal_keys([(_ident(i, "name", "n", "id"), i) for i in left])
            rp = _ordinal_keys([(_ident(i, "name", "n", "id"), i) for i in right])
            _walk_pairs(path, lp, rp, out)
            return
        if len(left) != len(right):
            out.append((path + "/length", len(left), len(right)))
        for index, (a, b) in enumerate(zip(left, right)):
            _walk_keyed(f"{path}/{index}", a, b, out)
        return
    unknown = (None, "", "·")             # the reference spells an unknown value `·`, `""` or nothing
    if left in unknown and right in unknown and not isinstance(left, bool) and not isinstance(right, bool):
        return
    numeric = (int, float)
    if (isinstance(left, numeric) and isinstance(right, numeric)
            and not isinstance(left, bool) and not isinstance(right, bool)):
        if left != right:
            out.append((path, left, right))
        return
    if left != right or type(left) is not type(right):
        out.append((path, left, right))


def _walk_pairs(path, left_pairs, right_pairs, out):
    right_map = dict(right_pairs)
    left_map = dict(left_pairs)
    for key, value in left_pairs:
        _walk_keyed(f"{path}/{key}", value, right_map.get(key, _MISSING), out)
    for key, value in right_pairs:
        if key not in left_map:
            _walk_keyed(f"{path}/{key}", _MISSING, value, out)
    common_left = [key for key, _ in left_pairs if key in right_map]
    common_right = [key for key, _ in right_pairs if key in left_map]
    if common_left != common_right:
        out.append((path + "/·order", common_left, common_right))


def _link_key(row):
    ends = [row[0][0] if isinstance(row[0], (list, tuple)) else row[0],
            row[1][0] if isinstance(row[1], (list, tuple)) else row[1]]
    return f"{ends[0]}→{ends[1]}/{row[2]}"


def _peer_pairs(expected, actual):
    left = _ordinal_keys([(_link_key(r), r) for r in expected.get("links", ())])
    right = _ordinal_keys([(_link_key(r), r) for r in actual.get("links", ())])
    return left, right


def _derived_route_words(route, nodes):
    """The three words fleet2's route page draws when a record carries no ``detail_*`` of its own."""
    from tools.fleet.explorer.pages import _route_after
    legs = tuple(route.get("legs") or ())
    if any(node.get("kind") == "gate" for node in nodes):
        leg_count = sum(1 for item in legs if str(item[0]) != "frame")
        trailer = f"{route.get('age') or '·'} · {leg_count} leg · human gate answered"
    else:
        trailer = f"{route.get('age') or '·'} · {len(legs)} leg{'s' if len(legs) != 1 else ''}"
    return {"detail_title": route.get("title"), "detail_trailer": trailer,
            "detail_after": _route_after(route, tuple(nodes)) or None}


def _without_derived_words(expected):
    """Drop the reference's ``detail_*`` words that equal what the route page derives from the same
    record: the live VM draws them from title/age/legs/nodes, so the fields carry no extra fact and
    any real difference in those sources still shows on its own row."""
    routes = expected.get("routes")
    if not isinstance(routes, list):
        return expected
    nodes = expected.get("nodes") or {}
    trimmed = []
    for route in routes:
        if not isinstance(route, dict) or not any(key in route for key in _DETAIL_WORDS):
            trimmed.append(route)
            continue
        derived = _derived_route_words(route, nodes.get(route.get("id")) or ())
        trimmed.append({key: value for key, value in route.items()
                        if key not in _DETAIL_WORDS or (value or None) != derived[key]})
    return dict(expected, routes=trimmed)


_DETAIL_WORDS = ("detail_title", "detail_trailer", "detail_after")


def diff_view_models_keyed(expected, actual):
    """Identity-aligned VM differences: (section path, expected, actual) triples.

    Lists are matched by the entity they describe (session tag, route id through the alias
    crosswalk, repo id, host+gpu, peer link endpoints, memory project then record). A pure
    reordering is recorded once as ``·order`` instead of cascading into every position.
    Returns (differences, crosswalk).
    """
    out = []
    expected = _without_derived_words(expected)
    crosswalk = _alias_route_rows(expected.get("routes") or (), actual.get("routes") or ())
    for section in sorted(set(expected) | set(actual)):
        if section.startswith("_"):
            continue
        left, right = expected.get(section), actual.get(section)
        if left is None or right is None or left == right:
            if left != right and not (left in (None, [], {}) and right in (None, [], {})):
                out.append((f"/{section}", left if left is not None else _MISSING,
                            right if right is not None else _MISSING))
            continue
        if isinstance(left, list) and isinstance(right, list):
            aligned = _section_pairs(section, left, right, crosswalk)
            if aligned is None:
                _walk_keyed(f"/{section}", left, right, out)
                continue
            if section == "compute":
                _compute_pairs(f"/{section}", aligned, out)
            elif section == "memory":
                _memory_pairs(f"/{section}", aligned, out)
            elif section == "answered":
                _answered_pairs(f"/{section}", aligned, out)
            else:
                _walk_pairs(f"/{section}", *aligned, out)
        elif section in ("nodes",):
            _walk_pairs(f"/{section}", *_section_pairs(section, left, right, crosswalk), out)
        elif section == "peer_edges":
            _walk_pairs("/peer_edges/links", *_peer_pairs(left, right), out)
            rest_l = {k: v for k, v in left.items() if k != "links"}
            rest_r = {k: v for k, v in right.items() if k != "links"}
            _walk_keyed("/peer_edges", rest_l, rest_r, out)
        elif section in ("generations", "cards"):
            _walk_keyed(f"/{section}", left, right, out)
        else:
            _walk_keyed(f"/{section}", left, right, out)
    return _collapse_live_only(out), crosswalk


def _collapse_live_only(out):
    """Rows only the live VM has, under one parent (a project's memory, a repo's answers, the link
    table, the feed), become one ``·live-only`` entry: a pure surplus is one fact, not one per row."""
    groups, result = {}, []
    for path, left, right in out:
        parts = path.split("/")
        section = parts[1] if len(parts) > 1 else ""
        parent = None
        if left is _MISSING and right is not _MISSING and not path.endswith(("/·order", "/length")):
            if section in ("memory", "answered") and len(parts) >= 4:
                parent = "/".join(parts[:3])
            elif section == "events" and len(parts) >= 3:
                parent = "/events"
            elif path.startswith("/peer_edges/links/"):
                parent = "/peer_edges/links"
        if parent is None:
            result.append((path, left, right))
        elif parent in groups:
            groups[parent].append(right)
        else:
            groups[parent] = [right]
            result.append((parent + "/·live-only", _MISSING, groups[parent]))
    return result


def _compute_pairs(path, aligned, out):
    left, right = aligned
    right_map = dict(right)
    for key, row in left:
        other = right_map.get(key, _MISSING)
        if other is _MISSING:
            out.append((f"{path}/{key}", row, _MISSING))
            continue
        for index in range(len(row) - 1):
            _walk_keyed(f"{path}/{key}/{index}", row[index],
                        other[index] if index < len(other) else _MISSING, out)
        for gpu, gpu_row in enumerate(row[-1]):
            other_gpu = other[-1][gpu] if gpu < len(other[-1]) else _MISSING
            _walk_keyed(f"{path}/{key}/gpu{gpu}", gpu_row, other_gpu, out)
        for gpu in range(len(row[-1]), len(other[-1])):
            out.append((f"{path}/{key}/gpu{gpu}", _MISSING, other[-1][gpu]))
    left_keys = {key for key, _ in left}
    for key, row in right:
        if key not in left_keys:
            out.append((f"{path}/{key}", _MISSING, row))
    if [k for k, _ in left if k in right_map] != [k for k, _ in right if k in left_keys]:
        out.append((path + "/·order", [k for k, _ in left if k in right_map],
                    [k for k, _ in right if k in left_keys]))


def _record_pairs(rows):
    return _ordinal_keys([(f"{row[0]}/{row[2]}", row) for row in rows])


def _memory_pairs(path, aligned, out):
    left, right = aligned
    right_map = dict(right)
    for key, row in left:
        other = right_map.get(key, _MISSING)
        if other is _MISSING:
            out.append((f"{path}/{key}", row, _MISSING))
        else:
            _walk_pairs(f"{path}/{key}", _record_pairs(row[1]), _record_pairs(other[1]), out)
    left_keys = {key for key, _ in left}
    for key, row in right:
        if key not in left_keys:
            out.append((f"{path}/{key}", _MISSING, row))
    if [k for k, _ in left if k in right_map] != [k for k, _ in right if k in left_keys]:
        out.append((path + "/·order", [k for k, _ in left if k in right_map],
                    [k for k, _ in right if k in left_keys]))


def _answered_pairs(path, aligned, out):
    left, right = aligned
    right_map = dict(right)
    for key, row in left:
        other = right_map.get(key, _MISSING)
        if other is _MISSING:
            out.append((f"{path}/{key}", row, _MISSING))
        else:
            _walk_pairs(f"{path}/{key}", _ordinal_keys([(r[0], r) for r in row[1]]),
                        _ordinal_keys([(r[0], r) for r in other[1]]), out)
    left_keys = {key for key, _ in left}
    for key, row in right:
        if key not in left_keys:
            out.append((f"{path}/{key}", _MISSING, row))
    if [k for k, _ in left if k in right_map] != [k for k, _ in right if k in left_keys]:
        out.append((path + "/·order", [k for k, _ in left if k in right_map],
                    [k for k, _ in right if k in left_keys]))


def _hits(frame):
    hits = frame.get("hits", ()) if isinstance(frame, dict) else getattr(frame, "hits", ())
    return [[hit.y, hit.x0, hit.x1, list(hit.action)] if hasattr(hit, "y") else list(hit)
            for hit in hits]


def _cells(frame):
    cells = frame.get("cells", ()) if isinstance(frame, dict) else getattr(frame, "cells", ())
    return [[list(cell) for cell in row] for row in cells]


def diff_frames(expected, actual, exclusions=()):
    """Report every cell/hit mismatch, with optional (row, start, end) exclusions."""
    differences = []
    left, right = _cells(expected), _cells(actual)
    for y in range(max(len(left), len(right))):
        a = left[y] if y < len(left) else []
        b = right[y] if y < len(right) else []
        for x in range(max(len(a), len(b))):
            if any(ey == y and lo <= x < hi for ey, lo, hi in exclusions):
                continue
            av = a[x] if x < len(a) else None
            bv = b[x] if x < len(b) else None
            if av != bv:
                differences.append({"kind": "cell", "row": y, "column": x,
                                    "expected": av, "actual": bv})
    expected_hits, actual_hits = _hits(expected), _hits(actual)
    if expected_hits != actual_hits:
        differences.append({"kind": "hits", "expected": expected_hits, "actual": actual_hits})
    return differences


def validate_ledger(ledger):
    """Validate difference rows and recompute summary values instead of trusting counters."""
    errors = []
    differences = ledger.get("differences")
    if not isinstance(differences, list):
        return ["differences must be a list"]
    seen = set()
    unclassified = class1 = 0
    for index, row in enumerate(differences):
        if not isinstance(row, dict):
            errors.append(f"differences[{index}] must be an object")
            continue
        ident = row.get("id")
        if not ident or ident in seen:
            errors.append(f"differences[{index}] has missing or duplicate id")
        seen.add(ident)
        if row.get("classification") not in (1, 2, 3):
            unclassified += 1
        if row.get("classification") == 1 and row.get("status") != "fixed":
            class1 += 1
        for required in ("axis", "entity", "field", "states", "widths", "source", "reason"):
            if required not in row:
                errors.append(f"{ident or index}: missing {required}")
        if row.get("classification") == 2 and "production_value" not in row:
            errors.append(f"{ident or index}: class 2 needs production_value")
        if row.get("classification") in (2, 3) and not str(row.get("reason") or "").strip():
            errors.append(f"{ident or index}: class {row['classification']} needs a reason")
    ledger["summary"] = {"unclassified": unclassified, "class1_residue": class1,
                          "differences": len(differences)}
    return errors


_SOURCE_FILES = {"state": "state.json", "history": "history_data.json",
                 "logs": "logs_hist.json", "raw": "raw_f5.json"}


def source_text_map(state, history, logs, raw):
    """Per-file searchable text of the four capture files (for class-2 'no source' checks)."""
    docs = {"state": state, "history": history, "logs": logs, "raw": raw}
    return {_SOURCE_FILES[name]: json.dumps(doc, ensure_ascii=False) for name, doc in docs.items()}


def _leaf_strings(value, minimum=12):
    if isinstance(value, str):
        if len(value) >= minimum and set(value) != {"x"}:
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _leaf_strings(item, minimum)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _leaf_strings(item, minimum)


def source_hits(value, texts):
    """Files in which any long text leaf of ``value`` occurs verbatim (empty: no source)."""
    found = []
    for leaf in _leaf_strings(value):
        for name, text in texts.items():
            if leaf in text and name not in found:
                found.append(name)
    return found


def _dig(value, pointer):
    """The part of ``value`` at a JSON pointer such as ``/0/lines`` (None when absent)."""
    for part in [p for p in pointer.split("/") if p != ""]:
        try:
            value = value[int(part)] if isinstance(value, (list, tuple)) else value[part]
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return value


def _printable(value):
    return "<absent>" if value is _MISSING else value


def _split_path(path):
    parts = path.lstrip("/").split("/")
    entity = "/".join(parts[:2]) if len(parts) > 1 else parts[0]
    field = "/".join(parts[2:]) if len(parts) > 2 else (parts[1] if len(parts) > 1 else parts[0])
    return parts[0], entity, field


def build_live_ledger(expected, actual, *, capture, rules=(), texts=None, fixed=()):
    """Identity-aligned ledger of reference-vs-live differences with rule classification.

    A rule is ``{"match": <regex on the VM path>, "classification": 2|3, "reason": ...,
    "source": [...]}``. Class 2 is refused (the row stays an open class 1) when any long text
    of the reference value occurs in the capture files: a sourced value is an adapter defect.
    """
    import re
    differences, crosswalk = diff_view_models_keyed(expected, actual)
    capture = dict(capture)
    capture["route_id_crosswalk"] = crosswalk
    compiled = [(re.compile(rule["match"]), rule) for rule in rules]
    rows = []
    for path, left, right in differences:
        section, entity, field = _split_path(path)
        row = {"id": f"live:{path}", "axis": "b", "entity": entity, "field": field,
               "states": SECTION_STATES.get(section, [section]), "widths": [140, 168],
               "expected": _printable(left), "actual": _printable(right),
               "source": [{"file": "capture bundle", "pointer": path}],
               "classification": 1, "status": "open",
               "reason": "Observed source-backed field mismatch; adapter fix or classification pending."}
        for pattern, rule in compiled:
            if not pattern.search(path):
                continue
            klass = rule["classification"]
            if klass == 2 and rule.get("check") == "none":
                row["source_checked"] = "no text to look up (numbers/structure); " + rule["reason"][:80]
            elif klass == 2 and texts is not None and left is not _MISSING:
                probed = [_dig(left, pointer) for pointer in rule["probe"]] if rule.get("probe") else [left]
                hits = source_hits(probed, texts)
                if hits:
                    row["reason"] = ("class-2 rule %s refused: reference text occurs in %s"
                                     % (rule.get("id", rule["match"]), ", ".join(hits)))
                    break
                row["source_checked"] = "reference text absent from the four capture files"
            row.update(classification=klass, status="reported" if klass == 2 else "accepted",
                       reason=rule["reason"], rule=rule.get("id"))
            if rule.get("would_come_from"):
                row["would_come_from"] = rule["would_come_from"]
            if rule.get("source"):
                row["source"] = rule["source"]
            row["overlay_value"] = row["expected"]        # what a comparison draw applies over the live value
            if klass == 2:
                row["production_value"] = rule.get("production_value", "·")
            break
        rows.append(row)
    for entry in fixed:
        rows.append(dict(entry, classification=1, status="fixed", axis="b"))
    ledger = {"schema": "fleet-parity-ledger-v1", "capture": capture, "differences": rows}
    errors = validate_ledger(ledger)
    if errors:
        ledger["errors"] = errors
    return ledger


def _purge_reference_modules(*roots):
    """Drop imported modules that live under a reference tree. Two reference trees share helper
    module names, and a helper left over from one tree would feed the other tree's renderer."""
    import sys
    from pathlib import Path as _Path
    resolved = [_Path(root).resolve() for root in roots]
    for name, module in tuple(sys.modules.items()):
        location = getattr(module, "__file__", None)
        if name == "fleet_reference_renderer":
            del sys.modules[name]
        elif location:
            try:
                path = _Path(location).resolve()
            except OSError:
                continue
            if any(root == path.parent or root in path.parents for root in resolved):
                del sys.modules[name]


def _state_args(label):
    if label in ("OVERVIEW", "ROUTES", "QUESTIONS", "HISTORY", "PEERS", "COMPUTE", "MEMORY", "MODELS", "SYSTEM"):
        return {"view": label}
    if label.startswith("repo-page"):
        return {"scope": "repo:hearting"}
    if label.startswith("session-page"):
        return {"scope": "f5"}
    if label.startswith("route-page"):
        return {"scope": "route:f5"}
    if label.startswith("ROUTES-open"):
        return {"view": "ROUTES", "ui": {"ropen:ROUTES": "f5"}}
    if label.startswith("QUESTIONS-fold"):
        return {"view": "QUESTIONS", "ui": {"qshut:QUESTIONS": "bb"}}
    if label.startswith("HISTORY-open"):
        ui = {"hopen:HISTORY": "0c"}
        if "picked-generation" in label:
            ui["gen:f5"] = 0
        return {"view": "HISTORY", "ui": ui}
    if label.startswith("PEERS-pick"):
        return {"view": "PEERS", "ui": {"pfocus:PEERS": "f5"}}
    return {"view": "OVERVIEW"}


def round_trip(args):
    approved, reference = args.approved.resolve(), args.reference.resolve()
    manifest = json.loads((Path(__file__).parent / "fixtures/explorer/manifest.json").read_text())
    baseline = load_reference(approved)
    baseline.path = approved
    sanitize_reference(baseline)
    vm = extract_view_model(baseline)
    states = list(manifest.get("states", ()))
    if args.states == "approved":
        states = states[:16]
    states = [name for name in states if name not in args.except_view]
    results, total = [], 0
    for width in args.widths:
        for name in states:
            expected = capture(approved, width, **_state_args(name))
            candidate = load_reference(reference)
            candidate.path = reference
            inject_view_model(candidate, vm)
            actual = capture(reference, width, renderer=candidate, **_state_args(name))
            diffs = diff_frames(expected, actual)
            total += len(diffs)
            results.append({"state": name, "width": width, "mismatch_count": len(diffs),
                            "mismatches": diffs[:100]})
    output = {"schema": "fleet-round-trip-v1", "approved_sha256": digest(approved / "mock13.py"),
              "reference_sha256": digest(reference / "mock13.py"), "state_count": len(states),
              "widths": args.widths, "diff_count": total, "results": results,
              "claim": "reference extraction/injection round trip; production live VM is not evaluated"}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "round-trip.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(f"round-trip: {len(results)} state-width captures, {total} mismatch records; {args.out / 'round-trip.json'}")
    return 0 if total == 0 else 1


SNAPSHOT_CLOCKS = {
    "snapshot": "state.json: latest compute_hosts.observed_at (the moment `fleet --json` finished)",
    "sections_on_snapshot": ["hosts", "usage", "repos", "sessions", "routes", "nodes", "questions",
                              "answered", "peer_nodes", "peer_edges", "compute", "memory",
                              "system", "events"],
    "detail": ("history_data.json / logs_hist.json / raw_f5.json carry no capture stamp of their "
               "own: their records keep their own absolute times (card `at`, handover `ts`, raw "
               "`read`) and every relative age is computed at the snapshot moment"),
    "timezone": "Asia/Seoul (the reference's absolute clock texts are local KST)",
}


def ledger_markdown(ledger):
    """The user-readable ledger: counts, the class-2 list (what the reference hand-wrote and where it
    would come from) and the class-3 list (each with its reason)."""
    rows = ledger["differences"]
    capture = ledger["capture"]
    counts = {1: 0, 2: 0, 3: 0}
    for row in rows:
        counts[row["classification"]] = counts.get(row["classification"], 0) + 1
    summary = ledger["summary"]
    lines = ["# 22:53 live VM parity ledger", "",
             f"- Capture: `{capture.get('id')}` (sha256 `{str(capture.get('sha256'))[:16]}…`), replayed at "
             f"{capture.get('now')} (`{capture.get('clocks', {}).get('snapshot')}`).",
             f"- Differences: {len(rows)} · class 1 {counts[1]} · class 2 {counts[2]} · class 3 {counts[3]}",
             f"- Unclassified: {summary['unclassified']} · class-1 residue: {summary['class1_residue']}", "",
             "Rows are aligned by identity (session tag, route id through the alias crosswalk, repo id, host+gpu,",
             "peer link endpoints, memory project then record); a pure reordering is one `·order` row.", "",
             "## Which clock each section uses", ""]
    clocks = capture.get("clocks") or {}
    for key in ("snapshot", "detail", "timezone"):
        if clocks.get(key):
            lines.append(f"- **{key}**: {clocks[key]}")
    lines.append(f"- sections on the snapshot clock: {', '.join(clocks.get('sections_on_snapshot', ()))}")
    lines += ["", "## Class 2 - the reference hand-wrote it; the four capture files have no source", "",
              "Production shows `·` (or omits the row) for these. `where it would come from` names the source that",
              "would supply the value if it were ever recorded.", ""]
    by_rule = {}
    for row in rows:
        if row["classification"] == 2:
            by_rule.setdefault(row.get("rule"), []).append(row)
    for rule_id, group in by_rule.items():
        lines.append(f"### {rule_id} ({len(group)})")
        lines.append(f"- why: {group[0]['reason']}")
        if group[0].get("would_come_from"):
            lines.append(f"- where it would come from: {group[0]['would_come_from']}")
        lines.append("- fields: " + ", ".join(f"`{row['id'][5:]}`" for row in group[:8]) +
                     (f" … (+{len(group) - 8})" if len(group) > 8 else ""))
        lines.append("")
    lines += ["## Class 3 - intended or justified differences", ""]
    by_rule = {}
    for row in rows:
        if row["classification"] == 3:
            by_rule.setdefault(row.get("rule"), []).append(row)
    for rule_id, group in by_rule.items():
        lines.append(f"### {rule_id} ({len(group)})")
        lines.append(f"- reason: {group[0]['reason']}")
        lines.append("- fields: " + ", ".join(f"`{row['id'][5:]}`" for row in group[:8]) +
                     (f" … (+{len(group) - 8})" if len(group) > 8 else ""))
        lines.append("")
    fixed = ledger.get("fixed_by_this_wave")
    if fixed:
        lines += ["## Class 1 - fixed in the adapter this wave (no longer a difference)", ""]
        lines += [f"- {item}" for item in fixed]
        lines.append("")
    return "\n".join(lines) + "\n"


def _snapshot_moment(state):
    stamps = [host.get("observed_at") for host in (state.get("compute_hosts") or {}).get("hosts", ())
              if isinstance(host, dict) and isinstance(host.get("observed_at"), (int, float))]
    block = (state.get("compute_hosts") or {}).get("observed_at")
    if isinstance(block, (int, float)):
        stamps.append(block)
    return max(stamps) if stamps else None


def live_ledger(reference, *, rules_path=None, now=None, out=None, previous=None, fixed_notes=None):
    """Replay the four 22:53 files through the real adapter and diff against the reference VM."""
    import os
    import time
    os.environ["TZ"] = "Asia/Seoul"
    time.tzset()
    from tools.fleet.explorer import live
    from tools.fleet.tests.snapshot_fixture import replay_source_bundle
    reference = Path(reference)
    files = {name: json.loads((reference / name).read_text(encoding="utf-8"))
             for name in _SOURCE_FILES.values()}
    moment = float(now) if now is not None else _snapshot_moment(files["state.json"])
    snapshot, detail = replay_source_bundle(reference / "state.json", reference / "history_data.json",
                                            reference / "logs_hist.json", reference / "raw_f5.json",
                                            now=moment)
    actual = live.build_view_model(snapshot, detail, None, now=moment).to_mapping()
    _purge_reference_modules(reference)
    renderer = load_reference(reference)
    renderer.path = reference
    expected = extract_view_model(renderer)
    rules = json.loads(Path(rules_path).read_text(encoding="utf-8")) if rules_path else []
    texts = source_text_map(files["state.json"], files["history_data.json"],
                            files["logs_hist.json"], files["raw_f5.json"])
    digest_input = json.dumps(files, sort_keys=True, ensure_ascii=False).encode("utf-8")
    import hashlib
    capture = {"id": "22:53-source-bundle", "now": moment, "clocks": SNAPSHOT_CLOCKS,
               "sha256": hashlib.sha256(digest_input).hexdigest(),
               "inputs": {name: str(reference / name) for name in _SOURCE_FILES.values()}}
    ledger = build_live_ledger(expected, actual, capture=capture, rules=rules, texts=texts)
    if previous:
        before = json.loads(Path(previous).read_text(encoding="utf-8"))
        now_ids = {row["id"] for row in ledger["differences"]}
        gone = [row["id"] for row in before.get("differences", ()) if row["id"] not in now_ids]
        ledger["baseline"] = {"file": str(previous), "rows": len(before.get("differences", ())),
                              "rows_resolved_by_adapter_fixes": len(gone)}
    if fixed_notes:
        ledger["fixed_by_this_wave"] = json.loads(Path(fixed_notes).read_text(encoding="utf-8"))
    if out is not None:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ledger.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=1) + "\n")
        (out / "ledger.md").write_text(ledger_markdown(ledger), encoding="utf-8")
        (out / "live-vm.json").write_text(json.dumps(
            {"schema": "fleet-live-vm-v1", "now": moment, "actual": actual,
             "expected": {k: v for k, v in expected.items() if not k.startswith("_")}},
            ensure_ascii=False, default=str) + "\n")
    return ledger, expected, actual


def live_ledger_command(args):
    ledger, _expected, _actual = live_ledger(args.reference, rules_path=args.rules, now=args.now,
                                             out=args.out, previous=args.previous, fixed_notes=args.fixed)
    counts = {}
    for row in ledger["differences"]:
        key = (row["classification"], row["status"])
        counts[key] = counts.get(key, 0) + 1
    print("live-ledger:", len(ledger["differences"]), "rows;", dict(sorted(counts.items(), key=str)),
          "summary", ledger["summary"], "errors", len(ledger.get("errors", ())))
    return 0 if ledger["summary"]["unclassified"] == 0 and ledger["summary"]["class1_residue"] == 0 else 1


def _equivalent(left, right):
    """Equal for the ledger's purposes: same number, or both an unknown spelling."""
    unknown = (None, "", "·")
    if left in unknown and right in unknown:
        return True
    numeric = (int, float)
    return (isinstance(left, numeric) and isinstance(right, numeric)
            and not isinstance(left, bool) and not isinstance(right, bool) and left == right)


def overlay_view_model(expected, actual):
    """The live VM with every classified (class 2/3) difference replaced by the reference value.

    Wherever the two sides are equal under the ledger's comparison the live value stays, so a
    cell difference left after the overlay is either a comparison rule that draws differently or a
    renderer difference at an identical VM; it can never hide a class-1 adapter defect.
    """
    def merge(left, right):
        if isinstance(left, dict) and isinstance(right, dict):
            return {key: (merge(value, right[key]) if key in right else value) for key, value in left.items()}
        if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)) and len(left) == len(right):
            return [merge(a, b) for a, b in zip(left, right)]
        return right if _equivalent(left, right) or left == right else left
    return merge(expected, actual)


def _nav_state(label):
    """The NavState a manifest state name stands for (the same table the oracle test uses)."""
    from tools.fleet.explorer.state import NavState
    views = ("OVERVIEW", "ROUTES", "QUESTIONS", "HISTORY", "PEERS", "COMPUTE", "MEMORY", "MODELS", "SYSTEM")
    if label in views:
        return NavState(view=label, title=label) if label != "OVERVIEW" else NavState()
    table = {
        "QUESTIONS-fold-bb": NavState(view="QUESTIONS", title="QUESTIONS", folds=(("qshut:QUESTIONS", "bb"),)),
        "HISTORY-open-0c": NavState(view="HISTORY", title="HISTORY", folds=(("hopen:HISTORY", "0c"),)),
        "HISTORY-open-0c-card": NavState(view="HISTORY", title="HISTORY", folds=(("hopen:HISTORY", "0c"),)),
        "HISTORY-open-0c-picked-generation": NavState(view="HISTORY", title="HISTORY", folds=(
            ("hopen:HISTORY", "0c"), ("ui:gen:f5", "0"))),
        "PEERS-pick-f5": NavState(view="PEERS", title="PEERS", folds=(("pfocus:PEERS", "f5"),)),
        "repo-page": NavState(scope="repo:xxxxxxxx"),         # the scrubbed repo id the goldens use
        "session-page-f5": NavState(scope="session:f5"),
        "route-page-f5": NavState(scope="route:f5"),
        "ROUTES-open-f5": NavState(view="ROUTES", folds=(("ropen:ROUTES", "f5"),)),
    }
    return table[label]


STATE_SECTIONS = {
    "OVERVIEW": ("sessions", "repos", "hosts", "usage", "compute", "events", "routes"),
    "ROUTES": ("routes", "nodes"), "QUESTIONS": ("questions", "answered"),
    "HISTORY": ("seats", "generations", "cards"), "PEERS": ("peer_nodes", "peer_edges", "sessions", "routes"),
    "COMPUTE": ("compute", "system"), "MEMORY": ("memory",), "MODELS": ("models",), "SYSTEM": ("system",),
    "repo-page": ("repos", "sessions", "routes"), "session-page": ("sessions", "seats", "generations", "cards"),
    "route-page": ("routes", "nodes"),
}


def _sections_of(label):
    for prefix in sorted(STATE_SECTIONS, key=len, reverse=True):
        if label.startswith(prefix):
            return STATE_SECTIONS[prefix]
    return ()


def cell_diff_summary(reference, approved, ledger, expected, actual, widths=(140, 168), rows=45):
    """See ``cell_diffs_command``; ``expected`` is the width-scrubbed hand VM (the one the checked
    fixture and goldens use), so both sides of every cell comparison are public-safe."""
    """Draw fleet2 from the overlay of the live VM and compare it with the reference drawn from the
    hand VM, for every manifest state; every cell difference is tied to the ledger rows of the
    sections that state draws (or flagged as unexplained)."""
    from tools.fleet.explorer.frame import draw
    from tools.fleet.explorer.view_model import ExplorerViewModel
    manifest = json.loads((Path(__file__).parent / "fixtures/explorer/manifest.json").read_text())
    overlay = overlay_view_model(expected, actual)
    for key in list(overlay):
        if key.startswith("_"):
            overlay.pop(key)
    vm = ExplorerViewModel.from_mapping(overlay)
    by_section = {}
    for row in ledger["differences"]:
        if row["classification"] in (2, 3):
            by_section.setdefault(row["id"].split("/")[1], []).append(row["id"])
    results, unexplained = [], 0
    for width in widths:
        for label in manifest["states"]:
            try:
                _purge_reference_modules(approved, reference)
                args = _state_args(label)
                if label == "repo-page":
                    args["scope"] = "repo:xxxxxxxx"               # the scrubbed repo id, as the goldens use
                reference_frame = capture(approved, width, scrub=True, **args)
            except Exception as exc:                      # a state the reference cannot draw
                results.append({"state": label, "width": width, "error": f"reference: {type(exc).__name__}: {exc}"})
                continue
            try:
                frame = draw(vm, _nav_state(label), width, rows, beat=1)
            except Exception as exc:
                results.append({"state": label, "width": width, "error": f"fleet2: {type(exc).__name__}: {exc}"})
                continue
            cells = [[list(cell) for cell in row] for row in frame.cells]
            actual_frame = {"cells": cells, "hits": [[h.y, h.x0, h.x1, list(h.action)] for h in frame.hits]}
            expected_frame = {"cells": [[list(cell) for cell in row] for row in reference_frame["cells"]],
                              "hits": [[h.y, h.x0, h.x1, list(h.action)] if hasattr(h, "y")
                                       else [h[0], h[1], h[2], list(h[3])]
                                       for h in reference_frame.get("hits", ())]}
            diffs = diff_frames(expected_frame, actual_frame)
            cell_diffs = [d for d in diffs if d["kind"] == "cell"]
            hit_diff = any(d["kind"] == "hits" for d in diffs)
            ledger_ids = [i for section in _sections_of(label) for i in by_section.get(section, ())]
            row = {"state": label, "width": width, "cell_diffs": len(cell_diffs), "hit_diff": hit_diff,
                   "ledger_rows_for_drawn_sections": len(ledger_ids),
                   "first_cell_diffs": cell_diffs[:5]}
            if cell_diffs or hit_diff:
                row["explained_by"] = ledger_ids[:12]
                if not ledger_ids:
                    unexplained += 1
                    row["unexplained"] = True
            results.append(row)
    return {"schema": "fleet-cell-diff-summary-v1", "widths": list(widths), "rows": rows,
            "overlay": "live VM with every class 2/3 ledger value replaced by the reference value",
            "states": len(manifest["states"]), "unexplained_states": unexplained, "results": results}


def cell_diffs_command(args):
    ledger, _private, actual = live_ledger(args.reference, rules_path=args.rules, now=args.now)
    _purge_reference_modules(args.approved, args.reference)
    renderer = load_reference(args.approved)
    renderer.path = args.approved
    scrubbed = extract_view_model(renderer, scrub=True)          # the hand VM as the goldens carry it
    summary = cell_diff_summary(args.reference, args.approved, ledger, scrubbed, actual, tuple(args.widths))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "2253-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    bad = [r for r in summary["results"] if r.get("cell_diffs") or r.get("hit_diff") or r.get("error")]
    print(f"cell-diffs: {len(summary['results'])} state-width draws, {len(bad)} with differences or errors, "
          f"{summary['unexplained_states']} unexplained; {args.out / '2253-summary.json'}")
    return 0 if not summary["unexplained_states"] and not any(r.get("error") for r in summary["results"]) else 1


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    rt = sub.add_parser("round-trip")
    rt.add_argument("--approved", type=Path, required=True)
    rt.add_argument("--reference", type=Path, required=True)
    rt.add_argument("--widths", type=lambda s: [int(x) for x in s.split(",")], default=[140, 168])
    rt.add_argument("--rows", type=int, default=45)
    rt.add_argument("--states", choices=("approved", "all"), default="approved")
    rt.add_argument("--except-view", action="append", default=[])
    rt.add_argument("--out", type=Path, required=True)
    rt.set_defaults(func=round_trip)
    cd = sub.add_parser("cell-diffs")
    cd.add_argument("--reference", type=Path, required=True)
    cd.add_argument("--approved", type=Path, required=True)
    cd.add_argument("--rules", type=Path)
    cd.add_argument("--now", type=float)
    cd.add_argument("--widths", type=lambda s: [int(x) for x in s.split(",")], default=[140, 168])
    cd.add_argument("--out", type=Path, required=True)
    cd.set_defaults(func=cell_diffs_command)
    ll = sub.add_parser("live-ledger")
    ll.add_argument("--reference", type=Path, required=True)
    ll.add_argument("--rules", type=Path)
    ll.add_argument("--now", type=float)
    ll.add_argument("--previous", type=Path)
    ll.add_argument("--fixed", type=Path)
    ll.add_argument("--out", type=Path, required=True)
    ll.set_defaults(func=live_ledger_command)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
