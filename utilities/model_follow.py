#!/usr/bin/env python3
"""Find the newer model a user's chosen model-table cell could follow. Read-only.

One rule for every harness. It sees only rows in the chosen shape (family, version,
visible in this account, previous-generation mark, accepted efforts, price, `upgrade`)
that each harness's own catalog reader produces (`tools/fleet/explorer/model_catalog.py`);
vendor naming patterns stay in those readers. Nothing here writes the user's
`models.conf`, starts a service, refreshes a catalog, or calls a model.

The result has one entry per cell (a depth line `model/<id>:<effort>` or a tier line).
A cell lists only the small edits its lines need ("this item for that one", "add this
name"); `final_lines()` merges the edits of every cell into one final value per line, so
a later step that writes applies that, never one cell's edit after another's.

One survey reads one environment: the process environment, like every caller. The CLI
version is asked once per survey, and a failure or a timeout counts as that one answer.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Mapping

try:
    import model_config
    import opencode_runtime
except ImportError:  # package import in focused unit tests
    from utilities import model_config, opencode_runtime

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
              settable=_SETTABLE) -> dict:
    """Decide what the cell `model` should follow, from catalog rows only.

    Returns ``{"kind": "follow"|"proposal"|"none", "reason", "model", "effort",
    "fit_from", "deprecated"}``. ``installed`` lazily gives the installed CLI version
    (a tuple). Every candidate, the `upgrade` pointer included, must be visible in this
    account, not marked previous-generation, storable at the cell's effort, and runnable
    by the installed CLI; a candidate that needs a newer CLI than a known-too-old or
    unknown one is not followed.
    """
    rows = list(rows)
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
        if not row.get("visible") or row.get("deprecated") or _fit(effort, row.get("efforts"), settable) is None:
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
                reason = "price x%.1f" % ratio if ratio != float("inf") else "price from free"
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
                          "model": model, "effort": effort or None})
            continue
        tier = model_config.TIER_KEY.fullmatch(key)
        if tier and tier.group(2) == "MODEL":
            name = tier.group(1)
            found.append({"target": "tier/" + name.lower().replace("_", "-"), "model": value,
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
            line = found.setdefault(item["key"], {"from": item["from"], "swaps": {}, "adds": []})
            for old, new in item.get("swap", {}).items():
                line["swaps"].setdefault(old, new)
            line["adds"].extend(name for name in item.get("add", ()) if name not in line["adds"])
    final = {}
    for key, line in found.items():
        tokens = [line["swaps"].get(token, token) for token in line["from"].split()]
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


# ---- one harness, then all ---------------------------------------------------------

def _catalog(harness):
    utilities = Path(__file__).resolve().parent
    root = str(utilities.parent)
    if root not in sys.path:
        sys.path.append(root)
    from tools.fleet.explorer import model_catalog
    return model_catalog.read_catalog(harness, deadline=_SERVICE_SECONDS)


def survey_harness(harness: str, *, source_root: str | Path | None = None,
                   read_catalog: Callable | None = None,
                   probe: Callable | None = None) -> dict:
    """Read-only survey of one harness; a list that cannot be read is skipped."""
    skipped = {"harness": harness, "status": "skipped", "reason": "", "entries": [], "cli": None}
    probe = probe or installed_cli
    settable = _settable(harness)
    try:
        values, receipt = model_config.resolve_config(harness, source_root=source_root)
        if receipt.source != "user":
            return {**skipped, "reason": "no-user-file"}
        catalog = (read_catalog or _catalog)(harness)
        if catalog.state != "available" or not catalog.rows:
            return {**skipped, "reason": "no-catalog"}
        seen = {}

        def observe():
            """The CLI's (version, path), asked at most once in this survey."""
            if "cli" not in seen:
                try:
                    seen["cli"] = probe(harness)
                except Exception:
                    seen["cli"] = (None, None)
            return seen["cli"]

        entries = []
        for order, cell in enumerate(cells(harness, values)):
            found = successor(catalog.rows, cell["model"], cell["effort"],
                              installed=lambda: observe()[0], settable=settable)
            entry = {"harness": harness, "target": cell["target"], "order": order,
                     "current": {"model": cell["model"], "effort": cell["effort"]},
                     "successor": None, "kind": found["kind"], "reason": found["reason"],
                     "deprecated": found["deprecated"], "companions": [], "command": None}
            if found["model"]:
                row = next(r for r in catalog.rows if r["id"] == found["model"])
                moves = companions(harness, values, cell["model"], found["model"],
                                   row.get("efforts"))
                if moves is None:  # its lines cannot move with it: not a cell to follow
                    entry.update(kind="none", reason="lines-do-not-fit")
                else:
                    entry["successor"] = {"model": found["model"], "effort": found["effort"],
                                          "fit_from": found["fit_from"]}
                    entry["companions"] = moves
                    entry["command"] = _command(harness, entry)
            entries.append(entry)
        return {"harness": harness, "status": "ok", "reason": "", "entries": entries,
                "cli": _cli_note(harness, catalog, entries, observe)}
    except Exception as exc:  # a list that cannot be read never fails update or refresh
        return {**skipped, "reason": f"unreadable: {type(exc).__name__}"}


def survey(harnesses: Iterable[str] = HARNESSES, **kwargs) -> list[dict]:
    return [survey_harness(harness, **kwargs) for harness in harnesses]


def counts(result: Mapping) -> tuple[int, int]:
    """(newer, proposal) entries of one harness survey."""
    kinds = [entry["kind"] for entry in result.get("entries", ())]
    return kinds.count("follow"), kinds.count("proposal")


def lines(results: Iterable[Mapping]) -> list[str]:
    """Result lines for update/refresh output; nothing to say gives no line."""
    out = []
    for result in results:
        for entry in result.get("entries", ()):
            if entry["kind"] == "none":
                continue
            found, now = entry["successor"], entry["current"]
            effort = found["effort"]
            if effort and found["fit_from"]:
                effort = f"{effort}, was {found['fit_from']}"
            shown = f" ({effort})" if effort and entry["kind"] == "follow" else ""
            if entry["kind"] == "follow":
                head = (f"model follow: newer {entry['harness']} {entry['target']} "
                        f"{now['model']} -> {found['model']}{shown}")
            else:
                head = (f"model follow: proposal {entry['harness']} {entry['target']} "
                        f"{now['model']} -> {found['model']} ({entry['reason']})")
            out.append(head + (f" · {entry['command']}" if entry["command"] else ""))
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
    return out


def follow_lines(harnesses: Iterable[str] = HARNESSES, **kwargs) -> list[str]:
    """`lines(survey(...))` that never raises: update and refresh must not fail on it."""
    try:
        return lines(survey(harnesses, **kwargs))
    except Exception:
        return []


if __name__ == "__main__":
    for text in follow_lines():
        print(text)
