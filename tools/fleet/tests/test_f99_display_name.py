#!/usr/bin/env python3
"""F-99 — canonical session display name.

Pure-fixture coverage for `session_handle.display_name()`/`resolve_display_inputs()`,
plus the additive `Session.runtime_name` --json field (Amendment 1 / R2-1) and the
Fleet render-side wiring (`_session_name`/`_session_name_companion`).
"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render                                          # noqa: E402
from fleet.model import Session                                   # noqa: E402
from fleet.session_handle import display_name, resolve_display_inputs, session_handle  # noqa: E402


class _HermeticStateRoot(unittest.TestCase):
    """Points AGENT_HOME/HOME at a fresh temp dir and clears every dispatch-state
    override so the hearting session-name registry (②) never touches real state."""

    def setUp(self):
        self._old_environ = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["AGENT_HOME"] = self.tmp.name
        os.environ["HOME"] = self.tmp.name
        for key in ("CLAUDE_HOME", "AGENT_DISPATCH_JOBS", "XDG_STATE_HOME", "HARNESS_STATE_ROOT"):
            os.environ.pop(key, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_environ)
        self.tmp.cleanup()

    def _write_pid_record(self, pid, name, name_source):
        sessions_dir = os.path.join(self.tmp.name, "sessions")
        os.makedirs(sessions_dir, exist_ok=True)
        with open(os.path.join(sessions_dir, "%d.json" % pid), "w", encoding="utf-8") as fh:
            json.dump({"name": name, "nameSource": name_source}, fh)

    def _registry_dir(self, harness):
        d = os.path.join(self.tmp.name, ".local", "state", "hearting", "dispatch",
                         "session-names", harness)
        os.makedirs(d, exist_ok=True)
        return d

    def _write_registry_name(self, harness, session_id, name):
        with open(os.path.join(self._registry_dir(harness), "%s.json" % session_id),
                  "w", encoding="utf-8") as fh:
            json.dump({"name": name, "set_at": 1.0}, fh)


class DisplayNamePrecedenceTest(unittest.TestCase):
    """Pure `display_name()` — no I/O, precedence ① runtime_name → ② title →
    registry_name → slug → cwd basename. Never a `CL/`/`CX/`/`OC/` sid8 handle."""

    def test_runtime_name_wins_over_everything(self):
        self.assertEqual(
            display_name("claude", "sid-1", runtime_name="custom", registry_name="hearting-fb",
                         title="AI title", slug="slug", cwd="/w/repo"),
            "custom")

    def test_title_wins_when_no_runtime_name(self):
        self.assertEqual(
            display_name("claude", "sid-1", runtime_name=None, registry_name="hearting-fb",
                         title="AI title", slug="slug", cwd="/w/repo"),
            "AI title")

    def test_registry_name_wins_when_no_runtime_name_or_title(self):
        self.assertEqual(
            display_name("claude", "sid-1", runtime_name=None, registry_name="hearting-fb",
                         title=None, slug="slug", cwd="/w/repo"),
            "hearting-fb")

    def test_slug_then_cwd_basename_fallback(self):
        self.assertEqual(
            display_name("codex", "sid-1", runtime_name=None, registry_name=None,
                         title=None, slug="slug-name", cwd="/w/repo"),
            "slug-name")
        self.assertEqual(
            display_name("codex", "sid-1", runtime_name=None, registry_name=None,
                         title=None, slug=None, cwd="/w/repo"),
            "repo")

    def test_never_returns_a_sid8_handle(self):
        for harness in ("claude", "codex", "opencode"):
            name = display_name(harness, "abcdefgh-123", runtime_name=None, registry_name=None,
                                title=None, slug=None, cwd=None)
            self.assertNotIn(session_handle(harness, "abcdefgh-123"), name)
            self.assertNotIn("CL/", name)
            self.assertNotIn("CX/", name)
            self.assertNotIn("OC/", name)


class ResolveDisplayInputsTest(_HermeticStateRoot):
    """`resolve_display_inputs()` — the I/O-bearing companion, fail-soft on every
    read (T-4), Claude ① gated on `nameSource != 'derived'` (Amendment 2)."""

    def test_derived_name_source_yields_no_runtime_name(self):
        self._write_pid_record(4242, "hearting-fb", "derived")
        inputs = resolve_display_inputs("claude", "sid-1", pid=4242)
        self.assertIsNone(inputs["runtime_name"])
        self.assertEqual(inputs["registry_name"], "hearting-fb")

    def test_user_set_name_source_yields_runtime_name(self):
        self._write_pid_record(4343, "my-custom-name", "user")
        inputs = resolve_display_inputs("claude", "sid-2", pid=4343)
        self.assertEqual(inputs["runtime_name"], "my-custom-name")
        self.assertEqual(inputs["registry_name"], "my-custom-name")

    def test_hearting_registry_wins_over_derived_pid_record(self):
        self._write_pid_record(4444, "hearting-fb", "derived")
        self._write_registry_name("claude", "sid-3", "renamed-by-user")
        inputs = resolve_display_inputs("claude", "sid-3", pid=4444)
        self.assertEqual(inputs["runtime_name"], "renamed-by-user")

    def test_missing_pid_record_and_registry_is_fail_soft(self):
        inputs = resolve_display_inputs("claude", "sid-missing", pid=999999)
        self.assertEqual(inputs, {"runtime_name": None, "registry_name": None})

    def test_malformed_registry_file_is_fail_soft(self):
        path = os.path.join(self._registry_dir("codex"), "sid-bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        inputs = resolve_display_inputs("codex", "sid-bad")
        self.assertIsNone(inputs["runtime_name"])

    def test_name_collision_appends_sid8_suffix(self):
        self._write_registry_name("codex", "sidabcdefgh", "shared-name")
        self._write_registry_name("codex", "sidzzzzzzzz", "shared-name")
        first = resolve_display_inputs("codex", "sidabcdefgh")["runtime_name"]
        second = resolve_display_inputs("codex", "sidzzzzzzzz")["runtime_name"]
        self.assertNotEqual(first, second)
        self.assertIn("sidabcdefgh"[:8], first)
        self.assertIn("sidzzzzzzzz"[:8], second)

    def test_no_collision_when_names_differ(self):
        self._write_registry_name("codex", "sid-a", "name-a")
        self._write_registry_name("codex", "sid-b", "name-b")
        self.assertEqual(resolve_display_inputs("codex", "sid-a")["runtime_name"], "name-a")
        self.assertEqual(resolve_display_inputs("codex", "sid-b")["runtime_name"], "name-b")


class DerivedSessionThreeSurfaceFixtureTest(_HermeticStateRoot):
    """Owner adjudication R2-5 / Amendment 2's named fixture: `session_name` = AI
    title, registry `name` = `hearting-fb`, `nameSource` = `derived` → statusline,
    Fleet, and the Herdr formatter must all resolve to ONE identical string. This
    is the pure-fixture half of that requirement (statusline/Herdr's own scripted
    comparison lives in evidence/, since neither has a native test harness)."""

    def test_statusline_and_fleet_chain_converge_on_the_same_string(self):
        self._write_pid_record(5151, "hearting-fb", "derived")
        session_name_stdin = "AI generated title"

        # statusline.sh's own call shape (adapters/claude/statusline.sh): resolve
        # inputs from the pid record, then pass the stdin session_name as `title`.
        statusline_inputs = resolve_display_inputs("claude", "sid-derived", pid=5151)
        statusline_name = display_name(
            "claude", "sid-derived", runtime_name=statusline_inputs["runtime_name"],
            registry_name=statusline_inputs["registry_name"], title=session_name_stdin,
            slug=None, cwd=None)

        # Fleet's collector shape: the same AI title lands on Session.title (the
        # same underlying sidecar value statusline's stdin session_name carries),
        # nameSource=derived leaves Session.runtime_name unset (C4).
        session = Session(harness="claude", pid=5151, session_id="sid-derived",
                          title=session_name_stdin, registry_name="hearting-fb",
                          runtime_name=None, slug="hearting-fb", cwd="/work/repo")
        fleet_name = render._session_name(session)

        self.assertEqual(statusline_name, session_name_stdin)
        self.assertEqual(fleet_name, session_name_stdin)
        self.assertEqual(statusline_name, fleet_name)

    def test_user_set_name_also_converges(self):
        self._write_pid_record(5252, "my-custom-name", "user")
        statusline_inputs = resolve_display_inputs("claude", "sid-user", pid=5252)
        statusline_name = display_name(
            "claude", "sid-user", runtime_name=statusline_inputs["runtime_name"],
            registry_name=statusline_inputs["registry_name"], title="unrelated AI title",
            slug=None, cwd=None)
        session = Session(harness="claude", pid=5252, session_id="sid-user",
                          title="unrelated AI title", registry_name="my-custom-name",
                          runtime_name="my-custom-name", slug="my-custom-name", cwd="/work/repo")
        fleet_name = render._session_name(session)
        self.assertEqual(statusline_name, "my-custom-name")
        self.assertEqual(fleet_name, "my-custom-name")


class RuntimeNameJsonAdditiveTest(unittest.TestCase):
    """R2-1 — `runtime_name` is additive in `--json`; a custom projection cannot
    silently drop it since `Session.to_dict()` serializes every public field."""

    def test_runtime_name_field_and_value_survive_to_dict(self):
        session = Session(harness="codex", pid=1, session_id="sid-1",
                          runtime_name="renamed-thread")
        payload = session.to_dict()
        self.assertIn("runtime_name", payload)
        self.assertEqual(payload["runtime_name"], "renamed-thread")

    def test_runtime_name_defaults_to_none_and_still_serializes(self):
        session = Session(harness="claude", pid=1, session_id="sid-2")
        payload = session.to_dict()
        self.assertIn("runtime_name", payload)
        self.assertIsNone(payload["runtime_name"])


class TagChipReplacesCompanionTest(unittest.TestCase):
    """F-100a (user 2026-09-03) — the F-99c dim ` · <registry_name>` companion is retired:
    it competed with the title for the same name-zone cells. The derived 2-hex tag now
    rides its own fixed chip slot between the status glyph and the harness text, and the
    title owns the whole name zone."""

    def _derived(self, **over):
        base = dict(harness="claude", pid=1, cwd="/work/repo", session_id="sid-1",
                    title="a real title", registry_name="hearting-fb", slug="hearting-fb",
                    session_tag="fb", liveness="idle", elapsed_min=3)
        base.update(over)
        return Session(**base)

    def test_the_companion_producer_is_gone(self):
        self.assertFalse(hasattr(render, "_session_name_companion"))

    def test_wide_row_shows_title_and_chip_never_the_companion(self):
        segs = render._session_row(self._derived(), narrow=False,
                                   name_width=render._wide_name_width(168))
        txt = "".join(t for t, _k in segs)
        self.assertIn("a real title", txt)
        self.assertNotIn(" · hearting-fb", txt)
        self.assertIn((" fb ", "tag_claude"), segs)

    def test_narrow_row_shows_title_and_chip_never_the_companion(self):
        l1, _l2 = render._session_row_2line(self._derived(), term_width=100)
        txt = "".join(t for t, _k in l1)
        self.assertIn("a real title", txt)
        self.assertNotIn("hearting-fb", txt)
        self.assertIn((" fb ", "tag_claude"), l1)

    def test_display_name_precedence_is_unchanged_by_the_chip(self):
        """The chip is additive: the F-99 name chain still decides the name zone."""
        self.assertEqual(render._session_name(self._derived()), "a real title")
        self.assertEqual(render._session_name(self._derived(title=None)), "hearting-fb")
        self.assertEqual(render._session_name(self._derived(runtime_name="mine")), "mine")

    def test_session_serializes_the_new_fields(self):
        payload = Session(harness="claude", pid=1, session_id="sid-2").to_dict()
        self.assertIn("session_tag", payload)
        self.assertIn("herdr_attached", payload)
        self.assertIsNone(payload["session_tag"])
        self.assertIsNone(payload["herdr_attached"])


if __name__ == "__main__":
    unittest.main()
