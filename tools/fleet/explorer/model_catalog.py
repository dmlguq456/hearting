"""Read model choices from the installed runtime's bounded catalog observation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys

from .types import ModelCatalog

_MAX_BYTES = 16 * 1024 * 1024


def _json(path):
    """One list file as JSON, or None. Only a regular file is read: the kind is checked on
    the opened file (so a swap after any earlier look cannot matter), and the open itself
    never waits, so a pipe, socket, or device in a list's place is skipped at once."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    except (OSError, ValueError, TypeError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            return None
        with os.fdopen(fd, encoding="utf-8", closefd=False) as stream:
            return json.load(stream)
    except (OSError, ValueError, UnicodeError, TypeError):
        return None
    finally:
        os.close(fd)


def _ids(value):
    """Extract model identifiers from the bounded public catalog record shapes."""
    found = []
    seen = set()

    def add(item, fallback=""):
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = next((item.get(key) for key in ("id", "slug", "model")
                         if isinstance(item.get(key), str) and item.get(key).strip()), fallback)
            if not name and isinstance(item.get("name"), str):
                name = item["name"].strip()
        else:
            name = ""
        if name and name not in seen:
            seen.add(name)
            found.append(name)

    if isinstance(value, list):
        for item in value:
            add(item)
    elif isinstance(value, dict):
        direct = value.get("models")
        if isinstance(direct, list):
            for item in direct:
                add(item)
        elif isinstance(direct, dict):
            for key, item in direct.items():
                add(item if isinstance(item, dict) else key)
        providers = value.get("providers")
        if isinstance(providers, dict):
            for provider in providers.values():
                models = provider.get("models") if isinstance(provider, dict) else None
                if isinstance(models, dict):
                    for key, item in models.items():
                        add(item, key)
                elif isinstance(models, list):
                    for item in models:
                        add(item)
        elif not direct:
            # OpenCode's cache is keyed directly by provider id, then model id.
            for provider in value.values():
                models = provider.get("models") if isinstance(provider, dict) else None
                if isinstance(models, dict):
                    for key, item in models.items():
                        add(item, key)
                elif isinstance(models, list):
                    for item in models:
                        add(item)
    return tuple(found)


def _claude_ids(document):
    """Claude's choices, or None when the cache is not a shape this reader knows.

    The current cache nests model records under ``catalog.config.models`` and names the
    aliases the runtime accepts in ``provider_alias_targets``. An alias stays a choice of
    its own (the runtime resolves it per provider) when one of its targets is a listed
    model. Only an explicitly empty model list is an empty catalog.
    """
    catalog = document.get("catalog") if isinstance(document, dict) else None
    config = catalog.get("config") if isinstance(catalog, dict) else None
    if not (isinstance(config, dict) and "models" in config):
        models = _ids(document)
        direct = document.get("models") if isinstance(document, dict) else document
        return models or (() if isinstance(direct, (list, dict)) and not direct else None)
    records = config["models"]
    if not isinstance(records, list):
        return None
    listed = tuple(dict.fromkeys(item["id"].strip() for item in records if isinstance(item, dict)
                                 and isinstance(item.get("id"), str) and item["id"].strip()))
    if records and not listed:
        return None
    aliases = []
    targets = config.get("provider_alias_targets")
    for alias, target in (targets.items() if isinstance(targets, dict) else ()):
        if isinstance(target, dict):
            per_provider = target.get("per_provider")
            target = (target.get("default"), *(per_provider.values() if isinstance(per_provider, dict) else ()))
        else:
            target = (target,)
        if isinstance(alias, str) and alias.strip() and any(
                isinstance(model, str) and model in listed for model in target):
            aliases.append(alias.strip())
    return tuple(dict.fromkeys((*aliases, *listed)))


# --- Chosen-shape rows for the model-follow rule (utilities/model_follow.py). ---
# The rule only sees rows; the vendor naming patterns that make a family and a
# version out of a model name stay here, in each harness's own reader. A name that
# does not match its pattern gets no family, and the rule never follows it.

def _version(text):
    match = re.search(r"\d+(?:\.\d+)*", text)
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def _levels(value):
    """Effort or variant names a listed model accepts, or None when not stated."""
    if isinstance(value, dict):
        value = list(value)
    if not isinstance(value, (list, tuple)):
        return None
    found = []
    for item in value:
        if isinstance(item, dict):
            item = next((item.get(key) for key in ("effort", "id", "level", "name", "value")
                         if isinstance(item.get(key), str)), None)
        if isinstance(item, str) and item.strip() and item.strip() not in found:
            found.append(item.strip())
    return tuple(found) or None


def _row(identifier, family, version, *, visible=True, deprecated=False, efforts=None,
         cost=None, upgrade=None, min_cli=None):
    return {"id": identifier, "family": family, "version": version, "visible": bool(visible),
            "deprecated": bool(deprecated), "efforts": efforts, "cost": cost,
            "upgrade": upgrade, "min_cli": min_cli}


_CLAUDE_ID = re.compile(r"^claude-([a-z][a-z0-9]*)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$")


def _claude_rows(document):
    catalog = document.get("catalog") if isinstance(document, dict) else None
    config = catalog.get("config") if isinstance(catalog, dict) else None
    records = config.get("models") if isinstance(config, dict) else None
    rows = []
    for record in records if isinstance(records, list) else ():
        identifier = record.get("id") if isinstance(record, dict) else None
        if not isinstance(identifier, str) or not identifier.strip():
            continue
        identifier = identifier.strip()
        runtime = record.get("runtime") if isinstance(record.get("runtime"), dict) else {}
        match = _CLAUDE_ID.match(identifier)
        family = runtime.get("family") if isinstance(runtime.get("family"), str) else None
        family = (family or (match.group(1) if match else "")).strip().lower() or None
        version = (int(match.group(2)), int(match.group(3) or 0)) if match else None
        section = record.get("section")
        min_cli = record.get("min_claude_code_version", runtime.get("min_claude_code_version"))
        rows.append(_row(
            identifier, family if version else None, version,
            visible=section == "main", deprecated=section == "overflow",
            efforts=_levels(record.get("effort_levels", runtime.get("effort_levels"))),
            min_cli=min_cli if isinstance(min_cli, str) else None))
    return tuple(rows)


_CODEX_SLUG = re.compile(r"^gpt-(\d+(?:\.\d+)*)-([a-z][a-z0-9]*)$")


def _codex_rows(document):
    records = document.get("models") if isinstance(document, dict) else None
    rows = []
    for record in records if isinstance(records, list) else ():
        slug = record.get("slug") if isinstance(record, dict) else None
        if not isinstance(slug, str) or not slug.strip():
            continue
        slug = slug.strip()
        match = _CODEX_SLUG.match(slug)
        description = record.get("description")
        upgrade = record.get("upgrade")
        upgrade = upgrade.get("model") if isinstance(upgrade, dict) else upgrade
        rows.append(_row(
            slug, match.group(2) if match else None,
            tuple(int(part) for part in match.group(1).split(".")) if match else None,
            visible=record.get("visibility") == "list" and record.get("supported_in_api") is not False,
            deprecated=isinstance(description, str) and description.strip().lower().startswith(
                ("previous generation", "legacy")),
            efforts=_levels(record.get("supported_reasoning_levels")),
            upgrade=upgrade if isinstance(upgrade, str) and upgrade.strip() else None))
    return tuple(rows)


def _opencode_row(identifier, family, status, cost, efforts):
    provider, _, model = identifier.partition("/")
    number = re.search(r"\d+(?:\.\d+)*", model)
    if not (isinstance(family, str) and family.strip() and number):
        family_key, version = None, None
    else:
        stem = model[:number.start()] + "#" + model[number.end():]
        family_key = f"{provider}|{family.strip().lower()}|{stem}"
        version = _version(number.group(0))
    cost = cost.get("input") if isinstance(cost, dict) else cost
    cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    return _row(identifier, family_key, version, visible=status != "deprecated",
                deprecated=status == "deprecated", efforts=efforts, cost=cost)


def _opencode_native_rows(native):
    records = native.get("records")
    variants = native.get("variants") or {}
    return tuple(_opencode_row(identifier, record.get("family"), record.get("status"),
                               record.get("cost"), variants.get(identifier))
                 for identifier, record in (records.items() if isinstance(records, dict) else ())
                 if isinstance(record, dict))


def _opencode_cache_rows(document):
    records = _opencode_provider_records(document)
    connected = _opencode_connected_providers(document)
    rows = []
    for provider, record in records.items():
        models = record.get("models") if provider in connected and isinstance(record, dict) else None
        if not isinstance(models, dict):
            continue
        for key, item in models.items():
            if not isinstance(item, dict):
                continue
            model = _opencode_model_identifier(item, key)
            if model:
                rows.append(_opencode_row(f"{provider}/{model}", item.get("family"),
                                          item.get("status"), item.get("cost"), None))
    return tuple(rows)


def _candidates(harness):
    home = Path.home()
    if harness == "claude":
        config = Path(os.environ.get("HEARTING_MODEL_CONFIG_HOME") or
                      os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        # One cache per account; the names carry no order, so the most recently written
        # snapshot is the runtime's latest observation. An older one never stands in.
        written = []
        for path in (config / "cache" / "model-catalog").glob("*.json"):
            try:
                written.append((-path.stat().st_mtime_ns, path.name, path))
            except OSError:
                continue
        return tuple(path for _mtime, _name, path in sorted(written)[:1])
    if harness == "codex":
        root = Path(os.environ.get("CODEX_HOME") or home / ".codex")
        return (root / "models_cache.json",)
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    return (cache_home / "opencode" / "models.json",)


def read_catalog(harness, *, deadline=None):
    """Return one source-qualified catalog; absence and parse failure stay unknown.

    ``deadline`` (seconds) ends the OpenCode service read as a whole; only a caller that
    asks for it (the model-follow survey) gets it, so Fleet reads as before.
    """
    if harness == "opencode":
        utilities = Path(__file__).resolve().parents[3] / "utilities"
        if str(utilities) not in sys.path:
            sys.path.insert(0, str(utilities))
        import opencode_runtime
        native = opencode_runtime.read_model_catalog(deadline=deadline)
        if native is not None:
            fields = {key: value for key, value in native.items() if key != "records"}
            return ModelCatalog(harness=harness, rows=_opencode_native_rows(native), **fields)
    for candidate in _candidates(harness):
        path = Path(candidate)
        document = _json(path)
        if document is None:
            continue
        if harness == "claude":
            models = _claude_ids(document)
            if models is None:
                continue
        elif harness == "opencode" and not (isinstance(document, dict)
                                               and isinstance(document.get("models"), list)):
            models = _opencode_ids(document)
        else:
            models = _ids(document)
        if (harness == "codex" and isinstance(document, dict)
                and isinstance(document.get("models"), list)
                and any(isinstance(item, dict) and "visibility" in item for item in document["models"])):
            models = _ids([item for item in document["models"]
                           if isinstance(item, dict) and item.get("visibility") == "list"])
        if models:
            if harness == "claude":
                rows = _claude_rows(document)
            elif harness == "codex":
                rows = _codex_rows(document)
            else:
                rows = _opencode_cache_rows(document)
            client = document.get("client_version") if isinstance(document, dict) else None
            return ModelCatalog(harness=harness, state="available", source=str(path),
                                models=models, observed_at=path.stat().st_mtime, rows=rows,
                                client_version=client if isinstance(client, str) else None)
        return ModelCatalog(harness=harness, state="empty", source=str(path),
                            observed_at=path.stat().st_mtime)
    return ModelCatalog(harness=harness)


def home_path():
    return str(Path.home() / ".local" / "share")


def _opencode_configured_providers():
    """Return explicitly configured provider ids without examining credentials."""
    configured = set()
    content = os.environ.get("OPENCODE_CONFIG_CONTENT")
    if content:
        try:
            config = json.loads(content) if len(content.encode("utf-8")) <= _MAX_BYTES else None
        except (TypeError, ValueError, UnicodeError):
            config = None
        providers = config.get("provider") if isinstance(config, dict) else None
        if isinstance(providers, dict):
            configured.update(key for key, value in providers.items() if isinstance(value, dict))
    override = os.environ.get("OPENCODE_CONFIG")
    if override:
        candidates = (Path(override),)
    else:
        config_dir = os.environ.get("OPENCODE_CONFIG_DIR")
        if config_dir:
            candidates = (Path(config_dir) / "opencode.json",)
        else:
            config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            candidates = (config_home / "opencode" / "opencode.json",)
    for path in candidates:
        config = _json(path)
        providers = config.get("provider") if isinstance(config, dict) else None
        if isinstance(providers, dict):
            configured.update(key for key, value in providers.items() if isinstance(value, dict))
    return configured


def _opencode_provider_records(document):
    if not isinstance(document, dict):
        return {}
    providers = document.get("providers")
    if isinstance(providers, dict):
        return providers
    # A direct models field is a flat catalog shape, not a provider map.
    if "models" in document:
        return {}
    return document


def _opencode_connected_providers(document):
    """Use cached auth ids, present provider env vars, or explicit config only."""
    records = _opencode_provider_records(document)
    auth_path = Path(os.environ.get("XDG_DATA_HOME") or home_path()) / "opencode" / "auth.json"
    auth = _json(auth_path)
    authenticated = set(auth) if isinstance(auth, dict) else set()
    configured = _opencode_configured_providers()
    connected = set()
    for provider, record in records.items():
        if provider in authenticated or provider in configured:
            connected.add(provider)
            continue
        names = record.get("env") if isinstance(record, dict) else None
        if isinstance(names, str):
            names = (names,)
        if isinstance(names, (list, tuple)) and any(
                isinstance(name, str) and bool(os.environ.get(name, "").strip()) for name in names):
            connected.add(provider)
    return connected


def _opencode_model_identifier(item, fallback=""):
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        name = next((item.get(key) for key in ("id", "slug", "model")
                     if isinstance(item.get(key), str) and item.get(key).strip()), "")
        if name:
            return name.strip()
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()
        if isinstance(item.get("name"), str):
            return item["name"].strip()
    return fallback.strip() if isinstance(fallback, str) else ""


def _opencode_ids(document):
    records = _opencode_provider_records(document)
    if not records:
        return _ids(document)
    connected = _opencode_connected_providers(document)
    found = []
    seen = set()
    for provider, record in records.items():
        if provider not in connected or not isinstance(provider, str) or not isinstance(record, dict):
            continue
        models = record.get("models")
        if isinstance(models, dict):
            entries = models.items()
        elif isinstance(models, list):
            entries = (("", item) for item in models)
        else:
            continue
        for key, item in entries:
            model = _opencode_model_identifier(item, key)
            if model:
                identifier = f"{provider}/{model}"
                if identifier not in seen:
                    seen.add(identifier)
                    found.append(identifier)
    return tuple(found)


def read_catalogs():
    """Read existing runtime catalogs; never start a service or contact a provider."""
    return {harness: read_catalog(harness) for harness in ("claude", "codex", "opencode")}
