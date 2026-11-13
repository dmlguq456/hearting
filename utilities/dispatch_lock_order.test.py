"""A82-5: the canonical lock-order table and its acquire/release boundaries.

PRD §13.53.4(3) requires the implementation cycle to *register* a canonical
total order plus explicit acquire/release API, and states that until the table
is registered the fence contract may not be claimed at all. These fixtures are
that registration's acceptance: the declared order, the refusal of every
non-ascending acquisition, and the typed (not timed-out) refusal of a
re-entrant `finalize()`.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import ast
import tempfile
import time
import unittest
from pathlib import Path

import artifact_producer
import dispatch_lock_order as L

ROOT = Path(__file__).resolve().parents[1]
SEARCH_ROOTS = (ROOT / "utilities", ROOT / "adapters", ROOT / "hooks", ROOT / "tools")


def _python_sources(roots):
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if ".test." in path.name or path.name.startswith("test_"):
                continue
            yield path


def _declared_lock_names(paths):
    """Every literal lock name passed to enter/leave/acquired/assert_* anywhere."""
    names = set()
    for path in (paths if not isinstance(paths, tuple) else _python_sources(paths)):
        try:
            tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            value = node.func.value
            if not (isinstance(value, ast.Name) and value.id == "dispatch_lock_order"):
                continue
            if node.func.attr not in {"enter", "leave", "acquired",
                                      "assert_held", "assert_not_held"}:
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                names.add(node.args[0].value)
    return names


class LockOrderTableTests(unittest.TestCase):
    def setUp(self):
        L.reset_for_test()

    def tearDown(self):
        L.reset_for_test()

    def test_registered_table_is_a_strict_total_order(self):
        ranks = [rank for rank, _, _ in L.LOCK_ORDER]
        names = [name for _, name, _ in L.LOCK_ORDER]
        self.assertEqual(ranks, sorted(ranks), "table rows must be listed in acquisition order")
        self.assertEqual(len(set(ranks)), len(ranks), "two locks may not share a rank")
        self.assertEqual(len(set(names)), len(names), "lock names must be unique")
        self.assertEqual(L.RANK, {name: rank for rank, name, _ in L.LOCK_ORDER})
        # The established node -> jobs order remains, with the shared delivery
        # transition inserted before producer admission.
        self.assertEqual(names[:4], ["node-completion", "jobs",
                                     "delivery-reference", "delivery-record"])
        self.assertLess(L.RANK["delivery-record"], L.RANK["producer-admission"])
        for row in L.table():
            self.assertTrue(row["description"], f"{row['lock']} must carry a description")

    def test_ascending_acquisition_is_allowed_and_tracked(self):
        with L.acquired("jobs"):
            self.assertEqual(L.held(), ("jobs",))
            with L.acquired("delivery-reference"):
                with L.acquired("delivery-record"):
                    self.assertEqual(L.held(), (
                        "jobs", "delivery-reference", "delivery-record"))
            self.assertEqual(L.held(), ("jobs",))
        self.assertEqual(L.held(), ())

    def test_every_non_ascending_acquisition_is_refused_typed(self):
        names = [name for _, name, _ in L.LOCK_ORDER]
        for held in names:
            for target in names:
                if L.RANK[target] > L.RANK[held]:
                    continue
                L.reset_for_test()
                L.enter(held)
                with self.assertRaises(L.LockOrderError) as caught:
                    L.enter(target)
                expected = "lock-reentry-forbidden" if target == held else "lock-order-violation"
                self.assertEqual(caught.exception.code, expected, f"{held} -> {target}")
                self.assertEqual(L.held(), (held,), "a refused acquisition must not mutate the stack")

    def test_reentry_is_refused_immediately_not_after_a_lock_timeout(self):
        """The point of the typed refusal: a re-entrant acquisition used to
        block on `flock` for the full admission timeout and surface as
        `admission lock busy`, which reads as load rather than a contract
        violation."""
        L.enter("producer-admission")
        started = time.monotonic()
        with self.assertRaises(L.LockOrderError) as caught:
            L.enter("producer-admission")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(caught.exception.code, "lock-reentry-forbidden")

    def test_release_discipline_and_unregistered_names_are_typed(self):
        L.enter("jobs")
        with self.assertRaises(L.LockOrderError) as out_of_order:
            L.leave("producer-admission")
        self.assertEqual(out_of_order.exception.code, "lock-release-out-of-order")
        self.assertEqual(L.held(), ("jobs",))
        L.leave("jobs")
        for call in (lambda: L.enter("not-a-lock"), lambda: L.leave("not-a-lock")):
            with self.assertRaises(L.LockOrderError) as unknown:
                call()
            self.assertEqual(unknown.exception.code, "lock-unregistered")

    def test_assert_helpers_name_the_reverify_point(self):
        L.enter("jobs")
        L.assert_held("jobs", "mutation")
        L.assert_not_held("producer-admission", "producer-finalize")
        with self.assertRaises(L.LockOrderError) as not_held:
            L.assert_held("producer-admission", "mutation")
        self.assertEqual(not_held.exception.code, "lock-not-held")
        L.enter("producer-admission")
        with self.assertRaises(L.LockOrderError) as held:
            L.assert_not_held("producer-admission", "producer-finalize")
        self.assertEqual(held.exception.code, "lock-reentry-forbidden")


class FinalizeReentryTests(unittest.TestCase):
    def setUp(self):
        L.reset_for_test()

    def tearDown(self):
        L.reset_for_test()

    def test_finalize_while_holding_producer_admission_is_typed_with_zero_mutation(self):
        """§13.53.4(3): the table names producer-admission as the lock that must
        be released before `finalize()`. Re-entering it is refused before any
        filesystem effect, so a violation costs nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = sorted(p.name for p in root.iterdir())
            L.enter("producer-admission")
            with self.assertRaises(artifact_producer.ProducerError) as caught:
                artifact_producer.finalize(root, cycle_id="cyc_" + "a" * 32)
            self.assertEqual(caught.exception.code, "finalize-reentry-forbidden")
            self.assertEqual(caught.exception.detail, "producer-finalize:producer-admission")
            self.assertEqual(sorted(p.name for p in root.iterdir()), before,
                             "a refused re-entry must leave the root untouched")

    def test_explicit_lock_fd_reentry_stays_refused(self):
        """The pre-existing guard for a caller that *passes* its fd must keep
        working; the new stack check covers the caller that does not."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(artifact_producer.ProducerError) as caught:
                artifact_producer.finalize(Path(tmp), cycle_id="cyc_" + "a" * 32,
                                           _admission_lock_fd=3)
            self.assertEqual(caught.exception.code, "finalize-reentry-forbidden")


class WiredAcquisitionSiteTests(unittest.TestCase):
    """The table only constrains the code that declares against it, so pin the
    real acquisition sites: an unwired lock would be invisible to the order."""

    def _assigned_and_called(self, relative):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            value = node.func.value
            if isinstance(value, ast.Name) and value.id == "dispatch_lock_order":
                argument = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
                calls.append((node.func.attr, argument))
        return calls

    def test_every_row_is_classified(self):
        """A rank nothing declares constrains nothing. `node-completion` is
        ordered by the contract but lives across a subprocess boundary, so it
        must be named as a precheck rather than implied to be enforced."""
        self.assertEqual(L.CROSS_PROCESS_PRECHECK, frozenset({"node-completion"}))
        for name in L.CROSS_PROCESS_PRECHECK:
            self.assertIn(name, L.RANK, "a precheck stage still holds its place in the order")
            self.assertIn("CROSS-PROCESS", L.DESCRIPTION[name])
        declared = set(L.DECLARED_IN_PROCESS) | set(L.CROSS_PROCESS_PRECHECK)
        self.assertEqual(declared, set(L.RANK), "every row must be classified")
        self.assertTrue(L.ENFORCED_COMPLETE <= set(L.DECLARED_IN_PROCESS))

    def test_declared_in_process_names_are_actually_declared_somewhere(self):
        """Measure the declaration sites, not the constant.

        The previous version of this fixture compared `DECLARED_IN_PROCESS` to
        a tuple literal, so it happily pinned a name that no code declared --
        which is exactly what had happened to `terminal-commit-state`."""
        declared = _declared_lock_names(SEARCH_ROOTS)
        for name in L.DECLARED_IN_PROCESS:
            self.assertIn(name, declared,
                          f"{name} is listed as declared in-process but nothing calls "
                          f"enter/acquired for it")
        for name in L.CROSS_PROCESS_PRECHECK:
            self.assertNotIn(name, declared,
                             f"{name} is now declared in-process; move it out of "
                             f"CROSS_PROCESS_PRECHECK instead of leaving the table lying")

    def test_enforced_complete_ranks_have_no_undeclared_writer(self):
        """Count from the acquisition side.

        A rank is only complete if every module that opens its physical lock
        file declares the rank. `artifact_restore_sealed` re-implements the
        admission acquisition against the identical path, and counting
        declarations alone would never have shown that."""
        for name in sorted(L.ENFORCED_COMPLETE):
            lock_file = L.ENFORCED_LOCK_FILES[name]
            undeclared = []
            for path in _python_sources(SEARCH_ROOTS):
                # The table module names the locks it governs; it acquires none.
                if path.name == "dispatch_lock_order.py":
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if lock_file not in text or "flock" not in text:
                    continue
                if name not in _declared_lock_names([path]):
                    undeclared.append(str(path.relative_to(ROOT)))
            self.assertEqual(undeclared, [],
                             f"{name} is claimed complete but these modules take "
                             f"{lock_file} without declaring it: {undeclared}")

    def test_producer_admission_declares_both_boundaries(self):
        calls = self._assigned_and_called("utilities/artifact_admission.py")
        self.assertIn(("enter", "producer-admission"), calls)
        self.assertIn(("leave", "producer-admission"), calls)

    def test_jobs_lock_declares_its_rank(self):
        calls = self._assigned_and_called("utilities/dispatch_terminal_commit.py")
        self.assertIn(("acquired", "jobs"), calls)

    def test_delivery_reference_and_record_locks_declare_their_order(self):
        reference_calls = self._assigned_and_called("utilities/dispatch_attempt_reference.py")
        record_calls = self._assigned_and_called("utilities/dispatch_pending_delivery.py")
        self.assertIn(("acquired", "delivery-reference"), reference_calls)
        self.assertIn(("acquired", "delivery-record"), record_calls)

    def test_finalize_declares_the_release_before_boundary(self):
        calls = self._assigned_and_called("utilities/artifact_producer_finalize.py")
        self.assertIn(("assert_not_held", "producer-admission"), calls)
        self.assertEqual(L.RELEASE_BEFORE_FINALIZE, ("producer-admission",))


if __name__ == "__main__":
    unittest.main()
