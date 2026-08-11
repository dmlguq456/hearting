import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet import gitinfo, render
from fleet import fleet as fleet_mod
from fleet.collectors import procscan
from fleet.model import Session


class F51GitTelemetryTest(unittest.TestCase):
    def _run_git(self, root, *args, check=True):
        return subprocess.run(["git", "-C", root] + list(args),
                              capture_output=True, text=True, check=check)

    def _make_repo(self, root, ahead=0, behind=0, configure_upstream=True):
        """A real repo with `ahead` commits on the checked-out branch past a local
        "upstream" branch and `behind` commits on "upstream" past the checked-out branch.
        Returns the checked-out branch's name (whatever `git init` picked as default)."""
        self._run_git(root, "init", "-q", ".")
        self._run_git(root, "config", "user.email", "a@b")
        self._run_git(root, "config", "user.name", "A")
        open(os.path.join(root, "base"), "w").close()
        self._run_git(root, "add", "base")
        self._run_git(root, "commit", "-qm", "base")
        # A short, fixed branch name keeps the fixed 14-cell `_BRANCH_SUFFIX_W` budget
        # comfortable for every (ahead, behind) pair this suite exercises, regardless of
        # what `git init` happened to pick as the local default branch name.
        self._run_git(root, "branch", "-m", "m")
        branch_name = "m"
        self._run_git(root, "branch", "upstream")
        if configure_upstream:
            self._run_git(root, "config", "branch.%s.remote" % branch_name, ".")
            self._run_git(root, "config", "branch.%s.merge" % branch_name, "refs/heads/upstream")
        for i in range(ahead):
            fname = "ahead-%d" % i
            open(os.path.join(root, fname), "w").close()
            self._run_git(root, "add", fname)
            self._run_git(root, "commit", "-qm", fname)
        if behind:
            self._run_git(root, "checkout", "-q", "upstream")
            for i in range(behind):
                fname = "behind-%d" % i
                open(os.path.join(root, fname), "w").close()
                self._run_git(root, "add", fname)
                self._run_git(root, "commit", "-qm", fname)
            self._run_git(root, "checkout", "-q", branch_name)
        return branch_name

    def _ahead_behind_sync(self, root):
        """Deterministically wait for the ONE worker thread `ahead_behind` spawns for
        `root`, by capturing its actual thread handle and joining it — no fixed-count
        polling loop, and an explicit failure (never a silent pass) if it hangs."""
        gitinfo._CACHE.clear(); gitinfo._INFLIGHT.clear()
        created = []
        orig_thread_cls = gitinfo.threading.Thread

        def capturing_thread(*args, **kwargs):
            t = orig_thread_cls(*args, **kwargs)
            created.append(t)
            return t

        with mock.patch.object(gitinfo.threading, "Thread", side_effect=capturing_thread):
            self.assertIsNone(gitinfo.ahead_behind(root))
        self.assertEqual(len(created), 1, "ahead_behind did not spawn exactly one worker thread")
        created[0].join(timeout=5)
        self.assertFalse(created[0].is_alive(), "worker thread did not finish within timeout")
        cached = gitinfo._CACHE.get(root)
        self.assertIsNotNone(cached, "worker never populated the cache")
        return cached[1]

    def _run_worker_direct(self, cwd, br, run_impl):
        gitinfo._CACHE.clear(); gitinfo._INFLIGHT.clear()
        with mock.patch.object(gitinfo.subprocess, "run", side_effect=run_impl):
            gitinfo._worker(cwd, br)
        return gitinfo._CACHE[cwd][1]

    def test_a16_branch_matches_rev_parse_abbrev_ref_except_detached(self):
        """A16: cross-check the file-parser `branch()` against `git rev-parse --abbrev-ref
        HEAD` on real fixtures — normal branch, linked worktree, non-repo. Detached HEAD is
        deliberately excluded from the direct-match: the parser returns a 7-char short sha
        (matching every other row's identity convention) while `rev-parse` literally returns
        the string "HEAD"."""
        with tempfile.TemporaryDirectory() as root:
            branch_name = self._make_repo(root, ahead=0, behind=0, configure_upstream=False)
            rp = self._run_git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            self.assertEqual(gitinfo.branch(root), rp)
            self.assertEqual(rp, branch_name)

            wt = tempfile.mkdtemp()
            try:
                os.rmdir(wt)
                self._run_git(root, "worktree", "add", "-q", "-b", "topic", wt)
                wt_rp = self._run_git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
                self.assertEqual(gitinfo.branch(wt), wt_rp)
                self.assertEqual(wt_rp, "topic")
            finally:
                self._run_git(root, "worktree", "remove", "-f", wt, check=False)

            with tempfile.TemporaryDirectory() as outside:
                self.assertIsNone(gitinfo.branch(outside))

            self._run_git(root, "checkout", "-q", "--detach", "HEAD")
            detached_rp = self._run_git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            full_sha = self._run_git(root, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(detached_rp, "HEAD")
            self.assertEqual(gitinfo.branch(root), full_sha[:7])
            self.assertNotEqual(gitinfo.branch(root), detached_rp)

    def test_a17_ahead_and_behind_are_measured_against_a_real_upstream(self):
        """A17: no injected `_CACHE` stub — every (ahead, behind) pair, including a genuine
        `behind`, is produced by real commits on a real local "upstream" branch and a real
        `git rev-list --left-right --count` call."""
        for ahead, behind in ((2, 1), (2, 0), (0, 1), (0, 0)):
            with self.subTest(ahead=ahead, behind=behind):
                with tempfile.TemporaryDirectory() as root:
                    branch_name = self._make_repo(root, ahead=ahead, behind=behind)
                    counts = self._ahead_behind_sync(root)
                    if ahead or behind:
                        self.assertEqual(counts, (ahead, behind))
                    else:
                        self.assertIsNone(counts)
                    visible = "".join(x[0] for x in
                                      render._branch_suffix_segs(root, branch_name))
                    if ahead:
                        self.assertIn("↑%d" % ahead, visible)
                    else:
                        self.assertNotIn("↑", visible)
                    if behind:
                        self.assertIn("↓%d" % behind, visible)
                    else:
                        self.assertNotIn("↓", visible)

    def test_a18_seven_no_evidence_cases_render_with_no_arrows(self):
        """A18: upstream unconfigured, detached HEAD, not-a-repo, a failing rev-list, a
        timing-out rev-list, an unpopulated cache, and a real ahead=0/behind=0 repo all
        resolve to `None` — zero `↑`/`↓` in the rendered suffix. The wide layout's
        not-a-repo case pins the `(—)` fallback as its own fixture."""
        # 1. upstream not configured at all.
        with tempfile.TemporaryDirectory() as root:
            branch_name = self._make_repo(root, ahead=1, behind=0, configure_upstream=False)
            self.assertIsNone(self._ahead_behind_sync(root))
            visible = "".join(x[0] for x in render._branch_suffix_segs(root, branch_name))
            self.assertNotIn("↑", visible); self.assertNotIn("↓", visible)

        # 2. detached HEAD.
        with tempfile.TemporaryDirectory() as root:
            self._make_repo(root, ahead=1, behind=1)
            self._run_git(root, "checkout", "-q", "--detach", "HEAD")
            self.assertIsNone(self._ahead_behind_sync(root))

        # 3. not a repo at all — also pins the wide `(—)` fallback.
        with tempfile.TemporaryDirectory() as outside:
            self.assertIsNone(gitinfo.ahead_behind(outside))
            visible = "".join(x[0] for x in render._branch_suffix_segs(outside, None))
            self.assertIn("(—)", visible)
            self.assertNotIn("↑", visible); self.assertNotIn("↓", visible)

        # 4. rev-list fails (nonzero exit).
        def run_fail(argv, **kwargs):
            if "rev-list" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="fatal")
            return subprocess.CompletedProcess(argv, 0, stdout="refs/heads/upstream\n", stderr="")
        self.assertIsNone(self._run_worker_direct("/tmp/a18-fail", "master", run_fail))

        # 5. rev-list times out.
        def run_timeout(argv, **kwargs):
            if "rev-list" in argv:
                raise subprocess.TimeoutExpired(argv, 2)
            return subprocess.CompletedProcess(argv, 0, stdout="refs/heads/upstream\n", stderr="")
        self.assertIsNone(self._run_worker_direct("/tmp/a18-timeout", "master", run_timeout))

        # 6. cache never populated yet (the very first call, before any worker finishes).
        with tempfile.TemporaryDirectory() as root:
            self._make_repo(root, ahead=1, behind=0)
            gitinfo._CACHE.clear(); gitinfo._INFLIGHT.clear()
            self.assertIsNone(gitinfo.ahead_behind(root))

        # 7. a real repo with ahead=0/behind=0 collapses to None (no zero-arrows shown).
        with tempfile.TemporaryDirectory() as root:
            self._make_repo(root, ahead=0, behind=0)
            self.assertIsNone(self._ahead_behind_sync(root))

    def test_a19_unconfigured_upstream_never_reaches_rev_list(self):
        """A19: when neither `branch.<name>.remote` nor `.merge` is set, the negative gate
        is a direct parse of the gitdir `config` file — zero `subprocess.run` calls, not
        just zero `rev-list` calls. When at least one key is present, `rev-list` runs
        exactly once, and a failing `rev-list` still resolves to `None` in silence."""
        with tempfile.TemporaryDirectory() as root:
            self._make_repo(root, ahead=1, behind=0, configure_upstream=False)
            with mock.patch.object(gitinfo.subprocess, "run",
                                   wraps=gitinfo.subprocess.run) as run_spy:
                self.assertIsNone(self._ahead_behind_sync(root))
            self.assertEqual(run_spy.call_args_list, [])

        with tempfile.TemporaryDirectory() as root:
            branch_name = self._make_repo(root, ahead=1, behind=0, configure_upstream=True)
            with mock.patch.object(gitinfo.subprocess, "run",
                                   wraps=gitinfo.subprocess.run) as run_spy:
                counts = self._ahead_behind_sync(root)
            self.assertEqual(counts, (1, 0))
            rev_list_calls = [c for c in run_spy.call_args_list
                              if "rev-list" in c.args[0]]
            self.assertEqual(len(rev_list_calls), 1)
            self.assertEqual(run_spy.call_count, 1)

        def run_fail(argv, **kwargs):
            self.assertIn("rev-list", argv)
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="fatal")
        with tempfile.TemporaryDirectory() as root:
            branch_name = self._make_repo(root, ahead=0, behind=0, configure_upstream=True)
            with mock.patch.object(gitinfo.subprocess, "run", side_effect=run_fail):
                self.assertIsNone(self._ahead_behind_sync(root))

    def test_a19b_configured_parses_quoted_and_dot_sections_case_insensitively(self):
        """Regression: `_configured` reads the gitdir `config` file directly (no
        subprocess), accepting both `[branch "name"]` and `[branch.name]` section forms,
        case-insensitive section/key names but a case-sensitive subsection/branch name,
        remote-only or merge-only presence, absent sections, and — for a linked
        worktree — the shared main-repo config rather than the linked gitdir."""
        with tempfile.TemporaryDirectory() as root:
            self._make_repo(root, ahead=0, behind=0, configure_upstream=False)
            with mock.patch.object(gitinfo.subprocess, "run", side_effect=AssertionError):
                self.assertFalse(gitinfo._configured(root, "m"))

                _linked, main = gitinfo.resolve_gitdir(root)
                cfg_path = os.path.join(main, "config")
                with open(cfg_path, "a", encoding="utf-8") as fh:
                    fh.write('[Branch "m"]\n\tREMOTE = .\n')
                self.assertTrue(gitinfo._configured(root, "m"))
                self.assertFalse(gitinfo._configured(root, "other"))

            with open(cfg_path, "a", encoding="utf-8") as fh:
                fh.write("[branch.dotform]\n\tmerge = refs/heads/upstream\n")
            with mock.patch.object(gitinfo.subprocess, "run", side_effect=AssertionError):
                self.assertTrue(gitinfo._configured(root, "dotform"))

            wt = tempfile.mkdtemp()
            try:
                os.rmdir(wt)
                self._run_git(root, "worktree", "add", "-q", "-b", "linked-topic", wt)
                self._run_git(root, "config", "branch.linked-topic.remote", ".")
                with mock.patch.object(gitinfo.subprocess, "run", side_effect=AssertionError):
                    self.assertTrue(gitinfo._configured(wt, "linked-topic"))
                    self.assertFalse(gitinfo._configured(wt, "nope"))
            finally:
                self._run_git(root, "worktree", "remove", "-f", wt, check=False)

        with tempfile.TemporaryDirectory() as outside:
            self.assertFalse(gitinfo._configured(outside, "m"))

    def test_a20_suffix_width_and_name_cap_invariant_across_widths_and_layouts(self):
        """A20: `_BRANCH_SUFFIX_W` and the 40-col name cap stay fixed at every width/layout,
        the row never overflows its terminal, `↓behind` drops before `↑ahead` under
        pressure, and the branch name is the last thing to give up its space."""
        self.assertEqual(render._BRANCH_SUFFIX_W, 14)
        self.assertEqual(render._NAME_WIDE_MAX, 40)
        old = gitinfo.ahead_behind
        try:
            # A branch name short enough to survive unclipped at every reserved suffix
            # width, so the width/layout sweep below isolates the suffix-budget invariant
            # from `_clip_w`'s ellipsis math (a separate, already-covered concern).
            gitinfo.ahead_behind = lambda cwd: (1, 2)
            s = Session(harness="claude", pid=1, cwd="/x", slug="proj",
                       title="a-very-long-session-title-that-keeps-on-going-and-going",
                       liveness="idle", ctx_pct=50, branch="feature/x", elapsed_min=5)
            for term_width in (60, 100, 138, 168, 200):
                layout = render._layout_mode(term_width)
                with self.subTest(term_width=term_width, layout=layout):
                    if layout == "wide":
                        name_w = render._wide_name_width(term_width)
                        segs = render._session_row(s, narrow=False, name_width=name_w)
                        text = "".join(t for t, _k in segs if t != render._RFLUSH)
                    else:
                        l1, _l2 = render._session_row_2line(s, term_width=term_width)
                        text = "".join(t for t, _k in l1)
                    self.assertLessEqual(render._dw(text), term_width)
                    # branch name always survives even under a long session title.
                    self.assertIn("(feature/x", text)
            # drop order at a hostile width: oversized ahead/behind digits overflow the
            # fixed suffix budget together, but individually ahead alone still fits — so
            # behind sheds first, exactly as `_branch_suffix_segs`'s degradation ladder
            # promises, and the branch name itself is never the thing that gives way.
            gitinfo.ahead_behind = lambda cwd: (123, 456)
            visible = "".join(x[0] for x in render._branch_suffix_segs("/x", "main",
                                                                       dim=True))
            self.assertIn("(main ↑123)", visible)
            self.assertIn("↑123", visible)
            self.assertNotIn("↓456", visible)
        finally:
            gitinfo.ahead_behind = old

    def test_a13_ahead_behind_returns_immediately_even_if_subprocess_run_blocks_forever(self):
        """A13: `subprocess.run` is replaced with a permanently-blocked stub — `ahead_behind`
        (and, downstream, `_build_lines`) must still return promptly with no ahead/behind
        shown, while the branch label — parsed straight from `.git/HEAD`, never subprocess —
        renders normally."""
        block = threading.Event()
        def blocked_run(*a, **k):
            block.wait()
            raise AssertionError("unreachable — the block was never released")
        try:
            with tempfile.TemporaryDirectory() as root:
                branch_name = self._make_repo(root, ahead=1, behind=0)
                gitinfo._CACHE.clear(); gitinfo._INFLIGHT.clear()
                with mock.patch.object(gitinfo.subprocess, "run", side_effect=blocked_run):
                    start = time.time()
                    result = gitinfo.ahead_behind(root)
                    elapsed = time.time() - start
                self.assertIsNone(result)
                self.assertLess(elapsed, 1.0)
                visible = "".join(x[0] for x in render._branch_suffix_segs(root, branch_name))
                self.assertIn("(" + branch_name + ")", visible)
                self.assertNotIn("↑", visible); self.assertNotIn("↓", visible)
        finally:
            block.set()   # release the stuck background worker thread before the run ends

    def test_detached_head_is_short_and_linked_worktree_uses_linked_head(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, ".git"))
            with open(os.path.join(root, ".git", "HEAD"), "w") as fh:
                fh.write("0123456789abcdef0123456789abcdef01234567\n")
            self.assertEqual(gitinfo.branch(root), "0123456")

    def test_branch_parser_handles_normal_linked_and_non_repo_without_subprocess(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(gitinfo.subprocess, "run",
                                                                       side_effect=AssertionError):
            os.makedirs(os.path.join(root, ".git", "refs", "heads"))
            with open(os.path.join(root, ".git", "HEAD"), "w") as fh: fh.write("ref: refs/heads/main\n")
            self.assertEqual(gitinfo.branch(root), "main")
            linked = os.path.join(root, "linked"); os.mkdir(linked)
            with open(os.path.join(linked, ".git"), "w") as fh: fh.write("gitdir: ../.git/worktrees/linked\n")
            os.makedirs(os.path.join(root, ".git", "worktrees", "linked"))
            with open(os.path.join(root, ".git", "worktrees", "linked", "HEAD"), "w") as fh:
                fh.write("ref: refs/heads/topic\n")
            self.assertEqual(gitinfo.branch(linked), "topic")
            with tempfile.TemporaryDirectory() as outside:
                self.assertIsNone(gitinfo.branch(outside))

    def test_real_repo_ahead_behind_counts_and_zero_suppresses_suffix(self):
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(["git", "init", "-q", root], check=True)
            subprocess.run(["git", "-C", root, "config", "user.email", "a@b"], check=True)
            subprocess.run(["git", "-C", root, "config", "user.name", "A"], check=True)
            open(os.path.join(root, "a"), "w").close()
            subprocess.run(["git", "-C", root, "add", "a"], check=True)
            subprocess.run(["git", "-C", root, "commit", "-qm", "base"], check=True)
            subprocess.run(["git", "-C", root, "branch", "--set-upstream-to", "HEAD", "master"],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Configure a local upstream ref explicitly, then create two ahead commits.
            subprocess.run(["git", "-C", root, "config", "branch.master.remote", "."], check=True)
            subprocess.run(["git", "-C", root, "config", "branch.master.merge", "refs/heads/upstream"], check=True)
            subprocess.run(["git", "-C", root, "branch", "upstream"], check=True)
            for name in ("b", "c"):
                open(os.path.join(root, name), "w").close()
                subprocess.run(["git", "-C", root, "add", name], check=True)
                subprocess.run(["git", "-C", root, "commit", "-qm", name], check=True)
            counts = self._ahead_behind_sync(root)
            self.assertEqual(counts, (2, 0))
            gitinfo._CACHE[root] = (time.time(), (2, 1))
            visible = "".join(x[0] for x in render._branch_suffix_segs(root, "main"))
            self.assertIn("↑2", visible); self.assertIn("↓1", visible)

    def test_a24_json_telemetry_projects_branch_ahead_behind_from_cache(self):
        """A24/F-51d: `--json`'s `branch_ahead`/`branch_behind` must reflect a populated
        `gitinfo` cache instead of always being null, while the snapshot path only ever
        reads that cache (never schedules a new background `git rev-list` itself — the
        cache is populated here by the SAME worker-thread mechanism `ahead_behind` already
        uses, exercised via `_ahead_behind_sync`, not by collect_all). An empty cache must
        still resolve both fields to `None` — absence stays normal, never synthesized 0."""
        with tempfile.TemporaryDirectory() as root:
            self._make_repo(root, ahead=2, behind=1)
            self.assertEqual(self._ahead_behind_sync(root), (2, 1))

            session = Session(harness="claude", pid=1, cwd=root, liveness="idle", mtime=1000)
            with mock.patch.object(procscan, "scan", return_value=[session]):
                buf = io.StringIO()
                with mock.patch.object(sys, "stdout", buf):
                    fleet_mod.main(["--json"])
            data = json.loads(buf.getvalue())
            matched = [s for s in data["sessions"] if s.get("cwd") == root]
            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0]["branch_ahead"], 2)
            self.assertEqual(matched[0]["branch_behind"], 1)

        with tempfile.TemporaryDirectory() as root2:
            self._make_repo(root2, ahead=0, behind=0)
            gitinfo._CACHE.clear(); gitinfo._INFLIGHT.clear()
            session2 = Session(harness="claude", pid=2, cwd=root2, liveness="idle", mtime=1000)
            with mock.patch.object(procscan, "scan", return_value=[session2]):
                buf2 = io.StringIO()
                with mock.patch.object(sys, "stdout", buf2):
                    fleet_mod.main(["--json"])
            data2 = json.loads(buf2.getvalue())
            matched2 = [s for s in data2["sessions"] if s.get("cwd") == root2]
            self.assertEqual(len(matched2), 1)
            self.assertIsNone(matched2[0]["branch_ahead"])
            self.assertIsNone(matched2[0]["branch_behind"])

    def test_suffix_keeps_branch_and_uses_arrow_order(self):
        old = gitinfo.ahead_behind
        try:
            gitinfo.ahead_behind = lambda cwd: (2, 1)
            segs = render._branch_suffix_segs("/tmp", "main")
            text = "".join(x[0] for x in segs)
            self.assertIn("(main ↑2 ↓1)", text)
            self.assertLess(text.index("↑2"), text.index("↓1"))
            self.assertIn((" ↑2", "lvl_g"), segs)
            self.assertIn((" ↓1", "lvl_r"), segs)
        finally:
            gitinfo.ahead_behind = old

    def test_live_snapshot_enrichment_deduplicates_cwd_and_attaches_metadata(self):
        first = Session(harness="codex", pid=1, cwd="/nas/repo", liveness="working")
        second = Session(harness="claude", pid=2, cwd="/nas/repo", liveness="idle")
        with mock.patch.object(gitinfo, "branch", return_value="main") as branch, \
             mock.patch.object(gitinfo, "worktree_count", return_value=4) as count, \
             mock.patch.object(gitinfo, "cached_ahead_behind", return_value=(2, 1)), \
             mock.patch.object(gitinfo, "ahead_behind", side_effect=AssertionError):
            gitinfo.enrich_entities([first, second], schedule_ahead=False)
        self.assertEqual(branch.call_count, 1)
        self.assertEqual(count.call_count, 1)
        for row in (first, second):
            self.assertEqual(row.branch, "main")
            self.assertEqual((row.branch_ahead, row.branch_behind), (2, 1))
            self.assertEqual(row.worktree_count, 4)


if __name__ == "__main__":
    unittest.main()
