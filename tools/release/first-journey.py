#!/usr/bin/env python3
"""Run an isolated, report-only first-use journey for Hearting releases."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import hashlib
import urllib.request
import urllib.error
import signal
import sqlite3
import re
import importlib.util
import math
import selectors
from pathlib import Path

VERSION = "1"
PROFILES = {"claude-only": ["claude"], "all": ["claude", "codex", "opencode"]}
TASK_HARNESSES = {"claude-only": ["claude"], "all": ["codex", "opencode"]}
AUTH_NAMES = {"claude": [".credentials.json"], "codex": ["auth.json"], "opencode": ["auth.json"]}
OPENCODE_V2_AUTH_NAME = "credentials-v2.json"
AUTH_CAPTURE_LIMIT = 64 * 1024
AUTH_CAPTURE_TIMEOUT = 10
UTILITIES = Path(__file__).resolve().parents[2] / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))
from opencode_runtime import is_v2 as opencode_is_v2, parse_version as parse_opencode_version
DEFAULT_PROMPT = "Fix the typo in greet's output, run the existing focused test, and show the diff."
_ACTIVE_AUTH: dict | None = None


def credential_complete(harness: str, data: bytes, generation: str | None = None) -> bool:
    """Check only the credential structure needed to identify a complete login."""
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if harness == "opencode" and generation == "v2":
        if not isinstance(value, list) or not value:
            return False
        ids: set[str] = set()
        transferable = False
        for entry in value:
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get("id"), str) or not entry["id"]
                    or entry["id"] in ids
                    or not isinstance(entry.get("integrationID"), str) or not entry["integrationID"]
                    or not isinstance(entry.get("label"), str)
                    or type(entry.get("active")) is not bool
                    or not isinstance(entry.get("value"), dict)):
                return False
            ids.add(entry["id"])
            credential = entry["value"]
            kind = credential.get("type")
            if kind == "key":
                if not isinstance(credential.get("key"), str) or not credential["key"]:
                    return False
                transferable = True
            elif kind == "oauth":
                expiry = credential.get("expires")
                if (not isinstance(credential.get("methodID"), str) or not credential["methodID"]
                        or not isinstance(credential.get("access"), str) or not credential["access"]
                        or not isinstance(credential.get("refresh"), str) or not credential["refresh"]
                        or type(expiry) not in (int, float)
                        or (isinstance(expiry, float) and not math.isfinite(expiry)) or expiry < 0
                        or (isinstance(expiry, float) and not expiry.is_integer())):
                    return False
                transferable = True
            elif kind == "external":
                if not isinstance(credential.get("methodID"), str) or not credential["methodID"]:
                    return False
            else:
                return False
        return transferable
    if not isinstance(value, dict):
        return False
    if harness == "claude":
        oauth = value.get("claudeAiOauth")
        return (isinstance(oauth, dict)
                and isinstance(oauth.get("accessToken"), str) and bool(oauth["accessToken"])
                and isinstance(oauth.get("refreshToken"), str) and bool(oauth["refreshToken"]))
    if harness == "codex":
        if isinstance(value.get("OPENAI_API_KEY"), str) and value["OPENAI_API_KEY"]:
            return True
        tokens = value.get("tokens")
        return (isinstance(tokens, dict)
                and isinstance(tokens.get("access_token"), str) and bool(tokens["access_token"])
                and isinstance(tokens.get("refresh_token"), str) and bool(tokens["refresh_token"]))
    if harness == "opencode":
        # Match the native auth variants before replacing an intact login.
        required = {"api": ("key",), "oauth": ("access", "refresh"),
                    "wellknown": ("key", "token")}
        for item in value.values():
            if not isinstance(item, dict):
                return False
            kind = item.get("type")
            if not isinstance(kind, str) or kind not in required:
                return False
            if any(not isinstance(item.get(key), str) or not item[key]
                   for key in required[kind]):
                return False
            if kind == "oauth":
                expires = item.get("expires")
                if (type(expires) not in (int, float) or expires < 0
                        or (isinstance(expires, float) and not expires.is_integer())):
                    return False
                if any(key in item and not isinstance(item[key], str)
                       for key in ("accountId", "enterpriseUrl")):
                    return False
            if kind == "api" and "metadata" in item:
                metadata = item["metadata"]
                if (not isinstance(metadata, dict)
                        or any(not isinstance(field, str) for field in metadata.values())):
                    return False
        return bool(value)
    return False


def atomic_auth_write(store: Path, data: bytes) -> None:
    """Replace a credential store through a fresh mode-0600 sibling file."""
    store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if store.parent.is_symlink():
        raise OSError("credential directory is a symlink")
    os.chmod(store.parent, 0o700)
    if store.is_symlink():
        raise OSError("credential store is a symlink")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{store.name}.", suffix=".tmp", dir=store.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, store)
        os.chmod(store, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def private_auth_command(argv: list[str], *, env: dict[str, str], timeout: int = AUTH_CAPTURE_TIMEOUT,
                         stdout_limit: int = AUTH_CAPTURE_LIMIT, stderr_limit: int = AUTH_CAPTURE_LIMIT) -> dict:
    """Capture secret-bearing auth output with concurrent fixed-size limits."""
    started = time.monotonic()
    process = None
    streams: dict = {}
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    reason = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, start_new_session=True)
        limits = {process.stdout: ("stdout", stdout_limit), process.stderr: ("stderr", stderr_limit)}
        with selectors.DefaultSelector() as selector:
            for stream, (name, _limit) in limits.items():
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            deadline = started + timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    if len(captured[name]) + len(chunk) > limits[key.fileobj][1]:
                        reason = "oversized"
                        break
                    captured[name].extend(chunk)
                if reason:
                    break
        if reason:
            _stop_private_process_group(process)
        else:
            code = process.wait(timeout=max(0.0, started + timeout - time.monotonic()))
            if code != 0:
                reason = "failed"
        return {"ok": reason is None, "reason": reason, "exit": process.returncode,
                "seconds": round(time.monotonic() - started, 3),
                "stdout": bytes(captured["stdout"]) if reason is None else b""}
    except subprocess.TimeoutExpired:
        if process:
            _stop_private_process_group(process)
        return {"ok": False, "reason": "timeout", "exit": 124,
                "seconds": round(time.monotonic() - started, 3), "stdout": b""}
    except OSError:
        if process:
            _stop_private_process_group(process)
        return {"ok": False, "reason": "failed", "exit": 127,
                "seconds": round(time.monotonic() - started, 3), "stdout": b""}
    except BaseException:
        if process:
            _stop_private_process_group(process)
        raise
    finally:
        if process:
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()


def _auth_read_latest(ledger: dict) -> bytes | None:
    if ledger["generation"] == "v2":
        result = private_auth_command(ledger["export_argv"], env=ledger["env"])
        if not result["ok"]:
            ledger["last_error"] = f"auth-export-{result['reason']}"
            return None
        return result["stdout"]
    target = ledger["target"]
    if target.is_file() and not target.is_symlink():
        return target.read_bytes()
    return None


def persist_active_auth() -> bool:
    ledger = _ACTIVE_AUTH
    if not ledger:
        return True
    try:
        latest = _auth_read_latest(ledger)
        if latest is None:
            return False
        if not credential_complete(ledger["harness"], latest, ledger["generation"]):
            ledger["last_error"] = "auth-export-incomplete"
            return False
        if latest != ledger["original"]:
            atomic_auth_write(ledger["store"], latest)
        ledger["persisted"] = True
        return True
    except (OSError, ValueError):
        ledger["last_error"] = "auth-persist-failed"
        return False


def auth_signal(signum, _frame):
    raise SystemExit(128 + signum)


def _stop_process_group(process: subprocess.Popen) -> None:
    """Stop and reap a started process and every child in its session."""
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    try: process.wait(timeout=0.25)
    except subprocess.TimeoutExpired: pass
    try: os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    process.wait()
    process.communicate()


def _stop_private_process_group(process: subprocess.Popen) -> None:
    """Reap a secret-output child without draining unbounded pipes into memory."""
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    try: process.wait(timeout=0.25)
    except subprocess.TimeoutExpired: pass
    try: os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    process.wait()
    for stream in (process.stdout, process.stderr):
        if stream:
            stream.close()


def safe_root(value: str, label: str, forbidden: list[Path]) -> Path:
    path = Path(value).expanduser().absolute()
    resolved = path.resolve()
    if path.is_symlink() or any(resolved == p or p in resolved.parents or resolved in p.parents for p in forbidden):
        raise ValueError(f"unsafe {label} path")
    if not resolved.exists():
        resolved.mkdir(parents=True, mode=0o700)
    if not resolved.is_dir():
        raise ValueError(f"{label} must be a directory")
    metadata = resolved.stat()
    if metadata.st_uid != os.getuid() and not (metadata.st_mode & 0o1000 and metadata.st_mode & 0o002):
        raise ValueError(f"{label} must be owned or a sticky shared temp directory")
    if platform.system() == "Linux":
        info = Path("/proc/self/mountinfo").read_text(errors="replace")
        # Reject paths under the project/NAS and host HOME irrespective of mount type.
        if any(str(resolved).startswith(prefix) for prefix in ("/home/nas/", str(Path.home()) + "/")):
            raise ValueError(f"{label} is on a protected path")
        records = []
        for line in info.splitlines():
            fields = line.split()
            mount = fields[4].replace("\\040", " ")
            if str(resolved) == mount or str(resolved).startswith(mount.rstrip("/") + "/"):
                records.append((len(mount), fields[fields.index("-") + 1]))
        if not records:
            raise ValueError(f"cannot identify {label} filesystem")
        fs_type = max(records)[1]
        if fs_type in {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs"}:
            raise ValueError(f"{label} is on a remote filesystem")
        if fs_type not in {"ext2", "ext3", "ext4", "xfs", "btrfs", "overlay", "tmpfs", "ramfs", "zfs"}:
            raise ValueError(f"cannot verify local {label} filesystem: {fs_type}")
    return resolved


def run(argv: list[str], *, env: dict[str, str], timeout: int, cwd: Path | None = None) -> dict:
    started = time.monotonic()
    process = None
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", start_new_session=True)
        output, _ = process.communicate(timeout=timeout)
        return {"exit": process.returncode, "seconds": round(time.monotonic() - started, 3), "output": output[-12000:]}
    except subprocess.TimeoutExpired:
        if process:
            _stop_process_group(process)
        return {"exit": 124, "seconds": round(time.monotonic() - started, 3), "output": "timeout"}
    except OSError as e:
        return {"exit": 127, "seconds": round(time.monotonic() - started, 3), "output": type(e).__name__}
    except BaseException:
        if process:
            _stop_process_group(process)
        raise


def clean_env(home: Path, paths: list[Path], extras: dict[str, str] | None = None) -> dict[str, str]:
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_DATA_HOME": str(home / ".local/share"), "XDG_STATE_HOME": str(home / ".local/state"),
           "XDG_CACHE_HOME": str(home / ".cache"), "TMPDIR": str(home / "tmp"),
           "PATH": os.pathsep.join(map(str, paths)), "LANG": "C.UTF-8", "TERM": "dumb",
           "USER": str(os.getuid()), "DISABLE_AUTOUPDATER": "1"}
    env.update(extras or {})
    return env


def sandbox(argv: list[str], world: Path, env: dict[str, str], cwd: Path | None = None,
            cli: list[tuple[Path, str]] | None = None, probes: bool = False,
            system_files: dict[str, Path] | None = None) -> list[str]:
    if platform.system() != "Linux":
        return argv
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("isolation-unavailable: bwrap not found")
    args = [bwrap, "--clearenv", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--new-session", "--die-with-parent", "--proc", "/proc", "--dev", "/dev",
            "--tmpfs", "/tmp", "--dir", "/run"]
    for directory in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/ssl", "/etc/ca-certificates"):
        if Path(directory).exists(): args += ["--ro-bind", directory, directory]
    host_files = system_files if system_files is not None else {
        name: Path("/etc") / name for name in ("resolv.conf", "hosts", "nsswitch.conf", "passwd", "group")}
    for name, path in host_files.items():
        if path.exists():
            # Bind the resolved source to /etc/name so a resolver target under
            # /run works even though /run is empty in the namespace.
            args += ["--ro-bind", str(path.resolve()), f"/etc/{name}"]
    real_home = str(Path.home())
    # The world keeps its host path inside the namespace, so paths the product
    # records (symlinks, worktrees, configs) also resolve for host-side checks.
    args += ["--dir", "/home", "--tmpfs", "/home", "--dir", real_home, "--dir", "/home/nas",
             "--tmpfs", "/home/nas", "--bind", str(world), str(world)]
    if cli: args += ["--dir", "/opt", "--dir", "/opt/journey-cli"]
    made_dirs = {"/opt", "/opt/journey-cli"}
    for source, destination in cli or []:
        parent = "/opt/journey-cli"
        for part in Path(destination).parent.relative_to("/opt/journey-cli").parts:
            parent = str(Path(parent) / part)
            if parent not in made_dirs:
                args += ["--dir", parent]; made_dirs.add(parent)
        args += ["--ro-bind", str(source), destination]
    for key, value in env.items():
        args += ["--setenv", key, value]
    args += ["--chdir", str(cwd if cwd else world / "home")]
    if probes:
        args += ["--setenv", "JOURNEY_PROBE_HOME", real_home,
                 "--setenv", "JOURNEY_PROBE_NAS", "/home/nas",
                 "--setenv", "JOURNEY_PROBE_NAME", world.name]
        probe = "import os,pathlib,json,shutil; h=pathlib.Path(os.environ['JOURNEY_PROBE_HOME']); n=pathlib.Path(os.environ['JOURNEY_PROBE_NAS']); r={'real_home_empty':not list(h.iterdir()),'nas_empty':not list(n.iterdir())}; h.joinpath(os.environ['JOURNEY_PROBE_NAME']).write_text('x'); n.joinpath(os.environ['JOURNEY_PROBE_NAME']).write_text('x'); r['herdr_absent_from_path']=shutil.which('herdr') is None; print(json.dumps(r))"
        return args + ["--", "python3", "-c", probe]
    return args + ["--"] + argv


def auth_file(root: Path, harness: str, generation: str | None = None) -> Path:
    name = OPENCODE_V2_AUTH_NAME if harness == "opencode" and generation == "v2" else AUTH_NAMES[harness][0]
    return root / harness / name


def opencode_generation(output: str, exit_code: int) -> str | None:
    """Classify only a successfully parsed sandbox version; never probe the host."""
    if exit_code != 0:
        return None
    version = parse_opencode_version(output)
    if version is None:
        return None
    return "v2" if opencode_is_v2(version=version) else "v1"


def _credential_identity(data: bytes) -> list[dict] | None:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, list):
        return None
    return [{key: entry.get(key) for key in ("id", "integrationID", "active", "value")}
            for entry in value if isinstance(entry, dict)]


def import_v2_credentials(store: Path, world: Path, executable: str, env: dict[str, str],
                          mounts: list[tuple[Path, str]]) -> tuple[bool, str | None]:
    """Import into one fresh world, unlink the transfer file, and verify by export."""
    try:
        original = store.read_bytes()
    except OSError:
        return False, "auth-import-missing"
    if not credential_complete("opencode", original, "v2"):
        return False, "auth-import-incomplete"
    temp_dir = world / "tmp"
    temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="journey-auth-", suffix=".json", dir=temp_dir)
    path = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
        imported = private_auth_command(sandbox([executable, "auth", "import", "--standalone", str(path)],
                                                world, env, cli=mounts), env=env)
    except OSError:
        return False, "auth-import-failed"
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    if not imported["ok"]:
        return False, f"auth-import-{imported['reason']}"
    exported = private_auth_command(sandbox([executable, "auth", "export", "--standalone"],
                                            world, env, cli=mounts), env=env)
    if not exported["ok"] or not credential_complete("opencode", exported["stdout"], "v2"):
        return False, "auth-import-verification-failed"
    if _credential_identity(exported["stdout"]) != _credential_identity(original):
        return False, "auth-import-verification-mismatch"
    return True, None


def published_installer(world: Path, tag: str, timeout: int) -> tuple[Path, str, str]:
    base = "https://github.com/dmlguq456/hearting/releases/download/"
    request = urllib.request.Request("https://github.com/dmlguq456/hearting/releases/latest", method="HEAD")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        resolved = response.geturl().rstrip("/").split("/")[-1]
    if tag != "latest":
        resolved = tag
    paths = []
    for suffix in ("install.sh", "install.sh.sha256"):
        with urllib.request.urlopen(base + resolved + "/" + suffix, timeout=timeout) as response:
            paths.append(response.read())
    installer, checksum = paths
    expected = checksum.decode("ascii", "strict").split()[0]
    actual = hashlib.sha256(installer).hexdigest()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected) or actual.lower() != expected.lower():
        raise ValueError("published installer checksum mismatch")
    target = world / "release-assets" / "install.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(installer)
    target.chmod(0o700)
    return target, resolved, actual


# §0.4 confirmation card: five labelled fields in order, then the option line.
CARD_LABELS = ((("작업",), ("이유",), ("경로",), ("범위",), ("완료",)),
               (("task", "work"), ("why", "reason"), ("route", "path"), ("scope",), ("done", "completion", "complete")))
CARD_OPTIONS = (("진행", "중단"), ("proceed", "stop"))
MAX_CANNED = 3


def card_answer(text: str) -> str | None:
    """Return the card's proceed word when the reply is a confirmation card, else None."""
    lines = [re.sub(r"^[\s>`•\-]+", "", line).replace("*", "").strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    for labels, (proceed, stop) in zip(CARD_LABELS, CARD_OPTIONS):
        cursor = 0
        for names in labels:
            found = next((i for i in range(cursor, len(lines))
                          if any(lines[i].lower().startswith(name) and lines[i][len(name):len(name) + 1] in (":", "：", " ")
                                 for name in names)), None)
            if found is None:
                break
            cursor = found + 1
        else:
            for line in lines[cursor:]:
                lower = line.lower()
                if "/" in line and proceed in lower and stop in lower:
                    return "진행" if proceed == "진행" else "Proceed"
    return None


def _number(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def telemetry(world: Path, harness: str) -> dict:
    """Sum token usage of every session in the disposable world; unknown stays None, never 0."""
    home = world / "home"
    if harness == "claude":
        seen: dict[str, dict] = {}; sessions = set()
        for project in (p for p in home.rglob("projects") if p.is_dir() and not p.is_symlink()):
            for file in project.rglob("*.jsonl"):
                for line in file.read_text(errors="replace").splitlines():
                    try: row = json.loads(line)
                    except json.JSONDecodeError: continue
                    msg = row.get("message") if isinstance(row, dict) else None
                    if row.get("type") != "assistant" or not isinstance(msg, dict): continue
                    if msg.get("id") and isinstance(msg.get("usage"), dict):
                        seen[msg["id"]] = msg["usage"]; sessions.add(row.get("sessionId") or file.stem)
        if not seen:
            return {"status": "unknown", "total": None, "sessions": 0, "reason": "no-claude-usage-records"}
        fields = {"input": "input_tokens", "output": "output_tokens",
                  "cache_creation": "cache_creation_input_tokens", "cache_read": "cache_read_input_tokens"}
        totals = {key: sum(_number(u.get(field)) or 0 for u in seen.values()) for key, field in fields.items()}
        return {"status": "measured", **totals, "total": sum(totals.values()),
                "total_formula": "input+output+cache_creation+cache_read",
                "sessions": len(sessions), "messages": len(seen), "reason": None}
    if harness == "opencode":
        databases = [db for db in home.rglob("opencode.db") if db.is_file()]
        if not databases:
            return {"status": "unknown", "total": None, "sessions": 0, "reason": "opencode-db-missing"}
        totals = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}; sessions = 0
        sources = []
        try:
            for db in databases:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
                    table = "session_v2" if "session_v2" in tables else "session"
                    sources.append((db, table))
                finally:
                    con.close()
            generations = {"v2" if table == "session_v2" else "v1" for _, table in sources}
            if len(generations) > 1:
                return {"status": "unknown", "total": None, "sessions": 0,
                        "reason": "mixed-opencode-generations"}
            for db in databases:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
                    table = "session_v2" if "session_v2" in tables else "session"
                    columns = {row[1] for row in con.execute(f"pragma table_info({table})")}
                    required = {"tokens_input", "tokens_output", "tokens_reasoning"}
                    if table == "session_v2":
                        required |= {"tokens_cache_read", "tokens_cache_write"}
                    if not required <= columns:
                        raise ValueError("schema")
                    selected = ("tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write") if table == "session_v2" else ("tokens_input", "tokens_output", "tokens_reasoning")
                    for row in con.execute(f"select {', '.join(selected)} from {table}"):
                        sessions += 1
                        for key, value in zip(selected, row):
                            totals[key.removeprefix("tokens_")] += _number(value) or 0
                finally:
                    con.close()
        except (sqlite3.Error, ValueError):
            return {"status": "unknown", "total": None, "sessions": 0, "reason": "unsupported-opencode-schema"}
        return {"status": "measured" if sessions else "unknown", **totals,
                "total": sum(totals.values()) if sessions else None,
                "total_formula": "input+output+reasoning+cache_read+cache_write (session_v2 table)" if generations == {"v2"} else "input+output+reasoning (session table)", "sessions": sessions,
                "reason": None if sessions else "no-opencode-sessions"}
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fleet"))
    from token_budget import parse_codex_token_count
    per_session = []; rate_limits = None
    for file in sorted(home.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime):
        last = None
        for line in file.read_text(errors="replace").splitlines():
            if '"token_count"' not in line: continue
            try: row = json.loads(line)
            except json.JSONDecodeError: continue
            payload = row.get("payload") if isinstance(row, dict) else None
            if row.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
                last = line; rate_limits = payload.get("rate_limits") or rate_limits
        if last:  # cumulative per session: take the last event, never sum events
            per_session.append(parse_codex_token_count(last))
    known = [t for t in per_session if t.session_total_tokens is not None]
    if not known:
        return {"status": "unknown", "total": None, "sessions": 0, "reason": "no-supported-codex-token-count"}
    def total(attr):
        values = [getattr(t, attr) for t in known]
        return None if any(v is None for v in values) else sum(values)
    return {"status": "measured" if len(known) == len(per_session) else "partial",
            "input": total("session_input_tokens"), "cached_input": total("session_cached_input_tokens"),
            "output": total("session_output_tokens"), "reasoning_output": total("session_reasoning_output_tokens"),
            "total": total("session_total_tokens"),
            "total_formula": "codex total_token_usage.total_tokens (cached/reasoning are subsets)",
            "sessions": len(per_session), "rate_limits": rate_limits, "reason": None}


def claude_account_snapshot(config: Path) -> dict | None:
    spec = importlib.util.spec_from_file_location("journey_usage_api", Path(__file__).resolve().parents[1] / "fleet/collectors/usage_api.py")
    if not spec or not spec.loader: return None
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    previous = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        os.environ["CLAUDE_CONFIG_DIR"] = str(config)
        data = module._fetch()
    finally:
        if previous is None: os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else: os.environ["CLAUDE_CONFIG_DIR"] = previous
    if not data: return None
    return {key: data.get(key) for key in ("rl_5h", "rl_7d", "rs_5h", "rs_7d")}


def event_text(output: str, harness: str) -> tuple[str, str | None, int]:
    """Final assistant text, session id, and permission denials from one turn's JSON events."""
    text = ""; sid = None; denials = 0
    for line in output.splitlines():
        try: row = json.loads(line)
        except json.JSONDecodeError: continue
        if not isinstance(row, dict): continue
        sid = sid or row.get("session_id") or row.get("thread_id") or row.get("sessionID")
        if harness == "claude":
            msg = row.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content")
                parts = [str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("type", "text") == "text"] if isinstance(content, list) else [str(content or "")]
                text = "\n".join(p for p in parts if p) or text
            if row.get("type") == "result":
                text = str(row.get("result") or text)
                denials = len(row.get("permission_denials") or [])
        elif harness == "codex":
            if row.get("type") == "thread.started": sid = row.get("thread_id") or sid
            item = row.get("item")
            if row.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
                text = str(item.get("text") or text)
        else:
            part = row.get("part")
            if row.get("type") == "text":
                text = str((part or {}).get("text") if isinstance(part, dict) else row.get("text") or text)
    return text, sid, denials


JUDGE_SCRIPT = r'''
import hashlib, json, subprocess, sys, tempfile
from pathlib import Path
expected, repo = sys.argv[1], Path.cwd().resolve()
git = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True).stdout
def check(label, greet, test):
    row = {"candidate": label, "greet_ok": False, "passed": False}
    if hashlib.sha256(test).hexdigest() != expected:
        return {**row, "reason": "baseline-test-edited"}
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "greet.py").write_bytes(greet); Path(tmp, "test_greet.py").write_bytes(test)
        try:
            out = subprocess.run([sys.executable, "greet.py"], cwd=tmp, capture_output=True, text=True, timeout=10)
            unit = subprocess.run([sys.executable, "-m", "unittest", "-q", "test_greet"], cwd=tmp, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            return {**row, "reason": "timeout"}
    row["greet_ok"] = out.returncode == 0 and out.stdout.strip() == "Hello"
    row["passed"] = row["greet_ok"] and unit.returncode == 0
    return {**row, "reason": None if row["passed"] else "greet-or-focused-test-failed"}
rows = []
trees = [Path(l[9:]) for l in git("worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")] or [repo]
for tree in trees:
    label = "worktree:" + ("main" if tree.resolve() == repo else tree.name)
    try: rows.append(check(label, (tree / "greet.py").read_bytes(), (tree / "test_greet.py").read_bytes()))
    except OSError: rows.append({"candidate": label, "greet_ok": False, "passed": False, "reason": "files-missing"})
for commit in git("log", "--all", "--format=%H").splitlines():
    show = lambda name: subprocess.run(["git", "show", commit + ":" + name], cwd=repo, capture_output=True).stdout
    greet, test = show("greet.py"), show("test_greet.py")
    if greet and test: rows.append(check("commit:" + commit[:12], greet, test))
print(json.dumps({"candidates": rows, "worktrees": len(trees),
                  "local_refs": git("for-each-ref", "--format=%(refname)", "refs/heads").splitlines()}))
'''


def judge_repo(repo: Path, baseline_test: bytes, wrap=None, env: dict[str, str] | None = None) -> dict:
    """Judge every worktree and commit of the demo repo; `wrap` runs it inside the sandbox."""
    argv = ["python3", "-c", JUDGE_SCRIPT, hashlib.sha256(baseline_test).hexdigest()]
    result = run(wrap(argv) if wrap else argv, env=env if env is not None else dict(os.environ), timeout=180, cwd=repo)
    try:
        data = json.loads(result["output"].strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"code_fixed": False, "focused_test_passed": False, "candidate": None, "candidates": [],
                "local_refs": [], "worktrees": 0, "reason": "judge-failed", "seconds": result["seconds"]}
    passed = next((row["candidate"] for row in data["candidates"] if row["passed"]), None)
    return {"code_fixed": any(row["greet_ok"] for row in data["candidates"]),
            "focused_test_passed": passed is not None, "candidate": passed, "reason": None,
            "seconds": result["seconds"], **data}


def route_evidence(world: Path) -> int:
    """Route files the installed product wrote anywhere in the world (`.runtime/routes/*.json`)."""
    return sum(1 for routes in world.rglob("routes") if routes.is_dir() and routes.parent.name == ".runtime"
               for _ in routes.glob("*.json"))


def turn_argv(harness: str, command: str, cwd: Path, sid: str | None, message: str,
              opencode_generation: str | None = None) -> list[str]:
    if harness == "claude":
        return [command, "-p", "--output-format", "stream-json", "--verbose", *(["--resume", sid] if sid else []), message]
    if harness == "codex":
        return [command, "exec", "resume", "--json", sid, message] if sid else [command, "exec", "--json", "--cd", str(cwd), message]
    if opencode_generation not in {"v1", "v2"}:
        raise ValueError("opencode-version-unknown")
    return [command, "run", *( ["--standalone"] if opencode_generation == "v2" else []), "--format", "json",
            *(["--session", sid] if sid else []), message]


TRANSPORT_HELP = {
    "claude": (["--help"], ("--resume", "--output-format")),
    "codex": (["exec", "resume", "--help"], ("--json",)),
    "opencode": (["run", "--help"], ("--session", "--format")),
}


def transport_result(harness: str, exit_code: int, output: str,
                     opencode_generation: str | None = None) -> dict:
    if harness == "opencode":
        if opencode_generation not in {"v1", "v2"}:
            return {"supported": False, "exit": exit_code, "required_flags": [],
                    "missing_flags": [], "reason": "opencode-version-unknown"}
        required = (*TRANSPORT_HELP[harness][1], *( ("--standalone",) if opencode_generation == "v2" else () ))
    else:
        required = TRANSPORT_HELP[harness][1]
    missing = [flag for flag in required if flag not in output]
    return {"supported": exit_code == 0 and not missing,
            "exit": exit_code, "required_flags": list(required), "missing_flags": missing}


def probe_transport(harness: str, executable: str, world: Path, env: dict[str, str],
                    mounts: list[tuple[Path, str]], timeout: int,
                    opencode_generation: str | None = None) -> dict:
    if harness == "opencode" and opencode_generation not in {"v1", "v2"}:
        return transport_result(harness, 1, "", opencode_generation)
    args, _required = TRANSPORT_HELP[harness]
    if harness == "opencode" and opencode_generation == "v2":
        args = ["run", "--help", "--standalone"]
    result = run(sandbox([executable, *args], world, env, cli=mounts), env=env, timeout=timeout)
    return transport_result(harness, result["exit"], result["output"], opencode_generation)


def headless_turn(harness: str, command: str, prompt: str, cwd: Path, world: Path,
                  env: dict[str, str], timeout: int, mounts: list[tuple[Path, str]], judge=None,
                  transport_supported: bool = True, opencode_generation: str | None = None) -> dict:
    """Drive one task session: answer only the confirmation card, stop on anything else."""
    if not transport_supported:
        return {"status": "unsupported-headless-transport", "turns": [], "canned_answers": 0,
                "session_id": None, "turn_index": 0, "output": ""}
    if harness == "opencode" and opencode_generation not in {"v1", "v2"}:
        return {"status": "opencode-version-unknown", "turns": [], "canned_answers": 0,
                "session_id": None, "turn_index": 0, "output": ""}
    deadline = time.monotonic() + timeout
    turns: list[dict] = []; texts: list[str] = []; answers = 0; sid = None; message = prompt
    status = "task-incomplete"
    while True:
        remaining = int(deadline - time.monotonic())
        if remaining < 5:
            status = "task-timeout"; break
        step = run(sandbox(turn_argv(harness, command, cwd, sid, message, opencode_generation), world, env, cwd, mounts),
                   env=env, timeout=remaining, cwd=cwd)
        text, found, denials = event_text(step["output"], harness)
        sid = sid or found
        texts.append(text)
        turns.append({"index": len(turns) + 1, "exit": step["exit"], "seconds": step["seconds"],
                      "session_id": sid, "permission_denials": denials})
        if step["exit"] == 124: status = "task-timeout"; break
        if step["exit"] != 0: status = "task-failed"; break
        if judge and judge():
            status = "candidate-complete"; break
        reply = card_answer(text)
        if reply and sid and answers < MAX_CANNED:
            answers += 1; message = reply; continue
        if reply:
            status = "confirmation-cap" if answers >= MAX_CANNED else "card-without-session"
        elif denials:
            status = "tool-permission-denied"
        else:
            status = "unexpected-question" if text.rstrip().endswith(("?", "？")) else "task-incomplete"
        break
    return {"status": status, "turns": turns, "canned_answers": answers, "session_id": sid,
            "turn_index": len(turns), "output": "\n".join(texts)}


def journey_profile(profile: str, args: argparse.Namespace, repo: Path) -> dict:
    global _ACTIVE_AUTH
    began = time.monotonic()
    record: dict = {"schema": "hearting-first-journey-v1", "schema_version": 1, "profile": profile,
                    "mode": "model-free" if args.model_free else "real",
                    "source": {"kind": args.source, "tag": None, "ref": args.ref if args.source == "checkout" else None, "sha256": None},
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "isolation": {"level": "linux-bwrap" if platform.system() == "Linux" else "env-home-only",
                                  "protected_paths_enforced": platform.system() == "Linux", "d3": "not-proven", "checks": [],
                                  "host_io_exceptions": ["source-staging", "summary-export"]},
                    "steps": [], "failures": [], "harnesses": [],
                    "account_usage": {"before": None, "after": None, "delta_pp": None, "attribution": "uncertain", "note": "may include other work", "reason": "measurement-credential-unavailable"},
                    "headless_uncovered": ["login", "first trust dialog", "theme", "native question tool"]}
    tempbase = Path(args.local_root).resolve()
    world = Path(tempfile.mkdtemp(prefix=f"first-journey-{profile}-", dir=tempbase))
    os.chmod(world, 0o700)
    record["isolation"]["world"] = str(world)
    try:
        for part in ("home", "tmp", "bin", "logs", "repos"): (world / part).mkdir(mode=0o700)
        commands = ["python3", "bash", "sh", "git", "curl", "tar", "gzip", "mktemp",
                    "mkdir", "chmod", "cp", "mv", "rm", "cat", "uname", "dirname", "readlink"]
        sysbin = world / "sysbin"
        sysbin.mkdir()
        for name in commands:
            found = shutil.which(name)
            if found:
                resolved = Path(found).resolve()
                if str(resolved).startswith(str(Path.home()) + "/"):
                    continue
                os.symlink(resolved, sysbin / name)
        cli_map = {name: shutil.which(name) for name in PROFILES[profile]}
        home = world / "home"
        for relative in ("tmp", ".config", ".local/share", ".local/state", ".cache", ".claude", ".codex"):
            (home / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
        binaries: dict[str, tuple[Path, str]] = {}
        cli_mounts: list[tuple[Path, str]] = []
        cli_bin = world / "bin"
        for index, harness in enumerate(PROFILES[profile]):
            path = cli_map[harness]
            if path:
                link = cli_bin / harness
                # Keep the source path out of PATH; runtime binding is a later, explicit step.
                resolved = Path(path).resolve()
                if harness == "codex":
                    release = resolved.parent.parent
                    target = f"/opt/journey-cli/codex"
                    cli_mounts.append((release, target))
                    executable = target + "/bin/codex"
                else:
                    target = f"/opt/journey-cli/{harness}"
                    cli_mounts.append((resolved, target))
                    executable = target
                os.symlink(executable, link)
                binaries[harness] = (resolved, executable)
        install_bin = home / ".local/bin"
        install_bin.mkdir(parents=True)
        # Installed launchers first, like a user's ~/.local/bin; then the profile CLIs.
        pathlist = [install_bin, cli_bin, sysbin]
        env = clean_env(home, pathlist, {"CLAUDE_CONFIG_DIR": str(home / ".claude"),
                                        "CODEX_HOME": str(home / ".codex")})
        for key in ("home", "tmp", "repos", "logs"):
            (world / key).mkdir(exist_ok=True)
        t0 = time.monotonic()
        if args.source == "checkout":
            builddir = world / "release-assets"
            p = subprocess.run([sys.executable, str(repo / "tools/install/build-release.py"),
                                "--version", "v0.0.0-journey.1", "--output", str(builddir), "--ref", args.ref],
                               cwd=repo, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
                               capture_output=True, text=True, timeout=args.step_timeout)
            step = {"name": "build-release", "seconds": round(time.monotonic()-t0, 3), "exit": p.returncode}
            if p.returncode == 0:
                index = {"tag_name": "v0.0.0-journey.1", "assets": []}
                for name in ("hearting.tar.gz", "hearting.tar.gz.sha256"):
                    index["assets"].append({"name": name, "browser_download_url": f"file://{builddir}/{name}"})
                (builddir / "release-index.json").write_text(json.dumps(index), encoding="utf-8")
                shutil.copy2(builddir / "install.sh", builddir / "checkout-install.sh")
        else:
            step = {"name": "release-source", "seconds": 0, "exit": 0, "detail": "published installer resolves release assets at install time"}
        record["steps"].append(step)
        # Install through the regular distribution installer; model-free checks never use auth.
        runtime_args = [part for harness in PROFILES[profile] for part in ("--runtime", harness)]
        assets = world / "release-assets"
        install_args = ["sh", str(assets / ("checkout-install.sh" if args.source == "checkout" else "install.sh")), *runtime_args, "--no-auto-update"]
        install_extras = {}
        if args.source == "checkout":
            install_extras = {"HARNESS_ALLOW_FILE_RELEASES": "1",
                              "HARNESS_RELEASE_INDEX_URL": f"file://{assets}/release-index.json"}
        resolved_tag, installer_sha = "checkout", None
        if args.source == "published":
            try:
                installer_path, resolved_tag, installer_sha = published_installer(world, args.tag, args.step_timeout)
            except Exception as exc:
                record["failures"].append({"reason": "published-installer-fetch-failed", "detail": type(exc).__name__})
                installer_path = None
            if installer_path is None:
                installed = {"exit": 1, "seconds": 0, "output": "published installer unavailable"}
            else:
                install_args[1] = str(installer_path)
                installed = run(sandbox(install_args, world, env, cli=cli_mounts), env=env, timeout=args.step_timeout)
        else:
            installed = run(sandbox(install_args, world, {**env, **install_extras}, cli=cli_mounts), env={**env, **install_extras}, timeout=args.step_timeout)
        record["source_detail"] = {"resolved_tag": resolved_tag, "installer_sha256": installer_sha}
        record["source"]["tag"] = resolved_tag
        record["source"]["sha256"] = installer_sha
        record["steps"].append({"name": "install", **{k:v for k,v in installed.items() if k != "output"}, "output_tail": installed["output"][-400:].replace(str(world), "<world>")})
        if installed["exit"] != 0:
            record["failures"].append({"reason": "install-failed", "detail": installed["output"][-300:].replace(str(world), "<world>")})
        else:
            visible = []
            for name in ("claude", "codex", "opencode", "herdr"):
                probe = run(sandbox(["sh", "-c", f"command -v {name}"], world, env, cli=cli_mounts), env=env, timeout=args.step_timeout)
                if probe["exit"] == 0: visible.append(name)
            versions = {}; transport_supported = {}; generations = {}
            for name in PROFILES[profile]:
                if name in binaries:
                    version = run(sandbox([binaries[name][1], "--version"], world, env, cli=cli_mounts), env=env, timeout=args.step_timeout)
                    if name == "opencode":
                        generations[name] = opencode_generation(version["output"], version["exit"])
                        versions[name] = version["output"][-120:].strip().replace(str(world), "<world>") if generations[name] else "unknown"
                    else:
                        versions[name] = version["output"][-120:].strip().replace(str(world), "<world>") if version["exit"] == 0 else "unknown"
                    if name in TASK_HARNESSES[profile]:
                        transport_supported[name] = probe_transport(name, binaries[name][1], world, env,
                                                                     cli_mounts, args.step_timeout, generations.get(name))
            expected_visible = ["claude"] if profile == "claude-only" else ["claude", "codex", "opencode"]
            record["environment"] = {"visible_cli_names": visible, "cli_versions": versions,
                                      "transport_flags": {"claude": "-p --output-format stream-json --verbose [--resume <id>]",
                                                          "codex": "exec --json --cd <repo> | exec resume --json <id>",
                                                          "opencode": "run [--standalone] --format json [--session <id>]"},
                                      "env_hygiene": ["DISABLE_AUTOUPDATER=1", "no permission flags"],
                                      "transport_supported": transport_supported,
                                      "allowlist_names": sorted(env)}
            if visible != expected_visible: record["failures"].append({"reason": "cli-visibility-mismatch"})
            if platform.system() == "Linux":
                probe = run(sandbox(["true"], world, env, cli=cli_mounts, probes=True), env=env, timeout=args.step_timeout)
                probe_values = {}
                try: probe_values = json.loads(probe["output"].splitlines()[-1])
                except (ValueError, IndexError): pass
                checks = {"real_home_empty": probe_values.get("real_home_empty") is True,
                          "nas_empty": probe_values.get("nas_empty") is True,
                          "home_write_not_host_visible": not (Path.home()/world.name).exists(), "nas_write_not_host_visible": not (Path("/home/nas")/world.name).exists(),
                          "forbidden_env_absent": not any(k.startswith(("HERDR_", "AGENT_", "CLAUDE_CODE_")) for k in env),
                          "herdr_absent_from_path": probe_values.get("herdr_absent_from_path") is True}
                record["isolation"]["checks"] = [{"check": k, "passed": v} for k,v in checks.items()]
                record["isolation"]["probe_exit"] = probe["exit"]
                record["isolation"]["probe_detail"] = probe["output"][-240:].replace(str(world), "<world>")
                record["isolation"]["d3"] = all(checks.values())
                if not record["isolation"]["d3"]: record["failures"].append({"reason": "isolation-probe-failed"})
            runtimes = PROFILES[profile]
            for runtime in runtimes:
                for label, command in (("runtime-status", ["harness", "runtime", "status", "--runtime", runtime, "--json"]),
                                       ("strict-doctor", ["hearting", "runtime", "doctor", "--runtime", runtime, "--strict"])):
                    checked = run(sandbox(command, world, env, cli=cli_mounts), env=env, timeout=args.step_timeout)
                    tail = checked["output"][-400:].replace(str(world), "<world>")
                    fresh = None
                    if label == "runtime-status" and checked["exit"] == 0:
                        try: fresh = json.loads(checked["output"]).get("freshness") == "fresh"
                        except (ValueError, AttributeError): fresh = False
                    record["steps"].append({"name": f"{label}:{runtime}", "exit": checked["exit"], "seconds": checked["seconds"], "fresh": fresh, "output_tail": tail})
                    if checked["exit"] != 0 or fresh is False:
                        record["failures"].append({"reason": f"{label}-failed", "harness": runtime})
            settings = home / ".claude/settings.json"
            if settings.exists():
                try:
                    cfg = json.loads(settings.read_text())
                    commands = [hook.get("command", "") for entries in cfg.get("hooks", {}).values()
                                for entry in entries for hook in entry.get("hooks", []) if isinstance(hook, dict)]
                    # Same rule as post-publish-smoke.sh: every $HOME/<file> a hook names must exist.
                    paths = [p for command in commands for p in re.findall(r"\$HOME/([^\s\"']+)", command)]
                    missing = sorted({p for p in paths if Path(p).suffix and not (home / p).exists()})
                    record["steps"].append({"name": "hook-files", "exit": 1 if missing else 0, "seconds": 0,
                                            "checked": len(set(paths)), "missing": len(missing)})
                    if missing: record["failures"].append({"reason": "claude-hook-command-missing", "count": len(missing),
                                                           "detail": ", ".join(missing[:3])})
                except (ValueError, OSError): record["failures"].append({"reason": "claude-settings-invalid"})
        # Demo test is explicitly observed before and after; baseline failure is expected.
        for harness in TASK_HARNESSES[profile]:
            repo_dir = world / "repos" / harness
            repo_dir.mkdir()
            fixture = repo / "tools/release/fixtures/first-journey"
            shutil.copy2(fixture / "greet.py", repo_dir / "greet.py")
            shutil.copy2(fixture / "focused_test.py.txt", repo_dir / "test_greet.py")
            baseline_env = clean_env(home, pathlist, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
            git_steps = []
            git_started = time.monotonic()
            for git_args in (["git", "init", "-q"], ["git", "config", "user.name", "Journey"],
                             ["git", "config", "user.email", "journey@localhost"],
                             ["git", "add", "greet.py", "test_greet.py"],
                             ["git", "commit", "-qm", "baseline demo"]):
                git_result = run(sandbox(list(git_args), world, baseline_env, repo_dir),
                                 env=baseline_env, timeout=args.step_timeout)
                git_steps.append(git_result["exit"])
                if git_result["exit"] != 0: break
            record["steps"].append({"name": f"baseline-repository:{harness}", "exit": git_steps[-1],
                                    "seconds": round(time.monotonic() - git_started, 3),
                                    "initial_commit": git_steps[-1] == 0})
            if git_steps[-1] != 0:
                record["failures"].append({"harness": harness, "reason": "baseline-repository-failed"})
            # Exercise the staged fixture in the private world and keep output local.
            baseline = run(sandbox(["python3", "-m", "unittest", "-v", "test_greet"], world,
                                   baseline_env, repo_dir), env=baseline_env,
                           timeout=args.step_timeout)
            record["steps"].append({"name": f"baseline:{harness}", "exit": baseline["exit"], "seconds": baseline["seconds"], "expected_failure": True, "output_tail": baseline["output"][-300:].replace(str(world), "<world>")})
            if baseline["exit"] == 0 or "Helo" not in baseline["output"] or "Hello" not in baseline["output"]:
                record["failures"].append({"harness": harness, "reason": "baseline-test-not-observed", "detail": baseline["output"][-300:]})
            if args.model_free:
                model = {"status": "skipped(model-free)", "turns": 0, "canned_answers": 0,
                         "session_tokens": {"status":"unknown","input":None,"output":None,"total":None,"reason":"model-free"}}
            elif any(step["name"] == "install" and step["exit"] != 0 for step in record["steps"]):
                model = {"status": "install-failed", "turns": 0, "canned_answers": 0, "session_tokens": {"status":"unknown","input":None,"output":None,"reason":"installation-failed"}}
            elif harness == "opencode" and generations.get(harness) not in {"v1", "v2"}:
                model = {"status": "opencode-version-unknown", "turns": 0, "canned_answers": 0,
                         "session_tokens": "unknown"}
            elif not transport_supported.get(harness, {}).get("supported", False):
                model = {"status": "unsupported-headless-transport", "turns": 0, "canned_answers": 0,
                         "session_tokens": "unknown", "transport": transport_supported.get(harness)}
            elif (not auth_file(Path(args.auth_root), harness, generations.get(harness)).is_file()
                  or auth_file(Path(args.auth_root), harness, generations.get(harness)).is_symlink()
                  or auth_file(Path(args.auth_root), harness, generations.get(harness)).parent.is_symlink()):
                model = {"status": "measurement-login-missing", "turns": 0, "canned_answers": 0, "session_tokens": "unknown"}
            elif not cli_map[harness]:
                model = {"status": "cli-missing", "turns": 0, "session_tokens": "unknown"}
            else:
                model = {"status": "pending", "turns": 1, "canned_answers": 0, "session_tokens": "unknown"}
                generation = generations.get(harness, "native")
                store = auth_file(Path(args.auth_root), harness, generation)
                try:
                    root = safe_root(args.auth_root, "auth-root", [Path.home(), Path("/home/nas")])
                    if root.stat().st_uid != os.getuid():
                        raise ValueError("auth-root must be owned by the current user")
                    with (root / ".lock").open("a") as lock:
                        os.chmod(root, 0o700)
                        os.chmod(root / ".lock", 0o600)
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        if store.is_symlink() or store.parent.is_symlink():
                            raise ValueError("credential store path is a symlink")
                        before = store.read_bytes()
                        if harness == "claude":
                            target = home / ".claude/.credentials.json"
                        elif harness == "codex":
                            target = home / ".codex/auth.json"
                        elif generation == "v2":
                            target = None
                        else:
                            target = home / ".local/share/opencode/auth.json"
                        if generation != "v2":
                            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                            with os.fdopen(fd, "wb") as stream:
                                stream.write(before)
                        if harness == "claude": env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
                        elif harness == "codex": env["CODEX_HOME"] = str(home / ".codex")
                        else: env["XDG_DATA_HOME"] = str(home / ".local/share")
                        export_argv = sandbox([binaries[harness][1], "auth", "export", "--standalone"], world, env, cli=cli_mounts) if generation == "v2" else None
                        if generation == "v2":
                            imported, import_error = import_v2_credentials(store, world, binaries[harness][1], env, cli_mounts)
                            if not imported:
                                model["status"] = import_error
                                record["harnesses"].append({"name": harness, "model": model})
                                record["failures"].append({"harness": harness, "reason": import_error})
                                continue
                        usage_started = time.monotonic()
                        account_before = claude_account_snapshot(home / ".claude") if harness == "claude" else None
                        if harness == "claude": record["steps"].append({"name":"usage-before:claude","exit":0 if account_before else 1,"seconds":round(time.monotonic()-usage_started,3),"status":"measured" if account_before else "unknown"})
                        started = time.monotonic()
                        baseline_test = (fixture / "focused_test.py.txt").read_bytes()
                        judge = lambda: judge_repo(repo_dir, baseline_test, lambda argv: sandbox(argv, world, baseline_env, repo_dir),
                                                   baseline_env)["focused_test_passed"]
                        # Invoke the CLI by name so an installed launcher in ~/.local/bin is used, as for a user.
                        _ACTIVE_AUTH = {"harness": harness, "generation": generation, "store": store,
                                        "target": target, "original": before, "env": env,
                                        "export_argv": export_argv, "persisted": False}
                        try:
                            outcome = headless_turn(harness, harness, args.prompt, repo_dir, world,
                                                    env, args.task_timeout, cli_mounts, judge,
                                                    opencode_generation=generation if harness == "opencode" else None)
                        finally:
                            pending_error = sys.exc_info()[1]
                            cleanup_error = None
                            try:
                                if not persist_active_auth():
                                    model["auth_persist_warning"] = _ACTIVE_AUTH.get("last_error", "auth-persist-failed") if _ACTIVE_AUTH else "auth-persist-failed"
                                    record["failures"].append({"harness": harness, "reason": "auth-persist-failed"})
                            except BaseException as exc:
                                cleanup_error = exc
                                model["auth_persist_warning"] = "auth-persist-interrupted"
                                record["failures"].append({"harness": harness, "reason": "auth-persist-failed"})
                            finally:
                                _ACTIVE_AUTH = None
                            if cleanup_error is not None and pending_error is None:
                                raise cleanup_error
                        record["steps"].append({"name": f"task:{harness}", "exit": outcome["turns"][-1]["exit"] if outcome["turns"] else 1,
                                                "seconds": round(time.monotonic() - started, 3), "turns": len(outcome["turns"])})
                        for turn in outcome["turns"]:
                            record["steps"].append({"name": f"turn:{harness}:{turn['index']}", "exit": turn["exit"], "seconds": turn["seconds"]})
                        usage_started = time.monotonic()
                        account_after = claude_account_snapshot(home / ".claude") if harness == "claude" else None
                        if harness == "claude": record["steps"].append({"name":"usage-after:claude","exit":0 if account_after else 1,"seconds":round(time.monotonic()-usage_started,3),"status":"measured" if account_after else "unknown"})
                        (world / "logs" / f"{harness}.assistant-text.txt").write_text(outcome["output"], encoding="utf-8")
                        model.update({"status": outcome["status"], "turns": len(outcome["turns"]),
                                      "canned_answers": outcome["canned_answers"], "turn_index": outcome["turn_index"],
                                      "permission_denials": sum(t["permission_denials"] for t in outcome["turns"])})
                        model["session_tokens"] = telemetry(world, harness)
                        if harness == "claude":
                            def delta(key, reset):
                                if not (account_before and account_after) or account_before.get(reset) != account_after.get(reset): return None
                                a, b = account_before.get(key), account_after.get(key)
                                return b - a if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
                            model["account_usage"] = {"before": account_before, "after": account_after,
                                                      "delta_pp": {"5h": delta("rl_5h", "rs_5h"), "7d": delta("rl_7d", "rs_7d")},
                                                      "attribution": "uncertain", "note": "may include other work",
                                                      "reason": None if account_before and account_after else "usage-endpoint-unavailable"}
                            record["account_usage"] = model["account_usage"]
                        elif harness == "codex":
                            model["account_usage"] = {"before": None, "after": model["session_tokens"].get("rate_limits"), "delta_pp": None,
                                                      "attribution": "uncertain", "note": "may include other work", "reason": "no-before-sample"}
                        else:
                            model["account_usage"] = {"before": None, "after": None, "delta_pp": None, "attribution": "uncertain",
                                                      "note": "may include other work", "reason": "unsupported-provider-account-usage"}
                        verify_started = time.monotonic()
                        verification = judge_repo(repo_dir, baseline_test, lambda argv: sandbox(argv, world, baseline_env, repo_dir), baseline_env)
                        verification["route_evidence_count"] = route_evidence(world)
                        verification["route_evidence_observed"] = verification["route_evidence_count"] > 0
                        record["steps"].append({"name": f"verify:{harness}", "exit": 0 if verification["focused_test_passed"] else 1,
                                                "seconds": round(time.monotonic() - verify_started, 3)})
                        model["verification"] = verification
                        # The repo judgment decides success, not the reply text or the exit code.
                        if verification["focused_test_passed"]:
                            model["status"] = "completed" if verification["route_evidence_observed"] else "fixed-without-route-evidence"
                        elif model["status"] == "candidate-complete":
                            model["status"] = "task-incomplete"
                except Exception as exc:
                    model["status"] = "task-error"
                    model["detail"] = type(exc).__name__
            record["harnesses"].append({"name": harness, "model": model})
            if model["status"] not in {"skipped(model-free)", "completed"}:
                record["failures"].append({"harness": harness, "reason": model["status"], "detail": str(model.get("detail", ""))[:200]})
        if platform.system() != "Linux": record["isolation"]["d3"] = "not-proven"
    except Exception as exc:
        record["failures"].append({"reason": "journey-error", "detail": (type(exc).__name__ + ": " + str(exc)[:300]).replace(str(world), "<world>")})
    finally:
        # The credential lifetime is owned by the same lock scope as the task.
        _ACTIVE_AUTH = None
        record["elapsed_seconds"] = round(time.monotonic() - began, 3)
        record["duration_s"] = record["elapsed_seconds"]
        record["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if args.keep_world == "always" or (args.keep_world == "failure" and record["failures"]):
            record["world"] = str(world)
        else:
            shutil.rmtree(world, ignore_errors=True)
    return record


def _tokens_line(tokens) -> str:
    if not isinstance(tokens, dict) or tokens.get("total") is None:
        reason = tokens.get("reason") if isinstance(tokens, dict) else tokens
        return f"unknown ({reason})"
    parts = [f"{k}={tokens[k]}" for k in ("input", "output", "cache_creation", "cache_read", "cached_input",
                                          "reasoning", "reasoning_output") if tokens.get(k) is not None]
    return f"total={tokens['total']} ({', '.join(parts)}; sessions={tokens.get('sessions')})"


def write_report(record: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "journey.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    source = record["source"] if isinstance(record["source"], dict) else {"kind": record["source"]}
    detail = record.get("source_detail", {})
    isolation = record["isolation"]
    lines = [f"# First journey: {record['profile']} ({record.get('mode', 'unknown')})", "",
             f"- Source: {source.get('kind')}; tag {detail.get('resolved_tag', source.get('tag'))}; "
             f"installer sha256 {detail.get('installer_sha256') or 'n/a'}",
             f"- Started: {record.get('started_at')}; finished: {record.get('finished_at')}; "
             f"elapsed {record['elapsed_seconds']} s",
             f"- Isolation: {isolation['level']}; d3={isolation['d3']}", "",
             "## Steps", "", "| step | exit | seconds |", "| --- | ---: | ---: |"]
    lines += [f"| {s['name']} | {s['exit']} | {s['seconds']} |" for s in record["steps"]]
    lines += ["", "## Tasks", ""]
    for h in record["harnesses"]:
        m = h["model"]; usage = m.get("account_usage") or {}
        lines.append(f"- {h['name']}: {m['status']}; turns={m['turns']}; canned answers={m.get('canned_answers', 0)}; "
                     f"session tokens {_tokens_line(m.get('session_tokens'))}")
        if usage:
            lines.append(f"  - account usage before={usage.get('before')} after={usage.get('after')} "
                         f"delta_pp={usage.get('delta_pp')}; attribution {usage.get('attribution')} ({usage.get('note')})")
        verification = m.get("verification")
        if verification:
            lines.append(f"  - verification: code_fixed={verification.get('code_fixed')} focused_test_passed="
                         f"{verification.get('focused_test_passed')} route_evidence={verification.get('route_evidence_count')} "
                         f"candidate={verification.get('candidate')}")
    account = record.get("account_usage") or {}
    lines += ["", "## Account usage", "",
              f"Before: {account.get('before')}; after: {account.get('after')}; delta_pp: {account.get('delta_pp')}; "
              f"attribution uncertain ({account.get('note')}); reason: {account.get('reason')}.",
              "", "## Headless uncovered", ""]
    lines += [f"- {x}" for x in record.get("headless_uncovered", [])]
    env = record.get("environment", {})
    lines += ["", "## Environment", "", f"- Visible CLIs: {env.get('visible_cli_names')}",
              f"- CLI versions: {env.get('cli_versions')}", f"- Transport: {env.get('transport_flags')}",
              f"- Transport supported: {env.get('transport_supported')}",
              f"- Env hygiene: {env.get('env_hygiene')}", f"- Env names: {env.get('allowlist_names')}",
              "", "## Isolation checks", ""]
    lines += [f"- {x['check']}: {'PASS' if x['passed'] else 'FAIL'}" for x in isolation.get("checks", [])] or ["- not-proven"]
    lines += [f"- host I/O exceptions: {isolation.get('host_io_exceptions')}", "", "## Failures", ""]
    lines += [f"- {f.get('harness', 'runner')}: {f['reason']}" + (f" — {f['detail']}" if f.get("detail") else "")
              for f in record["failures"]] or ["- None"]
    if record.get("world"):
        lines += ["", f"World kept for inspection (local only): {record['world']}"]
    (output / "journey.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login"); login.add_argument("--harness", choices=AUTH_NAMES, required=True); login.add_argument("--auth-root", default=f"/tmp/hearting-measure-auth-{os.getuid()}")
    runp = sub.add_parser("run")
    runp.add_argument("--output", required=True); runp.add_argument("--source", choices=("published", "checkout"), default="published")
    runp.add_argument("--tag", default="latest"); runp.add_argument("--ref", default="HEAD"); runp.add_argument("--auth-root", default=f"/tmp/hearting-measure-auth-{os.getuid()}")
    runp.add_argument("--local-root", default="/tmp"); runp.add_argument("--prompt", default=DEFAULT_PROMPT); runp.add_argument("--model-free", action="store_true")
    runp.add_argument("--keep-world", choices=("always", "failure", "never"), default="failure"); runp.add_argument("--step-timeout", type=int, default=300); runp.add_argument("--task-timeout", type=int, default=600)
    args = parser.parse_args()
    if args.command == "login":
        try:
            root = safe_root(args.auth_root, "auth-root", [Path.home(), Path("/home/nas")])
            if root.stat().st_uid != os.getuid():
                raise ValueError("auth-root must be owned by the current user")
        except Exception as exc:
            print(f"unsafe-auth-root: {type(exc).__name__}", file=sys.stderr); return 2
        harness = args.harness; cli = shutil.which(harness)
        if not cli: print("cli-missing", file=sys.stderr); return 1
        with (root / ".lock").open("a") as lock:
            os.chmod(root, 0o700)
            os.chmod(root / ".lock", 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            world = Path(tempfile.mkdtemp(prefix=f"first-journey-login-{harness}-", dir="/tmp"))
            os.chmod(world, 0o700)
            try:
                for name in ("home", "tmp", "config", "data", "state", "cache", "claude", "codex", "opencode"):
                    (world / name).mkdir(parents=True, mode=0o700, exist_ok=True)
                config = {"claude": world / "claude", "codex": world / "codex", "opencode": world / "opencode"}[harness]
                env = clean_env(world / "home", [Path("/usr/bin"), Path("/bin")], {
                    "CLAUDE_CONFIG_DIR": str(config), "CODEX_HOME": str(config),
                    "XDG_CONFIG_HOME": str(world / "config"), "XDG_DATA_HOME": str(world / "data"),
                    "XDG_STATE_HOME": str(world / "state"), "XDG_CACHE_HOME": str(world / "cache")})
                if harness == "opencode": env["XDG_DATA_HOME"] = str(world / "data")
                resolved = Path(cli).resolve()
                if harness == "codex": mounts=[(resolved.parent.parent,"/opt/journey-cli/codex")]; executable="/opt/journey-cli/codex/bin/codex"
                else: mounts=[(resolved,"/opt/journey-cli/"+harness)]; executable="/opt/journey-cli/"+harness
                generation = None
                if harness == "opencode":
                    probe = run(sandbox([executable, "--version"], world, env, cli=mounts), env=env,
                                timeout=AUTH_CAPTURE_TIMEOUT)
                    generation = opencode_generation(probe["output"], probe["exit"])
                    if generation is None:
                        print("opencode-version-unknown", file=sys.stderr)
                        return 1
                command = {"claude": [executable, "auth", "login"], "codex": [executable, "login"],
                           "opencode": [executable, "auth", "login", *( ["--standalone"] if generation == "v2" else [] )]}[harness]
                result = subprocess.run(sandbox(command, world, env, cli=mounts), env=env)
                if result.returncode == 0:
                    if harness == "opencode" and generation == "v2":
                        exported = private_auth_command(sandbox([executable, "auth", "export", "--standalone"],
                                                                 world, env, cli=mounts), env=env)
                        credential = exported["stdout"]
                        if not exported["ok"]:
                            print(f"auth-export-{exported['reason']}", file=sys.stderr)
                            return 1
                        if not credential_complete(harness, credential, generation):
                            print("login-credential-incomplete", file=sys.stderr)
                            return 1
                        atomic_auth_write(auth_file(root, harness, generation), credential)
                        return result.returncode
                    candidates = {"claude": config / ".credentials.json", "codex": config / "auth.json",
                                  "opencode": world / "data/opencode/auth.json"}
                    source = candidates[harness]
                    if not source.is_file() or source.is_symlink(): return 1
                    credential = source.read_bytes()
                    if not credential_complete(harness, credential, generation):
                        print("login-credential-incomplete", file=sys.stderr); return 1
                    target = auth_file(root, harness, generation)
                    atomic_auth_write(target, credential)
                return result.returncode
            finally:
                shutil.rmtree(world, ignore_errors=True)
    repo = Path(__file__).resolve().parents[2]
    try:
        local = safe_root(args.local_root, "local-root", [Path.home(), Path("/home/nas")])
        output = Path(args.output).expanduser().absolute()
        if output.is_symlink() or local == output.resolve():
            raise ValueError("output must be separate from local experiment root")
        auth = Path(args.auth_root).expanduser().absolute()
        args.local_root = str(local); args.auth_root = str(auth)
    except Exception as exc:
        print(f"unsafe-path: {type(exc).__name__}: {exc}", file=sys.stderr); return 2
    overall = 0
    signal.signal(signal.SIGINT, auth_signal)
    signal.signal(signal.SIGTERM, auth_signal)
    for profile in ("claude-only", "all"):
        record = journey_profile(profile, args, repo)
        write_report(record, output / profile)
        if record["failures"]: overall = 1
    return overall


if __name__ == "__main__":
    raise SystemExit(main())
