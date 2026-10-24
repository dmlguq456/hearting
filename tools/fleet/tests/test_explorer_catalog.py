"""The explorer reads bounded runtime catalogs without invoking their providers."""
import json
import os
import tempfile
import unittest
from unittest import mock

from tools.fleet.explorer import model_catalog


class ModelCatalogTests(unittest.TestCase):
    def test_supported_runtime_shapes_keep_catalog_order_and_deduplicate(self):
        self.assertEqual(model_catalog._ids({"models": [{"slug": "model-a"}, {"id": "model-b"},
                                                          {"slug": "model-a"}]}),
                         ("model-a", "model-b"))
        self.assertEqual(model_catalog._ids({"providers": {"provider": {"models": {
            "model-c": {"name": "display name"}, "model-d": {}}}}}), ("model-c", "model-d"))
        self.assertEqual(model_catalog._ids({"provider": {"models": {
            "model-e": {"id": "model-e"}, "model-f": {}}}}), ("model-e", "model-f"))

    def test_catalog_is_read_from_runtime_cache_and_missing_source_stays_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "catalog.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"models": [{"slug": "runtime-model"}]}, stream)
            with mock.patch.object(model_catalog, "_candidates", return_value=(path,)):
                catalog = model_catalog.read_catalog("codex")
            self.assertEqual((catalog.state, catalog.models), ("available", ("runtime-model",)))
            self.assertEqual(catalog.source, path)
        with mock.patch.object(model_catalog, "_candidates", return_value=()):
            missing = model_catalog.read_catalog("claude")
        self.assertEqual((missing.state, missing.models, missing.source), ("unknown", (), None))

    def test_runtime_paths_authentication_and_claude_catalog_glob(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.abspath(root)
            config = os.path.join(root, "claude")
            cache = os.path.join(root, "cache")
            data = os.path.join(root, "data")
            os.makedirs(os.path.join(config, "cache", "model-catalog"))
            os.makedirs(os.path.join(cache, "opencode"))
            os.makedirs(os.path.join(data, "opencode"))
            claude_path = os.path.join(config, "cache", "model-catalog", "catalog.json")
            with open(claude_path, "w", encoding="utf-8") as stream:
                json.dump({"models": [{"id": "claude-cache-model"}]}, stream)
            with open(os.path.join(cache, "opencode", "models.json"), "w", encoding="utf-8") as stream:
                json.dump({"providers": {"ready": {"models": {"allowed": {}}},
                                          "unused": {"models": {"denied": {}}}}}, stream)
            with open(os.path.join(data, "opencode", "auth.json"), "w", encoding="utf-8") as stream:
                json.dump({"ready": {"token": "must-not-be-read"}}, stream)
            codex_path = os.path.join(root, "models_cache.json")
            with open(codex_path, "w", encoding="utf-8") as stream:
                json.dump({"models": [{"id": "listed", "visibility": "list"},
                                       {"id": "hidden", "visibility": "hide"}]}, stream)
            env = {"HEARTING_MODEL_CONFIG_HOME": config, "XDG_CACHE_HOME": cache,
                   "XDG_DATA_HOME": data}
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertEqual(model_catalog.read_catalog("claude").models, ("claude-cache-model",))
                catalog = model_catalog.read_catalog("opencode")
                with mock.patch.object(model_catalog, "_candidates", return_value=(codex_path,)):
                    codex = model_catalog.read_catalog("codex")
            self.assertEqual(catalog.models, ("allowed",))
            self.assertEqual(catalog.source, os.path.join(cache, "opencode", "models.json"))
            self.assertEqual(codex.models, ("listed",))
