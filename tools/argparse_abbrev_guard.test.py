#!/usr/bin/env python3
"""AST guard: every tracked CLI that owns a boundary/destructive flag must
disable argparse's prefix-abbreviation on all of its ArgumentParser and
add_subparsers calls (plan item 2 -- `--include-w` silently arming
`--include-w16-namespace-delete`)."""
import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FLAG_RE = re.compile(
    r"^--(.*(force|delete|drop|prune|purge|wipe|remove|abandon|overwrite|kill"
    r"|reset|unproven|cascade|destroy|truncate|rewrite|evict|retire)|yes|apply)"
)


def tracked_py_files():
    # -c safe.directory=ROOT: the isolated test runner launches this suite
    # under a fresh $HOME, which trips git's dubious-ownership refusal for a
    # repository owned by a different (real) user than that empty profile.
    out = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "ls-files", "--", "*.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    return sorted(
        ROOT / line for line in out.splitlines()
        if line and not line.endswith(".test.py")
    )


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _add_argument_flags(tree):
    flags = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "add_argument":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--"):
                    flags.append(arg.value)
    return flags


def is_boundary_file(path, text, tree):
    # Plan item 2 also names a `# destructive-ok` file-level opt-in as a
    # boundary-file fallback, but that exact string is already an established,
    # differently-scoped repo convention: `# destructive-ok: reason=...;
    # boundary=...` annotates one proven-safe os.unlink/os.remove call, not a
    # whole CLI's flag surface (see tools/install/codex_launcher.py:358,
    # tools/install/user_model_config.py:35, utilities/artifact_restore_sealed.py:596,
    # utilities/peer-steward.py:211 -- none of which own an actual destructive
    # flag). Reusing it here would misclassify those 4 unrelated files as
    # boundary CLIs by plain substring collision. No file in this slice's
    # fixed_files needs the fallback (every one is caught by FLAG_RE below),
    # so it is dropped rather than guessed at; see dev log for the reported gap.
    return any(FLAG_RE.match(flag) for flag in _add_argument_flags(tree))


def _has_false_kw(call_node, kw_name):
    for kw in call_node.keywords:
        if kw.arg == kw_name:
            return isinstance(kw.value, ast.Constant) and kw.value.value is False
    return False


def _has_kw(call_node, kw_name):
    return any(kw.arg == kw_name for kw in call_node.keywords)


def missing_guard(tree):
    """`(call_kind, lineno)` for every ArgumentParser/add_subparsers call in
    `tree` that is missing its required abbreviation guard."""
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "ArgumentParser":
            if not _has_false_kw(node, "allow_abbrev"):
                violations.append(("ArgumentParser", node.lineno))
        elif name == "add_subparsers":
            if not _has_kw(node, "parser_class"):
                violations.append(("add_subparsers", node.lineno))
    return violations


def _parse(path):
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        # A few tracked `*.py` paths are non-Python payloads (e.g. shell
        # wrapper scripts with a `.py` suffix); they own no argparse call.
        return text, None
    return text, tree


class ArgparseAbbrevGuardTest(unittest.TestCase):
    def test_boundary_flag_parsers_disable_abbrev(self):
        violations = {}
        for path in tracked_py_files():
            text, tree = _parse(path)
            if tree is None:
                continue
            if not is_boundary_file(path, text, tree):
                continue
            v = missing_guard(tree)
            if v:
                violations[str(path.relative_to(ROOT))] = v
        self.assertEqual(
            violations, {},
            f"boundary-flag CLIs missing allow_abbrev=False / parser_class guard: {violations}",
        )

    def test_subcommand_prefix_refused(self):
        # Parses only -- the real command bodies (mem.delete_record,
        # artifact_producer.finalize) are stubbed out so a pre-fix parse that
        # wrongly *succeeds* on the abbreviated flag can never reach live state.
        import importlib.util

        sys.path.insert(0, str(ROOT / "utilities"))

        mem_spec = importlib.util.spec_from_file_location("mem_guard_check", ROOT / "tools" / "memory" / "mem.py")
        mem = importlib.util.module_from_spec(mem_spec)
        sys.modules[mem_spec.name] = mem
        mem_spec.loader.exec_module(mem)
        mem.delete_record = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("guard test must not reach the real delete_record")
        )

        ap_spec = importlib.util.spec_from_file_location("artifact_producer_guard_check", ROOT / "utilities" / "artifact_producer.py")
        ap = importlib.util.module_from_spec(ap_spec)
        sys.modules[ap_spec.name] = ap
        ap_spec.loader.exec_module(ap)
        ap.dispatch_terminal_commit.require_current_cleanup = lambda *a, **kw: None
        ap.finalize = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("guard test must not reach the real finalize")
        )

        old_argv = sys.argv
        try:
            sys.argv = ["mem", "delete", "some-id", "--forc"]
            with self.assertRaises(SystemExit) as ctx:
                mem.main()
            self.assertEqual(ctx.exception.code, 2)
        finally:
            sys.argv = old_argv

        with self.assertRaises(SystemExit) as ctx:
            ap.main(["finalize", "--artifact-root", "/tmp/x", "--cycle", "cyc_x", "--force-abandon"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
