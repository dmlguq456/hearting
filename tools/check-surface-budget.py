#!/usr/bin/env python3
"""Model-visible surface budget gate.

The nine documents an agent reads to route and dispatch work are the surface
the 2026-09-01 dispatch-complexity diagnosis said must shrink contractually.
Between that diagnosis and 2026-09-09 they grew 23.3% (341,436 -> 420,908
UTF-8 bytes) because nothing refused growth. This gate does.

Contract. Rules are fail-closed; bytes are advisory (2026-09-10, user: gates
that force length can be relaxed a lot -- the reduction is about what an agent
has to hold in mind, which is counted in rules, not kilobytes):

* every surface in ``SURFACES`` must exist, must have a sealed row in
  ``tools/surface-budget.json``, and must stay at or under its directive-count
  cap; going over its byte cap prints an ``ADVISORY`` line and does not fail;
* the budget file may not list a path outside ``SURFACES`` (a stale row would
  silently stop guarding a moved document);
* the byte totals and ``TOTAL_BYTE_CEILING`` are reported the same way --
  advisory; the rule totals and ``TOTAL_DIRECTIVE_CEILING`` still fail;
* ``--reseal`` rewrites caps to the current measurement plus ``HEADROOM``,
  and refuses to raise the sealed *measurement* of any surface unless
  ``--reason`` is given, in which case the raise is recorded in the file's
  ``history``.

Caps carry headroom on purpose. Sealing every surface at its exact size made
each of the nine permanently full: the next edit anywhere failed the gate no
matter how small, so the gate stopped reading as a budget and started reading
as a freeze, and growth landed by skipping it instead. A cap is a budget with
room to spend; what refuses drift is that the room is finite and one reseal
never widens it, since the margin is always taken from the measurement rather
than from the previous cap.

A directive is one match of ``DIRECTIVE_PATTERN`` outside fenced code blocks
and inline code spans.
The count is a footprint measure of rules the model must carry in memory, not
a token or billing estimate (core/ADAPTATION.md §6.1).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import re
import sys
from pathlib import Path

SCHEMA = 3
BUDGET_FILE = "tools/surface-budget.json"
# Room one ordinary edit needs: about a table row of prose, or two rules.
# Applied to the measurement at every reseal, never compounded onto the old
# cap, so repeated reseals cannot ratchet the surface upward.
HEADROOM_BYTES = 0.03
# Percentages are meaningless on counts this small, so directives get a flat
# floor and the percentage only matters for the largest documents.
HEADROOM_DIRECTIVES = 0.03
HEADROOM_DIRECTIVES_MIN = 2


def with_headroom(measured: dict[str, int]) -> dict[str, int]:
    """Cap for one surface: its measurement plus the standing margin."""
    return {
        "bytes": math.ceil(measured["bytes"] * (1 + HEADROOM_BYTES)),
        "directives": measured["directives"] + max(
            HEADROOM_DIRECTIVES_MIN,
            math.ceil(measured["directives"] * HEADROOM_DIRECTIVES),
        ),
    }
# Lower it only in the commit that lands a measured reduction; never raise it
# without a reviewed rationale in that commit.
#   2026-09-09 9c666bc2  420,908  kickoff seal
#   2026-09-09 84215f45  397,795  completion-delivery carriers moved to ADAPTATION §7
#   2026-09-09 7271f92f  361,173  §5.10 SD decision records moved to ADAPTATION §8
#   2026-09-09           362,830  RAISED +1,657: the astra instruction-surface review found four
#                                 rule clauses missing from the §5.10 summaries (SD-67 unproven-
#                                 absence pin, SD-94 owner-closure constraints, SD-64/71/77 evidence
#                                 order, SD-69 commit boundary); restored verbatim from §8.
#   2026-09-10 (this)    375,000  RAISED +12,170: caps now carry HEADROOM over the measurement
#                                 instead of sitting on it. Sealed at exact size, all nine surfaces
#                                 were permanently full — a one-row edit to core/HOOKS.md had to be
#                                 rewritten to 737 bytes against a 738-byte original, and separately
#                                 a +719-byte core/OPERATIONS.md change landed on main leaving the
#                                 gate red, because the only ways past a full budget are to shrink
#                                 the sentence or to skip the gate. The ceiling now sits just above
#                                 the sealed cap sum, so the 3% is free to spend and re-arming
#                                 another 3% still costs a reviewed change here.
TOTAL_BYTE_CEILING = 375_000
# The rule ceiling. Bytes were the first proxy because they are trivial to
# measure, but the thing being reduced is what an agent has to hold in mind
# while routing one dispatch -- and that is counted in rules, not kilobytes.
# The two moved in opposite directions on 2026-09-10: the rewake hook grew
# ~1KB while removing an entire class of rule the operator had to carry (the
# command-text shapes that lost an arm), and `dispatch-owner.py` grew 6.5KB
# while replacing "work out why you were refused" with a named reason. A
# budget that only counted bytes would have scored both as regressions.
#
# Lower it only in the commit that lands a measured reduction; never raise it
# without a reviewed rationale in that commit.
#   2026-09-10 (this)  480  first seal: 447 measured, 468 sealed caps
TOTAL_DIRECTIVE_CEILING = 480
SURFACES: tuple[str, ...] = (
    "core/CORE.md",
    "core/WORKFLOW.md",
    "core/CONVENTIONS.md",
    "core/OPERATIONS.md",
    "core/HOOKS.md",
    "core/MEMORY.md",
    "adapters/claude/CLAUDE.md",
    "capabilities/autopilot-code/references/dev-pipeline.md",
    "capabilities/autopilot-code/references/owner-execution.md",
)
# English imperatives plus the Korean forms the documents actually use.
# Hangul lookarounds are the word boundary: 반드시 is an adverb (반드시성 is
# not a directive); 금지 counts bare or conjugated (금지한다/금지된다/금지함)
# but not as a compound noun head (금지어, 필수금지).
DIRECTIVE_PATTERN = re.compile(
    r"\b(?:must|never|always|shall|do not|don't)\b"
    r"|(?<![가-힣])반드시(?![가-힣])"
    r"|(?<![가-힣])금지(?=[한된됨되함]|(?![가-힣]))"
    r"|해야(?:만)? 한다",
    re.IGNORECASE,
)
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def strip_fenced_code(text: str) -> str:
    """Drop fenced code blocks; an example of printed output is not a rule.

    CommonMark rules: a fence closes only on the same character, at least as
    long as the opener, with nothing but whitespace after it. A shorter fence
    of the same character inside the block is content, not a closer.
    """
    kept: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE_OPEN.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                continue
            kept.append(line)
            continue
        if (
            match
            and match.group(1)[0] == fence[0]
            and len(match.group(1)) >= len(fence)
            and not line[match.end():].strip()
        ):
            fence = None
    return "\n".join(kept)


_INLINE_CODE = re.compile(r"(`+)[^`\n]*?\1")


def strip_inline_code(text: str) -> str:
    """Drop `code spans`; `approval_policy=never` is a value, not a rule."""
    return _INLINE_CODE.sub(" ", text)


def count_directives(text: str) -> int:
    return len(DIRECTIVE_PATTERN.findall(strip_inline_code(strip_fenced_code(text))))


def measure(root: Path) -> dict[str, dict[str, int] | None]:
    """Return {surface: {"bytes": n, "directives": n}} or None when missing."""
    out: dict[str, dict[str, int] | None] = {}
    for rel in SURFACES:
        path = root / rel
        if not path.is_file():
            out[rel] = None
            continue
        raw = path.read_bytes()
        out[rel] = {
            "bytes": len(raw),
            "directives": count_directives(raw.decode("utf-8", errors="replace")),
        }
    return out


def load_budget(path: Path, *, strict_ceiling: bool = True, legacy_ok: bool = False) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    schema = data.get("schema")
    # Reseal is the one path that may read the outgoing schema, because reading
    # it is how the file gets migrated. `check` stays strict so a stale file can
    # never be mistaken for a current seal.
    accepted = {SCHEMA, SCHEMA - 1} if legacy_ok else {SCHEMA}
    if schema not in accepted:
        raise ValueError(f"budget schema must be {SCHEMA}")
    surfaces = data.get("surfaces")
    if not isinstance(surfaces, dict):
        raise ValueError("budget must carry a surfaces object")
    for rel, caps in surfaces.items():
        if not isinstance(caps, dict) or not all(
            isinstance(caps.get(k), int) and caps[k] >= 0 for k in ("bytes", "directives")
        ):
            raise ValueError(f"invalid caps for {rel}: {caps!r}")
    if not isinstance(data.get("total_bytes"), int) or data["total_bytes"] < 0:
        raise ValueError("budget must carry an integer total_bytes cap")
    # Schema 3 added the rule totals. Like the ceiling echo below, they are
    # required of a current seal but not of the outgoing file reseal reads to
    # migrate it -- otherwise the only command that can write the new fields
    # is the one that refuses to run without them.
    if strict_ceiling and (
        not isinstance(data.get("total_directives"), int) or data["total_directives"] < 0
    ):
        raise ValueError("budget must carry an integer total_directives cap")
    # The ceiling is a code constant; the file only echoes it for readers, and
    # an echo that disagrees with the code is a second source of truth.
    if strict_ceiling and data.get("ceiling_bytes") != TOTAL_BYTE_CEILING:
        raise ValueError(
            f"ceiling_bytes {data.get('ceiling_bytes')!r} != code ceiling {TOTAL_BYTE_CEILING}; reseal"
        )
    if strict_ceiling and data.get("ceiling_directives") != TOTAL_DIRECTIVE_CEILING:
        raise ValueError(
            f"ceiling_directives {data.get('ceiling_directives')!r} != code ceiling "
            f"{TOTAL_DIRECTIVE_CEILING}; reseal"
        )
    return data


def check(root: Path, budget_path: Path, *, quiet: bool = False,
          advisories: list[str] | None = None) -> list[str]:
    """Return failure strings (empty == pass); print one row per surface.

    Byte findings go to ``advisories`` (when given) instead of failing."""
    failures: list[str] = []
    if advisories is None:
        advisories = []
    try:
        budget = load_budget(budget_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"budget-unreadable {budget_path}: {exc}"]
    sealed = budget["surfaces"]
    current = measure(root)

    for rel in sorted(set(sealed) - set(SURFACES)):
        failures.append(f"stale-budget-row {rel}: not a model-visible surface; remove it")

    total_bytes = 0
    cap_sum = 0
    total_directives = 0
    directive_cap_sum = 0
    for rel in SURFACES:
        caps = sealed.get(rel)
        cur = current[rel]
        if caps is None:
            failures.append(f"unsealed-surface {rel}: add its row with --reseal")
        if cur is None:
            failures.append(f"missing-surface {rel}")
            continue
        total_bytes += cur["bytes"]
        total_directives += cur["directives"]
        if caps is None:
            continue
        cap_sum += caps["bytes"]
        directive_cap_sum += caps["directives"]
        status = "ok"
        if cur["bytes"] > caps["bytes"]:
            status = "over-bytes(advisory)"
            advisories.append(
                f"over-bytes {rel}: {cur['bytes']} > {caps['bytes']} "
                f"(+{cur['bytes'] - caps['bytes']}); reseal when the growth is deliberate"
            )
        if cur["directives"] > caps["directives"]:
            status = "over-directives" if status == "ok" else status + "+directives"
            failures.append(
                f"over-directives {rel}: {cur['directives']} > {caps['directives']}; "
                "move the rule into a machine check or retire one"
            )
        if not quiet:
            print(
                f"surface={rel} bytes={cur['bytes']}/{caps['bytes']} "
                f"directives={cur['directives']}/{caps['directives']} status={status}"
            )

    total_cap = budget["total_bytes"]
    directive_total_cap = budget["total_directives"]
    if not quiet:
        # Rules first: this is the number the reduction is actually about.
        print(
            f"total directives={total_directives}/{directive_total_cap} "
            f"ceiling={TOTAL_DIRECTIVE_CEILING}"
        )
        print(f"total bytes={total_bytes}/{total_cap} ceiling={TOTAL_BYTE_CEILING}")
    if cap_sum > TOTAL_BYTE_CEILING:
        advisories.append(f"caps-exceed-ceiling {cap_sum} > {TOTAL_BYTE_CEILING}: per-surface byte caps are past the code ceiling")
    if total_cap > TOTAL_BYTE_CEILING:
        advisories.append(f"total-cap-exceeds-ceiling {total_cap} > {TOTAL_BYTE_CEILING}")
    if total_bytes > total_cap:
        advisories.append(f"over-total {total_bytes} > {total_cap}")
    if directive_cap_sum > TOTAL_DIRECTIVE_CEILING:
        failures.append(
            f"directive-caps-exceed-ceiling {directive_cap_sum} > {TOTAL_DIRECTIVE_CEILING}: "
            "per-surface rule caps were raised past the code ceiling"
        )
    if directive_total_cap > TOTAL_DIRECTIVE_CEILING:
        failures.append(
            f"directive-total-cap-exceeds-ceiling {directive_total_cap} > {TOTAL_DIRECTIVE_CEILING}"
        )
    if total_directives > directive_total_cap:
        failures.append(
            f"over-total-directives {total_directives} > {directive_total_cap}; "
            "retire a rule or move it into a machine check before adding another"
        )
    return failures


def reseal(root: Path, budget_path: Path, *, reason: str | None, commit: str | None) -> list[str]:
    """Rewrite caps to current measurements; raises need a reason."""
    current = measure(root)
    missing = [rel for rel, cur in current.items() if cur is None]
    if missing:
        return [f"missing-surface {rel}" for rel in missing]
    previous: dict = {}
    if budget_path.is_file():
        try:
            # A lowered code ceiling leaves a stale echo behind; reseal is the
            # one path that rewrites it, so it must not refuse on that alone.
            previous = load_budget(budget_path, strict_ceiling=False, legacy_ok=True)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return [f"budget-unreadable {budget_path}: {exc}"]
    old_surfaces = previous.get("surfaces", {})
    caps = {rel: with_headroom(current[rel]) for rel in SURFACES}  # type: ignore[arg-type]
    total = sum(cap["bytes"] for cap in caps.values())
    if total > TOTAL_BYTE_CEILING:
        # Bytes are advisory: say so, but never refuse to seal the rule caps.
        print(f"ADVISORY: surface-budget over-ceiling {total} > {TOTAL_BYTE_CEILING} (bytes are advisory)")
    directive_total = sum(cap["directives"] for cap in caps.values())
    if directive_total > TOTAL_DIRECTIVE_CEILING:
        return [
            f"over-directive-ceiling {directive_total} > {TOTAL_DIRECTIVE_CEILING}: "
            "cannot seal more rules than the code ceiling allows"
        ]

    raises: list[dict] = []
    old_measured = previous.get("measured", {})
    for rel in SURFACES:
        cur = current[rel]
        # Schema 1 sealed caps at the measurement, so its caps are its
        # measurements; schema 2 records them separately.
        old = old_measured.get(rel) or old_surfaces.get(rel)
        if old is None:
            continue
        for key in ("bytes", "directives"):
            if cur[key] > old[key]:
                raises.append({"surface": rel, "field": key, "from": old[key], "to": cur[key]})
    measured_total = sum(cur["bytes"] for cur in current.values() if cur)
    old_total = previous.get("measured_total_bytes")
    if not isinstance(old_total, int):
        old_total = previous.get("total_bytes")
    if isinstance(old_total, int) and measured_total > old_total:
        raises.append({"surface": "*", "field": "total_bytes", "from": old_total, "to": measured_total})
    measured_directive_total = sum(cur["directives"] for cur in current.values() if cur)
    old_directive_total = previous.get("measured_total_directives")
    if not isinstance(old_directive_total, int):
        old_directive_total = previous.get("total_directives")
    if isinstance(old_directive_total, int) and measured_directive_total > old_directive_total:
        raises.append({
            "surface": "*", "field": "total_directives",
            "from": old_directive_total, "to": measured_directive_total,
        })
    if raises and not reason:
        return [
            "raise-needs-reason " + ", ".join(f"{r['surface']}:{r['field']} {r['from']}->{r['to']}" for r in raises)
            + "; pass --reason to record why the surface may grow"
        ]

    today = _dt.date.today().isoformat()
    history = list(previous.get("history", []))
    if raises:
        history.append({"on": today, "commit": commit or "-", "reason": reason, "raised": raises})
    data = {
        "schema": SCHEMA,
        "measurement": (
            "UTF-8 bytes per surface; directives = matches of "
            f"/{DIRECTIVE_PATTERN.pattern}/ (case-insensitive) outside fenced code blocks and inline code spans"
        ),
        "sealed_at": today,
        "sealed_commit": commit or previous.get("sealed_commit", "-"),
        "ceiling_bytes": TOTAL_BYTE_CEILING,
        "headroom": {
            "bytes": HEADROOM_BYTES,
            "directives": HEADROOM_DIRECTIVES,
            "directives_min": HEADROOM_DIRECTIVES_MIN,
        },
        "ceiling_directives": TOTAL_DIRECTIVE_CEILING,
        "total_bytes": total,
        "measured_total_bytes": measured_total,
        "total_directives": directive_total,
        "measured_total_directives": measured_directive_total,
        "surfaces": caps,
        "measured": {rel: dict(current[rel]) for rel in SURFACES},  # type: ignore[arg-type]
        "history": history,
    }
    budget_path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"sealed surfaces={len(SURFACES)} rules={measured_directive_total}/{directive_total} "
        f"measured={measured_total} caps={total} headroom={total - measured_total} "
        f"raises={len(raises)} path={budget_path}"
    )
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="harness repository root")
    parser.add_argument("--budget", default=BUDGET_FILE, help="budget file, relative to root by default")
    parser.add_argument("--reseal", action="store_true", help="rewrite caps to the current measurement")
    parser.add_argument("--reason", help="required when --reseal would raise any cap")
    parser.add_argument("--commit", help="commit id to record with --reseal")
    parser.add_argument("--quiet", action="store_true", help="print failures only")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    budget_path = Path(args.budget)
    if not budget_path.is_absolute():
        budget_path = root / budget_path

    if args.reseal:
        failures = reseal(root, budget_path, reason=args.reason, commit=args.commit)
    else:
        advisories: list[str] = []
        failures = check(root, budget_path, quiet=args.quiet, advisories=advisories)
        for line in advisories:
            print(f"ADVISORY: surface-budget {line}")
    for line in failures:
        print(f"FAIL: surface-budget {line}")
    if not failures:
        print("surface_budget=ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
