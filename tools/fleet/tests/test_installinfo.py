import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
from fleet import fleet, installinfo, render  # noqa: E402
from fleet.model import Session  # noqa: E402


class InstallInfoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.root = Path(self.tmp.name) / "hearting"
        self.root.mkdir(parents=True)
        self.env = {"HOME": str(self.home)}
        installinfo._REMOTE_CACHE.clear()
        installinfo._DIRTY_CACHE.clear()

    def tearDown(self):
        render.set_process_view(False)
        render.set_hearting(None)

    def _write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def _activation(self, runtime, mode, root=None, revision=None,
                    active_root=None, source_root=None):
        homes = {
            "claude": self.home / ".claude",
            "codex": self.home / ".codex",
            "opencode": self.home / ".config/opencode",
        }
        state = {
            "schema": 2, "mode": mode, "active_root": str(root or self.root),
            "source_root": str(root or self.root),
        }
        if active_root is not None:
            state["active_root"] = str(active_root)
        if source_root is not None:
            state["source_root"] = str(source_root)
        if revision is not None:
            state["active_revision"] = revision
        self._write_json(homes[runtime] / ".harness/activation.json", state)

    @staticmethod
    def _git(stdout="v9.1.0-2-gabc-dirty", returncode=0):
        def run(*_args, **_kwargs):
            return SimpleNamespace(returncode=returncode, stdout=stdout)
        return run

    def _git_head(self, oid):
        gitdir = self.root / ".git"
        (gitdir / "refs/heads").mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (gitdir / "refs/heads/main").write_text(oid + "\n", encoding="utf-8")

    def test_managed_stable_and_pinned_win_for_exact_release_root(self):
        state_path = self.home / ".local/state/hearting/distribution.json"
        for channel in ("stable", "pinned"):
            with self.subTest(channel=channel):
                self._write_json(state_path, {"schema": 1, "version": "v9.1.0",
                    "channel": channel, "release_root": str(self.root),
                    "runtimes": ["codex", "claude"]})
                with mock.patch.object(installinfo, "_git_version") as git_version:
                    value = installinfo.collect(self.root, self.env)
                self.assertEqual(value["version"], "v9.1.0")
                self.assertEqual(value["install_method"], "managed/" + channel)
                git_version.assert_not_called()

    def test_linked_uses_one_bounded_git_describe(self):
        self._activation("codex", "linked")
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout="v9.1.0-2-gabc-dirty\n")

        value = installinfo.collect(self.root, self.env, runner)
        self.assertEqual(value["install_method"], "linked")
        self.assertEqual(value["version"], "v9.1.0-2-gabc-dirty")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["timeout"], 2)

    def test_live_fast_local_fallback_reads_head_without_git_subprocess(self):
        self._activation("codex", "linked")
        head = "e" * 40
        self._git_head(head)

        def runner(*_args, **_kwargs):
            raise AssertionError("fast local version must not spawn git")

        value = installinfo.collect(self.root, self.env, runner, fast_local=True)
        self.assertEqual(value["version"], head[:8])

    def test_snapshot_describe_failure_falls_back_to_direct_head(self):
        self._activation("codex", "linked")
        head = "f" * 40
        self._git_head(head)
        value = installinfo.collect(self.root, self.env, self._git(returncode=1))
        self.assertEqual(value["version"], head[:8])

    def test_packaged_prefers_release_marker(self):
        self._activation("claude", "packaged", revision="a" * 40)
        (self.root / "RELEASE_VERSION").write_text("v4.0.0\n", encoding="utf-8")
        value = installinfo.collect(self.root, self.env, self._git(returncode=1))
        self.assertEqual((value["version"], value["install_method"]), ("v4.0.0", "packaged"))

    def test_packaged_falls_back_to_exact_activation_revision_without_git(self):
        revision = "a1" * 20
        self._activation("claude", "packaged", revision=revision)

        def runner(*_args, **_kwargs):
            raise AssertionError("packaged activation fallback must not spawn git")

        for kwargs in ({}, {"fast_local": True},
                       {"refresh_remote": True, "now": 1.0}):
            with self.subTest(kwargs=kwargs):
                value = installinfo.collect(self.root, self.env, runner, **kwargs)
                self.assertEqual((value["version"], value["install_method"]),
                                 (revision[:8], "packaged"))

    def test_packaged_revision_requires_valid_oid_and_exact_active_root(self):
        foreign = Path(self.tmp.name) / "foreign-active"
        foreign.mkdir()
        for revision, active_root in (("bad", self.root), ("b" * 40, foreign)):
            with self.subTest(revision=revision, active_root=active_root):
                self._activation("codex", "packaged", revision=revision,
                                 active_root=active_root, source_root=self.root)
                value = installinfo.collect(self.root, self.env, self._git(returncode=1))
                self.assertEqual((value["version"], value["install_method"]),
                                 ("unknown", "packaged"))

    def test_linked_live_refresh_uses_highest_head_exact_annotated_remote_semver(self):
        self._activation("codex", "linked")
        head = "b" * 40
        self._git_head(head)
        tag_object = "a" * 40
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            if argv[1] == "ls-remote":
                return SimpleNamespace(returncode=0, stdout=(
                    f"{head}\trefs/tags/v9.1.0\n"
                    f"{tag_object}\trefs/tags/v10.0.0\n"
                    f"{head}\trefs/tags/v10.0.0^{{}}\n"
                    f"{head}\trefs/tags/not-semver\n"))
            return SimpleNamespace(returncode=0, stdout="")

        value = installinfo.collect(self.root, self.env, runner,
                                    refresh_remote=True, now=100.0)
        self.assertEqual(value["version"], "v10.0.0")
        self.assertEqual(value["install_method"], "linked")
        self.assertEqual(sum(argv[1] == "ls-remote" for argv in calls), 1)

    def test_remote_release_dirty_suffix_and_success_ttl(self):
        self._activation("codex", "linked")
        head = "c" * 40
        self._git_head(head)
        remote_calls = 0

        def runner(argv, **_kwargs):
            nonlocal remote_calls
            if argv[1] == "ls-remote":
                remote_calls += 1
                return SimpleNamespace(returncode=0,
                    stdout=f"{head}\trefs/tags/v3.4.5\n")
            return SimpleNamespace(returncode=1, stdout="")

        first = installinfo.collect(self.root, self.env, runner,
                                    refresh_remote=True, now=100.0)
        cached = installinfo.collect(self.root, self.env, runner,
                                     refresh_remote=True, now=399.0)
        expired = installinfo.collect(self.root, self.env, runner,
                                      refresh_remote=True, now=401.0)
        self.assertEqual(first["version"], "v3.4.5-dirty")
        self.assertEqual(cached["version"], "v3.4.5-dirty")
        self.assertEqual(expired["version"], "v3.4.5-dirty")
        self.assertEqual(remote_calls, 2)

    def test_remote_failure_keeps_same_head_last_good(self):
        self._activation("codex", "linked")
        head = "d" * 40
        self._git_head(head)
        fail = False

        def runner(argv, **_kwargs):
            if argv[1] == "ls-remote":
                if fail:
                    return SimpleNamespace(returncode=1, stdout="")
                return SimpleNamespace(returncode=0,
                    stdout=f"{head}\trefs/tags/v7.8.9\n")
            return SimpleNamespace(returncode=0, stdout="")

        self.assertEqual(installinfo.collect(self.root, self.env, runner,
                         refresh_remote=True, now=1.0)["version"], "v7.8.9")
        fail = True
        self.assertEqual(installinfo.collect(self.root, self.env, runner,
                         refresh_remote=True, now=302.0)["version"], "v7.8.9")

    def test_managed_and_packaged_live_refresh_never_query_remote(self):
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=1, stdout="")

        distribution = self.home / ".local/state/hearting/distribution.json"
        self._write_json(distribution, {"schema": 1, "version": "v1.2.3",
            "channel": "stable", "release_root": str(self.root)})
        installinfo.collect(self.root, self.env, runner, refresh_remote=True, now=1.0)
        self.assertEqual(calls, [])

        distribution.unlink()
        self._activation("claude", "packaged")
        (self.root / "RELEASE_VERSION").write_text("v1.2.3\n", encoding="utf-8")
        installinfo.collect(self.root, self.env, runner, refresh_remote=True, now=1.0)
        self.assertEqual(calls, [])

    def test_conflicting_exact_activation_modes_are_mixed(self):
        self._activation("claude", "linked")
        self._activation("codex", "packaged")
        value = installinfo.collect(self.root, self.env, self._git("abc1234"))
        self.assertEqual(value["install_method"], "mixed")
        self.assertEqual(value["runtimes"], ["claude", "codex"])

    def test_foreign_and_symlink_state_never_label_current_root(self):
        foreign = Path(self.tmp.name) / "foreign"
        foreign.mkdir()
        self._activation("codex", "linked", foreign)
        distribution = self.home / ".local/state/hearting/distribution.json"
        target = Path(self.tmp.name) / "distribution-target.json"
        self._write_json(target, {"schema": 1, "version": "v99", "channel": "stable",
                                  "release_root": str(self.root)})
        distribution.parent.mkdir(parents=True, exist_ok=True)
        distribution.symlink_to(target)
        value = installinfo.collect(self.root, self.env, self._git(returncode=1))
        self.assertEqual((value["version"], value["install_method"]), ("unknown", "unmanaged"))

    def test_unsupported_activation_schema_is_ignored(self):
        self._activation("codex", "linked")
        path = self.home / ".codex/.harness/activation.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["schema"] = 999
        self._write_json(path, state)
        value = installinfo.collect(self.root, self.env, self._git(returncode=1))
        self.assertEqual((value["version"], value["install_method"]), ("unknown", "unmanaged"))

    def test_header_is_first_in_group_and_process_rendering(self):
        render.set_hearting({"version": "v7.0.0", "install_method": "linked"})
        for process in (False, True):
            with self.subTest(process=process):
                render.set_process_view(process)
                lines = render._build_lines([], [], "both", False, 0, layout="wide")
                self.assertEqual("".join(text for text, _key in lines[0]),
                                 "  hearting v7.0.0 · linked")
        render.set_process_view(False)

    def test_header_styles_release_build_dirty_and_method_as_quiet_metadata(self):
        render.set_hearting({"version": "v7.0.0-6-g0abc1234-dirty",
                             "install_method": "linked"})
        self.assertEqual(render._hearting_header_row(), [
            # F-77 gave the product name its own hue, so it is its own segment now.
            ("  ", None),
            ("hearting", "hearting_name"),
            (" ", None),
            ("v7.0.0", "version_release"),
            ("-6-g0abc1234", "version_build"),
            ("-dirty", "version_dirty"),
            (" · ", "dim"),
            ("linked", "version_method"),
        ])

    def test_header_keeps_untagged_commit_visually_subordinate(self):
        self.assertEqual(render._hearting_version_segments("0abc1234-dirty"), [
            ("0abc1234", "version_build"),
            ("-dirty", "version_dirty"),
        ])

    def test_header_and_first_divider_use_one_breathing_row_each(self):
        session = Session(harness="codex", pid=1, cwd="/work/repo", slug="session",
                          liveness="working")
        usage = [[("  usage", "dim")]]
        with mock.patch.object(render, "_usage_header_rows", return_value=usage):
            lines = render._build_lines([session], [], "both", False, 0, layout="narrow")

        self.assertIsNone(lines[1])
        self.assertEqual("".join(text for text, _key in lines[2]), "  usage")
        divider = next(i for i, line in enumerate(lines)
                       if line and line[0][0] == render._HFILL)
        header = next(i for i, line in enumerate(lines)
                      if line and "SESSIONS" in "".join(text for text, _key in line))
        self.assertEqual(header, divider + 2)
        self.assertIsNone(lines[divider + 1])
        self.assertIsNotNone(lines[header + 1])

    def test_process_view_keeps_same_compact_divider_spacing(self):
        render.set_process_view(True)
        lines = render._build_lines([], [], "both", False, 0, layout="narrow")
        self.assertIsNone(lines[1])
        divider = next(i for i, line in enumerate(lines)
                       if line and line[0][0] == render._HFILL)
        header = next(i for i, line in enumerate(lines)
                      if line and "PROCESS VIEW" in "".join(text for text, _key in line))
        self.assertEqual(header, divider + 2)
        self.assertIsNone(lines[divider + 1])
        self.assertIsNotNone(lines[header + 1])

    def test_snapshot_json_exposes_same_identity(self):
        identity = {"version": "v7.0.0", "install_method": "managed/stable",
                    "source": "distribution", "runtimes": ["codex"]}
        value = json.loads(fleet._snapshot_json([], [], hearting=identity))
        self.assertEqual(value["hearting"], identity)

    def test_json_snapshot_does_not_enable_remote_refresh(self):
        calls = []

        def identity(*_args, **kwargs):
            calls.append(kwargs)
            return {"version": "v1.0.0", "install_method": "linked",
                    "source": "activation", "runtimes": ["codex"]}

        with mock.patch.object(installinfo, "collect", side_effect=identity), \
             mock.patch.object(fleet, "collect_all", return_value=([], [])), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fleet.main(["--json"]), 0)
        self.assertEqual(calls, [{}])

    def test_live_collector_enables_remote_refresh_off_the_snapshot_paths(self):
        calls = []
        identity = {"version": "v1.0.0", "install_method": "linked",
                    "source": "activation", "runtimes": ["codex"]}

        def collect_identity(*_args, **kwargs):
            calls.append(kwargs)
            return identity

        def run_live(collector, _hfilter, _section, _interval):
            self.assertTrue(callable(collector.hearting_refresh))
            collector.hearting_refresh()
            return 0

        with mock.patch.object(installinfo, "collect", side_effect=collect_identity), \
             mock.patch.object(render, "run_live", side_effect=run_live):
            self.assertEqual(fleet.main([]), 0)
        self.assertEqual(calls, [
            {"fast_local": True},
            {"refresh_remote": True, "fast_local": True},
        ])


if __name__ == "__main__":
    unittest.main()
