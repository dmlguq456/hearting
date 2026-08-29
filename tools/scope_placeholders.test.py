#!/usr/bin/env python3
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools" / "check-scope-placeholders.py"

SPEC = importlib.util.spec_from_file_location("check_scope_placeholders", CHECKER)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class ScopePlaceholderVocabularyTest(unittest.TestCase):
    def test_case_a_current_topologies_scan_passes(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER)], cwd=ROOT, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_case_b_unregistered_token_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fixture = {
                "recipes": [
                    {
                        "quick": {
                            "write_scope": ["analysis_project/<future>/**"],
                        }
                    }
                ]
            }
            (tmp_path / "topologies.json").write_text(json.dumps(fixture), encoding="utf-8")
            vocab = (ROOT / "tools" / "scope-placeholders.tsv").read_text(encoding="utf-8")
            (tmp_path / "scope-placeholders.tsv").write_text(vocab, encoding="utf-8")
            result = self._run_checker_against(tmp_path)
            self.assertNotEqual(result.returncode, 0)

    def test_case_c_removing_mode_from_vocab_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fixture = {
                "recipes": [
                    {"quick": {"write_scope": ["analysis_project/<mode>/**"]}}
                ]
            }
            (tmp_path / "topologies.json").write_text(json.dumps(fixture), encoding="utf-8")
            vocab_lines = (ROOT / "tools" / "scope-placeholders.tsv").read_text(encoding="utf-8").splitlines()
            filtered = [line for line in vocab_lines if not line.startswith("<mode>\t")]
            (tmp_path / "scope-placeholders.tsv").write_text("\n".join(filtered) + "\n", encoding="utf-8")
            result = self._run_checker_against(tmp_path)
            self.assertNotEqual(result.returncode, 0)

    def test_case_d_mode_substitution_matches_real_path(self):
        scope = "analysis_project/<mode>/**"
        pattern = re.sub(r"<[a-z_]+>", "*", scope)
        root = pattern[:-3] if pattern.endswith("/**") else pattern
        parts = root.split("/")
        value = "analysis_project/code/mega-audit/cg/00_overview.md".split("/")
        self.assertTrue(len(value) >= len(parts))
        import fnmatch

        matched = all(
            fnmatch.fnmatchcase(value_part, pattern_part)
            for value_part, pattern_part in zip(value, parts)
        )
        self.assertTrue(matched)

    def _run_checker_against(self, tmp_path: Path):
        script = f"""
import sys
sys.path.insert(0, {str(CHECKER.parent)!r})
import importlib.util
spec = importlib.util.spec_from_file_location('c', {str(CHECKER)!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.TOPOLOGIES = __import__('pathlib').Path({str(tmp_path / 'topologies.json')!r})
mod.VOCAB = __import__('pathlib').Path({str(tmp_path / 'scope-placeholders.tsv')!r})
raise SystemExit(mod.main([]))
"""
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
