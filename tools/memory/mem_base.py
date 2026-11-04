"""Memory: constants and stateless helpers.

Record constants and limits, record parse/serialize, FTS and CJK text helpers,
entity extraction, schema-migration steps, row/meta conversion, query
tokenizing, candidate rendering, transcript text readers, repository rekey
helpers, and sync/exchange settings. Nothing here reads the environment at
import time or keeps state; that stays in `mem.py`.

Split out of `mem.py`, which re-exports every name defined here.
"""
import datetime
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from claude_project_dir import encode_project_dir_component
import protocol_v2
import sync_v2


TIERS = ("working", "durable")
SCOPES = ("project", "global")
WORKING_TTL_DAYS = 21
# v2 strength/access, v3 cwd remap, v4 injection, v5 delivery,
# v6 legacy cwd_origin re-normalization, v7 retrieval capsules and temporal state,
# v8 immutable operation/outbox/frontier/peer state; v9 sealed migration
# receipts and the durable old-writer capability fence; v10 bounded migration
# failure/status evidence.
SCHEMA_VERSION = 10
FM_ORDER = ["id", "tier", "scope", "type", "cwd_origin", "created", "updated",
            "expires", "source", "tags", "links", "strength", "last_accessed", "injection_flag",
            "delivery_state", "headline", "aliases", "entities", "topics", "artifact_refs",
            "status", "canonical_id", "superseded_by", "capsule_version"]
INJECT_DEFAULT_MAX_CHARS = 2000
INJECT_DEFAULT_MAX_BULLETS = 15
INJECT_DEFAULT_MAX_WORKING = 8
INJECT_DEFAULT_MAX_DURABLE = 4
INJECT_DEFAULT_CLEANUP_LINES = 2
INJECT_DEFAULT_SNIPPET_CHARS = 100

# Canonical column order for deterministic export/import round trips.
RECORD_COLS = ("id", "tier", "scope", "type", "cwd_origin", "created", "updated",
               "expires", "source", "tags", "links", "body", "strength", "last_accessed",
               "injection_flag", "delivery_state", "headline", "aliases", "entities", "topics",
               "artifact_refs", "status", "canonical_id", "superseded_by", "capsule_version")
DELIVERY_STATES = ("ordinary", "pending", "consumed")
RECORD_STATUSES = ("active", "superseded")
CAPSULE_LIST_FIELDS = ("aliases", "entities", "topics", "artifact_refs")
CANDIDATE_MAX_RESULTS = 6
CANDIDATE_MAX_UTF8_BYTES = 2400
CANDIDATE_MAX_QUERY_CHARS = 16000
CANDIDATE_MAX_FTS_TERMS = 32
CANDIDATE_SEEN_MAX_IDS = 512
CANDIDATE_SEEN_TTL_SECONDS = 24 * 60 * 60
CANDIDATE_SEEN_LOCK_WAIT_SECONDS = 1.0
# Values a bridge passes when it has no real session; they must never share a history.
CANDIDATE_SEEN_SESSIONLESS = frozenset(
    {"", "memory-prompt-hook", "opencode-plugin", "codex-hook"})
CANDIDATE_HOOK_STDIN_SECONDS = 0.5
# `mem candidates` runs inside every prompt hook: Claude cancels it at 10 s,
# the Codex and OpenCode bridges kill it at 2 s and 3 s. Its git and FTS reads
# share one deadline from process start; a read the deadline cuts shows no
# candidates this turn (PEER-HOOK measured 72 s with the DB pages evicted and
# a slow NAS cwd). Normal probes take about 0.2 s.
CANDIDATE_PROBE_BUDGET_SECONDS = 5.0
_PROBE_PROGRESS_STEPS = 1000     # SQLite VM steps between deadline checks
# Optional short body (off unless MEM_CANDIDATE_BODY=1): one clearly leading,
# newly shown record, at most CANDIDATE_BODY_MAX_CHARS characters, paid for by
# at most half of the bytes this session already saved by not repeating ids.
CANDIDATE_BODY_MAX_CHARS = 600
CANDIDATE_BODY_MIN_GAP = 2.0     # bm25 lead of rank 1 over rank 2 (rank 2 = 0 when alone)
CANDIDATE_BODY_MIN_BYTES = 48    # a body trimmed below this is not worth a line
RECALL_RECEIPT_SCHEMA = 1
RECALL_RECEIPT_MAX_AGE_SECONDS = 14 * 24 * 60 * 60
WRITE_ACTORS = ("manual", "distiller", "curator", "lifecycle", "sync", "restore", "tidy-applier")


class UnsupportedSchemaError(RuntimeError):
    """The on-disk schema is newer than this writer understands."""
# A distinct sentinel lets callers intentionally omit event cwd without
# changing the ambient fallback retained by existing journal callers.
_WRITE_EVENT_CWD_UNSET = object()
# Doctor thresholds mirror the cleanup-candidate defaults.
DOCTOR_DURABLE_SOFT_CEILING = 80
DOCTOR_WORKING_BLOAT_CEILING = 150


def artifact_root(cwd: Path) -> Path:
    """Return the project artifact root, preferring the neutral name."""
    agent = cwd / ".agent_reports"
    if agent.exists():
        return agent
    legacy = cwd / ".claude_reports"
    if legacy.exists():
        return legacy
    return agent


# Auto-commit prefix distinguishes synchronized dumps from manual commits.
AUTO_DUMP_MSG_PREFIX = "chore: dump — auto-sync"

# Injection and secret guards.
INJECTION_PAT = re.compile(
    r"(ignore (all |the )?previous|disregard (all|previous)|you must now|"
    r"system prompt|<\|.*?\|>|act as (an? )?(admin|root)|override (the )?instruction)", re.I)
SECRET_PAT = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
    r"(api[_-]?key|secret|token|password)\s*[:=]\s*[A-Za-z0-9_\-]{12,})", re.I)


# ---------- pure helpers ----------
def today():
    return datetime.date.today().isoformat()


def enc_cwd(path):
    # A memory-store key namespace (project_key fallbacks, source keys), frozen
    # so stored rows stay addressable. It is NOT Claude Code's projects-dir
    # name for every path; locate that with `encode_project_dir`.
    return re.sub(r"[/._]", "-", str(path))


def _git_rc(args, cwd):
    """Return a git exit code; never raise and return nonzero on failure."""
    try:
        r = subprocess.run(["git"] + args, cwd=str(cwd),
                           capture_output=True, text=True, timeout=5)
        return r.returncode
    except Exception:
        return 1


def _git_run(args, cwd, env=None, timeout=30):
    """Run git returning ``(rc, stdout, stderr)``; never raise."""
    try:
        e = None
        if env:
            e = os.environ.copy()
            e.update(env)
        r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout, env=e)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as ex:
        return 1, "", str(ex)


def _norm_remote(url):
    """Normalize an SCP or HTTPS remote URL to ``host/org/repo``."""
    u = url.strip()
    if not u:
        return ""
    # scp-like: git@host:org/repo(.git)
    m = re.match(r"^[\w.+-]+@([\w.-]+):(.+)$", u)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        # https://host/org/repo(.git) or ssh://host/org/repo
        m2 = re.match(r"^[a-zA-Z]+://(?:[^@/]+@)?([\w.-]+)(?::\d+)?/(.+)$", u)
        if m2:
            host, path = m2.group(1), m2.group(2)
        else:
            return ""  # Unrecognized; caller proceeds to the next fallback.
    path = re.sub(r"\.git$", "", path).strip("/")
    return f"{host}/{path}" if path else ""


def _seed_marker(marker):
    """Create a 16-hex project marker for a repository without a remote.

    Best-effort add it to ``.git/info/exclude``; failures are non-fatal.
    """
    try:
        val = os.urandom(8).hex()  # 16 hex chars
        marker.write_text(val + "\n", encoding="utf-8")
    except Exception:
        return None
    # best-effort: keep the marker out of `git status` via per-repo exclude (not tracked .gitignore)
    try:
        excl = marker.parent / ".git" / "info" / "exclude"
        if excl.parent.is_dir():
            cur = excl.read_text(encoding="utf-8") if excl.exists() else ""
            if ".claude-project-id" not in cur:
                with excl.open("a", encoding="utf-8") as f:
                    f.write(("" if cur.endswith("\n") or cur == "" else "\n")
                            + ".claude-project-id\n")
                sys.stderr.write(
                    f"[project_key] seeded .claude-project-id at {marker.parent} "
                    f"(+ .git/info/exclude)\n")
    except Exception:
        pass  # Failure only leaves the marker visible in git status.
    return val


def _decode_enc_cwd(enc):
    """Resolve an encoded cwd to an existing absolute path, or return None."""
    if not enc or not enc.startswith("-"):
        return None
    def walk(cur, rem, depth):
        if depth > 64:               # Bound malformed input and symlink loops.
            return None
        if rem == "":
            return cur if cur.is_dir() else None
        if not rem.startswith("-"):   # Remaining components start with a separator.
            return None
        body = rem[1:]
        if body == "":
            return cur if cur.is_dir() else None
        try:
            children = sorted(p.name for p in cur.iterdir())
        except Exception:
            return None
        for name in children:
            # Encode one component without a leading separator, by both rules:
            # Claude Code's projects-dir names and the legacy `enc_cwd` keys.
            for e in dict.fromkeys((encode_project_dir_component(name), re.sub(r"[/._]", "-", name))):
                if body == e:
                    cand = cur / name
                    if cand.is_dir():
                        return cand
                elif body.startswith(e + "-"):
                    r = walk(cur / name, body[len(e):], depth + 1)  # Remaining text begins with '-'.
                    if r is not None:
                        return r
        return None
    return walk(Path("/"), enc, 0)


def _event_cwd(raw):
    """Return a resolved existing absolute cwd for a source event, or None."""
    if isinstance(raw, Path):
        raw = str(raw)
    if not isinstance(raw, str) or not raw:
        return None
    if raw.startswith("-"):
        path = _decode_enc_cwd(raw)
    elif raw.startswith("/"):
        path = Path(raw)
    else:
        return None
    if path is None or not path.is_dir():
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    return str(resolved) if resolved.is_dir() else None


def slugify(text, n=4):
    words = re.findall(r"[A-Za-z0-9가-힣]+", text.lower())[:n]
    s = "-".join(words) or "note"
    return s[:48]


def norm_body(body):
    return re.sub(r"[\s\W_]+", " ", body.lower()).strip()


# ---------- frontmatter for migration input and projection output ----------
def parse_record(text):
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta, body = {}, parts[2].lstrip("\n")
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        elif v in ("null", ""):
            v = None
        meta[k] = v
    return meta, body


def serialize_record(meta, body):
    lines = ["---"]
    for k in FM_ORDER:
        # Expose truthy injection flags in Markdown for audit visibility.
        if k == "injection_flag":
            if not meta.get("injection_flag"):
                continue
        elif k not in meta or meta[k] is None:
            if k in ("expires", "source", "tags", "links", "strength", "last_accessed"):
                continue
        v = meta.get(k)
        if isinstance(v, list):
            v = "[" + ", ".join(v) + "]"
        elif v is None:
            v = "null"
        lines.append(f"{k}: {v}")
    lines += ["---", "", body.rstrip(), ""]
    return "\n".join(lines)


# ---------- read legacy Markdown migration sources ----------
def iter_md_files(root, exclude=()):
    """Iterate legacy Markdown migration sources; unused by DB-native reads."""
    exclude_set = set(exclude)
    for p in Path(root).rglob("*.md"):
        if p.name in exclude_set:
            continue
        if "_projection" in p.parts:
            continue
        # Hidden runtime-state components are not legacy sources of truth.
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            rel_parts = p.parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        try:
            meta, body = parse_record(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta["_path"] = p  # Migration-only metadata, absent from DB-native paths.
        yield meta, body


# ---------- DB connection and schema ----------
def _fts_available(con):
    try:
        con.execute("CREATE VIRTUAL TABLE temp.t USING fts5(x)")
        con.execute("DROP TABLE temp.t")
        return True
    except sqlite3.OperationalError:
        return False


# ---------- CJK bigram shadow index (audit W4, 2026-07-22) ----------
# System SQLite < 3.34 lacks the trigram tokenizer, so CJK substring recall
# used to fall back to an unranked LIKE scan. The shadow index stores each
# body with CJK runs rewritten as overlapping bigrams; unicode61 (available
# everywhere) then gives ranked bm25 substring matching for Korean/CJK.
_CJK_RUN_RE = re.compile(r"[　-鿿가-힯]+")


def _cjk_bigrams(run):
    """Overlapping bigrams of one CJK run; a single char stands alone."""
    if len(run) < 2:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def _cjk_shadow_text(text):
    """Rewrite CJK runs as space-joined overlapping bigrams.

    Latin/digit text passes through unchanged so mixed-script queries can
    still match inside the shadow row. Snippets always render from the
    original body, never from this transform.
    """
    def repl(m):
        return " " + " ".join(_cjk_bigrams(m.group(0))) + " "
    return _CJK_RUN_RE.sub(repl, text)


def _cjk_query_expr(q):
    """Build the shadow-index MATCH expression for a CJK-bearing query.

    Each subtoken becomes a phrase of its own shadow transform, giving exact
    substring semantics inside CJK runs (consecutive bigrams) with bm25
    ranking. A trailing single CJK char becomes a prefix phrase so it can
    meet indexed bigrams. Tokens are OR-combined like bucket 0.
    """
    terms, seen = [], set()
    for tok in q.split():
        for p in _KO_PARTICLES:      # same particle stemming as bucket 0
            if tok.endswith(p) and len(tok) - len(p) >= 2:
                tok = tok[: len(tok) - len(p)]
                break
        for part in _SUBTOKEN_RE.findall(tok):
            toks = _cjk_shadow_text(part).split()
            if not toks:
                continue
            phrase = '"' + " ".join(t.replace('"', '""') for t in toks) + '"'
            last = toks[-1]
            if len(last) == 1 and _has_cjk(last):
                phrase += " *"       # FTS5 prefix phrase: extend the last token
            if phrase not in seen:
                seen.add(phrase)
                terms.append(phrase)
    return " OR ".join(terms)


def _normalize_capsule_list(value, *, limit=24, item_chars=160):
    """Return a bounded, order-preserving list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            value = decoded if isinstance(decoded, list) else [value]
        except (json.JSONDecodeError, TypeError):
            value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out, seen = [], set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        item = re.sub(r"[\x00-\x1f\x7f]", " ", raw).strip()[:item_chars]
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
        if len(out) >= limit:
            break
    return out


_ENTITY_PATTERNS = (
    re.compile(r"`([^`\n]{2,80})`"),                                  # backtick identifiers
    re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8})"),  # file paths
    re.compile(r"(?<![0-9a-zA-Z])([0-9a-f]{7,40})(?![0-9a-zA-Z])"),   # commit hashes
    re.compile(r"\b([A-Z]{1,3}-\d{1,4}|rt-[0-9a-f]{6,})\b"),          # D-40 / F-19 / rt-*
)


def _extract_entities(body, headline, *, limit=12):
    """Purely mechanical entity extraction. No semantic judgement (D-40)."""
    text = f"{headline or ''}\n{body or ''}"
    out, seen = [], set()
    for pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            item = match.group(1).strip()
            key = item.casefold()
            if not item or key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= limit:
                return out
    return out


def _merge_entities(base, extracted):
    """Append-only merge: base order and values are preserved verbatim."""
    # Mechanical extraction must never mint or overwrite the reserved pointer
    # namespace. Explicit caller-supplied entities remain untouched here.
    extracted = [item for item in (extracted or [])
                 if not (isinstance(item, str) and item.startswith("apid:"))]
    return _normalize_capsule_list(list(base or []) + list(extracted))


class ApidCapacityError(ValueError):
    """Closed, user-actionable pointer capacity failure."""


_APID_SLOTS = frozenset(("root", "artifact", "cycle", "campaign", "shared-reference", "route"))
_APID_TARGET_SLOTS = _APID_SLOTS - {"root"}
_APID_CAPACITY_REASONS = frozenset((
    "apid-token-too-long", "apid-target-capacity", "apid-root-capacity",
    "apid-entity-capacity", "apid-in-artifact-refs", "apid-exact-write-mismatch",
))


def _normalize_explicit_entity_candidate(value, *, item_chars=160):
    """Normalize a complete explicit entity candidate without truncating it."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            value = decoded if isinstance(decoded, list) else [value]
        except (json.JSONDecodeError, TypeError):
            value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out, seen = [], set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        item = re.sub(r"[\x00-\x1f\x7f]", " ", raw).strip()[:item_chars]
        key = item.casefold()
        if item and key not in seen:
            seen.add(key); out.append(item)
    return out


def _validate_apid_state(entities, artifact_refs):
    """Validate before a capsule list can silently truncate or discard values."""
    entities = list(entities or [])
    refs = list(artifact_refs or [])
    if any(isinstance(item, str) and item.startswith("apid:") for item in refs):
        raise ApidCapacityError("apid-in-artifact-refs")
    parsed = []
    for item in entities:
        if not isinstance(item, str) or not item.startswith("apid:"):
            continue
        if len(item) > 160:
            raise ApidCapacityError("apid-token-too-long")
        pieces = item.split(":", 2)
        if len(pieces) != 3 or pieces[1] not in _APID_SLOTS or not pieces[2]:
            continue
        parsed.append(item)
    roots = [item for item in parsed if item.split(":", 2)[1] == "root"]
    targets = [item for item in parsed if item.split(":", 2)[1] in _APID_TARGET_SLOTS]
    if len(roots) > 1:
        raise ApidCapacityError("apid-root-capacity")
    if len(targets) > 4:
        raise ApidCapacityError("apid-target-capacity")
    # Capacity is an invariant of the complete explicit candidate.  Do not
    # inspect the bounded capsule representation: it intentionally truncates
    # at 24 and would turn a 25-item request into a successful 24-item write.
    if len(_normalize_explicit_entity_candidate(entities)) > 24:
        raise ApidCapacityError("apid-entity-capacity")


def _apid_set(value):
    return {item for item in _normalize_capsule_list(value)
            if isinstance(item, str) and item.startswith("apid:")}


def _default_headline(body):
    # Truncating after the strip can leave the cut edge on a space, so a second
    # pass over the same value would shorten it again. Normalization has to be
    # a fixpoint: a re-derived headline must equal the stored one, or a v2
    # operation's post-state stops matching the row it describes.
    return re.sub(r"[\x00-\x1f\x7f]", " ",
                  _first_line(body or "")).strip()[:240].strip()


def _normalize_headline(value, body):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", value or "").strip()[:240].strip()
    return text or _default_headline(body)


def _migrate_v2(con):
    """Backfill strength and last_accessed columns idempotently."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(records)")}
    if "strength" not in cols:
        con.execute("ALTER TABLE records ADD COLUMN strength INTEGER DEFAULT 1")
    if "last_accessed" not in cols:
        con.execute("ALTER TABLE records ADD COLUMN last_accessed TEXT")
    con.execute("UPDATE records SET strength=1 WHERE strength IS NULL")
    con.execute("UPDATE records SET last_accessed=COALESCE(updated,created) "
                "WHERE last_accessed IS NULL")


def _migrate_v3_apply(con, plan):
    """Apply a pure-SQL cwd_origin remap inside ``BEGIN IMMEDIATE``."""
    if not plan:               # plan may be None when cur>=3 (v3 already applied)
        return
    total = 0
    for old, new in plan["remap"].items():
        if new != old:
            total += con.execute(
                "UPDATE records SET cwd_origin=? WHERE cwd_origin=?",
                (new, old)).rowcount
    sys.stderr.write(f"[migrate v3] applied: remapped {total} records\n")


def _migrate_v4(con):
    """Add and idempotently backfill the injection_flag column."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(records)")}
    if "injection_flag" not in cols:
        con.execute("ALTER TABLE records ADD COLUMN injection_flag INTEGER DEFAULT 0")
    # Set the flag on matching bodies that are currently null or zero.
    for rid, body in con.execute(
            "SELECT id, body FROM records WHERE injection_flag IS NULL OR injection_flag=0"):
        if INJECTION_PAT.search(body or ""):
            con.execute("UPDATE records SET injection_flag=1 WHERE id=?", (rid,))
    # Normalize remaining null flags to zero.
    con.execute("UPDATE records SET injection_flag=0 WHERE injection_flag IS NULL")


def _pending_backfill(rtype, body):
    """Old records/dumps have no delivery state; fail-safe only explicit handoff shapes."""
    return rtype in ("hint", "handoff") or bool(re.match(r"^\s*HANDOFF\b", body or "", re.I))


def _migrate_v5(con):
    """Add delivery state and protect live legacy handoffs before any curator runs."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(records)")}
    if "delivery_state" not in cols:
        con.execute("ALTER TABLE records ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'ordinary'")
    for rid, rtype, body, state in con.execute(
            "SELECT id, type, body, delivery_state FROM records"):
        normalized = state if state in DELIVERY_STATES else "ordinary"
        if normalized == "ordinary" and _pending_backfill(rtype, body):
            normalized = "pending"
        if normalized != state:
            con.execute("UPDATE records SET delivery_state=? WHERE id=?", (normalized, rid))
    con.execute("UPDATE records SET expires=NULL WHERE delivery_state='pending'")


def _migrate_v6_apply(con, plan):
    """Apply the v6 cwd_origin remap as pure-SQL UPDATEs inside the lock.

    UPDATE of cwd_origin values only; no row is ever deleted.
    """
    if not plan:               # plan may be None when cur>=6 (already applied)
        return
    total = 0
    for old, new in plan["remap"].items():
        if new != old:
            total += con.execute(
                "UPDATE records SET cwd_origin=? WHERE cwd_origin=?",
                (new, old)).rowcount
    sys.stderr.write(f"[migrate v6] applied: remapped {total} records\n")


def _migrate_v8(con, installation_fingerprint):
    """Add local protocol-v2 ledgers and one active replica identity."""
    sync_v2.ensure_sync_schema(con)
    sync_v2.ensure_replica_identity(
        con, installation_fingerprint=installation_fingerprint
    )


def _migrate_v9(con, installation_fingerprint):
    """Add v28 migration receipts/fence storage without rewriting v2 state."""
    sync_v2.ensure_sync_schema(con)
    sync_v2.ensure_replica_identity(
        con, installation_fingerprint=installation_fingerprint
    )


def _migrate_v10(con, installation_fingerprint):
    """Add bounded cutover failure/status evidence without rewriting state."""
    sync_v2.ensure_sync_schema(con)
    sync_v2.ensure_replica_identity(
        con, installation_fingerprint=installation_fingerprint
    )


# ---------- DB row and metadata conversion ----------
def _row_to_meta(row):
    """Decode a SQLite row into ``(metadata, body)``, including tags and links."""
    d = dict(zip(RECORD_COLS, row))
    body = d.pop("body")
    # Repeated metadata fields are always lists.
    for k in ("tags", "links", *CAPSULE_LIST_FIELDS):
        v = d.get(k)
        if v is None:
            d[k] = []
        else:
            try:
                decoded = json.loads(v)
                d[k] = decoded if isinstance(decoded, list) else []
            except (json.JSONDecodeError, TypeError):
                d[k] = []
    d["status"] = d.get("status") if d.get("status") in RECORD_STATUSES else "active"
    d["canonical_id"] = d.get("canonical_id") or d.get("id")
    d["capsule_version"] = d.get("capsule_version") or 1
    return d, body


def _meta_to_params(meta, body):
    """Encode metadata and body as the canonical INSERT tuple."""
    tags = meta.get("tags") or []
    links = meta.get("links") or []
    capsule_lists = {
        key: _normalize_capsule_list(meta.get(key)) for key in CAPSULE_LIST_FIELDS
    }
    delivery_state = meta.get("delivery_state")
    if delivery_state not in DELIVERY_STATES:
        delivery_state = "pending" if _pending_backfill(meta.get("type"), body) else "ordinary"
    # A caller-supplied zero is not evidence of a clean body: the column
    # backfill and the protocol post-state check both recompute it, and a row
    # stored as zero for a guarded body is rejected at v2 seed construction.
    injection_flag = meta.get("injection_flag")
    if not injection_flag:
        injection_flag = 1 if INJECTION_PAT.search(body or "") else 0
    expires = None if delivery_state == "pending" else meta.get("expires")
    return (
        meta["id"],
        meta["tier"],
        meta["scope"],
        meta["type"],
        meta.get("cwd_origin"),    # None → SQL NULL
        meta.get("created"),
        meta.get("updated"),
        expires,
        meta.get("source"),        # None → SQL NULL
        json.dumps(tags, ensure_ascii=False),
        json.dumps(links, ensure_ascii=False),
        body,
        meta.get("strength", 1) or 1,    # None/0 → 1 default
        meta.get("last_accessed"),       # None → SQL NULL (back-filled by migration/import)
        injection_flag or 0,
        delivery_state,
        _normalize_headline(meta.get("headline"), body),
        json.dumps(capsule_lists["aliases"], ensure_ascii=False),
        json.dumps(capsule_lists["entities"], ensure_ascii=False),
        json.dumps(capsule_lists["topics"], ensure_ascii=False),
        json.dumps(capsule_lists["artifact_refs"], ensure_ascii=False),
        meta.get("status") if meta.get("status") in RECORD_STATUSES else "active",
        meta.get("canonical_id") or meta["id"],
        meta.get("superseded_by"),
        int(meta.get("capsule_version") or 1),
    )


def _canonical_record_state(state):
    """Return one wire-canonical complete record snapshot."""
    normalized = dict(state)
    for key in ("tags", "links", *CAPSULE_LIST_FIELDS):
        normalized[key] = sorted(
            set(normalized.get(key) or []),
            key=protocol_v2.canonical_bytes,
        )
    return normalized


def _record_state(con, rid):
    """Return one complete RECORD_COLS post-state as protocol JSON data."""
    row = con.execute(
        f"SELECT {', '.join(RECORD_COLS)} FROM records WHERE id=?", (rid,)
    ).fetchone()
    if row is None:
        return None
    meta, body = _row_to_meta(row)
    return _canonical_record_state({**meta, "body": body})


def _state_namespace(state):
    if not state:
        return None
    return "global" if state.get("scope") == "global" else state.get("cwd_origin")


def _lineage_access_times(con, record_ids):
    """Return the ``last_accessed`` each record's own v2 lineage already states.

    Reading a record touches its local access date but authors no operation, so
    the row and the folded state legitimately disagree on that one field. Every
    other consumer knows this — the coverage check drops `last_accessed` before
    comparing — but a destructive operation used to digest the live row, which
    made its prior-state evidence unmatchable the moment anyone had read the
    record. Carrying the lineage's own value forward keeps the evidence exact
    while leaving access recency server-local.
    """
    resolved = {}
    for rid in set(record_ids):
        heads = [str(row[0]) for row in con.execute(
            "SELECT op_id FROM sync_frontier WHERE record_id=?", (rid,))]
        # Concurrent heads have no single prior state to inherit from; leave
        # those to the ordinary conflict path rather than picking a winner.
        if len(heads) != 1:
            continue
        row = con.execute(
            "SELECT payload_bytes FROM sync_objects WHERE op_id=?", (heads[0],)
        ).fetchone()
        if row is None:
            continue
        try:
            payload = protocol_v2.canonical_loads(bytes(row[0]))
        except (ValueError, TypeError):
            continue
        for mutation in payload.get("mutations", ()):
            state = mutation.get("post_state")
            if str(mutation.get("record_id")) == rid and isinstance(state, dict) \
                    and state.get("last_accessed") is not None:
                resolved[rid] = state["last_accessed"]
    return resolved


def _with_lineage_access(state, rid, lineage):
    """Overlay one record state with the access date its lineage carries."""
    if not isinstance(state, dict) or rid not in lineage:
        return state
    return {**state, "last_accessed": lineage[rid]}


def _logical_project_namespace(con, serving_project):
    """Derive the stable sync identity from the existing immutable rename ops."""
    logical, identities = set(), []
    for row in con.execute("SELECT o.project_key,o.payload_bytes,o.op_id FROM sync_objects o "
                           "JOIN sync_applied a ON a.op_id=o.op_id "
                           "WHERE o.kind='rekey' AND a.result IN ('local','folded')"):
        payload = protocol_v2.canonical_loads(bytes(row[1]))
        if payload["provenance"]["source"] == serving_project:
            logical.add(row[0])
            identities.append(row[2])
    if len(logical) > 1:
        raise sync_v2.SyncInvariantError("serving project has conflicting repository identities")
    return next(iter(logical), serving_project), sorted(identities)


# ---------- write gate · dedup ----------
def quality_ok(body):
    b = body.strip()
    if len(b) < 15:
        return False, "too short (trivial and easy to rediscover)"
    if re.fullmatch(r"[\s\W_]+", b):
        return False, "no content"
    return True, ""


def sanitize(body):
    flags = []
    if INJECTION_PAT.search(body):
        flags.append("injection-pattern")
    masked = SECRET_PAT.sub(lambda m: m.group(0)[:4] + "***REDACTED***", body)
    if masked != body:
        flags.append("secret-masked")
    return masked, flags


def find_by_source(tier, scope, rtype, source, cwd_origin, con):
    """source-keyed lookup. Project records are namespaced by cwd_origin."""
    if not source:
        return None
    where = "tier=? AND scope=? AND type=? AND source=? AND status='active'"
    params = [tier, scope, rtype, source]
    if scope == "project":
        where += " AND cwd_origin=?"
        params.append(cwd_origin)
    row = con.execute(
        f"SELECT id FROM records WHERE {where} ORDER BY rowid DESC LIMIT 1",
        params).fetchone()
    return row[0] if row else None
SOURCE_HISTORY_MAX_BYTES = 1024 * 1024


def _source_history_max_bytes():
    raw = os.environ.get("MEM_SOURCE_HISTORY_MAX_BYTES", "")
    try:
        value = int(raw)
    except ValueError:
        return SOURCE_HISTORY_MAX_BYTES
    return value if value > 0 else SOURCE_HISTORY_MAX_BYTES


SOURCE_HISTORY_LOCK_WAIT_SECONDS = 2.0


# ---------- recall ----------

# Korean particles for suffix stripping, longest first for greedy matching.
_KO_PARTICLES = ("에서", "으로", "한테", "부터", "까지", "은", "는", "이", "가",
                 "을", "를", "에", "와", "과", "도", "만", "의", "로", "께")

# Word-like runs include alphanumeric and CJK ranges. Other punctuation splits
# subtokens, while each maximal CJK run stays intact.
_SUBTOKEN_RE = re.compile(r"[0-9A-Za-z　-鿿가-힯]+")


def _tokenize_query(q: str) -> list:
    """Split a natural-language query into escaped FTS OR-MATCH tokens.

    Preserve Korean particle stripping and CJK runs, split punctuation-delimited
    identifiers, retain multi-part originals for ranking, and leave trigram
    substring queries untokenized.
    """
    tokens = []
    seen = set()

    def _emit(term):
        escaped = '"' + term.replace('"', '""') + '"'
        if escaped not in seen:
            seen.add(escaped)
            tokens.append(escaped)

    for tok in q.split():
        # Strip Korean particles only when at least two stem characters remain.
        for p in _KO_PARTICLES:
            if tok.endswith(p) and len(tok) - len(p) >= 2:
                tok = tok[: len(tok) - len(p)]
                break
        if not tok:
            continue
        # Split internal punctuation while preserving CJK runs.
        parts = _SUBTOKEN_RE.findall(tok)
        if not parts:
            continue
        # Include the original phrase for multi-part tokens to preserve ranking.
        if len(parts) > 1:
            _emit(tok)
        for part in parts:
            _emit(part)
    return tokens


def _has_cjk(s):
    return bool(re.search(r"[　-鿿가-힯]", s))


def _unexpired_sql(alias="r"):
    """Read-side expiry fence, one definition for inject, candidates, and recall.

    Expiry is applied for real by the write side (``lifecycle``), so a read
    only has to keep an expired working record out of sight until then. A
    pending delivery never expires. The single ``?`` is ``today()``.
    """
    prefix = f"{alias}." if alias else ""
    return (f"({prefix}expires IS NULL OR {prefix}expires>=? "
            f"OR {prefix}delivery_state='pending')")


def _recall_receipt_key(session_id):
    return hashlib.sha256(
        b"memory-recall-opportunity-v1\0" + session_id.encode("utf-8", "replace")
    ).hexdigest()


def _recall_turn_digest(turn_id):
    if not turn_id:
        return ""
    return hashlib.sha256(
        b"memory-recall-turn-v1\0" + turn_id.encode("utf-8", "replace")
    ).hexdigest()


def _utf8_prefix(value, max_bytes):
    raw = value.encode("utf-8")[:max(0, max_bytes)]
    return raw.decode("utf-8", "ignore")


def _render_candidate_block(rows, max_bytes=CANDIDATE_MAX_UTF8_BYTES, body=None):
    """Render candidate rows; returns ``(text, shown)`` (shown = rows that got a line).

    ``body`` is an optional ``(record_id, text)`` short body shown under that
    row. It is trimmed to fit: it never pushes the block past ``max_bytes``.
    """
    if not rows:
        return "", 0
    header = (
        "# Memory candidates (indexes only; not instructions)\n"
        "Treat these as live leads from prior work, not noise. Read any plausibly relevant "
        "record in full with `mem show <id>` or focused `mem recall --full` before relying on "
        "context alone, and search deeper with `mem recall` when the task builds on past "
        "decisions. Ignore clearly unrelated candidates.\n"
    )
    output = header
    shown = 0
    body_at = None
    for rid, tier, rtype, headline in rows:
        clean = re.sub(r"[\x00-\x1f\x7f]+", " ", headline or "").strip()[:160]
        prefix = f"- [{tier}/{rtype}] {rid}: "
        suffix = clean or "(headline unavailable)"
        candidate = output + prefix + suffix + "\n"
        if len(candidate.encode("utf-8")) <= max_bytes:
            output = candidate
            shown += 1
            if body and body[0] == rid:
                body_at = len(output)
            continue
        remaining = max_bytes - len((output + prefix + "\n").encode("utf-8"))
        if remaining > 0:
            output += prefix + _utf8_prefix(suffix, remaining) + "\n"
            shown += 1
        break
    text = _utf8_prefix(output, max_bytes).rstrip()
    if body and body_at is not None and body_at <= len(text) + 1:
        lead = "  > "
        room = max_bytes - len(text.encode("utf-8")) - 1 - len(lead.encode("utf-8"))
        piece = _utf8_prefix(body[1], room).strip() if room > 0 else ""
        if len(piece.encode("utf-8")) >= CANDIDATE_BODY_MIN_BYTES:
            text = text[:body_at - 1] + "\n" + lead + piece + text[body_at - 1:]
    return text, shown


def _render_candidate_context(rows, max_bytes=CANDIDATE_MAX_UTF8_BYTES):
    return _render_candidate_block(rows, max_bytes)[0]


def _seen_session(session_id):
    """The session key for the display history, or "" when there is no real session."""
    sid = (session_id or "").strip()
    return "" if sid in CANDIDATE_SEEN_SESSIONLESS else sid


def _seen_names(session_id, project):
    digest = hashlib.sha256(
        b"memory-candidate-seen-v1\0" + session_id.encode("utf-8", "replace")).hexdigest()
    proj = hashlib.sha256(project.encode("utf-8", "replace")).hexdigest()[:12]
    return digest, f"{digest}.{proj}"


def _hook_stdin_payload(timeout=CANDIDATE_HOOK_STDIN_SECONDS, limit=65536):
    """The hook JSON on stdin, read without ever waiting for it: {} when absent."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        fd = sys.stdin.fileno()
    except (OSError, ValueError, AttributeError):
        return {}
    deadline = time.monotonic() + timeout
    chunks, total = [], 0
    while total < limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {}
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                return {}
            data = os.read(fd, 65536)
        except (OSError, ValueError):
            return {}
        if not data:
            break
        chunks.append(data)
        total += len(data)
    try:
        value = json.loads(b"".join(chunks).decode("utf-8", "replace"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _candidate_dedup_enabled():
    """Only the replay tool turns this off (MEM_CANDIDATE_DEDUP=0), to measure the old behaviour."""
    return os.environ.get("MEM_CANDIDATE_DEDUP", "").strip().lower() not in (
        "0", "off", "false", "no")


def _candidate_body_enabled():
    return os.environ.get("MEM_CANDIDATE_BODY", "").strip().lower() in (
        "1", "on", "true", "yes")


def _candidate_body_text(body):
    return re.sub(r"[\x00-\x1f\x7f\s]+", " ", body or "").strip()[:CANDIDATE_BODY_MAX_CHARS]


def _candidate_defer_requested():
    """True when the caller renders this probe into its own final output.

    Only a bridge that captures ``--hook`` output and emits later (the Codex
    prompt lifecycle, the OpenCode plugin) sets ``MEM_CANDIDATE_DEFER_COMMIT``;
    every standalone caller keeps today's immediate receipt. Deferring is never
    silent: the pending receipt rides in the same JSON object as the context.
    """
    return str(os.environ.get("MEM_CANDIDATE_DEFER_COMMIT", "")).strip().lower() in {
        "1", "on", "true", "yes"}


BOOTSTRAP_MAX_RESULTS = 6
BOOTSTRAP_MAX_UTF8_BYTES = 8192
BOOTSTRAP_TYPE_FENCE = (
    "(r.type IN ('feedback','decision','convention','preference','user-correction') "
    "OR r.type LIKE '%convention%' OR r.type LIKE '%preference%')"
)


def _write_actor(default="manual"):
    """Resolve the deterministic write actor from environment and caller default."""
    explicit = os.environ.get("MEM_ACTOR")
    if explicit in WRITE_ACTORS:
        return explicit
    if os.environ.get("MEM_DISTILL"):
        return "distiller"
    return default if default in WRITE_ACTORS else "manual"


def _user_text(content):
    """Extract user text from string/list content, excluding tool and image blocks."""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
    return "\n".join(p for p in parts if p)


def _assistant_text(content):
    """Extract assistant text and tool labels while excluding thinking blocks."""
    parts = []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                parts.append(b.get("text", ""))
            elif bt == "tool_use":
                parts.append(f"[tool:{b.get('name', '?')}]")
            # Exclude thinking blocks.
    return "\n".join(p for p in parts if p)


def _content_text(content):
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in ("output_text", "input_text", "text"):
                    parts.append(item.get("text", ""))
    return "\n".join(p for p in parts if p)


def _opencode_first_str(d, *keys):
    for key in keys:
        value = d.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _opencode_role(d):
    role = _opencode_first_str(d, "role", "author")
    if role in ("user", "assistant", "system"):
        return role
    # OpenCode 1.x places role metadata under info and content under parts.
    for key in ("info", "message", "session_message", "data"):
        value = d.get(key)
        if isinstance(value, dict):
            role = _opencode_role(value)
            if role:
                return role
    return None


def _opencode_tool_name(d):
    typ = str(d.get("type") or d.get("kind") or d.get("partType") or "").lower()
    name = _opencode_first_str(d, "name", "tool", "toolName", "tool_name")
    tool = d.get("tool")
    if isinstance(tool, dict):
        name = name or _opencode_first_str(tool, "name", "id")
    if name and ("tool" in typ or "call" in typ or "execute" in typ):
        return name
    return None


def _opencode_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_opencode_text(item) for item in value]
        return "\n".join(p for p in parts if p)
    if not isinstance(value, dict):
        return ""
    if _opencode_tool_name(value):
        return ""
    # Exclude internal reasoning and step markers from the delta.
    typ = str(value.get("type") or value.get("kind") or value.get("partType") or "").lower()
    if typ in ("reasoning", "step-start", "step-finish", "snapshot", "patch"):
        return ""
    # Prefer leaf text keys, then descend into OpenCode 1.x parts.
    for key in ("text", "content", "message", "body", "value", "parts"):
        item = value.get(key)
        text = _opencode_text(item)
        if text:
            return text
    return ""


def _opencode_items(payload):
    if isinstance(payload, list):
        for item in payload:
            yield from _opencode_items(item)
        return
    if not isinstance(payload, dict):
        return
    typ = str(payload.get("type") or payload.get("kind") or "").lower()
    if _opencode_role(payload) or _opencode_tool_name(payload) or typ in ("message", "tool_call", "tool"):
        yield payload
        return
    for key in ("messages", "events", "transcript", "items", "entries", "parts", "data"):
        if key in payload:
            yield from _opencode_items(payload[key])


# ---------- shared aspect extraction for export_profile and inject ----------
def _derive_aspect(meta, body):
    """Extract an aspect name from source, body marker, or record ID."""
    src = meta.get("source") or ""
    if src.startswith("user-profile:"):
        stem = src[len("user-profile:"):]
        if stem:
            return stem
    # Body aspect marker.
    for line in body.splitlines():
        if line.startswith("aspect:"):
            val = line.split(":", 1)[1].strip()
            if val:
                return val
    return None  # Unresolvable.


# ---------- profile (read-only) ----------
def _profile_store_state(db):
    """``(inode, size, mtime_ns)`` of the main file, or ``None`` once a ``-wal`` exists."""
    if Path(f"{db}-wal").exists():
        return None
    info = db.stat()
    return (info.st_ino, info.st_size, info.st_mtime_ns)


MANUAL_MEMO_HEADING = "## 사용자 수동 메모"


def _append_manual_memo(body, text):
    """Append one item under the exact `## 사용자 수동 메모` block.

    The heading is created at the end when absent. The rest of the body is
    byte-identical, so this is the only safe partial write to a profile.
    """
    item = text.strip()
    lines = body.splitlines()
    try:
        head = next(i for i, line in enumerate(lines) if line.strip() == MANUAL_MEMO_HEADING)
    except StopIteration:
        head = None
    if head is None:
        base = body.rstrip("\n")
        return f"{base}\n\n{MANUAL_MEMO_HEADING}\n- {item}\n" if base else f"{MANUAL_MEMO_HEADING}\n- {item}\n"
    insert = head + 1
    while insert < len(lines) and not lines[insert].strip().startswith("## "):
        insert += 1
    lines.insert(insert, f"- {item}")
    return "\n".join(lines) + "\n"


def _graveyard_prepare(con, rid, action="prune", canonical=None):
    """Prepare one compatibility graveyard line inside the DB transaction."""
    row = con.execute(
        f"SELECT {', '.join(RECORD_COLS)} FROM records WHERE id=?", (rid,)).fetchone()
    if row is None:
        return None
    rec = {}
    for k, v in zip(RECORD_COLS, row):
        if k in ("tags", "links", *CAPSULE_LIST_FIELDS):
            rec[k] = json.loads(v) if v else []
        else:
            rec[k] = v   # Preserve None as JSON null.
    rec["_deleted_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    rec["_action"] = action
    rec["_canonical"] = canonical
    return json.dumps(rec, sort_keys=True, ensure_ascii=False)


def _v2_restore_candidate(con, rid):
    """Reconstruct an effective tombstone's exact prior state from its DAG."""
    envelopes = [
        {"op_id": op_id,
         "payload": protocol_v2.canonical_loads(bytes(payload))}
        for op_id, payload in con.execute(
            "SELECT op_id,payload_bytes FROM sync_objects ORDER BY op_id")
    ]
    if not envelopes:
        return None
    folded = protocol_v2.fold_operations(envelopes)
    if folded.classification.hard_failures:
        raise sync_v2.SyncInvariantError(
            "cannot restore through a protocol hard failure"
        )
    tombstone_id = folded.tombstones.get(rid)
    if tombstone_id is None:
        return None
    tombstone_op = folded.classification.operations[tombstone_id]
    mutation = tombstone_op.mutation_for(rid)
    frontier = next(
        (item["heads"] for item in tombstone_op.payload["frontiers"]
         if item["record_id"] == rid),
        None,
    )
    if mutation is None or "tombstone" not in mutation or frontier is None:
        raise sync_v2.SyncInvariantError(
            "effective tombstone lacks restore evidence"
        )
    if not frontier:
        # A pre-v2 row can be deleted after the schema migration but before an
        # operator seed. Its compatibility graveyard is the only complete
        # prior state; retain the causal tombstone ID without inventing a
        # predecessor operation. The remote gate prevents this unseeded object
        # from being exchanged.
        return None, tombstone_id, protocol_v2.serving_project(tombstone_op.payload)
    closure = set()
    stack = list(frontier)
    while stack:
        op_id = stack.pop()
        if op_id in closure:
            continue
        operation = folded.classification.operations.get(op_id)
        if operation is None:
            raise sync_v2.SyncInvariantError(
                "restore frontier is missing a causal operation"
            )
        closure.add(op_id)
        stack.extend(operation.parents)
    prior = protocol_v2.fold_operations(
        [folded.classification.operations[op_id] for op_id in sorted(closure)]
    )
    if (prior.classification.hard_failures or prior.deferred or prior.quarantined
            or rid in prior.conflicts or rid not in prior.records):
        raise sync_v2.SyncInvariantError(
            "restore frontier does not materialize one complete prior state"
        )
    state = dict(prior.records[rid])
    expected = mutation["tombstone"]["prior_digest"]
    actual = hashlib.sha256(protocol_v2.canonical_bytes(state)).hexdigest()
    if actual != expected:
        raise sync_v2.SyncInvariantError(
            "restore prior state does not match tombstone evidence"
        )
    return state, tombstone_id, protocol_v2.serving_project(tombstone_op.payload)


def _emit_reattribute_result(result, json_output):
    state = result.get("state")
    if json_output:
        output = dict(result)
        record_ids = output.get("record_ids")
        if (isinstance(record_ids, list)
                and (len(record_ids) > protocol_v2.MAX_DIAGNOSTIC_IDS
                     or sum(len(str(value).encode("utf-8")) for value in record_ids) > 8192)):
            output.setdefault("record_ids_digest", _repository_rekey_ids_digest(record_ids))
            output.pop("record_ids", None)
        for field in ("operation_ids", "inverse_operation_ids"):
            values = output.get(field)
            if isinstance(values, list) and len(values) > protocol_v2.MAX_DIAGNOSTIC_IDS:
                output[field + "_count"] = len(values)
                output[field + "_digest"] = _repository_rekey_ids_digest(values)
                output[field] = values[:protocol_v2.MAX_DIAGNOSTIC_IDS]
        encoded = json.dumps(output, sort_keys=True, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 64 * 1024:
            output.pop("record_ids", None)
            encoded = json.dumps(output, sort_keys=True, ensure_ascii=False)
        print(encoded)
    elif state == "applied" and "migration_id" in result:
        pass
    elif state == "reattributed":
        print(f"[reattribute] {result.get('record_id')} "
              f"{result.get('from')}→{result.get('to')}")
    elif state == "applied":
        print(f"[reattribute] {result.get('record_count', 1)} record(s) moved")
    elif state == "already-applied":
        print(f"[reattribute] migration {result.get('migration_id')} already applied")
    elif state == "undone":
        print(f"[reattribute] migration {result.get('migration_id')} undone")
    elif state == "already-undone":
        print(f"[reattribute] migration {result.get('migration_id')} already undone")
    else:
        sys.stderr.write(f"[reattribute] {state or 'refused'}: "
                         f"{result.get('reason') or 'not eligible'}\n")
    return state in ("applied", "reattributed", "already-applied",
                     "undone", "already-undone")


def _github_repository(key):
    """Read repository identity through the user's existing gh authentication."""
    match = re.fullmatch(r"git:github\.com/([^/]+)/([^/]+)", key or "")
    if not match or not shutil.which("gh"):
        return None
    try:
        result = subprocess.run(
            ["gh", "api", "repos/" + "/".join(match.groups()),
             "--jq", "{id,full_name}"], capture_output=True, text=True, timeout=5)
        value = json.loads(result.stdout) if result.returncode == 0 else None
        if (isinstance(value, dict) and type(value.get("id")) is int
                and isinstance(value.get("full_name"), str)):
            return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def _repository_rename_evidence(old, current, current_repository=None):
    if old == current:
        return None
    before = _github_repository(old)
    after = current_repository or _github_repository(current)
    if (before and after and before["id"] == after["id"]
            and before["full_name"] == after["full_name"]
            and "git:github.com/" + after["full_name"] == current):
        return "github-repository-id:" + str(after["id"])
    return None


_REPOSITORY_REKEY_FORMAT = "repository-rekey-v1"
_REPOSITORY_REKEY_EVIDENCE = re.compile(r"^github-repository-id:[1-9][0-9]*$")


def _repository_rekey_reason(metadata):
    return json.dumps(metadata, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _repository_rekey_metadata(payload):
    """Return only canonical, structurally valid migration metadata."""
    try:
        reason = payload["provenance"]["reason"]
        metadata = json.loads(reason)
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(metadata, dict) or metadata.get("format") != _REPOSITORY_REKEY_FORMAT:
        return None
    phase = metadata.get("phase")
    required = {"format", "phase", "migration_id", "old", "current", "evidence",
                "batch_index", "batch_count", "backup_sha256"}
    if phase == "undo":
        required.add("apply_op_ids")
    if set(metadata) != required:
        return None
    if reason != _repository_rekey_reason(metadata):
        return None
    if (phase not in ("apply", "undo")
            or not isinstance(metadata.get("migration_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["migration_id"])
            or not isinstance(metadata.get("old"), str)
            or not metadata["old"].startswith("git:github.com/")
            or not isinstance(metadata.get("current"), str)
            or not metadata["current"].startswith("git:github.com/")
            or metadata["old"] == metadata["current"]
            or not isinstance(metadata.get("evidence"), str)
            or not _REPOSITORY_REKEY_EVIDENCE.fullmatch(metadata["evidence"])
            or not isinstance(metadata.get("backup_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["backup_sha256"])):
        return None
    index, count = metadata.get("batch_index"), metadata.get("batch_count")
    if (type(index) is not int or type(count) is not int
            or count < 1 or count > 1000000 or not 0 <= index < count):
        return None
    if phase == "undo":
        targets = metadata.get("apply_op_ids")
        if (not isinstance(targets, list) or len(targets) != 1
                or not isinstance(targets[0], str)
                or not re.fullmatch(r"[0-9a-f]{64}", targets[0])):
            return None
    return metadata


def _repository_rekey_manifest_id(old, current, evidence, records):
    """Hash sorted IDs and their exact pre-migration frontier sets."""
    records = sorted((str(rid), tuple(sorted(heads))) for rid, heads in records)
    digest = hashlib.sha256()
    digest.update(protocol_v2.canonical_bytes({
        "format": "repository-rekey-manifest-v1", "old": old,
        "current": current, "evidence": evidence, "record_count": len(records),
    }))
    for rid, heads in records:
        digest.update(protocol_v2.canonical_bytes({"record_id": rid, "heads": list(heads)}))
    return digest.hexdigest()


def _repository_rekey_ids_digest(record_ids):
    digest = hashlib.sha256()
    values = sorted(set(record_ids))
    digest.update(protocol_v2.canonical_bytes({"record_count": len(values)}))
    for rid in values:
        digest.update(protocol_v2.canonical_bytes({"record_id": rid}))
    return digest.hexdigest()


def _repository_rekey_objects(con, migration_id=None, phase=None):
    rows = con.execute(
        "SELECT o.op_id,o.payload_bytes,o.kind,o.project_key,a.result FROM sync_objects o "
        "LEFT JOIN sync_applied a ON a.op_id=o.op_id WHERE o.kind='rekey' "
        "ORDER BY o.op_id").fetchall()
    objects = []
    for op_id, raw, stored_kind, stored_project, result in rows:
        try:
            payload = protocol_v2.canonical_loads(bytes(raw))
            rebuilt = protocol_v2.build_operation(payload)
            validated = protocol_v2.validate_operation(rebuilt)
        except Exception:
            continue
        if (rebuilt["op_id"] != op_id or payload.get("kind") != stored_kind
                or payload.get("project_key") != stored_project
                or getattr(validated, "supported", None) is not True):
            continue
        metadata = _repository_rekey_metadata(payload)
        if metadata is None:
            continue
        if migration_id is not None and metadata["migration_id"] != migration_id:
            continue
        if phase is not None and metadata["phase"] != phase:
            continue
        objects.append({"op_id": op_id, "payload": payload,
                        "result": result, "metadata": metadata})
    return objects


def _repository_frontier_heads(payload, record_id):
    for frontier in payload.get("frontiers", ()):
        if frontier.get("record_id") == record_id:
            return list(frontier.get("heads", ()))
    return None


def _repository_prior_state(con, record_id, head_id, cache=None):
    cache = cache if cache is not None else {}
    if head_id not in cache:
        row = con.execute(
            "SELECT o.payload_bytes,a.result FROM sync_objects o "
            "LEFT JOIN sync_applied a ON a.op_id=o.op_id WHERE o.op_id=?", (head_id,)
        ).fetchone()
        if row is None or row[1] not in ("local", "folded"):
            cache[head_id] = None
        else:
            try:
                payload = protocol_v2.canonical_loads(bytes(row[0]))
                envelope = protocol_v2.build_operation(payload)
                validated = protocol_v2.validate_operation(envelope)
            except Exception:
                cache[head_id] = None
            else:
                if (envelope["op_id"] != head_id
                        or getattr(validated, "supported", None) is not True):
                    cache[head_id] = None
                else:
                    cache[head_id] = {
                        mutation["record_id"]: dict(mutation["post_state"])
                        for mutation in payload.get("mutations", ())
                        if isinstance(mutation.get("post_state"), dict)
                    }
    states = cache.get(head_id)
    return dict(states[record_id]) if states and record_id in states else None


def _repository_state_is_rekey(prior, post, *, old, current):
    if not isinstance(prior, dict) or not isinstance(post, dict):
        return False
    if prior.get("scope") != "project" or prior.get("type") == "profile":
        return False
    if prior.get("cwd_origin") != old:
        return False
    expected = dict(prior)
    expected["cwd_origin"] = current
    return post == expected


def _validate_repository_apply_group(con, migration_id):
    objects = _repository_rekey_objects(con, migration_id, "apply")
    if not objects:
        return None
    first = objects[0]["metadata"]
    common = {key: first[key] for key in
              ("migration_id", "old", "current", "evidence", "batch_count", "phase",
               "backup_sha256")}
    batches = {}
    all_records = []
    all_ids = set()
    logical_project = None
    prior_cache = {}
    for obj in objects:
        metadata, payload = obj["metadata"], obj["payload"]
        if any(metadata[key] != value for key, value in common.items()):
            return None
        index = metadata["batch_index"]
        if index in batches or obj["result"] not in ("local", "folded"):
            return None
        if (payload.get("kind") != "rekey" or payload.get("schema_minor") != 1
                or protocol_v2.serving_project(payload) != common["current"]):
            return None
        if logical_project is None:
            logical_project = payload.get("project_key")
        elif payload.get("project_key") != logical_project:
            return None
        mutations = payload.get("mutations", ())
        mutation_ids = [item.get("record_id") for item in mutations]
        if (not mutation_ids or mutation_ids != sorted(mutation_ids)
                or len(mutation_ids) != len(set(mutation_ids))
                or len(mutation_ids) > protocol_v2.MAX_MUTATIONS
                or set(mutation_ids) & all_ids):
            return None
        frontiers = {}
        prior_states, post_states = {}, {}
        for mutation in mutations:
            rid = mutation["record_id"]
            heads = _repository_frontier_heads(payload, rid)
            if not heads or len(heads) != 1:
                return None
            prior = _repository_prior_state(con, rid, heads[0], prior_cache)
            post = mutation.get("post_state")
            if not _repository_state_is_rekey(
                    prior, post, old=common["old"], current=common["current"]):
                return None
            frontiers[rid] = heads
            prior_states[rid] = prior
            post_states[rid] = dict(post)
            all_records.append((rid, heads))
        all_ids.update(mutation_ids)
        batches[index] = {**obj, "record_ids": mutation_ids,
                          "frontiers": frontiers,
                          "prior_states": prior_states,
                          "post_states": post_states}
    if set(batches) != set(range(common["batch_count"])):
        return None
    if _repository_rekey_manifest_id(
            common["old"], common["current"], common["evidence"], all_records
    ) != migration_id:
        return None
    ordered = [batches[index] for index in range(common["batch_count"])]
    return {**common, "batches": ordered,
            "project_key": logical_project,
            "record_ids": sorted(all_ids),
            "record_ids_digest": _repository_rekey_ids_digest(all_ids),
            "operation_ids": [item["op_id"] for item in ordered]}


def _validate_repository_undo_group(con, apply_group):
    objects = _repository_rekey_objects(con, apply_group["migration_id"], "undo")
    if not objects:
        return None
    batches = {}
    backup_sha256 = None
    for obj in objects:
        metadata, payload = obj["metadata"], obj["payload"]
        index = metadata["batch_index"]
        if (metadata["batch_count"] != len(apply_group["batches"])
                or metadata["old"] != apply_group["old"]
                or metadata["current"] != apply_group["current"]
                or metadata["evidence"] != apply_group["evidence"]
                or index in batches or obj["result"] not in ("local", "folded")):
            return None
        if backup_sha256 is None:
            backup_sha256 = metadata["backup_sha256"]
        elif metadata["backup_sha256"] != backup_sha256:
            return None
        target = apply_group["batches"][index]
        if (metadata["apply_op_ids"] != [target["op_id"]]
                or payload.get("kind") != "rekey" or payload.get("schema_minor") != 1
                or protocol_v2.serving_project(payload) != apply_group["old"]
                or payload.get("project_key") != apply_group["project_key"]):
            return None
        mutations = payload.get("mutations", ())
        mutation_ids = [item.get("record_id") for item in mutations]
        if mutation_ids != target["record_ids"]:
            return None
        for mutation in mutations:
            rid = mutation["record_id"]
            if (_repository_frontier_heads(payload, rid) != [target["op_id"]]
                    or mutation.get("post_state") != target["prior_states"][rid]):
                return None
        batches[index] = {**obj, "record_ids": mutation_ids}
    if set(batches) != set(range(len(apply_group["batches"]))):
        return None
    ordered = [batches[index] for index in range(len(batches))]
    return {"batches": ordered, "operation_ids": [item["op_id"] for item in ordered],
            "record_ids": list(apply_group["record_ids"]),
            "backup_sha256": backup_sha256}


def _repository_operation_is_ancestor(con, ancestor, descendant):
    if ancestor == descendant:
        return True
    return con.execute(
        "WITH RECURSIVE ancestors(op_id) AS ("
        "SELECT parent_op_id FROM sync_parents WHERE op_id=? UNION "
        "SELECT p.parent_op_id FROM sync_parents p JOIN ancestors a ON p.op_id=a.op_id) "
        "SELECT 1 FROM ancestors WHERE op_id=? LIMIT 1", (descendant, ancestor)
    ).fetchone() is not None


def _repository_reapply_descends_undo(con, undo_group, apply_group):
    mutations = {}
    for batch in apply_group["batches"]:
        for mutation in batch["payload"]["mutations"]:
            mutations[mutation["record_id"]] = (batch, mutation)
    for undo_batch in undo_group["batches"]:
        for rid in undo_batch["record_ids"]:
            target = mutations.get(rid)
            if target is None:
                return False
            batch, _mutation = target
            heads = _repository_frontier_heads(batch["payload"], rid) or []
            if not any(_repository_operation_is_ancestor(con, undo_batch["op_id"], head)
                       for head in heads):
                return False
    return True


def _repository_auto_rekey_suppressed(con, old, current, evidence):
    objects = [item for item in _repository_rekey_objects(con)
               if (item["metadata"]["old"], item["metadata"]["current"],
                   item["metadata"]["evidence"]) == (old, current, evidence)]
    migration_ids = sorted({item["metadata"]["migration_id"] for item in objects})
    groups = []
    for migration_id in migration_ids:
        group = _validate_repository_apply_group(con, migration_id)
        if group is not None:
            groups.append(group)
    for group in groups:
        if (group["old"], group["current"], group["evidence"]) != (old, current, evidence):
            continue
        undo = _validate_repository_undo_group(con, group)
        if undo is None:
            continue
        reapplied = any(
            other["migration_id"] != group["migration_id"]
            and (other["old"], other["current"], other["evidence"])
                == (old, current, evidence)
            and _repository_reapply_descends_undo(con, undo, other)
            for other in groups
        )
        if not reapplied:
            return True
    return False


def _repository_rekey_batches(record_ids, states, frontiers):
    batches, current, parent_ids, estimated = [], [], set(), 2048
    for rid in sorted(record_ids):
        heads = frontiers[rid]
        if not heads or len(heads) > protocol_v2.MAX_FRONTIER:
            raise sync_v2.SyncInvariantError("repository rekey has an unresolved frontier")
        encoded = len(protocol_v2.canonical_bytes(states[rid]))
        record_estimate = encoded + len(rid.encode("utf-8")) + 512 + 66 * len(heads)
        next_parents = parent_ids | set(heads)
        if current and (len(current) >= protocol_v2.MAX_MUTATIONS
                        or len(next_parents) > protocol_v2.MAX_PARENTS
                        or estimated + record_estimate > protocol_v2.MAX_ENVELOPE_BYTES):
            batches.append(current)
            current, parent_ids, estimated = [], set(), 2048
            next_parents = set(heads)
        if (len(next_parents) > protocol_v2.MAX_PARENTS
                or estimated + record_estimate > protocol_v2.MAX_ENVELOPE_BYTES):
            raise sync_v2.SyncInvariantError(
                "one repository record exceeds the bounded v2 rekey envelope")
        current.append(rid)
        parent_ids = next_parents
        estimated += record_estimate
    if current:
        batches.append(current)
    return batches


def _repository_rename_evidence_status(old, current, current_repository=None):
    before = _github_repository(old)
    after = current_repository or _github_repository(current)
    if not before or not after:
        return None, "repository-evidence-unavailable"
    if before["id"] != after["id"]:
        return None, "different-repository"
    if (before["full_name"] != after["full_name"]
            or "git:github.com/" + after["full_name"] != current):
        return None, "canonical-name-unconfirmed"
    return "github-repository-id:" + str(after["id"]), None


def _repository_undo_refusal(migration_id, reason, apply_group=None):
    return {"state": "refused", "reason": reason, "migration_id": migration_id,
            "record_count": len(apply_group["record_ids"]) if apply_group else 0,
            "record_ids": apply_group["record_ids"] if apply_group else [],
            "inverse_operation_ids": []}


def _snap_label(body):
    """Sanitize snapshot labels so data cannot forge control boundaries."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", _first_line(body))[:120]


def _visible_conflict_projects(con, current):
    """A rename must not hide the sole conflicted record before it can move."""
    visible = set(sync_v2.project_aliases(con, current)) | {'global'}
    candidates = {row[0] for row in con.execute(
        "SELECT DISTINCT project_key FROM sync_conflicts WHERE resolved_by IS NULL")}
    owner = current.rsplit('/', 1)[0] + '/' if current.startswith('git:github.com/') else ''
    candidates = {key for key in candidates - visible if owner and key.startswith(owner)}
    repository = _github_repository(current) if candidates else None
    if repository:
        visible.update(key for key in candidates
                       if _repository_rename_evidence(key, current, repository))
    return tuple(sorted(visible))


# ---------- D-39: comprehensive read-only doctor ----------
def _doctor_check(results, name, status, message):
    results.append((name, status, message))


# ---------- inject ----------
def _first_line(body):
    for l in body.splitlines():
        s = l.strip()
        if s and not s.startswith("---") and not s.startswith("#"):
            return s
    return body.strip()[:160]


def _env_int(name, default, minimum=0):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return max(minimum, val)


def _append_inject_line(entries, line, record_id=None):
    entries.append((line, record_id))


def _inject_block(entries, max_chars):
    """Return capped markdown plus ids that remain visible in the final block."""
    lines = [line for line, _ in entries]
    block = "\n".join(lines)
    if max_chars <= 0 or len(block) <= max_chars:
        return block, [rid for _, rid in entries if rid]

    hint = entries[-1] if entries and entries[-1][0].startswith("> Detailed recall:") else (
        '> Detailed recall: `bash <agent-home>/tools/memory/recall.sh "<query>"`',
        None,
    )
    body_entries = entries[:-1] if entries and entries[-1] == hint else entries
    notice = ("… Some session-start memory was omitted; use recall for details.", None)
    suffix = [("", None), notice, hint]
    kept = []
    for entry in body_entries:
        candidate = kept + [entry] + suffix
        candidate_block = "\n".join(line for line, _ in candidate)
        if len(candidate_block) <= max_chars:
            kept.append(entry)
        else:
            break

    final_entries = kept + suffix
    final_block = "\n".join(line for line, _ in final_entries)
    if len(final_block) > max_chars:
        fallback = hint[0]
        if len(fallback) > max_chars:
            fallback = fallback[:max_chars]
        return fallback, []
    return final_block, [rid for _, rid in final_entries if rid]


# ---------- sync ----------
def _sync_envelopes(con):
    return [
        {"op_id": op_id,
         "payload": protocol_v2.canonical_loads(bytes(payload))}
        for op_id, payload in con.execute(
            "SELECT op_id,payload_bytes FROM sync_objects ORDER BY op_id")
    ]


def _ingest_remote_objects(con, snapshot):
    for op_id, item in sorted(snapshot.operations.items()):
        op = protocol_v2.validate_operation(item)
        payload = protocol_v2.canonical_bytes(op.payload)
        prior = con.execute(
            "SELECT payload_bytes FROM sync_objects WHERE op_id=?", (op_id,)
        ).fetchone()
        if prior:
            if bytes(prior[0]) != payload:
                raise sync_v2.SyncInvariantError(f"immutable operation collision: {op_id}")
            continue
        con.execute(
            "INSERT INTO sync_objects(op_id,replica_id,counter,project_key,kind,"
            "object_path,payload_bytes,classification) VALUES(?,?,?,?,?,?,?,?)",
            (op_id, op.payload["replica_id"], str(op.payload["counter"]),
             op.payload["project_key"], op.payload["kind"], op.path,
             sqlite3.Binary(payload), "remote"),
        )
        for ordinal, parent in enumerate(op.parents):
            con.execute(
                "INSERT INTO sync_parents(op_id,parent_op_id,parent_ordinal) VALUES(?,?,?)",
                (op_id, parent, ordinal),
            )


def _resolved_blocked_map(result):
    """Resolve blocked evidence from final single-head explicit decisions in O(V+E)."""
    return protocol_v2.resolved_blocked_by(result)


SYNC_SETTINGS_KEYS = {
    "enabled": "MEM_SYNC_REMOTE",
    "exchange_dir": "MEM_SYNC_DIR",
    "ref": "MEM_SYNC_REF",
    "remote_url": "MEM_SYNC_REMOTE_URL",
}


def _sync_settings_path():
    """User-owned cross-runtime exchange policy, outside any runtime's home."""
    return (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "hearting" / "memory-sync.json")


def _sync_settings():
    """Read the shared exchange policy; an unreadable file is simply absent.

    Only Claude has a settings file that can carry environment defaults, so a
    policy kept solely in one runtime's config would leave the other adapters
    syncing local-only. This file is the portable surface all three read.
    """
    path = _sync_settings_path()
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    resolved = {}
    for key, name in SYNC_SETTINGS_KEYS.items():
        value = data.get(key)
        if isinstance(value, bool):
            value = "1" if value else "0"
        if isinstance(value, str) and value.strip():
            resolved[name] = value.strip()
    return resolved


def _sync_environment():
    """Overlay the shared policy with the process environment, which wins."""
    return {**_sync_settings(), **os.environ}


def _repository_rekey_status(outcomes):
    summary = []
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        entries = outcome.get("migrations")
        if not isinstance(entries, list):
            entries = [outcome]
        item = {"project_key": outcome.get("project_key"),
                "state": outcome.get("state")}
        item["migrations"] = [
            {key: migration[key] for key in
             ("state", "reason", "migration_id", "from", "to", "evidence",
              "record_count", "record_ids_digest", "operation_ids")
             if key in migration}
            for migration in entries if isinstance(migration, dict)
        ]
        summary.append(item)
    return summary


class _SyncLockBusy(RuntimeError):
    pass


# ---------- background exchange (D-83) ----------
# One exchange, two triggers. A foreground command that committed a mutation
# (W) and a main-session read whose last successful receive is stale (R) both
# ask ``_schedule_exchange`` to start the same detached ``_exchange-worker``;
# the worker runs the existing ``_sync_locked`` safety path in routine mode.
# Nothing here waits for the exchange, and every failure here is swallowed: a
# write or read never fails or slows down because the exchange could not start.
EXCHANGE_STATE_FILE = ".exchange-state.json"
EXCHANGE_RUN_LOCK = ".exchange-run.lock"
EXCHANGE_SCHEDULE_LOCK = ".exchange-schedule.lock"
EXCHANGE_WINDOW_SECONDS = 20.0        # writes that arrive inside it share one run
EXCHANGE_READ_INTERVAL_SECONDS = 600.0
EXCHANGE_RECEIVE_RETRY_SECONDS = 120.0  # a dead remote is not retried per prompt
EXCHANGE_SPAWN_GRACE_SECONDS = 5.0    # a just-spawned worker has not taken its lock yet
EXCHANGE_MAX_RUNS = 2                 # one run, plus at most one follow-up
EXCHANGE_NOTICE_REPEAT_SECONDS = 6 * 60 * 60  # the same failure reason speaks once per this


def _exchange_mode():
    """``on`` (default), ``off`` (MEM_EXCHANGE_AUTO=0), or ``inline`` (tests)."""
    value = os.environ.get("MEM_EXCHANGE_AUTO", "").strip().lower()
    if value in ("0", "off", "false", "no"):
        return "off"
    if value == "inline":
        return "inline"
    return "on"


def _exchange_try_flock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _exchange_repair_intents(state):
    raw = state.get("repair_intents")
    if not isinstance(raw, dict):
        return {}
    return {key: generation for key, generation in raw.items()
            if (isinstance(key, str) and key and len(key) <= protocol_v2.MAX_IDENTIFIER_BYTES
                and not re.search(r"[\x00-\x1f\x7f]", key)
                and type(generation) is int and generation > 0)}


def _exchange_num(state, key):
    value = state.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _exchange_main_session():
    """False inside any worker/dispatch child: only a main session triggers R."""
    env = os.environ
    if env.get("AGENT_SESSION_ROLE") == "worker":
        return False
    if env.get("AGENT_DISPATCH_CHILD") == "1":
        return False
    depth = env.get("AGENT_DISPATCH_DEPTH", "").strip()
    if depth:
        try:
            if int(depth) > 0:
                return False
        except ValueError:
            return False
    if env.get("OPENCODE_DISPATCH_SLUG", "").strip():
        return False
    if env.get("FLEET_TITLE_REFRESH") == "1":
        return False
    return True


def _exchange_read_interval():
    try:
        value = float(os.environ.get("MEM_SYNC_READ_INTERVAL_SECONDS", ""))
        if value >= 0:
            return value
    except ValueError:
        pass
    return EXCHANGE_READ_INTERVAL_SECONDS


def _exchange_window():
    try:
        value = float(os.environ.get("MEM_EXCHANGE_WINDOW_SECONDS", ""))
        if value >= 0:
            return value
    except ValueError:
        pass
    return EXCHANGE_WINDOW_SECONDS


def _exchange_read_due(state, now):
    return (now - _exchange_num(state, "last_success_receive") > _exchange_read_interval()
            and now - _exchange_num(state, "last_receive_attempt")
            > EXCHANGE_RECEIVE_RETRY_SECONDS)


def _exchange_clean_reason(value):
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "unknown")).strip()
    return text[:120] or "unknown"
