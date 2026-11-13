"""Bare profile aliases show the model their runtime catalog actually resolves.

The version comes from the Claude runtime's already-written catalog cache; the
profile keys (``opus``/``sonnet``) and the settings Fleet writes never change.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                     # noqa: E402
from fleet.explorer import focus, models                     # noqa: E402
from fleet.explorer.grid import CellGrid                     # noqa: E402


def _record(model_id, family, section="main"):
    return {"id": model_id, "name": model_id, "section": section,
            "runtime": {"family": family}}


def _write_catalog(root, records, name="catalog.json"):
    directory = os.path.join(root, "cache", "model-catalog")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name), "w", encoding="utf-8") as stream:
        json.dump({"catalog": {"config": {"models": records}}}, stream)


class AliasDisplayTest(unittest.TestCase):
    def _catalog(self, root):
        return mock.patch.dict(os.environ, {"HEARTING_MODEL_CONFIG_HOME": root}, clear=False)

    def test_profile_aliases_show_the_single_main_catalog_model(self):
        with tempfile.TemporaryDirectory() as root:
            _write_catalog(root, [
                _record("claude-opus-5-5", "opus"),
                _record("claude-sonnet-5-5", "sonnet"),
                _record("claude-haiku-5-5", "haiku"),
                _record("claude-fable-5-1", "fable"),
                _record("claude-opus-5", "opus", "overflow"),
            ])
            with self._catalog(root):
                self.assertEqual(render._dispatch_display_model("opus"), "Opus 5.5")
                self.assertEqual(render._dispatch_display_model("sonnet"), "Sonnet 5.5")
                # a proper-noun value is already an observed display name, not an alias
                self.assertEqual(render._dispatch_display_model("Opus"), "Opus")
                self.assertEqual(render._clean_model("sonnet"), "Sonnet 5.5")
                # wire ids never depend on the alias catalog
                self.assertEqual(render._dispatch_display_model("claude-opus-5"), "Opus 5")

    def test_missing_or_ambiguous_catalog_keeps_the_alias(self):
        with tempfile.TemporaryDirectory() as root:
            with self._catalog(root):
                self.assertEqual(render._dispatch_display_model("opus"), "Opus")
                self.assertEqual(render._dispatch_display_model("sonnet"), "Sonnet")
        with tempfile.TemporaryDirectory() as root:
            _write_catalog(root, [_record("claude-opus-5-5", "opus")], "a.json")
            _write_catalog(root, [_record("claude-opus-5", "opus"),
                                  _record("claude-sonnet-5-5", "sonnet")], "b.json")
            with self._catalog(root):
                # two accounts disagree about opus -> unresolved; sonnet still unique
                self.assertEqual(render._dispatch_display_model("opus"), "Opus")
                self.assertEqual(render._dispatch_display_model("sonnet"), "Sonnet 5.5")

    def test_profile_values_and_choices_stay_the_alias(self):
        values = {"CFG_MODEL_PROFILE_DEEP": "opus:xhigh",
                  "CFG_MODEL_PROFILE_BALANCED": "model/opus:high",
                  "CFG_TIER_OPUS_MODEL": "opus"}
        self.assertEqual(focus._profile_cell(values, "deep"), ["opus", "xhigh"])
        self.assertEqual(focus._profile_cell(values, "balanced"), ["opus", "high"])
        with tempfile.TemporaryDirectory() as root:
            _write_catalog(root, [_record("claude-opus-5-5", "opus")])
            with self._catalog(root):
                self.assertEqual(focus._profile_cell(values, "deep"), ["opus", "xhigh"])
                self.assertEqual(render._dispatch_display_model("opus"), "Opus 5.5")

    def test_models_screen_uses_the_resolved_display_name(self):
        with tempfile.TemporaryDirectory() as root:
            _write_catalog(root, [_record("claude-opus-5-5", "opus")])
            with self._catalog(root):
                shown = {"opus": render._clean_model(render._dispatch_display_model("opus"))}
                self.assertEqual(shown, {"opus": "Opus 5.5"})
                grid = CellGrid(60, 3)
                models._model_cell(grid, 1, 1, "claude", ("opus", "high"), shown, {}, 40)
                self.assertIn("Opus 5.5", "".join(cell[0] for cell in grid.cells[1]))

    def test_models_data_resolves_profile_aliases_when_main_only_is_set(self):
        class ModelConfig:
            @staticmethod
            def resolve_config(_adapter):
                return {"CFG_MODEL_PROFILE_DEEP": "opus:xhigh",
                        "CFG_TIER_OPUS_MODEL": "opus",
                        "CFG_MAIN_SESSION_ONLY_MODELS": "fable"}, {}

        with tempfile.TemporaryDirectory() as root:
            _write_catalog(root, [_record("claude-opus-5-5", "opus")])
            with self._catalog(root), \
                    mock.patch.object(focus, "utility", return_value=ModelConfig), \
                    mock.patch("tools.fleet.explorer.model_catalog.read_catalogs",
                               return_value={}):
                config = focus.read_model_config()

        self.assertEqual(config["profile_map"]["claude"]["deep"], ["opus", "xhigh"])
        self.assertEqual(config["shown"]["opus"], "Opus 5.5")
        self.assertEqual(config["shown"]["fable"], "Fable")


if __name__ == "__main__":
    unittest.main()
