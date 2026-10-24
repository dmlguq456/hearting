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

# D-86 enumerated write-contract targets, revised for the current standalone and
# routed cycle semantics. Match the meaning-owning sentence, not historical line
# numbers or superseded per-capability path recipes.
CYCLE_RELATIVE_TARGETS = [
    ("capabilities/autopilot-spec.md",
     "Spec work writes to `$AGENT_ARTIFACT_OUTPUT_DIR/spec/`. The canonical current "
     "blueprint is always `$AGENT_ARTIFACT_OUTPUT_DIR/spec/prd.md`."),
    ("capabilities/autopilot-code.md",
     "The standalone default is `$AGENT_ARTIFACT_OUTPUT_DIR/plans/<date>_<slug>/`, "
     "even when a `spec/` directory exists. Routed stages use their sealed node outputs "
     "and write scope; `AGENT_ARTIFACT_OUTPUT_DIR` supplies only the cycle base."),
    ("capabilities/autopilot-code.md",
     "2. **write only inside the open cycle.** Every durable artifact goes under the "
     "current node's declared outputs under `AGENT_ARTIFACT_OUTPUT_DIR`."),
    ("capabilities/autopilot-research.md",
     "The standalone default is `$AGENT_ARTIFACT_OUTPUT_DIR/research/<topic>/`. Routed "
     "stages use their declared outputs; the cycle output directory is only the base."),
    ("capabilities/autopilot-research.md",
     "1. The launcher prepares the route's cycle and carries its exact context."),
    ("capabilities/autopilot-lab.md",
     "2. **write only inside the open cycle.** Every durable artifact uses the selected "
     "node's declared outputs under `AGENT_ARTIFACT_OUTPUT_DIR`."),
    ("capabilities/autopilot-lab.md",
     "Config snapshots and manifests keep the `experiments/<slug>/_internal/configs/` "
     "suffix beneath that selected subtree."),
    ("capabilities/code-test.md",
     "When invoked from a `standard+` `autopilot-code` stage cycle, write verification "
     "evidence only to the test node's declared outputs and write scope."),
    ("capabilities/analyze-project.md",
     "writes structured artifacts to `$AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/`. Invoke it"),
    ("capabilities/code-plan/references/procedure.md",
     "For standalone planning, search `$AGENT_ARTIFACT_OUTPUT_DIR/plans/` for a similar "
     "plan and branch on its frontmatter status:"),
    ("capabilities/code-plan/references/procedure.md",
     "Save canonical plan and checklist to: the plan node's declared outputs"),
    ("capabilities/code-plan/references/procedure.md",
     "Use the supplied `plan.md` path to locate its sibling checklist and logs."),
    ("capabilities/code-plan/references/procedure.md",
     "The stage uses its node's declared outputs and write scope."),
    ("capabilities/code-execute/references/procedure.md",
     "For a registered stage, write `checklist.md` and step logs only to the runtime-declared "
     "output locations under `$AGENT_ARTIFACT_OUTPUT_DIR`, "
     "within the node's declared outputs and write scope."),
    ("capabilities/code-test/references/procedure.md",
     "Write a detailed test log to: the code-test node's declared report output"),
    ("capabilities/code-report/references/procedure.md",
     "5. Update $AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/code/ for successful steps "
     "only when that directory already exists."),
    ("capabilities/code-report/references/procedure.md",
     "git diff --stat -- $AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/code/ CLAUDE.md"),
    ("capabilities/draft-strategy/references/procedure.md",
     "- `--output <dir>`: `$AGENT_ARTIFACT_OUTPUT_DIR/documents/{date}_{name}/`"),
    ("capabilities/draft-refine/references/procedure.md",
     "- Otherwise fuzzy-search with `ls -d $AGENT_ARTIFACT_OUTPUT_DIR/documents/*$ARGUMENTS* 2>/dev/null`."),
    ("capabilities/draft-refine.md",
     "the resolver applies the selected node's declared outputs and write scope while preserving "
     "the existing draft target."),
    ("capabilities/design-init/references/procedure.md",
     "Look for `design_state.yaml` under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/<name>/` or "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/spec/design/`."),
    ("capabilities/design-init/references/procedure.md",
     "- prior design cycles under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/*` or `design/`"),
    ("capabilities/design-refs/references/procedure.md",
     "2. Otherwise select the latest applicable `design_state.yaml` under "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/designs/` or `$AGENT_ARTIFACT_OUTPUT_DIR/spec/*/design/`."),
    ("capabilities/design-tokens/references/procedure.md",
     "Find `design_state.yaml` under `$AGENT_ARTIFACT_OUTPUT_DIR/designs/<name>/` or "
     "`$AGENT_ARTIFACT_OUTPUT_DIR/spec/design/`."),
    ("capabilities/design-components/references/procedure.md",
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

CAPABILITY_CYCLE_CONTRACTS = [
    ("capabilities/autopilot-spec.md", (
        "1. **begin before the first write.** After route compile/bind, depth-0 runs",
    )),
    ("capabilities/autopilot-code.md", (
        "1. **prepare the route's cycle.** Registered frame/owner launch prepares or "
        "resumes the route's cycle and carries its exact `AGENT_ARTIFACT_*` context.",
        "2. **write only inside the open cycle.** Every durable artifact goes under the "
        "current node's declared outputs under `AGENT_ARTIFACT_OUTPUT_DIR`.",
    )),
    ("capabilities/autopilot-research.md", (
        "1. The launcher prepares the route's cycle and carries its exact context.",
        "2. Routed stages write to their declared outputs and write scope under the "
        "supplied cycle output directory; the producer owns cycle paths.",
    )),
    ("capabilities/autopilot-lab.md", (
        "1. The launcher prepares the route's cycle and carries its exact context.",
        "2. **write only inside the open cycle.** Every durable artifact uses the selected "
        "node's declared outputs under `AGENT_ARTIFACT_OUTPUT_DIR`.",
    )),
    ("capabilities/code-test.md", (
        "When invoked from a `standard+` `autopilot-code` stage cycle, write verification "
        "evidence only to the test node's declared outputs and write scope.",
        "the resolver applies the test node's declared outputs and write scope.",
    )),
    ("capabilities/analyze-project.md", (
        "1. **begin before the first write.** After the route is compiled and bound,",
        "2. **write only inside the open cycle.** Every durable artifact goes under",
    )),
]

# D-86 negative-probe reason tokens: must stay byte-identical (fleet_cutover_gate's
# negative probe compares them as literal strings).
REASON_TOKENS = ("legacy-top-level-write-denied", "shared-revision-immutable")

def _normalized(text):
    return re.sub(r"\s+", " ", text).strip()


class WordingTargetsTest(unittest.TestCase):
    def test_a16_8_listed_docs_use_cycle_relative_expressions(self):
        for rel_path, fragment in CYCLE_RELATIVE_TARGETS:
            content = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            self.assertIn(_normalized(fragment), _normalized(content),
                          f"{rel_path}: missing current artifact-output fragment")
            self.assertTrue(
                "$AGENT_ARTIFACT_OUTPUT_DIR" in fragment
                or "cycle" in fragment.lower()
                or "declared outputs" in fragment.lower()
                or "declared report output" in fragment.lower()
                or "selected subtree" in fragment.lower()
                or "`plan.md` path" in fragment.lower(),
                f"{rel_path}: target does not describe a standalone base or routed cycle output",
            )
            self.assertNotIn("<artifact-root>", fragment)

    def test_a16_8_each_capability_entry_doc_describes_its_routed_cycle_lifecycle(self):
        for rel_path, fragments in CAPABILITY_CYCLE_CONTRACTS:
            content = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            normalized = _normalized(content)
            for fragment in fragments:
                self.assertIn(_normalized(fragment), normalized,
                              f"{rel_path}: missing routed cycle contract: {fragment}")


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

    def test_finalize_primary_accepts_absolute_path_inside_cycle_artifacts(self):
        # 2026-09-16 DX: an absolute --primary failed `primary-artifact-missing`
        # with no hint that only cycle-relative locators are accepted.
        cycle = self.root / "campaigns" / "c" / "cyc"
        (cycle / "artifacts" / "documents").mkdir(parents=True)
        target = cycle / "artifacts" / "documents" / "report.md"
        target.write_text("x", encoding="utf-8")
        self.assertEqual(P._cycle_relative_primary(str(target), cycle), "artifacts/documents/report.md")
        self.assertEqual(P._cycle_relative_primary("artifacts/documents/report.md", cycle),
                         "artifacts/documents/report.md")
        self.assertEqual(P._cycle_relative_primary("documents/report.md", cycle), "documents/report.md")
        outside = str(self.root / "elsewhere.md")
        self.assertEqual(P._cycle_relative_primary(outside, cycle), outside)
        self.assertIsNone(P._cycle_relative_primary(None, cycle))
        with self.assertRaises(P.ProducerError) as ctx:
            P._choose_primary([("artifacts/documents/report.md", b"x")], "documents/other.md")
        self.assertEqual(ctx.exception.code, "primary-artifact-missing")
        self.assertIn("cycle-relative", ctx.exception.detail)
        self.assertIn("artifacts/documents/report.md", ctx.exception.detail)

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
