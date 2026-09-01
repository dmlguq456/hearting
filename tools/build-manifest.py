#!/usr/bin/env python3
"""Build compatibility metadata and catalogs from the canonical manifest.

`harness-manifest.json` is the versioned portable metadata source of truth.
`manifest.json`, catalog tables, and per-capability Contract blocks are
generated compatibility views.

Additional adapter/runtime inputs:
  - loops/README.md     "Active Loops" table + LOOP_LAYER constant
  - adapters/claude/settings.json  Claude adapter hook registration (read-only)
  - TRACKS              documented constant (below), validated against discovered skills

manifest.json is a PURE derivation — never hand-edit it. Edit the definitions, then
re-run this script directly. The build is byte-identical on
re-run (idempotent): GENERATED_FROM is a fixed string (NO date/timestamp), every list is
sorted by a stable key, json.dumps uses ensure_ascii=False + indent=2 + sort_keys=False
(field order preserved) + a trailing newline.

Custom skill/agent fields live nested under the reserved `metadata:` frontmatter key
(CC's recommended nest for custom fields — Desktop/strict-validator compatible). This
script FLATTENS metadata.* up to top-level manifest fields, so the manifest stays flat
and matches the consumer's setting_* columns 1:1.

Consumer mapping note: the consumer's `setting_hooks.kind` column == this manifest's
`hooks[].event` field (naming difference only). body_md / updated_at columns are derived
by the consumer from its own manual_docs body, NOT from this manifest (so they are omitted
here, by design).

Usage:
  python3 tools/build-manifest.py            # write manifest.json at repo root
  python3 tools/build-manifest.py --check    # build in memory, diff vs existing, exit 1 on drift
"""
import sys
import os
import re
import json
import glob
import shlex
import hashlib
import argparse

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML required: pip install pyyaml\n")
    sys.exit(2)

import harness_manifest
import capability_topology

# realpath (not abspath): this script is executed via the collapsed adapter symlink
# (adapters/claude/tools/build-manifest.py -> ../../../tools/build-manifest.py). abspath
# keeps the symlink path, resolving REPO_ROOT to repo/adapters/claude (double-path bug);
# realpath follows the link to the canonical tools/, giving the true repo root either way.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))  # tools/ -> repo root
MANIFEST_PATH = os.path.join(REPO_ROOT, "manifest.json")

# harness-layer-sync HLS-3 (hash manifest) / HLS-7 (surface derivation). The exemption ledger
# is the single declaration file for adapter shared-layer exceptions (like GSD's
# gsd-file-manifest.json + FIELD_CLASSIFICATION: one row = one decision). delta rows bind
# a canonical raw-byte sha256 baseline; the guard reds when live canonical drifts from it.
EXEMPTIONS_PATH = os.path.join(REPO_ROOT, "tools", "adaptation-exemptions.tsv")
SHARED_LAYERS = ("hooks", "utilities", "tools")

# Fixed provenance string — NO date/timestamp (idempotency invariant).
GENERATED_FROM = (
    "harness-manifest.json + adapter-owned hook/loop/track inputs "
    "(adapters/claude/settings.json, loops/README.md, tools/build-manifest.py)"
)

# hard_block allowlist = PreToolUse guards only (consumer attempt1 §2.4 / Claude adapter hook settings
# PreToolUse). herdr-agent-state is also registered on PreToolUse but is a state-marker,
# NOT a guard -> hard_block stays false for it.
GUARDS = {"artifact-guard", "git-state-guard", "spec-skill-gate", "core-first-guard", "builtin-memory-guard"}

# Loop-to-layer constant, based on loops/README.md membership and consumer §2.5.
# Study remains L4 per the OPS-view divergence recorded in consumer §23.12 V1. The layer table
# itself is left semantically unchanged — this map is the machine-readable layer source.
LOOP_LAYER = {"oncall": "L3", "note": "L3", "drill": "L4", "study": "L4", "runtime-watch": "L4"}

# 4-track skeleton — documented constant (source: README.md §4 / WORKFLOW.md §1 4-track
# chains; gate positions = artifact-guard rule, not parseable from prose, per consumer
# §2.6). `steps` reference real skill slugs and are VALIDATED against discovered skills in
# build_tracks() (typo in a hand-typed slug => hard ERROR, since tracks is the one curated
# constant in an otherwise pure transcription).
TRACKS = [
    {"id": "research-lab", "label": "Research & experiments", "color_token": "--cat-1",
     "steps": ["skill__analyze-project", "skill__autopilot-research", "skill__autopilot-spec",
               "skill__autopilot-code", "skill__autopilot-lab"],
     "gates": ["artifact-guard:after-research", "artifact-guard:after-spec"]},
    {"id": "library", "label": "Libraries & CLI", "color_token": "--cat-5",
     "steps": ["skill__analyze-project", "skill__autopilot-spec", "skill__autopilot-code"],
     "gates": ["artifact-guard:after-analyze", "artifact-guard:after-spec"]},
    {"id": "document", "label": "Documents", "color_token": "--cat-2",
     "steps": ["skill__analyze-project", "skill__autopilot-research", "skill__autopilot-draft",
               "skill__autopilot-refine", "skill__autopilot-apply"],
     "gates": ["artifact-guard:after-research"]},
    {"id": "app", "label": "Apps", "color_token": "--cat-3",
     "steps": ["skill__autopilot-spec", "skill__autopilot-design", "skill__autopilot-code",
               "skill__autopilot-ship"],
     "gates": ["artifact-guard:after-spec"]},
]


# ---------------------------------------------------------------------------
# frontmatter parsing (tolerant — mirrors the CC loader / consumer parseFrontmatter,
# which are line-regex tolerant; survives a future unquoted-colon description without
# crashing the build)
# ---------------------------------------------------------------------------
def _split_frontmatter(path):
    raw = open(path, encoding="utf-8").read()
    if not raw.startswith("---"):
        return ""
    parts = raw.split("---", 2)   # ['', frontmatter, body]
    return parts[1] if len(parts) >= 2 else ""


def _scalar(fm_text, key):
    m = re.search(r'(?m)^%s:[ \t]*(.+?)[ \t]*$' % re.escape(key), fm_text)
    if not m:
        return ""
    v = m.group(1).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        v = v[1:-1]
    return v


def _metadata_block(fm_text):
    """Extract the `metadata:` mapping by yaml-parsing just that indented block.
    Tolerates blank lines inside the block (stops at the next column-0 key)."""
    m = re.search(r'(?ms)^metadata:[ \t]*\n((?:(?:[ \t]+\S.*|[ \t]*)\n)*)', fm_text)
    if not m:
        return {}
    block = "metadata:\n" + m.group(1)
    try:
        d = yaml.safe_load(block)
        md = d.get("metadata") if isinstance(d, dict) else None
        return md if isinstance(md, dict) else {}
    except Exception:
        return {}


def parse_frontmatter(path):
    """Return a dict with name/argument-hint/model + metadata(dict).
    Strict yaml first; on any failure fall back to line-regex + isolated metadata parse."""
    fm_text = _split_frontmatter(path)
    fm = None
    try:
        loaded = yaml.safe_load(fm_text)
        if isinstance(loaded, dict):
            fm = loaded
    except Exception:
        fm = None
    if fm is None:
        sys.stderr.write("WARN: %s — strict YAML frontmatter parse failed; using tolerant "
                         "fallback (likely an unquoted ':' in a value)\n" % path)
        fm = {
            "name": _scalar(fm_text, "name"),
            "argument-hint": _scalar(fm_text, "argument-hint"),
            "model": _scalar(fm_text, "model"),
            "metadata": _metadata_block(fm_text),
        }
    md = fm.get("metadata")
    if not isinstance(md, dict):
        fm["metadata"] = {}
    return fm


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def build_skills(canonical):
    rows = []
    for identifier, spec in canonical["capabilities"].items():
        rows.append({
            "slug": "skill__%s" % identifier,
            "name": identifier,
            "group": spec["group"],
            "fam": spec["family"],
            "modes": spec["modes"],
            "blurb": spec["summary"],
            "invocation_class": spec["invocation"]["class"],
            "use_when": spec["invocation"]["use_when"],
            "not_for": spec["invocation"]["not_for"],
            "argument_hint": spec["argument_shape"],
        })
    return sorted(rows, key=lambda r: r["slug"])


def build_agents(canonical):
    """Native agent rows are kernel-only: team agents retired into the unit catalog
    (roles/units/**), which is dispatched, never instantiated as a native agent."""
    rows = []
    for agent in canonical["kernel"]["agents"]:
        rows.append({
            "slug": "agent__%s" % agent,
            "name": agent,
            "model": "",
            "modes": [],
            "blurb": "Kernel agent projected into every activation.",
        })
    return sorted(rows, key=lambda r: r["slug"])


def _parse_hook_command(cmd):
    """Return (mono, name, basename) or None. Strips env prefix + interpreter + path,
    retains the meaningful trailing arg (state for herdr, subcommand for mem.py) in name."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    i = 0
    while i < len(toks) and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', toks[i]):
        i += 1
    toks = toks[i:]
    if toks and toks[0] in ("bash", "sh", "zsh", "python3", "python", "node"):
        toks = toks[1:]
    if not toks:
        return None
    base = os.path.basename(toks[0])
    rest = toks[1:]
    if base.endswith(".sh"):
        mono = base[:-3]
    elif base.endswith(".py"):
        mono = base
    else:
        mono = base
    args = [a for a in rest if not a.startswith("-")]
    if base.endswith(".py") and args:
        name = "%s %s" % (mono, args[0])
    elif mono == "herdr-agent-state" and args:
        name = "%s %s" % (mono, args[0])
    else:
        name = mono
    return mono, name, base


def build_hooks():
    cfg = json.load(open(os.path.join(REPO_ROOT, "adapters", "claude", "settings.json"), encoding="utf-8"))
    rows = []
    seen = {}     # slug -> name; detects (mono,event) collisions that differ only by arg
    for event, entries in cfg.get("hooks", {}).items():
        for entry in entries:
            for h in entry.get("hooks", []):
                if h.get("type") != "command":
                    continue
                parsed = _parse_hook_command(h.get("command", ""))
                if not parsed:
                    continue
                mono, name, base = parsed
                if base.endswith(".test.sh"):     # exclude test harnesses (consumer §2.4)
                    continue
                slug = "hook__%s__%s" % (mono, event)
                if slug in seen:
                    if seen[slug] == name:
                        continue                  # true duplicate (identical registration)
                    # same (mono,event), different arg (e.g. herdr idle vs working on one
                    # event): make the loss VISIBLE and keep both rather than silently drop.
                    sys.stderr.write("WARN: hook slug collision %s (%r vs %r) — "
                                     "disambiguating with numeric suffix\n"
                                     % (slug, seen[slug], name))
                    n = 2
                    while ("%s__%d" % (slug, n)) in seen:
                        n += 1
                    slug = "%s__%d" % (slug, n)
                seen[slug] = name
                rows.append({
                    "slug": slug,
                    "name": name,
                    "mono": mono,
                    "event": event,
                    "hard_block": (event == "PreToolUse" and mono in GUARDS),
                })
    return sorted(rows, key=lambda r: (r["event"], r["mono"], r["slug"]))


def _split_row(line):
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _is_separator(line):
    return bool(re.match(r'^\s*\|?\s*:?-{2,}', line)) and set(line.strip()) <= set("|-: ")


_ACTIVE_LOOPS_HEADER = ["Loop", "Type", "Trigger", "Scope", "Work", "Output", "User touchpoint"]
_LOOP_CELL = re.compile(r'\*\*(.+?)\*\*\s*\(`([^`]+)`\)')


def build_loops():
    lines = open(os.path.join(REPO_ROOT, "loops", "README.md"), encoding="utf-8").read().split("\n")
    # Locate the active table by its exact seven-column header.
    start = None
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("|") and _split_row(line) == _ACTIVE_LOOPS_HEADER:
            start = idx
            break
    if start is None:
        return []
    rows = []
    for line in lines[start + 1:]:
        if not line.lstrip().startswith("|"):
            break                                   # table ended
        if _is_separator(line):
            continue                                # skip |---|---| row
        cells = _split_row(line)
        if len(cells) != len(_ACTIVE_LOOPS_HEADER):
            sys.stderr.write("WARN: active loop row has %d cols (expected %d), skipped: %s\n"
                             % (len(cells), len(_ACTIVE_LOOPS_HEADER), line.strip()[:60]))
            continue
        rec = dict(zip(_ACTIVE_LOOPS_HEADER, cells))
        m = _LOOP_CELL.search(rec["Loop"])
        if not m:
            continue
        display_name = m.group(1).strip()
        mono = m.group(2).strip().rstrip("/")        # drill cell is `drill/` -> drill
        rows.append({
            "slug": "loop__%s" % mono,
            "name": display_name,
            "mono": mono,
            "type": "time" if "Time" in rec["Type"] else "event",
            "schedule": rec["Trigger"],                # Verbatim Markdown; the consumer renders it.
            "blurb": rec["Work"],
            "output_path": rec["Output"],
            "layer": LOOP_LAYER.get(mono, ""),
        })
    return sorted(rows, key=lambda r: r["slug"])


def build_tracks(skill_slugs):
    for t in TRACKS:
        for s in t["steps"]:
            if s not in skill_slugs:
                sys.stderr.write("ERROR: track '%s' references unknown skill slug '%s'\n" % (t["id"], s))
                sys.exit(1)
    return [dict(t) for t in TRACKS]


# ---------------------------------------------------------------------------
# harness-layer-sync HLS-3 / HLS-7 — adaptation shared-layer surface + delta hash-manifest
#
# GSD-grounded (bin/install.js fileHash L7718 / saveLocalPatches L8057): baseline binding
# is a raw-byte sha256 of the CANONICAL file, and drift = live-hash != recorded-baseline.
# We keep only that half of GSD's model (the guard reds on drift → human re-derives the
# delta), dropping gsd-local-patches/reapply 3-way merge: our delta set is 2 internally
# owned files, not consumed upstream, so automatic patch replay is overkill.
# ---------------------------------------------------------------------------
def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _read_exemptions():
    """Parse tools/adaptation-exemptions.tsv -> list of dicts. Comment/blank lines skipped.
    Fields: adapter_path, class(wrapper|delta), rationale, delta_baseline."""
    rows = []
    if not os.path.exists(EXEMPTIONS_PATH):
        return rows
    with open(EXEMPTIONS_PATH, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cells = line.rstrip("\n").split("\t")
            if len(cells) < 2:
                continue
            rows.append({
                "adapter_path": cells[0],
                "class": cells[1],
                "rationale": cells[2] if len(cells) > 2 else "",
                "delta_baseline": cells[3] if len(cells) > 3 else "-",
            })
    return rows


def _canonical_for(adapter_path):
    """adapters/claude/<layer>/<name> -> <layer>/<name> (canonical top-level path)."""
    m = re.match(r"^adapters/claude/(hooks|utilities|tools)/(.+)$", adapter_path)
    if not m:
        return None
    return "%s/%s" % (m.group(1), m.group(2))


def delta_baselines():
    """For each delta exemption, the canonical path + its live raw-byte sha256.
    Used by --sync-baselines (write) and --check (verify)."""
    out = []
    for row in _read_exemptions():
        if row["class"] != "delta":
            continue
        canonical = _canonical_for(row["adapter_path"])
        if not canonical:
            continue
        canonical_abs = os.path.join(REPO_ROOT, canonical)
        live = _sha256(canonical_abs) if os.path.exists(canonical_abs) else None
        out.append({
            "adapter_path": row["adapter_path"],
            "canonical": canonical,
            "recorded": row["delta_baseline"],
            "live": live,
        })
    return out


def sync_baselines():
    """Rewrite the delta_baseline (4th) column of each delta row with the live canonical
    sha256. Idempotent: byte-identical output when nothing changed. Returns changed count."""
    if not os.path.exists(EXEMPTIONS_PATH):
        sys.stderr.write("no exemptions file at %s\n" % EXEMPTIONS_PATH)
        return 0
    live_by_path = {b["adapter_path"]: b["live"] for b in delta_baselines()}
    changed = 0
    lines = open(EXEMPTIONS_PATH, encoding="utf-8").read().split("\n")
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = line.split("\t")
        if len(cells) < 2 or cells[1] != "delta":
            continue
        want = live_by_path.get(cells[0])
        if not want:
            continue
        while len(cells) < 4:
            cells.append("-")
        if cells[3] != want:
            cells[3] = want
            lines[i] = "\t".join(cells)
            changed += 1
    open(EXEMPTIONS_PATH, "w", encoding="utf-8").write("\n".join(lines))
    return changed


def check_baselines():
    """Return (ok, messages). ok=False if any delta baseline is unset/invalid/drifted."""
    ok = True
    msgs = []
    for b in delta_baselines():
        rec = b["recorded"]
        if b["live"] is None:
            ok = False
            msgs.append("delta baseline: canonical %s is missing" % b["canonical"])
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", rec or ""):
            ok = False
            msgs.append("delta baseline unset/invalid for %s (expected sha256 of %s = %s; run --sync-baselines)"
                        % (b["adapter_path"], b["canonical"], b["live"]))
            continue
        if rec != b["live"]:
            ok = False
            msgs.append("delta baseline DRIFT for %s: canonical %s now %s but ledger records %s "
                        "(canonical changed — re-derive the delta patch, then --sync-baselines)"
                        % (b["adapter_path"], b["canonical"], b["live"], rec))
    return ok, msgs


def _layer_files(layer):
    """Top-level (non-directory) canonical files under <layer>/, sorted. __pycache__ excluded."""
    d = os.path.join(REPO_ROOT, layer)
    if not os.path.isdir(d):
        return []
    return sorted(
        f for f in os.listdir(d)
        if os.path.isfile(os.path.join(d, f)) and not f.endswith((".pyc", ".pyo"))
    )


def adaptation_surface(kind):
    """Filesystem-derived surface sets (HLS-7 — replaces hardcoded enumerations).
      codex-hooks     : adapters/codex/hooks/*.py native bridge basenames
      shared-canonical: '<layer>/<name>\t<class>' for every top-level shared file
    """
    if kind == "codex-hooks":
        d = os.path.join(REPO_ROOT, "adapters", "codex", "hooks")
        if not os.path.isdir(d):
            return []
        return sorted(f for f in os.listdir(d) if f.endswith(".py"))
    if kind == "shared-canonical":
        cls_by_path = {r["adapter_path"]: r["class"] for r in _read_exemptions()}
        rows = []
        for layer in SHARED_LAYERS:
            for name in _layer_files(layer):
                adapter_path = "adapters/claude/%s/%s" % (layer, name)
                rows.append("%s/%s\t%s" % (layer, name, cls_by_path.get(adapter_path, "collapsed")))
        return rows
    sys.stderr.write("unknown adaptation-surface kind: %s\n" % kind)
    sys.exit(2)


def _md_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def _replace_section(text, heading, body):
    pattern = re.compile(r"(?ms)^%s\n.*?(?=^## |\Z)" % re.escape(heading))
    if not pattern.search(text):
        raise ValueError("missing generated section %s" % heading)
    return pattern.sub(body.rstrip() + "\n\n", text, count=1)


def _capability_contract(identifier, spec):
    modes = ", ".join(spec["modes"]) or "none"
    topology = ""
    entry_layer = ""
    if spec["group"] == "entry":
        summary = capability_topology.capability_summary(capability_topology.load_registry(), identifier)
        topology = "\n| Execution topology | `%s`; registry `%s` |" % (
            ", ".join(summary["topology_classes"]), "capabilities/topologies.json")
    if spec["invocation"]["class"] == "entry-router":
        entry_layer = "\n| Entry load phase | `post-approval`; owner contract `capabilities/%s.md` |" % identifier
    return """## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `%s` |
| Group | `%s` |
| Supported modes | `%s` |
| Portable meaning | %s |
| Argument shape | `%s` |%s""" % (
        identifier,
        _md_cell(spec["group"]),
        _md_cell(modes),
        _md_cell(spec["summary"]),
        _md_cell(spec["argument_shape"]),
        topology + entry_layer,
    )


def _capability_catalog(canonical):
    lines = [
        "## Catalog",
        "<!-- GENERATED: harness-manifest.json -->",
        "",
        "| Capability | Group | Modes | Portable spec | Portable meaning | Claude realization | Codex realization | OpenCode realization |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for identifier, spec in canonical["capabilities"].items():
        modes = ", ".join(spec["modes"]) or "-"
        lines.append(
            "| `%s` | %s | %s | [`%s.md`](%s.md) | %s | "
            "`adapters/claude/skills/%s/SKILL.md` | "
            "`adapters/codex/skills/%s/SKILL.md` | "
            "`adapters/opencode/skills/%s/SKILL.md`; "
            "`adapters/opencode/commands/%s.md` |"
            % (
                identifier,
                _md_cell(spec["group"]),
                _md_cell(modes),
                identifier,
                identifier,
                _md_cell(spec["summary"]),
                identifier,
                identifier,
                identifier,
                identifier,
            )
        )
    return "\n".join(lines)


def _role_catalog(canonical):
    """Render the unit catalog (the former team-role rows retired into roles/units/**)."""
    lines = [
        "## Role Catalog",
        "<!-- GENERATED: harness-manifest.json -->",
        "",
        "| Family | Unit | Portable model role | Worker type | Floor | Catalog source |",
        "|---|---|---|---|---|---|",
    ]
    for unit, spec in canonical["units"].items():
        lines.append(
            "| `%s` | `%s` | `%s` | `%s` | `%s` | `roles/units/%s.md` |"
            % (
                _md_cell(spec["family"]),
                unit,
                _md_cell(spec["role"]),
                _md_cell(spec["worker_type"]),
                _md_cell(spec["floor"]),
                unit,
            )
        )
    return "\n".join(lines)


def generated_document_outputs(canonical):
    outputs = {}
    for identifier, spec in canonical["capabilities"].items():
        path = os.path.join(REPO_ROOT, "capabilities", "%s.md" % identifier)
        source = open(path, encoding="utf-8").read()
        outputs[path] = _replace_section(
            source, "## Contract", _capability_contract(identifier, spec)
        )

    capability_readme = os.path.join(REPO_ROOT, "capabilities", "README.md")
    outputs[capability_readme] = _replace_section(
        open(capability_readme, encoding="utf-8").read(),
        "## Catalog",
        _capability_catalog(canonical),
    )
    role_readme = os.path.join(REPO_ROOT, "roles", "README.md")
    outputs[role_readme] = _replace_section(
        open(role_readme, encoding="utf-8").read(),
        "## Role Catalog",
        _role_catalog(canonical),
    )
    return outputs


def build_manifest(canonical):
    skills = build_skills(canonical)
    skill_slugs = {r["slug"] for r in skills}
    topology_registry = capability_topology.load_registry()
    topology_validation = capability_topology.validate_registry(topology_registry, canonical)
    return {
        "generated_from": GENERATED_FROM,
        "canonical_manifest": "harness-manifest.json",
        "topology_registry": {
            "source": "capabilities/topologies.json",
            "digest": topology_validation["registry_digest"],
            "capabilities": topology_validation["capabilities"],
            "recipes": topology_validation["recipes"],
        },
        "manifest_version": canonical["product"]["manifest_version"],
        "skills": skills,
        "agents": build_agents(canonical),
        "units": canonical["units"],
        "modes": canonical["modes"],
        "ownership": canonical["ownership"],
        "hooks": build_hooks(),
        "loops": build_loops(),
        "tracks": build_tracks(skill_slugs),
    }


def render(manifest):
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    parser.add_argument("--sync-baselines", action="store_true")
    parser.add_argument("--adaptation-surface")
    args = parser.parse_args(argv)
    # HLS-7: filesystem-derived surface sets consumed by check-adaptation-boundary.sh.
    if args.adaptation_surface is not None:
        for line in adaptation_surface(args.adaptation_surface):
            print(line)
        return 0
    # HLS-3: regenerate delta baselines (canonical raw-byte sha256) into the exemption ledger.
    if args.sync_baselines:
        n = sync_baselines()
        print("synced %d delta baseline(s) in %s" % (n, os.path.relpath(EXEMPTIONS_PATH, REPO_ROOT)))
        return 0

    check = args.check
    canonical = harness_manifest.load()
    text = render(build_manifest(canonical))
    document_outputs = generated_document_outputs(canonical)
    if check:
        rc = 0
        existing = ""
        if os.path.exists(MANIFEST_PATH):
            existing = open(MANIFEST_PATH, encoding="utf-8").read()
        if text != existing:
            sys.stderr.write("manifest drift: manifest.json is out of date — run "
                             "`python3 tools/build-manifest.py`\n")
            rc = 1
        for path, expected in document_outputs.items():
            if open(path, encoding="utf-8").read() != expected:
                sys.stderr.write(
                    "generated catalog drift: %s is out of date — run "
                    "`python3 tools/build-manifest.py`\n"
                    % os.path.relpath(path, REPO_ROOT)
                )
                rc = 1
        # HLS-3: --check is the single drill/CI entry point — also verify delta baselines.
        ok, msgs = check_baselines()
        for m in msgs:
            sys.stderr.write(m + "\n")
        if not ok:
            rc = 1
        if rc == 0:
            print("manifest up-to-date; delta baselines bound")
        return rc
    for path, expected in document_outputs.items():
        open(path, "w", encoding="utf-8").write(expected)
    open(MANIFEST_PATH, "w", encoding="utf-8").write(text)
    print("wrote %s" % os.path.relpath(MANIFEST_PATH, REPO_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
