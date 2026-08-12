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
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    # exit 1 (environment/declaration error class), NOT 2 — 2 is reserved for
    # --check drift per the exit-code contract above.
    sys.stderr.write("PyYAML required: pip install pyyaml\n")
    sys.exit(1)

VALID_HARNESSES = {"claude", "codex", "opencode"}
VALID_WORKER_TYPES = {"owner", "stage", "review", "support"}
BOOTSTRAP_FILENAME = {"claude": "CLAUDE.md", "codex": "AGENTS.md"}


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


def first_existing(*paths):
    for p in paths:
        p = Path(p)
        if p.exists():
            return p
    return None


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
    if linkpath.is_symlink():
        linkpath.unlink()
    linkpath.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, linkpath)
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
    (instance_dir / bootstrap_filename).write_text(bootstrap_text, encoding="utf-8")

    return instance_dir, link_count


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
    home_root = Path(args.home_root) if args.home_root else agent_home / ".dispatch" / "homes"

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
