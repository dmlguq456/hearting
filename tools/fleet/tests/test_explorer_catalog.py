"""Catalog reads use the installed runtime and never invoke model providers."""
from contextlib import chdir
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from tools.fleet.explorer import model_catalog


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-catalog-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patch = mock.patch.dict(os.environ, {
            "HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "CODEX_HOME": str(self.root / "codex"),
            "HEARTING_MODEL_CONFIG_HOME": str(self.root / "claude"),
            "OPENCODE_CONFIG": "", "OPENCODE_CONFIG_CONTENT": "",
            "OPENCODE_CONFIG_DIR": "",
            "OPENCODE_RUNTIME_VERSION": "1.19.0",
        })
        patch.start()
        self.addCleanup(patch.stop)
        for command in ("subprocess.run", "subprocess.Popen"):
            patch = mock.patch(command, side_effect=AssertionError("catalog must not start a process"))
            patch.start()
            self.addCleanup(patch.stop)

    def _native_service(self, payload, *, change_record=False, pid=None, status=200, raw=None):
        path = self.root / "state/opencode/service.json"
        path.parent.mkdir(parents=True)
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                value = payload(self.path) if callable(payload) else payload
                body = json.dumps(value).encode() if raw is None else raw
                if change_record:
                    path.write_text("{}")
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        path.write_text(json.dumps({
            "version": "2.0.32", "url": f"http://127.0.0.1:{server.server_port}",
            "pid": os.getpid() if pid is None else pid, "password": "fixture-private-password",
        }))
        path.chmod(0o600)
        cache = self.root / "cache/opencode/models.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"models": ["old-provider/stale-model"]}))
        return path

    def test_v2_catalog_precedes_legacy_cache_and_keeps_selectable_native_ids(self):
        self._native_service({"location": {"directory": str(self.root)}, "data": [
            {"providerID": "native", "id": "friendly", "modelID": "transport-name",
             "enabled": True, "status": "active"},
            {"providerID": "other", "id": "friendly", "modelID": "same-transport",
             "enabled": True, "status": "beta"},
            {"providerID": "native", "id": "upstream/model", "modelID": "wire-id",
             "enabled": True, "status": "active"},
            {"providerID": "native", "id": "disabled", "enabled": False, "status": "active"},
        ]})
        catalog = model_catalog.read_catalog("opencode")
        self.assertEqual(catalog.state, "available")
        self.assertEqual(catalog.models, (
            "native/friendly", "other/friendly", "native/upstream/model"))
        self.assertTrue(catalog.source.endswith("service.json#/api/model"))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][0], "/api/model?" + urlencode({
            "location[directory]": os.getcwd()}))
        self.assertTrue(self.requests[0][1].startswith("Basic "))
        self.assertNotIn("fixture-private-password", repr(catalog))

    def test_caller_directory_selects_catalog_on_same_existing_service_without_writes(self):
        home = self.root / "service-home"
        project = self.root / "project space + &?#% 한글"
        home.mkdir()
        project.mkdir()
        by_directory = {
            str(home): [{"providerID": "native", "id": "home", "enabled": True, "status": "active"}],
            str(project): [{"providerID": "native", "id": "project", "enabled": True, "status": "active"}],
        }

        def snapshot(target):
            directory = parse_qs(urlsplit(target).query).get("location[directory]", [str(home)])[0]
            return {"location": {"directory": directory}, "data": by_directory[directory]}

        service = self._native_service(snapshot)
        for name, value in {
            "config/opencode/opencode.json": '{"model":"saved/missing"}\n',
            "config/codex/models.conf": "CFG_MODEL_CONTEXT_WINDOW=1000000\n",
            "data/opencode/auth.json": '{"fixture":{"token":"private-fixture"}}\n',
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        generation = service.stat()
        environment = dict(os.environ)
        original_cwd = os.getcwd()
        for directory, expected in ((home, "native/home"), (project, "native/project")):
            with self.subTest(directory=directory), chdir(directory):
                catalog = model_catalog.read_catalogs()["opencode"]
                self.assertEqual((catalog.state, catalog.models), ("available", (expected,)))
                self.assertEqual(os.getcwd(), str(directory))
        self.assertEqual(os.getcwd(), original_cwd)
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(service.stat(), generation)
        self.assertEqual({p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*")
                          if p.is_file()}, before)
        self.assertEqual([target for target, _auth in self.requests], [
            "/api/model?" + urlencode({"location[directory]": str(directory)})
            for directory in (home, project)])
        self.assertEqual(len({auth for _target, auth in self.requests}), 1)
        self.assertTrue(self.requests[0][1].startswith("Basic "))

    def test_explicit_directory_overrides_unavailable_cwd(self):
        self._native_service({"data": []})
        # The Fleet read imports the shared runtime through its normal boundary.
        model_catalog.read_catalog("opencode")
        import opencode_runtime
        self.requests.clear()
        with mock.patch.object(os, "getcwd", side_effect=FileNotFoundError("cwd removed")):
            catalog = opencode_runtime.read_model_catalog(directory=self.root)
        self.assertEqual((catalog["state"], catalog["models"]), ("empty", ()))
        self.assertEqual(self.requests[0][0], "/api/model?" + urlencode({
            "location[directory]": str(self.root)}))

    def test_invalid_or_unavailable_directory_is_unknown_without_server_home_fallback(self):
        service = self._native_service({"data": []})
        model_catalog.read_catalog("opencode")
        import opencode_runtime
        self.requests.clear()
        loop = self.root / "loop"
        loop.symlink_to(loop)
        for directory in ("", self.root / "missing", service, loop, 0, b"/tmp", "bad\x00path"):
            with self.subTest(directory=directory):
                catalog = opencode_runtime.read_model_catalog(directory=directory)
                self.assertEqual((catalog["state"], catalog["models"]), ("unknown", ()))
        for error in (FileNotFoundError("cwd removed"), PermissionError("cwd inaccessible")):
            with mock.patch.object(os, "getcwd", side_effect=error):
                catalog = model_catalog.read_catalog("opencode")
            self.assertEqual((catalog.state, catalog.models), ("unknown", ()))
        self.assertEqual(self.requests, [])

    def test_v2_response_size_bound_stays_unknown_without_cache_fallback(self):
        self._native_service(None, raw=b" " * (16 * 1024 * 1024 + 1))
        catalog = model_catalog.read_catalog("opencode")
        self.assertEqual((catalog.state, catalog.models), ("unknown", ()))
        self.assertEqual(len(self.requests), 1)

    def test_v2_http_failure_and_timeout_stay_unknown(self):
        self._native_service({"data": []}, status=503)
        self.assertEqual(model_catalog.read_catalog("opencode").state, "unknown")
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError) as request:
            self.assertEqual(model_catalog.read_catalog("opencode").state, "unknown")
        self.assertEqual(request.call_args.kwargs["timeout"], 2.0)
        self.assertEqual(len(self.requests), 1)

    def test_v2_oversized_service_record_stays_unknown_without_request(self):
        service = self._native_service({"data": []})
        service.write_bytes(b" " * (64 * 1024 + 1))
        catalog = model_catalog.read_catalog("opencode")
        self.assertEqual((catalog.state, catalog.models), ("unknown", ()))
        self.assertEqual(self.requests, [])

    def test_current_catalog_does_not_depend_on_service_pid_visibility(self):
        self._native_service({"data": [{"providerID": "native", "id": "model",
                                      "enabled": True, "status": "active"}]}, pid=999_999_999)
        catalog = model_catalog.read_catalog("opencode")
        self.assertEqual((catalog.state, catalog.models), ("available", ("native/model",)))

    def test_known_v2_without_service_does_not_reuse_old_cache(self):
        cache = self.root / "cache/opencode/models.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"models": ["old-provider/stale-model"]}))
        with mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "2.0.32"}):
            catalog = model_catalog.read_catalog("opencode")
        self.assertEqual((catalog.state, catalog.models), ("unknown", ()))

    def test_v2_empty_snapshot_is_empty_and_invalid_snapshot_is_unknown(self):
        path = self._native_service({"data": []})
        self.assertEqual(model_catalog.read_catalog("opencode").state, "empty")
        record = json.loads(path.read_text())
        record["url"] = "https://provider.invalid"
        path.write_text(json.dumps(record))
        self.assertEqual(model_catalog.read_catalog("opencode").state, "unknown")
        self.assertEqual(len(self.requests), 1)

    def test_v2_service_replacement_during_read_does_not_publish_a_stale_catalog(self):
        self._native_service({"data": [{"providerID": "native", "id": "model",
                                      "enabled": True, "status": "active"}]}, change_record=True)
        catalog = model_catalog.read_catalog("opencode")
        self.assertEqual((catalog.state, catalog.models), ("unknown", ()))

    def test_malformed_native_model_status_stays_unknown_without_legacy_cache_fallback(self):
        self._native_service({"data": [{"providerID": "native", "id": "model",
                                      "enabled": True, "status": []}]})
        catalog = model_catalog.read_catalog("opencode")
        self.assertEqual((catalog.state, catalog.models), ("unknown", ()))

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
                   "XDG_DATA_HOME": data, "XDG_CONFIG_HOME": os.path.join(root, "empty-config"),
                   "OPENCODE_CONFIG": ""}
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertEqual(model_catalog.read_catalog("claude").models, ("claude-cache-model",))
                catalog = model_catalog.read_catalog("opencode")
                with mock.patch.object(model_catalog, "_candidates", return_value=(codex_path,)):
                    codex = model_catalog.read_catalog("codex")
            self.assertEqual(catalog.models, ("ready/allowed",))
            self.assertEqual(catalog.source, os.path.join(cache, "opencode", "models.json"))
            self.assertEqual(codex.models, ("listed",))

    def _claude_cache(self, name, document, mtime=None):
        directory = self.root / "claude" / "cache" / "model-catalog"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
        if mtime is not None:
            os.utime(path, ns=(mtime, mtime))
        return path

    @staticmethod
    def _claude_v2(records, aliases=None):
        """The runtime's current cache: records nest under catalog.config beside alias targets."""
        return {"version": 2, "fetchedAt": 1, "staleAt": 2, "catalog": {
            "surface": "fixture",
            "config": {"id": "fixture", "models": [
                {"id": model_id, "name": model_id, "section": section, "runtime": {"family": family}}
                for model_id, family, section in records],
                "provider_alias_targets": aliases or {},
                "settings_vocabulary": {"effort_level": {"high": 3}}},
            "state": {"id": "fixture", "model": "selected-elsewhere", "selection_source": "fixture"}}}

    def _models_config(self):
        from tools.fleet.explorer import focus

        class ModelConfig:
            @staticmethod
            def resolve_config(adapter):
                if adapter != "claude":
                    raise LookupError(adapter)
                return {"CFG_MODEL_PROFILE_DEEP": "opus:xhigh", "CFG_TIER_OPUS_MODEL": "opus",
                        "CFG_MODEL_PROFILE_BALANCED": "sonnet:high", "CFG_TIER_SONNET_MODEL": "sonnet"}, {}

        with mock.patch.object(focus, "utility", return_value=ModelConfig):
            return focus.read_model_config()

    def test_current_claude_cache_feeds_models_choices_with_its_aliases(self):
        self._claude_cache("cache-cc.json", self._claude_v2(
            [("claude-opus-5-5", "opus", "main"), ("claude-sonnet-5-5", "sonnet", "main"),
             ("claude-opus-4-6", "opus", "overflow")],
            {"opus": {"default": "claude-opus-5-5", "per_provider": {"gateway": "claude-opus-4-6"}},
             "sonnet": {"default": "claude-sonnet-5-5"},
             "unlisted": {"default": "not-in-this-catalog"}}))
        catalog = model_catalog.read_catalog("claude")
        self.assertEqual((catalog.state, catalog.models), ("available", (
            "opus", "sonnet", "claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-6")))
        self.assertTrue(catalog.source.endswith("cache-cc.json"))

        config = self._models_config()
        self.assertEqual(config["catalog_sources"]["claude"]["state"], "available")
        self.assertEqual(config["model_choices"]["claude"], list(catalog.models))
        # the saved alias stays the profile value; the catalog never rewrites it to an id
        self.assertEqual(config["profile_map"]["claude"]["deep"], ["opus", "xhigh"])

        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.view_model import ExplorerViewModel, load_fixture
        values = load_fixture(Path(__file__).parent / "fixtures" / "explorer" / "view_model.json").to_mapping()
        values["models"] = config
        state = NavState(view="MODELS", title="MODELS", folds=(("ui:sel", "deep/claude"),))
        frame = draw(ExplorerViewModel.from_mapping(values), state, 160, 70, beat=1)
        self.assertNotIn("not in catalog", "\n".join(frame.text_rows()))
        picks = [hit.action[2] for hit in frame.hits if hit.action[:2] == ("ui", "model")]
        self.assertEqual(picks, list(catalog.models))

    def test_claude_reads_only_the_most_recently_written_cache(self):
        old = self._claude_v2([("old-model", "fixture", "main")])
        new = self._claude_v2([("new-model", "fixture", "main")])
        self._claude_cache("a-account.json", old, mtime=100_000_000_000)
        newest = self._claude_cache("z-account.json", new, mtime=200_000_000_000)
        catalog = model_catalog.read_catalog("claude")
        self.assertEqual((catalog.state, catalog.models, catalog.source),
                         ("available", ("new-model",), str(newest)))
        # equal write times fall back to the file name, so the choice stays stable
        self._claude_cache("z-account.json", new, mtime=100_000_000_000)
        self.assertEqual(model_catalog.read_catalog("claude").models, ("old-model",))
        # an unreadable newest snapshot is unknown; another account's older file never stands in
        for broken in ("{", {"version": 3, "catalog": {"items": [{"id": "future-model"}]}}):
            self._claude_cache("z-account.json", broken, mtime=300_000_000_000)
            catalog = model_catalog.read_catalog("claude")
            self.assertEqual((catalog.state, catalog.models, catalog.source), ("unknown", (), None))

    def test_claude_empty_catalog_is_empty_but_unrecognized_cache_keeps_profile_choices(self):
        empty_shapes = (self._claude_v2([]), {"models": []})
        unknown_shapes = (
            {"version": 3, "catalog": {"items": [{"id": "future-model"}]}},
            {"version": 2, "catalog": {"config": {"models": {"claude-x": {}}}}},
            {"version": 2, "catalog": {"config": {"models": [{"name": "Display only"}]}}},
            {"models": [{}]},
            42,
        )
        for document in empty_shapes + unknown_shapes:
            with self.subTest(document=document):
                self._claude_cache("catalog.json", document)
                catalog = model_catalog.read_catalog("claude")
                config = self._models_config()
                if document in empty_shapes:
                    self.assertEqual((catalog.state, catalog.models), ("empty", ()))
                    self.assertEqual(config["model_choices"]["claude"], [])
                else:
                    self.assertEqual((catalog.state, catalog.models), ("unknown", ()))
                    self.assertEqual(config["model_choices"]["claude"], ["opus", "sonnet"])

    def test_opencode_keeps_provider_identity_and_uses_local_connection_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.abspath(root)
            cache = os.path.join(root, "cache", "opencode")
            data = os.path.join(root, "data", "opencode")
            config = os.path.join(root, "config", "opencode")
            os.makedirs(cache)
            os.makedirs(data)
            os.makedirs(config)
            cache_path = os.path.join(cache, "models.json")
            with open(cache_path, "w", encoding="utf-8") as stream:
                json.dump({
                    "provider-auth": {"models": {
                        "shared": {"id": "shared"}, "upstream/model": {"id": "upstream/model"}}},
                    "provider-env": {"env": ["HEARTING_CATALOG_TEST_KEY"],
                                     "models": {"shared": {"name": "display only"}}},
                    "provider-configured": {"models": {"local": {"id": "local"}}},
                    "provider-unconnected": {"env": ["HEARTING_CATALOG_MISSING_KEY"],
                                             "models": {"hidden": {"id": "hidden"}}},
                    "provider-empty-env": {"env": ["HEARTING_CATALOG_EMPTY_KEY"],
                                           "models": {"also-hidden": {"id": "also-hidden"}}},
                }, stream)
            with open(os.path.join(data, "auth.json"), "w", encoding="utf-8") as stream:
                json.dump({"provider-auth": {"token": "never-output-secret"}}, stream)
            with open(os.path.join(config, "opencode.json"), "w", encoding="utf-8") as stream:
                json.dump({"provider": {"provider-configured": {
                    "options": {"baseURL": "http://local"}}}}, stream)
            env = {"XDG_CACHE_HOME": os.path.join(root, "cache"),
                   "XDG_DATA_HOME": os.path.join(root, "data"),
                   "XDG_CONFIG_HOME": os.path.join(root, "config"),
                   "OPENCODE_CONFIG": "", "HEARTING_CATALOG_TEST_KEY": "fixture-credential",
                   "HEARTING_CATALOG_MISSING_KEY": "", "HEARTING_CATALOG_EMPTY_KEY": "  "}
            with mock.patch.dict(os.environ, env, clear=False):
                catalog = model_catalog.read_catalog("opencode")
            self.assertEqual(catalog.models, (
                "provider-auth/shared", "provider-auth/upstream/model",
                "provider-env/shared", "provider-configured/local",
            ))
            self.assertNotIn("never-output-secret", repr(catalog))
            self.assertNotIn("fixture-credential", repr(catalog))

    def test_opencode_empty_cache_is_empty_and_malformed_cache_stays_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "models.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({}, stream)
            with mock.patch.object(model_catalog, "_candidates", return_value=(path,)):
                empty = model_catalog.read_catalog("opencode")
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write("{")
                malformed = model_catalog.read_catalog("opencode")
        self.assertEqual((empty.state, empty.models), ("empty", ()))
        self.assertEqual((malformed.state, malformed.models), ("unknown", ()))

    def test_opencode_config_directory_and_inline_config_are_connection_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            config_dir = os.path.join(root, "runtime-config")
            os.makedirs(config_dir)
            config_path = os.path.join(root, "override.json")
            with open(os.path.join(config_dir, "opencode.json"), "w", encoding="utf-8") as stream:
                json.dump({"provider": {"directory-provider": {}}}, stream)
            with open(config_path, "w", encoding="utf-8") as stream:
                json.dump({"provider": {"file-provider": {}}}, stream)
            env = {
                "OPENCODE_CONFIG": "",
                "OPENCODE_CONFIG_DIR": config_dir,
                "OPENCODE_CONFIG_CONTENT": json.dumps({"provider": {"inline-provider": {
                    "options": {"apiKey": "never-output-secret"}}}}),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                configured = model_catalog._opencode_configured_providers()
            self.assertEqual(configured, {"directory-provider", "inline-provider"})
            self.assertNotIn("never-output-secret", repr(configured))
            with mock.patch.dict(os.environ, {**env, "OPENCODE_CONFIG": config_path}, clear=False):
                overridden = model_catalog._opencode_configured_providers()
            self.assertEqual(overridden, {"file-provider", "inline-provider"})


if __name__ == "__main__":
    unittest.main()
