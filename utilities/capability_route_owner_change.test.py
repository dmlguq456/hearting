#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
"""An owner that starts on another harness than the one asked for is named where people read.

2026-11-14: five `compose --start --owner codex` launches started on OpenCode after Codex hit its
usage limit; only the JSON receipt said so. The real selector (`dispatch-owner.py main`) runs here
with a stubbed usage reader, capacity gauge and wrapper (no model is called); its receipt feeds the
same `[경로]` card, decision lines and completion summary the runtime prints.
"""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = _load("owner_change_route", HERE / "capability-route.py")
OWNER = _load("owner_change_selector", HERE / "dispatch-owner.py")
import artifact_producer  # noqa: E402
import model_config  # noqa: E402
import model_profile  # noqa: E402
import work_intent  # noqa: E402

HARNESSES = ("claude", "codex", "opencode")
RESET = "2026-11-20T05:56:46Z"


def _when():
    return datetime.fromisoformat(RESET.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")


class OwnerChangeVisibleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.log"
        self.language = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"})
        self.language.start()

    def tearDown(self):
        self.language.stop()
        self.tmp.cleanup()

    def route(self, requested, supported=HARNESSES):
        candidates = [{"harness": h, "transport": "headless", "surface": "registered-headless",
                       "status": "supported" if h in supported else "unsupported",
                       "probe_source": "fixture", "probe_time": "2026-11-14T00:00:00Z"} for h in HARNESSES]
        route = R.compose_route(
            capability="autopilot-code", capability_mode="dev", shape="solo", graph=None,
            slug="owner-change-" + requested, cwd=R.ROOT, artifact_root=str(self.root), unassigned=True,
            dispatch_evidence=None, registered_headless_evidence={"candidates": candidates},
            selection_pins=R._parse_selection_pins([], requested), profile="deep",
            work_request={"text": "review the PR", "owner_harness": requested})
        path = self.root / f"{route['route_id']}.json"
        path.write_text(json.dumps(route), encoding="utf-8")
        return route, path

    def model(self, harness, profile):
        config, _ = model_config.resolve_config(harness, runtime=self.root / "no-user-config")
        return model_profile.resolve_profile_values(harness, config, profile)["model"]

    def select(self, route, path, requested, states, supported=HARNESSES):
        """The real selector's receipt; the wrapper stub prints the lines the wrapper prints."""
        def wrapper(argv, env):
            harness = Path(argv[0]).parents[1].name
            profile = argv[argv.index("--model-profile") + 1]
            print(f"check=ok\nadapter={harness}\nworker_type=owner\nowner_harness={harness}\n"
                  f"model_profile={profile}\nmodel={self.model(harness, profile)}")
            return 0
        context = {"harnesses": sorted(supported), "policy": route["owner_harness_policy"],
                   "allocation": route["dispatch_allocation"]}
        binding = SimpleNamespace(route_file=str(path), route_id=route["route_id"], route_hash=route["route_hash"],
                                  route_node="one-shot", registry_digest="sha256:fixture",
                                  write_scope="source-scoped", completion_gate="quick-complete")
        repository = SimpleNamespace(verify=lambda *_args: None)
        argv = ["--start", "--route-evidence", str(path), "--jobs", str(self.jobs), "--slug", "owner-change",
                "--attempt-id", "att-owner-change-" + requested, "--prompt-text", "review the PR",
                "--adapter", requested]
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(OWNER, "_authoritative_jobs", return_value=str(self.jobs)))
            stack.enter_context(mock.patch.object(OWNER, "_sealed_owner_context", return_value=context))
            stack.enter_context(mock.patch.object(OWNER, "_usage", return_value=dict(states)))
            stack.enter_context(mock.patch.object(OWNER._capacity, "capacity_report", return_value={
                "scores": dict.fromkeys(HARNESSES, 50.0), "sources": {}}))
            stack.enter_context(mock.patch.object(OWNER, "derive_quick_owner_binding", return_value=binding))
            stack.enter_context(mock.patch.object(artifact_producer, "prepare_route_artifact_env", return_value={}))
            stack.enter_context(mock.patch.object(OWNER.frame_repository, "for_route",
                                                  return_value=contextlib.nullcontext(repository)))
            stack.enter_context(mock.patch.object(OWNER, "run_owner_wrapper", side_effect=wrapper))
            stack.enter_context(contextlib.redirect_stdout(output))
            code = OWNER.main(argv)
        return [{"attempt_id": "att-owner-change-" + requested, "exit_code": code, "receipt": output.getvalue()}]

    def card(self, route, requested, launches):
        return R.compose_card(route, owner_harness=requested, include_decisions=False, launches=launches)

    def test_each_requested_harness_same_limited_and_missing(self):
        for requested in HARNESSES:
            for case in ("same", "limited", "not-installed"):
                with self.subTest(requested=requested, case=case):
                    supported = [h for h in HARNESSES if case != "not-installed" or h != requested]
                    states = dict.fromkeys(HARNESSES, "ok")
                    if case == "limited":
                        states[requested] = f"limited({RESET})"
                    route, path = self.route(requested, supported)
                    launches = self.select(route, path, requested, states, supported)
                    receipt = launches[0]["receipt"]
                    started = work_intent.launched_owner(launches).get("harness")
                    card = self.card(route, requested, launches)
                    tiers = next(line for line in R.compose_decision_lines(route) if "tiers:" in line)
                    self.assertIn("one-shot=deep", tiers)
                    if case == "not-installed":
                        # An owner the route's probe could not start is refused, not swapped silently.
                        self.assertNotEqual(launches[0]["exit_code"], 0, receipt)
                        self.assertIn("reason=explicit-adapter-outside-route-evidence", receipt)
                        self.assertIsNone(started)
                        self.assertNotIn("담당 변경", card)
                        continue
                    self.assertEqual(launches[0]["exit_code"], 0, receipt)
                    # The tier is kept on the harness that ran: the tiers line names what runs.
                    self.assertIn("model_profile=deep", receipt)
                    if case == "same":
                        self.assertEqual(started, requested)
                        self.assertNotIn("담당 변경", card)
                    else:
                        self.assertNotEqual(started, requested)
                        model = self.model(started, "deep")
                        self.assertIn(f"  담당 변경: 요청 {requested} → {started} {model}(deep) — "
                                      f"{requested} 사용 한도 소진({_when()} 재설정)", card)
                    # The per-harness advisory follows the owner that started.
                    self.assertEqual("Codex owner:" in card, started == "codex", card)

    def test_change_line_follows_the_screen_language(self):
        route, path = self.route("codex")
        launches = self.select(route, path, "codex", {"claude": "ok", "codex": f"limited({RESET})", "opencode": "ok"})
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}):
            card = self.card(route, "codex", launches)
        started = work_intent.launched_owner(launches)["harness"]
        self.assertIn(f"Owner changed: requested codex → {started} ", card)
        self.assertIn(f"codex usage limit reached (resets {_when()})", card)
        self.assertNotIn("Codex owner:", card)

    def test_todays_receipt_names_opencode_and_drops_the_codex_advisory(self):
        receipt = ("status=eligible\nadapter=opencode\nselection_source=configured-balanced\n"
                   "explicit_adapter=codex\neligibility.codex=limited(2026-11-20T05:56:46Z)\n"
                   "rejected.1=codex:usage-limited(2026-11-20T05:56:46Z)\nadapter=opencode\n"
                   "worker_type=owner\nowner_harness=opencode\nmodel_profile=deep\n"
                   "model=opencode-go/glm-5.3-flash\n")
        route, _path = self.route("codex")
        card = self.card(route, "codex", [{"attempt_id": "a", "exit_code": 0, "receipt": receipt}])
        self.assertIn(f"  담당 변경: 요청 codex → opencode opencode-go/glm-5.3-flash(deep) — "
                      f"codex 사용 한도 소진({_when()} 재설정)", card)
        self.assertNotIn("Codex owner:", card)
        # A tier the harness did not keep is said in the same line.
        lower = receipt.replace("model_profile=deep", "model_profile=balanced")
        card = self.card(route, "codex", [{"attempt_id": "a", "exit_code": 0, "receipt": lower}])
        self.assertIn("opencode-go/glm-5.3-flash(등급 deep→balanced)", card)

    def test_completion_summary_names_the_owner_only_when_it_differs(self):
        for requested in HARNESSES:
            route, _path = self.route(requested)
            for ran in HARNESSES:
                with self.subTest(requested=requested, ran=ran):
                    model = self.model(ran, "deep")
                    owner = {"harness": ran, "model": model, "model_profile": "deep"}
                    summary = work_intent.completion_summary(route, None, owner=owner)
                    line = f"담당: {ran} {model}(deep) — 요청({requested})과 다름"
                    if ran == requested:
                        self.assertNotIn("담당:", summary)
                    else:
                        self.assertIn(line, summary)
        self.assertNotIn("담당:", work_intent.completion_summary(route, None))

    def test_sealed_summary_reads_the_owner_row(self):
        import dispatch_completion_join
        import dispatch_owner_input
        import dispatch_terminal_commit as commit
        route, path = self.route("codex")
        slot = self.root / "slot"
        slot.mkdir()
        row = SimpleNamespace(metadata={"harness": "opencode", "model": "opencode-go/glm-5.3-flash",
                                        "model_profile": "deep"})
        request = SimpleNamespace(jobs=str(self.jobs), owner_attempt_id="att-owner", route_file=str(path))
        with mock.patch.object(dispatch_completion_join, "exact_attempt_row", return_value=row), \
                mock.patch.object(dispatch_owner_input, "completion_corrections", return_value={}), \
                mock.patch.object(commit, "_completion_cost", return_value=None):
            commit._seal_completion_summary(request, route, None, slot)
        text = (slot / work_intent.SUMMARY_NAME).read_text(encoding="utf-8")
        self.assertIn("담당: opencode opencode-go/glm-5.3-flash(deep) — 요청(codex)과 다름", text)


if __name__ == "__main__":
    unittest.main()
