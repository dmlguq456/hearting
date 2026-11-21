#!/usr/bin/env python3
"""Find the newer model a user's model-table cell follows, and make the cell follow it.

One rule for every harness. It sees only rows in the chosen shape (family, version,
visible in this account, previous-generation mark, accepted efforts, price, `upgrade`)
that each harness's own catalog reader produces (`tools/fleet/explorer/model_catalog.py`);
vendor naming patterns stay in those readers. Nothing here starts a service, refreshes a
catalog, or calls a model.

The survey (`survey_harness`, `fleet_view`) only reads. It has one entry per cell (a depth
line `model/<id>:<effort>` or a tier line): the cell's state (its value and the record at
the end of its line, `model_follow_mark.py`), the newer model its list gives, and what an
update would write. A cell lists only the small edits its lines need ("this item for that
one", "add this name"); `final_lines()` merges the edits of every cell into one final value
per line, so the write applies that, never one cell's edit after another's.

`apply_harness` writes exactly that plan, through the table's own edit path
(`tools/install/model_settings.py::edit_file`: lock, `.bak`, whole-file re-validation,
comments kept). Every cell is all or nothing, and any failure is one line, never an
exception. `revert_rejected` is the same path for one refusal: the cells that followed a
refused model go back to their earlier value and hold it.

One survey reads one environment: the process environment, like every caller. The CLI
version is asked once per survey, and a failure or a timeout counts as that one answer.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping

try:
    import model_config
    import model_follow_mark
    import opencode_runtime
except ImportError:  # package import in focused unit tests
    from utilities import model_config, model_follow_mark, opencode_runtime

HARNESSES = ("claude", "codex", "opencode")
CASCADE_KEY = "CFG_TIER_DEEP_FAILOVER_CASCADE"
OPENCODE_FALLBACK_KEY = "CFG_V2_AUTO_FALLBACKS"
# A new model priced above PRICE_LIMIT times, or below 1/PRICE_LIMIT of, the current one
# is shown as a proposal instead of a model to follow (only OpenCode's list has prices).
PRICE_LIMIT = 2.0
# Budgets `hearting model set <harness> <target> <model>@<budget>` accepts. The efforts a
# new model is fitted to, and the command that names them, both come from this one set.
_SETTABLE = frozenset({"low", "medium", "high", "xhigh", "max"})
_SETTABLE_OPENCODE = _SETTABLE | {"runtime-default"}
_CLI_TIMEOUT = 3
# How long a pass waits for the table's lock. A pass runs inside other work (an activation
# holds locks of its own paths), so it never waits long: a table it cannot lock in this time is
# not written and the next update or refresh tries again.
LOCK_WAIT = 1.0
# A survey is optional advice, so it alone asks for the OpenCode service read to end as a
# whole (launch, Fleet and the installer keep the per-read wait).
_SERVICE_SECONDS = 3.0
_CLI_NAME = {"claude": "Claude Code", "codex": "Codex"}
_VERSION = re.compile(r"\d+(?:\.\d+)+")


def _numbers(text) -> tuple[int, ...] | None:
    match = _VERSION.search(text) if isinstance(text, str) else None
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


# ---- the rule ----------------------------------------------------------------------

def _settable(adapter: str):
    return _SETTABLE_OPENCODE if adapter == "opencode" else _SETTABLE


def _fit(effort, efforts, settable=_SETTABLE):
    """`(effort, fit_from)` a stored line can carry, or None when there is none.

    The user's effort stays, or moves to the value just below it (the lowest accepted one
    when nothing is below) among the new model's efforts that `hearting model set` can
    store. A model whose listed efforts are all unstorable (`minimal`, `none`), or an
    effort off the storable set, cannot be written, so it is not a model to follow. The
    result is also a value the model lists (or `runtime-default`): `hearting model set`
    refuses `high` for a model that lists only `runtime-default`, so it is not followed.
    """
    if effort is None:
        return None, None
    observed = tuple(item for item in efforts or () if item in settable)
    if efforts and not observed and effort != "runtime-default":
        return None
    fitted, was = opencode_runtime.fit_variant(effort, observed)
    if fitted not in settable or (efforts and fitted != "runtime-default" and fitted not in efforts):
        return None
    return fitted, was


def _carry(effort, efforts, settable) -> str | None:
    """The effort an item `model:effort` of a moving line takes on the new model, or None.

    One rule for the cell and for every line that moves with it: the value the new model
    accepts, nearest to the old one. An effort `hearting model set` cannot store
    (`minimal`, `none`) or `runtime-default` stays as written where the model accepts it.
    """
    if effort in settable and effort != "runtime-default":
        fitted = _fit(effort, efforts, settable)
        return fitted[0] if fitted else None
    keep = not effort or effort == "runtime-default" or not efforts or effort in efforts
    return effort if keep else None


def successor(rows: Iterable[Mapping], model: str, effort: str | None = None, *,
              installed: Callable[[], tuple | None] | None = None,
              settable=_SETTABLE, skip: Iterable[str] = ()) -> dict:
    """Decide what the cell `model` should follow, from catalog rows only.

    Returns ``{"kind": "follow"|"proposal"|"none", "reason", "model", "effort",
    "fit_from", "deprecated"}``. ``installed`` lazily gives the installed CLI version
    (a tuple). Every candidate, the `upgrade` pointer included, must be visible in this
    account, not marked previous-generation, storable at the cell's effort, and runnable
    by the installed CLI; a candidate that needs a newer CLI than a known-too-old or
    unknown one is not followed. A model in ``skip`` (a refused one) is never a candidate.
    """
    rows = list(rows)
    skip = frozenset(model_key(name) for name in skip)
    by_id = {row["id"]: row for row in rows}
    current = by_id.get(model)
    result = {"kind": "none", "reason": "not-in-list", "model": None, "effort": effort,
              "fit_from": None, "deprecated": False}
    if current is None:
        return result
    result["deprecated"] = bool(current.get("deprecated"))
    family, version = current.get("family"), current.get("version")
    if not family or not version:
        return {**result, "reason": "no-pattern"}

    def usable(row):
        if (model_key(row["id"]) in skip or not row.get("visible") or row.get("deprecated")
                or _fit(effort, row.get("efforts"), settable) is None):
            return False
        needs = _numbers(row.get("min_cli"))
        if needs is None:
            return True
        have = installed() if installed else None
        return have is not None and have >= needs

    def newer(row):
        return (row.get("family") == family and row.get("version") and row["version"] > version
                and row["id"] != model and usable(row))

    chosen, reason = None, None
    pointer = by_id.get(current.get("upgrade") or "")
    if pointer is not None and usable(pointer):
        if pointer.get("family") == family:
            chosen = pointer if newer(pointer) else None
        else:
            chosen, reason = pointer, "other family"
    if chosen is None and reason is None:
        found = sorted((row for row in rows if newer(row)),
                       key=lambda row: (row["version"], row["id"]))
        chosen = found[-1] if found else None
    if chosen is None:
        return {**result, "reason": "no-successor"}
    if reason is None:
        before, after = current.get("cost"), chosen.get("cost")
        if before is not None and after is not None and (before or after):
            ratio = after / before if before else float("inf")
            if ratio > PRICE_LIMIT or ratio < 1 / PRICE_LIMIT:
                reason = "price x%.2f" % ratio if ratio != float("inf") else "price from free"
    fitted, fit_from = _fit(effort, chosen.get("efforts"), settable)
    return {**result, "kind": "proposal" if reason else "follow", "reason": reason or "newer",
            "model": chosen["id"], "effort": fitted, "fit_from": fit_from}


# ---- cells and the lines that must move with them ----------------------------------

def cells(adapter: str, values: Mapping[str, str]) -> list[dict]:
    """The cells of a user's table that name a concrete model, in file order."""
    suffix = "VARIANT" if adapter == "opencode" else "EFFORT"
    found = []
    for key, value in values.items():
        profile = re.fullmatch(r"CFG_MODEL_PROFILE_([A-Z0-9_]+)", key)
        if (profile and not profile.group(1).startswith("GRANULARITY")
                and value.startswith("model/") and value.count(":") == 1):
            model, effort = value[len("model/"):].split(":")
            found.append({"target": "profile/" + profile.group(1).lower().replace("_", "-"),
                          "key": key, "effort_key": None, "model": model, "effort": effort or None})
            continue
        tier = model_config.TIER_KEY.fullmatch(key)
        if tier and tier.group(2) == "MODEL":
            name = tier.group(1)
            found.append({"target": "tier/" + name.lower().replace("_", "-"), "key": key,
                          "effort_key": f"CFG_TIER_{name}_{suffix}", "model": value,
                          "effort": values.get(f"CFG_TIER_{name}_{suffix}")})
    return found


def _swaps(value: str, current: str, replacement: str, efforts, settable) -> dict[str, str] | None:
    """`{old item: new item}` for the `model:effort` items of `value` that name `current`;
    None when one of them has no effort the new model accepts."""
    found = {}
    for item in value.split():
        name, sep, effort = item.partition(":")
        if sep and name == current:
            carried = _carry(effort, efforts, settable)
            if carried is None:
                return None
            found[item] = f"{replacement}:{carried}"
    return found


def companions(adapter: str, values: Mapping[str, str], current: str, replacement: str,
               efforts) -> list[dict] | None:
    """The edits other lines need so they follow one cell, never a whole new value.

    `{"key", "from", "swap": {old item: new item}}` for a list of `model:effort` items, and
    `{"key", "from", "add": [name]}` for the main-session-only list (a name is only added).
    Every new item is one the replacement accepts. None when some line cannot move to
    the replacement that way: a cell whose lines cannot move is not followed at all.
    Two cells can edit the same line; `final_lines()` merges them.
    """
    settable = _settable(adapter)
    found = []
    for key in (CASCADE_KEY, *((OPENCODE_FALLBACK_KEY,) if adapter == "opencode" else ())):
        swap = _swaps(values.get(key, ""), current, replacement, efforts, settable)
        if swap is None:
            return None
        if swap:
            found.append({"key": key, "from": values[key], "swap": swap})
    reserved = values.get(model_config.MAIN_SESSION_ONLY_KEY, "").split()
    if (adapter in ("claude", "codex") and model_config.restricted_model(current, reserved)
            and not model_config.restricted_model(replacement, reserved)):
        found.append({"key": model_config.MAIN_SESSION_ONLY_KEY,
                      "from": values[model_config.MAIN_SESSION_ONLY_KEY], "add": [replacement]})
    return found


def final_lines(entries: Iterable[Mapping]) -> dict[str, dict]:
    """One final value per companion line, from the edits of every given entry.

    `{key: {"from": value now, "to": final value}}`. A swap changes every item equal to its
    old item once (the first cell in file order wins if two cells map one item differently);
    an add appends a name that is not there yet, in file order. The file order is each
    entry's `order` (the survey sets it), so entries may come in any order; an entry without
    `order` counts as 0, so it ties with an explicit 0 and ties keep the order given. Every item of `to` is one its model accepts, because an
    entry carries only swaps that the survey checked. A later step that writes takes `to`
    as it is and applies nothing else to the line.
    """
    found: dict[str, dict] = {}
    for entry in sorted(entries, key=lambda entry: entry.get("order", 0)):
        for item in entry.get("companions", ()):
            line = found.setdefault(item["key"], {"from": item["from"], "swaps": {}, "adds": [],
                                                   "removes": []})
            for old, new in item.get("swap", {}).items():
                line["swaps"].setdefault(old, new)
            line["adds"].extend(name for name in item.get("add", ()) if name not in line["adds"])
            line["removes"].extend(name for name in item.get("remove", ()) if name not in line["removes"])
    final = {}
    for key, line in found.items():
        tokens = [line["swaps"].get(token, token) for token in line["from"].split()
                  if token not in line["removes"]]
        tokens += [name for name in line["adds"] if name not in tokens]
        final[key] = {"from": line["from"], "to": " ".join(tokens)}
    return final


def _command(adapter, entry) -> str | None:
    found = entry["successor"]
    budget = f"@{found['effort']}" if found["effort"] else ""
    return f"hearting model set {adapter} {entry['target']} {found['model']}{budget}"


# ---- reading the installed CLI -----------------------------------------------------

def installed_cli(harness: str) -> tuple[tuple | None, str | None]:
    """(version, path) of the harness CLI on PATH. One short `--version` call; a CLI that
    is missing, fails, times out, or prints no version gives version None (the CLI notes
    are then not shown), and a failed run's text is never read for a version."""
    path = shutil.which(harness)
    if not path:
        return None, None
    try:
        done = subprocess.run([path, "--version"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=_CLI_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None, path
    if done.returncode != 0:
        return None, path
    return _numbers((done.stdout or "") + " " + (done.stderr or "")), path


def upgrade_command(harness: str, path: str | None) -> str | None:
    """The command that raises this CLI, only when the install method is evident."""
    try:
        real = os.path.realpath(path) if path else ""
    except OSError:
        return None
    if "/Cellar/" in real or "/homebrew/" in real.lower():
        return {"codex": "brew upgrade codex", "claude": "brew upgrade claude-code"}.get(harness)
    if "node_modules/@openai/codex" in real and harness == "codex":
        return "npm i -g @openai/codex"
    if "node_modules/@anthropic-ai/claude-code" in real and harness == "claude":
        return "npm i -g @anthropic-ai/claude-code"
    if harness == "claude" and "/claude/versions/" in real:
        return "claude update"
    return None


def _cli_note(harness, catalog, entries, observe) -> dict | None:
    """The 'CLI is too old to list the next model' signal; needs only local facts."""
    if harness == "codex":
        names = list(dict.fromkeys(e["current"]["model"] for e in entries
                                   if e["kind"] == "none" and e["reason"] == "no-successor"
                                   and e["deprecated"]))
        if not names or not catalog.client_version:
            return None
        have, path = observe()
        if have is None or have != _numbers(catalog.client_version):
            return None
        return {"harness": harness, "version": ".".join(map(str, have)), "models": names,
                "kind": "previous-generation", "command": upgrade_command(harness, path)}
    if harness == "claude":
        need = [row for row in catalog.rows if row.get("visible") and _numbers(row.get("min_cli"))]
        if not need:
            return None
        have, path = observe()
        if have is None:
            return None
        behind = [row for row in need if _numbers(row["min_cli"]) > have]
        if not behind:
            return None
        return {"harness": harness, "version": ".".join(map(str, have)),
                "models": [(row["id"], row["min_cli"]) for row in behind],
                "kind": "min-version", "command": upgrade_command(harness, path)}
    return None


# ---- going back ---------------------------------------------------------------------

def _moved_to(marks: Mapping[str, Mapping], key: str, model: str) -> bool:
    """Whether the line `key` carries a record of a follow to `model`: the only lines that
    go back with a cell. A line with none (or another one) is the person's own."""
    record = marks.get(key)
    return bool(record) and record["kind"] == "follow" and model_key(record["to"]) == model_key(model)


def _back_companions(adapter: str, values: Mapping[str, str], marks: Mapping[str, Mapping],
                     refused: str, back: str, now_effort, was_effort) -> list[dict]:
    """The edits other lines need so they go back with a cell: `refused` -> `back`.

    The inverse of `companions()`, from the table alone (no list is read to go back), and only
    for a line whose own record says a follow moved it to `refused`: items `refused:<effort>`
    return to `back:<effort>` (the effort the cell held before it was lowered when the item
    carries the cell's lowered one), and a name the follow added to the main-session-only list
    is taken out again. One record cannot tell two cells' earlier values apart: two cells that
    followed to one model from different ones share a line, and it goes back to the earlier
    value of the cell that goes back (the result line names every item that moved).
    """
    found = []
    for key in (CASCADE_KEY, *((OPENCODE_FALLBACK_KEY,) if adapter == "opencode" else ())):
        if not _moved_to(marks, key, refused):
            continue
        swap = {}
        for item in values.get(key, "").split():
            name, sep, effort = item.partition(":")
            if sep and name == refused:
                if was_effort and effort == now_effort:
                    effort = was_effort
                swap[item] = f"{back}:{effort}"
        if swap:
            found.append({"key": key, "from": values[key], "swap": swap})
    reserved = values.get(model_config.MAIN_SESSION_ONLY_KEY, "").split()
    if (adapter in ("claude", "codex") and refused in reserved
            and _moved_to(marks, model_config.MAIN_SESSION_ONLY_KEY, refused)):
        others = [name for name in reserved if name != refused]
        if model_config.restricted_model(back, others):
            found.append({"key": model_config.MAIN_SESSION_ONLY_KEY,
                          "from": values[model_config.MAIN_SESSION_ONLY_KEY], "remove": [refused]})
    return found


def _state_counts(states: Iterable[Mapping]) -> dict[str, int]:
    states = list(states)
    return {"followed": sum(1 for st in states if st["why"] == "followed"),
            "kept": sum(1 for st in states if st["state"] == "keep"),
            "held": sum(1 for st in states if st["state"] == "held")}


def revert_stage(adapter: str, values: Mapping[str, str], marks: Mapping[str, Mapping],
                 rejected: Iterable[str], today: str) -> list[dict]:
    """The cells that followed a refused model, each with the write that takes it back.

    A cell qualifies when its value is the model its record says it followed to, and that
    model is among `rejected`. Nothing about the list is read: the table has all it needs.
    """
    rejected = frozenset(rejected)
    suffix = "VARIANT" if adapter == "opencode" else "EFFORT"
    entries = []
    for order, cell in enumerate(cells(adapter, values)):
        record = marks.get(cell["key"])
        state = model_follow_mark.cell_state(cell["model"], record)
        if state["why"] != "followed" or model_key(cell["model"]) not in rejected:
            continue
        before, refused, was = record["from"], record["to"], record.get("effort_was")
        effort = was or cell["effort"]
        if cell["effort_key"] is None:
            changes = {cell["key"]: f"model/{before}:{effort or ''}"}
        else:
            changes = {cell["key"]: before}
            if was and cell["effort_key"] in values:
                changes[cell["effort_key"]] = was
        entries.append({
            "harness": adapter, "target": cell["target"], "order": order, "key": cell["key"],
            "action": "revert", "current": {"model": refused, "effort": cell["effort"]},
            "back": {"model": before, "effort": effort}, "refused": refused,
            "companions": _back_companions(adapter, values, marks, refused, before, cell["effort"], was),
            "write": {"changes": changes, "mark": model_follow_mark.held_record(today, refused, before),
                      "companion_mark": None}})
    return entries


def model_key(value) -> str:
    """The comparison form of a model name (the form the outcome record keeps)."""
    return re.sub(r"(\[[^\]]*\]|#.*)$", "", str(value or "").strip()).lower()


def _combine(entries: Iterable[Mapping]) -> tuple[dict, dict]:
    """`(changes, marks)` the given entries write together: each cell's own lines, and one
    merged final value for every line two cells may share."""
    ordered = sorted(entries, key=lambda entry: entry.get("order", 0))
    changes, marks = {}, {}
    for entry in ordered:
        changes.update(entry["write"]["changes"])
        marks[entry["key"]] = entry["write"]["mark"]
    for key, line in final_lines(ordered).items():
        changes[key] = line["to"]
    for entry in ordered:
        for item in entry.get("companions", ()):
            marks.setdefault(item["key"], entry["write"]["companion_mark"])
    return changes, marks


def _linked_lines(adapter: str, values: Mapping[str, str], marks: Mapping[str, Mapping],
                  state: Mapping, cell: Mapping) -> list[str]:
    """The keys of the companion lines a cell's follow moved: lines whose own record reads the
    same follow (`A -> B`) and that still hold `B`. Read from the table alone; a cell that did not
    follow has none."""
    if state["why"] != "followed":
        return []
    keys = [CASCADE_KEY, *((OPENCODE_FALLBACK_KEY,) if adapter == "opencode" else ()),
            *((model_config.MAIN_SESSION_ONLY_KEY,) if adapter in ("claude", "codex") else ())]
    found = []
    for key in keys:
        record = marks.get(key)
        if not record or record["kind"] != "follow" or key not in values:
            continue
        if (model_key(record["from"]) != model_key(state["from"])
                or model_key(record["to"]) != model_key(state["to"])):
            continue
        names = [item.partition(":")[0] for item in values[key].split()]
        if model_key(state["to"]) in {model_key(name) for name in names}:
            found.append(key)
    return found


def follow_stage(adapter: str, values: Mapping[str, str], marks: Mapping[str, Mapping],
                 rows, observe: Callable, today: str, *, skip: Iterable[str] = ()) -> list[dict]:
    """One entry per cell: its state, the newer model its list gives, and what an update does.

    `branch` says what happens to the cell: `will-follow` (an update writes it), `kept` (a
    newer model exists but the cell is held by a person), `proposal` (a different family or
    a very different price: only suggested), or `none`. A model any line of the table holds as
    refused (`held`) or that is in `skip` (the models the outcome record shows refused) is never
    followed to, for every cell: the follow and the going back agree on it. A cell whose newest
    model is skipped that way says so in `skipped`. `rows` None (no list) gives `none` throughout.
    """
    settable = _settable(adapter)
    refused = {record["to"] for record in marks.values() if record["kind"] == "held"} | set(skip)
    entries = []
    for order, cell in enumerate(cells(adapter, values)):
        state = model_follow_mark.cell_state(cell["model"], marks.get(cell["key"]))
        entry = {"harness": adapter, "target": cell["target"], "order": order, "key": cell["key"],
                 "current": {"model": cell["model"], "effort": cell["effort"]}, "state": state,
                 "successor": None, "kind": "none", "reason": "no-catalog", "deprecated": False,
                 "companions": [], "command": None, "branch": "none", "action": None, "write": None,
                 "skipping": sorted(refused), "skipped": None,
                 "linked": _linked_lines(adapter, values, marks, state, cell)}
        entries.append(entry)
        if rows is None:
            continue
        found = successor(rows, cell["model"], cell["effort"], installed=lambda: observe()[0],
                          settable=settable, skip=refused)
        entry.update(kind=found["kind"], reason=found["reason"], deprecated=found["deprecated"])
        if not found["model"]:
            if refused:  # would the newest model be followed if it were not refused?
                open_ = successor(rows, cell["model"], cell["effort"], installed=lambda: observe()[0],
                                  settable=settable)
                if open_["model"] and model_key(open_["model"]) in {model_key(m) for m in refused}:
                    entry["skipped"] = open_["model"]
            continue
        row = next(r for r in rows if r["id"] == found["model"])
        moves = companions(adapter, values, cell["model"], found["model"], row.get("efforts"))
        if moves is None:  # its lines cannot move with it: not a cell to follow
            entry.update(kind="none", reason="lines-do-not-fit")
            continue
        entry["successor"] = {"model": found["model"], "effort": found["effort"],
                              "fit_from": found["fit_from"]}
        entry["companions"] = moves
        entry["command"] = _command(adapter, entry)
        if found["kind"] == "proposal":
            entry["branch"] = "proposal"
        elif state["state"] == "keep":
            entry["branch"] = "kept"
        else:
            entry["branch"] = "will-follow"
            entry["action"] = "follow"
            now = cell["effort"]
            if cell["effort_key"] is None:
                changes = {cell["key"]: f"model/{found['model']}:{found['effort'] or ''}"}
            else:
                changes = {cell["key"]: found["model"]}
                if found["effort"] and found["effort"] != now and cell["effort_key"] in values:
                    changes[cell["effort_key"]] = found["effort"]
            record = model_follow_mark.follow_record(today, cell["model"], found["model"],
                                                     found["fit_from"])
            entry["write"] = {"changes": changes, "mark": record, "companion_mark": record}
    return entries


def _accepted(adapter, text, entries, ok) -> tuple[list[dict], list[dict]]:
    """`(taken, refused)`: the entries the whole file takes. Every cell is all or nothing:
    all of them at once, else one at a time in file order."""
    entries = list(entries)
    if not entries:
        return [], []
    if ok(text, *_combine(entries)):
        return entries, []
    taken, refused = [], []
    for entry in entries:
        if ok(text, *_combine([*taken, entry])):
            taken.append(entry)
        else:
            refused.append(entry)
    return taken, refused


def _plan_writes(reverts, follows) -> list[dict]:
    """What an update writes, as plain data (the shape both the view and the lines use)."""
    out = []
    for entry in [*reverts, *follows]:
        if entry["action"] not in ("revert", "follow"):
            continue
        item = {"action": entry["action"], "harness": entry["harness"], "target": entry["target"],
                "from": entry["current"]["model"], "order": entry["order"],
                "lines": [], "companions": entry["companions"], "write": entry["write"],
                "key": entry["key"]}
        if entry["action"] == "revert":
            item.update(to=entry["back"]["model"], effort=entry["back"]["effort"], effort_was=None,
                        refused=entry["refused"])
        else:
            found = entry["successor"]
            item.update(to=found["model"], effort=found["effort"], effort_was=found["fit_from"])
        out.append(item)
    for item in out:
        item["lines"] = [{"key": key, "from": line["from"], "to": line["to"]}
                         for key, line in final_lines([item]).items()]
    return out


# ---- one harness, then all ---------------------------------------------------------

def _catalog(harness):
    utilities = Path(__file__).resolve().parent
    root = str(utilities.parent)
    if root not in sys.path:
        sys.path.append(root)
    from tools.fleet.explorer import model_catalog
    return model_catalog.read_catalog(harness, deadline=_SERVICE_SECONDS)


def _rejected(harness: str) -> frozenset:
    try:
        import model_outcome_record
        return model_outcome_record.rejected_models(harness)
    except Exception:
        return frozenset()


def _observer(harness, probe):
    seen = {}

    def observe():
        """The CLI's (version, path), asked at most once in this survey."""
        if "cli" not in seen:
            try:
                seen["cli"] = probe(harness)
            except Exception:
                seen["cli"] = (None, None)
        return seen["cli"]

    return observe


def _read_catalog(harness, read_catalog):
    catalog = (read_catalog or _catalog)(harness)
    return catalog if catalog.state == "available" and catalog.rows else None


def _own_home(harness: str, runtime: str | Path | None) -> Path:
    """The config home of `harness` itself. `HEARTING_MODEL_CONFIG_HOME` pins one harness's
    home for a worker whose own home is masked; it says nothing about another harness's, so it
    is not read here: a pass or a close for `harness` never lands on a table that is not its own.
    (A pass that finds no table at the harness's own home skips, as for any harness not set up.)"""
    if runtime is not None:
        return Path(runtime)
    env = {key: value for key, value in os.environ.items() if key != "HEARTING_MODEL_CONFIG_HOME"}
    return model_config.runtime_home(harness, None, env)


def _read_only(path) -> bool:
    """Whether the person made this table read-only (no write bit for its owner, or no write
    access): a table they left like that is theirs, and nothing writes it on its own."""
    try:
        return not (os.stat(path).st_mode & stat.S_IWUSR) or not os.access(path, os.W_OK)
    except OSError:
        return False


def survey_harness(harness: str, *, source_root: str | Path | None = None,
                   read_catalog: Callable | None = None, probe: Callable | None = None,
                   runtime: str | Path | None = None, today: str | None = None,
                   rejected: Iterable[str] | None = None) -> dict:
    """Read-only survey of one harness; a list that cannot be read is skipped.

    `entries` has one entry per cell (also without a list: a cell's state is the table's),
    `plan` is what an update would write (the same stage functions `apply_harness` runs, every
    cell taken, from the same file text), `states` counts followed, kept and held cells.
    `read_only` says the table is read-only and has something to write (then `plan` is empty:
    nothing writes it). Nothing is written and no list is refreshed.
    """
    skipped = {"harness": harness, "status": "skipped", "reason": "", "entries": [], "cli": None,
               "plan": [], "states": {"followed": 0, "kept": 0, "held": 0}, "read_only": False}
    today = today or model_follow_mark.today()
    try:
        runtime = _own_home(harness, runtime)
        _values, receipt = model_config.resolve_config(harness, runtime=runtime, source_root=source_root)
        if receipt.source != "user":
            return {**skipped, "reason": "no-user-file"}
        # The cells are the selected file's own lines, as the write reads them (a value the
        # reader derives for a line the file does not have is not a cell to follow).
        text = Path(receipt.selected_path).read_text(encoding="utf-8")
        values = model_config.parse_config(receipt.selected_path)
        marks = model_follow_mark.read_marks(text)
        cell_list = cells(harness, values)
        skipped["states"] = _state_counts(
            model_follow_mark.cell_state(cell["model"], marks.get(cell["key"])) for cell in cell_list)
        refused = frozenset(_rejected(harness) if rejected is None else rejected)
        reverts = revert_stage(harness, values, marks, refused, today)
        skipped["plan"] = _plan_writes(reverts, [])
        try:
            catalog = _read_catalog(harness, read_catalog)
        except Exception:  # a list that cannot be read: the table's own states are still shown
            catalog = None
        observe = _observer(harness, probe or installed_cli)
        entries = follow_stage(harness, values, marks, catalog.rows if catalog else None,
                               observe, today, skip=refused)
        for entry in reverts:
            match = next(e for e in entries if e["key"] == entry["key"])
            match.update(branch="revert", action="revert", write=entry["write"],
                         companions=entry["companions"], refused=entry["refused"],
                         back=entry["back"])
        read_only = _read_only(receipt.selected_path)
        if catalog is None:
            pending = bool(reverts) and read_only
            return {**skipped, "reason": "no-catalog", "entries": entries, "read_only": pending,
                    "plan": [] if read_only else skipped["plan"]}
        # What an update writes: the reverts first, then the follows of what is left.
        changes1, marks1 = _combine(reverts) if reverts else ({}, {})
        values1, marks_after = ({**values, **changes1},
                                {**marks, **{k: v for k, v in marks1.items() if v}})
        for key in [k for k, v in marks1.items() if not v]:
            marks_after.pop(key, None)
        later = (follow_stage(harness, values1, marks_after, catalog.rows, observe, today, skip=refused)
                 if reverts else entries)
        plan = _plan_writes(reverts, [e for e in later if e["action"] == "follow"])
        pending = read_only and bool(plan)
        return {"harness": harness, "status": "ok", "reason": "", "entries": entries,
                "cli": _cli_note(harness, catalog, entries, observe),
                "plan": [] if read_only else plan, "states": skipped["states"], "read_only": pending}
    except Exception as exc:  # a list that cannot be read never fails update or refresh
        return {**skipped, "reason": f"unreadable: {type(exc).__name__}"}


def survey(harnesses: Iterable[str] = HARNESSES, **kwargs) -> list[dict]:
    return [survey_harness(harness, **kwargs) for harness in harnesses]


# ---- writing ------------------------------------------------------------------------

def _model_settings():
    root = str(Path(__file__).resolve().parents[1] / "tools" / "install")
    if root not in sys.path:
        sys.path.insert(0, root)
    import model_settings
    return model_settings


def _check(settings, harness, source_root):
    def ok(text, changes, marks):
        return settings.candidate_ok(harness, settings.edit_text(text, changes, marks), source_root)
    return ok


def _short(exc: Exception) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:160]


def apply_harness(harness: str, *, source_root: str | Path | None = None,
                  read_catalog: Callable | None = None, probe: Callable | None = None,
                  runtime: str | Path | None = None, today: str | None = None,
                  rejected: Iterable[str] | None = None, lock_wait: float = LOCK_WAIT) -> dict:
    """Write what the survey plans, to this harness's table. Never raises.

    First the cells that followed a model the record shows refused go back (a refusal the
    close did not get to); then the cells that follow a newer model of the list, never one the
    record shows refused. Each is all or nothing and the file is written once. The result
    carries `written` and `reverted` (the entries that went in), `refused` (cells the
    whole-file check turned down), `backup`, and `error` (one line) when nothing could be
    written; a list that cannot be read still lets the first part run. The table's lock is
    waited for `lock_wait` seconds (a table that is locked longer is not written: the next
    update or refresh does it). A table the person made read-only is not written: the result
    says `read_only` when it had something to write.
    """
    result = {"harness": harness, "status": "skipped", "reason": "", "entries": [], "cli": None,
              "plan": [], "states": {"followed": 0, "kept": 0, "held": 0}, "written": [],
              "reverted": [], "refused": [], "backup": None, "error": None, "read_only": False}
    today = today or model_follow_mark.today()
    try:
        runtime = _own_home(harness, runtime)
        values, receipt = model_config.resolve_config(harness, runtime=runtime, source_root=source_root)
        if receipt.source != "user":
            return {**result, "reason": "no-user-file"}
        catalog = None
        try:
            catalog = _read_catalog(harness, read_catalog)
        except Exception:
            catalog = None
        observe = _observer(harness, probe or installed_cli)
        if catalog is not None and any(row.get("min_cli") for row in catalog.rows):
            observe()  # the CLI is asked before the table is locked, never under the lock
        refused = frozenset(_rejected(harness) if rejected is None else rejected)
        settings = _model_settings()
        ok = _check(settings, harness, source_root)
        held = {}

        def build(text: str) -> dict:
            values = settings.parse_text(text)
            marks = model_follow_mark.read_marks(text)
            reverts, bad_reverts = _accepted(harness, text, revert_stage(harness, values, marks, refused, today), ok)
            changes, mark_edits = _combine(reverts) if reverts else ({}, {})
            text1 = settings.edit_text(text, changes, mark_edits) if reverts else text
            values1 = settings.parse_text(text1) if reverts else values
            marks1 = model_follow_mark.read_marks(text1) if reverts else marks
            entries = follow_stage(harness, values1, marks1,
                                   catalog.rows if catalog is not None else None, observe, today,
                                   skip=refused)
            follows, bad_follows = _accepted(
                harness, text1, [e for e in entries if e["action"] == "follow"], ok)
            if _read_only(receipt.selected_path):  # the person's own: nothing is written
                held.update(reverts=[], follows=[], entries=entries, refused=[], text=text,
                            read_only=bool(reverts or follows))
                return {"changed": {}, "marks": {}}
            changes2, marks2 = _combine(follows) if follows else ({}, {})
            final = settings.edit_text(text1, changes2, marks2) if follows else text1
            held.update(reverts=reverts, follows=follows, entries=entries,
                        refused=[*bad_reverts, *bad_follows], text=final, read_only=False)
            return {"changed": {**changes, **changes2}, "marks": {**mark_edits, **marks2}}

        edited = settings.edit_file(harness, build, runtime=runtime, source_root=source_root,
                                    lock_wait=lock_wait)
        entries = held["entries"]
        marks_now = model_follow_mark.read_marks(held["text"])
        for entry in entries:  # what the write already did is not planned again
            if entry["action"] == "follow" and entry in held["follows"]:
                entry["branch"] = "written"
        return {**result, "status": "ok", "entries": entries, "reverted": held["reverts"],
                "read_only": held["read_only"],
                "written": held["follows"], "refused": held["refused"],
                "backup": edited.get("backup"), "plan": [],
                "states": _state_counts(
                    model_follow_mark.cell_state(c["model"], marks_now.get(c["key"]))
                    for c in cells(harness, settings.parse_text(held["text"]))),
                "cli": _cli_note(harness, catalog, entries, observe) if catalog is not None else None,
                "config_path": edited.get("config_path")}
    except Exception as exc:  # a table that cannot be written never fails update or refresh
        return {**result, "status": "error", "error": _short(exc)}


def revert_rejected(harness: str, model: str, *, deadline: float | None = None,
                    source_root: str | Path | None = None,
                    runtime: str | Path | None = None, today: str | None = None) -> bool:
    """The refusal of `model` is confirmed: the cells that followed to it go back and hold it.

    Called once by the close that recorded the refusal. Best effort and bounded: it never
    raises, a table that cannot be locked before `deadline` (a `time.monotonic()` time) is
    not written (no deadline means the pass's own short wait), a read-only table is not
    written, and a table with nothing that followed `model` costs one small file read.
    """
    try:
        runtime = _own_home(harness, runtime)
        path = model_config.user_path(harness, runtime=runtime)
        text = path.read_text(encoding="utf-8")
        wanted = model_key(model)
        if model_follow_mark.PREFIX not in text or wanted not in text.lower():
            return False
        settings = _model_settings()
        ok = _check(settings, harness, source_root)
        today = today or model_follow_mark.today()
        done = []

        def build(current: str) -> dict:
            values = settings.parse_text(current)
            marks = model_follow_mark.read_marks(current)
            if _read_only(path):  # the person's own table: the next update says so, once
                return {"changed": {}, "marks": {}}
            taken, _bad = _accepted(harness, current, revert_stage(harness, values, marks, {wanted}, today), ok)
            done.extend(taken)
            changes, mark_edits = _combine(taken) if taken else ({}, {})
            return {"changed": changes, "marks": mark_edits}

        wait = LOCK_WAIT if deadline is None else max(deadline - time.monotonic(), 0.0)
        settings.edit_file(harness, build, runtime=runtime, source_root=source_root, lock_wait=wait)
        return bool(done)
    except Exception:  # noqa: BLE001 - a table that cannot be edited never changes the close
        return False


# ---- reading for others -------------------------------------------------------------

def counts(result: Mapping) -> tuple[int, int]:
    """(newer, proposal) entries of one harness survey: cells an update would follow, and
    cells with a proposal only."""
    branches = [entry.get("branch") for entry in result.get("entries", ())]
    return branches.count("will-follow"), branches.count("proposal")


def state_counts(result: Mapping) -> tuple[int, int, int]:
    """(followed, kept, held) cells of one harness survey."""
    states = result.get("states") or {}
    return states.get("followed", 0), states.get("kept", 0), states.get("held", 0)


# Why a cell is not one an update can follow, whatever the list shows next: a model the list does
# not know (a Claude alias such as `opus` is one), a model with no family and version, or a
# newer model whose companion lines cannot move with it.
_NOT_FOLLOWABLE = frozenset({"not-in-list", "no-pattern", "lines-do-not-fit"})


def fleet_view(harnesses: Iterable[str] = HARNESSES, **kwargs) -> list[dict]:
    """What a screen shows for each harness's table. Read-only: no model call, no list
    refresh, no write (a survey's own reads only).

    One item per harness: ``{"harness", "status", "reason", "cells", "plan", "cli", "states",
    "read_only"}`` (`read_only`: the table is read-only and has something to write, so `plan` is
    empty: nothing writes it on its own). The
    cells and their states are the table's own, so they are given without a list too (`next`
    is empty then, except for a cell waiting to go back).
    A cell is ``{"target", "model", "effort", "state": "follow"|"keep"|"held", "why",
    "since": {"date", "from", "to", "effort_was": the effort the follow lowered or None, "lines":
    keys of the companion lines the follow moved, read from their own records} | None,
    "followable": False for a cell no update can follow (`reason` is `not-in-list`, `no-pattern`
    or `lines-do-not-fit`), None without a list, else True, "reason": the survey's reason for the
    cell, "held": the refused model (the one the cell holds
    on, or the newest model its list gives that the record shows refused) | None,
    "next": {"model", "effort", "branch": "will-follow"|"proposal"|"kept"|"revert", "reason",
    "command"} | None, "lines": the companion lines that move with it}``. `plan` is what an
    update would write right now, each item with its target, `from`, `to`, effort and
    companion lines; applying it is `apply_harness`, which runs the same stage functions.
    """
    out = []
    for result in survey(harnesses, **kwargs):
        shown = []
        for entry in result["entries"]:
            state, found = entry["state"], entry["successor"]
            record = state["date"] is not None
            following = None
            if entry["branch"] in ("will-follow", "proposal", "kept"):
                following = {"model": found["model"], "effort": found["effort"],
                             "branch": entry["branch"], "reason": entry["reason"],
                             "command": entry["command"], "fit_from": found["fit_from"]}
            elif entry["branch"] == "revert":
                following = {"model": entry["back"]["model"], "effort": entry["back"]["effort"],
                             "branch": "revert", "reason": f"{entry['refused']} rejected",
                             "command": None, "fit_from": None}
            shown.append({
                "target": entry["target"], "model": entry["current"]["model"],
                "effort": entry["current"]["effort"], "state": state["state"], "why": state["why"],
                "since": ({"date": state["date"], "from": state["from"], "to": state["to"],
                           "effort_was": state["effort_was"], "lines": entry["linked"]}
                          if record and state["why"] in ("followed", "reverted", "held", "kept") else None),
                "followable": (None if entry["reason"] == "no-catalog" else
                               entry["reason"] not in _NOT_FOLLOWABLE),
                "reason": entry["reason"],
                "held": state["skip"] or entry.get("skipped"), "next": following,
                "lines": [{"key": key, "from": line["from"], "to": line["to"]}
                          for key, line in final_lines([entry]).items()]})
        out.append({"harness": result["harness"], "status": result["status"],
                    "reason": result["reason"], "cells": shown,
                    "plan": [{k: v for k, v in item.items() if k not in ("write", "companions", "order")}
                             for item in result["plan"]],
                    "cli": result["cli"], "states": result["states"],
                    "read_only": result.get("read_only", False)})
    return out


# ---- result lines -------------------------------------------------------------------

_LINE_NAME = {CASCADE_KEY: "cascade", OPENCODE_FALLBACK_KEY: "v2-auto-fallbacks",
              model_config.MAIN_SESSION_ONLY_KEY: "main-session-only"}


def _diff(before: str, after: str) -> str:
    old, new = before.split(), after.split()
    if len(old) == len(new):
        return ", ".join(f"{a} -> {b}" for a, b in zip(old, new) if a != b)
    return ", ".join([f"-{t}" for t in old if t not in new] + [f"+{t}" for t in new if t not in old])


def lines(results: Iterable[Mapping]) -> list[str]:
    """Result lines for update/refresh output; nothing to say gives no line."""
    out = []
    wrote = False
    for result in results:
        harness = result["harness"]
        backup = Path(result["backup"]).name if result.get("backup") else None
        for entry in result.get("reverted", ()):
            out.append(f"model follow: {harness} {entry['target']} back to {entry['back']['model']} "
                       f"· {entry['refused']} rejected")
            wrote = True
        for entry in result.get("written", ()):
            found, now = entry["successor"], entry["current"]
            effort = found["effort"]
            if effort and found["fit_from"]:
                effort = f"{effort}, was {found['fit_from']}"
            out.append(f"model follow: {harness} {entry['target']} {now['model']} -> {found['model']}"
                       + (f" ({effort})" if effort else "") + (f" · backup {backup}" if backup else ""))
            wrote = True
        merged = final_lines([*result.get("reverted", ()), *result.get("written", ())])
        for key, line in merged.items():
            out.append(f"model follow: {harness} {_LINE_NAME.get(key, key)} {_diff(line['from'], line['to'])}")
        for entry in result.get("refused", ()):
            out.append(f"model follow: {harness} {entry['target']} not changed "
                       "(the table would not accept the new lines)")
        if result.get("error"):
            out.append(f"model follow: {harness} table not updated ({result['error']})")
        if result.get("read_only"):
            out.append(f"model follow: {harness} table is read-only · not updated")
        for entry in result.get("entries", ()):
            if entry.get("branch") != "proposal":
                continue
            found, now = entry["successor"], entry["current"]
            out.append(f"model follow: proposal {harness} {entry['target']} "
                       f"{now['model']} -> {found['model']} ({entry['reason']})"
                       + (f" · {entry['command']}" if entry["command"] else ""))
        note = result.get("cli")
        if note:
            name = _CLI_NAME[note["harness"]]
            how = note["command"] or f"update the {name} CLI yourself"
            if note["kind"] == "previous-generation":
                out.append(f"model follow: {note['harness']} CLI {note['version']} lists "
                           f"{', '.join(note['models'])} as previous generation with no "
                           f"successor · {how}")
            else:
                needs = ", ".join(f"{model} (needs {version})" for model, version in note["models"][:3])
                more = len(note["models"]) - 3
                out.append(f"model follow: {note['harness']} CLI {note['version']} is older "
                           f"than {needs}{f' and {more} more' if more > 0 else ''} · {how}")
    if wrote:
        out.append("model follow: the models the field weights were measured on may differ from "
                   "the table now · re-check the weights when convenient")
    return out


def apply_lines(harnesses: Iterable[str] = HARNESSES, **kwargs) -> list[str]:
    """Follow what is to be followed in each harness's table and say what was done; never
    raises: update and refresh must not fail on it."""
    try:
        return lines([apply_harness(harness, **kwargs) for harness in harnesses])
    except Exception:
        return []


if __name__ == "__main__":  # read-only: what the survey sees, without writing anything
    for text in lines(survey()):
        print(text)
