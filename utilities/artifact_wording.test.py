#!/usr/bin/env python3
"""A-16.8 tests: D-86 doc wording is cycle-relative and the denial hint is one line in two places."""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_producer as P  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# D-86 enumerated target list (PRD 29.1 / plan Phase S4): exactly the write-describing
# sentences the enumerated line numbers named, matched by sentence content, not line
# number. Each fragment must appear verbatim, cycle-relative, in its file.
CYCLE_RELATIVE_TARGETS = [
    ("capabilities/autopilot-spec.md",
     "Spec work writes to `$AGENT_ARTIFACT_OUTPUT_DIR/spec/`. The canonical current "
     "blueprint is always `$AGENT_ARTIFACT_OUTPUT_DIR/spec/prd.md`."),
    ("capabilities/autopilot-code.md",
     "Code work normally writes to `$AGENT_ARTIFACT_OUTPUT_DIR/plans/<date>_<slug>/`, "
     "even when a `spec/` directory exists."),
    ("capabilities/autopilot-code.md",
     "- `standard+`: create or resume `$AGENT_ARTIFACT_OUTPUT_DIR/plans/<date>_<slug>/`."),
    ("capabilities/autopilot-research.md",
     "Research work writes to `$AGENT_ARTIFACT_OUTPUT_DIR/research/<topic>/`."),
    ("capabilities/autopilot-research.md",
     "2. Compile and bind the selected route, then resolve or create "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/research/<topic>/`; if resuming, read `pipeline_state.yaml`."),
    ("capabilities/autopilot-lab.md",
     "`$AGENT_ARTIFACT_OUTPUT_DIR/experiments/<slug>/_internal/configs/` — there is no `--out`."),
    ("capabilities/autopilot-lab.md",
     "(`$AGENT_ARTIFACT_OUTPUT_DIR/experiments/<slug>/_internal/configs/<run_id>.manifest.json`)"),
    ("capabilities/code-test.md",
     "evidence only under `$AGENT_ARTIFACT_OUTPUT_DIR/plans/<date>_<slug>/test_logs/` and"),
    ("capabilities/analyze-project.md",
     "writes structured artifacts to `$AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/`. Invoke it"),
    ("skills/code-plan/SKILL.md",
     "Search `$AGENT_ARTIFACT_OUTPUT_DIR/plans/` for a similar plan and branch on its frontmatter status:"),
    ("skills/code-plan/SKILL.md",
     "Save canonical plan to: $AGENT_ARTIFACT_OUTPUT_DIR/plans/{YYYY-MM-DD}_{short-task-name}/plan.md"),
    ("skills/code-plan/SKILL.md",
     "Save execution checklist to: $AGENT_ARTIFACT_OUTPUT_DIR/plans/{YYYY-MM-DD}_{short-task-name}/checklist.md"),
    ("skills/code-plan/SKILL.md",
     "Set `{log_dir}` to the directory containing root `plan.md`; for example, "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/plans/2026-03-18_task/plan.md` resolves to "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/plans/2026-03-18_task/`."),
    ("skills/code-execute/SKILL.md",
     "Example: `$AGENT_ARTIFACT_OUTPUT_DIR/plans/2026-03-18_refactor_engine/plan.md` → "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/plans/2026-03-18_refactor_engine/`"),
    ("skills/code-test/SKILL.md",
     "Plan path: use the task root above `plan/`; for example, "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/plans/2026-03-18_refactor/plan.md` → "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/plans/2026-03-18_refactor/`."),
    ("skills/code-report/SKILL.md",
     "5. Update $AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/code/ for successful steps "
     "only when that directory already exists."),
    ("skills/code-report/SKILL.md",
     "git diff --stat -- $AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/code/ CLAUDE.md"),
    ("skills/draft-strategy/SKILL.md",
     "- `--output <dir>`: `$AGENT_ARTIFACT_OUTPUT_DIR/documents/{date}_{name}/`"),
    ("skills/draft-refine/SKILL.md",
     "- Otherwise fuzzy-search with `ls -d $AGENT_ARTIFACT_OUTPUT_DIR/documents/*$ARGUMENTS* 2>/dev/null`."),
    ("skills/draft-refine/SKILL.md",
     "- $AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/paper/*.md is the primary source produced "
     "by `analyze-project --mode paper`"),
    ("skills/design-init/SKILL.md",
     "Look for `design_state.yaml` under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/<name>/` or "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/spec/design/`."),
    ("skills/design-init/SKILL.md",
     "- prior design cycles under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/*` or `design/`"),
    ("skills/design-refs/SKILL.md",
     "2. Otherwise select the latest applicable `design_state.yaml` under "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/designs/` or `$AGENT_ARTIFACT_OUTPUT_DIR/spec/*/design/`."),
    ("skills/design-tokens/SKILL.md",
     "Find `design_state.yaml` under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/<name>/` or "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/spec/design/`."),
    ("skills/design-components/SKILL.md",
     "Find `design_state.yaml` under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/<name>/` or the "
     "app's `design/` directory."),
    ("core/CONVENTIONS.md",
     "`$AGENT_ARTIFACT_OUTPUT_DIR/research/<topic>/` contains T1 `pipeline_summary.md`"),
    ("core/CONVENTIONS.md",
     "`$AGENT_ARTIFACT_OUTPUT_DIR/documents/<date>_<name>/` contains T1 pipeline state"),
    ("core/DESIGN_PRINCIPLES.md",
     "- `$AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/{code,paper,doc}/*` from `analyze-project`;"),
    ("core/DESIGN_PRINCIPLES.md",
     "- `$AGENT_ARTIFACT_OUTPUT_DIR/research/<topic>/*` from `autopilot-research`;"),
]

CAPABILITY_ENTRY_DOCS = [
    "capabilities/autopilot-spec.md",
    "capabilities/autopilot-code.md",
    "capabilities/autopilot-research.md",
    "capabilities/autopilot-lab.md",
    "capabilities/code-test.md",
    "capabilities/analyze-project.md",
]

# D-86 negative-probe reason tokens: must stay byte-identical (fleet_cutover_gate's
# negative probe compares them as literal strings).
REASON_TOKENS = ("legacy-top-level-write-denied", "shared-revision-immutable")

_BEGIN_RE = re.compile(r"\bbegin\b", re.IGNORECASE)


class WordingTargetsTest(unittest.TestCase):
    def test_a16_8_listed_docs_use_cycle_relative_expressions(self):
        for rel_path, fragment in CYCLE_RELATIVE_TARGETS:
            content = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            self.assertIn(fragment, content, f"{rel_path}: missing cycle-relative fragment")
            self.assertIn("$AGENT_ARTIFACT_OUTPUT_DIR", fragment)
            self.assertNotIn("<artifact-root>", fragment)

    def test_a16_8_each_capability_entry_doc_mentions_begin(self):
        for rel_path in CAPABILITY_ENTRY_DOCS:
            content = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            self.assertTrue(_BEGIN_RE.search(content), f"{rel_path}: no mention of begin")


class HintContractTest(unittest.TestCase):
    _ENV_KEYS = ("AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "artifact-root"
        self.root.mkdir()
        producer_dir = self.root / ".runtime" / "artifact-producer" / "v1"
        producer_dir.mkdir(parents=True)
        (producer_dir / "cutover.json").write_text('{"state": "active"}\n', encoding="utf-8")
        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        self._tmp.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_a16_8_check_write_hint_equals_resolve_output_detail(self):
        target = self.root / "plans" / "2026-01-01_x" / "plan.md"
        verdict = P.check_write(self.root, target)
        self.assertEqual(verdict["verdict"], "deny")
        self.assertEqual(verdict["reason"], "legacy-top-level-write-denied")
        self.assertEqual(verdict["hint"], P.LEGACY_WRITE_HINT)

        with self.assertRaises(P.ProducerError) as ctx:
            P.resolve_output_dir(self.root, "plans")
        self.assertEqual(ctx.exception.code, "legacy-top-level-write-denied")
        self.assertEqual(ctx.exception.detail, f"plans: {P.LEGACY_WRITE_HINT}")
        # The same one-line hint rides both surfaces.
        self.assertEqual(verdict["hint"], ctx.exception.detail.split(": ", 1)[1])

    def test_a16_8_reason_tokens_unchanged(self):
        for token in REASON_TOKENS:
            self.assertIn(token, ("legacy-top-level-write-denied", "shared-revision-immutable"))
        target = self.root / "plans" / "2026-01-01_x" / "plan.md"
        self.assertEqual(P.check_write(self.root, target)["reason"], "legacy-top-level-write-denied")
        self.assertEqual(P.check_write(self.root, self.root / "shared" / "spec" / "x")["reason"],
                          "shared-revision-immutable")


class ClaudeProjectionTest(unittest.TestCase):
    def test_a16_8_claude_skill_projection_in_sync(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "sync-entry-skill-layer.py"), "--check"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0,
                          f"adapters/claude/skills projection is stale: {result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
