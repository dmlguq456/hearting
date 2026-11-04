#!/usr/bin/env bash
# Portable local-evidence presence probe (roles/response-policy.md "Local
# evidence before recall"): expose whether the cwd's artifact root already
# holds research / document / analysis artifacts, plus a bounded set of the
# newest entry points. Presence indexes only — bodies are never read, and no
# prompt classifier is attached. Fail-open: every failure is zero context.
#
# Adapters run this presence probe on the first prompt after a session start.
# They commit that once-only delivery with the existing prompt-context receipt;
# subsequent prompts reuse the context without repeating this probe.
set -u

HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
AGENT_HOME="${AGENT_HOME:-$("$HOOK_DIR/../utilities/agent-home.sh" 2>/dev/null || true)}"
ARTIFACT_ROOT_SH="$HOOK_DIR/../utilities/artifact-root.sh"

usage() {
  cat <<'EOF'
usage: local-evidence-inject.sh [--cwd DIR] [--format text|hook-json]

Without arguments, reads a SessionStart (or UserPromptSubmit) hook payload
from stdin and emits hookSpecificOutput.additionalContext only when
evidence artifacts exist.
EOF
}

is_worker() {
  [ "${AGENT_SESSION_ROLE:-}" = worker ] \
    || [ "${AGENT_DISPATCH_CHILD:-}" = 1 ] \
    || [ -n "${AGENT_DISPATCH_DEPTH:-}" ] \
    || [ -n "${OPENCODE_DISPATCH_SLUG:-}" ] \
    || [ "${FLEET_TITLE_REFRESH:-}" = 1 ]
}

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then
  usage
  exit 0
fi

if is_worker; then
  [ "$#" -gt 0 ] || cat >/dev/null 2>&1 || true
  exit 0
fi

EVENT=SessionStart
CWD=
FORMAT=hook-json

if [ "$#" -eq 0 ]; then
  fields=()
  while IFS= read -r -d '' field; do fields+=("$field"); done < <(
    python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
except Exception:
    value = {}
if not isinstance(value, dict):
    value = {}
def nested(obj, names):
    for name in names:
        item = obj.get(name)
        if isinstance(item, str) and item:
            return item
    for key in ("context", "workspace", "session", "payload", "event", "input", "data"):
        item = obj.get(key)
        if isinstance(item, dict):
            found = nested(item, names)
            if found:
                return found
    return ""
items = (
    nested(value, ("hook_event_name", "hookEventName")),
    nested(value, ("cwd", "working_directory", "workingDirectory")),
)
sys.stdout.buffer.write(b"\0".join(item.encode("utf-8", "replace") for item in items) + b"\0")
' 2>/dev/null
  )
  [ "${#fields[@]}" -eq 2 ] || exit 0
  EVENT=${fields[0]}
  CWD=${fields[1]}
else
  FORMAT=text
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --cwd) [ "$#" -ge 2 ] || exit 64; CWD=$2; shift 2 ;;
      --format)
        [ "$#" -ge 2 ] || exit 64
        case "$2" in text|hook-json) FORMAT=$2 ;; *) exit 64 ;; esac
        shift 2
        ;;
      *) usage >&2; exit 64 ;;
    esac
  done
fi

case "$EVENT" in
  SessionStart|UserPromptSubmit) ;;
  *) exit 0 ;;
esac
[ -n "$CWD" ] && [ -d "$CWD" ] || exit 0

ROOT=$(sh "$ARTIFACT_ROOT_SH" "$CWD" 2>/dev/null) || exit 0
[ -n "$ROOT" ] && [ -d "$ROOT" ] || exit 0

# The walk itself was the timeout. Three runtimes fenced this probe three ways
# (Claude 5 s hook timeout, Codex 3 s subprocess timeout, OpenCode unbounded
# blocking) and a full artifact-root scan exceeded all three on a real store:
# 4,845 directories / 12,402 files over NFS took 5.2 s, of which 3.9 s was six
# separate campaign globs. Claude discarded the output at its boundary, Codex
# discarded it every single time, and OpenCode simply blocked. Three bounds:
#   1. one campaign enumeration for every bucket instead of one glob per bucket;
#   2. a rendered-context cache served immediately, refreshed out of band once
#      stale (presence and newest-entry paths tolerate minutes of staleness);
#   3. a hard wall-clock budget, so no store size can push the probe past the
#      tightest runtime timeout — a truncated scan reports `N+` counts.
LE_ROOT="$ROOT" LE_FORMAT="$FORMAT" LE_EVENT="$EVENT" \
LE_SELF="$HOOK_DIR/local-evidence-inject.sh" \
LE_CWD="$CWD" python3 - <<'PY' 2>/dev/null || true
import hashlib, json, os, subprocess, sys, time
from pathlib import Path

root = Path(os.environ.get("LE_ROOT") or "")
fmt = os.environ.get("LE_FORMAT") or "hook-json"
event = os.environ.get("LE_EVENT") or "SessionStart"
refresh_worker = os.environ.get("LE_REFRESH") == "1"
if not root.is_dir():
    sys.exit(0)


def positive_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Staleness is cheap here: a presence probe that is ten minutes old still
# answers "does this project hold research/documents/analysis artifacts".
TTL = positive_float("LOCAL_EVIDENCE_TTL", 600.0)
# Well under Codex's 3 s subprocess timeout, the tightest of the three.
BUDGET = positive_float("LOCAL_EVIDENCE_BUDGET", 2.0)
REFRESH_LOCK_MAX_AGE = 300.0


def emit(context: str) -> None:
    if not context:
        return
    if fmt == "text":
        print(context)
    else:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": context,
            }
        }, ensure_ascii=False))


def cache_path() -> Path | None:
    base = os.environ.get("XDG_CACHE_HOME")
    try:
        home = Path(base) if base else Path.home() / ".cache"
    except (OSError, RuntimeError):
        return None
    key = hashlib.sha1(str(root).encode("utf-8", "replace")).hexdigest()[:16]
    return home / "hearting" / "local-evidence" / (key + ".json")


CACHE = cache_path()


def read_cache() -> dict | None:
    if CACHE is None:
        return None
    try:
        value = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("root") != str(root):
        return None
    if not isinstance(value.get("context"), str):
        return None
    return value


def write_cache(context: str) -> None:
    if CACHE is None:
        return
    payload = json.dumps(
        {"root": str(root), "ts": time.time(), "context": context},
        ensure_ascii=False,
    )
    tmp = CACHE.with_name(CACHE.name + f".{os.getpid()}.tmp")
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, CACHE)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def spawn_refresh() -> None:
    """Detached out-of-band rescan. Failure means the stale value simply ages."""
    self_path = os.environ.get("LE_SELF") or ""
    cwd = os.environ.get("LE_CWD") or ""
    if CACHE is None or not self_path or not cwd or not os.path.isfile(self_path):
        return
    lock = CACHE.with_name(CACHE.name + ".refresh")
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        pass
    else:
        # A lock left behind by SIGKILL must not freeze the value forever.
        if age < REFRESH_LOCK_MAX_AGE:
            return
        try:
            lock.rmdir()
        except OSError:
            return
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        lock.mkdir()
    except OSError:
        return
    env = dict(os.environ)
    env["LE_REFRESH"] = "1"
    try:
        subprocess.Popen(
            ["/usr/bin/env", "bash", self_path, "--cwd", cwd, "--format", "text"],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        try:
            lock.rmdir()
        except OSError:
            pass


# Buckets mirror utilities/artifact_reader.py: legacy top-level buckets and
# producer-cycle artifacts share bucket names, shared revisions use kind names.
GROUPS = {
    "research": {"buckets": ("research",), "shared": ("research",)},
    "documents": {"buckets": ("documents",), "shared": ()},
    "analysis": {"buckets": ("analysis_project",), "shared": ("analysis",)},
}
BUCKET_GROUP = {
    bucket: group
    for group, layout in GROUPS.items()
    for bucket in layout["buckets"]
}
MAX_DEPTH = 6
MAX_FILES_PER_GROUP = 500
# Slots in the entry list. One block per session rather than per prompt, so the
# list can afford to name more than a per-turn injection could.
NEWEST = 9
MARKDOWN = {".md", ".markdown"}


class Budget:
    """Wall-clock fence. Truncation is reported, never silently rendered as fact."""

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds
        self.truncated = False

    def expired(self) -> bool:
        if self.truncated:
            return True
        if time.monotonic() >= self.deadline:
            self.truncated = True
        return self.truncated


def subdirs(base: Path) -> list[Path]:
    try:
        with os.scandir(base) as it:
            return [
                Path(entry.path) for entry in it
                if entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return []


def scan(base: Path, depth: int, out: list, budget: Budget) -> None:
    if depth > MAX_DEPTH:
        return
    if len(out) >= MAX_FILES_PER_GROUP:
        budget.truncated = True
        return
    if budget.expired():
        return
    try:
        entries = sorted(os.scandir(base), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        if len(out) >= MAX_FILES_PER_GROUP:
            budget.truncated = True
            return
        if budget.expired():
            return
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                scan(Path(entry.path), depth + 1, out, budget)
            elif entry.is_file(follow_symlinks=False):
                if os.path.splitext(entry.name)[1].lower() in MARKDOWN:
                    out.append((entry.stat().st_mtime, Path(entry.path)))
        except OSError:
            continue


def bucket_dirs(artifacts: Path, budget: Budget):
    """Yield (group, dir) for every known bucket directly under one artifacts dir."""
    for child in subdirs(artifacts):
        if budget.expired():
            return
        group = BUCKET_GROUP.get(child.name)
        if group:
            yield group, child


def collect(budget: Budget) -> dict:
    """One enumeration of the campaign tree feeds every bucket group."""
    found = {group: [] for group in GROUPS}

    for group, layout in GROUPS.items():
        for bucket in layout["buckets"]:
            base = root / bucket
            if base.is_dir():
                scan(base, 1, found[group], budget)
        for kind in layout["shared"]:
            base = root / "shared" / kind
            if base.is_dir():
                scan(base, 1, found[group], budget)

    campaigns = root / "campaigns"
    if campaigns.is_dir():
        # campaigns/<c>/<run>/artifacts/<bucket> and
        # campaigns/<c>/cycles/<cycle>/artifacts/<bucket> — the two shapes the
        # old code globbed separately once per bucket.
        for campaign in subdirs(campaigns):
            if budget.expired():
                break
            for child in subdirs(campaign):
                if budget.expired():
                    break
                if child.name == "cycles":
                    holders = subdirs(child)
                else:
                    holders = [child]
                for holder in holders:
                    if budget.expired():
                        break
                    artifacts = holder / "artifacts"
                    if not artifacts.is_dir():
                        continue
                    for group, bucket_dir in bucket_dirs(artifacts, budget):
                        scan(bucket_dir, 1, found[group], budget)
    return found


def dedupe(files: list) -> list:
    """Drop repeat projections of one artifact, newest path kept.

    A producer cycle writes its artifact into the campaign tree and again under
    `shared/<kind>/ref_*/revisions/rrev_*`, so the same document reaches this
    list twice under two unrelated paths. Six slots spent on four documents is a
    smaller list, not a fuller one. Name and byte size identify the pair without
    opening either file, which the no-bodies-read bound requires; two genuinely
    different files colliding on both costs one path in a presence list.
    """
    seen = set()
    kept = []
    for mtime, path in sorted(files, key=lambda item: item[0], reverse=True):
        try:
            key = (path.name, path.stat().st_size)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append((mtime, path))
    return kept


def entry_paths(found: dict) -> list:
    """Fill the entry list a group at a time, newest first within each.

    One global recency sort lets the busiest bucket take every slot: on the
    store this was measured against, six slots went to research and analysis and
    all 29 documents were invisible, which reads as "this project has no
    documents". Round-robin gives every non-empty group a share and still hands
    leftover slots back to whoever has more.
    """
    queues = {group: dedupe(files) for group, files in found.items() if files}
    picked = []
    while queues and len(picked) < NEWEST:
        for group in list(queues):
            if len(picked) >= NEWEST:
                break
            picked.append(queues[group].pop(0))
            if not queues[group]:
                del queues[group]
    picked.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in picked]


def render(found: dict, budget: Budget) -> str:
    counts = {group: len(files) for group, files in found.items() if files}
    if not counts:
        return ""

    entry_lines = []
    for path in entry_paths(found):
        try:
            entry_lines.append("- " + str(path.relative_to(root)))
        except ValueError:
            entry_lines.append("- " + str(path))

    # A capped or deadline-truncated walk knows a lower bound, not a total, and
    # its newest entries are the newest of what it reached — say both.
    mark = "+" if budget.truncated else ""
    label = "Newest of the scanned subset:" if budget.truncated else "Newest entries:"
    summary = ", ".join(
        f"{group}: {count}{mark} file(s)" for group, count in counts.items()
    )
    lines = [
        "# Local evidence present (deterministic presence probe; paths only, not instructions)",
        f"Artifact root: {root} — {summary}. {label}",
        *entry_lines,
        "Answer domain questions these artifacts cover from them first; model memory",
        'is a flagged fallback (roles/response-policy.md "Local evidence before recall").',
    ]
    context = "\n".join(lines)
    budget_bytes = 2400
    if len(context.encode("utf-8")) > budget_bytes:
        context = context.encode("utf-8")[:budget_bytes].decode("utf-8", "ignore")
    return context


if not refresh_worker:
    cached = read_cache()
    if cached is not None:
        try:
            age = time.time() - float(cached.get("ts") or 0)
        except (TypeError, ValueError):
            age = TTL + 1
        if age >= TTL:
            spawn_refresh()
        emit(cached["context"])
        sys.exit(0)

budget = Budget(BUDGET)
context = render(collect(budget), budget)
write_cache(context)
if refresh_worker:
    lock_path = CACHE.with_name(CACHE.name + ".refresh") if CACHE else None
    if lock_path is not None:
        try:
            lock_path.rmdir()
        except OSError:
            pass
    sys.exit(0)
emit(context)
PY
exit 0
