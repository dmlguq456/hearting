#!/usr/bin/env python3
"""Build deterministic GitHub Release assets from one committed Git ref."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path


ARCHIVE_NAME = "hearting.tar.gz"
INSTALLER_NAME = "install.sh"
INSTALLER_CHECKSUM_NAME = INSTALLER_NAME + ".sha256"
INSTALLER_MARKER = "__AGENT_HARNESS_DISTRIBUTION_PY_V1__"
DEFAULT_REPOSITORY = "dmlguq456/hearting"
VERSION_RE = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _run_git(root: Path, *args: str, stdout=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _checksum(path: Path, checksum_path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    checksum_path.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    os.chmod(checksum_path, 0o644)


def build_installer(
    version: str,
    output: Path,
    distribution_source: bytes,
    repository: str = DEFAULT_REPOSITORY,
) -> tuple[Path, Path]:
    if not VERSION_RE.fullmatch(version):
        raise SystemExit(f"release version must be SemVer with a v prefix: {version!r}")
    prerelease = version.partition("-")[2]
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease.split(".")):
        raise SystemExit(f"numeric prerelease identifiers cannot have leading zeroes: {version!r}")
    if not REPOSITORY_RE.fullmatch(repository):
        raise SystemExit(f"invalid release repository: {repository!r}")
    try:
        source = distribution_source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("distribution.py must be UTF-8") from exc
    if f"\n{INSTALLER_MARKER}\n" in f"\n{source}\n":
        raise SystemExit("distribution.py collides with the installer heredoc marker")
    if not source.endswith("\n"):
        source += "\n"

    output.mkdir(parents=True, exist_ok=True)
    installer_path = output / INSTALLER_NAME
    checksum_path = output / INSTALLER_CHECKSUM_NAME
    script = f"""#!/bin/sh
# Generated release-bound Hearting installer.
# Bootstrap code and archive are both fixed to {repository}@{version}.
set -eu

RELEASE_VERSION='{version}'
REPOSITORY='{repository}'

for arg in "$@"; do
  case "$arg" in
    --version|--version=*|--repository|--repository=*)
      echo "hearting: this installer is fixed to $REPOSITORY@$RELEASE_VERSION; use the install.sh asset from the requested repository and release tag" >&2
      exit 64
      ;;
  esac
done

PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then
  echo "hearting: Python 3.11+ is required." >&2
  exit 1
fi

TMP=$(mktemp -d "${{TMPDIR:-/tmp}}/hearting-bootstrap.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
MODULE="$TMP/distribution.py"

cat > "$MODULE" <<'{INSTALLER_MARKER}'
{source}{INSTALLER_MARKER}

"$PY" "$MODULE" bootstrap --repository "$REPOSITORY" --version "$RELEASE_VERSION" "$@"
"""
    installer_path.write_text(script, encoding="utf-8")
    os.chmod(installer_path, 0o755)
    _checksum(installer_path, checksum_path)
    return installer_path, checksum_path


def build(
    root: Path,
    version: str,
    output: Path,
    ref: str,
    repository: str = DEFAULT_REPOSITORY,
) -> tuple[Path, Path, Path, Path]:
    if not VERSION_RE.fullmatch(version):
        raise SystemExit(f"release version must be SemVer with a v prefix: {version!r}")
    _run_git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    # Peel to the commit before asking for its timestamp. `git show -s --format=%ct`
    # on an ANNOTATED tag prints the tag body instead of applying the format, so the
    # int() below died on the release message — every annotated tag was unbuildable
    # (measured 2026-09-10 on v2.134.0; v2.133.0 is annotated too).
    epoch = int(
        _run_git(root, "show", "-s", "--format=%ct", f"{ref}^{{commit}}")
        .stdout.decode().strip()
    )
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / ARCHIVE_NAME
    checksum_path = output / (ARCHIVE_NAME + ".sha256")

    with tempfile.TemporaryDirectory(prefix="hearting-release-") as temp:
        tar_path = Path(temp) / "source.tar"
        with tar_path.open("wb") as handle:
            _run_git(
                root,
                "archive",
                "--format=tar",
                "--prefix=hearting/",
                ref,
                "--",
                ".",
                ":(exclude).agent_reports",
                ":(exclude).claude_reports",
                ":(exclude).capability-grounding",
                ":(exclude).route-grounding",
                stdout=handle,
            )
        marker = tarfile.TarInfo("hearting/RELEASE_VERSION")
        marker.mode = 0o644
        marker.uid = marker.gid = 0
        marker.uname = marker.gname = "root"
        marker.mtime = epoch
        payload = (version + "\n").encode("utf-8")
        marker.size = len(payload)
        with tarfile.open(tar_path, "a") as bundle:
            bundle.addfile(marker, io.BytesIO(payload))

        with tar_path.open("rb") as source, archive_path.open("wb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9
            ) as compressed:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    compressed.write(chunk)

    os.chmod(archive_path, 0o644)
    _checksum(archive_path, checksum_path)

    distribution_source = _run_git(
        root, "show", f"{ref}:tools/install/distribution.py"
    ).stdout
    installer_path, installer_checksum_path = build_installer(
        version, output, distribution_source, repository
    )
    return archive_path, checksum_path, installer_path, installer_checksum_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", default="dist")
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPOSITORY),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    assets = build(
        root, args.version, Path(args.output).resolve(), args.ref, args.repository
    )
    for asset in assets:
        print(asset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
