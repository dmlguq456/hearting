import hashlib, importlib.util, io, json, os, subprocess, sys, tarfile, tempfile, unittest, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("provenance", ROOT / "tools/lab-config-provenance.py")
M = importlib.util.module_from_spec(SPEC); assert SPEC and SPEC.loader; SPEC.loader.exec_module(M)
CLEAN_ENV = {
    k: v for k, v in os.environ.items()
    if k != "AGENT_ARTIFACT_ROOT"
    and not k.startswith(("AGENT_ROUTE_", "AGENT_REVIEW_", "AGENT_DISPATCH_", "AGENT_ARTIFACT_"))
}
TOOL = str(ROOT / "tools/lab-config-provenance.py")

def run_cli(*args, env=None, cwd=None):
    return subprocess.run(["python3", TOOL, *args], env=env or CLEAN_ENV, cwd=cwd,
                          capture_output=True, text=True)

def seal_cli(repo, config, slug, artifact_root, run_id=None, config_ref=None, env=None):
    args = ["seal", "--repo", str(repo), "--config", config, "--slug", slug, "--artifact-root", str(artifact_root)]
    if run_id is not None: args += ["--run-id", run_id]
    if config_ref is not None: args += ["--config-ref", config_ref]
    return run_cli(*args, env=env)

def manifest_dir(artifact_root, slug):
    return Path(artifact_root) / "experiments" / slug / "_internal" / "configs"


class TestProvenance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"; self.repo.mkdir()
        (self.repo / "configs").mkdir(); (self.repo / "configs_exp/demo").mkdir(parents=True); (self.repo / "configs_legacy").mkdir()
        (self.repo / "configs/a.yaml").write_text("adopted")
        (self.repo / "configs_exp/demo/b.yaml").write_text("experiment")
        (self.repo / "configs_legacy/c.yaml").write_text("legacy")

    def _dir(self):
        return Path(tempfile.mkdtemp(dir=self.temp.name))

    def _custom_repo(self, roots, layout_name="bcresnet", extra_top=None):
        repo = self._dir()
        payload = {"schema_version": 1, "layout": layout_name, "roots": roots}
        if extra_top: payload.update(extra_top)
        (repo / ".lab-config-layout.json").write_text(json.dumps(payload))
        for rel in roots.values() if isinstance(roots, dict) else []:
            if isinstance(rel, str) and not rel.startswith("/") and ".." not in Path(rel).parts:
                (repo / rel).mkdir(parents=True, exist_ok=True)
        return repo

    def _git_repo(self):
        repo = self._dir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
        (repo / "configs").mkdir()
        (repo / "configs/a.yaml").write_text("v1\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
        return repo

    def _borrowed_smoke_fixture(self, slug):
        spec = importlib.util.spec_from_file_location(
            "artifact_producer_fixture_for_borrowed_smoke", ROOT / "utilities/artifact_producer.test.py")
        fixture_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture_module)
        inherited = {key: value for key, value in os.environ.items()
                     if key.startswith(("AGENT_ROUTE_", "AGENT_REVIEW_", "AGENT_DISPATCH_", "AGENT_ARTIFACT_"))}
        for key in inherited:
            os.environ.pop(key, None)
        fixture = fixture_module.ProducerTestBase("runTest")
        fixture.setUp()
        try:
            runtime_home = fixture._env.get("AGENT_HOME") or str(ROOT)
            source = Path(fixture._tmp.name) / "execution-source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "fixture@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Fixture"], check=True)
            (source / "configs").mkdir()
            config = source / "configs" / "a.yaml"
            config.write_text("borrowed smoke config\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "source head"], check=True)
            source_head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()

            fixture.activate()
            dispatch_rows = []
            for harness in ("codex", "claude", "opencode"):
                dispatch_rows.append({
                    "parent_harness": harness, "parent_transport": "headless",
                    "parent_sandbox": fixture_module.R.WRAPPER_PARENT_SANDBOXES[harness][0],
                    "child_harness": harness, "launch_authority": "conductor", "status": "supported",
                    "probe_source": f"{harness}-fixture", "probe_time": "2026-07-16T00:00:00Z",
                    "failure_class": "", "checked_worktree": str(source.resolve()),
                    "failure_scope": "none", "codex_command": "ok" if harness == "codex" else "not-applicable",
                    "retry_on_isolated_worktree": 0,
                })
            route = fixture_module.R.compose_route(
                capability="autopilot-code", capability_mode="dev", shape="staged",
                graph="execute,autopilot-lab:smoke,impl-review,test,report", slug=slug,
                cwd=source, artifact_root=fixture.root, intensity="standard", unassigned=True,
                spec_read="fixture", dispatch_evidence={"tuples": dispatch_rows, "native_subagent": []},
                jobs=fixture.jobs, parent_harness="codex",
            )
            node = next(row for row in route["nodes"] if row.get("part") == "autopilot-lab:smoke")
            route_file = Path(fixture_module.L.admit_runtime_route(fixture.root, route).route_file)
            begun = fixture_module.P.begin(
                fixture.root, route_file=route_file, capability=route["capability"],
                intensity=route["effective_intensity"], require_cycle=True)
            env = dict(CLEAN_ENV)
            env.update({
                "AGENT_HOME": runtime_home,
                "AGENT_DISPATCH_JOBS": str(fixture.jobs),
                "AGENT_ARTIFACT_ROOT": str(fixture.root),
                "AGENT_ROUTE_FILE": str(route_file), "AGENT_ROUTE_ID": route["route_id"],
                "AGENT_ROUTE_NODE": node["id"], "AGENT_DISPATCH_WORKTREE": str(source),
                **begun["env"],
            })
            return {"fixture": fixture, "fixture_module": fixture_module, "inherited": inherited,
                    "source": source, "config": config, "source_head": source_head,
                    "route": route, "route_file": route_file, "node": node,
                    "begun": begun, "env": env}
        except Exception:
            self._cleanup_borrowed_smoke_fixture(fixture, inherited)
            raise

    def _cleanup_borrowed_smoke_fixture(self, fixture, inherited):
        fixture.doCleanups()
        for key in tuple(os.environ):
            if key.startswith(("AGENT_ROUTE_", "AGENT_REVIEW_", "AGENT_DISPATCH_", "AGENT_ARTIFACT_")):
                os.environ.pop(key, None)
        os.environ.update(inherited)

    def test_borrowed_smoke_part_seals_config_inside_its_scope(self):
        fixture = self._borrowed_smoke_fixture("scope89-borrowed-smoke")
        try:
            source, config = fixture["source"], fixture["config"]
            route, node, begun, env = fixture["route"], fixture["node"], fixture["begun"], fixture["env"]
            before_head = fixture["source_head"]
            before_status = subprocess.check_output(
                ["git", "-C", str(source), "status", "--porcelain=v1"], text=True)
            sealed = run_cli("seal", "--config", "configs/a.yaml", "--slug", "demo", env=env, cwd=source)
            self.assertEqual(sealed.returncode, 0, sealed.stderr)
            manifest = json.loads(sealed.stdout)
            output_dir = Path(begun["cycle_dir"]) / "artifacts"
            mapped = node["part_io"]["reviews/smoke-attestation.json"]
            part_root = mapped.removesuffix("reviews/smoke-attestation.json")
            expected_dir = output_dir / part_root / "reviews/smoke/experiments/demo/_internal/configs"
            self.assertEqual(Path(manifest["snapshot_path"]).parent, expected_dir)
            self.assertEqual(Path(manifest["source_path"]), config.resolve())
            self.assertEqual(manifest["source_commit"], before_head)
            self.assertEqual(manifest["source_git_state"], "clean")
            self.assertFalse(manifest["source_dirty"])
            manifest_path = expected_dir / f"{manifest['run_id']}.manifest.json"
            snapshot_path = Path(manifest["snapshot_path"])
            manifest_before, snapshot_before = manifest_path.read_bytes(), snapshot_path.read_bytes()
            manifest_mtime, snapshot_mtime = manifest_path.stat().st_mtime_ns, snapshot_path.stat().st_mtime_ns
            verified = run_cli("verify", "--manifest", str(manifest_path), env=env, cwd=source)
            self.assertEqual(verified.returncode, 0, verified.stderr)

            attestation = output_dir / mapped
            attest = subprocess.run([
                sys.executable, str(ROOT / "tools/smoke-attestation.py"), "attest",
                "--input", "configs/a.yaml", "--config-manifest", str(manifest_path),
                "--cwd", str(source), "--output", str(attestation), "--",
                sys.executable, "-c", "print('cpu smoke')",
            ], cwd=source, env=env, capture_output=True, text=True)
            self.assertEqual(attest.returncode, 0, attest.stderr)
            attest_verify = subprocess.run([
                sys.executable, str(ROOT / "tools/smoke-attestation.py"), "verify",
                "--attestation", str(attestation),
            ], cwd=source, env=env, capture_output=True, text=True)
            self.assertEqual(attest_verify.returncode, 0, attest_verify.stderr)

            sealed_again = run_cli("seal", "--config", "configs/a.yaml", "--slug", "demo", env=env, cwd=source)
            self.assertEqual(sealed_again.returncode, 0, sealed_again.stderr)
            self.assertEqual(manifest_before, manifest_path.read_bytes())
            self.assertEqual(snapshot_before, snapshot_path.read_bytes())
            self.assertEqual(manifest_mtime, manifest_path.stat().st_mtime_ns)
            self.assertEqual(snapshot_mtime, snapshot_path.stat().st_mtime_ns)
            self.assertEqual(before_status, subprocess.check_output(
                ["git", "-C", str(source), "status", "--porcelain=v1"], text=True))
            self.assertEqual(before_head, subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip())
            self.assertFalse((output_dir / "reviews/smoke/experiments/demo/_internal/configs").exists())
            self.assertFalse((output_dir / "_internal/parts/autopilot-lab/full-run/reviews/smoke/experiments/demo/_internal/configs").exists())
        finally:
            self._cleanup_borrowed_smoke_fixture(fixture["fixture"], fixture["inherited"])

    def test_borrowed_smoke_part_keeps_repository_and_output_guards(self):
        fixture = self._borrowed_smoke_fixture("scope89-borrowed-smoke-guards")
        try:
            source, env = fixture["source"], fixture["env"]
            outside = Path(fixture["fixture"]._tmp.name) / "outside.yaml"
            outside.write_text("outside repository\n", encoding="utf-8")
            escaped = run_cli("seal", "--config", str(outside), "--slug", "demo", env=env, cwd=source)
            self.assertEqual(escaped.returncode, 65, escaped.stderr)
            self.assertIn("path escapes repository", escaped.stderr)

            symlink = source / "configs" / "escape.yaml"
            symlink.symlink_to(outside)
            symlink_result = run_cli("seal", "--config", "configs/escape.yaml", "--slug", "demo",
                                     env=env, cwd=source)
            self.assertEqual(symlink_result.returncode, 65, symlink_result.stderr)
            self.assertIn("repository", symlink_result.stderr)
            output_dir = Path(fixture["begun"]["cycle_dir"]) / "artifacts"
            self.assertFalse((output_dir / "_internal/parts/autopilot-lab/smoke/reviews/smoke/experiments/demo/_internal/configs").exists())
        finally:
            self._cleanup_borrowed_smoke_fixture(fixture["fixture"], fixture["inherited"])

    # ---- T1/base regression (unchanged behavior) ----

    def test_resolution_is_explicit_and_traversal_safe(self):
        self.assertEqual(M.resolve_ref(self.repo, "a.yaml")["namespace"], "default")
        self.assertEqual(M.resolve_ref(self.repo, "exp:demo/b.yaml")["namespace"], "exp")
        self.assertEqual(M.resolve_ref(self.repo, "legacy:c.yaml")["namespace"], "legacy")
        with self.assertRaises(SystemExit): M.resolve_ref(self.repo, "b.yaml")
        with self.assertRaises(SystemExit): M.resolve_ref(self.repo, "c.yaml")
        with self.assertRaises(SystemExit): M.resolve_ref(self.repo, "exp:../x")

    def test_prefixed_refs_cannot_escape_their_lifecycle_root(self):
        (self.repo / "secret").mkdir()
        (self.repo / "secret/s.yaml").write_text("secret")
        escapes = [
            "exp:../configs/a.yaml",
            "legacy:../configs/a.yaml",
            "exp:../secret/s.yaml",
            "../../etc/passwd",
        ]
        for ref in escapes:
            with self.subTest(ref=ref):
                with self.assertRaises(SystemExit):
                    M.resolve_ref(self.repo, ref)

    def test_symlink_escape_from_lifecycle_root_is_rejected(self):
        outside = Path(self.temp.name) / "outside.yaml"; outside.write_text("outside")
        (self.repo / "configs_exp/demo/link.yaml").symlink_to(outside)
        with self.assertRaises(SystemExit):
            M.resolve_ref(self.repo, "exp:demo/link.yaml")

    def test_control_characters_in_ref_are_rejected(self):
        for bad in ("configs/a.yaml\n", "configs/a.yaml\r", "configs/a.yaml\x00", "exp:demo/b.yaml\n"):
            with self.subTest(ref=bad):
                with self.assertRaises(SystemExit):
                    M.resolve_ref(self.repo, bad)

    def test_run_id_binds_slug_and_hash(self):
        one = M.run_id("s1", "exp:s1/config.yaml", "a" * 64)
        self.assertNotEqual(one, M.run_id("s2", "exp:s2/config.yaml", "a" * 64))
        self.assertEqual(one, M.run_id("s1", "exp:s1/config.yaml", "a" * 64))
        self.assertNotEqual(one, M.run_id("s1", "exp:s1/config.yaml", "b" * 64))

    def test_unstructured_does_not_move_files(self):
        repo = Path(self.temp.name) / "unstructured"; repo.mkdir(); cfg = repo / "model.yaml"; cfg.write_text("x")
        before = sorted(p.relative_to(repo) for p in repo.rglob("*"))
        self.assertEqual(M.resolve_ref(repo, "./model.yaml")["layout"], "legacy/unstructured")
        with self.assertRaises(SystemExit): M.resolve_ref(repo, "model.yaml")
        after = sorted(p.relative_to(repo) for p in repo.rglob("*"))
        self.assertEqual(before, after)

    def test_checkpoint_directory_name_inference_is_rejected(self):
        repo = Path(self.temp.name) / "ckpt-repo"; repo.mkdir()
        (repo / "checkpoints/run_2026-01-01_epoch12").mkdir(parents=True)
        with self.assertRaises(SystemExit):
            M.resolve_ref(repo, "run_2026-01-01_epoch12")
        with self.assertRaises(SystemExit):
            M.resolve_ref(repo, "checkpoints/run_2026-01-01_epoch12")

    # ---- T-old-1/2/3, T-F5-2: --out/--run-id -> --artifact-root/optional --run-id ----

    def test_seal_is_idempotent_and_verify_is_fail_closed(self):
        artifact_root = self._dir()
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id_value = json.loads(result.stdout)["run_id"]
        manifest = manifest_dir(artifact_root, "demo") / f"{run_id_value}.manifest.json"
        first = manifest.read_bytes()
        result2 = seal_cli(self.repo, "a.yaml", "demo", artifact_root, run_id=run_id_value)
        self.assertEqual(result2.returncode, 0, result2.stderr)
        self.assertEqual(first, manifest.read_bytes())
        snapshot = next(manifest_dir(artifact_root, "demo").glob("*.yaml")); snapshot.write_text("tampered")
        result3 = run_cli("verify", "--manifest", str(manifest))
        self.assertEqual(result3.returncode, 65)

    def test_routed_seal_uses_declared_smoke_support_and_execution_worktree_head(self):
        spec = importlib.util.spec_from_file_location(
            "artifact_producer_fixture_for_lab_context", ROOT / "utilities/artifact_producer.test.py")
        fixture_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture_module)
        inherited = {key: value for key, value in os.environ.items()
                     if key.startswith(("AGENT_ROUTE_", "AGENT_REVIEW_", "AGENT_DISPATCH_", "AGENT_ARTIFACT_"))}
        for key in inherited:
            os.environ.pop(key, None)
        fixture = fixture_module.ProducerTestBase("runTest")
        fixture.setUp()
        runtime_home = fixture._env.get("AGENT_HOME") or str(ROOT)
        try:
            os.environ["AGENT_HOME"] = runtime_home
            source = Path(fixture._tmp.name) / "execution-source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "fixture@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Fixture"], check=True)
            (source / "configs").mkdir()
            (source / "configs/a.yaml").write_text("route config\n")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "source head"], check=True)
            source_head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()

            subprocess.run(["git", "init", "-q", str(fixture.root)], check=True)
            subprocess.run(["git", "-C", str(fixture.root), "config", "user.email", "fixture@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(fixture.root), "config", "user.name", "Fixture"], check=True)
            (fixture.root / "artifact-base.txt").write_text("artifact head\n")
            subprocess.run(["git", "-C", str(fixture.root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(fixture.root), "commit", "-qm", "artifact head"], check=True)
            artifact_head = subprocess.check_output(["git", "-C", str(fixture.root), "rev-parse", "HEAD"], text=True).strip()
            self.assertNotEqual(source_head, artifact_head)

            fixture.activate()
            dispatch = fixture_module.dispatch_evidence()
            for row in dispatch["tuples"]:
                row["checked_worktree"] = str(source)
            route = fixture_module.R.compile_route(
                "autopilot-lab", "setup", "standard", cwd=source,
                artifact_root=fixture.root, predicates=[], transport="headless",
                tracking="tracked", tracked_gate_evidence=fixture_module.gate_evidence(),
                dispatch_evidence=dispatch, slug="context-lab")
            route_binding = fixture_module.L.admit_runtime_route(fixture.root, route)
            route_file = Path(route_binding.route_file)
            begun = fixture_module.P.begin(
                fixture.root, route_file=route_file, capability="autopilot-lab",
                intensity="standard", require_cycle=True)
            node = next(row for row in route["nodes"] if row["id"] == "smoke")
            env = dict(CLEAN_ENV)
            env.update({
                "AGENT_HOME": runtime_home,
                "AGENT_DISPATCH_JOBS": str(fixture.jobs),
                "AGENT_ARTIFACT_ROOT": str(fixture.root),
                "AGENT_ROUTE_FILE": str(route_file), "AGENT_ROUTE_ID": route["route_id"],
                "AGENT_ROUTE_NODE": node["id"], "AGENT_DISPATCH_WORKTREE": str(source),
                **begun["env"],
            })
            result = run_cli("seal", "--config", "configs/a.yaml", "--slug", "demo",
                             env=env, cwd=source)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest["source_commit"], source_head)
            self.assertEqual(manifest["source_git_state"], "clean")
            self.assertFalse(manifest["source_dirty"])
            expected_dir = Path(begun["cycle_dir"]) / "artifacts/reviews/smoke/experiments/demo/_internal/configs"
            self.assertEqual(Path(manifest["snapshot_path"]).parent, expected_dir)
            verify = run_cli("verify", "--manifest", str(expected_dir / f"{manifest['run_id']}.manifest.json"), env=env)
            self.assertEqual(verify.returncode, 0, verify.stderr)
        finally:
            fixture.doCleanups()
            for key in tuple(os.environ):
                if key.startswith(("AGENT_ROUTE_", "AGENT_REVIEW_", "AGENT_DISPATCH_", "AGENT_ARTIFACT_")):
                    os.environ.pop(key, None)
            os.environ.update(inherited)

    def test_seal_rerun_does_not_touch_existing_manifest_or_snapshot_bytes(self):
        artifact_root = self._dir()
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id_value = json.loads(result.stdout)["run_id"]
        out = manifest_dir(artifact_root, "demo")
        manifest = out / f"{run_id_value}.manifest.json"; snapshot = next(out.glob("*.yaml"))
        manifest_before = manifest.read_bytes(); snapshot_before = snapshot.read_bytes()
        manifest_mtime = manifest.stat().st_mtime_ns; snapshot_mtime = snapshot.stat().st_mtime_ns
        result2 = seal_cli(self.repo, "a.yaml", "demo", artifact_root, run_id=run_id_value)
        self.assertEqual(result2.returncode, 0, result2.stderr)
        self.assertEqual(manifest_before, manifest.read_bytes())
        self.assertEqual(snapshot_before, snapshot.read_bytes())
        self.assertEqual(manifest_mtime, manifest.stat().st_mtime_ns)
        self.assertEqual(snapshot_mtime, snapshot.stat().st_mtime_ns)

    def test_promotion_recommendation_path_does_not_mutate_canonical_source(self):
        source = self.repo / "configs/a.yaml"
        before_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        before_mtime = source.stat().st_mtime_ns
        artifact_root = self._dir()
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        after_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        after_mtime = source.stat().st_mtime_ns
        self.assertEqual(before_sha, after_sha)
        self.assertEqual(before_mtime, after_mtime)

    # T-F5-2 -- a bogus AGENT_ARTIFACT_ROOT must not redirect output; every
    # path is explicit (--artifact-root is required).
    def test_tool_does_not_infer_artifact_root(self):
        artifact_root = self._dir()
        env = dict(CLEAN_ENV); env["AGENT_ARTIFACT_ROOT"] = "/nonexistent/bogus-root"
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id_value = json.loads(result.stdout)["run_id"]
        self.assertTrue((manifest_dir(artifact_root, "demo") / f"{run_id_value}.manifest.json").is_file())
        self.assertFalse(Path("/nonexistent/bogus-root").exists())

    def test_package_data(self):
        repo = self._dir(); (repo / "pyproject.toml").write_text("include = ['configs', 'configs_exp', 'configs_legacy']")
        self.assertEqual(run_cli("package-data", "--repo", str(repo)).returncode, 0)

    def test_package_data_three_variants(self):
        all_included = self._dir()
        (all_included / "pyproject.toml").write_text("include = ['configs', 'configs_exp', 'configs_legacy']")
        result = run_cli("package-data", "--repo", str(all_included))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["missing"], [])

        configs_missing = self._dir()
        (configs_missing / "pyproject.toml").write_text("include = ['configs_exp', 'configs_legacy']")
        result = run_cli("package-data", "--repo", str(configs_missing))
        self.assertEqual(result.returncode, 65)
        self.assertEqual(json.loads(result.stdout)["missing"], ["configs"])

        no_file = self._dir()
        result = run_cli("package-data", "--repo", str(no_file))
        self.assertEqual(result.returncode, 65)
        self.assertEqual(sorted(json.loads(result.stdout)["missing"]), ["configs", "configs_exp", "configs_legacy"])

    # ================= F1: declared physical roots =================

    def test_T_F1_1_declared_roots_resolve_bare_and_prefixed(self):
        repo = self._custom_repo({"default": "conf", "exp": "conf/experiments", "legacy": "conf/archive"})
        (repo / "conf/a.yaml").write_text("a")
        (repo / "conf/experiments/demo").mkdir(parents=True, exist_ok=True)
        (repo / "conf/experiments/demo/b.yaml").write_text("b")
        (repo / "conf/archive/c.yaml").write_text("c")
        self.assertEqual(M.resolve_ref(repo, "a.yaml")["path"], str((repo / "conf/a.yaml").resolve()))
        self.assertEqual(M.resolve_ref(repo, "config:a.yaml")["path"], str((repo / "conf/a.yaml").resolve()))
        self.assertEqual(M.resolve_ref(repo, "legacy:c.yaml")["path"], str((repo / "conf/archive/c.yaml").resolve()))

    def test_T_F1_2_default_names_do_not_resolve_under_custom_layout(self):
        repo = self._custom_repo({"default": "conf", "exp": "conf_exp", "legacy": "conf_legacy"})
        (repo / "conf/a.yaml").write_text("a")
        with self.assertRaises(SystemExit):
            M.resolve_ref(repo, "configs/a.yaml")

    def test_T_F1_3_declared_roots_still_traversal_safe(self):
        repo = self._custom_repo({"default": "conf", "exp": "conf_exp", "legacy": "conf_legacy"})
        (repo / "conf_exp/demo").mkdir(parents=True); (repo / "conf_exp/demo/b.yaml").write_text("b")
        (repo / "secret.yaml").write_text("s")
        with self.assertRaises(SystemExit):
            M.resolve_ref(repo, "exp:../secret.yaml")
        outside = Path(self.temp.name) / "outside.yaml"; outside.write_text("o")
        (repo / "conf_exp/link.yaml").symlink_to(outside)
        with self.assertRaises(SystemExit):
            M.resolve_ref(repo, "exp:link.yaml")

    def test_T_F1_4_malformed_declaration_fails_closed(self):
        cases = []

        bad_json = self._dir(); (bad_json / ".lab-config-layout.json").write_text("{not json")
        cases.append(bad_json)

        missing_roots = self._dir(); (missing_roots / ".lab-config-layout.json").write_text(json.dumps({"schema_version": 1}))
        cases.append(missing_roots)

        extra_key = self._dir()
        (extra_key / ".lab-config-layout.json").write_text(json.dumps({
            "schema_version": 1, "roots": {"default": "c", "exp": "e", "legacy": "l", "extra": "x"}}))
        cases.append(extra_key)

        absolute_root = self._dir()
        (absolute_root / ".lab-config-layout.json").write_text(json.dumps({
            "schema_version": 1, "roots": {"default": "/etc", "exp": "e", "legacy": "l"}}))
        cases.append(absolute_root)

        traversal_root = self._dir()
        (traversal_root / ".lab-config-layout.json").write_text(json.dumps({
            "schema_version": 1, "roots": {"default": "c/../x", "exp": "e", "legacy": "l"}}))
        cases.append(traversal_root)

        for repo in cases:
            with self.subTest(repo=repo):
                with self.assertRaises(SystemExit):
                    M.layout_spec(repo.resolve())

    def test_T_F1_5_nested_roots_use_longest_match(self):
        repo = self._custom_repo({"default": "conf", "exp": "conf/experiments", "legacy": "conf/archive"})
        (repo / "conf/experiments/demo").mkdir(parents=True, exist_ok=True)
        (repo / "conf/experiments/demo/b.yaml").write_text("b")
        resolved = M.resolve_ref(repo, "conf/experiments/demo/b.yaml")
        self.assertEqual(resolved["namespace"], "exp")
        self.assertEqual(resolved["config_ref"], "exp:demo/b.yaml")

    def test_T_F1_6_lexically_duplicate_roots_fail_closed(self):
        repo = self._dir()
        (repo / ".lab-config-layout.json").write_text(json.dumps({
            "schema_version": 1, "roots": {"default": "conf", "exp": "conf", "legacy": "conf_legacy"}}))
        with self.assertRaises(SystemExit):
            M.layout_spec(repo.resolve())

    def test_T_F1_7_segment_boundary_prefix_is_not_a_match(self):
        repo = self._custom_repo({"default": "conf", "exp": "conf_exp", "legacy": "conf_legacy"})
        (repo / "conference").mkdir(); (repo / "conference/x.yaml").write_text("x")
        resolved = M.resolve_ref(repo, "conference/x.yaml")
        self.assertEqual(resolved["namespace"], "path")
        self.assertEqual(resolved["config_ref"], "path:conference/x.yaml")

    def test_T_F1_8_label_only_declaration_is_honest_about_roots(self):
        repo = self._dir()
        (repo / ".lab-config-layout").write_text("bcresnet\n")
        label, roots, kind = M.layout_spec(repo.resolve())
        self.assertEqual(kind, "label-only")
        self.assertEqual(label, "declared/bcresnet")
        self.assertEqual(roots, M.DEFAULT_ROOTS)

    def test_T_F1_9_bad_json_reports_declaration_path_in_stderr(self):
        repo = self._dir(); decl = repo / ".lab-config-layout.json"; decl.write_text("{not json")
        result = run_cli("resolve", "--repo", str(repo), "--ref", "a.yaml")
        self.assertEqual(result.returncode, 65)
        self.assertIn(str(decl), result.stderr)

    def test_T_F1_10_symlinked_root_collapse_fails_closed(self):
        repo = self._dir()
        (repo / "conf").mkdir(); (repo / "conf_legacy").mkdir()
        (repo / "conf_link").symlink_to(repo / "conf", target_is_directory=True)
        (repo / ".lab-config-layout.json").write_text(json.dumps({
            "schema_version": 1, "roots": {"default": "conf", "exp": "conf_link", "legacy": "conf_legacy"}}))
        with self.assertRaises(SystemExit):
            M.layout_spec(repo.resolve())

    # ================= F2: canonical config_ref =================

    def test_T_F2_1_five_forms_normalize_to_the_same_config_ref(self):
        forms = ["a.yaml", "config:a.yaml", "configs/a.yaml", "./configs/a.yaml", str((self.repo / "configs/a.yaml").resolve())]
        for ref in forms:
            with self.subTest(ref=ref):
                self.assertEqual(M.resolve_ref(self.repo, ref)["config_ref"], "config:a.yaml")

    def test_T_F2_2_exp_forms_normalize(self):
        for ref in ("exp:demo/b.yaml", "configs_exp/demo/b.yaml"):
            with self.subTest(ref=ref):
                self.assertEqual(M.resolve_ref(self.repo, ref)["config_ref"], "exp:demo/b.yaml")

    def test_T_F2_3_custom_layout_normalizes(self):
        repo = self._custom_repo({"default": "conf", "exp": "conf_exp", "legacy": "conf_legacy"})
        (repo / "conf/a.yaml").write_text("a")
        self.assertEqual(M.resolve_ref(repo, "conf/a.yaml")["config_ref"], "config:a.yaml")

    def test_T_F2_4_seal_rejects_forged_config_ref(self):
        artifact_root = self._dir()
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root, config_ref="config:forged.yaml")
        self.assertEqual(result.returncode, 65)

    def test_T_F2_5_run_id_config_ref_grammar_is_validated(self):
        # G2 hardening (round 5) made --config-sha256 required at the argparse
        # level; without a valid one, every case here would hit that gate
        # (exit 2) instead of the grammar check this test targets, going
        # vacuous. A valid 64-hex sha keeps the grammar assertion live.
        for bad in ("config:../x", "not-a-ref", "config:\x01bad", "config:trailing/"):
            with self.subTest(ref=bad):
                result = run_cli("run-id", "--slug", "demo", "--config-ref", bad, "--config-sha256", "a" * 64)
                self.assertEqual(result.returncode, 65)

    def test_T_F2_6_symlinked_declared_root_still_attributes_correctly(self):
        repo = self._dir()
        (repo / "conf").mkdir(); (repo / "conf_legacy").mkdir()
        (repo / "actual_exp").mkdir(); (repo / "actual_exp/demo").mkdir()
        (repo / "actual_exp/demo/b.yaml").write_text("b")
        (repo / "conf_exp").symlink_to(repo / "actual_exp", target_is_directory=True)
        (repo / ".lab-config-layout.json").write_text(json.dumps({
            "schema_version": 1, "roots": {"default": "conf", "exp": "conf_exp", "legacy": "conf_legacy"}}))
        resolved = M.resolve_ref(repo, "exp:demo/b.yaml")
        self.assertEqual(resolved["namespace"], "exp")
        self.assertEqual(resolved["config_ref"], "exp:demo/b.yaml")

    # ================= G1: nested declared-root namespace crossover =================
    # Three fixtures are needed because crossover direction is a function of
    # nesting geometry -- no single layout exercises all six directions.

    def _fixture_a(self):
        # default:conf, exp:conf/experiments, legacy:conf/archive
        repo = self._custom_repo({"default": "conf", "exp": "conf/experiments", "legacy": "conf/archive"})
        (repo / "conf/experiments/demo").mkdir(parents=True, exist_ok=True)
        (repo / "conf/experiments/demo/x.yaml").write_text("x")
        (repo / "conf/archive/y.yaml").write_text("y")
        return repo

    def _fixture_b(self):
        # exp:conf/exp, default:conf/exp/inner, legacy:conf/exp/old
        repo = self._custom_repo({"exp": "conf/exp", "default": "conf/exp/inner", "legacy": "conf/exp/old"})
        (repo / "conf/exp/inner/a.yaml").write_text("a")
        (repo / "conf/exp/old/o.yaml").write_text("o")
        return repo

    def _fixture_c(self):
        # legacy:conf, default:conf/adopted, exp:conf/experiments
        repo = self._custom_repo({"legacy": "conf", "default": "conf/adopted", "exp": "conf/experiments"})
        (repo / "conf/adopted/d.yaml").write_text("d")
        (repo / "conf/experiments/demo").mkdir(parents=True, exist_ok=True)
        (repo / "conf/experiments/demo/e.yaml").write_text("e")
        return repo

    def test_T_G1_1_fixture_A_default_to_exp_crossover_is_rejected(self):
        repo = self._fixture_a()
        result = run_cli("resolve", "--repo", str(repo), "--ref", "config:experiments/demo/x.yaml")
        self.assertEqual(result.returncode, 65)
        self.assertIn("requested the config namespace", result.stderr)
        self.assertIn("resolves under exp", result.stderr)

    def test_T_G1_2_fixture_A_default_to_legacy_crossover_is_rejected(self):
        repo = self._fixture_a()
        result = run_cli("resolve", "--repo", str(repo), "--ref", "config:archive/y.yaml")
        self.assertEqual(result.returncode, 65)
        self.assertIn("requested the config namespace", result.stderr)
        self.assertIn("resolves under legacy", result.stderr)

    def test_T_G1_3_fixture_B_exp_to_default_crossover_is_rejected(self):
        repo = self._fixture_b()
        result = run_cli("resolve", "--repo", str(repo), "--ref", "exp:inner/a.yaml")
        self.assertEqual(result.returncode, 65)
        self.assertIn("requested the exp namespace", result.stderr)
        self.assertIn("resolves under config", result.stderr)

    def test_T_G1_4_fixture_B_exp_to_legacy_crossover_is_rejected(self):
        repo = self._fixture_b()
        result = run_cli("resolve", "--repo", str(repo), "--ref", "exp:old/o.yaml")
        self.assertEqual(result.returncode, 65)
        self.assertIn("requested the exp namespace", result.stderr)
        self.assertIn("resolves under legacy", result.stderr)

    def test_T_G1_5_fixture_C_legacy_to_default_crossover_is_rejected(self):
        repo = self._fixture_c()
        result = run_cli("resolve", "--repo", str(repo), "--ref", "legacy:adopted/d.yaml")
        self.assertEqual(result.returncode, 65)
        self.assertIn("requested the legacy namespace", result.stderr)
        self.assertIn("resolves under config", result.stderr)

    def test_T_G1_6_fixture_C_legacy_to_exp_crossover_is_rejected(self):
        repo = self._fixture_c()
        result = run_cli("resolve", "--repo", str(repo), "--ref", "legacy:experiments/demo/e.yaml")
        self.assertEqual(result.returncode, 65)
        self.assertIn("requested the legacy namespace", result.stderr)
        self.assertIn("resolves under exp", result.stderr)

    def test_T_G1_7_physical_path_reaches_nested_root_that_prefixed_ref_cannot(self):
        repo = self._fixture_a()
        rejected = run_cli("resolve", "--repo", str(repo), "--ref", "config:experiments/demo/x.yaml")
        self.assertEqual(rejected.returncode, 65)
        allowed = run_cli("resolve", "--repo", str(repo), "--ref", "conf/experiments/demo/x.yaml")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(json.loads(allowed.stdout)["config_ref"], "exp:demo/x.yaml")

    def test_T_G1_8_requested_parameter_mismatch_is_rejected(self):
        with self.assertRaises(SystemExit):
            M.resolve_ref(self.repo, "a.yaml", requested="exp")
        self.assertEqual(M.resolve_ref(self.repo, "exp:demo/b.yaml", requested="exp")["namespace"], "exp")

    # ================= F3: slug/run-id safety =================

    def test_T_F3_1_slug_is_required_and_validated(self):
        artifact_root = self._dir()
        for bad_slug in ("../x", ".", "..", "a" * 65, "bad\x01slug"):
            with self.subTest(slug=bad_slug):
                result = seal_cli(self.repo, "a.yaml", bad_slug, artifact_root)
                self.assertEqual(result.returncode, 65)

    def test_T_F3_2_run_id_stem_excludes_namespace_prefix(self):
        rid = M.run_id("demo", "config:a.yaml", "a" * 64)
        self.assertIn("__a__", rid)
        self.assertNotIn("config-a", rid)
        self.assertNotIn("config:a", rid)

    def test_T_F3_3_run_id_is_optional_and_verified(self):
        artifact_root = self._dir()
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        computed = json.loads(result.stdout)["run_id"]
        mismatched = seal_cli(self.repo, "a.yaml", "demo", artifact_root, run_id="not-the-computed-value")
        self.assertEqual(mismatched.returncode, 65)
        traversal = seal_cli(self.repo, "a.yaml", "demo", artifact_root, run_id="../../evil")
        self.assertEqual(traversal.returncode, 65)
        matching = seal_cli(self.repo, "a.yaml", "demo", artifact_root, run_id=computed)
        self.assertEqual(matching.returncode, 0, matching.stderr)

    def test_T_F3_4_manifest_path_matches_derived_output_directory(self):
        artifact_root = self._dir()
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id_value = json.loads(result.stdout)["run_id"]
        manifest = manifest_dir(artifact_root, "demo") / f"{run_id_value}.manifest.json"
        snapshot = next(manifest_dir(artifact_root, "demo").glob("*.yaml"))
        self.assertEqual(manifest.resolve().parent, snapshot.resolve().parent)

    # ================= G2: standalone run-id validation =================

    def test_T_G2_1_unsafe_slug_is_rejected(self):
        for bad_slug in ("../evil", "", "a" * 65):
            with self.subTest(slug=bad_slug):
                result = run_cli("run-id", "--slug", bad_slug, "--config-ref", "config:a.yaml",
                                  "--config-sha256", "a" * 64)
                self.assertEqual(result.returncode, 65)

    def test_T_G2_2_malformed_config_sha256_is_rejected(self):
        for bad_sha in ("z" * 64, "A" * 64, "a" * 63, "a" * 65):
            with self.subTest(sha=bad_sha):
                result = run_cli("run-id", "--slug", "demo", "--config-ref", "config:a.yaml",
                                  "--config-sha256", bad_sha)
                self.assertEqual(result.returncode, 65)

    def test_T_G2_3_missing_config_sha256_is_argparse_rejected(self):
        result = run_cli("run-id", "--slug", "demo", "--config-ref", "config:a.yaml")
        self.assertEqual(result.returncode, 2)

    def test_T_G2_4_non_positive_attempt_is_rejected(self):
        for bad_attempt in ("0", "-1"):
            with self.subTest(attempt=bad_attempt):
                result = run_cli("run-id", "--slug", "demo", "--config-ref", "config:a.yaml",
                                  "--config-sha256", "a" * 64, "--attempt", bad_attempt)
                self.assertEqual(result.returncode, 65)
        ok = run_cli("run-id", "--slug", "demo", "--config-ref", "config:a.yaml",
                      "--config-sha256", "a" * 64, "--attempt", "1")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue(ok.stdout.strip().endswith("__a1"))

    def test_T_G2_5_run_id_matches_seal_computed_id(self):
        artifact_root = self._dir()
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        cli = run_cli("run-id", "--slug", "demo", "--config-ref", manifest["config_ref"],
                       "--config-sha256", manifest["source_sha256"])
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(cli.stdout.strip(), manifest["run_id"])

    # ================= F4: source-scoped git state =================

    def test_T_F4_1_clean(self):
        repo = self._git_repo()
        state, commit = M.git_state(repo, Path("configs/a.yaml"))
        self.assertEqual(state, "clean"); self.assertRegex(commit, r"^[0-9a-f]{40}$")

    def test_T_F4_2_staged(self):
        repo = self._git_repo()
        (repo / "configs/a.yaml").write_text("v2\n")
        subprocess.run(["git", "-C", str(repo), "add", "configs/a.yaml"], check=True)
        state, _ = M.git_state(repo, Path("configs/a.yaml"))
        self.assertEqual(state, "staged")

    def test_T_F4_3_staged_and_unstaged(self):
        repo = self._git_repo()
        (repo / "configs/a.yaml").write_text("v2\n")
        subprocess.run(["git", "-C", str(repo), "add", "configs/a.yaml"], check=True)
        (repo / "configs/a.yaml").write_text("v3\n")
        state, _ = M.git_state(repo, Path("configs/a.yaml"))
        self.assertEqual(state, "staged+unstaged")

    def test_T_F4_4_untracked(self):
        repo = self._git_repo()
        (repo / "configs/new.yaml").write_text("new\n")
        state, _ = M.git_state(repo, Path("configs/new.yaml"))
        self.assertEqual(state, "untracked")

    def test_T_F4_5_non_git_directory(self):
        repo = self._dir(); (repo / "configs").mkdir(); (repo / "configs/a.yaml").write_text("x")
        state, commit = M.git_state(repo, Path("configs/a.yaml"))
        self.assertEqual(state, "unknown-no-git"); self.assertEqual(commit, "unknown")

    # Must never come back "clean" for a probe that is actually source-scoped.
    def test_T_F4_6_dirty_sibling_does_not_taint_clean_config(self):
        repo = self._git_repo()
        (repo / "configs/sibling.yaml").write_text("dirty\n")
        state, _ = M.git_state(repo, Path("configs/a.yaml"))
        self.assertEqual(state, "clean")

    def test_T_F4_7_gitignored_config_is_ignored_and_dirty(self):
        repo = self._git_repo()
        (repo / ".gitignore").write_text("configs/ignored.yaml\n")
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "ignore"], check=True)
        (repo / "configs/ignored.yaml").write_text("secret\n")
        state, _ = M.git_state(repo, Path("configs/ignored.yaml"))
        self.assertEqual(state, "ignored")

    def test_T_F4_8_rename_record_does_not_misparse_next_record(self):
        repo = self._git_repo()
        subprocess.run(["git", "-C", str(repo), "mv", "configs/a.yaml", "configs/renamed.yaml"], check=True)
        state, _ = M.git_state(repo, Path("configs/renamed.yaml"))
        self.assertIn(state, ("staged", "staged+unstaged"))

    # ================= F5: artifact-root derivation =================

    def test_T_F5_1_linked_worktree_writes_to_primary_artifact_root(self):
        primary = self._git_repo()
        (primary / ".agent_reports").mkdir()
        linked = Path(self.temp.name) / "linked-worktree"
        subprocess.run(["git", "-C", str(primary), "worktree", "add", str(linked)], check=True, capture_output=True, text=True)
        env = dict(CLEAN_ENV); env.pop("AGENT_ARTIFACT_ROOT", None)
        artifact_root_probe = subprocess.run(
            ["sh", str(ROOT / "utilities/artifact-root.sh"), str(linked)],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(artifact_root_probe.returncode, 0, artifact_root_probe.stderr)
        artifact_root = Path(artifact_root_probe.stdout.strip())
        before = sorted(p.relative_to(linked) for p in linked.rglob("*") if ".git" not in p.parts)
        result = seal_cli(linked, "configs/a.yaml", "demo", artifact_root, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id_value = json.loads(result.stdout)["run_id"]
        self.assertTrue((manifest_dir(artifact_root, "demo") / f"{run_id_value}.manifest.json").is_file())
        after = sorted(p.relative_to(linked) for p in linked.rglob("*") if ".git" not in p.parts)
        self.assertEqual(before, after)

    def test_T_F5_3_artifact_root_must_be_absolute_and_exist(self):
        for bad in ("relative-dir", str(Path(self.temp.name) / "does-not-exist")):
            with self.subTest(bad=bad):
                result = seal_cli(self.repo, "a.yaml", "demo", bad)
                self.assertEqual(result.returncode, 65)

    # ================= F6: fail-closed verify + schema v2 =================

    def _sealed_manifest(self):
        artifact_root = self._dir()
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id_value = json.loads(result.stdout)["run_id"]
        manifest = manifest_dir(artifact_root, "demo") / f"{run_id_value}.manifest.json"
        return artifact_root, manifest, json.loads(manifest.read_text())

    def test_T_F6_1_key_set_mismatch_is_rejected(self):
        _, manifest, data = self._sealed_manifest()
        missing_key = dict(data); del missing_key["source_git_state"]
        manifest.write_text(json.dumps(missing_key))
        self.assertEqual(run_cli("verify", "--manifest", str(manifest)).returncode, 65)
        extra_key = dict(data); extra_key["extra"] = "x"
        manifest.write_text(json.dumps(extra_key))
        self.assertEqual(run_cli("verify", "--manifest", str(manifest)).returncode, 65)

    def test_T_F6_2_schema_version_1_is_rejected(self):
        _, manifest, data = self._sealed_manifest()
        v1 = dict(data); v1["schema_version"] = 1
        manifest.write_text(json.dumps(v1))
        self.assertEqual(run_cli("verify", "--manifest", str(manifest)).returncode, 65)

    def test_T_F6_3_type_and_pattern_violations_are_rejected(self):
        _, manifest, data = self._sealed_manifest()
        cases = [
            {"source_dirty": "false"},
            {"source_sha256": "a" * 63},
            {"source_git_state": "not-a-state"},
            {"config_ref": "not-a-ref"},
        ]
        for change in cases:
            with self.subTest(change=change):
                bad = dict(data); bad.update(change)
                manifest.write_text(json.dumps(bad))
                self.assertEqual(run_cli("verify", "--manifest", str(manifest)).returncode, 65)

    def test_T_F6_4_unsafe_run_id_is_rejected(self):
        _, manifest, data = self._sealed_manifest()
        bad = dict(data); bad["run_id"] = "../evil"
        manifest.write_text(json.dumps(bad))
        self.assertEqual(run_cli("verify", "--manifest", str(manifest)).returncode, 65)

    def test_T_F6_5_source_and_snapshot_digest_must_agree(self):
        _, manifest, data = self._sealed_manifest()
        bad = dict(data); bad["source_sha256"] = "0" * 64
        manifest.write_text(json.dumps(bad))
        self.assertEqual(run_cli("verify", "--manifest", str(manifest)).returncode, 65)

    def test_T_F6_6_snapshot_filename_and_digest_are_checked(self):
        artifact_root, manifest, data = self._sealed_manifest()
        snapshot = Path(data["snapshot_path"])
        original = snapshot.read_bytes()
        snapshot.write_text("tampered")
        self.assertEqual(run_cli("verify", "--manifest", str(manifest)).returncode, 65)
        snapshot.write_bytes(original)
        renamed = snapshot.with_name("not-the-hash.yaml"); snapshot.rename(renamed)
        bad = dict(data); bad["snapshot_path"] = str(renamed)
        manifest.write_text(json.dumps(bad))
        self.assertEqual(run_cli("verify", "--manifest", str(manifest)).returncode, 65)

    def test_T_F6_7_manifest_must_stay_in_situ(self):
        artifact_root, manifest, data = self._sealed_manifest()
        outside = self._dir()
        moved_manifest = outside / manifest.name; moved_snapshot = outside / Path(data["snapshot_path"]).name
        moved_manifest.write_bytes(manifest.read_bytes())
        moved_snapshot.write_bytes(Path(data["snapshot_path"]).read_bytes())
        rewritten = dict(data); rewritten["snapshot_path"] = str(moved_snapshot)
        moved_manifest.write_text(json.dumps(rewritten))
        self.assertEqual(run_cli("verify", "--manifest", str(moved_manifest)).returncode, 65)

    def test_T_F6_8_source_absence_passes_but_source_tamper_fails(self):
        artifact_root, manifest, data = self._sealed_manifest()
        source = Path(data["source_path"])
        backup = source.read_bytes()
        source.unlink()
        result = run_cli("verify", "--manifest", str(manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source_present=false", result.stdout)
        source.write_text("tampered")
        result2 = run_cli("verify", "--manifest", str(manifest))
        self.assertEqual(result2.returncode, 65)
        source.write_bytes(backup)

    def test_T_F6_9_required_keys_are_a_three_way_anchor(self):
        schema = json.loads((ROOT / "capabilities/lab-config-manifest.schema.json").read_text())
        self.assertEqual(set(schema["required"]), M.MANIFEST_KEYS)
        _, _, data = self._sealed_manifest()
        self.assertEqual(set(data.keys()), M.MANIFEST_KEYS)

    # ================= G3: manifest identity re-proof =================

    def test_T_G3_1_relative_source_path_is_rejected(self):
        _, manifest, data = self._sealed_manifest()
        bad = dict(data); bad["source_path"] = "relative/path.yaml"
        manifest.write_text(json.dumps(bad))
        result = run_cli("verify", "--manifest", str(manifest))
        self.assertEqual(result.returncode, 65)
        self.assertIn("source_path must be an absolute path", result.stderr)

    def test_T_G3_2_relative_snapshot_path_is_rejected(self):
        _, manifest, data = self._sealed_manifest()
        bad = dict(data); bad["snapshot_path"] = "relative/snapshot.yaml"
        manifest.write_text(json.dumps(bad))
        result = run_cli("verify", "--manifest", str(manifest))
        self.assertEqual(result.returncode, 65)
        self.assertIn("snapshot_path must be an absolute path", result.stderr)

    def test_T_G3_3_source_dirty_disagreement_is_rejected(self):
        _, manifest, data = self._sealed_manifest()
        bad = dict(data); bad["source_git_state"] = "clean"; bad["source_dirty"] = True
        manifest.write_text(json.dumps(bad))
        result = run_cli("verify", "--manifest", str(manifest))
        self.assertEqual(result.returncode, 65)
        self.assertIn("source_dirty disagrees with source_git_state", result.stderr)

    def test_T_G3_4_manifest_filename_must_match_run_id(self):
        _, manifest, data = self._sealed_manifest()
        renamed = manifest.with_name("other.manifest.json")
        manifest.rename(renamed)
        result = run_cli("verify", "--manifest", str(renamed))
        self.assertEqual(result.returncode, 65)
        self.assertIn("manifest filename must be", result.stderr)

    def test_T_G3_5_chain_violation_fires_independent_of_colocation(self):
        _, manifest, data = self._sealed_manifest()
        outside_root = self._dir()
        broken = outside_root / "demo" / "_internal" / "configs"
        broken.mkdir(parents=True)
        new_snapshot = broken / Path(data["snapshot_path"]).name
        new_snapshot.write_bytes(Path(data["snapshot_path"]).read_bytes())
        rewritten = dict(data); rewritten["snapshot_path"] = str(new_snapshot)
        new_manifest = broken / manifest.name
        new_manifest.write_text(json.dumps(rewritten))
        # co-location is satisfied (both files share `broken`); only the
        # experiments/<slug>/_internal/configs chain is violated, and it must
        # fire on its own.
        result = run_cli("verify", "--manifest", str(new_manifest))
        self.assertEqual(result.returncode, 65)
        self.assertIn("experiments", result.stderr)

    def test_T_G3_6_run_id_swap_fails_recomputation(self):
        _, manifest, data = self._sealed_manifest()
        bad = dict(data); bad["run_id"] = "other-run-id"
        renamed = manifest.with_name("other-run-id.manifest.json")
        manifest.write_text(json.dumps(bad))
        manifest.rename(renamed)
        result = run_cli("verify", "--manifest", str(renamed))
        self.assertEqual(result.returncode, 65)
        self.assertIn("run_id does not match the sealed identity", result.stderr)

    def test_T_G3_7_config_ref_swap_fails_recomputation(self):
        _, manifest, data = self._sealed_manifest()
        bad = dict(data); bad["config_ref"] = "config:different.yaml"
        manifest.write_text(json.dumps(bad))
        result = run_cli("verify", "--manifest", str(manifest))
        self.assertEqual(result.returncode, 65)
        self.assertIn("run_id does not match the sealed identity", result.stderr)

    # T-G3-R2/R3: `experiments` itself is a symlink to a real sibling
    # directory. `cmd_seal`'s `resolve(strict=True)` collapses that segment in
    # the recorded paths, so verify must accept the manifest when addressed
    # via the documented derived path (through the `experiments` symlink) and
    # reject it when addressed via the fully-resolved form -- a stated,
    # accepted limitation (plan §1.3), not a bug.
    def test_T_G3_R2_symlinked_experiments_segment_verifies_via_derived_path(self):
        artifact_root = self._dir()
        real_dir = artifact_root / "exp-store"; real_dir.mkdir()
        (artifact_root / "experiments").symlink_to(real_dir, target_is_directory=True)
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id_value = json.loads(result.stdout)["run_id"]
        derived = artifact_root / "experiments" / "demo" / "_internal" / "configs" / f"{run_id_value}.manifest.json"
        self.assertTrue(derived.is_file())
        verify_result = run_cli("verify", "--manifest", str(derived))
        self.assertEqual(verify_result.returncode, 0, verify_result.stderr)

    def test_T_G3_R3_resolved_form_of_symlinked_experiments_is_the_documented_limitation(self):
        artifact_root = self._dir()
        real_dir = artifact_root / "exp-store"; real_dir.mkdir()
        (artifact_root / "experiments").symlink_to(real_dir, target_is_directory=True)
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id_value = json.loads(result.stdout)["run_id"]
        resolved = real_dir / "demo" / "_internal" / "configs" / f"{run_id_value}.manifest.json"
        self.assertTrue(resolved.is_file())
        verify_result = run_cli("verify", "--manifest", str(resolved))
        self.assertEqual(verify_result.returncode, 65)

    def test_T_G3_R4_slug_level_symlink_seals_and_verifies_via_derived_path(self):
        artifact_root = self._dir()
        (artifact_root / "experiments").mkdir()
        real_slug_dir = artifact_root / "real-demo"; real_slug_dir.mkdir()
        (artifact_root / "experiments" / "demo").symlink_to(real_slug_dir, target_is_directory=True)
        result = seal_cli(self.repo, "a.yaml", "demo", artifact_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id_value = json.loads(result.stdout)["run_id"]
        derived = artifact_root / "experiments" / "demo" / "_internal" / "configs" / f"{run_id_value}.manifest.json"
        self.assertTrue(derived.is_file())
        verify_result = run_cli("verify", "--manifest", str(derived))
        self.assertEqual(verify_result.returncode, 0, verify_result.stderr)

        # A second symlink at the <slug> level, `experiments/other-slug`,
        # points at the same real directory -- the same physical manifest is
        # now reachable under a different slug. run_id/config_ref/
        # source_sha256 are untouched; the "candidate 1 wins" chain-slug
        # recovery plus exact run-id recomputation must reject this, since the
        # manifest's own run_id was computed against "demo", not "other-slug".
        (artifact_root / "experiments" / "other-slug").symlink_to(real_slug_dir, target_is_directory=True)
        via_other_slug = (artifact_root / "experiments" / "other-slug" / "_internal" / "configs"
                           / f"{run_id_value}.manifest.json")
        self.assertTrue(via_other_slug.is_file())
        tampered_result = run_cli("verify", "--manifest", str(via_other_slug))
        self.assertEqual(tampered_result.returncode, 65)
        self.assertIn("run_id does not match the sealed identity", tampered_result.stderr)

    # ================= F8: archive-verified packaging =================

    def _make_zip(self, path, members):
        with zipfile.ZipFile(path, "w") as zf:
            for name, content in members.items():
                zf.writestr(name, content)

    def _make_tar(self, path, mode, plain=(), symlinks=(), dirs=()):
        with tarfile.open(path, mode) as tf:
            for name in dirs:
                info = tarfile.TarInfo(name=name); info.type = tarfile.DIRTYPE
                tf.addfile(info)
            for name, content in plain:
                data = content.encode()
                info = tarfile.TarInfo(name=name); info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            for name, target in symlinks:
                info = tarfile.TarInfo(name=name); info.type = tarfile.SYMTYPE; info.linkname = target
                tf.addfile(info)

    def test_T_F8_1_zip_archive_with_all_roots_verifies(self):
        archive = Path(self.temp.name) / "pkg.whl"
        self._make_zip(archive, {
            "pkg/configs/a.yaml": "x", "pkg/configs_exp/demo/b.yaml": "x", "pkg/configs_legacy/c.yaml": "x",
        })
        result = run_cli("package-data", "--repo", str(self.repo), "--archive", str(archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["check"], "archive"); self.assertTrue(data["verified"]); self.assertEqual(data["missing"], [])

    def test_T_F8_2_missing_root_fails_closed(self):
        archive = Path(self.temp.name) / "pkg-partial.whl"
        self._make_zip(archive, {"pkg/configs_exp/demo/b.yaml": "x", "pkg/configs_legacy/c.yaml": "x"})
        result = run_cli("package-data", "--repo", str(self.repo), "--archive", str(archive))
        self.assertEqual(result.returncode, 65)
        self.assertEqual(json.loads(result.stdout)["missing"], ["configs"])

    def test_T_F8_3_tar_gz_archive_works(self):
        archive = Path(self.temp.name) / "pkg.tar.gz"
        self._make_tar(archive, "w:gz", plain=[
            ("pkg/configs/a.yaml", "x"), ("pkg/configs_exp/demo/b.yaml", "x"), ("pkg/configs_legacy/c.yaml", "x"),
        ])
        result = run_cli("package-data", "--repo", str(self.repo), "--archive", str(archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["verified"])

    def test_T_F8_4_unsupported_archive_format_fails_closed(self):
        archive = Path(self.temp.name) / "pkg.rar"; archive.write_bytes(b"not-an-archive")
        result = run_cli("package-data", "--repo", str(self.repo), "--archive", str(archive))
        self.assertEqual(result.returncode, 65)

    def test_T_F8_5_static_mode_does_not_claim_verification(self):
        repo = self._dir(); (repo / "pyproject.toml").write_text("include = ['configs', 'configs_exp', 'configs_legacy']")
        result = run_cli("package-data", "--repo", str(repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["check"], "static-declaration")

    def test_T_F8_6_symlink_and_hardlink_members_count_as_present(self):
        archive = Path(self.temp.name) / "pkg-links.tar"
        self._make_tar(archive, "w", plain=[
            ("pkg/_real/a.yaml", "x"), ("pkg/configs_exp/demo/b.yaml", "x"), ("pkg/configs_legacy/c.yaml", "x"),
        ], symlinks=[("pkg/configs/a.yaml", "../_real/a.yaml")])
        result = run_cli("package-data", "--repo", str(self.repo), "--archive", str(archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["verified"])

    def test_T_F8_7_leading_dot_slash_is_normalized(self):
        archive = Path(self.temp.name) / "pkg-dotslash.tar"
        self._make_tar(archive, "w", plain=[
            ("./pkg/configs/a.yaml", "x"), ("./pkg/configs_exp/demo/b.yaml", "x"), ("./pkg/configs_legacy/c.yaml", "x"),
        ])
        result = run_cli("package-data", "--repo", str(self.repo), "--archive", str(archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["verified"])

    def test_T_F8_8_unrelated_nested_same_name_dir_is_a_visible_false_positive(self):
        archive = Path(self.temp.name) / "pkg-nested.tar"
        self._make_tar(archive, "w", plain=[
            ("pkg/tests/fixtures/configs/x.yaml", "x"),
            ("pkg/configs_exp/demo/b.yaml", "x"), ("pkg/configs_legacy/c.yaml", "x"),
        ])
        result = run_cli("package-data", "--repo", str(self.repo), "--archive", str(archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["verified"])
        self.assertEqual(data["matched"]["default"], "pkg/tests/fixtures/configs/x.yaml")

    def test_T_F8_9_directory_only_entry_is_missing(self):
        archive = Path(self.temp.name) / "pkg-dironly.tar"
        self._make_tar(archive, "w", dirs=["pkg/configs/"], plain=[
            ("pkg/configs_exp/demo/b.yaml", "x"), ("pkg/configs_legacy/c.yaml", "x"),
        ])
        result = run_cli("package-data", "--repo", str(self.repo), "--archive", str(archive))
        self.assertEqual(result.returncode, 65)
        self.assertEqual(json.loads(result.stdout)["missing"], ["configs"])

    def test_T_F8_10_custom_declared_roots_via_archive(self):
        repo = self._custom_repo({"default": "conf", "exp": "conf_exp", "legacy": "conf_legacy"})
        archive = Path(self.temp.name) / "pkg-custom.whl"
        self._make_zip(archive, {"pkg/conf/a.yaml": "x", "pkg/conf_exp/demo/b.yaml": "x", "pkg/conf_legacy/c.yaml": "x"})
        result = run_cli("package-data", "--repo", str(repo), "--archive", str(archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["verified"])


if __name__ == "__main__": unittest.main()
