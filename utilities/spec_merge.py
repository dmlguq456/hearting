"""D-122 deterministic spec merge. Pure bytes in/out; no filesystem authority.

The publisher owns manifests, ancestry, component deletion and CAS. This module
only combines independently changed units; it never manufactures review text.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import PurePosixPath
import re

POLICY_VERSION = "spec-merge-v1"
RECEIPT = "_internal/shared-base.json"


class MergeConflict(ValueError):
    def __init__(self, conflicts):
        self.conflicts = sorted(conflicts, key=lambda row: (
            row["path"], tuple(row["section"]), row["reason"]))
        self.code = "spec-merge-conflict"
        super().__init__(json.dumps(self.conflicts, ensure_ascii=False, sort_keys=True))


def _fail(path, section, reason):
    raise MergeConflict([{"path": path, "section": list(section), "reason": reason}])


def _sha(value):
    return hashlib.sha256(value).hexdigest() if value is not None else None


def _evidence(path, section, kind, decision, base, ours, latest, merged):
    return {"path": path, "section": list(section), "kind": kind, "decision": decision,
            "base_sha256": _sha(base), "ours_sha256": _sha(ours),
            "latest_sha256": _sha(latest), "merged_sha256": _sha(merged)}


def _choose(path, section, base, ours, latest):
    if ours == latest:
        return ours, "coalesced"
    if ours == base:
        return latest, "latest"
    if latest == base:
        return ours, "ours"
    _fail(path, section, "delete-versus-modify" if ours is None or latest is None
          else "same-unit-changed")


def _decode(path, value):
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        _fail(path, (), "text-encoding-unsupported")


def _is_snapshot(path):
    parts = PurePosixPath(path).parts
    return any(parts[i:i + 2] == ("_internal", "versions")
               for i in range(len(parts) - 2))


def _join_units(path, section, chunks):
    """Keep original bytes, refusing a concatenation that erases a line boundary."""
    result = []
    for chunk in chunks:
        if not chunk:
            continue
        if (result and not result[-1].endswith((b"\n", b"\r"))
                and not chunk.startswith((b"\n", b"\r"))):
            _fail(path, section, "unit-boundary-no-newline")
        result.append(chunk)
    return b"".join(result)


def _merge_order(path, parent, base, ours, latest, retained):
    """Union sibling order constraints. Ties are sorted by heading/key bytes."""
    edges = {key: set() for key in retained}
    indegree = dict.fromkeys(retained, 0)
    for order in (base, ours, latest):
        order = [key for key in order if key in retained]
        for left, right in zip(order, order[1:]):
            if right not in edges[left]:
                edges[left].add(right)
                indegree[right] += 1
    result = []
    ready = sorted(key for key in retained if indegree[key] == 0)
    while ready:
        current = ready.pop(0)
        result.append(current)
        for successor in sorted(edges[current]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
                ready.sort()
    if len(result) != len(retained):
        _fail(path, parent, "unit-order-ambiguous")
    return result


_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _markdown(path, data):
    text = _decode(path, data)
    nodes = {}
    children = defaultdict(list)
    stack = []
    key = ()
    current = []
    fence = None
    previous = ""
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*", raw):
                fence = None
            current.append(line)
            previous = raw
            continue
        opening = _FENCE.match(raw)
        if opening and not (opening[1][0] == "`" and "`" in opening[2]):
            fence = (opening[1][0], len(opening[1]))
            current.append(line)
            previous = raw
            continue
        # Setext/multiline headings need a distinct parser; do not mistake
        # them for independently mergeable preamble lines.
        if re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", raw) and previous.strip():
            _fail(path, key, "markdown-setext-unsupported")
        match = _HEADING.match(raw)
        if not match:
            current.append(line)
            previous = raw
            continue
        nodes[key] = "".join(current).encode("utf-8")
        level = len(match[1])
        title = re.sub(r"[ \t]+#+[ \t]*$", "", match[2] or "").strip()
        if not title:
            _fail(path, key, "markdown-heading-empty")
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else ()
        key = (*parent, "#" * level + " " + title)
        if key in nodes or key in children[parent]:
            _fail(path, key, "markdown-heading-duplicate")
        children[parent].append(key)
        stack.append((level, key))
        current = [line]
        previous = raw
    nodes[key] = "".join(current).encode("utf-8")
    if fence:
        _fail(path, key, "markdown-fence-unclosed")
    return nodes, children


def _preamble_tokens(data):
    # Plain wrapped prose is one paragraph, never merged sentence fragments.
    # Metadata/list lines and blank separators are independently addressable.
    tokens, paragraph = [], []
    def flush():
        if paragraph:
            tokens.append("".join(paragraph))
            paragraph.clear()
    for line in data.decode("utf-8").splitlines(keepends=True):
        if not line.strip() or re.match(r"\s*(?:>|[-*+]\s|[A-Za-z_][\w -]*:)", line):
            flush()
            tokens.append(line)
        else:
            paragraph.append(line)
    flush()
    return tokens


def _merge_preamble(path, base, ours, latest):
    if ours == latest or ours == base or latest == base:
        return _choose(path, (), base, ours, latest)
    original = _preamble_tokens(base)
    changes = []
    for version in (ours, latest):
        tokens = _preamble_tokens(version)
        branch = [(a, b, tuple(tokens[c:d])) for tag, a, b, c, d in
                  SequenceMatcher(a=original, b=tokens, autojunk=False).get_opcodes()
                  if tag != "equal"]
        for edit in branch:
            if edit in changes:
                continue
            for other in changes:
                a, b, _ = edit
                c, d, _ = other
                overlap = (max(a, c) < min(b, d) or a == b == c == d
                           or (a == b and c <= a < d) or (c == d and a <= c < b))
                if overlap:
                    _fail(path, (), "metadata-text-overlap")
            changes.append(edit)
    result, cursor = [], 0
    for start, end, replacement in sorted(changes):
        result.extend(original[cursor:start])
        result.extend(replacement)
        cursor = end
    result.extend(original[cursor:])
    return _join_units(path, (), (token.encode("utf-8") for token in result)), "merged"


_BLUEPRINT = re.compile(r"^## ✦ 청사진 요약 ⟨v(?:0|[1-9][0-9]*), ([0-9]{4}-[0-9]{2}-[0-9]{2})⟩$")


def _blueprint_heading(key):
    match = _BLUEPRINT.fullmatch(key[-1]) if key else None
    if match is None:
        return False
    try:
        date.fromisoformat(match[1])
        return True
    except ValueError:
        return False


def _align_blueprint(path, parsed):
    """Align one existing PRD summary, keeping its actual header/body bytes.

    Other titles, moves to another parent, new summaries and ambiguous matches
    retain the original Markdown rules. No numbers or dates are synthesized.
    """
    if PurePosixPath(path).name != "prd.md":
        return parsed, None
    matches = [[key for key in nodes if _blueprint_heading(key)] for nodes, _ in parsed]
    if len(matches[0]) != 1 or any(len(keys) > 1 for keys in matches):
        return parsed, None
    target = matches[0][0]
    if (any(keys and keys[0][:-1] != target[:-1] for keys in matches)
            or not any(keys and keys[0] != target for keys in matches)):
        return parsed, None
    aligned = []
    for (nodes, children), keys in zip(parsed, matches):
        source = keys[0] if keys else None
        def key_for(key):
            return target + key[len(source):] if source and key[:len(source)] == source else key
        aligned.append(({key_for(key): value for key, value in nodes.items()},
                        defaultdict(list, {key_for(parent): [key_for(key) for key in order]
                                           for parent, order in children.items()})))
    return aligned, target


def _merge_blueprint(path, key, base, ours, latest):
    if any(value is None for value in (base, ours, latest)):
        return _choose(path, key, base, ours, latest)
    parts = []
    for value in (base, ours, latest):
        heading, separator, body = value.partition(b"\n")
        parts.append((heading + separator, body))
    heading, _ = _choose(path, key, *(part[0] for part in parts))
    body, _ = _choose(path, key, *(part[1] for part in parts))
    merged = _join_units(path, key, (heading, body))
    # Keep the ordinary evidence decision whenever the entire unit agrees.
    if ours == latest or ours == base or latest == base:
        return _choose(path, key, base, ours, latest)
    return merged, "merged"


def _markdown_changes_covered(path, base, ours, published):
    parsed = [_markdown(path, data) for data in (base, ours, published)]
    parsed, blueprint = _align_blueprint(path, parsed)
    (bn, _), (on, oc), (pn, pc) = parsed
    def content(key, value):
        if value is None:
            return None
        body = value.partition(b"\n")[2] if key == blueprint else value
        return body.rstrip(b"\r\n")
    for key in set(bn) | set(on):
        before, after = content(key, bn.get(key)), content(key, on.get(key))
        if before != after and content(key, pn.get(key)) != after:
            return False
    # Extra published units may interleave; the adopted units keep their order.
    for parent, order in oc.items():
        retained = set(order) & set(pc[parent])
        if [key for key in order if key in retained] != [key for key in pc[parent] if key in retained]:
            return False
    return True


def changes_covered(base, ours, published):
    """Read-only contract coverage, not a merge or authorization to publish.

    The spec capability defines pipeline state/summary as execution metadata.
    PRD bodies and other contract payloads must be preserved; only the standard
    summary label and trailing blank separator lines are disregarded.
    """
    changed_prd = False
    for path in set(base) | set(ours):
        if path == RECEIPT or "/_internal/versions/" in "/" + path:
            continue
        name = PurePosixPath(path).name
        if name in {"pipeline_state.yaml", "pipeline_summary.md"}:
            continue
        before, after, actual = base.get(path), ours.get(path), published.get(path)
        if before == after:
            continue
        changed_prd |= name == "prd.md"
        if name == "prd.md" and all(data is not None for data in (before, after, actual)):
            if not _markdown_changes_covered(path, before, after, actual):
                return False
        elif actual != after:
            return False
    return changed_prd


def _merge_markdown(path, base, ours, latest):
    parsed = [_markdown(path, version) for version in (base, ours, latest)]
    parsed, blueprint = _align_blueprint(path, parsed)
    bn, bc = parsed[0]
    on, oc = parsed[1]
    ln, lc = parsed[2]
    for nodes, children in parsed[1:]:
        removed = set(bn) - set(nodes)
        added = set(nodes) - set(bn)
        if removed and added:
            _fail(path, (), "markdown-heading-rename-ambiguous")
        for parent in set(bc) | set(children):
            retained = set(bc[parent]) & set(children[parent])
            if ([key for key in bc[parent] if key in retained]
                    != [key for key in children[parent] if key in retained]):
                _fail(path, parent, "markdown-heading-reordered")
    merged, evidence, errors = {}, [], []
    for key in sorted(set(bn) | set(on) | set(ln)):
        b, o, l = bn.get(key), on.get(key), ln.get(key)
        try:
            value, decision = (_merge_preamble(path, b, o, l) if key == () else
                               _merge_blueprint(path, key, b, o, l) if key == blueprint else
                               _choose(path, key, b, o, l))
        except MergeConflict as exc:
            errors.extend(exc.conflicts)
            continue
        if value is not None:
            merged[key] = value
        if b != o or b != l:
            evidence.append(_evidence(path, key, "markdown", decision, b, o, l, value))
    if errors:
        raise MergeConflict(errors)
    for key in merged:
        if key and key[:-1] not in merged:
            _fail(path, key, "markdown-parent-deleted")
    def render(parent):
        siblings = {key for key in merged if key and key[:-1] == parent}
        order = _merge_order(path, parent, bc[parent], oc[parent], lc[parent], siblings)
        chunks = (_join_units(path, key, (merged[key], render(key))) for key in order)
        return _join_units(path, parent, chunks)
    return _join_units(path, (), (merged.get((), b""), render(()))), evidence


_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]+(.*)|[ \t]*)$")
_VERSION = re.compile(rb"version:[ \t]+(0|[1-9][0-9]*)([ \t]*(?:#[^\r\n]*)?)(\r?\n)?\Z")


def _yaml_plain(path, line, quote=None):
    """Mask quoted scalars/comments before checking structural indirection."""
    plain = []
    i = 0
    while i < len(line):
        char = line[i]
        if quote:
            if char == "\\" and quote == '"':
                i += 2
                continue
            if char == quote:
                if quote == "'" and i + 1 < len(line) and line[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char == "#" and (i == 0 or line[i - 1].isspace()):
            break
        if char in "\"'" and (i == 0 or line[i - 1] in " :[,{\t-"):
            quote = char
            plain.append(" ")
        else:
            plain.append(char)
        i += 1
    result = "".join(plain)
    if re.search(r"(?:^|[\s,[{])(?:[&*!][^\s]|<<\s*:)", result):
        _fail(path, (), "yaml-indirection-unsupported")
    return result, quote


def _yaml(path, data):
    text = _decode(path, data)
    nodes, order, current, key = {}, [], [], ()
    scalar_indent = None
    flow = []
    quote = None
    quote_indent = None
    plain_indent = None
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        indent = len(raw) - len(raw.lstrip(" "))
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            _fail(path, (), "yaml-tab-indentation")
        if scalar_indent is not None:
            if not raw.strip() or indent > scalar_indent:
                current.append(line)
                continue
            scalar_indent = None
        in_quote = quote is not None
        if in_quote and raw.strip() and indent <= quote_indent:
            _fail(path, key, "yaml-multiline-or-unclosed-quote")
        plain, quote = _yaml_plain(path, raw, quote)
        if quote is not None and not in_quote:
            quote_indent = indent
        if any(ord(char) < 32 and char != "\t" for char in raw):
            _fail(path, key, "yaml-control-character")
        in_flow = bool(flow)
        for char in plain:
            if char in "[{":
                flow.append(char)
            elif char in "]}":
                if not flow or flow.pop() != ("[" if char == "]" else "{"):
                    _fail(path, key, "yaml-flow-unbalanced")
        if raw.startswith(("---", "...", "%")):
            _fail(path, (), "yaml-document-directive-unsupported")
        if not raw.strip() or raw.lstrip().startswith("#"):
            current.append(line)
            continue
        if in_quote:
            current.append(line)
            continue
        if in_flow and indent == 0 and not re.fullmatch(r"[\]}]+[ \t]*(?:#.*)?", raw):
            _fail(path, key, "yaml-flow-unbalanced")
        sequence = raw.lstrip().startswith("- ") or raw.strip() == "-"
        if indent or sequence or in_flow:
            if not key:
                _fail(path, (), "yaml-root-not-mapping")
            # Nested mappings, indentationless sequences and folded scalar
            # continuations stay in the same opaque top-level key unit.
            mapping = re.match(r"(?:[A-Za-z_][\w.-]*|[\"']).*?:", raw.lstrip())
            if not (in_flow or mapping or sequence
                    or raw.lstrip().startswith(("[", "]", "{", "}", ","))
                    or (plain_indent is not None and indent > plain_indent)):
                _fail(path, key, "yaml-nested-syntax-unsupported")
            if mapping or sequence:
                plain_indent = indent
        else:
            if in_flow:
                _fail(path, key, "yaml-flow-unbalanced")
            match = _KEY.fullmatch(raw)
            if not match:
                _fail(path, (), "yaml-root-key-unsupported")
            nodes[key] = "".join(current).encode("utf-8")
            key = (match[1],)
            if key in nodes or key in order:
                _fail(path, key, "yaml-key-duplicate")
            order.append(key)
            plain_indent = indent
            current = []
        current.append(line)
        if re.search(r":\s*[|>][1-9+-]*\s*$", plain):
            scalar_indent = indent
    if flow:
        _fail(path, key, "yaml-flow-unbalanced")
    if quote:
        _fail(path, key, "yaml-multiline-or-unclosed-quote")
    nodes[key] = "".join(current).encode("utf-8")
    return nodes, order


def _version_merge(path, base, ours, latest):
    matches = [_VERSION.fullmatch(value) if value is not None else None
               for value in (base, ours, latest)]
    if not all(matches):
        _fail(path, ("version",), "yaml-version-not-decimal")
    numbers = [int(match[1]) for match in matches]
    # Comments/newlines are not numeric metadata; differing edits there do
    # not gain the version exception.
    if (numbers[1] < numbers[0] or numbers[2] < numbers[0]
            or len({(match[2], match[3]) for match in matches}) != 1):
        _fail(path, ("version",), "yaml-version-not-monotone")
    return (ours if numbers[1] >= numbers[2] else latest), "version-max"


def _merge_yaml(path, base, ours, latest):
    parsed = [_yaml(path, value) for value in (base, ours, latest)]
    (bn, bo), (on, oo), (ln, lo) = parsed
    merged, evidence, errors = {}, [], []
    for key in sorted(set(bn) | set(on) | set(ln)):
        b, o, l = bn.get(key), on.get(key), ln.get(key)
        try:
            if key == ("version",) and o != l and o != b and l != b:
                value, decision = _version_merge(path, b, o, l)
            else:
                value, decision = _choose(path, key, b, o, l)
        except MergeConflict as exc:
            errors.extend(exc.conflicts)
            continue
        if value is not None:
            merged[key] = value
        if b != o or b != l:
            evidence.append(_evidence(path, key, "yaml-key", decision, b, o, l, value))
    if errors:
        raise MergeConflict(errors)
    keys = set(merged) - {()}
    order = _merge_order(path, (), bo, oo, lo, keys)
    return _join_units(path, (), [merged.get((), b""), *(merged[key] for key in order)]), evidence


def merge_trees(base: dict[str, bytes], ours: dict[str, bytes], latest: dict[str, bytes]):
    """Return ``(merged, JSON evidence)`` or all safely detected conflicts.

    Input keys are canonical relative POSIX file paths. Input dictionaries and
    bytes are never mutated. The one receipt exception retains exact O bytes.
    """
    for tree in (base, ours, latest):
        for path, data in tree.items():
            if (not isinstance(path, str) or not path or path.startswith("/")
                    or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/"))
                    or not isinstance(data, bytes)):
                _fail(str(path), (), "tree-entry-invalid")
    merged, units, conflicts = {}, [], []
    for path in sorted(set(base) | set(ours) | set(latest)):
        b, o, l = base.get(path), ours.get(path), latest.get(path)
        try:
            if path == RECEIPT:
                value, decision = o, "source-receipt"
            elif _is_snapshot(path) and len({value for value in (b, o, l) if value is not None}) > 1:
                _fail(path, (), "immutable-snapshot-conflict")
            elif o == l or o == b or l == b:
                value, decision = _choose(path, (), b, o, l)
            elif b is not None and o is not None and l is not None and path.endswith(".md"):
                value, details = _merge_markdown(path, b, o, l)
                units.extend(details)
                decision = None
            elif b is not None and o is not None and l is not None and PurePosixPath(path).name == "pipeline_state.yaml":
                value, details = _merge_yaml(path, b, o, l)
                units.extend(details)
                decision = None
            else:
                value, decision = _choose(path, (), b, o, l)
            if value is not None:
                merged[path] = value
            if decision is not None and (b != o or b != l):
                units.append(_evidence(path, (), "file", decision, b, o, l, value))
        except MergeConflict as exc:
            conflicts.extend(exc.conflicts)
    if conflicts:
        raise MergeConflict(conflicts)
    return merged, {"policy_version": POLICY_VERSION,
                    "units": sorted(units, key=lambda row: (row["path"], row["section"], row["kind"]))}
