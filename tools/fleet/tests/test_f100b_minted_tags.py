#!/usr/bin/env python3
"""F-100b (user 2026-09-03) — the `[xx]` tag for harnesses that mint no derived name.

Claude's tag is read off its derived `<basename>-<xx>` session name (F-100a,
`derived_tag`). Codex and OpenCode expose no such name, so Fleet mints the tag from the
canonical session id with `minted_tag`. The badge itself is unchanged: `_session_tag_chip`
is harness-neutral and draws whatever `Session.session_tag` holds.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import re
import sys
import unittest

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render                                          # noqa: E402
from fleet.model import Session                                   # noqa: E402
from fleet.session_handle import derived_tag, minted_tag          # noqa: E402


# Synthetic UUIDv7-shaped thread IDs preserve the shared `01` prefix that made
# truncation unsuitable for assigning distinct session badges.
_CODEX_IDS = ("01000009-0000-7000-8000-00000000000f",
              "01000007-0000-7000-8000-00000000000d",
              "01000008-0000-7000-8000-00000000000e")
_OPENCODE_IDS = ("ses_7f3a19c2b0004e11", "ses_1c9d4400ff21ae03")


class MintedTagTest(unittest.TestCase):
    def test_shape_is_two_lowercase_hex(self):
        for sid in _CODEX_IDS + _OPENCODE_IDS:
            with self.subTest(sid=sid):
                self.assertRegex(minted_tag(sid), r"^[0-9a-f]{2}$")

    def test_deterministic_and_pure(self):
        """Nothing is persisted, so the same id must always mint the same tag."""
        for sid in _CODEX_IDS:
            self.assertEqual(minted_tag(sid), minted_tag(sid))
        self.assertEqual(minted_tag(_CODEX_IDS[0]),
                         minted_tag("  %s  " % _CODEX_IDS[0]))

    def test_missing_or_non_string_ids_yield_none(self):
        for sid in (None, "", "   ", 1234, b"01000009", ("01",)):
            with self.subTest(sid=sid):
                self.assertIsNone(minted_tag(sid))

    def test_uuidv7_prefix_would_collide_but_the_hash_does_not(self):
        """The reason this is a hash and not `sid[:2]`: every Codex id starts `01`."""
        self.assertEqual({sid[:2] for sid in _CODEX_IDS}, {"01"})
        self.assertEqual(len({minted_tag(sid) for sid in _CODEX_IDS}), len(_CODEX_IDS))

    def test_ids_that_differ_late_still_separate(self):
        a = "01000009-0000-7000-8000-00000000000f"
        b = "01000009-0000-7000-8000-000000000010"
        self.assertNotEqual(minted_tag(a), minted_tag(b))

    def test_spread_over_the_available_values(self):
        """Not a uniformity proof — a guard against a truncation that pins one value."""
        tags = {minted_tag("session-%d" % i) for i in range(512)}
        self.assertGreater(len(tags), 200)

    def test_minted_and_derived_stay_separate_functions(self):
        """A minted tag is not a name suffix: `derived_tag` must not read an id."""
        self.assertIsNone(derived_tag(_CODEX_IDS[0]))


class MintedTagChipTest(unittest.TestCase):
    def _s(self, harness, **over):
        base = dict(harness=harness, pid=1, cwd="/x", slug="s", title="a title",
                    liveness="idle", elapsed_min=1)
        base.update(over)
        return Session(**base)

    def test_chip_draws_a_minted_tag_for_codex_and_opencode(self):
        for harness in ("codex", "opencode"):
            tag = minted_tag(_CODEX_IDS[0])
            segs = render._session_tag_chip(self._s(harness, session_tag=tag))
            with self.subTest(harness=harness):
                self.assertEqual("".join(t for t, _k in segs), "[%s] " % tag)
                self.assertEqual(sum(render._dw(t) for t, _k in segs), render._TAG_W)

    def test_untagged_row_keeps_the_same_slot_width(self):
        for harness in ("codex", "opencode"):
            segs = render._session_tag_chip(self._s(harness))
            with self.subTest(harness=harness):
                self.assertEqual(sum(render._dw(t) for t, _k in segs), render._TAG_W)


class CollectorWiringCensusTest(unittest.TestCase):
    """The badge is only as good as the two assignments that feed it; a refactor that
    drops one leaves a silently blank slot that no fixture above would catch."""

    def _source(self, name):
        path = os.path.join(_TOOLS_DIR, "fleet", "collectors", name)
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_codex_and_opencode_collectors_use_the_shared_resolver(self):
        for name in ("codex.py", "opencode.py"):
            with self.subTest(collector=name):
                source = self._source(name)
                self.assertRegex(source, r"sess\.session_tag\s*=\s*resolve_tag\(")

    def test_claude_collector_keeps_reading_its_derived_name(self):
        """F-100a is untouched: Claude never mints, it reads the runtime's own suffix.

        F-26b (plan.md B-1) moved the `derived_tag(name)` call out of claude.py and
        into `session_registry.apply_to_session` (both claude.py and codex.py now
        delegate their registry reads there), so a source-text grep for the call site
        no longer holds. Assert the behavior it protects instead: applying a claude
        registry record whose name is `nameSource="derived"` still derives the 2-hex
        `session_tag` from that name, and a codex record with the same shape does not
        (minted_tag, not derived_tag, owns that branch for codex/opencode).
        """
        from fleet import session_registry
        from fleet.model import Session

        record = {field: None for field in session_registry.FIELDS}
        record.update(name="hearting-46", nameSource="derived")
        claude_sess = Session(harness="claude", pid=1)
        codex_sess = Session(harness="codex", pid=2)
        session_registry.apply_to_session(claude_sess, record, "claude")
        session_registry.apply_to_session(codex_sess, record, "codex")
        self.assertEqual(claude_sess.session_tag, derived_tag("hearting-46"))
        self.assertIsNotNone(claude_sess.session_tag)
        self.assertIsNone(codex_sess.session_tag)
        source = self._source("claude.py")
        self.assertNotIn("minted_tag", source)


if __name__ == "__main__":
    unittest.main()
