"""Title lifecycle state is independent of release cwd; failures remain readable."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import refresh_title as rt, render, titles
from fleet.model import DispatchJob, Session
from fleet.tests.test_f17_title_refresh import _ConfigHomeMixin
from fleet.tests import test_title_consistency


class SummaryGovernorRootTest(_ConfigHomeMixin, unittest.TestCase):
    def test_missing_governor_spec_or_loader_keeps_a_failure_reason(self):
        for spec in (None, SimpleNamespace(loader=None)):
            with self.subTest(spec=spec), \
                 mock.patch.object(rt, "_resolve_commands", return_value=[(["codex"], None, None)]), \
                 mock.patch.object(rt.importlib.util, "spec_from_file_location", return_value=spec):
                box = {}
                self.assertEqual(rt.run_worker("prompt", capacity_held=True, provider_box=box), "")
                self.assertEqual(box, {"error": "governor-loader-unavailable"})

    def test_release_cwd_never_calls_project_governor_root_for_any_provider(self):
        for provider in ("claude", "codex", "opencode"):
            with self.subTest(provider=provider):
                governor = SimpleNamespace(
                    default_root=mock.Mock(side_effect=RuntimeError("immutable installed source tree")),
                    title_start_budget=mock.Mock(return_value=20),
                    acquire=mock.Mock(return_value="token"), release=mock.Mock())
                spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _module: None))
                box = {}
                state = Path(self._tmp.name) / "state"
                with mock.patch.dict(os.environ, {"AGENT_MODEL_GOVERNOR_ROOT": "",
                                                  "XDG_STATE_HOME": str(state), "HARNESS_STATE_ROOT": ""}), \
                     mock.patch.object(rt, "_resolve_commands", return_value=[([provider], None, None)]), \
                     mock.patch.object(rt.importlib.util, "spec_from_file_location", return_value=spec), \
                     mock.patch.object(rt.importlib.util, "module_from_spec", return_value=governor), \
                     mock.patch.object(rt, "run_provider_cascade", return_value=("TITLE: 제목 복구\nNOW: 검증 중", 0)):
                    self.assertTrue(rt.run_worker("prompt", capacity_held=True, provider_box=box))
                    governor.acquire.side_effect = RuntimeError("fixture root denied")
                    error_box = {}
                    self.assertEqual(rt.run_worker("prompt", capacity_held=True, provider_box=error_box), "")
                governor.default_root.assert_not_called()
                self.assertEqual(governor.acquire.call_args.kwargs["budget"], 20)
                root = governor.acquire.call_args.args[0]
                self.assertEqual(root, state / "hearting" / "dispatch" / "model-worker-governor")
                governor.release.assert_called_once_with(root, "token")
                self.assertEqual(box["provider"], provider)
                self.assertNotIn("error", box)
                self.assertEqual(error_box["error"], "RuntimeError: fixture root denied")

    def test_provider_failure_reason_is_saved_and_cleared_by_success(self):
        path = test_title_consistency.TitleConsistencyTest._transcript(self, "codex")

        def failed(_prompt, **kwargs):
            kwargs["provider_box"]["error"] = "RuntimeError: fixture governor failure"
            return ""

        args = ["--harness", "codex", "--sid", "sid-error", "--transcript", str(path),
                "--slotdir", str(Path(self._tmp.name) / "slot")]
        with mock.patch.object(rt, "run_worker", side_effect=failed):
            rt.main(args)
        self.assertEqual(titles.read("sid-error", "codex")["summary_error"],
                         "RuntimeError: fixture governor failure")
        with mock.patch.object(rt, "run_worker", return_value="TITLE: 제목 복구 확인\nNOW: 회귀 확인 중"):
            rt.main(args)
        self.assertNotIn("summary_error", titles.read("sid-error", "codex"))

    def test_missing_first_now_keeps_priority_on_a_periodic_retry(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                path = test_title_consistency.TitleConsistencyTest._transcript(self, harness)
                sid = "dispatch-retry-" + harness
                args = ["--harness", harness, "--sid", sid, "--transcript", str(path),
                        "--slotdir", str(Path(self._tmp.name) / "slot")]

                def refused(_prompt, **kwargs):
                    self.assertTrue(kwargs["priority"])
                    kwargs["provider_box"]["error"] = "_StartBudgetReached: title window full"
                    return ""

                with mock.patch.object(rt, "run_worker", side_effect=refused):
                    rt.main(args)
                failed = titles.read(sid, harness)
                self.assertFalse(failed.get("summary"))
                self.assertEqual(failed["offset"], 0)
                self.assertIn("_StartBudgetReached", failed["summary_error"])
                # Same scheduler entry, no --priority/--quota-class flags.
                with mock.patch.object(rt, "run_worker", return_value="TITLE: 제목 한도 복구\nNOW: 첫 요약 확인 중") as worker:
                    rt.main(args)
                self.assertTrue(worker.call_args.kwargs["priority"])
                self.assertEqual(titles.read(sid, harness)["summary"], "첫 요약 확인 중")
                self.assertNotIn("summary_error", titles.read(sid, harness))

    def test_codex_main_owner_worker_keep_exec_and_summary_on_one_detail_row(self):
        for entity in (Session(harness="codex", pid=1, liveness="working"),
                       DispatchJob(key="code", harness="codex", depth=1, liveness="working"),
                       DispatchJob(key="code-test", harness="codex", depth=2, liveness="working")):
            for width in (100, 168):
                entity.summary = "한국어 NOW 복구 확인"
                entity.exec_child = {"comm": "zsh", "etime_s": 120, "kind": "work"}
                rows = (render._context_detail_row(entity, term_width=width)
                        if isinstance(entity, Session) else
                        render._dispatch_summary_detail_row(entity, depth=entity.depth, term_width=width))
                self.assertEqual(len(rows), 1)
                text = "".join(t for t, _ in rows[0])
                self.assertIn("⚙ zsh 2m", text)
                self.assertIn("NOW", text)
                if width >= 168:
                    self.assertIn(entity.summary, text)
                self.assertLessEqual(sum(render._dw(t) for t, _ in rows[0]), width)


if __name__ == "__main__":
    unittest.main()
