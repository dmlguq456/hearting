"""Bridge reference drawing data and Fleet's typed explorer view model."""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

_SOURCE_MODULE = re.compile(r"mock\d+$")


def load_isolated(reference):
    """Load a reference renderer with source modules of its own directory only.

    The renderer's size and palette helpers reach its sibling modules by comparing directories, and
    ``load_reference`` only drops modules from the directory it loads. A copy loaded after another
    reference (the approved original) would otherwise import that one's modules and draw different
    rows.
    """
    from tools.fleet.tests.freeze_explorer import load_reference
    for name in [name for name in sys.modules if _SOURCE_MODULE.match(name)]:
        del sys.modules[name]
    path = Path(reference)
    renderer = load_reference(path)
    renderer.path = path
    return renderer


def extract_view_model(renderer, *, scrub=False):
    """Extract the complete reference data table, including non-VM drawing literals."""
    from tools.fleet.tests.freeze_explorer import _fixture_values
    values = copy.deepcopy(_fixture_values(renderer))
    if scrub:
        from tools.fleet.tests.freeze_explorer import sanitize_reference
        sanitize_reference(renderer)
        values = copy.deepcopy(_fixture_values(renderer))
    else:
        # Parity evidence needs the hand VM's original values. _fixture_values also serves the
        # public scrubbed fixture, so restore its width-redacted source records only in this
        # private in-memory extraction path.
        values.update({
            "attempts": {**copy.deepcopy(getattr(renderer, "_RAW", {})), "route": "f5"},
            "nodes": _jsonable(copy.deepcopy(getattr(renderer, "ROUTE_NODES", {}))),
            "cards": copy.deepcopy(getattr(renderer, "HDATA", {})),
            "dialogue": {"logs": copy.deepcopy(getattr(renderer, "LOGS", {})),
                         "generation_text": copy.deepcopy(getattr(renderer, "GEN_TEXT_0C", {}))},
            "peer_nodes": copy.deepcopy(getattr(renderer, "PEER_PROJECTS", ())),
            "peer_edges": {"links": copy.deepcopy(renderer.mock6.LINKS),
                           "cards": _peer_cards(renderer),
                           "cross": copy.deepcopy(getattr(renderer, "PEER_CROSS", ())),
                           "side_lanes": copy.deepcopy(getattr(renderer, "PEER_SIDE_LANES", ()))},
            "compute": copy.deepcopy(renderer.mock3.COMPUTE),
            "memory": copy.deepcopy(getattr(renderer.mock4, "MEMORY", ())),
            "questions": copy.deepcopy(renderer.mock4.QUESTIONS),
            "answered": copy.deepcopy(renderer.mock4.ANSWERED),
            "events": copy.deepcopy(renderer.mock3.FEED),
        })
        seats = [list(row) for row in renderer.seats_by_repo()]
        generations = {}
        for seat in seats:
            while len(seat) < 5:
                seat.append(False)
            chain = renderer.chain(seat[0])
            cards = [item for item in chain if item.get("card")]
            seat.append(str(renderer.card_title(cards[-1]) or "") if cards else "")
            seat.append(copy.deepcopy(renderer.LINEAGE.get(seat[0], ())))
            generations[seat[0]] = [{
                "harness": item.get("h"), "sid": str(item.get("sid") or ""),
                "at": item.get("at"), "kind": item.get("kind"),
                "connection": item.get("conn"), "note": item.get("note"),
                "card": bool(item.get("card")), "detail": True,
                "title": str(renderer.card_title(item) or ""),
            } for item in chain]
        values["seats"], values["generations"] = seats, generations
    # Keep the reference's independent data tables available for an exact in-memory round trip.
    # This private token is never serialized into public fixtures.
    modules = {}
    for name in ("mock2", "mock3", "mock4", "mock5", "mock6", "mock9", "mock12"):
        module = getattr(renderer, name, None)
        if module is not None:
            modules[name] = {key: copy.deepcopy(value) for key, value in vars(module).items()
                             if key.isupper() and _jsonish(value)}
    globals_copy = {key: copy.deepcopy(value) for key, value in vars(renderer).items()
                    if key.isupper() and _jsonish(value)}
    values = _jsonable(values)
    # The private snapshot keeps the tables as the reference holds them: the palette has a None
    # key and style tuples a drawing helper looks up, which a JSON-shaped copy would change.
    values["_reference_snapshot"] = {"globals": globals_copy, "modules": modules}
    return values


def _peer_cards(renderer):
    """The owner-stage rows the reference hand-writes under each PEERS card (``cards[tag].owners``)."""
    cards = {}
    for tag, owners in getattr(getattr(renderer, "mock5", None), "OWN", {}).items():
        cards[tag] = {"owners": [
            {"state": state, "label": label,
             "stages": [{"name": name, "state": stage_state} for name, stage_state in stages]}
            for state, label, stages in owners]}
    return cards


def _jsonish(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, dict):
        return all(_jsonish(k) and _jsonish(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return all(_jsonish(item) for item in value)
    return False


def _jsonable(value, *, raw_keys=False):
    """Copy evidence values without retaining renderer callbacks or live objects.

    ``raw_keys`` keeps dictionary keys as they are (the private snapshot's palette has a ``None``
    key a drawing helper looks up); public values stringify them.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {(key if raw_keys else str(key)): _jsonable(item, raw_keys=raw_keys)
                for key, item in value.items() if not callable(item)}
    if isinstance(value, set):
        return sorted((_jsonable(item, raw_keys=raw_keys) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        items = [_jsonable(item, raw_keys=raw_keys) for item in value if not callable(item)]
        return items
    return None


def inject_model_catalog(renderer, catalog):
    """Replace mock choices from an authenticated runtime catalog observation."""
    if not isinstance(catalog, dict) or not isinstance(catalog.get("providers"), dict):
        raise ValueError("catalog requires provider-keyed model entries")
    choices, shown = {}, {}
    for harness, entries in catalog["providers"].items():
        if harness not in ("claude", "codex", "opencode") or not isinstance(entries, list):
            continue
        choices[harness] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            model_id = str(entry["id"])
            choices[harness].append(model_id)
            if entry.get("version"):
                shown[model_id] = str(entry["version"])
    renderer.MODEL_CHOICES = choices
    renderer.MODEL_SHOWN = shown
    renderer.MAIN_ONLY = tuple(str(item) for item in catalog.get("main_only", ()))
    return {"providers": sorted(choices), "model_counts": {key: len(value) for key, value in choices.items()},
            "main_only_count": len(renderer.MAIN_ONLY)}


def inject_view_model(renderer, vm, *, catalog=None):
    """Inject an extracted reference VM into its isolated renderer namespace.

    Exact extraction round trips restore the independently loaded module tables. For a
    serialized Fleet VM, fields without a safe reference representation fail explicitly.
    """
    values = dict(vm)
    snapshot = values.pop("_reference_snapshot", None)
    if snapshot is not None:
        for key, value in snapshot["globals"].items():
            setattr(renderer, key, copy.deepcopy(value))
        for name, fields in snapshot["modules"].items():
            module = getattr(renderer, name, None)
            if module is not None:
                for key, value in fields.items():
                    setattr(module, key, copy.deepcopy(value))
        injected = sorted(snapshot["globals"])
        if catalog is not None:
            inject_model_catalog(renderer, catalog)
        return {"injected": injected, "unresolved": []}
    catalog = catalog or (values.get("models") or {}).get("catalog")
    unsupported = sorted(set(values) - {"identity", "clock", "hosts", "usage", "census", "repos",
                                        "sessions", "routes", "nodes", "attempts", "questions",
                                        "answered", "seats", "generations", "cards", "dialogue",
                                        "peer_nodes", "peer_edges", "compute", "memory", "models",
                                        "config", "system", "events", "pane_graph", "source_records"})
    if unsupported:
        raise ValueError("unsupported view-model fields: " + ", ".join(unsupported))
    if catalog is not None:
        inject_model_catalog(renderer, catalog)
    unresolved = ["PROJECTS/session source rows", "route rows and lineage", "peer graph layout",
                  "mock-specific drawing literals"]
    return {"injected": [], "unresolved": unresolved}


def round_trip(approved, reference, widths=(140, 168), rows=45, states=None, except_view=("MODELS",)):
    """Draw the manifest states from the approved original and from the cycle copy fed the
    approved data, each from its own freshly loaded modules; return the per-state difference counts.

    This is ``parity_compare round-trip`` with the isolation ``load_isolated`` documents (the CLI
    loads both through ``load_reference`` and shares the approved modules with the copy).
    """
    import json
    from tools.fleet.tests.freeze_explorer import capture, sanitize_reference
    from tools.fleet.tests.parity_compare import _state_args, diff_frames
    approved, reference = Path(approved).resolve(), Path(reference).resolve()
    manifest = json.loads((Path(__file__).parent / "fixtures/explorer/manifest.json").read_text())
    baseline = load_isolated(approved)
    sanitize_reference(baseline)
    vm = extract_view_model(baseline)
    results = []
    for width in widths:
        for name in [name for name in (states or manifest.get("states", ())) if name not in except_view]:
            expected = capture(approved, width, renderer=load_isolated(approved), **_state_args(name))
            candidate = load_isolated(reference)
            inject_view_model(candidate, vm)
            actual = capture(reference, width, renderer=candidate, **_state_args(name))
            differences = diff_frames(expected, actual)
            results.append({"state": name, "width": width, "mismatch_count": len(differences),
                            "mismatches": differences[:20]})
    return {"schema": "fleet-round-trip-v1", "state_count": len({row["state"] for row in results}),
            "widths": list(widths), "diff_count": sum(row["mismatch_count"] for row in results),
            "results": results}


# -- a live view model into the copy's hand tables ----------------------------------------------------

def _tup(value):
    """JSON lists back to the tuples the drawing code unpacks and compares."""
    if isinstance(value, (list, tuple)):
        return tuple(_tup(item) for item in value)
    return value


def _put(target, value):
    """Replace a table in place, so every module that imported it by name sees the new rows."""
    if isinstance(target, list):
        target[:] = value
    elif isinstance(target, dict):
        target.clear()
        target.update(value)
    return target


_STATES = ("working", "idle", "blocked", "done", "dead")


def _state(value):
    """The copy knows five states; Fleet's board draws any other word (unknown, stale) as an idle dot."""
    return value if value in _STATES else "idle"


def _session_row(row):
    model = row.get("model")
    return dict(tag=row["tag"], steward=bool(row.get("steward")), h=row.get("harness") or "claude",
                m=_tup(model) if model else None, e=row.get("effort"), st=_state(row.get("state")),
                title=row.get("title") or "", ctx=row.get("context"),
                win=row.get("window") if row.get("window") is not None else 1.0,
                now=row.get("now") or "", age=row.get("age") or "·", **({"ask": row["ask"]} if row.get("ask") else {}))


def _route_record(row):
    stages = [dict(name=s.get("name") or s.get("n"), st=s.get("st") or "todo", el=s.get("el"), h=s.get("h"),
                   m=_tup(s["m"]) if s.get("m") else None, e=s.get("e"), gate=bool(s.get("gate")),
                   **{k: s[k] for k in ("r", "host", "par") if k in s}) for s in row.get("stages") or ()]
    legs = [tuple(leg) for leg in row.get("legs") or ()] or [("route", "active", None)]
    record = dict(tag=row.get("session_tag") or "", pick=row["id"], st=row.get("state") or "idle",
                  h=row.get("harness") or "claude", m=_tup(row["model"]) if row.get("model") else None,
                  e=row.get("effort"), title=row.get("title") or "", legs=legs, stages=stages,
                  age=row.get("age") or "·", loops=row.get("loops"), leg_no=len(legs))
    if row.get("activity"):
        record["act"] = row["activity"]
    if row.get("tree_label"):
        record["tree_label"] = row["tree_label"]
    if row.get("resume"):
        record["resume"] = row["resume"]
    return record


def inject_live(renderer, vm):
    """Fill every hand table the copy draws from with a live Fleet view model.

    The copy keeps its 22:53 look for 22:53 data: this only replaces the rows, never the drawing code,
    and each table a live capture does not carry becomes an empty one, not the hand rows.
    """
    vm = dict(vm)
    m2, m3, m4, m5, m6, m12 = (getattr(renderer, name) for name in ("mock2", "mock3", "mock4", "mock5", "mock6", "mock12"))
    repos = vm.get("repos") or ()
    sessions = [_session_row(row) for row in vm.get("sessions") or () if row.get("tag")]
    by_repo = {}
    for source, row in zip([s for s in vm.get("sessions") or () if s.get("tag")], sessions):
        by_repo.setdefault(source.get("repo_id") or source.get("repo") or "·", []).append(row)
    projects = [(repo.get("name") or repo.get("id"), repo.get("done"), repo.get("age") or "·",
                 by_repo.pop(repo.get("id") or repo.get("name"), [])) for repo in repos]
    projects += [(name, None, "·", rows) for name, rows in by_repo.items()]
    _put(m3.PROJECTS, projects)
    _put(m5.SESS, {s["tag"]: s for _n, _d, _a, rows in projects for s in rows})
    renderer.PROJECT_OF.clear()
    renderer.PROJECT_OF.update({s["tag"]: name for name, _d, _a, rows in projects for s in rows})
    renderer.SESS_ORDER[:] = [s["tag"] for _n, _d, _a, rows in projects for s in rows]

    _put(m12.HOSTS, [(h.get("mark") or "◇", h.get("name"), h.get("key") or "host") for h in vm.get("hosts") or ()])
    _put(m3.USAGE, [(u.get("text"), u.get("key"), [_tup(w) if w else None for w in u.get("windows") or ()])
                    for u in vm.get("usage") or ()])
    _inject_routes(renderer, vm)
    _inject_peers(renderer, vm, sessions)
    _inject_rest(renderer, vm, sessions)
    _inject_history(renderer, vm)
    _inject_models(renderer, vm.get("models"))
    renderer.ROOT_STATUS.update(_status(renderer, vm, 0))
    return {"injected": sorted(renderer.mock2.LIVE), "unresolved": []}


def _deep(value):
    """Lists to tuples all the way down (the drawing code unpacks them); dictionaries keep their keys."""
    if isinstance(value, dict):
        return {key: _deep(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_deep(item) for item in value)
    return value


def _segments(parts):
    return [tuple(part) if isinstance(part, (list, tuple)) else part for part in parts or ()]


def _stage(stage):
    out = dict(stage)
    if isinstance(out.get("who"), (list, tuple)):
        out["who"] = tuple(out["who"])
    for key in ("rounds", "qa"):
        if key in out:
            out[key] = [_tup(item) for item in out[key] or ()]
    if "group" in out:
        out["group"] = [_stage(item) for item in out["group"]]
    return out


def _node(node, renderer):
    """A route node as the copy's drawing reads it: segment lines are lists of (text, style) tuples."""
    out = dict(node)
    out["title"] = _segments(node.get("title"))
    out["lines"] = [_segments(line) for line in node.get("lines") or ()]
    out["detail"] = [_stage(stage) for stage in node.get("detail") or ()]
    if node.get("kind") == "leg" and out["detail"]:
        # the hand table's first leg line is a callable (`stage <leg> n/m`), which a serialized node cannot carry
        out["lines"].insert(0, renderer._leg_line(out["detail"]))
    out["loops"] = [_tup(item) for item in node.get("loops") or ()]
    return out


_NO_WORKER = ("worker", [], [])


def _inject_routes(renderer, vm):
    m5 = renderer.mock5
    rows, kinds, shapes, workers, past, ctx = [], {}, {}, {}, {}, {}
    for row in vm.get("routes") or ():
        pick = row["id"]
        record = _route_record(row)
        worker = row.get("worker")
        rows.append(dict(pick=pick, repo=row.get("repo") or "·", **{"from": row.get("session_tag") or None},
                         r=record, worker=_deep(worker) if worker else _NO_WORKER, wt=row.get("worktree"),
                         sub=_tup(row["subworker"]) if row.get("subworker") else None))
        kinds[pick] = (row.get("capability") or "·", row.get("mode"), row.get("intensity"), row.get("profile"))
        shapes[pick] = row.get("shape") or "·"
        workers[pick] = (row.get("worker_stage") or "·", row.get("worker_unit"))
        if row.get("past_from"):
            past[pick] = row["past_from"]
        if row.get("owner_context"):
            ctx[pick] = _tup(row["owner_context"])
    _put(renderer.ROUTE_ROWS, rows)
    _put(renderer.ROUTE_KIND, kinds)
    _put(renderer.ROUTE_SHAPE, shapes)
    _put(renderer.WORKER_KIND, workers)
    _put(renderer.PAST_FROM, past)
    _put(renderer.OWNER_CTX, ctx)
    nodes = vm.get("nodes") or {}
    _put(renderer.ROUTE_NODES, {pick: [_node(node, renderer) for node in nodes.get(pick) or ()] for pick in kinds})
    heads, after = {}, {}
    source = {row["id"]: row for row in vm.get("routes") or ()}
    for z in rows:
        r, row = z["r"], source[z["pick"]]
        title = row.get("detail_title") or r["title"]
        trailer = row.get("detail_trailer") or "%s · %s" % (r["age"], renderer.nof(len(r["legs"]), "leg"))
        heads[z["pick"]] = [(z["repo"] + "/", "title_dim"), (" › ", "line")] + \
            (renderer.tagp(z["from"]) + [(" › ", "line")] if z["from"] else []) + \
            [(title, "head"), ("  " + trailer, "dim")]
    for z in rows:
        before = z["r"]["legs"][:-1]
        note = source[z["pick"]].get("detail_after")
        if note:
            after[z["pick"]] = note
        elif before:
            after[z["pick"]] = "before this leg: %s — outside this snapshot" % " · ".join(
                name + " " + renderer.STATE_MARK[{"active": "active"}.get(state, state)][0] + (" " + progress if progress else "")
                for name, state, progress in before)
    _put(renderer.ROUTE_HEAD, heads)
    _put(renderer.ROUTE_AFTER, after)
    renderer.REC_OF.clear()


def _links(vm):
    edges = vm.get("peer_edges") or {}
    out = []
    for link in edges.get("links") or ():
        out.append([_tup(item) if isinstance(item, list) and item and isinstance(item[0], str) and len(item) == 2
                    and item[1] in ("claude", "codex", "opencode") else item for item in link])
    return out


def _inject_peers(renderer, vm, sessions):
    m5, m6 = renderer.mock5, renderer.mock6
    edges = vm.get("peer_edges") or {}
    _put(m6.LINKS, _links(vm))
    _put(renderer.PEER_PROJECTS, [(repo, [list(col) for col in cols]) for repo, cols in vm.get("peer_nodes") or ()])
    _put(renderer.PEER_CROSS, [tuple(item) for item in edges.get("cross") or ()])
    renderer.PEER_SIDE_LANES.clear()
    renderer.PEER_SIDE_LANES.update(tuple(item) for item in edges.get("side_lanes") or ())
    own = {}
    for tag, card in (edges.get("cards") or {}).items():
        own[tag] = [(o.get("state"), o.get("label"), [(st.get("name"), st.get("state")) for st in o.get("stages") or ()])
                    for o in card.get("owners") or ()]
    _put(m5.OWN, own)
    _put(m6.OWN, {k: [(st, kind, [dict(name=n, st=t) for n, t in stages]) for st, kind, stages in v] for k, v in own.items()})
    system = vm.get("system") or {}
    extra, extra6 = {}, {}
    owner = (system.get("unregistered_gpu_summary") or {}).get("owner")
    held = {(p.get("host"), p.get("gpu")) for p in system.get("gpu_processes") or () if p.get("session_id") == owner}
    if owner and held:
        extra[owner] = [("GPU ×%d" % len(held), "dim")]
        extra6[owner] = ("GPU ×%d" % len(held), "gpu")
    for q in vm.get("questions") or ():
        text = "answer needed · %s" % q.get("wait")
        extra[q["tag"]] = [(text, "blocked")]
        extra6[q["tag"]] = ("● " + text, "ask")
    _put(m5.EXTRA, extra)
    _put(m6.EXTRA, extra6)
    if m5.SESS:      # one box width for the whole graph, from the longest title (import-time in the copy)
        renderer.BOX_W = min(44, max(28, max(renderer.width(z["title"]) for z in m5.SESS.values()) + 4))
    inside = {}
    for tag, owners in own.items():
        inside[tag] = [(state, label, "%d/%d" % (sum(1 for _n, t in stages if t == "done"), len(stages)) if stages else "")
                       for state, label, stages in owners]
    system = vm.get("system") or {}
    owner = (system.get("unregistered_gpu_summary") or {}).get("owner")
    held = {(p.get("host"), p.get("gpu")) for p in system.get("gpu_processes") or () if p.get("session_id") == owner}
    if owner and held:
        inside.setdefault(owner, []).append((None, "GPU ×%d" % len(held), ""))
    _put(renderer.INSIDE, inside)


def _inject_rest(renderer, vm, sessions):
    m2, m3, m4, m5, m9 = renderer.mock2, renderer.mock3, renderer.mock4, renderer.mock5, renderer.mock9
    m4.QUESTIONS[:] = [dict(tag=q["tag"], h=q.get("h") or "claude", m=_tup(q["m"]) if q.get("m") else None,
                            e=q.get("e"), kind=q.get("kind") or "decision", wait=q.get("wait") or "·",
                            project=q.get("project") or "·", title=q.get("title") or "", pane=q.get("pane") or "·",
                            now=q.get("now") or "") for q in vm.get("questions") or ()]
    m4.ANSWERED[:] = [(repo, [tuple(item) for item in rows]) for repo, rows in vm.get("answered") or ()]
    _put(m9.GIT, {})
    renderer.BRANCH_OF.clear()
    for row in vm.get("sessions") or ():
        renderer.BRANCH_OF[row["tag"]] = row.get("branch") or ""
    _put(renderer.SESSION_PANE, {row["tag"]: row["pane"] for row in vm.get("sessions") or () if row.get("pane")})
    working = sum(1 for row in vm.get("sessions") or () if row.get("state") == "working")
    idle = sum(1 for row in vm.get("sessions") or () if row.get("state") == "idle")
    asking = sum(1 for row in vm.get("sessions") or () if row.get("state") == "blocked" or row.get("ask"))
    system = vm.get("system") or {}
    install = system.get("install") or {}
    live = m2.LIVE
    live.clear()
    live["census"] = (working, idle, asking)
    live["version"] = install.get("version") or "·"
    live["watch"] = {w.get("repo"): w.get("text") for w in (vm.get("peer_edges") or {}).get("watches") or ()}
    when = {}
    for up, down in (vm.get("peer_edges") or {}).get("side_lanes") or ():
        pair = []
        for a, b in ((up, down), (down, up)):
            hit = next((l for l in (vm.get("peer_edges") or {}).get("links") or ()
                        if isinstance(l[0], list) and isinstance(l[1], list) and l[0][0] == a and l[1][0] == b), None)
            pair.append(str(hit[3]).replace(" ago", "") if hit else "·")
        when[(up, down)] = tuple(pair)
    live["lane_when"] = when
    live["handover_mark"] = {row[0]: "gen %d · %s" % (row[2] - 1, str(row[3]).split("· ")[-1].replace(" ago", ""))
                              for row in vm.get("seats") or () if "· " in str(row[3]) and str(row[3]).endswith("ago")}
    clock = vm.get("clock")
    if isinstance(clock, (int, float)):
        import datetime
        live["clock"] = datetime.datetime.fromtimestamp(clock).strftime("%H:%M")
    dispatch = system.get("dispatch") or {}
    delivery = system.get("peer_delivery") or {}
    summary = system.get("unregistered_gpu_summary") or {}
    usage_api = system.get("usage_api") or {}
    live["system"] = dict(
        version=install.get("version") or "·", managed=bool(install.get("managed")), pinned=bool(install.get("pinned")),
        distribution=bool(install.get("distribution")), runtimes=tuple(system.get("runtimes") or ()),
        usage_state=usage_api.get("state") or "·", usage_fresh=str(usage_api.get("freshness") or "·"),
        active=dispatch.get("governor_active", "·"), cap=dispatch.get("governor_cap", "·"),
        dispatch=system.get("dispatch_count", "·"), jobs=dispatch.get("jobs", "·"),
        spin=dispatch.get("resource_running", 0), running=dispatch.get("resource_running", "·"),
        exited=dispatch.get("resource_exited", "·"), pending=str(delivery.get("pending", "·")),
        obligations=[(o.get("tag"), o.get("text"), o.get("age") or "") for o in system.get("obligations") or ()],
        records=str(delivery.get("records", "·")), gpu_count=summary.get("count", len(system.get("gpu_processes") or ())),
        gpu_owner=summary.get("owner") or "·",
        gpu_rows=[tuple(row) for row in (system.get("unregistered_gpu_processes") or ())[:2]])
    answered_total = system.get("answered_total")
    rows = (vm.get("answered") or [[None, []]])[0]
    live["answered_head"] = "%s%d · %s decision records in all projects" % (
        (rows[0] + " ") if rows[0] else "", len(rows[1]), answered_total if answered_total is not None else sum(len(r[1]) for r in vm.get("answered") or ()))
    _put(m3.FEED, [tuple(e) for e in vm.get("events") or ()])
    _put(m3.COMPUTE, [_deep(host) for host in vm.get("compute") or ()])
    for module in (m4, renderer.mock12, m9):
        if hasattr(module, "COMPUTE") and module.COMPUTE is not m3.COMPUTE:
            _put(module.COMPUTE, list(m3.COMPUTE))
    _put(m4.MEMORY, [(repo, [tuple(item) for item in rows]) for repo, rows in vm.get("memory") or ()])
    procs = {}
    for p in system.get("gpu_processes") or ():
        try:
            gb = float(str(p.get("vram", "0")).split()[0])
        except ValueError:
            gb = 0.0
        procs.setdefault((p.get("host"), p.get("gpu")), []).append(
            dict(tag=p.get("session_id"), repo=p.get("cwd") or "·", cmd=p.get("command") or "", gb=gb,
                 age=p.get("age") or "·", unreg=not p.get("registered")))
    _put(renderer.GPU_PROCS, procs)
    renderer.ROOT_STATUS.update(_status(renderer, vm, working))


def _status(renderer, vm, working):
    sessions, routes, questions = vm.get("sessions") or (), vm.get("routes") or (), vm.get("questions") or ()
    n = lambda k, w: "%d %s%s" % (k, w, "" if k == 1 else "s")
    system = vm.get("system") or {}
    seats = renderer.mock5.SEATS
    hosts = vm.get("compute") or ()
    gpus = [g for h in hosts for g in h[5]]
    busy = sum(1 for g in gpus if g[1] == "working")
    links = (vm.get("peer_edges") or {}).get("links") or ()
    pending = (system.get("peer_delivery") or {}).get("pending", "·")
    return {
        "OVERVIEW": "%s · %s · %s" % (n(len(sessions), "session"), n(len(routes), "route"), n(len(questions), "question")),
        "PEERS": "%s · %s · %s" % (n(len(links), "link"), n(sum(1 for s in sessions if s.get("steward")), "steward"),
                                    n(sum(1 for link in links if len(link) > 2 and link[2] == "handover"), "handover")),
        "ROUTES": "%d open · %s" % (sum(1 for r in routes if r.get("state") != "done"),
                                     n(sum(1 for r in routes if r.get("state") == "dead"), "stopped leg")),
        "QUESTIONS": "%d waiting · %s today" % (len(questions), n(system.get("answered_total", len(questions)), "decision record")),
        "HISTORY": "%s · %s" % (n(len(seats), "seat"), n(sum(z[2] for z in seats), "generation")),
        "COMPUTE": "%s · GPU %d/%d" % (n(len(hosts), "host"), busy, len(gpus)),
        "MEMORY": "today %s added · %s expired" % ((system.get("memory_summary") or {}).get("added_today", "·"),
                                                   (system.get("memory_summary") or {}).get("expired", "·")),
        "SYSTEM": "%s · %s pending" % ((system.get("install") or {}).get("version", "·"),
                                       ("1 delivery" if pending == 1 else "%s deliveries" % pending)),
    }


def _inject_history(renderer, vm):
    """Seats, their generation marks and the card text the copy keeps per seat."""
    m5 = renderer.mock5
    seats = [list(row) for row in vm.get("seats") or ()]
    _put(m5.SEATS, [tuple(row[:5]) if len(row) >= 5 else tuple(row[:4]) for row in seats])
    _put(renderer.LINEAGE, {row[0]: [tuple(mark) for mark in (row[6] if len(row) > 6 else ())] for row in seats})
    _put(renderer.SEAT, {row[0]: tuple(row) for row in m5.SEATS})
    _put(renderer.HDATA, {tag: _deep_lists(block) for tag, block in (vm.get("cards") or {}).items()})
    generations = vm.get("generations") or {}
    _put(renderer.NOW_SID, {tag: (items[-1].get("sid") if items else "") for tag, items in generations.items()})
    dialogue = vm.get("dialogue") or {}
    _put(renderer.LOGS, dict(dialogue.get("logs") or {}))
    _put(renderer.GEN_TEXT_0C, dict(dialogue.get("generation_text") or {}))
    _put(renderer.UI, {})
    renderer.mock2.LIVE["seat_title"] = {row[0]: row[5] for row in seats if len(row) > 5 and row[5]}


def _deep_lists(value):
    return value


def _inject_models(renderer, models):
    """The MODELS tables from a model-config read; absent a read, the copy keeps its hand tables."""
    if not isinstance(models, dict):
        return
    live = renderer.mock2.LIVE
    renderer.PROFILES[:] = list(models.get("profiles") or ())
    _put(renderer.PROFILE_MAP, {h: {p: (_tup(cell) if cell else None) for p, cell in cells.items()}
                                for h, cells in (models.get("profile_map") or {}).items()})
    _put(renderer.MODEL_CHOICES, {h: list(items) for h, items in (models.get("model_choices") or {}).items()})
    _put(renderer.MAIN_ONLY, {h: list(items) for h, items in (models.get("main_only") or {}).items()})
    renderer.EFFORTS[:] = list(models.get("efforts") or renderer.EFFORTS)
    _put(renderer.POOLS, {p: (_deep(pool) if pool else None) for p, pool in (models.get("pools") or {}).items()})
    _put(renderer.EFF_KEY, dict(models.get("effort_keys") or renderer.EFF_KEY))
    _put(renderer.MODEL_SHOWN, dict(models.get("shown") or {}))
    _put(renderer.CTX, dict(models.get("context") or renderer.CTX))
    _put(renderer.CTX_HOW, dict(models.get("context_source") or renderer.CTX_HOW))
    if models.get("tidy"):
        renderer.TIDY = tuple(models["tidy"])
    _put(renderer.KTOK, dict(models.get("token_units") or renderer.KTOK))
    allocation = models.get("allocation") or {}
    if allocation.get("fields"):
        live["allocation_fields"] = [(label, [tuple(part) for part in parts]) for label, parts in allocation["fields"]]
    if allocation.get("note"):
        live["allocation_note"] = allocation["note"]
