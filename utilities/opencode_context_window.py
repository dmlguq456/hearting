"""Resolve and cap OpenCode's installed native model limits before launch."""
from __future__ import annotations

import json
import hashlib
import os
import sys
import shutil
import subprocess
import time
from pathlib import Path
from typing import Mapping

try:
    from model_config import parse_context_window, resolve_config, resolve_context_window
    import opencode_runtime
except ImportError:  # package import in focused unit tests
    from utilities.model_config import parse_context_window, resolve_config, resolve_context_window
    from utilities import opencode_runtime


class OpenCodeContextError(ValueError):
    """The selected OpenCode model's effective native limit cannot be verified."""


def _integer_limit(limits: Mapping[str, object], key: str, *, required: bool) -> int | None:
    value = limits.get(key)
    if value is None and not required:
        return None
    if type(value) is not int or value < 0:
        raise OpenCodeContextError(f"OpenCode native model limit.{key} is unavailable or invalid")
    return value


def _json_objects(output: str):
    decoder = json.JSONDecoder()
    cursor = 0
    while True:
        start = output.find("{", cursor)
        if start < 0:
            return
        try:
            value, end = decoder.raw_decode(output, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        cursor = end
        if isinstance(value, dict):
            yield value


def _run(command: list[str], *, cwd: str, env: Mapping[str, str]) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, env=dict(env), text=True,
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpenCodeContextError(f"cannot inspect installed OpenCode model limits: {exc}") from exc
    if result.returncode != 0:
        raise OpenCodeContextError(
            f"installed OpenCode limit inspection exited {result.returncode}"
        )
    return result.stdout


def _installed_command(command: str | None = None,
                       environ: Mapping[str, str] | None = None) -> str:
    if command:
        return command
    path = None if environ is None else environ.get("PATH")
    return shutil.which("opencode", path=path) or "opencode"


def _installed_version(command: str, *, cwd: str, env: Mapping[str, str]) -> str | None:
    try:
        result = subprocess.run([command, "--version"], cwd=cwd, env=dict(env),
                                text=True, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    parsed = opencode_runtime.parse_version((result.stdout or "") + "\n" + (result.stderr or ""))
    return ".".join(str(part) for part in parsed) if parsed else None


def _resolved_config(*, cwd: str, env: Mapping[str, str], command: str) -> dict[str, object]:
    output = _run([command, "debug", "config"], cwd=cwd, env=env)
    try:
        config = json.loads(output)
    except json.JSONDecodeError as exc:
        raise OpenCodeContextError("installed OpenCode did not return resolved JSON config") from exc
    if not isinstance(config, dict):
        raise OpenCodeContextError("installed OpenCode did not return a resolved JSON object")
    return config


def _model_limits(config: Mapping[str, object], model: str) -> dict[str, int | None]:
    provider, model_id = model.split("/", 1)
    model_id = model_id.partition("#")[0]
    providers = config.get("provider")
    provider_config = providers.get(provider) if isinstance(providers, dict) else None
    models = provider_config.get("models") if isinstance(provider_config, dict) else None
    model_config = models.get(model_id) if isinstance(models, dict) else None
    limits = model_config.get("limit") if isinstance(model_config, dict) else None
    if limits is None:
        limits = {}
    if not isinstance(limits, dict):
        raise OpenCodeContextError(f"resolved OpenCode model {model} has an invalid limit object")
    return {
        "context": _integer_limit(limits, "context", required=False),
        "input": _integer_limit(limits, "input", required=False),
        "output": _integer_limit(limits, "output", required=False),
    }


def _minimum_limits(*sources: Mapping[str, int | None]) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for key in ("context", "input", "output"):
        values = [source[key] for source in sources if source.get(key) is not None]
        result[key] = min(values) if values else None
    if result["context"] is None:
        raise OpenCodeContextError("resolved OpenCode model has no native context limit")
    return result


def _api_json(path: str, *, cwd: str, env: Mapping[str, str], command: str) -> object:
    # V2's normal API client attaches to the user's managed service. A client
    # environment change cannot reconfigure that already-running process.
    # Standalone gives this bounded config-resolution probe its own server
    # initialized from the exact environment that will be passed to the worker.
    output = _run([command, "api", "--standalone", "GET", path], cwd=cwd, env=env)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise OpenCodeContextError(f"OpenCode V2 API {path} did not return JSON") from exc


def _v2_location(payload: Mapping[str, object], *, cwd: str) -> str:
    location = payload.get("location")
    directory = location.get("directory") if isinstance(location, dict) else None
    if not isinstance(directory, str) or not directory:
        raise OpenCodeContextError("OpenCode V2 API response has no location directory")
    try:
        actual = str(Path(directory).resolve(strict=True))
        expected = str(Path(cwd).resolve(strict=True))
    except OSError as exc:
        raise OpenCodeContextError(f"cannot verify OpenCode V2 location: {exc}") from exc
    if actual != expected:
        raise OpenCodeContextError(
            f"OpenCode V2 API location {actual} differs from launch directory {expected}"
        )
    return actual


def _v2_model_info(payload: object, *, cwd: str, model: str | None = None) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise OpenCodeContextError("OpenCode V2 model API response is not an object")
    _v2_location(payload, cwd=cwd)
    data = payload.get("data")
    if model is None:
        if data is None:
            raise OpenCodeContextError("OpenCode V2 default model is unavailable")
        if not isinstance(data, dict):
            raise OpenCodeContextError("OpenCode V2 default model is malformed")
        selected = data
    else:
        if not isinstance(data, list):
            raise OpenCodeContextError("OpenCode V2 model list is malformed")
        if not isinstance(model, str) or model.count("/") < 1:
            raise OpenCodeContextError("selected OpenCode model must use provider/model syntax")
        provider, model_id = model.split("/", 1)
        model_id = model_id.partition("#")[0]
        if not provider or not model_id:
            raise OpenCodeContextError("selected OpenCode model must use provider/model syntax")
        matches = [row for row in data if isinstance(row, dict)
                   and row.get("providerID") == provider and row.get("id") == model_id]
        if len(matches) != 1:
            raise OpenCodeContextError(
                f"OpenCode V2 model {provider}/{model_id} is missing or ambiguous"
            )
        selected = matches[0]
    provider_id, model_id = selected.get("providerID"), selected.get("id")
    if (not isinstance(provider_id, str) or not provider_id
            or not isinstance(model_id, str) or not model_id
            or not isinstance(selected.get("modelID"), str)
            or selected.get("enabled") is not True
            or selected.get("status") not in ("active", "alpha", "beta", "deprecated")):
        raise OpenCodeContextError("OpenCode V2 selected model is disabled or malformed")
    limits = selected.get("limit")
    if not isinstance(limits, dict):
        raise OpenCodeContextError("OpenCode V2 selected model has no limit object")
    result = {
        "providerID": provider_id,
        "id": model_id,
        "modelID": selected["modelID"],
        "status": selected["status"],
        "variants": opencode_runtime.observed_variants(selected.get("variants")),
        "limits": {
            "context": _integer_limit(limits, "context", required=True),
            "input": _integer_limit(limits, "input", required=False),
            "output": _integer_limit(limits, "output", required=True),
        },
    }
    return result


def _v2_snapshot(*, cwd: str, env: Mapping[str, str], command: str,
                 model: str | None = None) -> dict[str, object]:
    path = "/api/model/default" if model is None else "/api/model"
    for attempt in range(2):
        payload = _api_json(path, cwd=cwd, env=env, command=command)
        try:
            # Each `api --standalone` call owns a short-lived server process.
            # This is evidence about resolution of this exact config/location,
            # not a persistent server identity for a later `run` process.
            return _v2_model_info(payload, cwd=cwd, model=model)
        except OpenCodeContextError as exc:
            first_snapshot = any(marker in str(exc) for marker in (
                "default model is unavailable", "default model is malformed",
                "model list is malformed", "missing or ambiguous",
            ))
            if attempt or not first_snapshot:
                raise
            # V2 documents that a model snapshot can precede initial plugin
            # settlement. Re-read once; never poll indefinitely or invent a row.
            time.sleep(0.05)
    raise OpenCodeContextError("OpenCode V2 model snapshot remained unavailable")


def _verify_final_limits(model: str, applied: Mapping[str, int | None],
                         final: Mapping[str, int | None]) -> None:
    """Reject a higher-priority managed layer that defeats the inline cap."""
    for key in ("context", "input", "output"):
        cap = applied[key]
        if cap is None:
            continue
        actual = final[key]
        if actual is None:
            raise OpenCodeContextError(
                f"final OpenCode model {model} limit.{key} is missing after context override"
            )
        if actual > cap:
            raise OpenCodeContextError(
                f"final OpenCode model {model} limit.{key}={actual} exceeds applied cap {cap}; "
                "a higher-priority managed setting overrides OPENCODE_CONFIG_CONTENT"
            )


def _models_dev_cache_path(env: Mapping[str, str]) -> Path:
    explicit = env.get("OPENCODE_MODELS_PATH")
    if explicit:
        return Path(explicit).expanduser()
    cache_dir = env.get("HEARTING_OPENCODE_MODELS_CACHE")
    if not cache_dir:
        home = Path(env.get("HOME") or Path.home()).expanduser()
        cache_dir = str(Path(env.get("XDG_CACHE_HOME") or home / ".cache") / "opencode")
    path = Path(cache_dir).expanduser()
    return path if path.name == "models.json" else path / "models.json"


def _native_limits(model: str, *, env: Mapping[str, str]) -> dict[str, int | None]:
    """Read OpenCode's installed models.dev snapshot before user overrides."""
    if not isinstance(model, str) or model.count("/") < 1:
        raise OpenCodeContextError("selected OpenCode model must use provider/model syntax")
    provider, model_id = model.split("/", 1)
    if not provider or not model_id:
        raise OpenCodeContextError("selected OpenCode model must use provider/model syntax")
    cache = _models_dev_cache_path(env)
    try:
        if cache.is_symlink() or not cache.is_file() or cache.stat().st_size > 64 * 1024 * 1024:
            raise OpenCodeContextError("installed models.dev cache is missing or unsafe")
        catalog = json.loads(cache.read_text(encoding="utf-8"))
    except OpenCodeContextError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OpenCodeContextError(f"cannot read installed models.dev cache: {exc}") from exc
    provider_record = catalog.get(provider) if isinstance(catalog, dict) else None
    models = provider_record.get("models") if isinstance(provider_record, dict) else None
    model_record = models.get(model_id) if isinstance(models, dict) else None
    if not isinstance(model_record, dict):
        raise OpenCodeContextError(f"installed models.dev cache has no standard model record for {model}")
    limits = model_record.get("limit")
    if not isinstance(limits, dict):
        raise OpenCodeContextError(f"models.dev record for {model} has no native limit object")
    return {
        "context": _integer_limit(limits, "context", required=True),
        "input": _integer_limit(limits, "input", required=False),
        "output": _integer_limit(limits, "output", required=False),
    }


def apply_context_override(config_content: str, model: str, context_window: str | int,
                           native_limits: Mapping[str, int | None], *,
                           provider_key: str = "provider",
                           provider_keys: tuple[str, ...] | None = None
                           ) -> tuple[str, dict[str, int | None]]:
    """Add only selected-model limit fields, preserving every other config value."""
    raw = config_content.strip()
    try:
        config = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise OpenCodeContextError(f"OPENCODE_CONFIG_CONTENT is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise OpenCodeContextError("OPENCODE_CONFIG_CONTENT must contain a JSON object")
    try:
        requested = int(parse_context_window(str(context_window)), 10)
    except ValueError as exc:
        raise OpenCodeContextError(f"resolved context window is invalid: {exc}") from exc
    native_context = _integer_limit(native_limits, "context", required=True)
    native_input = _integer_limit(native_limits, "input", required=False)
    native_output = _integer_limit(native_limits, "output", required=False)
    provider, model_id = model.split("/", 1)
    model_id = model_id.partition("#")[0]

    configured_limits = []
    source_keys = tuple(dict.fromkeys((*(provider_keys or ()), provider_key)))
    for source_key in source_keys:
        source_providers = config.get(source_key)
        if source_providers is None:
            continue
        if not isinstance(source_providers, dict):
            raise OpenCodeContextError(
                f"OPENCODE_CONFIG_CONTENT {source_key} must be an object"
            )
        source_provider = source_providers.get(provider)
        if source_provider is None:
            continue
        if not isinstance(source_provider, dict):
            raise OpenCodeContextError(f"OpenCode provider {provider} in {source_key} must be an object")
        source_models = source_provider.get("models")
        if source_models is None:
            continue
        if not isinstance(source_models, dict):
            raise OpenCodeContextError(f"OpenCode provider {provider} {source_key}.models must be an object")
        source_model = source_models.get(model_id)
        if source_model is None:
            continue
        if not isinstance(source_model, dict):
            raise OpenCodeContextError(f"OpenCode model {model} config in {source_key} must be an object")
        source_limit = source_model.get("limit")
        if source_limit is None:
            continue
        if not isinstance(source_limit, dict):
            raise OpenCodeContextError(
                f"OpenCode model {model} config {source_key}.limit must be an object"
            )
        configured_limits.append({
            "context": _integer_limit(source_limit, "context", required=False),
            "input": _integer_limit(source_limit, "input", required=False),
            "output": _integer_limit(source_limit, "output", required=False),
        })

    providers = config.setdefault(provider_key, {})
    if not isinstance(providers, dict):
        raise OpenCodeContextError(f"OPENCODE_CONFIG_CONTENT {provider_key} must be an object")
    provider_config = providers.setdefault(provider, {})
    if not isinstance(provider_config, dict):
        raise OpenCodeContextError(f"OpenCode provider {provider} must be an object")
    models = provider_config.setdefault("models", {})
    if not isinstance(models, dict):
        raise OpenCodeContextError(f"OpenCode provider {provider} models must be an object")
    model_config = models.setdefault(model_id, {})
    if not isinstance(model_config, dict):
        raise OpenCodeContextError(f"OpenCode model {model} config must be an object")
    limits = model_config.setdefault("limit", {})
    if not isinstance(limits, dict):
        raise OpenCodeContextError(f"OpenCode model {model} limit must be an object")
    existing_context = _integer_limit(limits, "context", required=False)
    existing_input = _integer_limit(limits, "input", required=False)
    # The installed 1.19.0 consumer prefers limit.input to limit.context.
    # Preserve a smaller configured context cap. The installed consumer uses
    # `input` independently and prefers it for compaction, so that ceiling is
    # capped below without rewriting context to the native input ceiling.
    context_caps = [requested, native_context]
    if existing_context is not None:
        context_caps.append(existing_context)
    context_caps.extend(source["context"] for source in configured_limits
                        if source["context"] is not None)
    context = min(context_caps)
    limits["context"] = context
    input_cap = None
    if native_input is not None or existing_input is not None:
        input_caps = [context]
        if native_input is not None:
            input_caps.append(native_input)
        if existing_input is not None:
            input_caps.append(existing_input)
        input_caps.extend(source["input"] for source in configured_limits
                          if source["input"] is not None)
        input_cap = min(input_caps)
        limits["input"] = input_cap
    existing_output = _integer_limit(limits, "output", required=False)
    output_caps = [value for value in (native_output, existing_output) if value is not None]
    output_caps.extend(source["output"] for source in configured_limits
                       if source["output"] is not None)
    output_cap = min(output_caps) if output_caps else None
    if output_cap is not None:
        limits["output"] = output_cap
    result = {"context": context, "input": input_cap, "output": output_cap}
    return json.dumps(config, ensure_ascii=False, separators=(",", ":")), result


def apply_runtime_config(config: Mapping[str, object], *, environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Apply the shared budget to providers already visible to OpenCode."""
    env = dict(os.environ if environ is None else environ)
    result = json.loads(json.dumps(config))
    if not isinstance(result, dict):
        raise OpenCodeContextError("OpenCode config hook input must be an object")
    home = Path(env.get("HOME") or Path.home()).expanduser()
    runtime = Path(env.get("OPENCODE_CONFIG_DIR") or
                   Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "opencode").expanduser()
    values, _receipt = resolve_config("opencode", runtime=runtime, environ=env)
    requested, _ = resolve_context_window(values)
    providers = result.get("provider")
    if providers is None:
        providers = {}
        result["provider"] = providers
    if not isinstance(providers, dict):
        raise OpenCodeContextError("OpenCode config hook provider must be an object")
    known: set[str] = set(providers)
    for field in ("model", "small_model"):
        model = result.get(field)
        if isinstance(model, str) and "/" in model:
            known.add(model.split("/", 1)[0])

    data_home = Path(env.get("XDG_DATA_HOME") or home / ".local/share")
    auth = data_home / "opencode" / "auth.json"
    try:
        if auth.is_file() and not auth.is_symlink() and auth.stat().st_size <= 1024 * 1024:
            auth_data = json.loads(auth.read_text(encoding="utf-8"))
            if isinstance(auth_data, dict):
                known.update(str(key) for key in auth_data if isinstance(key, str))
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    state_home = Path(env.get("XDG_STATE_HOME") or home / ".local/state")
    recent_path = state_home / "opencode" / "model.json"
    try:
        recent = json.loads(recent_path.read_text(encoding="utf-8"))
        rows = recent.get("recent", []) if isinstance(recent, dict) else []
        for row in rows if isinstance(rows, list) else []:
            provider = row.get("providerID") if isinstance(row, dict) else None
            if isinstance(provider, str):
                known.add(provider)
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    try:
        catalog_path = _models_dev_cache_path(env)
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        catalog = {}
    if isinstance(catalog, dict):
        for provider, record in catalog.items():
            names = record.get("env", []) if isinstance(record, dict) else []
            if isinstance(names, str):
                names = [names]
            if isinstance(names, list) and any(isinstance(name, str) and env.get(name) for name in names):
                known.add(provider)
    disabled = result.get("disabled_providers", [])
    enabled = result.get("enabled_providers")
    disabled = set(disabled) if isinstance(disabled, list) else set()
    enabled = set(enabled) if isinstance(enabled, list) else None
    known -= disabled
    if enabled is not None:
        known &= enabled

    content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    applied = 0
    for provider in sorted(known):
        catalog_provider = catalog.get(provider) if isinstance(catalog, dict) else None
        catalog_models = catalog_provider.get("models", {}) if isinstance(catalog_provider, dict) else {}
        config_provider = providers.get(provider)
        config_models = config_provider.get("models", {}) if isinstance(config_provider, dict) else {}
        models = set(catalog_models) if isinstance(catalog_models, dict) else set()
        models.update(config_models if isinstance(config_models, dict) else ())
        for model_id in sorted(models):
            model = f"{provider}/{model_id}"
            native_record = catalog_models.get(model_id) if isinstance(catalog_models, dict) else None
            native_limits = native_record.get("limit") if isinstance(native_record, dict) else None
            native_limits = native_limits if isinstance(native_limits, dict) else {}
            configured = config_models.get(model_id) if isinstance(config_models, dict) else None
            configured_limits = configured.get("limit") if isinstance(configured, dict) else None
            configured_limits = configured_limits if isinstance(configured_limits, dict) else {}
            cap = native_limits.get("context", configured_limits.get("context"))
            if type(cap) is not int:
                continue
            input_cap = native_limits.get("input", configured_limits.get("input"))
            output_cap = native_limits.get("output", configured_limits.get("output"))
            # A config entry makes OpenCode rebuild the catalog record (dropping
            # e.g. tiered cost), so add one only where the budget lowers a limit.
            if (model_id not in config_models and cap <= int(requested)
                    and (type(input_cap) is not int or input_cap <= cap)):
                continue
            try:
                content, _limits = apply_context_override(
                    content, model, requested,
                    {"context": cap, "input": input_cap if type(input_cap) is int else None,
                     "output": output_cap if type(output_cap) is int else None})
                applied += 1
            except OpenCodeContextError:
                continue
    return json.loads(content) if applied else result


def _hook_main() -> None:
    try:
        raw = sys.stdin.readline(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return
        payload = json.loads(raw)
        config = payload.get("config") if isinstance(payload, dict) else None
        if not isinstance(config, dict):
            return
        home = Path(os.environ.get("HOME") or Path.home()).expanduser()
        runtime = Path(os.environ.get("OPENCODE_CONFIG_DIR") or
                       Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
                       / "opencode").expanduser()
        values, _receipt = resolve_config("opencode", runtime=runtime, environ=os.environ)
        requested, _source = resolve_context_window(values)
        print(json.dumps({"config": apply_runtime_config(config),
                          "context_window_request": str(requested)}, ensure_ascii=False))
    except Exception:
        # The plugin retains its input config on any helper error.
        return


def prepare_launch_context(*, model: str, context_window: str | int,
                           config_content: str, cwd: str,
                           environ: Mapping[str, str] | None = None,
                           command: str | None = None,
                           variant: str | None = None) -> dict[str, object]:
    """Inspect the installed runtime's effective model limit and prepare its cap."""
    env = dict(os.environ if environ is None else environ)
    native_command = _installed_command(command, environ=env)
    env["OPENCODE_CONFIG_CONTENT"] = config_content
    if "OPENCODE_RUNTIME_VERSION" in env:
        parsed_version = opencode_runtime.parse_version(env["OPENCODE_RUNTIME_VERSION"])
        if parsed_version is None:
            raise OpenCodeContextError("opencode-runtime-version-unavailable")
        runtime_version = ".".join(str(part) for part in parsed_version)
    else:
        runtime_version = _installed_version(native_command, cwd=cwd, env=env)
    version_parts = opencode_runtime.parse_version(runtime_version)
    is_v2 = bool(version_parts and opencode_runtime.is_v2(version=version_parts))
    if is_v2:
        model_variant = model.partition("#")[2] if isinstance(model, str) else ""
        effective_variant = (variant if variant not in (None, "", "runtime-default")
                             else (model_variant or variant))
        before_info = _v2_snapshot(
            cwd=cwd, env=env, command=native_command,
            model=None if model == "inherit" else model,
        )
        selected_model = (f"{before_info['providerID']}/{before_info['id']}"
                          if model == "inherit" else model)
        assert isinstance(selected_model, str)
        if effective_variant not in (None, "", "runtime-default"):
            variant_model = selected_model.partition("#")[0]
            observed = before_info.get("variants") or ()
            if opencode_runtime.variant_support(
                    variant_model, effective_variant, observed) == "unsupported":
                raise OpenCodeContextError(
                    f"Variant unavailable for {variant_model}: {effective_variant} "
                    f"(지원: {', '.join(observed)})")
        native_limits = dict(before_info["limits"])
        updated, applied = apply_context_override(
            config_content, selected_model, context_window, native_limits,
            provider_key="providers", provider_keys=("providers", "provider"),
        )
        final_env = {**env, "OPENCODE_CONFIG_CONTENT": updated}
        after_info = _v2_snapshot(
            cwd=cwd, env=final_env, command=native_command,
            model=None if model == "inherit" else selected_model,
        )
        if ((before_info["providerID"], before_info["id"], before_info["modelID"])
                != (after_info["providerID"], after_info["id"], after_info["modelID"])):
            raise OpenCodeContextError("OpenCode V2 selected model changed during context override")
        final_limits = dict(after_info["limits"])
        _verify_final_limits(selected_model, applied, final_limits)
        compaction = {
            "state": "unknown", "enabled": None, "trigger_tokens": None,
            "context_tokens": final_limits["context"],
            "input_tokens": final_limits["input"],
            "output_allowance_tokens": final_limits["output"],
            "configured_reserve_tokens": None, "effective_reserve_tokens": None,
            "source": "opencode-v2-model-api",
            "reason": "native-v2-compaction-behavior-unverified",
        }
        return {
            "config_content": updated,
            "model": selected_model,
            "variant": effective_variant,
            "catalog_limits": {"context": None, "input": None, "output": None},
            "configured_limits": {"context": None, "input": None, "output": None},
            "native_limits": native_limits,
            "applied_limits": applied,
            "final_limits": final_limits,
            "compaction_policy": compaction,
            "runtime_version": runtime_version,
            "native_application": "standalone-api:OPENCODE_CONFIG_CONTENT:providers-model-limit",
            "execution_mode": "standalone",
            "runtime_target": "standalone-per-command",
            # The probe and worker are separate private server processes. Do
            # not bind a probe PID to the eventual worker receipt.
            "server_identity": None,
            "location_identity": str(Path(cwd).resolve(strict=True)),
            "config_identity": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
        }
    before_config = _resolved_config(cwd=cwd, env=env, command=native_command)
    selected_model = before_config.get("model") if model == "inherit" else model
    if not isinstance(selected_model, str) or selected_model.count("/") < 1:
        raise OpenCodeContextError("selected OpenCode model is not declared by resolved native config")
    configured_limits = _model_limits(before_config, selected_model)
    try:
        catalog_limits = _native_limits(selected_model, env=env)
    except OpenCodeContextError:
        # Native custom models need no models.dev record. Their existing
        # context declaration is already the consumer's operating limit.
        if configured_limits["context"] is None:
            raise
        catalog_limits = {"context": None, "input": None, "output": None}
    limits = _minimum_limits(catalog_limits, configured_limits)
    updated, applied = apply_context_override(config_content, selected_model, context_window, limits)
    final_env = {**env, "OPENCODE_CONFIG_CONTENT": updated}
    after_config = _resolved_config(cwd=cwd, env=final_env, command=native_command)
    final_model = after_config.get("model")
    if model == "inherit" and final_model != selected_model:
        raise OpenCodeContextError(
            f"final OpenCode model {final_model!r} differs from capped inherited model {selected_model!r}"
        )
    final_limits = _model_limits(after_config, selected_model)
    _verify_final_limits(selected_model, applied, final_limits)
    compaction = after_config.get("compaction") if isinstance(after_config, dict) else None
    compaction = compaction if isinstance(compaction, dict) else {}
    auto = compaction.get("auto")
    configured_reserve = compaction.get("reserved")
    context_limit = final_limits.get("context")
    input_limit = final_limits.get("input")
    output_limit = final_limits.get("output")
    policy = {
        "state": "unknown", "enabled": None, "trigger_tokens": None,
        "context_tokens": _integer_limit(final_limits, "context", required=False),
        "input_tokens": _integer_limit(final_limits, "input", required=False),
        "output_allowance_tokens": _integer_limit(final_limits, "output", required=False),
        "configured_reserve_tokens": None, "effective_reserve_tokens": None,
        "source": "opencode-v1.19.0-session-overflow",
        "reason": "native-compaction-input-unavailable",
    }
    if type(configured_reserve) is int and configured_reserve >= 0:
        policy["configured_reserve_tokens"] = configured_reserve
    if runtime_version != "1.19.0":
        policy.update(reason="native-consumer-version-unverified")
    elif auto is False or context_limit == 0:
        policy.update(state="disabled", enabled=False, reason="native-auto-compaction-disabled")
    elif auto in (None, True):
        output_allowance = policy["output_allowance_tokens"]
        if policy["configured_reserve_tokens"] is not None:
            reserve = policy["configured_reserve_tokens"]
        elif output_allowance is not None:
            reserve = min(20_000, output_allowance)
        else:
            reserve = None
        trigger = None
        effective_reserve = None
        if (type(input_limit) is int and input_limit > 0 and reserve is not None):
            trigger = max(0, input_limit - reserve)
            effective_reserve = reserve
        elif ((input_limit is None or type(input_limit) is int and input_limit == 0)
              and type(context_limit) is int and context_limit >= 0
              and output_allowance is not None):
            trigger = max(0, context_limit - output_allowance)
            effective_reserve = output_allowance
        if trigger is not None:
            policy.update(state="applied", enabled=True, trigger_tokens=trigger,
                          effective_reserve_tokens=effective_reserve, reason=None)
        else:
            policy.update(enabled=True, reason="native-output-or-input-limit-unavailable")
    else:
        policy.update(reason="invalid-native-compaction-auto-setting")
    return {
        "config_content": updated,
        "model": selected_model,
        "catalog_limits": catalog_limits,
        "configured_limits": configured_limits,
        "native_limits": limits,
        "applied_limits": applied,
        "final_limits": final_limits,
        "compaction_policy": policy,
        "runtime_version": runtime_version,
        "variant": variant,
        "native_application": "OPENCODE_CONFIG_CONTENT:provider-model-limit",
        "server_identity": None,
        "location_identity": str(Path(cwd).resolve()),
        "config_identity": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
    }


if __name__ == "__main__":
    _hook_main()
