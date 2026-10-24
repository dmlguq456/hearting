#!/usr/bin/env python3
"""Deterministic entry-router layer gate, including owner-link resolution."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMIT, TOTAL_LIMIT = 4096, 53248
manifest = json.loads((ROOT / "harness-manifest.json").read_text())
entries = sorted(name for name, spec in manifest["capabilities"].items()
                 if spec["invocation"]["class"] == "entry-router")
assert len(entries) == 12, f"expected exactly 12 entry routers, got {len(entries)}"
owner_row = re.compile(
    r"^\| Entry load phase \| `post-approval`; owner contract `capabilities/([^`]+)\.md` \|$",
    re.MULTILINE,
)
declared_owners = {}
for capability in (ROOT / "capabilities").glob("*.md"):
    matches = owner_row.findall(capability.read_text(encoding="utf-8"))
    assert len(matches) <= 1, f"{capability.relative_to(ROOT)} has duplicate owner rows"
    if matches:
        declared_owners[capability.stem] = matches[0]
assert sorted(declared_owners) == entries, "capability owner rows must match exactly the 12 entry routers"
assert all(name == target for name, target in declared_owners.items()), "capability owner row target mismatch"
for document, phrases in {
    "core/DESIGN_PRINCIPLES.md": ("compact pre-approval router", "assigned stage worker"),
    "capabilities/README.md": ("compact pre-approval router", "Assigned stage workers"),
}.items():
    text = " ".join((ROOT / document).read_text(encoding="utf-8").split())
    assert all(phrase in text for phrase in phrases), f"{document} lost entry-layer contract"

workflow = (ROOT / "core/WORKFLOW.md").read_text(encoding="utf-8")
assert "uses that entry as the primary route" in workflow
assert "`direct` is an intensity inside the selected entry route" in workflow
analysis_invocation = manifest["capabilities"]["analyze-project"]["invocation"]
assert "user asks to analyze existing code" in analysis_invocation["use_when"]
assert "default initial analysis to persistent output" in analysis_invocation["use_when"]
assert "conversational, read-only, or no-file analysis" in analysis_invocation["not_for"]
for bootstrap in (
    "adapters/claude/CLAUDE.md",
    "adapters/codex/AGENTS.md",
    "adapters/opencode/AGENTS.md",
):
    text = (ROOT / bootstrap).read_text(encoding="utf-8")
    routing = " ".join(text.split("Route by `core/WORKFLOW.md §0.2`", 1)[1].split())
    compose = re.search(r"`(?:utilities/capability-route.py|preflight.sh|hearting run capability-route) compose`", routing)
    assert compose is not None, f"{bootstrap} lost the compose route surface"
    normalized = " ".join(routing.split())
    assert "The main session decides only whether the work is direct (§0.2.1)" in normalized, (
        f"{bootstrap} must limit the main session to deciding direct or not"
    )
    assert "use `--shape direct` for one atomic, reversible inline change; otherwise omit `--shape` and let the frame decide" in normalized, (
        f"{bootstrap} must send other new work to the frame by omitting shape"
    )


def anchor(value: str) -> str:
    value = re.sub(r"`([^`]*)`", r"\1", value).lower()
    value = re.sub(r"[^a-z0-9 _-]", "", value)
    return re.sub(r"[ _]", "-", value).strip("-")


def owner_targets(text: str) -> list[tuple[str, bool]]:
    markdown = [(target, True) for target in re.findall(r"\]\(([^)]+)\)", text)]
    literals = [(target, False) for target in re.findall(r"`((?:\.\.?/)?[^`\s#]+\.md(?:#[^`\s]+)?)`", text)]
    return markdown + literals


def resolve_owner(owner: Path, root: Path = ROOT) -> None:
    text = owner.read_text(encoding="utf-8")
    assert "](<agent-home>/" not in text, f"{owner.relative_to(ROOT)} has a non-CommonMark owner link"
    for target, required in owner_targets(text):
        if "*" in target or ("<" in target and not target.startswith("<agent-home>/")) or "{" in target or target in {"path", "url", "target", "filename.md", "file.md"} or target.startswith(("http:", "https:", "mailto:")):
            continue
        path_text, separator, fragment = target.partition("#")
        if not path_text:
            continue
        if not path_text.endswith(".md") or path_text.startswith("/"):
            continue
        if path_text.startswith("<agent-home>/"):
            target_path = root / path_text[len("<agent-home>/"):]
        elif path_text.startswith(("capabilities/", "core/", "roles/", "adapters/")):
            target_path = root / path_text
        else:
            target_path = (owner.parent / path_text).resolve()
        try:
            target_path.relative_to(root)
        except ValueError:
            raise AssertionError(f"{owner} target escapes document root: {target}")
        if not required and not target_path.exists():
            # Inline-code filenames may describe future artifacts; only resolve
            # them when they name a concrete adjacent Skill reference.
            continue
        assert target_path.is_file(), f"{owner.relative_to(ROOT)} unresolved target {target}"
        if required and separator and not re.fullmatch(r"L\d+(?:-L?\d+)?", fragment):
            anchors = {anchor(line[1:].strip()) for line in target_path.read_text(encoding="utf-8").splitlines()
                       if line.startswith("#")}
            assert fragment in anchors, f"{owner.relative_to(ROOT)} unresolved anchor {target}"


OWNER_TREES = (
    "skills",
    "adapters/claude/skills",
    "adapters/claude/plugin-marketplace/plugins/hearting-claude/skills",
)
import sys
sys.path.insert(0, str(ROOT / "tools"))
import harness_manifest

for tree in OWNER_TREES + ("adapters/codex/skills", "adapters/opencode/skills"):
    sizes = []
    for name in entries:
        skill = ROOT / tree / name / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        size = len(skill.read_bytes())
        sizes.append(size)
        assert size <= LIMIT, f"{skill.relative_to(ROOT)} exceeds {LIMIT} bytes"
        assert "Use when " in text and "Not for " in text, f"{skill} lost manifest routing metadata"
        if tree in OWNER_TREES:
            assert "Post-approval owner contract" in text and "Do not load references" in text
            assert not (skill.parent / "README.md").exists(), f"{skill.parent} retains redundant README.md"
            assert not (skill.parent / "references").exists(), f"{skill.parent} keeps a duplicate procedure tree"
            assert "## Shared Procedure References" in text
        else:
            assert "## Shared Procedure References" in text if tree.endswith(("codex/skills", "opencode/skills")) else True
            assert "Before approval" in text and f"capabilities/{name}.md" in text
            assert "## Projected Portable Details" not in text and "## Portable Contract" not in text
        targets = harness_manifest.procedure_paths(name)
        for target in targets:
            logical = target.relative_to(ROOT).as_posix()
            assert logical in text, f"{skill.relative_to(ROOT)} omits shared procedure edge {logical}"
    assert sum(sizes) <= TOTAL_LIMIT, f"{tree} aggregate exceeds {TOTAL_LIMIT} bytes"
    print(f"PASS {tree}: entries={len(entries)} total={sum(sizes)} max={max(sizes)}")

# Native plugin bundles include the deterministic portable document closure.
portable_sources = harness_manifest.portable_bundle_files(ROOT)
portable_paths = {path.relative_to(ROOT).as_posix() for path in portable_sources}
assert "docs/lab-execution-access.md" in portable_paths, "linked lab access document is absent from plugin closure"
assert len(portable_sources) == 192, f"expected 192 portable bundle sources, got {len(portable_sources)}"
assert {path for path in portable_paths if path not in {
    path.relative_to(ROOT).as_posix()
    for section in ("core", "capabilities", "roles")
    for path in (ROOT / section).rglob("*") if path.is_file() and not path.is_symlink()
} | {"adapters/claude/references/design-runtime.md"}} == {
    "docs/lab-execution-access.md", "tools/web-bundle/README.md"
}, "unexpected tracked linked documents in plugin bundle closure"


def check_bundle(expected: dict[str, bytes], actual: dict[str, bytes]) -> None:
    assert actual.keys() == expected.keys(), "plugin bundle path set drift"
    for path, content in expected.items():
        assert actual[path] == content, f"plugin bundle content drift: {path}"


def mutation_must_fail(label: str, check) -> None:
    try:
        check()
    except AssertionError:
        return
    raise AssertionError(f"{label} mutation was not detected")


expected_bundle = {
    path.relative_to(ROOT).as_posix(): path.read_bytes() for path in portable_sources
}
for plugin in (
    ROOT / "adapters/claude/plugin-marketplace/plugins/hearting-claude",
    ROOT / "adapters/codex/plugins/hearting-codex",
):
    actual_bundle = {
        path.relative_to(plugin).as_posix(): path.read_bytes()
        for section in ("capabilities", "core", "roles", "adapters", "docs", "tools")
        for path in (plugin / section).rglob("*") if path.is_file()
    }
    check_bundle(expected_bundle, actual_bundle)

first_bundle_path = sorted(expected_bundle)[0]
mutation_must_fail(
    "deleted plugin bundle reference",
    lambda: check_bundle(expected_bundle, {key: value for key, value in expected_bundle.items()
                                           if key != first_bundle_path}),
)
altered_bundle = dict(expected_bundle)
altered_bundle[first_bundle_path] += b"\nmutation"
mutation_must_fail("altered plugin bundle reference", lambda: check_bundle(expected_bundle, altered_bundle))

# All adapters expose the same canonical procedure path set per capability.
for name in sorted(harness_manifest.skill_capabilities(manifest)):
    expected_paths = [path.relative_to(ROOT).as_posix() for path in harness_manifest.procedure_paths(name)]
    skill_paths = [
        ROOT / "skills" / name / "SKILL.md",
        ROOT / "adapters/claude/skills" / name / "SKILL.md",
        ROOT / "adapters/codex/skills" / name / "SKILL.md",
        ROOT / "adapters/opencode/skills" / name / "SKILL.md",
    ]
    edge_sets = []
    for skill in skill_paths:
        text = skill.read_text(encoding="utf-8")
        declared = re.findall(r"(?m)^- `(capabilities/[^`]+/references/[^`]+)`$", text)
        edge_sets.append(declared)
        assert declared == expected_paths, f"{skill.relative_to(ROOT)} procedure read-set drift"
    assert all(edges == edge_sets[0] for edges in edge_sets), f"{name} native procedure read-set differs"
    if expected_paths:
        for skill in skill_paths[2:]:
            original = skill.read_text(encoding="utf-8")
            edge = expected_paths[0]
            mutated = original.replace(f"- `{edge}`\n", "", 1)

            def check_edges(text: str, required: list[str] = expected_paths) -> None:
                declared = re.findall(r"(?m)^- `(capabilities/[^`]+/references/[^`]+)`$", text)
                assert declared == required, f"{skill.relative_to(ROOT)} procedure read-set drift"

            mutation_must_fail(f"deleted {skill.parent.parent.name} procedure edge", lambda: check_edges(mutated))

for document in sorted((ROOT / "capabilities").glob("*/references/*.md")):
    resolve_owner(document)
resolve_owner(ROOT / "capabilities/draft-strategy/references/delegate-prompt.md")
for plugin in (
    ROOT / "adapters/claude/plugin-marketplace/plugins/hearting-claude",
    ROOT / "adapters/codex/plugins/hearting-codex",
):
    for section in ("capabilities", "core", "roles", "adapters"):
        for document in sorted((plugin / section).rglob("*.md")):
            resolve_owner(document, plugin)

execute_procedure = (ROOT / "capabilities/code-execute/references/procedure.md").read_text()
assert "checklist" in execute_procedure.lower() and "stage graph" in execute_procedure.lower()
refine_stages = (ROOT / "capabilities/autopilot-refine/references/process-stages.md").read_text()
assert "### Stage A" in refine_stages and "### Stage E" in refine_stages
assert "--user-refine" in (ROOT / "capabilities/autopilot-code/references/arguments-and-decisions.md").read_text()
assert "report-only" in (ROOT / "capabilities/audit/references/report-and-autofix.md").read_text()
assert "user-refine" in (ROOT / "capabilities/autopilot-code/references/arguments-and-decisions.md").read_text()
print(f"PASS portable-procedure-links: native-trees=5 entries={len(entries)}")
