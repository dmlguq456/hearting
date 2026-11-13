"""A ``sitecustomize`` shim that records where a collection pass spends its time.

The producer runs as ``python3 utilities/human_wait_state.py --refresh-cache``, so
the file is ``__main__`` and an import hook keyed on the module name never sees
it. The shim therefore finds its targets by *code object*: with Python >= 3.12
``sys.monitoring`` ``PY_START``/``PY_RETURN``/``PY_UNWIND`` match the pair
``(basename of co_filename, co_qualname)``; any other code object returns
``DISABLE`` and costs nothing afterwards. Older Pythons use an import hook that
wraps the same names (``__main__`` is aliased by its file's basename).

Each *root* call (a pass) is written as one JSONL record with the per-target
counts and totals that ran inside it, the registry-cache metrics delta and the
``open``/listing audit counts. ``required`` roots that never appear are a
failure the orchestrator reports instead of publishing a number.

Production session discovery is replaced, as the hermetic benchmark does:
``procscan.scan`` returns only the fixture's sessions and ``_scan_processes``
finds nothing, so the ps view of the operator's own sessions never enters.
"""
from __future__ import annotations

# (basename without .py, co_qualname). The names follow the cumulative profile of one cold
# ``fleet --once`` and the plan's table.
TARGETS = (
    ("human_wait_state", "_refresh_cache"), ("human_wait_state", "_route_demands"),
    ("human_wait_state", "publish_cache"), ("human_wait_state", "_sleep_until_registry_change"),
    ("human_gate_observation", "read_answer_waits"),
    ("route_authority", "decide_continuation"),
    ("capability_route_verify", "verify_route"),
    ("capability_route_nodes", "unit_catalog_digest"),
    ("__init__", "collect_all"), ("procscan", "scan"),
    ("dispatch", "collect"), ("dispatch", "_scan_registry_evidence"),
    ("dispatch", "_attach_stall_observation"), ("dispatch", "_parse_registry_view"),
    ("dispatch", "parsed_registry_rows"),
    ("dispatch_notice_state", "notice_is_current"), ("session_notice", "notice_is_current"),
    ("codex", "_initialize_lifecycle_cursor"), ("codex", "_read_lifecycle_range"),
    ("claude", "_tail_subagents"), ("route_chain", "enrich"),
    ("resource_runs", "collect"), ("steward", "read_relations"),
    ("dispatch_registry_cache", "RegistryView.__init__"),
    ("dispatch_replacement", "registry_rows"),
    ("session_line", "session_route_status"),
    ("render", "_explorer_compose"), ("render", "_draw"),
    # The route judgments (speed-e2e leg C): what an answer-wait pass and a Fleet pass spend per
    # route, and where a registry replacement's time goes.
    ("human_gate_observation", "_generation_candidates"), ("human_gate_observation", "_route_outcome"),
    ("human_gate_observation", "pending_human_waits"),
    ("dispatch_owner_input", "blocked_owner_answers"),
    ("owner_route_binding", "_verified_binding"),
    ("dispatch_supervision", "notice_is_current"), ("dispatch_supervision", "update_attempt_stall_observation"),
    ("dispatch_supervision", "_recorded_completion_result"),
    ("dispatch_seat_handover", "effective_parent"), ("dispatch_seat_handover", "_lineage_signature"),
    ("dispatch", "_registry_evidence_for_path"), ("dispatch", "_build_registry_derived_index"),
    ("dispatch", "_job_selection"), ("dispatch", "_prime_route_display_rows"),
    ("dispatch_seat_handover", "job_holders"),
    # The floor and the rebuild inside ``RegistryView`` after a replacement: reading and digesting
    # the new generation against segment reconstruction.
    ("dispatch_registry_cache", "_reusable_prefix"), ("dispatch_registry_cache", "_RegistrySegment.__init__"),
    ("dispatch_registry_cache", "_iter_lf_chunks"),
)
PRODUCER_ROOTS = (("human_wait_state", "_refresh_cache"),)
FLEET_ROOTS = (("__init__", "collect_all"), ("render", "_explorer_compose"), ("render", "_draw"))
REQUIRED = {"producer": ("human_wait_state._refresh_cache",),
            "fleet": ("__init__.collect_all", "render._explorer_compose", "render._draw")}

SHIM_SOURCE = r'''
import importlib.util, json, os, sys, threading, time

_OUT = os.environ.get("E2E_SHIM_OUT")
if _OUT:
    _TARGETS = {tuple(k.split(":", 1)): k.replace(":", ".") for k in json.loads(os.environ["E2E_SHIM_TARGETS"])}
    _ROOTS = {tuple(k.split(":", 1)) for k in json.loads(os.environ["E2E_SHIM_ROOTS"])}
    _FORCE_HOOK = os.environ.get("E2E_SHIM_MODE") == "import-hook"
    _lock = threading.Lock()
    _fh = open(_OUT, "a", buffering=1)
    _active = {}          # root call id -> accumulator
    _stacks = {}          # thread id -> list of (name, t0, root_acc|None, is_outer)
    _depth = {}
    _counts = {"open": 0, "listing": 0}
    _next = [0]

    def _emit(rec):
        with _lock:
            _fh.write(json.dumps(rec) + "\n")

    def _metrics():
        for name in ("dispatch_registry_cache", "utilities.dispatch_registry_cache"):
            mod = sys.modules.get(name)
            if mod is not None and hasattr(mod, "metrics_snapshot"):
                try:
                    return dict(mod.metrics_snapshot())
                except Exception:
                    return {}
        return {}

    def _audit(event, args):
        if event == "open":
            _counts["open"] += 1
        elif event in ("os.listdir", "os.scandir"):
            _counts["listing"] += 1
    sys.addaudithook(_audit)

    def _enter(key, name):
        t0 = time.monotonic_ns()
        tid = threading.get_ident()
        stack = _stacks.setdefault(tid, [])
        depth = _depth.setdefault((tid, name), 0)
        _depth[(tid, name)] = depth + 1
        acc = None
        if key in _ROOTS:
            _next[0] += 1
            acc = {"root": name, "index": _next[0], "t0": t0, "wall0": time.time(), "seg": {},
                   "open0": _counts["open"], "list0": _counts["listing"], "metrics0": _metrics()}
            with _lock:
                _active[_next[0]] = acc
        stack.append((name, t0, acc, depth == 0))

    _last_model = [None]

    def _get(obj, key):
        try:
            return obj.get(key)
        except Exception:
            return None

    def _model_digest():
        """What the Fleet explorer's screen model shows about routes and questions (compact, no text)."""
        model = None
        for mod_name, mod in list(sys.modules.items()):
            if mod_name == "render" or mod_name.endswith(".render"):
                model = getattr(mod, "_EXPLORER_MODEL", None)
                if model is not None:
                    break
        if model is None:
            return None
        try:
            routes = {}
            for row in getattr(model, "routes", ()) or ():
                doer = _get(row, "doer")
                owner = _get(_get(_get(row, "visibility"), "codes"), "owner")
                routes[str(_get(row, "id"))] = {
                    "state": _get(row, "state"), "outcome": _get(row, "outcome"),
                    "active": [_get(stage, "name") for stage in (_get(row, "stages") or ())
                               if _get(stage, "st") == "active"],
                    "tool": _get(doer, "tool"), "attempt": _get(owner, "attempt_id")}
            return {"routes": routes,
                    "questions": [str(_get(q, "id")) for q in (getattr(model, "questions", ()) or ())]}
        except Exception as exc:
            return {"error": repr(exc)}

    def _leave(name):
        t1 = time.monotonic_ns()
        tid = threading.get_ident()
        stack = _stacks.get(tid) or []
        for position in range(len(stack) - 1, -1, -1):
            if stack[position][0] == name:
                _n, t0, acc, outer = stack.pop(position)
                break
        else:
            return
        _depth[(tid, name)] = max(0, _depth.get((tid, name), 1) - 1)
        with _lock:
            actives = list(_active.values())
        if outer:
            for other in actives:
                if other is acc:
                    continue
                seg = other["seg"].setdefault(name, {"n": 0, "total_ns": 0, "max_ns": 0})
                seg["n"] += 1
                seg["total_ns"] += t1 - t0
                seg["max_ns"] = max(seg["max_ns"], t1 - t0)
        if acc is not None:
            with _lock:
                _active.pop(acc["index"], None)
            before, after = acc.pop("metrics0"), _metrics()
            acc.update(t1=t1, wall1=time.time(), open=_counts["open"] - acc.pop("open0"),
                       listing=_counts["listing"] - acc.pop("list0"),
                       metrics={k: after[k] - before.get(k, 0) for k in after
                                if isinstance(after[k], (int, float)) and after[k] != before.get(k, 0)},
                       pid=os.getpid())
            if name == "render._draw":
                # After t1 is taken, so it is not part of the pass: the model this frame was drawn
                # from (``_draw`` has decided between the published, replayed and loading frame by now).
                digest = _model_digest()
                if digest is not None and digest != _last_model[0]:
                    acc["model"] = _last_model[0] = digest
            _emit(acc)

    mode = "import-hook"
    mon = getattr(sys, "monitoring", None)
    if mon is not None and not _FORCE_HOOK:
        mode = "monitoring"
        TOOL = 3
        try:
            mon.use_tool_id(TOOL, "e2e-latency")
        except ValueError:
            mon.free_tool_id(TOOL)
            mon.use_tool_id(TOOL, "e2e-latency")
        ev = mon.events

        def _key(code):
            base = os.path.basename(code.co_filename)
            return (base[:-3] if base.endswith(".py") else base, code.co_qualname)

        def _start(code, offset):
            key = _key(code)
            if key not in _TARGETS:
                return mon.DISABLE
            _enter(key, _TARGETS[key])

        def _ret(code, offset, value=None):
            key = _key(code)
            if key not in _TARGETS:
                return mon.DISABLE
            _leave(_TARGETS[key])

        def _unwind(code, offset, exc):
            key = _key(code)
            if key in _TARGETS:
                _leave(_TARGETS[key])
            # PY_UNWIND cannot be disabled: returning DISABLE here removes the callback.
        mon.register_callback(TOOL, ev.PY_START, _start)
        mon.register_callback(TOOL, ev.PY_RETURN, _ret)
        mon.register_callback(TOOL, ev.PY_UNWIND, _unwind)
        mon.set_events(TOOL, ev.PY_START | ev.PY_RETURN | ev.PY_UNWIND)

    _wrapped = set()

    def _wrap_module(module):
        stem = (os.path.basename(getattr(module, "__file__", "") or "")[:-3]) or ""
        if module.__name__ == "__main__":
            stem = stem or "__main__"
        for (base, qual), name in _TARGETS.items():
            if base != stem or "." in qual:
                continue
            fn = getattr(module, qual, None)
            if fn is None or not callable(fn) or (module.__name__, qual) in _wrapped:
                continue
            def make(fn, key, name):
                def wrapper(*a, **k):
                    _enter(key, name)
                    try:
                        return fn(*a, **k)
                    finally:
                        _leave(name)
                wrapper.__wrapped__ = fn
                wrapper.__name__ = getattr(fn, "__name__", qual)
                return wrapper
            setattr(module, qual, make(fn, (base, qual), name))
            _wrapped.add((module.__name__, qual))

    # --- session discovery of the fixture only (replaces the ps view of real sessions) ---
    _SESSIONS = os.environ.get("E2E_SHIM_SESSIONS")

    def _patch(module):
        name = module.__name__
        if name.endswith(".collectors.procscan") and _SESSIONS:
            def scan(**_kw):
                rows = []
                for item in json.load(open(_SESSIONS)):
                    session = module.Session(harness=item["harness"], pid=item["pid"],
                                             cwd=item["cwd"], elapsed_min=30)
                    session.proc_start = item.get("pid_start")
                    rows.append(session)
                return rows
            module.scan = scan
            # ``read_proc_start`` is left as it is: the fixture's sessions are real sleeper
            # processes whose start time /proc gives, and the Fleet judges its registry rows by a
            # compiled selection only while that reader is the native one (``_job_selection``).
            # Replacing it sent every Fleet pass down the slow path that visits every finished
            # row, a path an operating Fleet does not take.
        elif name.endswith(".collectors.dispatch"):
            module._scan_processes = lambda *a, **k: []

    class _Finder:
        busy = set()
        def find_spec(self, name, path, target=None):
            watch = name.endswith((".collectors.procscan", ".collectors.dispatch"))
            if not (watch or mode == "import-hook") or name in self.busy:
                return None
            self.busy.add(name)
            try:
                spec = importlib.util.find_spec(name)
            except Exception:
                spec = None
            finally:
                self.busy.discard(name)
            if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
                return None
            loader = spec.loader
            original = loader.exec_module
            def exec_module(module, _orig=original):
                _orig(module)
                _patch(module)
                if mode == "import-hook":
                    # a ``__main__`` script defined its names before this import ran
                    main = sys.modules.get("__main__")
                    if main is not None and getattr(main, "__file__", None):
                        _wrap_module(main)
                    _wrap_module(module)
            loader.exec_module = exec_module
            return spec
    sys.meta_path.insert(0, _Finder())
    for _m in list(sys.modules.values()):
        if getattr(_m, "__name__", "").endswith((".collectors.procscan", ".collectors.dispatch")):
            _patch(_m)
    _emit({"shim": "ready", "mode": mode, "pid": os.getpid(), "role": os.environ.get("E2E_SHIM_ROLE", "")})
'''


def render() -> str:
    return SHIM_SOURCE


def shim_env(*, out: str, shim_dir: str, role: str, roots=None, sessions: str | None = None,
             mode: str | None = None, targets=None) -> dict:
    """Environment additions that make a child load the shim and write ``out``."""
    import json
    key = lambda pair: f"{pair[0]}:{pair[1]}"  # noqa: E731
    chosen = roots if roots is not None else (PRODUCER_ROOTS if role == "producer" else FLEET_ROOTS)
    env = {"PYTHONPATH": shim_dir, "E2E_SHIM_OUT": out, "E2E_SHIM_ROLE": role,
           "E2E_SHIM_TARGETS": json.dumps([key(t) for t in (targets or TARGETS)]),
           "E2E_SHIM_ROOTS": json.dumps([key(r) for r in chosen])}
    if sessions:
        env["E2E_SHIM_SESSIONS"] = sessions
    if mode:
        env["E2E_SHIM_MODE"] = mode
    return env


def install(shim_dir) -> str:
    """Write ``sitecustomize.py`` into ``shim_dir``; returns the directory."""
    from pathlib import Path
    path = Path(shim_dir)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    (path / "sitecustomize.py").write_text(SHIM_SOURCE, encoding="utf-8")
    return str(path)


def read_passes(path) -> list[dict]:
    """Pass records of a child, oldest first (the ``ready`` line is skipped)."""
    import json
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a partial last line while the child still writes
                if "root" in rec:
                    rows.append(rec)
    except OSError:
        return []
    rows.sort(key=lambda r: r["t0"])
    return rows


def with_models(passes: list[dict]) -> list[dict]:
    """Every pass record carrying ``model_now``: the last screen-model digest seen at or before it.

    The shim writes a digest only when it changed, so a pass without ``model`` still shows
    the previous one. ``None`` until the first digest.
    """
    current = None
    out = []
    for rec in passes:
        if rec.get("model") is not None:
            current = rec["model"]
        out.append({**rec, "model_now": current})
    return out


def self_check(passes: list[dict], role: str) -> dict:
    """A required target with no record at all means the shim is blind: fail, don't publish."""
    seen = {}
    for rec in passes:
        seen[rec["root"]] = seen.get(rec["root"], 0) + 1
        for name, seg in rec["seg"].items():
            seen[name] = seen.get(name, 0) + seg["n"]
    required = REQUIRED.get(role, ())
    missing = [name for name in required if not seen.get(name)]
    absent = sorted({f"{b}.{q}" for b, q in TARGETS} - set(seen) - set(required))
    return {"role": role, "required": list(required), "failures": missing,
            "absent": absent, "ok": not missing}


def shim_mode(path) -> str | None:
    """``monitoring`` or ``import-hook``, from the shim's own ready line."""
    import json
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("shim") == "ready":
                    return rec.get("mode")
    except OSError:
        pass
    return None
