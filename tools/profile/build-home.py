#!/usr/bin/env python3
"""build-home.py — build a masked, per-dispatch config home from a
profiles/<name>.yaml declaration (spec/dispatch-profiles/prd.md §4.1).

The home is a symlink partial-projection of the single repo source — no
content fork. Core and guard files remain available for deterministic checks,
but the bootstrap input is only the runtime attach template plus the selected
specialization fragments. The dispatch prompt owns the canonical worker kernel
and exactly one declared worker type.

Usage:
  python3 tools/profile/build-home.py <name> --check
  python3 tools/profile/build-home.py <name> --instance <slug> [--home-root DIR]

Exit codes: 0 ok / 1 declaration or template error / 2 --check drift.
Never exit 3 — that code is dispatch-wrapper-owned (preflight gate).
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    # exit 1 (environment/declaration error class), NOT 2 — 2 is reserved for
    # --check drift per the exit-code contract above.
    sys.stderr.write("PyYAML required: pip install pyyaml\n")
    sys.exit(1)

VALID_HARNESSES = {"claude", "codex", "opencode"}
VALID_WORKER_TYPES = {"owner", "stage", "review", "support", "frame"}
BOOTSTRAP_FILENAME = {"claude": "CLAUDE.md", "codex": "AGENTS.md", "opencode": "AGENTS.md"}


def resolve_agent_home():
    """Env-first, else the canonical resolver, else marker-walk.

    Must work byte-identically from both the repo copy (tools/profile/) and
    the concrete adapter mirror (adapters/claude/tools/profile/) — this file
    is itself a symlink into the canonical tree, so `Path(__file__).resolve()`
    always lands at the same physical location regardless of invocation path,
    letting the canonical `utilities/dispatch_contract.py` be imported
    directly. Marker-walk remains the fallback for the (unsupported today)
    case where that import cannot succeed.
    """
    env_home = os.environ.get("AGENT_HOME")
    if env_home:
        candidate = Path(env_home)
        if (candidate / "core" / "CORE.md").is_file():
            return candidate.resolve()
    here = Path(__file__).resolve()
    utilities_dir = here.parents[1].parent / "utilities"
    if (utilities_dir / "dispatch_contract.py").is_file():
        sys.path.insert(0, str(utilities_dir))
        from dispatch_contract import resolve_agent_home as _resolve_agent_home

        resolved = _resolve_agent_home()
        if (resolved / "core" / "CORE.md").is_file():
            return resolved
    for candidate in here.parents:
        if (candidate / "core" / "CORE.md").is_file():
            return candidate
    sys.stderr.write(
        "build-home: could not resolve AGENT_HOME (no core/CORE.md marker found)\n"
    )
    sys.exit(1)


def resolve_dispatch_state_root(agent_home):
    """Canonical dispatch state root for `agent_home`, falling back to the
    legacy `<agent_home>/.dispatch` shape if the resolver cannot be imported."""
    here = Path(__file__).resolve()
    utilities_dir = here.parents[1].parent / "utilities"
    if (utilities_dir / "dispatch_contract.py").is_file():
        sys.path.insert(0, str(utilities_dir))
        from dispatch_contract import resolve_dispatch_state_root as _resolve

        return _resolve(agent_home)
    return agent_home / ".dispatch"


def first_existing(*paths):
    for p in paths:
        p = Path(p)
        if p.exists():
            return p
    return None


def claude_global_config_file(env, *, cwd=None):
    """Resolve Claude's global config file for account metadata projection.

    Claude Code 2.1.300 checks the legacy `.config.json` in its resolved
    config directory first, then uses `.claude.json` under a non-empty custom
    CLAUDE_CONFIG_DIR or the home directory. An explicitly empty config-dir
    value keeps the CLI's config directory empty, while its global config file
    still falls back to the home directory.
    """
    cwd = Path.cwd() if cwd is None else Path(cwd)
    home_dir = Path(env.get('HOME') or str(Path.home())).resolve(strict=False)
    raw_config_dir = env.get('CLAUDE_CONFIG_DIR')
    if raw_config_dir is None:
        config_dir = home_dir / '.claude'
        global_dir = home_dir
    elif raw_config_dir == '':
        config_dir = cwd
        global_dir = home_dir
    else:
        config_dir = Path(raw_config_dir)
        if not config_dir.is_absolute():
            config_dir = cwd / config_dir
        config_dir = config_dir.resolve(strict=False)
        global_dir = config_dir

    legacy_config = config_dir / '.config.json'
    current_config = global_dir / '.claude.json'
    try:
        metadata = legacy_config.stat()
        if stat.S_ISREG(metadata.st_mode) and (
                metadata.st_size > 0 or
                (metadata.st_mode & 0o222) == 0 and not current_config.exists()):
            return legacy_config
    except OSError:
        pass
    return current_config


def load_declaration(agent_home, name):
    decl_path = agent_home / "profiles" / f"{name}.yaml"
    if not decl_path.is_file():
        sys.stderr.write(f"build-home: profile declaration not found: {decl_path}\n")
        sys.exit(1)
    with decl_path.open("r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            sys.stderr.write(f"build-home: failed to parse {decl_path}: {e}\n")
            sys.exit(1)
    if not isinstance(data, dict):
        sys.stderr.write(f"build-home: {decl_path} did not parse to a mapping\n")
        sys.exit(1)
    return decl_path, data


def validate_declaration(agent_home, decl_path, data):
    """Validate required fields + model_role XOR model + fragments/expose
    schema. Any error -> stderr + exit 1 (declaration/template error class).
    Returns (harness, fragments, expose) on success.
    """
    errors = []

    for field in ("name", "description", "harness", "worker_type"):
        if not data.get(field):
            errors.append(f"missing required field: {field}")

    harness = data.get("harness")
    if harness is not None and harness not in VALID_HARNESSES:
        errors.append(
            f"invalid harness: {harness!r} (must be one of {sorted(VALID_HARNESSES)})"
        )

    worker_type = data.get("worker_type")
    if worker_type is not None and worker_type not in VALID_WORKER_TYPES:
        errors.append(
            f"invalid worker_type: {worker_type!r} (must be one of {sorted(VALID_WORKER_TYPES)})"
        )

    has_model_role = bool(data.get("model_role"))
    has_model = bool(data.get("model"))
    if has_model_role and has_model:
        errors.append("model_role and model are mutually exclusive — declare exactly one")
    elif not has_model_role and not has_model:
        errors.append("exactly one of model_role or model is required")

    fragments = data.get("fragments") or []
    if not isinstance(fragments, list):
        errors.append("fragments must be a list")
        fragments = []
    else:
        for frag in fragments:
            if not isinstance(frag, str):
                errors.append(f"fragments entries must be strings, got: {frag!r}")
                continue
            if not (agent_home / frag).is_file():
                errors.append(f"fragment not found: {agent_home / frag}")

    expose = data.get("expose") or {}
    if not isinstance(expose, dict):
        errors.append("expose must be a mapping")
        expose = {}
    else:
        unknown = set(expose.keys()) - {"skills", "agents", "triggers"}
        if unknown:
            errors.append(f"expose has unknown keys: {sorted(unknown)}")
        for key in ("skills", "agents", "triggers"):
            if key in expose and expose[key] is not None and not isinstance(expose[key], list):
                errors.append(f"expose.{key} must be a list")

    if errors:
        for e in errors:
            sys.stderr.write(f"build-home: {decl_path}: {e}\n")
        sys.exit(1)

    return harness, worker_type, fragments, expose


def assemble_bootstrap(agent_home, name, harness, worker_type, fragments):
    """Plain concat: header + bootstrap-<harness>.md template + each
    fragment file in declared order. No transformation. Missing template or
    fragment -> fail loud, exit 1 (this is where an unimplemented harness
    like `opencode` in v1 fails, since no bootstrap-opencode.md ships yet).
    """
    template_path = agent_home / "profiles" / "templates" / f"bootstrap-{harness}.md"
    if not template_path.is_file():
        sys.stderr.write(f"build-home: missing template {template_path}\n")
        sys.exit(1)

    pieces = [
        f"<!-- generated-from: profiles/{name}.yaml — do not edit; worker-type: {worker_type} -->"
    ]
    pieces.append(template_path.read_text(encoding="utf-8").rstrip("\n"))
    for frag in fragments:
        frag_path = agent_home / frag
        if not frag_path.is_file():
            sys.stderr.write(f"build-home: missing fragment {frag_path}\n")
            sys.exit(1)
        pieces.append(frag_path.read_text(encoding="utf-8").rstrip("\n"))

    return "\n".join(pieces) + "\n"


def publish_symlink(source, target):
    """Reuse a concurrent identical publication; replace stale links atomically."""
    source, target = Path(source).resolve(), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(source, target_is_directory=source.is_dir())
        return
    except FileExistsError:
        if not target.is_symlink():
            raise ValueError(f'worker home collision: {target}')
        if target.resolve() == source:
            return
    # No unlink window: a reader always sees a complete old or new projection.
    with tempfile.TemporaryDirectory(prefix='.worker-link-', dir=target.parent) as staging:
        temporary = Path(staging) / 'link'
        temporary.symlink_to(source, target_is_directory=source.is_dir())
        if not target.is_symlink():
            raise ValueError(f'worker home collision: {target}')
        os.replace(temporary, target)


def write_worker_text(path, text):
    """Publish complete generated bytes without truncating another builder's file."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.worker-file-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_profile_bootstrap(path, text):
    """Publish once; a matching regular bootstrap can be reused, never overwritten."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.worker-bootstrap-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink():
                # An older preparation may have left the attach-template link.
                os.replace(temporary, path)
            elif path.read_text(encoding='utf-8') != text:
                raise ValueError(f'worker home collision: {path}')
    finally:
        Path(temporary).unlink(missing_ok=True)


def link(target, linkpath):
    """Python port of install-runtime-projection.sh's link() primitive.

    Skip when target is missing. Refuse to clobber a real (non-symlink)
    file or directory. Otherwise unlink any prior symlink and relink.
    Returns True if a link was (re)created, False on skip/refuse.
    """
    target = Path(target)
    linkpath = Path(linkpath)
    if not target.exists():
        print(f"skip={linkpath} reason=projection-target-missing")
        return False
    if linkpath.exists() and not linkpath.is_symlink():
        print(f"skip={linkpath} reason=non-symlink-exists")
        return False
    publish_symlink(target, linkpath)
    print(f"link={linkpath}")
    return True


def build_instance(agent_home, name, harness, worker_type, fragments, expose, slug, home_root):
    # Fail fast on template/fragment problems before touching the filesystem.
    bootstrap_text = assemble_bootstrap(agent_home, name, harness, worker_type, fragments)

    instance_dir = home_root / f"{slug}.{name}"
    instance_dir.mkdir(parents=True, exist_ok=True)

    link_count = 0

    # L0 hard include (DP-2) — not surfaced in the declaration.
    if link(agent_home / "core", instance_dir / "core"):
        link_count += 1
    if link(agent_home / "hooks", instance_dir / "hooks"):
        link_count += 1

    if harness == "claude":
        settings_src = first_existing(
            agent_home / "settings.json",
            agent_home / "adapters" / "claude" / "settings.json",
        )
        if settings_src is not None:
            if link(settings_src, instance_dir / "settings.json"):
                link_count += 1
        else:
            print(f"skip={instance_dir / 'settings.json'} reason=projection-target-missing")
    elif harness == "codex":
        hooks_json_src = first_existing(
            agent_home / "codex-hooks" / "hooks.json",
            agent_home / "adapters" / "codex" / "hooks" / "hooks.json",
        )
        if hooks_json_src is not None:
            if link(hooks_json_src, instance_dir / "hooks.json"):
                link_count += 1
        else:
            print(f"skip={instance_dir / 'hooks.json'} reason=projection-target-missing")

    # expose subset
    for skill_name in expose.get("skills") or []:
        src = first_existing(
            agent_home / "skills" / skill_name,
            agent_home / "adapters" / "claude" / "skills" / skill_name,
        )
        if src is not None:
            if link(src, instance_dir / "skills" / skill_name):
                link_count += 1
        else:
            print(
                f"skip={instance_dir / 'skills' / skill_name} reason=projection-target-missing"
            )

    for agent_name in expose.get("agents") or []:
        src = first_existing(
            agent_home / "agents" / f"{agent_name}.md",
            agent_home / "adapters" / "claude" / "agents" / f"{agent_name}.md",
        )
        if src is not None:
            if link(src, instance_dir / "agents" / f"{agent_name}.md"):
                link_count += 1
        else:
            print(
                f"skip={instance_dir / 'agents' / (agent_name + '.md')} "
                "reason=projection-target-missing"
            )

    # triggers: v1 no-op regardless of content (empty list is the only
    # declared case today; session state (projects/, sessions/, .statusline/)
    # is deliberately never linked so it stays instance-isolated).

    # credentials shared, never duplicated/mutated. The source is the RUNTIME config
    # home, not the repo: in the split layout (AGENT_HOME=~/hearting, runtime
    # ~/.claude) `agent_home/.credentials.json` never exists, so every profiled child
    # spawned logged-out (jobs.log note=dead-auth 2026-07-19 r1b — depth-2 stage
    # dispatch silently degraded to inline). Same shape as the codex wrapper, which
    # links auth.json from CODEX_HOME with a ~/.codex fallback. CLAUDE_CONFIG_DIR is
    # consulted first so a profiled parent's own (linked) credential resolves through.
    if harness == "claude":
        env_home = os.environ.get("CLAUDE_CONFIG_DIR")
        creds_src = first_existing(
            *([Path(env_home) / ".credentials.json"] if env_home else []),
            Path.home() / ".claude" / ".credentials.json",
            agent_home / ".credentials.json",   # legacy AGENT_HOME=~/.claude layout
        )
    else:
        creds_src = first_existing(agent_home / ".credentials.json")
    if creds_src is not None:
        if link(creds_src, instance_dir / ".credentials.json"):
            link_count += 1

    bootstrap_filename = BOOTSTRAP_FILENAME[harness]
    write_worker_text(instance_dir / bootstrap_filename, bootstrap_text)

    return instance_dir, link_count


def read_worker_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError:
        import importlib.util
        parser_path = Path(__file__).resolve().parent / '_tomli/__init__.py'
        spec = importlib.util.spec_from_file_location('hearting_worker_tomli', parser_path)
        tomllib = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = tomllib
        spec.loader.exec_module(tomllib)
    return tomllib.loads(Path(path).read_text()) if Path(path).is_file() else {}


def worker_hook_state_override(home, origins):
    """Read existing user decisions for the same relocated hook definitions.

    Native Codex hashes the definition but keys its decisions by source path.
    Supply path aliases only for this invocation. Native hash comparison still
    rejects changed/unapproved definitions; disabled hooks stay disabled. Read
    again at each command build so a resumed worker sees current user decisions.
    """
    home = Path(home)
    state = read_worker_toml(home / 'config.toml').get('hooks', {}).get('state', {})
    aliases = {}
    for key, value in state.items():
        parts = key.rsplit(':', 3)
        if len(parts) != 4 or parts[0] not in origins:
            continue
        target = str(home / Path(parts[0]).name) + ':' + ':'.join(parts[1:])
        aliases[target] = value
    if not aliases:
        return None
    def scalar(value):
        return ('true' if value else 'false') if isinstance(value, bool) else json.dumps(value)
    # This schema contains only enabled and trusted_hash, never credentials.
    inline = '{' + ','.join(json.dumps(key) + '={' + ','.join(
        json.dumps(field) + '=' + scalar(value) for field, value in item.items()
        if field in ('enabled', 'trusted_hash')) + '}' for key, item in aliases.items()) + '}'
    return 'hooks.state=' + inline


def worker_home_key(agent_home, harness, worker_type, identity):
    """The per-attempt home directory name; ``agent_home`` is its resolved string."""
    return hashlib.sha256((str(agent_home) + '\0' + harness + '\0' + worker_type + '\0' + identity).encode()).hexdigest()[:32]


def _dispatch_defaults_module():
    code_root = Path(__file__).resolve().parents[2]
    path = code_root / 'utilities' / 'dispatch-defaults.py'
    name = '_hearting_worker_home_dispatch_defaults'
    module = sys.modules.get(name)
    if module is not None and Path(module.__file__).resolve() == path.resolve():
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError('dispatch defaults loader is unavailable')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _worker_account_selection(harness, env):
    """Read the existing validated dispatch-defaults config from this env."""
    defaults = _dispatch_defaults_module()
    config_path = defaults.default_config_path(environ=env)
    if not env.get('DISPATCH_DEFAULTS_CONFIG') and not os.path.lexists(config_path):
        # Account selection is optional. A minimal packaged root may omit the
        # shipped defaults file entirely; keep the pre-account worker-home
        # behavior in that case. Explicit paths and present configs still pass
        # through the validated loader below, so requested-input and content
        # errors remain visible.
        return None, None
    config, _warnings = defaults.load_validated(
        config_path, defaults.default_topology_path())
    account = defaults.query_worker_account(config, harness)
    configured = config.get('worker_account')
    pin_config = (str(Path(config_path).resolve(strict=False))
                  if isinstance(configured, dict) and configured else None)
    return account, pin_config


def _resolve_worker_account_path(value, *, env, cwd=None):
    """Resolve `~` from the passed HOME and relative paths from launch cwd."""
    cwd = Path.cwd() if cwd is None else Path(cwd)
    home = Path(env.get('HOME') or str(Path.home()))
    if value == '~':
        path = home
    elif value.startswith('~/'):
        path = home / value[2:]
    else:
        path = Path(value)
        if not path.is_absolute():
            path = cwd / path
    return path.resolve(strict=False)


def build_worker_home(agent_home, harness, worker_type, identity, *, env=None, destination=None, profile=None,
                      runtime_observation=None):
    """Default typed profile for every launch; never changes the caller's home.

    Runtime-owned credentials stay linked. User permissions and hooks retain
    their original bytes; only automatic main/catalog discovery is narrowed.
    The existing declaration profiles still add their selected specialization
    through build_instance; both paths use the same runtime attach templates.
    """
    from copy import deepcopy
    agent_home = Path(agent_home).resolve()
    env = dict(os.environ if env is None else env)
    if harness not in VALID_HARNESSES or worker_type not in VALID_WORKER_TYPES:
        raise ValueError('invalid worker home type')
    jobs = Path(env.get('AGENT_DISPATCH_JOBS') or resolve_dispatch_state_root(agent_home) / 'jobs.log')
    key = worker_home_key(agent_home, harness, worker_type, identity)
    home = Path(destination) if destination is not None else jobs.parent / 'homes/workers' / key
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    values = {'HEARTING_WORKER_HOME': str(home)}
    home_env = env.get('HOME') or str(Path.home())
    account_value, defaults_config = _worker_account_selection(harness, env)
    account_source = (
        _resolve_worker_account_path(account_value, env=env)
        if account_value is not None else None
    )
    if defaults_config is not None:
        # OpenCode's worker XDG_CONFIG_HOME is private. Carry the existing
        # config path so nested workers read the same account selection.
        values['DISPATCH_DEFAULTS_CONFIG'] = defaults_config
    if harness == 'codex':
        model_config_home = env.get('CODEX_HOME') or Path(home_env) / '.codex'
    elif harness == 'claude':
        model_config_home = env.get('CLAUDE_CONFIG_DIR') or Path(home_env) / '.claude'
    else:
        config_home = env.get('XDG_CONFIG_HOME') or Path(home_env) / '.config'
        model_config_home = env.get('OPENCODE_CONFIG_DIR') or Path(config_home) / 'opencode'
    values['HEARTING_MODEL_CONFIG_HOME'] = str(Path(model_config_home).expanduser().absolute())
    if harness == 'opencode':
        cache_home = env.get('XDG_CACHE_HOME') or Path(home_env) / '.cache'
        values['HEARTING_OPENCODE_MODELS_CACHE'] = str(
            (Path(cache_home).expanduser() / 'opencode').absolute())

    def symlink(source, target):
        if source is None:
            return
        source, target = Path(source), Path(target)
        if not source.exists():
            return
        publish_symlink(source, target)

    def read_json(path):
        path = Path(path)
        if not path.is_file():
            return {}
        text = path.read_text()
        if path.suffix == '.jsonc':
            text = re.sub(r'("(?:\\.|[^"\\])*")|/\*.*?\*/|//[^\n]*',
                          lambda m: m.group(1) or '', text, flags=re.S)
            text = re.sub(r'("(?:\\.|[^"\\])*")|,\s*([}\]])',
                          lambda m: m.group(1) or m.group(2), text)
        return json.loads(text)

    def write_json(path, value):
        write_worker_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    filename = BOOTSTRAP_FILENAME[harness]
    if profile:
        decl_path, data = load_declaration(agent_home, profile)
        declared_harness, declared_type, fragments, expose = validate_declaration(agent_home, decl_path, data)
        if (declared_harness, declared_type) != (harness, worker_type):
            raise ValueError('profile worker home type mismatch')
        # The declaration's body is specialization; its full skill catalog is
        # not needed because the dispatcher supplies the assigned contract.
        bootstrap = home / filename
        write_profile_bootstrap(bootstrap, assemble_bootstrap(agent_home, profile, harness, worker_type, fragments))
    else:
        symlink(agent_home / 'profiles/templates' / f'bootstrap-{harness}.md', home / filename)
    symlink(agent_home, home / 'hearting')
    if harness == 'codex':
        source = Path(env.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
        symlink(source / 'hooks', home / 'hooks')
        for name in ('auth.json', 'config.toml'):
            auth_source = account_source if account_source is not None else source
            target = (auth_source if name == 'auth.json' else source) / name
            if (name == 'auth.json' and account_source is not None
                    and not target.is_file()):
                old_link = home / name
                if old_link.is_symlink():
                    old_link.unlink()
            if (name == 'auth.json' and account_source is None
                    and not target.is_file()):
                target = Path.home() / '.codex/auth.json'
            symlink(target, home / name)
        hooks = first_existing(source / 'hooks.json', agent_home / 'adapters/codex/hooks/hooks.json')
        symlink(hooks, home / 'hooks.json')
        if (source / 'agent-config/models.conf').is_file():
            symlink(source / 'agent-config', home / 'agent-config')
        else:
            symlink(agent_home / 'adapters/codex/config/models.conf', home / 'agent-config/models.conf')
        # These are lookup pointers, not native auto-discovered skill/agent dirs.
        for name, target in {'agent-core': 'core', 'agent-capabilities': 'capabilities',
                             'agent-roles': 'roles', 'agent-bin': 'adapters/codex/bin',
                             'agent-hooks': 'adapters/codex/hooks', 'agent-tools': 'adapters/codex/tools',
                             'agent-utilities': 'adapters/codex/utilities', 'agent-scaffolds': 'adapters/codex/scaffolds',
                             'agent-skills': 'adapters/codex/skills', 'agent-agents': 'adapters/codex/agents',
                             'agent-modes': 'adapters/codex/modes', 'agent-plugin-marketplace': 'adapters/codex/plugin-marketplace',
                             'hearting-readme.md': 'adapters/codex/README.md'}.items():
            symlink(agent_home / target, home / name)
        config = read_worker_toml(source / 'config.toml')
        original_config_dir = (source / 'config.toml').resolve().parent
        origins = sorted({str(folder / name) for folder in (source, original_config_dir)
                          for name in ('hooks.json', 'config.toml')})
        values['HEARTING_CODEX_HOOK_SOURCES'] = json.dumps(origins)
        # Disabled plugin entries still sync their payloads unless the global feature is off.
        overrides = ['features.apps=false', 'features.plugins=false', 'features.multi_agent=false',
                     'features.default_mode_request_user_input=false', 'agents.enabled=false']
        def toml_value(value):
            if isinstance(value, bool):
                return 'true' if value else 'false'
            if isinstance(value, str):
                return json.dumps(value)
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, list):
                return '[' + ','.join(toml_value(item) for item in value) + ']'
            if isinstance(value, dict):
                return '{' + ','.join(json.dumps(key) + '=' + toml_value(item) for key, item in value.items()) + '}'
            raise ValueError('unsupported worker config value')
        # CLI dotted-key overrides do not parse quoted path components. Replace
        # the whole table so IDs containing dots/@ retain their exact identity.
        for field in ('mcp_servers', 'plugins'):
            narrowed = {name: {**value, 'enabled': False} for name, value in config.get(field, {}).items()}
            if field == 'mcp_servers':
                # Disabled servers need only a valid transport descriptor. Do
                # not copy auth headers/env secrets into command arguments.
                narrowed = {name: {**({'url': 'https://localhost.invalid'} if 'url' in value else {'command': 'true'}),
                                   'enabled': False} for name, value in config.get(field, {}).items()}
            overrides.append(field + '=' + toml_value(narrowed))
        # Native built-in skills are seeded in a fresh home. Disable their actual
        # paths using the documented per-path control, without a private flag.
        disabled = set()
        for base in (source / 'skills', home / 'skills', source / 'plugins/cache',
                     Path.home() / '.codex/plugins/cache', Path.home() / '.agents/skills'):
            if base.is_dir():
                for skill in base.rglob('SKILL.md'):
                    disabled.add(str(skill.parent))
                    disabled.add(str(skill))
                    if skill.is_relative_to(source / 'skills'):
                        disabled.add(str(home / 'skills' / skill.parent.relative_to(source / 'skills')))
                        disabled.add(str(home / 'skills' / skill.relative_to(source / 'skills')))
        overrides.append('skills.config=[' + ','.join('{path=' + json.dumps(path) + ',enabled=false}' for path in sorted(disabled)) + ']')
        values.update(CODEX_HOME=str(home), HEARTING_CODEX_WORKER_OVERRIDES=json.dumps(overrides))
    elif harness == 'claude':
        source = Path(env.get('CLAUDE_CONFIG_DIR') or Path(home_env) / '.claude')
        source = source.resolve(strict=False)
        # Capacity identity lives separately from credentials/settings. Keep
        # its account metadata without inheriting global MCP/onboarding state.
        # The account copy is optional: a config that cannot be read or parsed
        # leaves the launch unscoped instead of failing it.
        auth_env = env
        selected_settings_env = {}
        if account_source is not None:
            auth_env = dict(env)
            auth_env['CLAUDE_CONFIG_DIR'] = str(account_source)
            auth_env.pop('CLAUDE_SECURESTORAGE_CONFIG_DIR', None)
            account_settings = read_json(account_source / 'settings.json')
            selected_settings_env = (
                account_settings.get('env')
                if isinstance(account_settings, dict)
                and isinstance(account_settings.get('env'), dict) else {}
            )
            if selected_settings_env.get('CLAUDE_SECURESTORAGE_CONFIG_DIR') == '':
                selected_settings_env = dict(selected_settings_env)
                selected_settings_env.pop('CLAUDE_SECURESTORAGE_CONFIG_DIR', None)
        try:
            config = read_json(claude_global_config_file(auth_env, cwd=Path.cwd()))
        except (OSError, ValueError):
            config = {}
        account = config.get('oauthAccount') if isinstance(config, dict) else None
        if account:
            write_json(home / '.claude.json', {'oauthAccount': account})
        symlink(source / 'hooks', home / 'hooks')
        settings = read_json(source / 'settings.json')
        settings['enabledPlugins'] = {name: False for name in settings.get('enabledPlugins', {})}
        settings['autoMemoryEnabled'] = False
        settings.pop('extraKnownMarketplaces', None)
        settings.pop('statusLine', None)
        utilities_dir = agent_home / 'utilities'
        if str(utilities_dir) not in sys.path:
            sys.path.insert(0, str(utilities_dir))
        from claude_credential_recovery import resolve_storage_binding

        settings_env = settings.get('env') if isinstance(settings.get('env'), dict) else {}
        if account_source is not None:
            secure_env = dict(env)
            secure_env['CLAUDE_CONFIG_DIR'] = str(account_source)
            secure_env.pop('CLAUDE_SECURESTORAGE_CONFIG_DIR', None)
            secure_storage = resolve_storage_binding(
                secure_env, account_source, settings_env=selected_settings_env,
                cwd=Path.cwd())
        else:
            secure_storage = resolve_storage_binding(
                env, source, settings_env=settings_env, cwd=Path.cwd())
        if secure_storage is not None:
            values['CLAUDE_SECURESTORAGE_CONFIG_DIR'] = secure_storage
            if account_source is not None:
                if not isinstance(settings.get('env'), dict):
                    settings['env'] = {}
                settings['env']['CLAUDE_SECURESTORAGE_CONFIG_DIR'] = secure_storage
            # Settings env is applied over the inherited launch environment by
            # Claude. Resolve a relative user value once against this launch's
            # caller directory so later nested launches keep the same store.
            if 'CLAUDE_SECURESTORAGE_CONFIG_DIR' in settings_env:
                settings['env']['CLAUDE_SECURESTORAGE_CONFIG_DIR'] = secure_storage
            storage_dir = (Path(secure_storage) if secure_storage else
                           Path(home_env) / '.claude').resolve(strict=False)
            old_link = home / '.credentials.json'
            if (old_link.is_symlink() and
                    old_link.resolve(strict=False) ==
                    (storage_dir / '.credentials.json').resolve(strict=False)):
                # Remove only the old generated link to this exact native store.
                # Regular files and links to any other target remain candidates.
                old_link.unlink()
        write_json(home / 'settings.json', settings)
        # Project/local settings and managed policy still load normally. The
        # copied user settings retain all user guard hooks and permission rules.
        values['CLAUDE_CONFIG_DIR'] = str(home)
    else:
        # XDG isolation belongs to OpenCode, not the user's compute inventory.
        # Pin the existing inventory input before masking XDG_CONFIG_HOME, so
        # same- and cross-harness descendants keep reading the original file.
        config_root = Path(env.get('XDG_CONFIG_HOME') or Path(env.get('HOME') or Path.home()) / '.config')
        inventory = env.get('COMPUTE_HOSTS_CONFIG') or config_root / 'hearting/compute-hosts.yaml'
        values['COMPUTE_HOSTS_CONFIG'] = str(Path(inventory).expanduser().absolute())
        utilities = agent_home / 'utilities'
        if str(utilities) not in sys.path:
            sys.path.insert(0, str(utilities))
        import opencode_runtime
        if runtime_observation is None:
            probe_env = dict(os.environ)
            probe_env.update(env)
            runtime_observation = opencode_runtime.observe_launch(environ=probe_env)
        opencode_runtime.validate_observation_environment(runtime_observation, env)
        v2_runtime = runtime_observation.generation == 'v2'
        source = Path(env.get('OPENCODE_CONFIG_DIR') or Path(env.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'opencode')
        config = read_json(first_existing(source / 'opencode.jsonc', source / 'opencode.json') or source / 'opencode.json')
        config = deepcopy(config)
        # Worker evidence is Git + Hearting artifacts/checkpoints. Native
        # snapshots otherwise walk large NAS checkouts before the first turn.
        config['snapshot'] = False
        inline = json.loads(env.get('OPENCODE_CONFIG_CONTENT') or '{}')
        if not isinstance(inline, dict):
            raise ValueError('OPENCODE_CONFIG_CONTENT must contain a JSON object')
        inline['snapshot'] = False
        values['OPENCODE_CONFIG_CONTENT'] = json.dumps(inline)
        config['instructions'] = [path for path in config.get('instructions', [])
                                  if not (Path(path).name in ('AGENTS.md', 'CLAUDE.md') and
                                          Path(path).is_file() and 'Adapter Bootstrap' in Path(path).read_text())]
        if v2_runtime:
            config['skills'] = []
            config['mcp'] = {'servers': {}}
            config['plugins'] = []
            config.pop('plugin', None)
            permissions = config.get('permissions', [])
            if not isinstance(permissions, list):
                permissions = []
            permissions.append({'action': 'skill', 'resource': '*', 'effect': 'deny'})
            config['permissions'] = permissions
            config.pop('permission', None)
            config['plugins'] = []
            config.pop('plugin', None)
        else:
            config['skills'] = {'paths': []}
            config['mcp'] = {name: {**value, 'enabled': False} for name, value in config.get('mcp', {}).items()}
            permission = config.get('permission', {})
            if isinstance(permission, str):
                permission = {'*': permission}
            permission['skill'] = {'*': 'deny'}
            config['permission'] = permission
            for name in ('plugins', 'plugin'):
                symlink(source / name, home / name)
        write_json(home / 'opencode.json', config)
        runtime = home / 'runtime'
        for kind in ('data', 'cache', 'state', 'config'):
            target = runtime / kind
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not v2_runtime:
                values['XDG_' + kind.upper() + '_HOME'] = str(target)
        if not v2_runtime:
            data = (account_source if account_source is not None else
                    Path(env.get('XDG_DATA_HOME') or Path.home() / '.local/share'))
            auth_source = data / 'opencode/auth.json'
            if account_source is not None and not auth_source.is_file():
                old_link = runtime / 'data/opencode/auth.json'
                if old_link.is_symlink():
                    old_link.unlink()
            symlink(auth_source, runtime / 'data/opencode/auth.json')
            symlink(home, runtime / 'config/opencode')
            values['OPENCODE_CONFIG_DIR'] = str(home)
        values.update(OPENCODE_DISABLE_CLAUDE_CODE_PROMPT='1',
                      OPENCODE_DISABLE_CLAUDE_CODE_SKILLS='1')
        if v2_runtime:
            # The OpenCode client discovers the existing shared service through
            # XDG state/config; it never receives a database path or legacy auth
            # file to connect directly from this nested worker.
            values.update(OPENCODE_DB=None, OPENCODE_AUTH=None, OPENCODE_DATA_DIR=None)
    # schema 2 adds the identity retention needs to bind a home to its attempt.
    metadata = {'harness': harness, 'worker_type': worker_type,
                'source': str(agent_home), 'profile': profile,
                'schema': 2, 'identity': identity, 'key': key,
                'destination': destination is not None}
    if harness == 'claude':
        metadata['claude_secure_storage_dir'] = secure_storage
    write_json(home / 'worker-home.json', metadata)
    return values


def do_check(agent_home, name):
    decl_path, data = load_declaration(agent_home, name)
    harness, worker_type, fragments, expose = validate_declaration(agent_home, decl_path, data)

    # Template + fragment existence, and fail-loud on an unimplemented
    # harness template (e.g. opencode in v1), happen inside assemble.
    first = assemble_bootstrap(agent_home, name, harness, worker_type, fragments)

    # v1 ships no persisted instance to diff against (homes are ephemeral
    # and not created by --check), so drift is confirmed by reassembling
    # the same inputs a second time and requiring byte-identical output.
    second = assemble_bootstrap(agent_home, name, harness, worker_type, fragments)
    if first != second:
        sys.stderr.write(
            f"build-home: --check drift — bootstrap reassembly is not deterministic for {name}\n"
        )
        sys.exit(2)

    print(f"check=ok name={name} harness={harness} worker_type={worker_type} fragments={len(fragments)}")
    sys.exit(0)


def do_instance(agent_home, name, slug, home_root):
    decl_path, data = load_declaration(agent_home, name)
    harness, worker_type, fragments, expose = validate_declaration(agent_home, decl_path, data)
    instance_dir, link_count = build_instance(
        agent_home, name, harness, worker_type, fragments, expose, slug, home_root
    )
    print(f"instance={instance_dir} harness={harness} links={link_count}")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(
        description="Build a masked per-dispatch config home from profiles/<name>.yaml"
    )
    parser.add_argument("name", help="profile name (profiles/<name>.yaml)")
    parser.add_argument("--instance", metavar="SLUG", help="build a per-dispatch instance home")
    parser.add_argument(
        "--check", action="store_true", help="validate declaration/template/fragments only, no writes"
    )
    parser.add_argument(
        "--home-root",
        metavar="DIR",
        default=None,
        help="override the instance home root (default: $AGENT_HOME/.dispatch/homes/)",
    )
    args = parser.parse_args()

    agent_home = resolve_agent_home()
    # Shared-home-root contract: the profile home lands under <AGENT_HOME>/.dispatch/homes/,
    # and the readers (fleet _proj_home(), utilities/dispatch-liveness.sh via agent-home.sh)
    # must resolve the SAME root to find the isolated transcript. Profile dispatch therefore
    # presumes a consistent AGENT_HOME across writer (wrapper) and readers; with AGENT_HOME
    # unset the reader fallbacks diverge and profile jobs read as false-DEAD (see plan Risks).
    home_root = Path(args.home_root) if args.home_root else resolve_dispatch_state_root(agent_home) / "homes"

    if args.check:
        do_check(agent_home, args.name)
        return

    if args.instance:
        do_instance(agent_home, args.name, args.instance, home_root)
        return

    # No action requested — validate only, same declaration/template checks
    # as --check, without the determinism re-check ceremony.
    decl_path, data = load_declaration(agent_home, args.name)
    harness, worker_type, fragments, _expose = validate_declaration(agent_home, decl_path, data)
    assemble_bootstrap(agent_home, args.name, harness, worker_type, fragments)
    print(f"declaration=ok name={args.name} harness={harness} worker_type={worker_type}")
    sys.exit(0)


if __name__ == "__main__":
    main()
