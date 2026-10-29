"""Read model choices from the installed runtime's bounded catalog observation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from .types import ModelCatalog

_MAX_BYTES = 16 * 1024 * 1024


def _json(path):
    try:
        if Path(path).stat().st_size > _MAX_BYTES:
            return None
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, UnicodeError, TypeError):
        return None


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


def _candidates(harness):
    home = Path.home()
    if harness == "claude":
        config = Path(os.environ.get("HEARTING_MODEL_CONFIG_HOME") or
                      os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        return tuple(sorted((config / "cache" / "model-catalog").glob("*.json")))
    if harness == "codex":
        root = Path(os.environ.get("CODEX_HOME") or home / ".codex")
        return (root / "models_cache.json",)
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    return (cache_home / "opencode" / "models.json",)


def read_catalog(harness):
    """Return one source-qualified catalog; absence and parse failure stay unknown."""
    if harness == "opencode":
        utilities = Path(__file__).resolve().parents[3] / "utilities"
        if str(utilities) not in sys.path:
            sys.path.insert(0, str(utilities))
        import opencode_runtime
        native = opencode_runtime.read_model_catalog()
        if native is not None:
            return ModelCatalog(harness=harness, **native)
    for candidate in _candidates(harness):
        path = Path(candidate)
        document = _json(path)
        if document is None:
            continue
        if harness == "opencode" and not (isinstance(document, dict)
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
            return ModelCatalog(harness=harness, state="available", source=str(path),
                                models=models, observed_at=path.stat().st_mtime)
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
