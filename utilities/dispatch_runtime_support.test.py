"""SD-120/121 activation as checked support (PRD §13.53.2).

Before this cycle the route hardcoded `runtime_support.terminal_commit=False`
with no surface that could ever set it, so the Claude adapter never passed
`--enable-terminal-commit` and the live fast path was unreachable -- which is
also why A49-14 could not be run at all. §13.53.2 requires activation to be
sealed as checked route/runtime capability rather than a switch an owner has
to remember. These fixtures pin that the seal is fail-closed in every
direction and that no operator value can force it open on a runtime that does
not publish the contract.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import shutil
import tempfile
import unittest
from pathlib import Path

import dispatch_runtime_support as S

ROOT = Path(__file__).resolve().parents[1]


def _stage_complete_runtime(destination: Path) -> Path:
    """Copy exactly the modules the census requires into a fake runtime root."""
    for relative, _ in S.REQUIRED_SURFACES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    return destination


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _stage_complete_runtime(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_runtime_publishing_every_surface_is_supported(self):
        verdict = S.probe_runtime(self.root)
        self.assertTrue(verdict.supported, verdict)
        self.assertEqual(verdict["reason"], "runtime-contract-complete")
        self.assertEqual(verdict["missing"], [])

    def test_this_checkout_publishes_the_whole_contract(self):
        """The census must agree with the tree it was written against; a drift
        here means a required surface was renamed without updating the table."""
        self.assertTrue(S.probe_runtime(ROOT).supported, S.probe_runtime(ROOT))

    def test_each_missing_module_closes_the_gate_and_is_named(self):
        for relative, _ in S.REQUIRED_SURFACES:
            with self.subTest(module=relative):
                with tempfile.TemporaryDirectory() as tmp:
                    root = _stage_complete_runtime(Path(tmp))
                    (root / relative).unlink()
                    verdict = S.probe_runtime(root)
                    self.assertFalse(verdict.supported)
                    self.assertEqual(verdict["reason"], "runtime-contract-incomplete")
                    self.assertIn(relative, verdict["missing"])

    def test_each_missing_symbol_closes_the_gate_and_is_named(self):
        for relative, symbols in S.REQUIRED_SURFACES:
            for symbol in symbols:
                with self.subTest(module=relative, symbol=symbol):
                    with tempfile.TemporaryDirectory() as tmp:
                        root = _stage_complete_runtime(Path(tmp))
                        target = root / relative
                        source = target.read_text(encoding="utf-8")
                        # Rename only the *definition*; call sites may remain,
                        # which is exactly the "surface silently dropped"
                        # shape the census has to catch.
                        # Cover every top-level binding form the census reads:
                        # def / class (with or without bases) / plain and
                        # annotated assignment.
                        for form in (f"def {symbol}(", f"class {symbol}(", f"class {symbol}:",
                                     f"\n{symbol} =", f"\n{symbol}: "):
                            source = source.replace(form, form.replace(symbol, symbol + "_gone", 1))
                        target.write_text(source, encoding="utf-8")
                        verdict = S.probe_runtime(root)
                        self.assertFalse(verdict.supported, f"{relative}:{symbol} went undetected")
                        self.assertIn(f"{relative}:{symbol}", verdict["missing"])

    def test_unreadable_and_unparsable_roots_fail_closed(self):
        self.assertFalse(S.probe_runtime(None).supported)
        self.assertEqual(S.probe_runtime(None)["reason"], "runtime-root-unknown")
        self.assertFalse(S.probe_runtime("").supported)
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-such-root"
            self.assertFalse(S.probe_runtime(missing).supported)
            self.assertEqual(S.probe_runtime(missing)["reason"], "runtime-root-unreadable")
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_complete_runtime(Path(tmp))
            broken = root / S.REQUIRED_SURFACES[0][0]
            broken.write_text("def (:\n", encoding="utf-8")
            verdict = S.probe_runtime(root)
            self.assertFalse(verdict.supported)
            self.assertIn(S.REQUIRED_SURFACES[0][0], verdict["missing"])

    def test_a_file_that_is_a_directory_is_not_a_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _stage_complete_runtime(Path(tmp))
            relative = S.REQUIRED_SURFACES[0][0]
            (root / relative).unlink()
            (root / relative).mkdir()
            self.assertFalse(S.probe_runtime(root).supported)


class OperatorSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _stage_complete_runtime(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_config_selects_auto_and_the_census_decides(self):
        for config in (None, {}, {"runtime": {}}, {"runtime": "not-a-mapping"}):
            self.assertEqual(S.query_support_mode(config), "auto",
                             "an absent key must leave the census in charge")
        self.assertTrue(S.terminal_commit_support(self.root, None).supported)

    def test_off_closes_the_gate_on_a_complete_runtime(self):
        verdict = S.terminal_commit_support(self.root, {"runtime": {"terminal_commit": "off"}})
        self.assertFalse(verdict.supported)
        self.assertEqual(verdict["reason"], "operator-disabled")

    def test_no_operator_value_can_force_an_incomplete_runtime_open(self):
        """`off` is the only non-default value: there is deliberately no `on`,
        so config can never override a runtime that lacks the contract."""
        self.assertNotIn("on", S.SUPPORT_MODES)
        self.assertNotIn("true", S.SUPPORT_MODES)
        with tempfile.TemporaryDirectory() as tmp:
            incomplete = _stage_complete_runtime(Path(tmp))
            (incomplete / S.REQUIRED_SURFACES[0][0]).unlink()
            for value in ("auto", "off", "on", True, "forced", None):
                verdict = S.terminal_commit_support(incomplete, {"runtime": {"terminal_commit": value}})
                self.assertFalse(verdict.supported, f"{value!r} opened an incomplete runtime")

    def test_any_unrecognised_present_value_fails_closed(self):
        """The safe direction is the legacy path. In particular a YAML 1.1
        reader resolves a bare `off` to boolean `False`; that must stay
        disabled rather than falling back to `auto` and silently opening a
        gate the operator closed."""
        for value in (False, "off", "OFF", " off ", "no", "ON", "true", 1, [], {"nested": True}, None):
            with self.subTest(value=value):
                self.assertEqual(
                    S.query_support_mode({"runtime": {"terminal_commit": value}}), "off")

    def test_only_an_exact_auto_keeps_the_census_in_charge(self):
        for value in ("auto", "AUTO", " Auto "):
            self.assertEqual(S.query_support_mode({"runtime": {"terminal_commit": value}}), "auto")

    def test_a_boolean_false_disables_a_complete_runtime(self):
        verdict = S.terminal_commit_support(self.root, {"runtime": {"terminal_commit": False}})
        self.assertFalse(verdict.supported)
        self.assertEqual(verdict["reason"], "operator-disabled")


class ContractNameTests(unittest.TestCase):
    def test_contract_names_match_what_the_route_publishes(self):
        self.assertEqual(S.TERMINAL_COMMIT_CONTRACT, "terminal_commit_v1")
        self.assertEqual(S.TERMINAL_HANDOFF_CONTRACT, "terminal_handoff_claim_v1")
        self.assertEqual(S.PRODUCER_BINDING_CONTRACT, "producer_binding_v1")

    def test_the_lock_order_table_is_a_required_surface(self):
        """§13.53.4(3): the fence contract may not be claimed before the table
        is registered, so activation must depend on the table existing."""
        modules = [relative for relative, _ in S.REQUIRED_SURFACES]
        self.assertIn("utilities/dispatch_lock_order.py", modules)


if __name__ == "__main__":
    unittest.main()
