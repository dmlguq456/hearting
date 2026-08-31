#!/usr/bin/env python3
"""Reject unproved destructive installer calls and non-hermetic fixtures."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Iterable


ANNOTATION = re.compile(
    r"destructive-ok:\s*reason=(?P<reason>[^;]+);\s*boundary=(?P<boundary>.+?)\s*$"
)
SHELL_DESTRUCTIVE = re.compile(r"(?:^|[;'\"|&()\s])(rm|rmdir|mv)\s+")
HOME_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?HOME=", re.MULTILINE)


@dataclass(frozen=True)
class Finding:
    category: str
    path: Path
    line: int
    detail: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.category}: {self.detail}"


def _annotation(lines: list[str], line: int) -> bool:
    for index in range(max(0, line - 4), line):
        match = ANNOTATION.search(lines[index])
        if not match:
            continue
        reason = match.group("reason").strip()
        boundary = match.group("boundary").strip()
        if reason and boundary and "*" not in boundary:
            return True
    return False


def _qualname(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualname(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _is_destructive_call(call: ast.Call) -> bool:
    name = _qualname(call.func)
    leaf = name.rsplit(".", 1)[-1]
    if name in {
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "os.replace",
        "os.rename",
        "shutil.rmtree",
    }:
        return True
    if leaf in {"unlink", "rmdir", "_writable_rmtree"}:
        return True
    if leaf == "replace" and isinstance(call.func, ast.Attribute):
        value = call.func.value
        return isinstance(value, ast.Name) and any(
            token in value.id.lower()
            for token in ("path", "link", "temp", "staging", "destination")
        )
    return False


def _is_write_call(call: ast.Call) -> bool:
    name = _qualname(call.func)
    if name.rsplit(".", 1)[-1] in {"write_bytes", "write_text", "symlink_to"}:
        return True
    if name == "open" and len(call.args) > 1:
        mode = call.args[1]
        return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and any(
            flag in mode.value for flag in "wax+"
        )
    return False


def _python_findings(path: Path, text: str) -> list[Finding]:
    lines = text.splitlines()
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [Finding("parse-error", path, exc.lineno or 1, str(exc))]
    findings: list[Finding] = []
    function_authorized_calls: set[int] = set()
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and _annotation(
            lines, function.lineno
        ):
            function_authorized_calls.update(
                id(child) for child in ast.walk(function) if isinstance(child, ast.Call)
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_destructive_call(node):
            if id(node) not in function_authorized_calls and not _annotation(lines, node.lineno):
                findings.append(
                    Finding(
                        "unannotated-destructive",
                        path,
                        node.lineno,
                        _qualname(node.func),
                    )
                )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == "rollback"
            or node.name == "restore"
            or node.name.startswith("_restore")
            or node.name.startswith("rollback_")
        ):
            deletes = [
                child.lineno
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and _is_destructive_call(child)
            ]
            writes = [
                child.lineno
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and _is_write_call(child)
            ]
            for delete_line in deletes:
                if any(delete_line < write_line <= delete_line + 16 for write_line in writes):
                    findings.append(
                        Finding(
                            "unlink-then-write",
                            path,
                            delete_line,
                            "rollback must use atomic replace through safe_fs",
                        )
                    )
                    break
    if (
        "test" in path.name
        and ("patch.dict" in text or "os.environ" in text)
        and re.search(r"[\"']HOME[\"']\s*:", text)
        and "fixture_env" not in text
        and not re.search(r"[\"']ZDOTDIR[\"']\s*:", text)
    ):
        findings.append(
            Finding(
                "inherited-selector",
                path,
                1,
                "private HOME fixture does not scrub/rebind ZDOTDIR through fixture_env",
            )
        )
    return findings


def _shell_findings(path: Path, text: str) -> list[Finding]:
    lines = text.splitlines()
    findings: list[Finding] = []
    for line_number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        match = SHELL_DESTRUCTIVE.search(line)
        if match and not _annotation(lines, line_number):
            findings.append(
                Finding(
                    "unannotated-destructive",
                    path,
                    line_number,
                    match.group(1),
                )
            )
    if HOME_ASSIGNMENT.search(text) and "fixture_env.py" not in text:
        line = text[: HOME_ASSIGNMENT.search(text).start()].count("\n") + 1
        findings.append(
            Finding(
                "inherited-selector",
                path,
                line,
                "private HOME fixture does not use fixture_env.py",
            )
        )
    return findings


def _files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(
                item
                for item in path.rglob("*")
                if item.is_file() and item.suffix in {".py", ".sh"}
            )
        elif path.suffix in {".py", ".sh"}:
            yield path


def scan_paths(paths: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in _files(paths):
        text = path.read_text(encoding="utf-8")
        findings.extend(
            _python_findings(path, text) if path.suffix == ".py" else _shell_findings(path, text)
        )
    return sorted(findings, key=lambda item: (str(item.path), item.line, item.category))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("tools/install")])
    args = parser.parse_args(argv)
    findings = scan_paths(args.paths)
    for finding in findings:
        print(finding.render())
    if findings:
        print(f"destructive-call-guard: FAIL findings={len(findings)}")
        return 1
    print("destructive-call-guard: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
