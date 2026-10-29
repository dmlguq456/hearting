#!/usr/bin/env python3
"""F-99 — canonical session display name.

Pure fixtures cover the display helpers and Fleet wiring; an actual subprocess
runs Claude's statusline.sh against isolated pid, title, config and state paths.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render                                          # noqa: E402
from fleet.model import Session                                   # noqa: E402
from fleet.session_handle import (current_session_subject, display_name,
                                  resolve_display_inputs, session_handle)  # noqa: E402

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
_STATUSLINE = os.path.join(_REPO_ROOT, "adapters", "claude", "statusline.sh")


class _HermeticStateRoot(unittest.TestCase):
    """Points AGENT_HOME/HOME at a fresh temp dir and clears every dispatch-state
    override so the hearting session-name registry (②) never touches real state."""

    def setUp(self):
        self._old_environ = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["AGENT_HOME"] = self.tmp.name
        os.environ["HOME"] = self.tmp.name
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp.name
        os.environ["HARNESS_CAPACITY_REFRESH_DISABLE"] = "1"
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
    """Pure fixtures for current user-name / successful-title / blank selection.

    These check selector inputs only; a separate test executes the real shell
    consumer with isolated pid, title, config and state paths.
    """

    def test_statusline_and_fleet_chain_converge_on_the_same_string(self):
        self._write_pid_record(5151, "hearting-fb", "derived")
        successful_title = "Persisted successful title"

        # Pure selection fixture: derived labels and native session_name are not
        # promoted; only persisted success is the fallback after a user name.
        statusline_inputs = resolve_display_inputs("claude", "sid-derived", pid=5151)
        statusline_name = current_session_subject(
            statusline_inputs["runtime_name"], successful_title)

        # Fleet's collector shape receives the same last successful title.
        session = Session(harness="claude", pid=5151, session_id="sid-derived",
                          title=successful_title, registry_name="hearting-fb",
                          runtime_name=None, slug="hearting-fb", cwd="/work/repo")
        fleet_name = render._session_name(session)

        self.assertEqual(current_session_subject(statusline_inputs["runtime_name"], None), "")
        self.assertEqual(statusline_name, successful_title)
        self.assertEqual(fleet_name, successful_title)
        self.assertEqual(statusline_name, fleet_name)

    def test_user_set_name_also_converges(self):
        self._write_pid_record(5252, "my-custom-name", "user")
        statusline_inputs = resolve_display_inputs("claude", "sid-user", pid=5252)
        statusline_name = current_session_subject(
            statusline_inputs["runtime_name"], "last successful title")
        session = Session(harness="claude", pid=5252, session_id="sid-user",
                          title="unrelated AI title", registry_name="my-custom-name",
                          runtime_name="my-custom-name", slug="my-custom-name", cwd="/work/repo")
        fleet_name = render._session_name(session)
        self.assertEqual(statusline_name, "my-custom-name")
        self.assertEqual(fleet_name, "my-custom-name")


@unittest.skipUnless(os.path.isfile("/proc/self/stat") and shutil.which("cc"),
                     "native Claude fixture requires Linux /proc and cc")
class StatuslineSubprocessTest(_HermeticStateRoot):
    """Real statusline with native executable/argv and exact private registry."""

    @classmethod
    def setUpClass(cls):
        native = tempfile.TemporaryDirectory()
        cls.addClassCleanup(native.cleanup)
        root = Path(native.name)
        source = root / "runtime.c"
        source.write_text(r'''#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
int main(void) {
    char buffer[8192], path[8192];
    snprintf(path, sizeof(path), "/proc/%d/stat", getpid());
    FILE *f = fopen(path, "r");
    if (!f || !fgets(buffer, sizeof(buffer), f)) return 2;
    fclose(f);
    char *token = strtok(strrchr(buffer, ')') + 2, " ");
    for (int i=0; i<19 && token; i++) token = strtok(NULL, " ");
    if (!token) return 3;
    snprintf(path, sizeof(path), "%s/sessions/%d.json", getenv("CLAUDE_CONFIG_DIR"), getpid());
    f = fopen(path, "w");
    if (!f) return 4;
    fprintf(f, "{\"pid\":%d,\"procStart\":\"%s\",\"sessionId\":\"%s\","
            "\"name\":\"%s\",\"nameSource\":\"%s\"}", getpid(), token, getenv("TEST_SESSION_ID"),
            getenv("TEST_SESSION_NAME"), getenv("TEST_NAME_SOURCE"));
    if (fclose(f) != 0) return 5;
    pid_t child = fork();
    if (child < 0) return 6;
    if (child == 0) { execl("/bin/bash", "bash", getenv("STATUSLINE_TEST_SCRIPT"), NULL); _exit(7); }
    int status;
    if (waitpid(child, &status, 0) != child) return 8;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 9;
}
''', encoding="utf-8")
        cls.binary = root / "claude" / "versions" / "8.7.6"
        cls.binary.parent.mkdir(parents=True)
        subprocess.run([shutil.which("cc"), str(source), "-o", str(cls.binary)],
                       check=True, capture_output=True, timeout=30)

    def setUp(self):
        super().setUp()
        self.title_state = Path(self.tmp.name) / "title-state"
        self.title_state.mkdir()
        self.claude_config = Path(self.tmp.name) / "claude-config"
        self.claude_config.mkdir()
        (self.claude_config / "sessions").mkdir()
        # Both the native registry and legacy name reader use this private home.
        tools_link = self.claude_config / "tools"
        tools_link.mkdir()
        (tools_link / "fleet").symlink_to(Path(_TOOLS_DIR) / "fleet",
                                          target_is_directory=True)
        utilities = self.claude_config / "utilities"
        utilities.mkdir()
        (utilities / "claude_process_identity.py").symlink_to(
            Path(_REPO_ROOT) / "utilities" / "claude_process_identity.py")

    def _run_statusline(self, sid, cwd_name, *, name, name_source, title=None,
                        native_name="automatic stdin title"):
        cwd = Path(self.tmp.name) / "workspaces" / cwd_name
        cwd.mkdir(parents=True, exist_ok=True)
        if title is not None:
            title_dir = self.title_state / "claude"
            title_dir.mkdir(parents=True, exist_ok=True)
            (title_dir / (sid + ".json")).write_text(json.dumps({
                "title": title, "ts": 1, "source": "refresher",
            }), encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "AGENT_HOME": str(self.claude_config),
            "CLAUDE_CONFIG_DIR": str(self.claude_config),
            "FLEET_TITLE_STATE_DIR": str(self.title_state),
            "FLEET_TITLE_DISABLE": "1",
            "STATUSLINE_TEST_SCRIPT": _STATUSLINE,
            "TEST_SESSION_ID": sid,
            "TEST_SESSION_NAME": name,
            "TEST_NAME_SOURCE": name_source,
        })
        payload = json.dumps({
            "session_id": sid, "session_name": native_name, "cwd": str(cwd),
            "model": {"display_name": "Fixture"},
        })
        # The isolated suite runner owns the timeout and descendant cleanup.
        # The native parent waits for statusline before fixture paths retire.
        with subprocess.Popen([str(self.binary)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env) as proc:
            output, error = proc.communicate(payload)
        self.assertEqual(proc.returncode, 0, error)
        registry = json.loads((self.claude_config / "sessions" /
                               (str(proc.pid) + ".json")).read_text(encoding="utf-8"))
        tap = json.loads((self.claude_config / ".statusline" /
                          (sid + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(tap["session_id"], registry["sessionId"])
        self.assertEqual(tap["session_id"], sid)
        self.assertEqual(registry["pid"], proc.pid)
        self.assertEqual(tap["pid"], proc.pid)
        self.assertRegex(tap["proc_start"], r"^[0-9]+$")
        self.assertEqual(tap["proc_start"], registry["procStart"])
        return output

    def test_real_statusline_selects_user_name_last_success_blank_and_cwd_name(self):
        explicit = self._run_statusline(
            "statusline-explicit", "explicit-workspace", name="User chosen subject",
            name_source="user", title="Older successful title")
        self.assertIn("User chosen subject", explicit)
        self.assertNotIn("Older successful title", explicit)

        last_success = self._run_statusline(
            "statusline-last-success", "title-workspace", name="derived-workspace-42",
            name_source="derived", title="Last successful title")
        self.assertIn("Last successful title", last_success)
        self.assertNotIn("derived-workspace-42", last_success)

        no_success = self._run_statusline(
            "statusline-no-success", "blank-workspace", name="derived-generated-name",
            name_source="derived")
        self.assertNotIn("automatic stdin title", no_success)
        self.assertNotIn("derived-generated-name", no_success)

        cwd_name = "same-name-workspace"
        same_as_cwd = self._run_statusline(
            "statusline-same-cwd", cwd_name, name=cwd_name, name_source="user",
            title="Older title")
        self.assertGreaterEqual(same_as_cwd.count(cwd_name), 2, same_as_cwd)


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
        self.assertIn(("fb", "tag"), segs)
        self.assertIn("[fb]", txt)

    def test_narrow_row_shows_title_and_chip_never_the_companion(self):
        l1, _l2 = render._session_row_2line(self._derived(), term_width=100)
        txt = "".join(t for t, _k in l1)
        self.assertIn("a real title", txt)
        self.assertNotIn("hearting-fb", txt)
        self.assertIn(("fb", "tag"), l1)
        self.assertIn("[fb]", txt)

    def test_display_name_precedence_is_unchanged_by_the_chip(self):
        """The chip remains visible when no Fleet title has succeeded yet."""
        self.assertEqual(render._session_name(self._derived()), "a real title")
        self.assertEqual(render._session_name(self._derived(title=None)), "")
        self.assertEqual(render._session_name(self._derived(runtime_name="mine")), "mine")

    def test_session_serializes_the_new_fields(self):
        payload = Session(harness="claude", pid=1, session_id="sid-2").to_dict()
        self.assertIn("session_tag", payload)
        self.assertIn("herdr_attached", payload)
        self.assertIsNone(payload["session_tag"])
        self.assertIsNone(payload["herdr_attached"])


if __name__ == "__main__":
    unittest.main()
