#!/usr/bin/env python3
"""Find the newer model a user's chosen model-table cell could follow. Read-only.

One rule for every harness. It sees only rows in the chosen shape (family, version,
visible in this account, previous-generation mark, accepted efforts, price, `upgrade`)
that each harness's own catalog reader produces (`tools/fleet/explorer/model_catalog.py`);
vendor naming patterns stay in those readers. Nothing here writes the user's
`models.conf`, starts a service, refreshes a catalog, or calls a model.

The result has one entry per cell (a depth line `model/<id>:<effort>` or a tier line),
each carrying the lines that would have to change with it, so a later step that writes
only needs to apply what is listed here.
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
# Budgets `hearting model set <harness> <target> <model>@<budget>` accepts.
_SETTABLE = {"low", "medium", "high", "xhigh", "max"}
_SETTABLE_OPENCODE = _SETTABLE | {"runtime-default"}
_CLI_NAME = {"claude": "Claude Code", "codex": "Codex"}
_VERSION = re.compile(r"\d+(?:\.\d+)+")


def _numbers(text) -> tuple[int, ...] | None:
    match = _VERSION.search(text) if isinstance(text, str) else None
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


# ---- the rule ----------------------------------------------------------------------

def _fit(effort, efforts):
    """The user's effort, or the one just below it when the new model lacks it."""
    if effort is None or not efforts:
        return effort, None
    return opencode_runtime.fit_variant(effort, tuple(efforts))


def successor(rows: Iterable[Mapping], model: str, effort: str | None = None, *,
              installed: Callable[[], tuple | None] | None = None) -> dict:
    """Decide what the cell `model` should follow, from catalog rows only.

    Returns ``{"kind": "follow"|"proposal"|"none", "reason", "model", "effort",
    "fit_from", "deprecated"}``. ``installed`` lazily gives the installed CLI version
    (a tuple); a candidate that needs a newer CLI than a known-too-old or unknown one
    is not followed.
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
        if not row.get("visible"):
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
    if pointer is not None and pointer.get("visible"):
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
    fitted, fit_from = _fit(effort, chosen.get("efforts"))
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


def _swap(value: str, current: str, replacement: str, efforts) -> str | None:
    """`model:effort` entries with `current` swapped for `replacement`, or None."""
    items, changed = [], False
    for item in value.split():
        name, sep, effort = item.partition(":")
        if sep and name == current:
            effort = _fit(effort, efforts)[0]
            item, changed = f"{replacement}:{effort}", True
        items.append(item)
    return " ".join(items) if changed else None


def companions(adapter: str, values: Mapping[str, str], current: str, replacement: str,
               efforts) -> list[dict]:
    """Other lines that name `current` and so must change with its cell."""
    found = []
    swapped = _swap(values.get(CASCADE_KEY, ""), current, replacement, efforts)
    if swapped:
        found.append({"key": CASCADE_KEY, "from": values[CASCADE_KEY], "to": swapped})
    if adapter == "opencode":
        swapped = _swap(values.get(OPENCODE_FALLBACK_KEY, ""), current, replacement, efforts)
        if swapped:
            found.append({"key": OPENCODE_FALLBACK_KEY, "from": values[OPENCODE_FALLBACK_KEY],
                          "to": swapped})
    reserved = values.get(model_config.MAIN_SESSION_ONLY_KEY, "").split()
    if (adapter in ("claude", "codex") and model_config.restricted_model(current, reserved)
            and not model_config.restricted_model(replacement, reserved)):
        found.append({"key": model_config.MAIN_SESSION_ONLY_KEY,
                      "from": values[model_config.MAIN_SESSION_ONLY_KEY],
                      "add": replacement, "to": " ".join([*reserved, replacement])})
    return found


def _command(adapter, entry) -> str | None:
    model, effort = entry["successor"]["model"], entry["successor"]["effort"]
    settable = _SETTABLE_OPENCODE if adapter == "opencode" else _SETTABLE
    budget = f"@{effort}" if effort in settable else ""
    return f"hearting model set {adapter} {entry['target']} {model}{budget}"


# ---- reading the installed CLI -----------------------------------------------------

def installed_cli(harness: str) -> tuple[tuple | None, str | None]:
    """(version, path) of the harness CLI on PATH. One short `--version` call; any
    failure gives (None, ...) and the CLI notes are simply not shown."""
    path = shutil.which(harness)
    if not path:
        return None, None
    try:
        done = subprocess.run([path, "--version"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.SubprocessError):
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


def _cli_note(harness, catalog, entries, probe) -> dict | None:
    """The 'CLI is too old to list the next model' signal; needs only local facts."""
    if harness == "codex":
        names = list(dict.fromkeys(e["current"]["model"] for e in entries
                                   if e["kind"] == "none" and e["reason"] == "no-successor"
                                   and e["deprecated"]))
        if not names or not catalog.client_version:
            return None
        have, path = probe(harness)
        if have is None or have != _numbers(catalog.client_version):
            return None
        return {"harness": harness, "version": ".".join(map(str, have)), "models": names,
                "kind": "previous-generation", "command": upgrade_command(harness, path)}
    if harness == "claude":
        need = [row for row in catalog.rows if row.get("visible") and _numbers(row.get("min_cli"))]
        if not need:
            return None
        have, path = probe(harness)
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
    return model_catalog.read_catalog(harness)


def survey_harness(harness: str, *, environ: Mapping[str, str] | None = None,
                   source_root: str | Path | None = None,
                   read_catalog: Callable | None = None,
                   probe: Callable | None = None) -> dict:
    """Read-only survey of one harness; a list that cannot be read is skipped."""
    skipped = {"harness": harness, "status": "skipped", "reason": "", "entries": [], "cli": None}
    probe = probe or installed_cli
    try:
        values, receipt = model_config.resolve_config(harness, environ=environ, source_root=source_root)
        if receipt.source != "user":
            return {**skipped, "reason": "no-user-file"}
        catalog = (read_catalog or _catalog)(harness)
        if catalog.state != "available" or not catalog.rows:
            return {**skipped, "reason": "no-catalog"}
        cache = {}

        def installed():
            if "v" not in cache:
                cache["v"] = probe(harness)[0]
            return cache["v"]

        entries = []
        for cell in cells(harness, values):
            found = successor(catalog.rows, cell["model"], cell["effort"], installed=installed)
            entry = {"harness": harness, "target": cell["target"],
                     "current": {"model": cell["model"], "effort": cell["effort"]},
                     "successor": None, "kind": found["kind"], "reason": found["reason"],
                     "deprecated": found["deprecated"], "companions": [], "command": None}
            if found["model"]:
                row = next(r for r in catalog.rows if r["id"] == found["model"])
                entry["successor"] = {"model": found["model"], "effort": found["effort"],
                                      "fit_from": found["fit_from"]}
                entry["companions"] = companions(harness, values, cell["model"], found["model"],
                                                 row.get("efforts"))
                entry["command"] = _command(harness, entry)
            entries.append(entry)
        return {"harness": harness, "status": "ok", "reason": "", "entries": entries,
                "cli": _cli_note(harness, catalog, entries, probe)}
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
