"""Automate the Migration Order checks behind the PRD ``verify`` command.

Channel drivers define projection, generator-drift, preflight, and bootstrap
checks. This module provides the shared read-only runner and check builders.
"""

import subprocess
from pathlib import Path


def check_symlink(check_id, dest, expected_source):
    """Build a zero-argument check for a symlink's resolved target."""

    def _check():
        dest_path = Path(dest)
        expected = Path(expected_source)
        if not dest_path.is_symlink():
            return {"id": check_id, "ok": False, "detail": f"not a symlink or missing: {dest_path}"}
        try:
            resolved = dest_path.resolve()
        except OSError as exc:
            return {"id": check_id, "ok": False, "detail": f"resolution failed: {exc}"}
        expected_resolved = expected.resolve() if expected.exists() else expected
        if resolved != expected_resolved:
            return {
                "id": check_id, "ok": False,
                "detail": f"symlink target mismatch: {dest_path} -> {resolved} (expected: {expected_resolved})",
            }
        return {"id": check_id, "ok": True, "detail": f"symlink OK: {dest_path} -> {resolved}"}

    return _check


def check_cmd(check_id, argv, must_match=None, cwd=None):
    """Run a read-only subprocess and require exit zero plus an optional stdout match."""
    must_match = must_match or []

    def _check():
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, cwd=cwd, timeout=60,
            )
        except FileNotFoundError as exc:
            return {"id": check_id, "ok": False, "detail": f"execution failed: {exc}"}
        except subprocess.TimeoutExpired:
            return {"id": check_id, "ok": False, "detail": f"timeout: {' '.join(argv)}"}
        if result.returncode != 0:
            return {
                "id": check_id, "ok": False,
                "detail": f"exit={result.returncode} stderr={result.stderr[:300]!r}",
            }
        import re
        for pattern in must_match:
            if not re.search(pattern, result.stdout, re.MULTILINE):
                return {
                    "id": check_id, "ok": False,
                    "detail": f"pattern mismatch: {pattern!r} (stdout excerpt: {result.stdout[:300]!r})",
                }
        return {"id": check_id, "ok": True, "detail": f"OK: {' '.join(argv)}"}

    return _check


def check_file_exists(check_id, path):
    """Build a zero-argument file-existence check."""

    def _check():
        exists = Path(path).exists()
        return {
            "id": check_id, "ok": exists,
            "detail": f"{'present' if exists else 'missing'}: {path}",
        }

    return _check


def check_managed_copy(check_id, dest, integration_id, version):
    """Build a zero-argument check that a managed integration copy carries its pinned version."""

    def _check():
        try:
            import herdr_integration
            ok = bool(herdr_integration.version_marker(Path(dest), integration_id, version))
        except Exception as exc:  # noqa: BLE001 -- a broken copy is a failed check, not a crash
            return {"id": check_id, "ok": False, "detail": f"{dest}: managed copy check failed: {exc}"}
        return {"id": check_id, "ok": ok,
                "detail": f"{dest} carries the current {integration_id} integration" if ok
                else f"{dest} does not carry the current {integration_id} integration"}

    return _check


def check_herdr_integration(runtime, source_root):
    """Verify the pinned Herdr asset markers and real entrypoint registrations."""
    def _check():
        try:
            import herdr_integration
            results = herdr_integration.source_checks(runtime, Path(source_root))
        except Exception as exc:
            return {"id": f"{runtime}.herdr.integration", "ok": False,
                    "detail": f"integration check failed: {exc}"}
        failed = [item for item in results if not item.get("ok")]
        return {"id": f"{runtime}.herdr.integration", "ok": not failed,
                "detail": "; ".join(item["detail"] for item in failed)
                if failed else f"pinned Herdr v{herdr_integration.VERSIONS[runtime]} assets and registrations present"}
    return _check


def run(runtime, driver):
    """Run a driver's checks in order for one runtime.

    Return shape: [{"id": str, "ok": bool, "detail": str}, ...]
    """
    checks = driver.checks()
    if not checks:
        # Placeholder until driver.checks() supplies projection, drift,
        # preflight-contract, and bootstrap-smoke checks.
        return [{"id": f"{runtime}.no-checks", "ok": True, "detail": "check list not implemented (scaffold)"}]
    results = []
    for check in checks:
        results.append(check())
    return results
