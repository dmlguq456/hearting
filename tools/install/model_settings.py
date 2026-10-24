"""Explicit, ownership-preserving edits to a runtime's selected model config."""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utilities"))
import model_config
import model_profile
import safe_fs


class ModelSettingsError(ValueError):
    pass


class ModelSettingsUsageError(ModelSettingsError):
    """A user supplied an unknown or malformed command argument."""


_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_BUDGETS = {
    "claude": {"low", "medium", "high", "xhigh", "max"},
    "codex": {"low", "medium", "high", "xhigh", "max"},
    "opencode": {"low", "medium", "high", "xhigh", "max", "runtime-default"},
}


def _norm(value: str) -> str:
    return re.sub(r"[-_ ]+", "-", value.strip().lower())


def _key_name(value: str) -> str:
    return re.sub(r"[- ]+", "_", value.strip().upper())


def _resolve_target(adapter: str, raw: str, values: dict[str, str], *,
                    source_root: str | Path | None = None) -> dict[str, str]:
    kind, name = "", raw
    if "/" in raw:
        prefix, suffix = raw.split("/", 1)
        if prefix in {"tier", "profile", "role"}:
            kind, name = prefix, suffix
    name_norm = _norm(name)
    if not name_norm:
        raise ModelSettingsUsageError("target name is empty")

    tiers = sorted({m.group(1) for key in values if (m := model_config.TIER_KEY.fullmatch(key))})
    profiles = [key[len("CFG_MODEL_PROFILE_"):].lower().replace("_", "-")
                for key in values if key.startswith("CFG_MODEL_PROFILE_")
                and key != "CFG_MODEL_PROFILE_GRANULARITY"
                and not key.startswith("CFG_MODEL_PROFILE_GRANULARITY_")]
    if "CFG_MODEL_PROFILE_BALANCED" not in values and "balanced" not in profiles:
        profiles.append("balanced")
    if "top" not in profiles:
        profiles.append("top")  # An explicit edit is the only way to opt in.
    role_matches = []
    for key, text in values.items():
        if key.startswith("CFG_ROLES_"):
            for role in text.split("|"):
                if _norm(role) == name_norm:
                    role_matches.append((role.strip(), key.removeprefix("CFG_ROLES_")))

    tier = next((t for t in tiers if _norm(t) == name_norm), None)
    profile = next((p for p in profiles if _norm(p) == name_norm), None)
    if adapter == "opencode" and (kind == "role" or not (kind or tier or profile)):
        # OpenCode declares role families in its existing consumer, rather than
        # CFG_ROLES_* rows. Consult that routing surface without a model call.
        root = (Path(source_root) if source_root is not None else
                model_config.repository_root())
        mapped = subprocess.run(
            [str(root / "adapters/opencode/bin/role-map.sh"), name_norm.replace("-", " ")],
            text=True, capture_output=True, timeout=10)
        if mapped.returncode not in (0, 64):
            raise ModelSettingsError(f"cannot resolve OpenCode role: {mapped.stderr.strip()}")
        routing = dict(line.split("=", 1) for line in mapped.stdout.splitlines() if "=" in line)
        family_tier = {"fast": "MINI", "balanced": "LIGHT",
                       "deep": "DEEP" if "DEEP" in tiers else "BALANCED_DEEP"}
        routed_tier = family_tier.get(routing.get("family")) if mapped.returncode == 0 else None
        # The external family has no editable config tier. Environment overrides
        # remain native inputs of role-map.sh and are never written here.
        role_matches = [(routing["role"], routed_tier)] if routed_tier in tiers else []
    role = role_matches[0] if role_matches else None
    if kind == "tier" and tier:
        pass
    elif kind == "profile" and profile:
        pass
    elif kind == "role" and role:
        tier = role[1]
    elif kind:
        raise ModelSettingsUsageError(f"unknown {kind} target: {name}")
    elif tier:
        kind = "tier"
    elif profile:
        kind = "profile"
    elif role:
        kind, tier = "role", role[1]
    else:
        raise ModelSettingsUsageError(f"unknown target: {raw}")
    if kind == "tier":
        if not tier:
            raise ModelSettingsUsageError(f"unknown tier: {name}")
        return {"kind": kind, "name": tier.lower().replace("_", "-"), "tier": tier}
    if kind == "profile":
        if not profile:
            raise ModelSettingsUsageError(f"unknown profile: {name}")
        return {"kind": kind, "name": profile, "profile": profile}
    if kind == "role":
        if not role:
            raise ModelSettingsUsageError(f"unknown role: {name}")
        return {"kind": kind, "name": role[0], "tier": role[1], "shared": True}
    raise ModelSettingsUsageError(f"invalid target: {raw}")


def _parse_model(adapter: str, requested: str) -> tuple[str, str | None]:
    if not requested or requested != requested.strip() or any(ord(c) < 32 for c in requested):
        raise ModelSettingsUsageError("model must be a non-empty safe identifier")
    if requested.count("@") > 1:
        raise ModelSettingsUsageError("model may contain at most one @budget suffix")
    if "@" in requested:
        model, budget = requested.rsplit("@", 1)
        if budget not in _BUDGETS[adapter]:
            raise ModelSettingsUsageError(f"unsupported {adapter} budget: {budget!r}")
    else:
        model, budget = requested, None
    if not _MODEL.fullmatch(model):
        raise ModelSettingsUsageError("model must use letters, digits, dot, underscore, slash, or hyphen")
    return model, budget


def _profile_budget(adapter: str, budget: str) -> str:
    return budget


def _edit_bytes(raw: bytes, changed: dict[str, str]) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModelSettingsError("configuration is not valid UTF-8") from exc
    lines = text.splitlines(keepends=True)
    preferred_newline = "\r\n" if "\r\n" in text else "\n"
    found: set[str] = set()
    for i, line in enumerate(lines):
        match = model_config.ASSIGNMENT.fullmatch(line.rstrip("\r\n").strip())
        if match and match.group(1) in changed:
            key = match.group(1)
            # Replace only the assignment value; retain indentation, comment, and newline.
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            body = line[:-len(newline)] if newline else line
            comment = ""
            value = match.group(2)
            comment_pos = value.find("#")
            if comment_pos >= 0:
                comment = " " + value[comment_pos:].lstrip()
            prefix = body[:body.find(key) + len(key)]
            lines[i] = f"{prefix}={model_config._shell_quote(changed[key])}{comment}{newline}"
            found.add(key)
    missing = [key for key in changed if key not in found]
    if missing:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += preferred_newline
        lines.extend(f"{key}={model_config._shell_quote(changed[key])}{preferred_newline}" for key in missing)
    return "".join(lines).encode("utf-8")


def _candidate(adapter: str, values: dict[str, str], target: dict[str, str],
               model: str, budget: str | None) -> tuple[dict[str, str], dict[str, str]]:
    changed: dict[str, str] = {}
    suffix = "VARIANT" if adapter == "opencode" else "EFFORT"
    if target["kind"] == "scalar":
        changed[target["key"]] = model
    elif target["kind"] in {"tier", "role"}:
        tier = target["tier"]
        key = f"CFG_TIER_{tier}_MODEL"
        changed[key] = model
        if budget is not None:
            changed[f"CFG_TIER_{tier}_{suffix}"] = budget
    else:
        profile = target["profile"]
        profile_values = values
        if profile == "balanced" and "CFG_MODEL_PROFILE_BALANCED" not in values:
            profile_values = model_config._derive_balanced_values(adapter, values)
        if profile == "top" and "CFG_MODEL_PROFILE_TOP" not in values:
            try:
                deep = model_profile.resolve_profile_values(adapter, values, "deep")
            except ValueError as exc:
                raise ModelSettingsError(f"cannot resolve deep profile for top declaration: {exc}") from exc
            selected_budget = budget if budget is not None else deep["budget"]
            changed["CFG_MODEL_PROFILE_TOP"] = f"model/{model}:{_profile_budget(adapter, selected_budget)}"
            return dict(values), changed
        try:
            current = model_profile.resolve_profile_values(adapter, profile_values, profile)
        except ValueError as exc:
            raise ModelSettingsError(f"cannot resolve current {profile} profile: {exc}") from exc
        selected_budget = budget if budget is not None else current["budget"]
        changed[f"CFG_MODEL_PROFILE_{_key_name(profile)}"] = f"model/{model}:{_profile_budget(adapter, selected_budget)}"
    return dict(values), changed


def _backup(path: Path, payload: bytes, mode: int) -> Path:
    stem = path.with_name(path.name + ".bak")
    for suffix in range(1000):
        candidate = stem if suffix == 0 else stem.with_name(stem.name + f".{suffix}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            continue
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            safe_fs._fsync_parent(candidate)
            return candidate
        except BaseException:
            # destructive-ok: reason=discard an incomplete exclusive backup; boundary=one backup leaf created with O_EXCL by this invocation
            candidate.unlink(missing_ok=True)
            raise
    raise ModelSettingsError(f"could not allocate an exclusive backup beside {path}")


def set_model(adapter: str, target_name: str, requested: str, *, runtime: str | Path | None = None,
              environ: dict[str, str] | None = None, source_root: str | Path | None = None,
              dry_run: bool = False, _locked: bool = False) -> dict[str, object]:
    if adapter not in model_config.ADAPTERS:
        raise ModelSettingsError(f"unknown harness: {adapter!r}")
    context_target = _norm(target_name) == "context-window" and "/" not in target_name
    if context_target:
        try:
            model = model_config.parse_context_window(requested)
        except model_config.ModelConfigError as exc:
            raise ModelSettingsUsageError(str(exc)) from exc
        explicit_budget = None
    else:
        model, explicit_budget = _parse_model(adapter, requested)
    runtime_path = model_config.runtime_home(adapter, runtime, environ)
    path = model_config.user_path(adapter, runtime=runtime_path)
    shipped = model_config.shipped_path(adapter, source_root=source_root)
    missing = not path.exists() and not path.is_symlink()
    if missing:
        raw = shipped.read_bytes()
        mode = stat.S_IMODE(shipped.stat().st_mode)
        values = model_config.parse_config(shipped)
        reason = "user-missing; shipped seed"
    else:
        if path.is_symlink() or not path.is_file():
            raise ModelSettingsError(f"user config must be a regular non-symlink file: {path}")
        info = os.lstat(path)
        if info.st_nlink != 1:
            raise ModelSettingsError(f"user config has a hardlink alias: {path}")
        raw = path.read_bytes()
        mode = stat.S_IMODE(info.st_mode)
        try:
            values, receipt = model_config.resolve_config(adapter, runtime=runtime_path,
                                                         source_root=source_root)
        except model_config.ModelConfigError as exc:
            raise ModelSettingsError(f"cannot select user configuration: {exc}") from exc
        if receipt.source != "user":
            raise ModelSettingsError(f"existing user config preserved: {receipt.reason} ({path})")
        # The complete selected values may contain read-time balanced derivation. Keep raw source
        # declarations authoritative for unrelated edits; only a requested missing profile is materialized.
        try:
            values = model_config.parse_config(path, allow_symlink=False)
        except model_config.ModelConfigError as exc:
            raise ModelSettingsError(f"existing user config is malformed: {exc}") from exc
        reason = receipt.reason
    target = ({"kind": "scalar", "name": "context-window",
               "key": model_config.CONTEXT_WINDOW_KEY} if context_target else
              _resolve_target(adapter, target_name, values, source_root=source_root))
    updated, changed = _candidate(adapter, values, target, model, explicit_budget)
    candidate = _edit_bytes(raw, changed)
    parsed = _parse_bytes(candidate)
    with tempfile.TemporaryDirectory(prefix="hearting-model-selected-") as isolated:
        isolated_path = Path(isolated) / "agent-config" / "models.conf"
        isolated_path.parent.mkdir()
        isolated_path.write_bytes(candidate)
        _resolved, receipt = model_config.resolve_config(
            adapter, runtime=Path(isolated), source_root=source_root)
        if receipt.source != "user" or Path(receipt.selected_path) != isolated_path:
            raise ModelSettingsError(
                f"candidate was not selected as the complete user file: {receipt.reason}")
    declared_profiles = [key[len("CFG_MODEL_PROFILE_"):].lower().replace("_", "-")
                         for key in parsed if key.startswith("CFG_MODEL_PROFILE_")
                         and key != "CFG_MODEL_PROFILE_GRANULARITY"
                         and not key.startswith("CFG_MODEL_PROFILE_GRANULARITY_")]
    for declared_profile in declared_profiles:
        try:
            model_profile.resolve_profile_values(adapter, parsed, declared_profile)
        except ValueError as exc:
            raise ModelSettingsError(
                f"candidate profile {declared_profile} is invalid: {exc}") from exc
    if target["kind"] == "scalar":
        try:
            model_config.resolve_context_window(parsed)
        except model_config.ModelConfigError as exc:
            raise ModelSettingsError(f"candidate context window is invalid: {exc}") from exc
    elif target["kind"] == "profile":
        try:
            model_profile.resolve_profile_values(adapter, parsed, target["profile"])
        except ValueError as exc:
            raise ModelSettingsError(f"candidate profile is invalid: {exc}") from exc
    else:
        tier = target["tier"]
        if not parsed.get(f"CFG_TIER_{tier}_MODEL"):
            raise ModelSettingsError(f"candidate does not declare tier {tier}")
    context_fields: dict[str, object] = {}
    if target["kind"] == "scalar":
        old_context_window = model_config.resolve_context_window(values)[0]
        new_context_window = model_config.resolve_context_window(parsed)[0]
        old_model = old_budget = new_model = new_budget = None
        unchanged = (target["key"] in values and old_context_window == new_context_window)
        context_fields = {
            "requested_context_window": new_context_window,
            "old_context_window": old_context_window,
            "new_context_window": new_context_window,
        }
    else:
        old_profile_values = values
        if target.get("profile") == "balanced" and "CFG_MODEL_PROFILE_BALANCED" not in values:
            old_profile_values = model_config._derive_balanced_values(adapter, values)
        old_profile = (None if target.get("profile") == "top" and "CFG_MODEL_PROFILE_TOP" not in values else
                       model_profile.resolve_profile_values(adapter, old_profile_values, target["profile"])
                       if "profile" in target else None)
        old_model = (values.get(f"CFG_TIER_{target['tier']}_MODEL") if "tier" in target else
                     old_profile["model"] if old_profile else None)
        old_budget = (values.get(f"CFG_TIER_{target['tier']}_{'VARIANT' if adapter == 'opencode' else 'EFFORT'}") if "tier" in target else
                      old_profile["budget"] if old_profile else None)
        new_values = parsed
        new_model = (new_values.get(f"CFG_TIER_{target['tier']}_MODEL") if "tier" in target else
                     model_profile.resolve_profile_values(adapter, new_values, target["profile"])["model"])
        new_budget = (new_values.get(f"CFG_TIER_{target['tier']}_{'VARIANT' if adapter == 'opencode' else 'EFFORT'}") if "tier" in target else
                      model_profile.resolve_profile_values(adapter, new_values, target["profile"])["budget"])
        unchanged = old_model == new_model and (explicit_budget is None or old_budget == new_budget)
    if unchanged:
        projected = None
        if target["kind"] == "scalar" and not dry_run and adapter in {"claude", "codex"}:
            try:
                from native_context import project_context_window
                if adapter == "claude":
                    import runtime_activation
                    projected = project_context_window(
                        adapter, runtime=runtime_path, source_root=source_root,
                        claude_merge=lambda **kwargs: runtime_activation._merge_claude_settings(
                            Path(source_root or model_config.repository_root()), None,
                            "global", config_path=runtime_path / "settings.json", **kwargs),
                        context_only=True)
                else:
                    projected = project_context_window(adapter, runtime=runtime_path,
                                                       source_root=source_root)
            except Exception as exc:
                raise ModelSettingsError(
                    f"models.conf is unchanged at {path}; native context projection failed: {exc}") from exc
        return {"operation": "model-set", "status": "unchanged", "exit": 0,
                "runtime": adapter, "config_path": str(path), "target": target,
                "requested_model": requested, "old_model": old_model, "new_model": new_model,
                "old_budget": old_budget, "new_budget": new_budget, "changed_keys": [],
                "backup": None, "source_reason": reason,
                "lines": ([f"model set: unchanged {adapter} context-window -> {new_context_window} tokens",
                           *(projected or {}).get("report_lines", [])]
                          if target["kind"] == "scalar" else
                          [f"model set: unchanged {adapter} {target['name']} -> {old_model}"]),
                **context_fields}
    if dry_run:
        return {"operation": "model-set", "status": "planned", "exit": 0,
                "runtime": adapter, "config_path": str(path), "target": target,
                "requested_model": requested, "old_model": old_model, "new_model": new_model,
                "old_budget": old_budget, "new_budget": new_budget, "changed_keys": sorted(changed),
                "backup": None, "source_reason": reason,
                "lines": ([f"model set: would update {adapter} context-window -> {new_context_window} tokens"]
                          if target["kind"] == "scalar" else
                          [f"model set: would update {adapter} {target['name']} -> {new_model}"]),
                **context_fields}
    if not _locked:
        # Do all authoritative reads again after acquiring the stable canonical lock.
        # Concurrent cooperating edits therefore compose instead of losing keys.
        path.parent.mkdir(parents=True, exist_ok=True)
        with safe_fs.TargetLock(path):
            return set_model(adapter, target_name, requested, runtime=runtime_path,
                             source_root=source_root, dry_run=False, _locked=True)
    backup = None
    current = safe_fs.capture_state(path, include_payload=True)
    if missing and current.kind != "missing":
        raise ModelSettingsError(f"user config changed during update; preserving successor: {path}")
    if not missing and (current.kind != "file" or current.payload != raw or current.mode != mode):
        raise ModelSettingsError(f"user config changed during update; preserving successor: {path}")
    if not missing:
        backup = _backup(path, raw, mode)
    auth = safe_fs.authority(path, owner="model-settings:set", allowed_paths=(path,),
                             allow_leaf_symlink=False, expected=current)
    safe_fs.atomic_write_bytes(auth, candidate, mode, expected=current, create_parents=True)
    projection_lines = []
    if target["kind"] == "scalar" and adapter in {"claude", "codex"}:
        try:
            from native_context import project_context_window
            if adapter == "claude":
                import runtime_activation
                projected = project_context_window(
                    adapter, runtime=runtime_path, source_root=source_root,
                    claude_merge=lambda **kwargs: runtime_activation._merge_claude_settings(
                        Path(source_root or model_config.repository_root()), None,
                        "global", config_path=runtime_path / "settings.json", **kwargs),
                    context_only=True)
            else:
                projected = project_context_window(adapter, runtime=runtime_path,
                                                   source_root=source_root)
            projection_lines = [line for line in projected.get("report_lines", [])
                                if not line.startswith("context window:")]
            if projected.get("path"):
                projection_lines.append(f"native context: {projected['path']}")
        except Exception as exc:
            raise ModelSettingsError(
                f"models.conf was saved at {path}, but native context projection failed: {exc}") from exc
    # A non-cooperating writer can race the final precheck->rename window; do not roll back a successor.
    if target["kind"] == "scalar":
        lines = [f"model set: {adapter} context-window {old_context_window} -> {new_context_window} tokens",
                 f"config: {path}", f"backup: {backup or 'none (new user config)'}"]
        lines.extend(projection_lines)
    else:
        lines = [f"model set: {adapter} {target['name']} {old_model} -> {new_model}",
                 f"config: {path}", f"backup: {backup or 'none (new user config)'}"]
    if target.get("shared"):
        lines.insert(1, f"shared tier: {target['tier']} (all consumers of this tier follow the change)")
    return {"operation": "model-set", "status": "changed", "exit": 0,
            "runtime": adapter, "config_path": str(path), "target": target,
            "requested_model": requested, "old_model": old_model, "new_model": new_model,
            "old_budget": old_budget, "new_budget": new_budget, "changed_keys": sorted(changed),
            "backup": str(backup) if backup else None, "source_reason": reason,
            "lines": lines, **context_fields}


def get_context_window(adapter: str, *, runtime: str | Path | None = None,
                       environ: dict[str, str] | None = None,
                       source_root: str | Path | None = None) -> dict[str, object]:
    """Read the resolved common context request without touching user state."""
    if adapter not in model_config.ADAPTERS:
        raise ModelSettingsUsageError(f"unknown harness: {adapter!r}")
    values, receipt = model_config.resolve_config(
        adapter, runtime=runtime, environ=environ, source_root=source_root
    )
    value, provenance = model_config.resolve_context_window(values)
    native = {
        "codex": "Codex CLI and App Server model_context_window config",
        "claude": "Claude CLAUDE_CODE_AUTO_COMPACT_WINDOW environment",
        "opencode": "OpenCode selected-model limit.context, capped by installed native context/input limits",
    }[adapter]
    return {
        "operation": "model-context-window-get", "status": "resolved", "exit": 0,
        "runtime": adapter, "config_path": receipt.selected_path,
        "source": receipt.source, "source_reason": receipt.reason,
        "context_window": value, "provenance": provenance,
        "native_application": native, "measured_effective_capacity": False,
        "lines": [
            f"model get: {adapter} context-window={value} ({provenance}; source={receipt.source}/{receipt.reason})",
            f"config: {receipt.selected_path}", f"native application: {native}",
            "effective service capacity is not measured by this query",
        ],
    }


def _parse_bytes(raw: bytes) -> dict[str, str]:
    # Use the project's parser on a private in-memory temporary file without touching user paths.
    fd, name = tempfile.mkstemp(prefix="hearting-model-candidate-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        return model_config.parse_config(name, allow_symlink=False)
    finally:
        # destructive-ok: reason=discard the private candidate parser input; boundary=one mkstemp leaf created by this invocation
        Path(name).unlink(missing_ok=True)
