"""Fail-soft, read-only collector for the SD-122 peer-message ledger.

Never writes. Bounded per file (tail 64KB) and overall (24h window, 200 records).
A missing or unreadable ledger returns an empty result — every Session field this
feeds stays at its snapshot default so the rendered board is byte-identical to a
pre-SD-122 board.
"""
import importlib.util
import copy
import threading
from collections import OrderedDict
from itertools import islice
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_TAIL_BYTES = 64 * 1024
_WINDOW_SECONDS = 24 * 3600
_MAX_RECORDS = 200
_MAX_PENDING_DUTIES = 128
_PENDING_DUTY_STATES = {"pending", "unknown", "delivery-pending", "cleanup-pending"}

# C-2 — a herdr sender trailer written 2026-09-08T02:54Z~11:18Z carried a trailing
# " ; ref=<32hex>" transfer-ref suffix inside the display name (the writer
# already separates them today; this is read-side tolerance for the records
# still on disk from that window, display only — the ledger itself is untouched).
_REF_TRAILER_RE = re.compile(r"\s*;\s*ref=[0-9a-f]{32}\s*$")

_peer_message_module = None


class _PassiveParseCache:
    def __init__(self):
        self.entries, self.bytes = OrderedDict(), 0
        self.lock = threading.RLock()

    def read(self, key, generation, now, load):
        with self.lock:
            held = self.entries.get(key)
            if held is not None and held[0] == generation and now >= held[2].observed_at:
                self.entries.move_to_end(key)
                return held[2]
        value = load()
        size = value.byte_size
        with self.lock:
            old = self.entries.pop(key, None)
            if old is not None:
                self.bytes -= old[1]
            if value.cacheable and size <= 4 * _TAIL_BYTES:
                self.entries[key] = (generation, size, value)
                self.bytes += size
            while len(self.entries) > 4096 or self.bytes > 32 * 1024 * 1024:
                _key, old = self.entries.popitem(last=False)
                self.bytes -= old[1]
        return value


_PASSIVE_PARSE_CACHE = _PassiveParseCache()


def _file_generation(path):
    info = os.stat(path)
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _clean_from_name(name):
    if not isinstance(name, str):
        return name
    return _REF_TRAILER_RE.sub("", name)


def _note_diagnostic(diagnostics, operation, error):
    if diagnostics is None:
        return
    name = error if isinstance(error, str) else type(error).__name__
    item = {"operation": operation, "error": name}
    if item not in diagnostics:
        diagnostics.append(item)


def _load_peer_message(diagnostics=None):
    """Lazy-load `utilities/peer-message.py` (hyphenated — not `import`able by
    name) once, so `_state_roots()` can ask it for the peer ledger's canonical
    root (C-8). Fail-soft and cached: a read-only collector must never raise,
    and re-executing the module on every tick would be wasted work."""
    global _peer_message_module
    if _peer_message_module is not None:
        return _peer_message_module
    here = Path(__file__).resolve()
    for candidate in here.parents:
        pm_path = candidate / "utilities" / "peer-message.py"
        try:
            if not stat.S_ISREG(pm_path.stat().st_mode):
                continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            _note_diagnostic(diagnostics, "resolve_peer_module", exc)
            continue
        try:
            spec = importlib.util.spec_from_file_location("peer_message", str(pm_path))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            _note_diagnostic(diagnostics, "resolve_peer_module", exc)
            return None
        _peer_message_module = module
        return module
    _note_diagnostic(diagnostics, "resolve_peer_module", "ResolverUnavailable")
    return None


def _peer_ledger_root(diagnostics=None):
    module = _load_peer_message(diagnostics)
    if module is None:
        return None
    try:
        return str(module.peer_state_root())
    except Exception as exc:
        _note_diagnostic(diagnostics, "resolve_peer_root", exc)
        return None


def _agent_home():
    from dispatch_contract import resolve_agent_home
    return (os.environ.get("AGENT_HOME") or os.environ.get("CLAUDE_HOME")
            or str(resolve_agent_home()))


def _runtime_ledger_roots(diagnostics=None):
    """F-100c — each installed runtime resolves ITS OWN dispatch state root
    (`~/.codex/.harness/dispatch`, `~/.config/opencode/.harness/dispatch`, …; measured
    2026-09-03), so a Codex/OpenCode receiver's `notice` lands in that runtime's ledger,
    not in the stable per-user root the Claude side writes to. The board reads all of
    them; existence-gated, so a runtime that was never activated adds nothing."""
    out = []
    # Root discovery is also on the prompt path. It needs runtime locations,
    # not the Fleet dispatch collector and its entire supervisor dependency graph.
    for home in (os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
                 os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
                 os.path.expanduser("~/.config/opencode")):
        if not home:
            continue
        root = os.path.join(home, ".harness", "dispatch")
        active = False
        for child in ("peer-messages", "peer-steward"):
            try:
                is_dir = stat.S_ISDIR(os.stat(os.path.join(root, child)).st_mode)
            except FileNotFoundError:
                continue
            except OSError as exc:
                _note_diagnostic(diagnostics, "resolve_runtime_roots", exc)
                continue
            if is_dir:
                active = True
                break
        if active:
            out.append(root)
    return out


def _state_roots(diagnostics=None):
    """F-98d — read through the SAME resolver chain the writer (`peer-message.py`) uses,
    not `dispatch._row_state_roots()`'s no-row default (which pins to the release tree's
    `.dispatch` and ignores an inherited `AGENT_DISPATCH_JOBS`). Modelled on
    `tools/fleet/route.py`'s `_dispatch_state_roots` — lazy import, tolerant of every
    failure (never raises out of a read-only collector). F-100c appends every installed
    runtime's own dispatch root (`_runtime_ledger_roots`).

    C-8 — index 0 is always the peer ledger's canonical root
    (`peer-message.py::peer_state_root()`), promoting it to the front (never
    duplicating it) when it is already present. This is the same list
    `peer-message.py:412 steward_marker_roots` reads back through the
    "fleet-reader" branch, and its "chain index 0 = the writer's root" contract
    depends on this ordering. A resolver failure here is swallowed — the rest
    of the roots still apply."""
    if diagnostics is None:
        diagnostics = []
    roots = []
    try:
        home = _agent_home()
    except Exception as exc:
        _note_diagnostic(diagnostics, "resolve_dispatch_home", exc)
        home = None
    if home is not None:
        here = Path(__file__).resolve()
        found_contract = False
        for candidate in here.parents:
            utilities_dir = candidate / "utilities"
            contract_path = utilities_dir / "dispatch_contract.py"
            try:
                if not stat.S_ISREG(contract_path.stat().st_mode):
                    continue
            except FileNotFoundError:
                continue
            except OSError as exc:
                _note_diagnostic(diagnostics, "resolve_dispatch_roots", exc)
                continue
            found_contract = True
            if str(utilities_dir) not in sys.path:
                sys.path.insert(0, str(utilities_dir))
            try:
                from dispatch_contract import dispatch_state_roots
                roots = [str(root) for root in dispatch_state_roots(
                    Path(home), jobs=os.environ.get("AGENT_DISPATCH_JOBS"))]
            except Exception as exc:
                _note_diagnostic(diagnostics, "resolve_dispatch_roots", exc)
                roots = []
            break
        if not found_contract:
            _note_diagnostic(diagnostics, "resolve_dispatch_roots", "ResolverUnavailable")
    try:
        for extra in _runtime_ledger_roots(diagnostics):
            if extra not in roots:
                roots.append(extra)
    except Exception as exc:
        _note_diagnostic(diagnostics, "resolve_runtime_roots", exc)
    peer_root = _peer_ledger_root(diagnostics)
    if peer_root is not None:
        peer_norm = os.path.normpath(peer_root)
        roots = [r for r in roots if os.path.normpath(r) != peer_norm]
        roots.insert(0, peer_root)
    _state_roots.last_diagnostics = list(diagnostics)
    return tuple(roots)


_state_roots.last_diagnostics = []


def _tail_lines(path, diagnostics=None):
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # discard the first, possibly-incomplete line
            return fh.read().splitlines()
    except OSError as exc:
        _note_diagnostic(diagnostics, "read_peer_ledger", exc)
        return []


def _parse_ts(ts):
    try:
        raw = ts.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        from datetime import datetime
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return None


def _record_shape_valid(rec):
    malformed_record = False
    for field in ("from", "to", "delivery"):
        value = rec.get(field)
        if value is not None and not isinstance(value, dict):
            malformed_record = True
            break
    if not malformed_record:
        for field in ("from", "to"):
            endpoint = rec.get(field) or {}
            sid = endpoint.get("session_id")
            if sid is not None and not isinstance(sid, str):
                malformed_record = True
                break
    if not isinstance(rec.get("kind"), str):
        malformed_record = True
    delivery = rec.get("delivery") or {}
    status = delivery.get("status") or ""
    if not isinstance(status, str):
        malformed_record = True
    return not malformed_record


def _prepared_ledger(path, now, cache):
    generation = None
    if cache is not None:
        try:
            generation = (_file_generation(path), _tail_lines, json.loads,
                          _parse_ts, _record_shape_valid, open, os.path.getsize)
        except OSError:
            pass

    def load():
        diagnostics, rows = [], []
        malformed, size = 0, 128
        for line in _tail_lines(path, diagnostics):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                rec = None
            if not isinstance(rec, dict):
                malformed += 1
                _note_diagnostic(diagnostics, 'parse_peer_record', 'MalformedRecord')
                continue
            ts = _parse_ts(rec.get('ts', ''))
            if ts is None:
                _note_diagnostic(diagnostics, 'parse_peer_record', 'InvalidTimestamp')
                continue
            # Old valid timestamps cannot re-enter this window unless the clock
            # moves backwards, which invalidates this file's prepared result.
            if (now - ts) > _WINDOW_SECONDS:
                continue
            rows.append((ts, rec if _record_shape_valid(rec) else None))
            size += len(line) * 4
        return SimpleNamespace(observed_at=now, source_token=object(), records=tuple(rows), malformed=malformed,
            diagnostics=tuple(diagnostics), byte_size=size,
            cacheable=not any(item['operation'] == 'read_peer_ledger' for item in diagnostics))

    return (cache.read(path, generation, now, load)
            if cache is not None and generation is not None else load())


def _bounded_duty_paths(root, limit, diagnostics=None):
    """Bound directory enumeration as well as file reads; this is a preview."""
    if limit <= 0:
        return []
    try:
        with os.scandir(root) as entries:
            # Allow room for sidecar locks, then sort only this bounded sample.
            paths = [Path(entry.path) for entry in islice(entries, limit * 4)
                     if entry.name.endswith(".json")]
        return sorted(paths)[:limit]
    except FileNotFoundError:
        return []
    except OSError as exc:
        _note_diagnostic(diagnostics, "scan_peer_duties", exc)
        return []


def _ledger_paths(roots, diagnostics, scan_status):
    """Enumerate the existing two-level ledger layout while preserving scan errors."""
    paths = []
    for root in roots:
        root_failed = False
        ledger_root = os.path.join(str(root), "peer-messages")
        try:
            month_dirs = os.scandir(ledger_root)
        except FileNotFoundError:
            scan_status["checked_roots"] += 1  # a missing ledger is a complete empty scan
            continue
        except OSError as exc:
            _note_diagnostic(diagnostics, "scan_peer_ledger", exc)
            scan_status["failed_roots"] += 1
            continue

        try:
            with month_dirs:
                for month in month_dirs:
                    if month.name.startswith("."):
                        continue
                    try:
                        if not month.is_dir(follow_symlinks=True):
                            continue
                    except OSError as exc:
                        root_failed = True
                        _note_diagnostic(diagnostics, "scan_peer_ledger", exc)
                        continue
                    try:
                        with os.scandir(month.path) as entries:
                            for entry in entries:
                                if entry.name.startswith(".") or not entry.name.endswith(".jsonl"):
                                    continue
                                try:
                                    if entry.is_file(follow_symlinks=True):
                                        paths.append(entry.path)
                                except OSError as exc:
                                    root_failed = True
                                    _note_diagnostic(diagnostics, "scan_peer_ledger", exc)
                    except OSError as exc:
                        root_failed = True
                        _note_diagnostic(diagnostics, "scan_peer_ledger", exc)
        except OSError as exc:
            root_failed = True
            _note_diagnostic(diagnostics, "scan_peer_ledger", exc)
        if root_failed:
            scan_status["failed_roots"] += 1
        else:
            scan_status["checked_roots"] += 1
    return paths


def _regular_file_exists(path, diagnostics, operation):
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        _note_diagnostic(diagnostics, operation, exc)
        return None


def _pending_obligations(roots, now, diagnostics=None):
    """Read a bounded, body-free view of accepted peer follow-through."""
    by_session = {}
    seen = set()
    examined = 0

    def add(key, kind, state, created, ref=None):
        if not key or not isinstance(key[1], str) or not key[1]:
            return
        row = by_session.setdefault(key, [])
        entry = {"kind": kind, "state": state,
                 "age_min": max(0, int((now - created) // 60))}
        if isinstance(ref, str) and ref:
            entry["ref"] = ref[:80]
        row.append(entry)

    for state_root in roots:
        if examined >= _MAX_PENDING_DUTIES:
            break
        obligation_root = Path(state_root) / "peer-steward" / "obligations"
        paths = _bounded_duty_paths(obligation_root, _MAX_PENDING_DUTIES - examined,
                                    diagnostics)
        for path in paths:
            examined += 1
            try:
                if path.is_symlink() or path.stat().st_size > 128 * 1024:
                    continue
                duty = json.loads(path.read_text(encoding="utf-8"))
            except OSError as exc:
                _note_diagnostic(diagnostics, "read_peer_duty", exc)
                continue
            except (ValueError, UnicodeError) as exc:
                _note_diagnostic(diagnostics, "parse_peer_duty", exc)
                continue
            if not isinstance(duty, dict):
                _note_diagnostic(diagnostics, "parse_peer_duty", "MalformedRecord")
                continue
            if (not isinstance(duty.get("state"), str)
                    or duty.get("state") not in _PENDING_DUTY_STATES):
                continue
            duty_id = duty.get("id")
            if not isinstance(duty_id, str) or duty_id in seen:
                continue
            seen.add(duty_id)
            intent = duty.get("intent") or {}
            if not isinstance(intent, dict):
                _note_diagnostic(diagnostics, "parse_peer_duty", "MalformedRecord")
                continue
            identity = intent.get("identity") or duty.get("identity") or {}
            if not isinstance(identity, dict):
                _note_diagnostic(diagnostics, "parse_peer_duty", "MalformedRecord")
                continue
            kind = intent.get("kind") or duty.get("kind") or ""
            kind = kind.lower() if isinstance(kind, str) else ""
            if kind == "message":
                target = intent.get("to") or identity
                if not isinstance(target, dict):
                    _note_diagnostic(diagnostics, "parse_peer_duty", "MalformedRecord")
                    continue
                label = "delay" if duty_id.startswith("delay-") or intent.get("delay_notice_for") else "message"
                observation = duty.get("observation") or {}
                if not isinstance(observation, dict):
                    observation = {}
                ref = intent.get("ref") or observation.get("transfer_ref")
            elif kind == "retire":
                target, label, ref = identity, "retire", None
            else:
                continue
            key = (str(target.get("harness") or "").lower(), target.get("session_id"))
            created = duty.get("accepted_at") or duty.get("updated_at") or now
            try:
                created = float(created)
            except (TypeError, ValueError):
                created = now
            add(key, label, duty.get("state"), created, ref)

        watch_root = Path(state_root) / "peer-watches"
        arms = _bounded_duty_paths(watch_root, _MAX_PENDING_DUTIES - examined,
                                    diagnostics)
        for path in arms:
            if examined >= _MAX_PENDING_DUTIES:
                break
            if path.name.endswith((".receipt.json", ".ack.json", ".observation.json")):
                continue
            examined += 1
            try:
                if path.is_symlink() or path.stat().st_size > 128 * 1024:
                    continue
                arm = json.loads(path.read_text(encoding="utf-8"))
            except OSError as exc:
                _note_diagnostic(diagnostics, "read_peer_watch", exc)
                continue
            except (ValueError, UnicodeError) as exc:
                _note_diagnostic(diagnostics, "parse_peer_watch", exc)
                continue

            if not isinstance(arm, dict):
                _note_diagnostic(diagnostics, "parse_peer_watch", "MalformedRecord")
                continue
            watch_id = arm.get("watch_id")
            if not isinstance(watch_id, str) or watch_id in seen:
                continue
            seen.add(watch_id)
            receipt = watch_root / f"{watch_id}.receipt.json"
            ack = watch_root / f"{watch_id}.ack.json"
            receipt_exists = _regular_file_exists(receipt, diagnostics, "read_peer_watch")
            ack_exists = _regular_file_exists(ack, diagnostics, "read_peer_watch")
            if receipt_exists is None or ack_exists is None or (receipt_exists and ack_exists):
                continue
            agent = arm.get("agent") or {}
            if not isinstance(agent, dict):
                _note_diagnostic(diagnostics, "parse_peer_watch", "MalformedRecord")
                continue
            key = (str(agent.get("harness") or "").lower(), agent.get("session_id"))
            created = _parse_ts(str(arm.get("armed_ts") or ""))
            add(key, "watch", "receipt-pending" if receipt_exists else "watching",
                created if created is not None else now, None)
    return by_session


_OUTPUT_OBSERVATION = None
_OUTPUT_LOCK = threading.RLock()
_OUTPUT_MAX_BYTES = 96 * 1024
_OUTPUT_STATS = {'hits': 0, 'loads': 0, 'rejections': 0}


def _project_records(records, now, clocks):
    """Pure whole-ledger projection; the caller owns fresh file/time reads."""
    ordered = sorted(records, key=lambda pair: pair[0])
    by_session = {}
    pending = {}

    def _key(block):
        sid = block.get("session_id")
        return (str(block.get("harness") or "").lower(), sid) if sid else None

    def _row(key):
        return by_session.setdefault(key, {"sent_1h": 0, "recv_1h": 0,
                                           "last_recv": None, "last_sent": None})

    def _peer_name(block, key):
        """The ledger's own display name for an endpoint, or ``None``.

        Never the raw session id. A codex→claude transfer record carries
        ``from.name: null`` (measured 2026-09-09), and the old fallback pasted the
        36-character id into the name slot, which is what put
        `← 0100000b-0000-7000-8000-000000000013` on the board. Naming an unnamed peer
        is the renderer's job — it can see the tag of a row the collector cannot.
        """
        name = _clean_from_name(block.get("name"))
        if not isinstance(name, str):
            return None
        name = name.strip()
        if not name:
            return None
        sid = key[1] if key else None
        return None if sid and name == sid else name

    def _record_recv(to_key, kind, frm, to, from_key, age_min):
        row = _row(to_key)
        clocks[(to_key, 'last_recv')] = ts
        if age_min <= 60:
            row["recv_1h"] += 1
        row["last_recv"] = {"from_name": _peer_name(frm, from_key),
                             "from_session_id": from_key[1] if from_key else None,
                             # An identity-less sender still has a harness, and the sent
                             # side has always kept it. Dropping it here left the renderer
                             # unable to look the peer up at all (measured 2026-09-10).
                             "from_harness": (from_key[0] if from_key
                                              else str(frm.get("harness") or "").lower()),
                             "kind": kind, "age_min": age_min}

    def _record_sent(from_key, kind, frm, to, to_key, age_min):
        row = _row(from_key)
        clocks[(from_key, 'last_sent')] = ts
        row["last_sent"] = {"to_name": _peer_name(to, to_key),
                            "to_session_id": to_key[1] if to_key else None,
                            "to_harness": (to_key[0] if to_key
                                           else str(to.get("harness") or "").lower()),
                            "kind": kind, "age_min": age_min}

    def _upgrade_recv(to_key, inherited, frm, to, from_key, age_min):
        # A correlated notice is, by construction, the newest successful receipt for
        # `to_key` (it is processed in ts order and only reached once its matching
        # sent record popped off `pending`) — so it replaces `last_recv` unconditionally,
        # even when a later record from a different sender had already overwritten it.
        # `recv_1h` is untouched here: the notice and its correlated sent record are one
        # logical message and must count once (F-101i).
        row = _row(to_key)
        clocks[(to_key, 'last_recv')] = ts
        row["last_recv"] = {"from_name": _peer_name(frm, from_key),
                             "from_session_id": from_key[1] if from_key else None,
                             # An identity-less sender still has a harness, and the sent
                             # side has always kept it. Dropping it here left the renderer
                             # unable to look the peer up at all (measured 2026-09-10).
                             "from_harness": (from_key[0] if from_key
                                              else str(frm.get("harness") or "").lower()),
                             "kind": inherited, "age_min": age_min}

    # Correlation is bounded by the existing tail/window/max limits. A clipped sender
    # leaves a notice as `notice` (honest miss); LIFO reduces but cannot eliminate an
    # ambiguity when the same exact pair sends several kinds in quick succession.
    for ts, rec in ordered:
        frm, to = rec.get("from") or {}, rec.get("to") or {}
        from_key, to_key = _key(frm), _key(to)
        age_min = max(0, int((now - ts) // 60))
        kind = rec.get("kind")
        status = ((rec.get("delivery") or {}).get("status") or "").lower()
        deliverable = status != "failed"
        if from_key:
            # The endpoint's row exists as soon as the ledger names it, but only a
            # non-notice record is a SEND. F-101i, send side: a herdr message writes two
            # records naming the same sender — its own `steer` and the receiver's `notice`
            # receipt — so counting both made one sent message read `✉ 2/…` (measured
            # 2026-09-09 on the codex↔claude test pair). `recv_1h` already counted such a
            # pair once; this is the same rule on the other axis, and it agrees with
            # `last_sent`, which was never set from a notice.
            row = _row(from_key)
            if kind != "notice" and age_min <= 60:
                row["sent_1h"] += 1
        if kind != "notice":
            if from_key and to_key and deliverable:
                pending.setdefault((from_key, to_key), []).append(kind)
            if to_key and deliverable:
                _record_recv(to_key, kind, frm, to, from_key, age_min)
            if from_key and deliverable:
                _record_sent(from_key, kind, frm, to, to_key, age_min)
        else:
            stack = pending.get((from_key, to_key)) if from_key and to_key else None
            if stack:
                inherited = stack.pop()
                if to_key and deliverable:
                    _upgrade_recv(to_key, inherited, frm, to, from_key, age_min)
            elif to_key and deliverable:
                _record_recv(to_key, "notice", frm, to, from_key, age_min)

    return by_session


def _peer_result(records, pending_duties, now, *, reuse=False, source_tokens=None,
                 include_records=True):
    from runtime_observation import retained_size
    global _OUTPUT_OBSERVATION
    # Production tokens name a prepared file incarnation and its row index,
    # retaining only tiny tokens rather than the old full ledger dictionaries.
    tokens = tuple(source_tokens) if source_tokens is not None else tuple(records)
    signature = (tuple((ts, token, max(0, int((now-ts)//60)) <= 60)
                       for (ts, _rec), token in zip(records, tokens)),
                 _project_records, _clean_from_name)
    held = None
    if reuse:
        with _OUTPUT_LOCK:
            if _OUTPUT_OBSERVATION is not None and _OUTPUT_OBSERVATION[0] == signature:
                held = _OUTPUT_OBSERVATION
                _OUTPUT_STATS['hits'] += 1
    if held is None:
        clocks = {}
        by_session = _project_records(records, now, clocks)
        if reuse:
            with _OUTPUT_LOCK:
                _OUTPUT_STATS['loads'] += 1
                admitted = retained_size((signature, clocks, by_session), _OUTPUT_MAX_BYTES) <= _OUTPUT_MAX_BYTES
                if not admitted:
                    _OUTPUT_STATS['rejections'] += 1
                _OUTPUT_OBSERVATION = (((copy.deepcopy(signature[0]), *signature[1:]) if source_tokens is None else signature,
                                        clocks, copy.deepcopy(by_session))
                                       if admitted else None)
    else:
        clocks, by_session = held[1], copy.deepcopy(held[2])
    for (key, direction), ts in clocks.items():
        by_session[key][direction]['age_min'] = max(0, int((now-ts)//60))
    for key, duties in pending_duties.items():
        by_session.setdefault(key, {'sent_1h': 0, 'recv_1h': 0,
                                   'last_recv': None, 'last_sent': None})['pending_obligations'] = sorted(
            duties, key=lambda item: (item.get('kind', ''), item.get('age_min', 0)))[:8]
    published = ((copy.deepcopy([rec for _ts, rec in records]) if reuse else
                  [rec for _ts, rec in records]) if include_records else [])
    return {'records': published, 'by_session': by_session}


def collect(state_roots=None, *, include_health=False, include_records=True):
    records = []
    source_tokens = {}
    malformed = 0
    now = time.time()
    diagnostics = []
    from . import dispatch
    cache = _PASSIVE_PARSE_CACHE if dispatch._COLLECTION_INPUTS.get() is not None else None
    if state_roots is None:
        try:
            roots = tuple(_state_roots(diagnostics))
        except Exception as exc:
            roots = ()
            _note_diagnostic(diagnostics, "resolve_state_roots", exc)
    else:
        try:
            roots = tuple(state_roots)
        except Exception as exc:
            roots = ()
            _note_diagnostic(diagnostics, "resolve_state_roots", exc)

    seen_files = set()
    scan_status = {"checked_roots": 0, "failed_roots": 0}
    paths = _ledger_paths(roots, diagnostics, scan_status)
    for path in paths:
        try:
            real = os.path.realpath(path)
            if real in seen_files:
                continue
            seen_files.add(real)
        except OSError as exc:
            _note_diagnostic(diagnostics, "resolve_peer_path", exc)
            continue
        prepared = _prepared_ledger(path, now, cache)
        malformed += prepared.malformed
        for item in prepared.diagnostics:
            _note_diagnostic(diagnostics, item['operation'], item['error'])
        for record_index, (ts, rec) in enumerate(prepared.records):
            if (now - ts) > _WINDOW_SECONDS:
                continue
            if rec is None:
                _note_diagnostic(diagnostics, 'parse_peer_record', 'MalformedRecord')
                continue
            records.append((ts, rec))
            source_tokens[id(rec)] = (getattr(prepared, "source_token", None) or object(), record_index)

    records.sort(key=lambda pair: pair[0], reverse=True)
    records = records[:_MAX_RECORDS]

    pending_duties = _pending_obligations(roots, now, diagnostics)
    result = _peer_result(records, pending_duties, now, reuse=cache is not None,
                          source_tokens=[source_tokens[id(rec)] for _ts, rec in records],
                          include_records=include_records)
    by_session = result['by_session']

    if not diagnostics:
        state = "success"
    elif scan_status["checked_roots"] or records or by_session:
        state = "unknown"
    else:
        state = "failed"
    collect.last_diagnostics = list(diagnostics)
    collect.last_malformed = malformed
    if include_health:
        return result, SimpleNamespace(state=state, diagnostics=tuple(diagnostics))
    return result


collect.last_diagnostics = []
collect.last_malformed = 0
