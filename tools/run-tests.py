#!/usr/bin/env python3
"""Full-suite test runner (C-3/C-4). stdlib-only.

Collects every ``*.test.py`` / ``*.test.sh`` / ``test_*.py`` file in the repo
(symlinks not followed), runs each suite in isolation, and classifies results
against a known-failure baseline (tools/test-baseline.tsv) and an isolation
opt-out declaration (tools/test-isolation.tsv).

Isolation is owned by a single helper, build_isolated_env(), so the
definition cannot drift. There is no code path that hands a suite subprocess
the caller's real (ambient) environment — the runtime the caller invoked this
script from is never propagated. See owner addendum A: an "ambient" execution
profile does not exist here on purpose.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = 600
DEFAULT_JOBS = 4

PRUNE_DIRS = {".git", "__pycache__", "node_modules"}

EXPECTED_FAILURE_KINDS = {"exit-nonzero", "timeout", "error", "missing-binary", "assertion"}
ISOLATION_PROFILES = ("isolated", "installed-layout", "live-registry", "ci-like")

BASELINE_COLUMNS = [
    "suite_path",
    "test_id",
    "expected_failure_kind",
    "isolation_profile",
    "reason",
    "defect_id",
    "review_by",
    "fingerprint",
]
ISOLATION_COLUMNS = ["suite_path", "needs", "reason", "defect_id", "review_by"]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def collect_suites(root: Path) -> list[Path]:
    suites: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        for name in filenames:
            if name.endswith(".test.py") or name.endswith(".test.sh") or (
                name.startswith("test_") and name.endswith(".py")
            ):
                p = Path(dirpath) / name
                if p.is_symlink():
                    continue
                suites.append(p)
    suites.sort()
    return suites


def suite_relpath(root: Path, suite: Path) -> str:
    return suite.relative_to(root).as_posix()


def glob_match(relpath: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(relpath, pattern.rstrip("/") + "/*")


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

_PASSTHROUGH_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ")
_EXPLICIT_UNSET_KEYS = (
    "AGENT_HOME",
    "AGENT_DISPATCH_JOBS",
    "HARNESS_STATE_ROOT",
    "CLAUDE_HOME",
    "AGENT_ROUTE_FILE",
    "AGENT_ROUTE_ID",
    "AGENT_ROUTE_NODE",
    "AGENT_ARTIFACT_ROOT",
)


def build_isolated_env(tmpdir: Path) -> dict[str, str]:
    """The single owner of the isolation definition (Q4). No caller-ambient
    execution path exists anywhere in this runner; every subprocess gets an
    environment built by this function (or build_installed_layout_env, which
    layers on top of it) or by live-registry's explicit, declared exposure.
    """
    home = tmpdir / "home"
    xdg_state = tmpdir / "xdg-state"
    xdg_data = tmpdir / "xdg-data"
    xdg_cache = tmpdir / "xdg-cache"
    runner_tmp = tmpdir / "tmp"
    for d in (home, xdg_state, xdg_data, xdg_cache, runner_tmp):
        d.mkdir(parents=True, exist_ok=True)

    env: dict[str, str] = {}
    for key in _PASSTHROUGH_ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOME"] = str(home)
    env["XDG_STATE_HOME"] = str(xdg_state)
    env["XDG_DATA_HOME"] = str(xdg_data)
    env["XDG_CACHE_HOME"] = str(xdg_cache)
    env["TMPDIR"] = str(runner_tmp)
    # _EXPLICIT_UNSET_KEYS are simply omitted from `env` (env -i semantics: a
    # subprocess launched with this dict as its full environment never sees
    # them, regardless of what the caller's ambient shell has set).
    return env


def build_installed_layout_env(tmpdir: Path, install_prefix: Path) -> dict[str, str]:
    """Layer a simulated installed-release layout on top of the isolated env
    (Phase 4 / Step 4.2). Still no real $HOME exposure.
    """
    env = build_isolated_env(tmpdir)
    releases_dir = install_prefix / "home" / ".local" / "share" / "hearting" / "releases"
    release_dirs = sorted(releases_dir.glob("*")) if releases_dir.is_dir() else []
    # `installer.py install claude` (no bundled-release fixture set up) lands
    # the runtime pointer at <HOME>/.claude, with hooks/utilities/tools
    # symlinked from there -- the closest available "installed layout" shape
    # when no releases/<v> bundle exists in this fixture.
    agent_home = str(release_dirs[-1]) if release_dirs else str(install_prefix / "home" / ".claude")
    env["AGENT_HOME"] = agent_home
    env["XDG_DATA_HOME"] = str(install_prefix / "home" / ".local" / "share")
    env["XDG_STATE_HOME"] = str(install_prefix / "home" / ".local" / "state")
    return env


def build_ci_like_env(tmpdir: Path, repo_root: Path) -> dict[str, str]:
    """Layer a GitHub-runner-shaped environment on top of the isolated env
    (diagnostic/reproduction profile only, not used as the CI default). Same
    single-owner pattern as build_installed_layout_env: everything starts
    from build_isolated_env() and this function only adds to it.
    """
    env = build_isolated_env(tmpdir)
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    home = Path(env["HOME"])
    for d in (home / ".local" / "bin", home / ".local" / "share", home / ".local" / "state"):
        d.mkdir(parents=True, exist_ok=True)
    runner_temp = home / "work" / "_temp"
    runner_temp.mkdir(parents=True, exist_ok=True)
    env["RUNNER_TEMP"] = str(runner_temp)
    gitconfig = tmpdir / "gitconfig"
    gitconfig.write_text(f"[safe]\n\tdirectory = {repo_root}\n", encoding="utf-8")
    env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    env["HEARTING_ENV_LAYOUT"] = "github-runner"
    return env


# ---------------------------------------------------------------------------
# Environment fingerprint (MA-W1-011)
# ---------------------------------------------------------------------------

# A closed, declared probe list rather than "every binary on PATH". The full
# name set is ~2100 entries here, dominated by system packages, so one
# `apt upgrade` would change the hash and mark every baseline row foreign.
# These are the names the corpus actually shells out to, plus the shells.
FINGERPRINT_PROBE_BINARIES = (
    "bash", "bwrap", "claude", "codex", "ffmpeg", "gh", "git", "jq", "lsof",
    "node", "npm", "npx", "opencode", "python3", "sh", "sudo",
)


def fingerprint_inputs(env: dict[str, str], profile: str) -> dict:
    """The four declared fingerprint axes.

    Deliberately excluded: absolute paths, nproc, git/python versions, host
    name, timestamps. Any of those makes the fingerprint a host identity, every
    row foreign, and the baseline contract dead. --jobs and nproc are recorded
    in the report header instead, where they inform timing analysis without
    invalidating verdicts.
    """
    path = env.get("PATH", "")
    directories = [d for d in path.split(os.pathsep) if d]
    probes = {}
    for name in FINGERPRINT_PROBE_BINARIES:
        probes[name] = any(os.access(os.path.join(d, name), os.X_OK) for d in directories)
    if profile == "ci-like":
        layout = "github-runner"
    elif profile == "installed-layout":
        layout = "installed-release"
    else:
        layout = "synthetic"
    sandbox = "none"
    if env.get("HEARTING_REQUIRE_PIDNS") == "1":
        sandbox = "pidns"
    elif shutil.which("bwrap", path=env.get("PATH", "")):
        sandbox = "bwrap"
    return {
        "probes": probes,
        "sandbox": sandbox,
        "home_layout": layout,
        "os_family": f"{os.uname().sysname.lower()}-{'glibc' if sys.platform == 'linux' else 'unknown'}",
    }


def environment_fingerprint(env: dict[str, str], profile: str) -> str:
    payload = json.dumps(fingerprint_inputs(env, profile), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def baseline_row_applicable(row: dict, current_fingerprint: str) -> bool:
    """Whether a baseline row may be applied under the current fingerprint.

    An empty fingerprint means "unknown" and keeps the pre-fingerprint contract,
    so rows recorded before this column existed behave exactly as before.
    """
    recorded = (row.get("fingerprint") or "").strip()
    return recorded == "" or recorded == current_fingerprint


TEMP_PARENT_UNPROVEN_EXIT = 70


def resolve_git_executable() -> str | None:
    """Resolve the git binary explicitly. ``env={}`` on the probe means PATH
    lookup would silently depend on the exec default, so a non-standard Git
    install must be named rather than guessed."""
    configured = os.environ.get("RUN_TESTS_GIT")
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    found = shutil.which("git")
    if found:
        return found
    for fallback in ("/usr/bin/git", "/usr/local/bin/git", "/bin/git"):
        if os.access(fallback, os.X_OK):
            return fallback
    return None


def probe_git_containment(git_exe: str, candidate: Path) -> str:
    """Classify one candidate: "outside" (proven non-repository), "inside"
    (a real worktree), or "uncertain". Only a Git error that literally says
    "not a git repository" proves "outside"; every other failure -- missing
    binary, permission error, a broken or non-standard Git -- is uncertain
    and must never be read as a safe candidate."""
    try:
        probe = subprocess.run(
            [git_exe, "-C", str(candidate), "rev-parse", "--show-toplevel"],
            env={}, capture_output=True, text=True,
        )
    except OSError:
        return "uncertain"
    if probe.returncode == 0:
        return "inside" if probe.stdout.strip() else "uncertain"
    stderr = (probe.stderr or "").lower()
    if "not a git repository" in stderr or "not a git repo" in stderr:
        return "outside"
    return "uncertain"


def choose_suite_temp_parent() -> Path:
    """Choose a writable temp parent proven to sit outside every Git worktree."""
    candidates: list[Path] = []
    configured = os.environ.get("RUN_TESTS_TMP_ROOT")
    if configured:
        candidates.append(Path(configured))
    candidates.extend((Path("/var/tmp"), Path(tempfile.gettempdir())))
    git_exe = resolve_git_executable()
    if git_exe is None:
        print("temp-root-unproven: no usable git executable to probe candidates",
              file=sys.stderr)
        raise SystemExit(TEMP_PARENT_UNPROVEN_EXIT)
    rejected: list[str] = []
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            writable = os.access(candidate, os.W_OK)
        except OSError as exc:
            rejected.append(f"{candidate}: unusable ({exc.__class__.__name__})")
            continue
        if not writable:
            rejected.append(f"{candidate}: not writable")
            continue
        verdict = probe_git_containment(git_exe, candidate)
        if verdict == "outside":
            return candidate
        rejected.append(f"{candidate}: {verdict}")
    print("temp-root-inside-git-tree: no candidate proven outside a Git worktree; "
          + "; ".join(rejected), file=sys.stderr)
    raise SystemExit(TEMP_PARENT_UNPROVEN_EXIT)


_INSTALLED_LAYOUT_LOCK = threading.Lock()
_INSTALLED_LAYOUT_CACHE: dict[str, Path] = {}


def get_installed_layout_prefix() -> Path | None:
    """Build (once, cached) a simulated install prefix using the repo's own
    install entry point. Returns None if fixture construction fails, in
    which case the caller must hard-fail the whole installed-layout profile
    (no silent skip).
    """
    with _INSTALLED_LAYOUT_LOCK:
        cached = _INSTALLED_LAYOUT_CACHE.get("prefix")
        if cached is not None:
            return cached
        tmp = Path(tempfile.mkdtemp(prefix="w1-installed-layout-", dir=str(choose_suite_temp_parent())))
        env = build_isolated_env(tmp / "build-env")
        installer = ROOT / "tools" / "install" / "installer.py"
        if not installer.exists():
            return None
        install_home = tmp / "home"
        install_home.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(install_home)
        try:
            result = subprocess.run(
                [sys.executable, str(installer), "install", "claude", "--scope", "global", "--yes", "--json"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except Exception:
            return None
        try:
            payload = json.loads(result.stdout or "{}")
        except Exception:
            return None
        if any(not c.get("ok", True) for c in payload.get("checks", [])):
            return None
        _INSTALLED_LAYOUT_CACHE["prefix"] = tmp
        return tmp


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class SuiteResult:
    __slots__ = (
        "relpath",
        "returncode",
        "timed_out",
        "stdout",
        "stderr",
        "duration_s",
        "profile",
        "failing_test_ids",
    )

    def __init__(self, relpath, returncode, timed_out, stdout, stderr, duration_s, profile):
        self.relpath = relpath
        self.returncode = returncode
        self.timed_out = timed_out
        self.stdout = stdout
        self.stderr = stderr
        self.duration_s = duration_s
        self.profile = profile
        self.failing_test_ids = extract_failing_test_ids(stdout, stderr)

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out


_UNITTEST_FAIL_RE = re.compile(r"^(FAIL|ERROR):\s+(\S+)\s+\(([\w.]+)\)", re.MULTILINE)


def failure_signature(result: SuiteResult) -> str:
    """Return a stable, compact signature for the first concrete failure."""
    text = "\n".join((result.stderr or "", result.stdout or ""))
    patterns = [
        re.compile(r"^\s*(?:[\w.]+\.)?(?:[A-Za-z_]\w*)(?:Error|Exception):\s+.+$", re.MULTILINE),
        re.compile(r"^\s*reason=[^\s].*$", re.MULTILINE),
        re.compile(r"^\s*(?:ERROR|FAIL):\s+.+$", re.MULTILINE),
    ]
    line = ""
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            line = match.group(0).strip()
            break
    if not line:
        line = last_nonempty_line(text)
    line = re.sub(r"\s+", " ", line)
    line = re.sub(r"/(?:tmp|var/tmp)/[^ ]+", "<TMP>", line)
    return f"{classify_kind(result)}:{line}" if line else classify_kind(result)


def extract_failing_test_ids(stdout: str, stderr: str) -> list[str]:
    """Parse unittest-style 'FAIL: test_x (module.Class)' / 'ERROR: ...' lines
    into `Class.test_method` ids. Falls back to no per-test ids (whole-file
    granularity) when nothing recognizable is present — that is expected for
    *.test.sh suites and any suite not using unittest's default reporter.
    """
    ids: list[str] = []
    for blob in (stdout, stderr):
        for m in _UNITTEST_FAIL_RE.finditer(blob or ""):
            method, qualname = m.group(2), m.group(3)
            # qualname is "module.Class.method" (or "module.method" for a
            # bare function test) — the class is the second-to-last segment.
            parts = qualname.split(".")
            cls = parts[-2] if len(parts) >= 2 else parts[-1]
            ids.append(f"{cls}.{method}")
    return sorted(set(ids))


def run_suite(suite: Path, root: Path, env: dict[str, str], profile: str, timeout: int) -> SuiteResult:
    relpath = suite_relpath(root, suite)
    if suite.name.endswith(".sh"):
        cmd = ["bash", str(suite)]
    else:
        cmd = [sys.executable, str(suite)]
    started = datetime.datetime.now()
    timed_out = False
    # start_new_session=True makes the child the leader of its own process
    # group. That ordering matters: killpg() is only ever called after this
    # guarantee, because killing the runner's own group would kill the runner.
    # communicate(timeout=...) is kept as-is (hand-rolling capture invites a
    # pipe deadlock on large output); only the timeout path adds the group kill.
    proc = subprocess.Popen(
        cmd,
        cwd=str(root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        rc = 124
        out, err = reap_process_group(proc)
    duration = (datetime.datetime.now() - started).total_seconds()
    return SuiteResult(relpath, rc, timed_out, out, err, duration, profile)


def reap_process_group(proc: subprocess.Popen) -> tuple[str, str]:
    """Terminate a timed-out suite together with every descendant it left
    behind. A timed-out suite's supervisor or fake-server daemon otherwise
    survives the runner and pollutes the host (P3).

    Only safe because run_suite() started the child with
    start_new_session=True, so getpgid(child) is the child's own group and
    never the runner's.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None and pgid != os.getpgid(0):
        for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(pgid, sig)
            except OSError:
                break
            try:
                proc.wait(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
    else:  # pragma: no cover - defensive; the group guarantee should hold
        proc.kill()
    try:
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = "", ""
    return out or "", err or ""


def last_nonempty_line(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if stripped:
            return re.sub(r"\s+", " ", stripped)
    return ""


def classify_kind(result: SuiteResult) -> str:
    if result.timed_out:
        return "timeout"
    stderr_tail = (result.stderr or "")
    if "AssertionError" in stderr_tail or re.search(r"\bFAILED\b", stderr_tail):
        return "assertion"
    if re.search(r"(command not found|No such file or directory).*\b(binary|executable)?\b", stderr_tail, re.IGNORECASE) or \
       re.search(r": command not found$", last_nonempty_line(stderr_tail)):
        return "missing-binary"
    if re.search(r"(Traceback|ImportError|ModuleNotFoundError|SyntaxError|CollectionError)", stderr_tail):
        return "error"
    return "exit-nonzero"


# ---------------------------------------------------------------------------
# Baseline TSV (known-failure contract)
# ---------------------------------------------------------------------------


class BaselineError(Exception):
    pass


def _read_tsv(path: Path, columns: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not path.exists():
        return rows
    header_seen = False
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if not header_seen:
                header_seen = True
                continue
            if len(cols) != len(columns):
                raise BaselineError(f"{path}:{lineno}: expected {len(columns)} columns, got {len(cols)}")
            rows.append(dict(zip(columns, cols)))
    return rows


def select_baseline_row(rows: list[dict[str, str]], current_fingerprint: str) -> dict[str, str]:
    """Pick the one row that applies under ``current_fingerprint`` when a
    (suite, test) key carries several fingerprint-scoped rows: exact match,
    then the unscoped (empty fingerprint) row, then the first row -- which the
    downstream applicability check will treat as foreign."""

    current = (current_fingerprint or "").strip()
    for row in rows:
        if (row.get("fingerprint") or "").strip() == current and current:
            return row
    for row in rows:
        if not (row.get("fingerprint") or "").strip():
            return row
    return rows[0]


def load_baseline(path: Path, current_fingerprint: str = "") -> dict[tuple[str, str], dict[str, str]]:
    rows = _read_tsv(path, BASELINE_COLUMNS)
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    rows_by_key: dict[tuple[str, str], list[dict[str, str]]] = {}
    by_suite_test_ids: dict[str, set[str]] = {}
    for row in rows:
        suite_path, test_id = row["suite_path"], row["test_id"]
        if "*" in suite_path or "*" in test_id:
            raise BaselineError(f"wildcard not allowed: {suite_path} / {test_id}")
        if test_id != "-" and not re.match(r"^\w+\.\w+$", test_id):
            raise BaselineError(f"invalid test_id (must be '-' or Class.test_method): {test_id}")
        if not row["defect_id"]:
            raise BaselineError(f"empty defect_id for {suite_path} / {test_id}")
        if not DATE_RE.match(row["review_by"]):
            raise BaselineError(f"review_by must be YYYY-MM-DD: {row['review_by']!r} for {suite_path}")
        if row["expected_failure_kind"] not in EXPECTED_FAILURE_KINDS:
            raise BaselineError(f"expected_failure_kind not in closed vocabulary: {row['expected_failure_kind']!r}")
        key = (suite_path, test_id)
        # One row per (suite, test) *per environment*: several rows may share
        # a key only when every one of them carries a distinct, non-empty
        # fingerprint (the same test can fail with a different kind on the
        # CI runner than on the maintainer host, 2026-08-30). A repeated
        # fingerprint -- or a second unscoped row -- is still a duplicate.
        siblings = rows_by_key.setdefault(key, [])
        fp = (row.get("fingerprint") or "").strip()
        if any(((r.get("fingerprint") or "").strip() == fp) for r in siblings):
            raise BaselineError(f"duplicate row: {suite_path} / {test_id}")
        if siblings and (not fp or any(not (r.get("fingerprint") or "").strip() for r in siblings)):
            raise BaselineError(
                f"duplicate row: {suite_path} / {test_id} (fingerprint-scoped rows may not mix with an unscoped row)"
            )
        siblings.append(row)
        by_suite_test_ids.setdefault(suite_path, set()).add(test_id)

    for suite_path, ids in by_suite_test_ids.items():
        if "-" in ids and len(ids) > 1:
            raise BaselineError(f"ambiguous rows: {suite_path} has both '-' and specific test_id rows")
    for key, siblings in rows_by_key.items():
        by_key[key] = select_baseline_row(siblings, current_fingerprint)
    return by_key


def load_isolation_tsv(path: Path) -> dict[str, dict[str, str]]:
    rows = _read_tsv(path, ISOLATION_COLUMNS)
    by_suite: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["needs"] not in ("installed-layout", "live-registry"):
            raise BaselineError(f"needs must be installed-layout|live-registry: {row['needs']!r}")
        if row["suite_path"] in by_suite:
            raise BaselineError(f"duplicate isolation row: {row['suite_path']}")
        by_suite[row["suite_path"]] = row
    return by_suite


def is_expired(review_by: str, today: str) -> bool:
    return review_by < today


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

HARD_FAIL_VERDICTS = {
    "XPASS",
    "STALE",
    "EXPIRED",
    "KIND-MISMATCH",
    "FAIL",
    "TIMEOUT",
    "ERROR",
    "UNDECLARED-ISOLATION-OPTOUT",
    "ISOLATION-OPTOUT-UNNEEDED",
}


class Verdict:
    def __init__(self, suite_path, test_id, verdict, detail=""):
        self.suite_path = suite_path
        self.test_id = test_id
        self.verdict = verdict
        self.detail = detail


def classify_result(
    result: SuiteResult,
    baseline: dict[tuple[str, str], dict[str, str]],
    today: str,
    fingerprint: str = "",
) -> list[Verdict]:
    """Classify a single suite execution into one or more per-test verdicts.

    Fingerprint handling (MA-W1-011) is a row *applicability filter*, never a
    verdict that preempts the failure paths. A foreign row simply cannot be
    applied, so a failing suite matched only by foreign rows is treated exactly
    as if it had no baseline row at all: an unlisted FAIL. BASELINE-FOREIGN is
    produced only where the suite passed, replacing XPASS with a held verdict.
    That asymmetry is the whole point -- the fingerprint must never be able to
    turn a real failure green.
    """
    suite_path = result.relpath
    verdicts: list[Verdict] = []

    all_whole_file = baseline.get((suite_path, "-"))
    all_specific = {
        test_id: row for (sp, test_id), row in baseline.items() if sp == suite_path and test_id != "-"
    }
    applicable = (lambda row: row is not None and baseline_row_applicable(row, fingerprint))
    whole_file_entry = all_whole_file if applicable(all_whole_file) else None
    specific_entries = {
        test_id: row for test_id, row in all_specific.items() if applicable(row)
    }
    foreign_whole_file = all_whole_file is not None and whole_file_entry is None
    foreign_specific = {
        test_id for test_id, row in all_specific.items() if test_id not in specific_entries
    }

    if result.passed:
        # Suite passed outright. Any baseline entry for this suite is now
        # stale/unneeded — the list only shrinks (owner addendum / plan R4-Q3).
        # A row recorded under a different environment cannot support that
        # judgement, so it is held rather than reported as an unexpected pass.
        if whole_file_entry is not None:
            verdicts.append(Verdict(suite_path, "-", "XPASS"))
        elif foreign_whole_file:
            verdicts.append(Verdict(suite_path, "-", "BASELINE-FOREIGN",
                                    detail="baseline row recorded under another environment"))
        for test_id, row in specific_entries.items():
            verdicts.append(Verdict(suite_path, test_id, "XPASS"))
        for test_id in sorted(foreign_specific):
            verdicts.append(Verdict(suite_path, test_id, "BASELINE-FOREIGN",
                                    detail="baseline row recorded under another environment"))
        if not verdicts:
            verdicts.append(Verdict(suite_path, "-", "PASS"))
        return verdicts

    # Suite failed (nonzero exit or timeout).
    kind = classify_kind(result)
    failing_ids = result.failing_test_ids

    if not failing_ids:
        # Whole-file granularity only (sh suite, collection error, or a
        # reporter format run_suite() cannot parse).
        row = whole_file_entry
        if row is None:
            # A foreign row is diagnostically different from no row at all --
            # same FAIL verdict, but --seed-baseline reads this token to propose
            # a row stamped with the current fingerprint.
            suffix = " (baseline-foreign)" if foreign_whole_file else ""
            if specific_entries or foreign_specific:
                # baseline expects specific tests but we only observed a
                # whole-file failure signature: unlisted at whole-file grain.
                verdicts.append(Verdict(suite_path, "-", "FAIL",
                                        detail="unlisted whole-file failure" + suffix))
            else:
                verdicts.append(Verdict(suite_path, "-", "FAIL",
                                        detail="unlisted failure" + suffix))
            return verdicts
        if is_expired(row["review_by"], today):
            verdicts.append(Verdict(suite_path, "-", "EXPIRED"))
            return verdicts
        if row["expected_failure_kind"] != kind:
            verdicts.append(
                Verdict(suite_path, "-", "KIND-MISMATCH", detail=f"expected={row['expected_failure_kind']} actual={kind}")
            )
            return verdicts
        verdicts.append(Verdict(suite_path, "-", "KNOWN-FAIL"))
        return verdicts

    # Per-test failure ids available.
    for test_id in failing_ids:
        row = specific_entries.get(test_id) or whole_file_entry
        if row is None:
            was_foreign = test_id in foreign_specific or foreign_whole_file
            verdicts.append(Verdict(
                suite_path, test_id, "FAIL",
                detail="unlisted failure" + (" (baseline-foreign)" if was_foreign else ""),
            ))
            continue
        if is_expired(row["review_by"], today):
            verdicts.append(Verdict(suite_path, test_id, "EXPIRED"))
            continue
        if row["expected_failure_kind"] != kind:
            verdicts.append(
                Verdict(suite_path, test_id, "KIND-MISMATCH", detail=f"expected={row['expected_failure_kind']} actual={kind}")
            )
            continue
        verdicts.append(Verdict(suite_path, test_id, "KNOWN-FAIL"))
    return verdicts


# ---------------------------------------------------------------------------
# Leak sweep
# ---------------------------------------------------------------------------


def live_state_snapshot() -> set[str]:
    live_root = Path(os.path.expanduser("~")) / ".local" / "state" / "hearting"
    if not live_root.is_dir():
        return set()
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(live_root, followlinks=False):
        for name in filenames:
            out.add(str(Path(dirpath) / name))
    return out


# ---------------------------------------------------------------------------
# Report I/O
# ---------------------------------------------------------------------------

REPORT_COLUMNS = [
    "suite_path",
    "test_id",
    "verdict",
    "kind",
    "isolation_profile",
    "duration_s",
    "detail",
]


def write_report(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write("# tools/run-tests.py report\n")
        fh.write("\t".join(REPORT_COLUMNS) + "\n")
        for row in rows:
            fh.write("\t".join(str(row.get(c, "")) for c in REPORT_COLUMNS) + "\n")


SEED_VERDICTS = {"FAIL", "TIMEOUT", "ERROR", "EXPIRED", "KIND-MISMATCH"}


def write_seed_baseline(path: Path, rows: list[dict[str, str]], fingerprint: str, today: str) -> int:
    """Write a *proposed* baseline stamped with the current fingerprint.

    This is an artifact for a human to review, never an automatic replacement
    for the repository baseline: seeding a failure into the baseline is exactly
    the move that hides a real defect, so the decision stays with a person.
    """
    review_by = f"{int(today[:4]) + 1}{today[4:]}"
    proposed = []
    for row in rows:
        if row.get("verdict") not in SEED_VERDICTS:
            continue
        proposed.append({
            "suite_path": row["suite_path"],
            "test_id": row.get("test_id") or "-",
            "expected_failure_kind": row.get("kind") or "assertion",
            "isolation_profile": row.get("isolation_profile") or "isolated",
            "reason": f"seed: {row.get('detail', '')}".strip()[:400] or "seed: observed failure",
            "defect_id": "SEED-REVIEW",
            "review_by": review_by,
            "fingerprint": fingerprint,
        })
    with path.open("w", encoding="utf-8") as fh:
        fh.write(f"# proposed baseline rows observed under fingerprint={fingerprint}\n")
        fh.write("# review before adopting: a seeded row silences a real failure\n")
        fh.write("\t".join(BASELINE_COLUMNS) + "\n")
        for row in proposed:
            fh.write("\t".join(row[c] for c in BASELINE_COLUMNS) + "\n")
    return len(proposed)


def read_report(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    header_seen = False
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if not header_seen:
                header_seen = True
                continue
            if len(cols) != len(REPORT_COLUMNS):
                continue
            rows.append(dict(zip(REPORT_COLUMNS, cols)))
    return rows


# ---------------------------------------------------------------------------
# --compare-baseline
# ---------------------------------------------------------------------------


def compare_baseline(base_report: Path, head_report: Path) -> int:
    base_rows = read_report(base_report)
    head_rows = read_report(head_report)

    def failing_keys(rows):
        keys = set()
        for row in rows:
            if row["verdict"] not in ("PASS", "KNOWN-FAIL", "FLAKY-KNOWN-FAIL"):
                keys.add((row["suite_path"], row["test_id"]))
            elif row["verdict"] in ("KNOWN-FAIL", "FLAKY-KNOWN-FAIL"):
                # A known-fail in a --report-only run still represents an
                # observed failure at that point in history.
                keys.add((row["suite_path"], row["test_id"]))
        return keys

    base_fail = failing_keys(base_rows)
    head_fail = failing_keys(head_rows)

    pre_existing = sorted(base_fail & head_fail)
    regression = sorted(head_fail - base_fail)
    fixed = sorted(base_fail - head_fail)

    out = {
        "pre_existing": [{"suite_path": s, "test_id": t} for s, t in pre_existing],
        "regression": [{"suite_path": s, "test_id": t} for s, t in regression],
        "fixed": [{"suite_path": s, "test_id": t} for s, t in fixed],
    }
    print(json.dumps(out, sort_keys=True))
    return 1 if regression else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--census", action="store_true")
    p.add_argument("--select", action="append", default=[])
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--retries", type=int, default=0, help="extra attempts for flaky-timing baseline suites")
    p.add_argument(
        "--retry-budget", type=int, default=None,
        help="total seconds the serial retry pass may spend (default: --timeout)",
    )
    p.add_argument(
        "--isolation",
        choices=ISOLATION_PROFILES,
        default="isolated",
        help="Explicit single-profile override (used by Phase 5 narrow_verify commands). "
        "Without this flag the per-suite profile is decided by tools/test-isolation.tsv.",
    )
    p.add_argument("--report-only", action="store_true")
    p.add_argument(
        "--xpass-nonfatal", action="store_true",
        help="report XPASS (baseline known-fail that passed) without failing the run; "
             "for environments that did not seed the baseline (MA-W1-011)",
    )
    p.add_argument("--report", type=Path, default=None)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--baseline", type=Path, default=ROOT / "tools" / "test-baseline.tsv")
    p.add_argument("--isolation-tsv", type=Path, default=ROOT / "tools" / "test-isolation.tsv")
    p.add_argument("--no-leak-sweep", action="store_true", help="diagnostic only; never used in CI")
    p.add_argument("--compare-baseline", action="store_true")
    p.add_argument("--fingerprint", action="store_true",
                   help="print the current environment fingerprint and exit")
    p.add_argument("--fingerprint-explain", action="store_true",
                   help="print the fingerprint inputs as JSON and exit")
    p.add_argument("--seed-baseline", type=Path, default=None,
                   help="write a proposed baseline TSV stamped with the current "
                        "fingerprint; never rewrites the repository baseline")
    p.add_argument("--base-report", type=Path)
    p.add_argument("--head-report", type=Path)
    return p


def cmd_census(suites: list[Path], root: Path) -> int:
    py = sum(1 for s in suites if s.name.endswith(".py"))
    sh = sum(1 for s in suites if s.name.endswith(".sh"))
    print(f"collected={len(suites)} py={py} sh={sh}")
    return 0


def run_profile(
    suites: list[Path],
    root: Path,
    profile: str,
    jobs: int,
    timeout: int,
    install_prefix: Path | None,
) -> list[SuiteResult]:
    results: list[SuiteResult] = []
    tmp_roots: list[Path] = []

    def make_env(idx: int) -> dict[str, str]:
        tmp = Path(tempfile.mkdtemp(prefix=f"w1-{profile}-{idx}-", dir=str(choose_suite_temp_parent())))
        tmp_roots.append(tmp)
        if profile == "isolated":
            return build_isolated_env(tmp)
        if profile == "installed-layout":
            assert install_prefix is not None
            return build_installed_layout_env(tmp, install_prefix)
        if profile == "live-registry":
            env = build_isolated_env(tmp)
            env["HOME"] = os.environ.get("HOME", env["HOME"])
            return env
        if profile == "ci-like":
            return build_ci_like_env(tmp, root)
        raise ValueError(profile)

    try:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            futures = []
            for idx, suite in enumerate(suites):
                env = make_env(idx)
                futures.append(pool.submit(run_suite, suite, root, env, profile, timeout))
            for fut in futures:
                results.append(fut.result())
    finally:
        for tmp in tmp_roots:
            shutil.rmtree(tmp, ignore_errors=True)
    return results


def main(argv: list[str]) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.compare_baseline:
        if not args.base_report or not args.head_report:
            print("--compare-baseline requires --base-report and --head-report", file=sys.stderr)
            return 64
        return compare_baseline(args.base_report, args.head_report)

    # The fingerprint describes the environment suites will actually run in, so
    # it is computed from a representative profile env rather than the caller's.
    with tempfile.TemporaryDirectory(prefix="fingerprint-") as fp_tmp:
        fp_root = Path(fp_tmp)
        if args.isolation == "ci-like":
            fp_env = build_ci_like_env(fp_root, args.root.resolve())
        else:
            fp_env = build_isolated_env(fp_root)
        run_fingerprint = environment_fingerprint(fp_env, args.isolation)
        fp_inputs = fingerprint_inputs(fp_env, args.isolation)

    if args.fingerprint_explain:
        print(json.dumps({"fingerprint": run_fingerprint, "inputs": fp_inputs},
                         sort_keys=True, indent=2))
        return 0
    if args.fingerprint:
        print(run_fingerprint)
        return 0

    root = args.root.resolve()
    suites = collect_suites(root)

    relpaths = [suite_relpath(root, s) for s in suites]
    selected = suites
    if args.select:
        selected = [s for s, r in zip(suites, relpaths) if any(glob_match(r, g) for g in args.select)]
    if args.exclude:
        rel_selected = [suite_relpath(root, s) for s in selected]
        selected = [s for s, r in zip(selected, rel_selected) if not any(glob_match(r, g) for g in args.exclude)]

    if args.census:
        return cmd_census(selected, root)

    try:
        baseline = load_baseline(args.baseline, run_fingerprint)
        isolation_tsv = load_isolation_tsv(args.isolation_tsv)
    except BaselineError as exc:
        print(f"baseline parse error: {exc}", file=sys.stderr)
        return 65

    today = datetime.date.today().isoformat()

    before_leak = set() if args.no_leak_sweep else live_state_snapshot()

    # STALE must be judged against the full repo corpus (a suite file that no
    # longer exists), not the --exclude-narrowed run set — excluding one known
    # suite from an otherwise-full run does not make other baseline rows
    # stale. --select is different: it expresses "only look at this narrow
    # subset", so a baseline row outside every --select glob is simply out of
    # scope for this invocation, not evidence of staleness.
    full_corpus_relpaths = {suite_relpath(root, s) for s in suites}
    stale_rows: list[dict[str, str]] = []
    for (suite_path, test_id) in baseline:
        if args.select and not any(glob_match(suite_path, g) for g in args.select):
            continue
        if suite_path not in full_corpus_relpaths:
            stale_rows.append(
                {
                    "suite_path": suite_path,
                    "test_id": test_id,
                    "verdict": "STALE",
                    "kind": "",
                    "isolation_profile": "",
                    "duration_s": "",
                    "detail": "baseline suite file not collected (missing or renamed)",
                }
            )

    # Decide per-suite profile.
    explicit_profile = args.isolation
    isolation_flag_given = any(a == "--isolation" or a.startswith("--isolation=") for a in argv)
    profile_of: dict[str, str] = {}
    for r in (suite_relpath(root, s) for s in selected):
        needs_row = isolation_tsv.get(r)
        if needs_row is not None and not isolation_flag_given:
            profile_of[r] = needs_row["needs"]
        else:
            profile_of[r] = explicit_profile

    suites_by_profile: dict[str, list[Path]] = {p: [] for p in ISOLATION_PROFILES}
    for suite, r in zip(selected, (suite_relpath(root, s) for s in selected)):
        suites_by_profile[profile_of[r]].append(suite)

    install_prefix = None
    if suites_by_profile["installed-layout"]:
        install_prefix = get_installed_layout_prefix()
        if install_prefix is None:
            print("FATAL: installed-layout fixture construction failed", file=sys.stderr)
            if not args.report_only:
                return 70

    all_results: list[SuiteResult] = []
    results_by_suite: dict[str, list[SuiteResult]] = {}
    for profile in ISOLATION_PROFILES:
        batch = suites_by_profile[profile]
        if not batch:
            continue
        batch_results = run_profile(batch, root, profile, args.jobs, args.timeout, install_prefix)
        all_results.extend(batch_results)
        for result in batch_results:
            results_by_suite.setdefault(result.relpath, []).append(result)

    # Retries are deliberately limited to baseline rows explicitly marked as
    # flaky-timing. Every attempt receives a fresh isolated environment.
    if args.retries < 0:
        print("--retries must be non-negative", file=sys.stderr)
        return 64
    retry_budget_exhausted: set[str] = set()
    if args.retries:
        # The retry pass re-runs flaky suites serially after the main run, and
        # nothing bounded its total cost: N flaky suites could each consume the
        # full per-suite timeout. The per-suite timeout still applies to every
        # attempt; --retry-budget bounds the sum (P3).
        retry_budget = args.retry_budget if args.retry_budget is not None else args.timeout
        retry_spent = 0.0
        for suite in selected:
            rel = suite_relpath(root, suite)
            rows = [row for (sp, _), row in baseline.items() if sp == rel]
            # A foreign row cannot select a suite for retry aggregation either.
            # The aggregate branch below computes its verdict directly instead
            # of going through classify_result(), so without this filter it
            # would be a side entrance around the P8 contract: a failing suite
            # whose only row is foreign could come out KNOWN-FAIL.
            if not any(
                row["reason"].startswith("flaky-timing:")
                and baseline_row_applicable(row, run_fingerprint)
                for row in rows
            ):
                continue
            profile = profile_of[rel]
            extra: list[SuiteResult] = []
            for _ in range(args.retries):
                remaining = retry_budget - retry_spent
                if remaining <= 0:
                    retry_budget_exhausted.add(rel)
                    break
                # Checking the budget only *before* an attempt does not bound
                # it: with 1s left and --timeout 600 the attempt could still
                # run 600s. Clamp this attempt's timeout to what is left.
                attempt_timeout = max(1, min(args.timeout, int(remaining)))
                attempt_results = run_profile(
                    [suite], root, profile, 1, attempt_timeout, install_prefix
                )
                extra.extend(attempt_results)
                retry_spent += sum(r.duration_s for r in attempt_results)
                if attempt_timeout < args.timeout:
                    retry_budget_exhausted.add(rel)
            results_by_suite[rel].extend(extra)

    # Undeclared/unneeded isolation opt-out detection: only for suites that
    # actually failed under `isolated` and have no declared opt-out anywhere.
    # Each hard-fail record is (key, verdict, kind, signature) so the summary
    # line can name what happened, not just which suite::test failed.
    hard_failures: list[tuple[str, str, str, str]] = []
    xpass_nonfatal: list[str] = []
    report_rows: list[dict[str, str]] = []
    verdict_counts: dict[str, int] = {}

    isolation_sensitive: dict[str, bool] = {}
    for result in all_results:
        if result.profile != "isolated" or result.passed:
            continue
        r = result.relpath
        if r in isolation_tsv:
            continue  # declared; handled by ISOLATION-OPTOUT-UNNEEDED below via its own profile run.
        if (r, "-") in baseline or any(sp == r for (sp, _tid) in baseline):
            continue  # covered by baseline known-failure contract instead.
        if install_prefix is None:
            continue
        env_tmp = Path(tempfile.mkdtemp(prefix="w1-isosense-", dir=str(choose_suite_temp_parent())))
        try:
            probe_env = build_installed_layout_env(env_tmp, install_prefix)
            probe = run_suite(root / r, root, probe_env, "installed-layout", args.timeout)
        finally:
            shutil.rmtree(env_tmp, ignore_errors=True)
        if probe.passed:
            isolation_sensitive[r] = True

    for suite_path in isolation_sensitive:
        report_rows.append(
            {
                "suite_path": suite_path,
                "test_id": "-",
                "verdict": "UNDECLARED-ISOLATION-OPTOUT",
                "kind": "",
                "isolation_profile": "isolated",
                "duration_s": "",
                "detail": "isolated FAIL + installed-layout PASS, not declared in test-isolation.tsv or test-baseline.tsv",
            }
        )
        verdict_counts["UNDECLARED-ISOLATION-OPTOUT"] = verdict_counts.get("UNDECLARED-ISOLATION-OPTOUT", 0) + 1
        hard_failures.append((
            f"{suite_path}::-", "UNDECLARED-ISOLATION-OPTOUT", "",
            "isolated FAIL + installed-layout PASS, not declared",
        ))

    for suite_path, row in isolation_tsv.items():
        matching = [r for r in all_results if r.relpath == suite_path and r.profile == "isolated"]
        if matching and matching[0].passed:
            report_rows.append(
                {
                    "suite_path": suite_path,
                    "test_id": "-",
                    "verdict": "ISOLATION-OPTOUT-UNNEEDED",
                    "kind": "",
                    "isolation_profile": "isolated",
                    "duration_s": "",
                    "detail": "declared opt-out but passes under isolated",
                }
            )
            verdict_counts["ISOLATION-OPTOUT-UNNEEDED"] = verdict_counts.get("ISOLATION-OPTOUT-UNNEEDED", 0) + 1
            hard_failures.append((
                f"{suite_path}::-", "ISOLATION-OPTOUT-UNNEEDED", "",
                "declared opt-out but passes under isolated",
            ))

    processed_flaky: set[str] = set()
    for result in all_results:
        needs_row = isolation_tsv.get(result.relpath)
        skip_leak = needs_row is not None and needs_row.get("needs") == "live-registry"
        attempt_results = results_by_suite.get(result.relpath, [result])
        flaky_rows = [
            row for (sp, _), row in baseline.items()
            if sp == result.relpath
            and row["reason"].startswith("flaky-timing:")
            and baseline_row_applicable(row, run_fingerprint)
        ]
        if args.retries and flaky_rows:
            # all_results contains the first attempt for each suite; the
            # retry attempts are retained in results_by_suite. Emit one
            # aggregate row per flaky suite, not one row per attempt.
            if result.relpath in processed_flaky:
                continue
            processed_flaky.add(result.relpath)
            expected = {row["expected_failure_kind"] for row in flaky_rows}
            outcomes = []
            mismatch = False
            for attempt in attempt_results:
                if attempt.passed:
                    outcomes.append("pass")
                else:
                    actual = classify_kind(attempt)
                    if actual not in expected:
                        mismatch = True
                        outcomes.append("kind-mismatch")
                    else:
                        outcomes.append("known-fail")
            # Aggregating per-attempt rows into one whole-file row loses which
            # test failed; keep the failing ids so a different failure inside a
            # flaky suite cannot hide behind the aggregate (P3).
            failing_ids = sorted({
                test_id
                for attempt in attempt_results
                if not attempt.passed
                for test_id in attempt.failing_test_ids
            })
            detail = f"attempts={len(outcomes)}; outcomes={','.join(outcomes)}; policy=flaky-timing"
            if failing_ids:
                detail += f"; failing={','.join(failing_ids)}"
            if result.relpath in retry_budget_exhausted:
                detail += "; retry-budget-exhausted"
            verdict = "KIND-MISMATCH" if mismatch else ("XPASS" if all(x == "pass" for x in outcomes) else "KNOWN-FAIL" if all(x == "known-fail" for x in outcomes) else "FLAKY-KNOWN-FAIL")
            verdicts = [Verdict(result.relpath, "-", verdict, detail=detail)]
        else:
            verdicts = classify_result(result, baseline, today, run_fingerprint)
        for v in verdicts:
            verdict_counts[v.verdict] = verdict_counts.get(v.verdict, 0) + 1
            result_kind = classify_kind(result) if not result.passed else ""
            signature = failure_signature(result) if not result.passed else ""
            report_rows.append(
                {
                    "suite_path": v.suite_path,
                    "test_id": v.test_id,
                    "verdict": v.verdict,
                    "kind": result_kind,
                    "isolation_profile": result.profile,
                    "duration_s": f"{result.duration_s:.2f}",
                    "detail": v.detail or (f"signature={signature}" if not result.passed else ""),
                }
            )
            if v.verdict in HARD_FAIL_VERDICTS:
                if v.verdict == "XPASS" and args.xpass_nonfatal:
                    # MA-W1-007/MA-W1-011: the known-failure baseline was seeded
                    # in one environment; a suite that passes elsewhere is an
                    # unexpected pass to review, not a regression. CI opts in
                    # until the baseline carries an environment fingerprint.
                    xpass_nonfatal.append(f"{v.suite_path}::{v.test_id}")
                else:
                    hard_failures.append((f"{v.suite_path}::{v.test_id}", v.verdict, result_kind, v.detail or signature))
        _ = skip_leak  # per-suite leak exemption is enforced globally below

    for row in stale_rows:
        verdict_counts[row["verdict"]] = verdict_counts.get(row["verdict"], 0) + 1
        report_rows.append(row)
        hard_failures.append((f"{row['suite_path']}::{row['test_id']}", row["verdict"], "", row["detail"]))

    collected_total = len(selected)
    profile_subtotal = sum(len(v) for v in suites_by_profile.values())
    if profile_subtotal != collected_total:
        print(
            f"FATAL: profile subtotal ({profile_subtotal}) != collected ({collected_total})",
            file=sys.stderr,
        )
        hard_failures.append((
            "__profile_subtotal_mismatch__::-", "ERROR", "internal",
            f"profile subtotal ({profile_subtotal}) != collected ({collected_total})",
        ))

    leak_new: set[str] = set()
    if not args.no_leak_sweep:
        after_leak = live_state_snapshot()
        live_registry_paths = {sp for sp, row in isolation_tsv.items() if row["needs"] == "live-registry"}
        new_entries = after_leak - before_leak
        if new_entries and live_registry_paths:
            # A live-registry opt-out is a declared, deliberate exception:
            # entries are only excused when at least one such suite ran.
            leak_new = new_entries
        else:
            leak_new = new_entries
        if leak_new:
            for entry in sorted(leak_new):
                print(f"LIVE-STATE-LEAK: {entry}", file=sys.stderr)
            if not live_registry_paths:
                hard_failures.append((
                    "__live_state_leak__::-", "ERROR", "internal",
                    f"{len(leak_new)} new live-state path(s) written during the run",
                ))

    if args.report:
        write_report(args.report, report_rows)
    if args.seed_baseline:
        # The proposal must never become the contract by accident: writing it
        # over the configured baseline would replace the whole row set with the
        # current run's hard failures.
        if args.seed_baseline.resolve() == args.baseline.resolve():
            print("--seed-baseline must not target the configured --baseline",
                  file=sys.stderr)
            return 64
        seeded = write_seed_baseline(args.seed_baseline, report_rows, run_fingerprint, today)
        print(f"seed-baseline={args.seed_baseline} rows={seeded}")

    total = len(selected)
    print(f"collected={total}")
    print(f"env: jobs={args.jobs} nproc={os.cpu_count()} timeout={args.timeout} profile={explicit_profile}")
    print(f"fingerprint={run_fingerprint}")
    for key in ("PASS", "FAIL", "ERROR", "TIMEOUT", "KNOWN-FAIL", "FLAKY-KNOWN-FAIL", "XPASS", "STALE", "EXPIRED", "KIND-MISMATCH",
                "BASELINE-FOREIGN",
                "UNDECLARED-ISOLATION-OPTOUT", "ISOLATION-OPTOUT-UNNEEDED"):
        if key in verdict_counts:
            print(f"{key}={verdict_counts[key]}")
    unclassified = sum(1 for row in report_rows if row["verdict"] == "KNOWN-FAIL")
    for profile in ISOLATION_PROFILES:
        print(f"profile[{profile}]={len(suites_by_profile[profile])}")

    if xpass_nonfatal:
        print(f"XPASS-NONFATAL={len(xpass_nonfatal)}")
        for item in xpass_nonfatal:
            print(f"xpass: {item}")
    if hard_failures:
        # Name every hard failure in the summary: a CI log that only carries
        # the counts cannot be diagnosed without the report artifact
        # (2026-08-30, six CI-only failures with no suite names).
        print(f"HARD-FAIL={len(hard_failures)}")
        for key, verdict, kind, signature in hard_failures[:100]:
            print(f"hard-fail: {key} verdict={verdict} kind={kind} signature={signature}")
    if args.report_only:
        return 0
    return 1 if hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
