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

    def tearDown(self):
        render.set_process_view(False)
        render.set_hearting(None)

    def _write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def _activation(self, runtime, mode, root=None):
        homes = {
            "claude": self.home / ".claude",
            "codex": self.home / ".codex",
            "opencode": self.home / ".config/opencode",
        }
        self._write_json(homes[runtime] / ".harness/activation.json", {
            "schema": 2, "mode": mode, "active_root": str(root or self.root),
            "source_root": str(root or self.root),
        })

    @staticmethod
    def _git(stdout="v9.1.0-2-gabc-dirty", returncode=0):
        def run(*_args, **_kwargs):
            return SimpleNamespace(returncode=returncode, stdout=stdout)
        return run

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

    def test_packaged_prefers_release_marker(self):
        self._activation("claude", "packaged")
        (self.root / "RELEASE_VERSION").write_text("v4.0.0\n", encoding="utf-8")
        value = installinfo.collect(self.root, self.env, self._git(returncode=1))
        self.assertEqual((value["version"], value["install_method"]), ("v4.0.0", "packaged"))

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
            ("  hearting ", "head"),
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


if __name__ == "__main__":
    unittest.main()
