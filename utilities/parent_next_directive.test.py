#!/usr/bin/env python3
"""Contract tests for the receipt's `parent_next` directive."""
import ast
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import harness_capabilities as HC  # noqa: E402
import parent_next_directive as pnd  # noqa: E402


ATTEMPT = "att-0123456789abcdef0123456789abcdef"
WATCH = "4f35a133b4a50a94"
WRAPPERS = (
    "adapters/claude/bin/dispatch-headless.py",
    "adapters/codex/bin/dispatch-headless.py",
    "adapters/opencode/bin/dispatch-headless.py",
)


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` assignments."""
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = node.value.value
    return found


def _resolver_return_values(source_path: Path) -> set[str]:
    """Every value `resolve_parent_completion_delivery` can return, from source.

    Read independently of `parent_next_directive`'s own tables: a table that
    classifies itself proves nothing about a wrapper that grew a new return.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    constants = _string_constants(tree)
    # A wrapper may return a constant it imported (e.g. MANAGED_PARENT_DELIVERY).
    # Resolve those only from the modules this wrapper actually imports, so an
    # unrelated utility that happens to share a constant name cannot answer for
    # it (review round 2, minor 1).
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        module = ROOT / "utilities" / f"{node.module}.py"
        if not module.is_file():
            continue
        wanted = {alias.name for alias in node.names}
        try:
            imported = ast.parse(module.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for name, value in _string_constants(imported).items():
            if name in wanted:
                constants.setdefault(name, value)
    resolver = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "resolve_parent_completion_delivery"
        ),
        None,
    )
    assert resolver is not None, f"resolver not found in {source_path}"
    values: set[str] = set()
    for node in ast.walk(resolver):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            values.add(node.value.value)
        elif isinstance(node.value, ast.Name):
            # An unresolvable name must fail loudly rather than be skipped:
            # a silently ignored return is exactly the drift this test exists
            # to catch.
            assert node.value.id in constants, (
                f"{source_path}: unresolved delivery return {node.value.id}"
            )
            values.add(constants[node.value.id])
        elif (isinstance(node.value, ast.Call)
              and isinstance(node.value.func, ast.Attribute)
              and isinstance(node.value.func.value, ast.Name)
              and node.value.func.attr == "resolve_parent_completion_delivery"):
            # Follow the actual imported shared resolver; retain fail-closed
            # handling for unknown calls or newly introduced return forms.
            # Since PR #303 a wrapper delegates to dispatch_wrapper_common,
            # whose own resolver follows through to dispatch_parent_completion;
            # recursion re-runs this same check on either module.
            alias = node.value.func.value.id
            imports = {item.asname or item.name: item.name
                       for statement in tree.body if isinstance(statement, ast.Import)
                       for item in statement.names}
            module_name = imports.get(alias)
            assert module_name in ("dispatch_parent_completion", "dispatch_wrapper_common"), source_path
            values.update(_resolver_return_values(ROOT / "utilities" / f"{module_name}.py"))
        elif (isinstance(node.value, ast.Subscript)
              and isinstance(node.value.slice, ast.Constant)
              and node.value.slice.value == "carrier"):
            # A carrier an adapter declares (`harness_capabilities`): every
            # declared one is a value the resolver can return.
            values.update(HC.declared_carriers())
        else:
            raise AssertionError(f"{source_path}: unsupported return expression")
    return values


class ParentNextTest(unittest.TestCase):
    def test_every_carrier_delivery_ends_the_turn_without_a_command(self):
        for delivery in pnd.CARRIER_DELIVERIES:
            with self.subTest(delivery=delivery):
                action, reason, command = pnd.parent_next(
                    delivery, ATTEMPT, agent_home=ROOT
                )
                self.assertEqual(action, pnd.NEXT_END_TURN)
                self.assertEqual(reason, pnd.CARRIER_DELIVERIES[delivery])
                # A carrier receipt must not hand the model a wait command:
                # that is the exact behaviour the carrier contract forbids.
                self.assertEqual(command, "")

    def test_poll_fallback_authorizes_one_bounded_wait(self):
        action, reason, command = pnd.parent_next(
            "poll-fallback", ATTEMPT, agent_home=ROOT
        )
        self.assertEqual(action, pnd.NEXT_BOUNDED_WAIT)
        self.assertEqual(reason, "explicit-poll-fallback")
        self.assertEqual(
            command,
            f"{ROOT}/utilities/dispatch-wait.sh --attempt-id {ATTEMPT} --max 600",
        )

    def test_the_printed_wait_command_is_actually_executable(self):
        """A printed command that is not on any PATH is a lost completion."""
        _, _, command = pnd.parent_next("poll-fallback", ATTEMPT, agent_home=ROOT)
        entry = Path(command.split(" ", 1)[0])
        self.assertTrue(entry.is_file(), command)
        self.assertTrue(os.access(entry, os.X_OK), command)

    def test_unknown_delivery_fails_closed_to_a_wait(self):
        for delivery in ("", "   ", "invented-carrier", None):
            with self.subTest(delivery=delivery):
                action, reason, _ = pnd.parent_next(delivery, ATTEMPT, agent_home=ROOT)
                self.assertEqual(action, pnd.NEXT_BOUNDED_WAIT)
                self.assertEqual(reason, "delivery-unrecognized")

    def test_missing_attempt_or_root_yields_no_command_but_still_waits(self):
        for attempt in (None, "", "-"):
            with self.subTest(attempt=attempt):
                action, reason, command = pnd.parent_next(
                    "poll-fallback", attempt, agent_home=ROOT
                )
                self.assertEqual(action, pnd.NEXT_BOUNDED_WAIT)
                self.assertEqual(reason, "explicit-poll-fallback-attempt-unknown")
                self.assertEqual(command, "")
        action, reason, command = pnd.parent_next("poll-fallback", ATTEMPT)
        self.assertEqual(action, pnd.NEXT_BOUNDED_WAIT)
        self.assertEqual(reason, "explicit-poll-fallback-entrypoint-unknown")
        self.assertEqual(command, "")

    def test_receipt_lines_are_three_ordered_key_values(self):
        lines = pnd.receipt_lines("claude-parent-runtime", ATTEMPT, agent_home=ROOT)
        self.assertEqual(
            lines,
            [
                "parent_next=end-turn",
                "parent_next_reason=carrier-claude-async-rewake",
                "parent_next_command=-",
            ],
        )
        wait_lines = pnd.receipt_lines("poll-fallback", ATTEMPT, agent_home=ROOT)
        self.assertEqual(wait_lines[0], "parent_next=bounded-wait")
        self.assertIn("utilities/dispatch-wait.sh", wait_lines[2])

    def test_next_values_are_exactly_the_two_documented_words(self):
        self.assertEqual(pnd.NEXT_VALUES, {"end-turn", "bounded-wait"})

    def test_every_recipient_kind_is_classified(self):
        import dispatch_pending_delivery

        unclassified = (
            set(dispatch_pending_delivery.RECIPIENT_KINDS)
            - set(pnd.CARRIER_DELIVERIES)
            - set(pnd.WAIT_DELIVERIES)
        )
        self.assertEqual(unclassified, set())

    def test_every_wrapper_resolver_return_value_is_classified(self):
        """Drift guard read from the wrappers, not from this module's tables."""
        classified = set(pnd.CARRIER_DELIVERIES) | set(pnd.WAIT_DELIVERIES)
        for wrapper in WRAPPERS:
            with self.subTest(wrapper=wrapper):
                values = _resolver_return_values(ROOT / wrapper)
                self.assertTrue(values, wrapper)
                self.assertEqual(values - classified, set(), sorted(values))

    def test_the_supervisor_default_is_a_carrier_not_a_fallback(self):
        """`parent-runtime-supervised` is every wrapper's default return.

        Pinned by name: if it ever drops out of the carrier table, the receipt
        starts telling ordinary registered owners to poll.
        """
        self.assertEqual(
            pnd.parent_next("parent-runtime-supervised", ATTEMPT, agent_home=ROOT),
            (pnd.NEXT_END_TURN, "carrier-session-supervisor", ""),
        )


class StewardNextTest(unittest.TestCase):
    def test_hook_wake_is_the_only_carrier(self):
        self.assertEqual(
            pnd.steward_next("hook", WATCH, agent_home=ROOT),
            (pnd.NEXT_END_TURN, "carrier-steward-watch", ""),
        )

    def test_a_codex_queue_wake_is_a_carrier_on_the_armed_line_only(self):
        self.assertEqual(
            pnd.steward_next("queue", WATCH, agent_home=ROOT),
            (pnd.NEXT_END_TURN, "carrier-steward-watch-codex-queue", ""),
        )
        action, reason, _command = pnd.steward_next("queue", WATCH, agent_home=ROOT,
                                                    arms_hook=False)
        self.assertEqual((action, reason), (pnd.NEXT_BOUNDED_WAIT, "steward-line-does-not-arm"))

    def test_a_watch_without_a_carrier_waits_on_its_own_surface(self):
        # `--wake none`, and `--wake auto` resolved outside a Claude session,
        # both arm a watcher that no hook will ever deliver.
        for wake in ("none", "", None, "auto", "invented"):
            with self.subTest(wake=wake):
                action, reason, command = pnd.steward_next(
                    wake, WATCH, agent_home=ROOT
                )
                self.assertEqual(action, pnd.NEXT_BOUNDED_WAIT)
                self.assertTrue(reason.startswith("steward-wake-"), reason)
                self.assertEqual(
                    command,
                    f"python3 {ROOT}/utilities/peer-steward.py join {WATCH}"
                    f" --timeout {pnd.WAIT_MAX_SECONDS * 1000}",
                )
                # Never the dispatch attempt wait: it takes no watch target.
                self.assertNotIn("dispatch-wait", command)

    def test_missing_watch_or_root_still_refuses_to_end_the_turn(self):
        for watch_id in (None, "", "-"):
            with self.subTest(watch_id=watch_id):
                action, _, command = pnd.steward_next("none", watch_id, agent_home=ROOT)
                self.assertEqual(action, pnd.NEXT_BOUNDED_WAIT)
                self.assertEqual(command, "")
        action, _, command = pnd.steward_next("none", WATCH)
        self.assertEqual(action, pnd.NEXT_BOUNDED_WAIT)
        self.assertEqual(command, "")

    def test_a_line_that_arms_no_hook_never_claims_a_carrier(self):
        """`rearm` prints from the session; no hook arms from that line."""
        action, reason, command = pnd.steward_next(
            "hook", WATCH, agent_home=ROOT, arms_hook=False
        )
        self.assertEqual(action, pnd.NEXT_BOUNDED_WAIT)
        self.assertEqual(reason, "steward-line-does-not-arm")
        self.assertIn(f"peer-steward.py join {WATCH}", command)

    def test_every_steward_wait_command_is_bounded(self):
        """herdr waits forever without `--timeout`; `bounded-wait` must bind."""
        for wake, arms in (("none", True), ("hook", False), ("auto", True)):
            with self.subTest(wake=wake, arms_hook=arms):
                _, _, command = pnd.steward_next(
                    wake, WATCH, agent_home=ROOT, arms_hook=arms
                )
                self.assertIn(f"--timeout {pnd.WAIT_MAX_SECONDS * 1000}", command)

    def test_an_explicit_watch_timeout_is_carried_into_the_command(self):
        _, _, command = pnd.steward_next(
            "none", WATCH, agent_home=ROOT, timeout_ms=90_000
        )
        # The join outlives the watch's own deadline by the declared margin.
        self.assertTrue(
            command.endswith(f"--timeout {90_000 + pnd.JOIN_SLACK_MS}"), command
        )
        _, _, capped = pnd.steward_next(
            "none", WATCH, agent_home=ROOT, timeout_ms=21_540_000
        )
        self.assertTrue(
            capped.endswith(f"--timeout {pnd.WAIT_MAX_SECONDS * 1000}"), capped
        )

    def test_steward_fields_render_inline(self):
        text = pnd.steward_fields("hook", WATCH, agent_home=ROOT)
        self.assertEqual(
            text,
            "parent_next=end-turn parent_next_reason=carrier-steward-watch "
            "parent_next_command=-",
        )


class PrintedCommandHomeTest(unittest.TestCase):
    """A printed command names the installed-release pointer, never the release that printed it."""

    def setUp(self):
        inherited = dict(os.environ)
        for key in tuple(os.environ):
            if key.startswith(("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_", "AGENT_THREAD_")):
                os.environ.pop(key, None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(inherited)))
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        data = Path(self._tmp.name) / "share" / "hearting"
        for version in ("v1.0.1", "v1.0.2"):
            (data / "releases" / version / "core").mkdir(parents=True)
            (data / "releases" / version / "core" / "CORE.md").write_text("x\n")
        self.pointer = data / "current"
        self.pointer.symlink_to(data / "releases" / "v1.0.2")
        self.old = data / "releases" / "v1.0.1"
        self.projection = Path(self._tmp.name) / "projection"
        self.projection.symlink_to(data / "releases" / "v1.0.1")

    def test_a_managed_release_prints_as_the_current_pointer(self):
        for root in (self.old, self.old.resolve(), self.projection, self.pointer):
            with self.subTest(root=str(root)):
                self.assertEqual(pnd.command_home(root), str(self.pointer))
        self.assertEqual(pnd.entrypoint(self.old, "utilities/x.py"), f"{self.pointer}/utilities/x.py")
        self.assertIn(f"{self.pointer}/utilities/dispatch-wait.sh",
                      pnd.wait_command(ATTEMPT, agent_home=self.old))

    def test_a_checkout_or_unknown_root_prints_as_given(self):
        self.assertEqual(pnd.command_home(ROOT), str(ROOT))
        self.assertEqual(pnd.command_home(""), "")
        self.pointer.unlink()  # a release whose pointer is gone has nothing better to name
        self.assertEqual(pnd.command_home(self.old), str(self.old))

    def test_resume_and_correction_name_the_registry_only_when_it_is_not_the_default(self):
        state = Path(self._tmp.name) / "state"
        default = state / "dispatch" / "jobs.log"
        other = Path(self._tmp.name) / "other" / "jobs.log"
        route = Path(self._tmp.name) / "route.json"
        with unittest.mock.patch.dict(os.environ, {"HARNESS_STATE_ROOT": str(state)}):
            resume = pnd.resume_command(route, default, agent_home=self.old)
            self.assertEqual(resume.split()[1:], [f"{self.pointer}/utilities/capability-route.py",
                                                  "start", "--route", str(route)])
            self.assertIn(f"--jobs {other}", pnd.resume_command(route, other, agent_home=self.old))
            correction = pnd.correction_command("att-1", default, agent_home=self.old)
            self.assertEqual(correction.split()[1:], [f"{self.pointer}/utilities/capability-route.py",
                                                      "correct", "--attempt-id", "att-1"])
            self.assertIn(f"correct --jobs {other} --attempt-id att-1",
                          pnd.correction_command("att-1", other, agent_home=self.old))
        # A canonical route record is named by its ID; any other route file by its path.
        canonical = Path(self._tmp.name) / ".agent_reports" / ".runtime" / "routes" / ("rt-" + "a" * 16 + ".json")
        with unittest.mock.patch.dict(os.environ, {"HARNESS_STATE_ROOT": str(state)}):
            self.assertEqual(pnd.resume_command(canonical, default, agent_home=self.old).split()[-2:],
                             ["--route", "rt-" + "a" * 16])
        # A bare start reads AGENT_DISPATCH_JOBS first: a different one keeps --jobs named.
        with unittest.mock.patch.dict(os.environ, {"HARNESS_STATE_ROOT": str(state),
                                                   "AGENT_DISPATCH_JOBS": str(other)}):
            self.assertIn(f"--jobs {default}", pnd.resume_command(route, default, agent_home=self.old))
        with unittest.mock.patch.dict(os.environ, {"HARNESS_STATE_ROOT": str(state),
                                                   "AGENT_DISPATCH_JOBS": str(default)}):
            self.assertNotIn("--jobs", pnd.resume_command(route, default, agent_home=self.old))

    def test_completion_notices_name_the_installed_release(self):
        import dispatch_completion_join as join
        with unittest.mock.patch("parent_next_directive.command_home", return_value=str(self.pointer)):
            finish = join.completion_harvest_command("att-x", "finish-workflow", jobs="/j/jobs.log", surface="-")
        self.assertIn(f"{self.pointer}/utilities/dispatch_terminal_commit.py finish", finish)


if __name__ == "__main__":
    unittest.main()
