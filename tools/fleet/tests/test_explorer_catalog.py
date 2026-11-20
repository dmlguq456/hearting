"""Catalog reads use the installed runtime and never invoke model providers."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from contextlib import chdir
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
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

    def test_v2_variant_rows_reach_the_catalog_and_model_config(self):
        self._native_service({"location": {"directory": str(self.root)}, "data": [
            {"providerID": "opencode-go", "id": "argon", "enabled": True, "status": "active",
             "variants": [{"id": "low", "settings": {}}, {"id": "high"}]},
            {"providerID": "opencode-go", "id": "glm", "enabled": True, "status": "active",
             "variants": ["minimal", {"id": "none"}]},
            {"providerID": "opencode-go", "id": "missing", "enabled": True, "status": "active"},
            {"providerID": "opencode-go", "id": "empty", "enabled": True, "status": "active",
             "variants": []},
            {"providerID": "opencode-go", "id": "broken", "enabled": True, "status": "active",
             "variants": [{"name": "high"}]},
        ]})
        catalog = model_catalog.read_catalog("opencode")
        self.assertEqual(catalog.models, (
            "opencode-go/argon", "opencode-go/glm", "opencode-go/missing",
            "opencode-go/empty", "opencode-go/broken"))
        self.assertEqual(catalog.variants, {
            "opencode-go/argon": ("low", "high"),
            "opencode-go/glm": ("minimal", "none"),
        })

        from tools.fleet.explorer import focus

        class ModelConfig:
            @staticmethod
            def resolve_config(adapter):
                if adapter != "opencode":
                    raise LookupError(adapter)
                return {"CFG_MODEL_PROFILE_BALANCED": "balanced:high",
                        "CFG_TIER_BALANCED_MODEL": "opencode-go/argon",
                        "CFG_TIER_BALANCED_VARIANT": "high"}, {}

        with mock.patch.object(focus, "utility", return_value=ModelConfig):
            config = focus.read_model_config()
        self.assertEqual(config["catalog_sources"]["opencode"]["variants"], catalog.variants)
        self.assertEqual(config["efforts"], ["low", "medium", "high", "xhigh", "max"])

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


    # --- chosen-shape rows for the model-follow rule: vendor patterns live in each reader ---

    def _codex_cache(self, models, client_version="0.163.0"):
        path = self.root / "codex" / "models_cache.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"client_version": client_version, "models": models}))

    def test_codex_rows_carry_family_version_visibility_mark_levels_and_upgrade(self):
        self._codex_cache([
            {"slug": "gpt-6.1-sol", "visibility": "list", "supported_in_api": True,
             "description": "Previous generation coding model",
             "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}],
             "upgrade": {"model": "gpt-6.2-sol"}},
            {"slug": "gpt-6.2-sol", "visibility": "list", "description": "Newest",
             "supported_reasoning_levels": ["low", "high"]},
            {"slug": "gpt-6-luna", "visibility": "hide", "description": "Legacy small model"},
            {"slug": "gpt-6.1-terra", "visibility": "list", "supported_in_api": False},
            {"slug": "gpt-6.2-sol-codex", "visibility": "list"},
            {"slug": "o9-mini", "visibility": "list"}])
        catalog = model_catalog.read_catalog("codex")
        self.assertEqual(catalog.client_version, "0.163.0")
        rows = {row["id"]: row for row in catalog.rows}
        self.assertEqual((rows["gpt-6.1-sol"]["family"], rows["gpt-6.1-sol"]["version"]), ("sol", (6, 1)))
        self.assertEqual((rows["gpt-6.2-sol"]["family"], rows["gpt-6.2-sol"]["version"]), ("sol", (6, 2)))
        self.assertEqual(rows["gpt-6.1-sol"]["efforts"], ("high", "xhigh"))
        self.assertEqual(rows["gpt-6.2-sol"]["efforts"], ("low", "high"))
        self.assertEqual(rows["gpt-6.1-sol"]["upgrade"], "gpt-6.2-sol")
        self.assertTrue(rows["gpt-6.1-sol"]["deprecated"])
        self.assertFalse(rows["gpt-6.2-sol"]["deprecated"])
        self.assertEqual((rows["gpt-6-luna"]["visible"], rows["gpt-6-luna"]["deprecated"],
                          rows["gpt-6-luna"]["version"]), (False, True, (6,)))
        self.assertFalse(rows["gpt-6.1-terra"]["visible"])
        # names off the gpt-<version>-<name> pattern get no family and are never followed
        for odd in ("gpt-6.2-sol-codex", "o9-mini"):
            self.assertEqual((rows[odd]["family"], rows[odd]["version"]), (None, None))

    def test_claude_rows_carry_family_version_section_levels_and_cli_floor(self):
        document = self._claude_v2([("claude-opus-5-5", "opus", "main"),
                                    ("claude-haiku-5-5", "haiku", "main"),
                                    ("claude-opus-4-6", "opus", "overflow"),
                                    ("claude-sonnet-4-5-20250929", "sonnet", "main"),
                                    ("odd-name", "opus", "main")])
        records = document["catalog"]["config"]["models"]
        records[0]["effort_levels"] = ["low", {"id": "high"}, "max"]
        records[0]["min_claude_code_version"] = "2.1.280"
        records[1]["runtime"]["min_claude_code_version"] = "2.1.293"
        self._claude_cache("one.json", document)
        catalog = model_catalog.read_catalog("claude")
        rows = {row["id"]: row for row in catalog.rows}
        self.assertEqual((rows["claude-opus-5-5"]["family"], rows["claude-opus-5-5"]["version"],
                          rows["claude-opus-5-5"]["visible"]), ("opus", (5, 5), True))
        self.assertEqual(rows["claude-opus-5-5"]["efforts"], ("low", "high", "max"))
        self.assertEqual(rows["claude-opus-5-5"]["min_cli"], "2.1.280")
        self.assertEqual(rows["claude-haiku-5-5"]["min_cli"], "2.1.293")
        self.assertEqual((rows["claude-opus-4-6"]["visible"], rows["claude-opus-4-6"]["deprecated"]),
                         (False, True))
        self.assertEqual(rows["claude-sonnet-4-5-20250929"]["version"], (4, 5))
        self.assertEqual((rows["odd-name"]["family"], rows["odd-name"]["version"]), (None, None))
        # the aliases the runtime accepts are choices, not rows: they follow the runtime themselves
        self.assertNotIn("opus", rows)

    def test_opencode_cache_rows_group_by_provider_family_and_name_stem(self):
        cache = self.root / "cache/opencode/models.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"ready": {"models": {
            "muse-spark-1.3-contributor": {"family": "muse", "status": "active",
                                           "cost": {"input": 0.1, "output": 0.4}},
            "muse-spark-1.2-contributor": {"family": "muse", "status": "deprecated", "cost": {"input": 0.1}},
            "deepseek-v4.1-flash": {"family": "deepseek"}, "unversioned": {"family": "muse"},
            "no-family-2": {}}}, "unused": {"models": {"hidden-3": {"family": "x"}}}}))
        auth = self.root / "data/opencode/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"ready": {}}))
        rows = {row["id"]: row for row in model_catalog.read_catalog("opencode").rows}
        self.assertEqual(set(rows), {"ready/muse-spark-1.3-contributor", "ready/muse-spark-1.2-contributor",
                                     "ready/deepseek-v4.1-flash", "ready/unversioned", "ready/no-family-2"})
        new, old = rows["ready/muse-spark-1.3-contributor"], rows["ready/muse-spark-1.2-contributor"]
        self.assertEqual(new["family"], old["family"])  # the same product line, only the version differs
        self.assertEqual((new["version"], old["version"]), ((1, 3), (1, 2)))
        self.assertEqual((new["cost"], old["deprecated"], old["visible"]), (0.1, True, False))
        self.assertNotEqual(rows["ready/deepseek-v4.1-flash"]["family"], new["family"])
        for odd in ("ready/unversioned", "ready/no-family-2"):
            self.assertEqual((rows[odd]["family"], rows[odd]["version"]), (None, None))

    def test_opencode_service_rows_carry_family_status_price_and_variants(self):
        self._native_service({"location": {"directory": str(self.root)}, "data": [
            {"providerID": "ready", "id": "muse-spark-1.3-contributor", "enabled": True,
             "status": "active", "family": "muse", "cost": {"input": 0.1},
             "variants": [{"id": "low"}, {"id": "high"}]},
            {"providerID": "ready", "id": "muse-spark-1.2-contributor", "enabled": True,
             "status": "deprecated", "family": "muse", "cost": 0.5},
            {"providerID": "ready", "id": "disabled-9", "enabled": False, "status": "active", "family": "x"},
        ]})
        catalog = model_catalog.read_catalog("opencode")
        rows = {row["id"]: row for row in catalog.rows}
        self.assertEqual(set(rows), {"ready/muse-spark-1.3-contributor", "ready/muse-spark-1.2-contributor"})
        self.assertEqual(rows["ready/muse-spark-1.3-contributor"]["efforts"], ("low", "high"))
        self.assertEqual((rows["ready/muse-spark-1.3-contributor"]["cost"], rows["ready/muse-spark-1.2-contributor"]["cost"]),
                         (0.1, 0.5))
        self.assertEqual((rows["ready/muse-spark-1.2-contributor"]["deprecated"],
                          rows["ready/muse-spark-1.2-contributor"]["visible"]), (True, False))
        self.assertNotIn("fixture-private-password", repr(catalog))

    def test_rows_are_empty_when_the_cache_names_models_only(self):
        self._claude_cache("flat.json", {"models": [{"id": "claude-opus-5-5"}]})
        self.assertEqual(model_catalog.read_catalog("claude").rows, ())
        self._codex_cache([])
        self.assertEqual(model_catalog.read_catalog("codex").rows, ())


    # --- harness files -> rows -> model-follow lines, on a fixture home, nothing written ---

    SHIPPED = Path(__file__).resolve().parents[3] / "adapters"

    def _user_conf(self, harness, *replacements):
        base = {"claude": self.root / "claude", "codex": self.root / "codex",
                "opencode": self.root / "config" / "opencode"}[harness]
        text = (self.SHIPPED / harness / "config" / "models.conf").read_text()
        for old, new in replacements:
            self.assertIn(old, text)
            text = text.replace(old, new)
        path = base / "agent-config" / "models.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def _follow(self, harness, installed=(None, None)):
        utilities = str(Path(__file__).resolve().parents[3] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        import model_follow
        tree = sorted(p for p in self.root.rglob("*"))
        sizes = {p: p.stat().st_size for p in tree if p.is_file()}
        # A worker's masked home pins every harness's model config to one directory;
        # this fixture is an ordinary user home, where each harness has its own.
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "claude")}):
            os.environ.pop("HEARTING_MODEL_CONFIG_HOME", None)
            result = model_follow.survey_harness(harness, probe=lambda _h: installed)
        self.assertEqual(sorted(self.root.rglob("*")), tree)  # nothing created, not even a backup
        self.assertEqual({p: p.stat().st_size for p in tree if p.is_file()}, sizes)
        return result, model_follow.lines([result])

    def test_codex_files_give_newer_proposal_and_cli_lines(self):
        path = self._user_conf("codex",
                               ("CFG_MODEL_PROFILE_DEEP=deep:xhigh", "CFG_MODEL_PROFILE_DEEP=model/gpt-6-astra:high"))
        before = path.read_bytes()
        self._codex_cache([
            {"slug": "gpt-6-astra", "visibility": "list", "upgrade": {"model": "gpt-6.2-sol"},
             "description": "Previous generation"},
            {"slug": "gpt-6.1-sol", "visibility": "list", "description": "Previous generation"},
            {"slug": "gpt-6.2-sol", "visibility": "list", "description": "Newest",
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
            {"slug": "gpt-6-luna", "visibility": "list", "description": "Previous generation"}])
        result, lines = self._follow("codex", installed=((0, 163, 0), "/opt/bin/codex"))
        self.assertEqual(lines, [
            "model follow: proposal codex tier/top gpt-6-astra -> gpt-6.2-sol (other family)"
            " · hearting model set codex tier/top gpt-6.2-sol@high",
            "model follow: newer codex tier/deep gpt-6.1-sol -> gpt-6.2-sol (high, was xhigh)"
            " · hearting model set codex tier/deep gpt-6.2-sol@high",
            "model follow: proposal codex profile/deep gpt-6-astra -> gpt-6.2-sol (other family)"
            " · hearting model set codex profile/deep gpt-6.2-sol@high",
            "model follow: codex CLI 0.163.0 lists gpt-6-luna as previous generation with no"
            " successor · update the Codex CLI yourself"])
        cascade = {e["target"]: e["companions"] for e in result["entries"]}["tier/deep"]
        self.assertEqual([c["key"] for c in cascade], ["CFG_TIER_DEEP_FAILOVER_CASCADE"])
        import model_follow
        final = model_follow.final_lines([e for e in result["entries"] if e["target"] == "tier/deep"])
        self.assertTrue(final["CFG_TIER_DEEP_FAILOVER_CASCADE"]["to"].startswith("gpt-6.2-sol:high "))
        self.assertEqual(path.read_bytes(), before)

    def test_claude_files_give_newer_and_cli_lines_and_leave_aliases_alone(self):
        self._user_conf("claude",
                        ("CFG_TIER_LIGHT_MODEL=sonnet", "CFG_TIER_LIGHT_MODEL=claude-sonnet-4-5"),
                        ("CFG_MODEL_PROFILE_DEEP=deep:xhigh", "CFG_MODEL_PROFILE_DEEP=model/claude-opus-4-6:xhigh"))
        document = self._claude_v2(
            [("claude-sonnet-4-5", "sonnet", "main"), ("claude-sonnet-5-5", "sonnet", "main"),
             ("claude-opus-4-6", "opus", "main"), ("claude-opus-5-5", "opus", "main"),
             ("claude-fable-9-0", "fable", "main")],
            {"opus": {"default": "claude-opus-5-5"}})
        records = {r["id"]: r for r in document["catalog"]["config"]["models"]}
        records["claude-opus-5-5"]["effort_levels"] = ["low", "medium", "high"]
        records["claude-fable-9-0"]["min_claude_code_version"] = "9.0.0"
        self._claude_cache("one.json", document)
        result, lines = self._follow("claude", installed=((2, 1, 5), "/h/.local/share/claude/versions/2.1.5"))
        self.assertEqual(lines, [
            "model follow: newer claude tier/light claude-sonnet-4-5 -> claude-sonnet-5-5 (medium)"
            " · hearting model set claude tier/light claude-sonnet-5-5@medium",
            "model follow: newer claude profile/deep claude-opus-4-6 -> claude-opus-5-5 (high, was xhigh)"
            " · hearting model set claude profile/deep claude-opus-5-5@high",
            "model follow: claude CLI 2.1.5 is older than claude-fable-9-0 (needs 9.0.0) · claude update"])
        # the alias cells name no listed id: nothing to do for them
        aliases = [e for e in result["entries"] if e["current"]["model"] in ("opus", "sonnet", "fable")]
        self.assertTrue(aliases and {e["kind"] for e in aliases} == {"none"})

    def test_opencode_files_give_newer_and_price_proposal_lines(self):
        self._user_conf("opencode",
                        ("CFG_MODEL_PROFILE_DEEP=balanced-deep:xhigh",
                         "CFG_MODEL_PROFILE_DEEP=model/opencode-go/muse-spark-1.2-contributor:high"))
        cache = self.root / "cache/opencode/models.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"opencode-go": {"models": {
            "muse-spark-1.2-contributor": {"family": "muse", "cost": {"input": 0.1}},
            "muse-spark-1.3-contributor": {"family": "muse", "cost": {"input": 0.1}},
            "deepseek-v4.1-flash": {"family": "deepseek", "cost": {"input": 0.1}},
            "deepseek-v4.2-flash": {"family": "deepseek", "cost": {"input": 0.5}}}}}))
        auth = self.root / "data/opencode/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"opencode-go": {}}))
        result, lines = self._follow("opencode", installed=((2, 0, 32), None))
        self.assertEqual([line.split(" · ")[0] for line in lines], [
            "model follow: proposal opencode tier/light opencode-go/deepseek-v4.1-flash -> "
            "opencode-go/deepseek-v4.2-flash (price x5.0)",
            "model follow: proposal opencode tier/mini opencode-go/deepseek-v4.1-flash -> "
            "opencode-go/deepseek-v4.2-flash (price x5.0)",
            "model follow: newer opencode profile/deep opencode-go/muse-spark-1.2-contributor -> "
            "opencode-go/muse-spark-1.3-contributor (high)"])
        self.assertTrue(lines[2].endswith(" · hearting model set opencode profile/deep "
                                          "opencode-go/muse-spark-1.3-contributor@high"))
        self.assertIsNone(result["cli"])  # OpenCode never gets the CLI note


ROOT = Path(__file__).resolve().parents[3]


def _fixture_env(root):
    return {"HOME": str(root), "PATH": "", "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"), "CODEX_HOME": str(root / "codex"),
            "HEARTING_MODEL_CONFIG_HOME": str(root / "claude"), "OPENCODE_RUNTIME_VERSION": "1.19.0"}


class ListPlaceTests(unittest.TestCase):
    """A list's place that is not a regular file is skipped at once, never waited on."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="fleet-place-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = _fixture_env(self.root)

    def places(self):
        return {"claude": self.root / "claude/cache/model-catalog/one.json",
                "codex": self.root / "codex/models_cache.json",
                "opencode": self.root / "cache/opencode/models.json"}

    def read_in_child(self, harness):
        """The catalog state a fresh process reads, with a time limit (a wait would be a failure)."""
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "from tools.fleet.explorer import model_catalog;"
                "print(model_catalog.read_catalog(sys.argv[2]).state)")
        done = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT), harness],
                              env=self.env, capture_output=True, text=True, timeout=10)
        return done.stdout.strip()

    def test_a_pipe_in_each_harnesss_list_place_is_unknown_and_returns_at_once(self):
        for harness, place in self.places().items():
            with self.subTest(harness=harness):
                place.parent.mkdir(parents=True, exist_ok=True)
                os.mkfifo(place)
                started = time.monotonic()
                self.assertEqual(self.read_in_child(harness), "unknown")
                self.assertLess(time.monotonic() - started, 5)

    def test_a_socket_or_a_directory_is_skipped_too(self):
        place = self.places()["codex"]
        place.parent.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(place))
            self.assertEqual(self.read_in_child("codex"), "unknown")
        place.unlink()
        place.mkdir()
        self.assertEqual(self.read_in_child("codex"), "unknown")

    def test_a_regular_file_and_a_link_to_one_read_as_before(self):
        document = {"models": [{"slug": "gpt-6-elm", "visibility": "list"}]}
        place = self.places()["codex"]
        place.parent.mkdir(parents=True)
        real = self.root / "real.json"
        real.write_text(json.dumps(document))
        place.symlink_to(real)
        self.assertEqual(model_catalog._json(place), document)
        self.assertEqual(self.read_in_child("codex"), "available")
        place.unlink()
        place.write_text("{broken")
        self.assertIsNone(model_catalog._json(place))
        place.write_text(json.dumps(document) + " " * (model_catalog._MAX_BYTES))
        self.assertIsNone(model_catalog._json(place))
        self.assertIsNone(model_catalog._json(self.root / "absent.json"))

    def test_a_link_to_a_pipe_is_skipped(self):
        place = self.places()["codex"]
        place.parent.mkdir(parents=True)
        pipe = self.root / "pipe"
        os.mkfifo(pipe)
        place.symlink_to(pipe)
        self.assertEqual(self.read_in_child("codex"), "unknown")

    def test_what_is_opened_is_what_is_judged_even_if_the_place_changed_since(self):
        place = self.root / "list.json"
        place.write_text("{}")  # a regular file when anyone looked
        read, write = os.pipe()  # but a pipe when it is opened
        self.addCleanup(os.close, write)
        flags = []
        real_open = os.open

        def swapped(path, flag, *args):
            flags.append(flag)
            return read if str(path) == str(place) else real_open(path, flag, *args)

        with mock.patch.object(model_catalog.os, "open", side_effect=swapped):
            self.assertIsNone(model_catalog._json(place))
        self.assertTrue(flags and flags[0] & os.O_NONBLOCK)
        with self.assertRaises(OSError):  # the descriptor was released by the reader
            os.fstat(read)


class ServiceDeadlineTests(unittest.TestCase):
    """A service that stops answering never holds the survey past a fixed total time."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="fleet-svc-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patch = mock.patch.dict(os.environ, _fixture_env(self.root))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(model_catalog, "_candidates", return_value=())
        patch.start()
        self.addCleanup(patch.stop)

    def serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.service(server.server_port)
        return server

    def service(self, port):
        path = self.root / "state/opencode/service.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": "2.0.32", "url": f"http://127.0.0.1:{port}"}))
        path.chmod(0o600)

    def timed(self, seconds=1.0):
        import opencode_runtime
        started = time.monotonic()
        with mock.patch.object(opencode_runtime, "SERVICE_READ_SECONDS", seconds):
            catalog = model_catalog.read_catalog("opencode")
        return catalog, time.monotonic() - started

    def test_a_service_that_accepts_and_never_answers_ends_in_the_total_time(self):
        held = []
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        self.service(listener.getsockname()[1])
        threading.Thread(target=lambda: held.append(listener.accept()[0]), daemon=True).start()
        catalog, elapsed = self.timed(1.0)
        self.assertEqual((catalog.state, catalog.models, catalog.rows), ("unknown", (), ()))
        self.assertLess(elapsed, 2.5)
        for conn in held:
            conn.close()

    def test_a_body_sent_a_few_bytes_at_a_time_ends_in_the_total_time(self):
        body = json.dumps({"data": []}).encode()

        class Slow(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.4)
                except OSError:
                    pass

            def log_message(self, *_args):
                pass

        self.serve(Slow)
        catalog, elapsed = self.timed(1.0)
        self.assertEqual((catalog.state, catalog.models), ("unknown", ()))
        self.assertLess(elapsed, 2.5)

    def test_headers_sent_a_few_bytes_at_a_time_end_in_the_total_time(self):
        class SlowHeaders(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}":
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.2)
                except OSError:
                    pass

            def log_message(self, *_args):
                pass

        self.serve(SlowHeaders)
        catalog, elapsed = self.timed(1.0)
        self.assertEqual((catalog.state, catalog.models), ("unknown", ()))
        self.assertLess(elapsed, 2.5)

    def test_a_prompt_answer_still_reads_and_the_deadline_is_a_few_seconds(self):
        import opencode_runtime

        class Prompt(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"data": [{"providerID": "p", "id": "m-1", "enabled": True,
                                             "status": "active"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.serve(Prompt)
        catalog, _elapsed = self.timed(opencode_runtime.SERVICE_READ_SECONDS)
        self.assertEqual((catalog.state, catalog.models), ("available", ("p/m-1",)))
        self.assertLessEqual(opencode_runtime.SERVICE_READ_SECONDS, 5)

    def test_no_way_exists_to_start_the_service_from_a_read(self):
        starts = [mock.patch.object(subprocess, name) for name in ("Popen", "run", "call", "check_call",
                                                                  "check_output")]
        starts += [mock.patch.object(os, name) for name in ("system", "fork", "posix_spawn", "posix_spawnp",
                                                           "execv", "execve", "spawnv")]
        mocks = []
        for patch in starts:
            mocks.append(patch.start())
            self.addCleanup(patch.stop)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()  # nothing listens there
        self.service(port)
        self.assertEqual(self.timed(1.0)[0].state, "unknown")
        (self.root / "state/opencode/service.json").unlink()
        self.assertEqual(model_catalog.read_catalog("opencode").state, "unknown")  # no record, a v1 runtime, no cache
        self.assertEqual([m.call_count for m in mocks], [0] * len(mocks))


if __name__ == "__main__":
    unittest.main()
