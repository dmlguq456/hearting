"""Capability route: execution scope, campaign choice and the compose card.

Entry execution scopes, start approvals, compose campaign selection and
slugs, and the compose observation, omission and decision lines.

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import artifact_locator as ARTIFACT_LOCATOR
import route_identity as ROUTE_IDENTITY
import route_plan as ROUTE_PLAN
import route_authority as ROUTE_AUTHORITY
import owner_write_advisory as OWNER_WRITE_ADVISORY
import resource_resume as RESOURCE_RESUME

from capability_route_base import (
    canonical_routes_dir, ORDER, route_hash, TOPO, UI_TEXT,
)

from capability_route_dispatch import (
    _workflow_contract,
)

from capability_route_nodes import (
    _frame_node, _no_model_node,
)

from capability_route_compose import (
    _compose_spec_read_notice, _turn_peer_source, ROUTE_FRAME_CAPABILITY, shape_for_intensity,
)


def route_plan_execution_scope(binding):
    """The start choice every leg of one frame decision carries.

    A leg keeps `complete` only when each start approval it declares was given for that leg in the
    same interview; a part not approved there (one held for the person included) keeps its gate.
    `report` carries as it is, except to a later staged leg that would have no step left before its
    first approval (that leg keeps its gate).
    """
    if not isinstance(binding, dict) or type(binding.get("index")) is not int:
        return None
    approvals = (((binding.get("record") or {}).get("decision") or {}).get("approvals") or {})
    scope = approvals.get("execution_scope")
    if scope not in ("complete", "report") or (binding["index"] == 0 and scope == "report"):
        return scope
    legs = binding.get("legs")
    if not isinstance(legs, list) or not 0 <= binding["index"] < len(legs) or not isinstance(legs[binding["index"]], dict):
        return None
    leg = legs[binding["index"]]
    if scope == "report":
        return scope if leg.get("shape") != "staged" or project_entry_execution_scope(leg, "report").get("graph") else None
    given = {(row.get("key"), row.get("leg")) for row in approvals.get("given") or []
             if isinstance(row, dict) and row.get("accepted") is True}
    return scope if all((key, binding["index"]) in given for key, _part in declared_start_approvals(leg)) else None


def _bind_entry_execution_scope(route, scope):
    """Seal the already-selected entry scope and remove only the matching new-route preview wait."""
    if scope not in ("complete", "report"):
        return route
    route = json.loads(json.dumps(route))
    route["entry_execution_scope"] = scope
    route["entry_scope_contract_version"] = 1
    _project_entry_scope_nodes(route.get("nodes") or [], scope)
    route["human_gate_bindings"] = [row for row in route.get("human_gate_bindings") or []
                                    if row.get("gate") != "preview-disposition"]
    route["human_gates"] = sorted({row["gate"] for row in route["human_gate_bindings"]})
    route["workflow_contract"] = _workflow_contract(
        TOPO.load_registry(), route.get("nodes") or [], route["human_gate_bindings"])
    route["route_hash"] = route_hash(route)
    route["route_id"] = ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
    return route


def valid_entry_execution_scope_marker(version, scope):
    """Whether optional scope metadata uses the one supported, typed contract."""
    return (version is None and scope is None) or (type(version) is int and version == 1
                                                   and scope in ("complete", "report"))


def _project_entry_scope_nodes(nodes, scope):
    if scope not in ("complete", "report"):
        return nodes
    for node in nodes:
        if "inline_human_gates" in node:
            remaining = [gate for gate in node.get("inline_human_gates", []) if gate != "preview-disposition"]
            if remaining:
                node["inline_human_gates"] = remaining
            else:
                node.pop("inline_human_gates", None)
        if node.get("continuation") == {"kind": "human-gate", "gate": "preview-disposition"}:
            node["continuation"] = {"kind": "inline-next"}
    return nodes


def project_entry_execution_scope(leg, scope, registry=None):
    """Copy a proposed leg and, for report, stop a staged graph before its first catalog approval.

    Catalog part ids are resolved before cutoff, so borrowed namespaced tokens share exactly the
    same behavior as host stage ids. An explicit graph is only narrowed; an omitted graph expands
    from its base recipe before the same cutoff is applied.
    """
    result = json.loads(json.dumps(leg))
    if scope != "report" or result.get("shape") != "staged":
        return result
    registry = registry or TOPO.load_registry()
    capability = result.get("capability")
    recipes = [r for r in registry.get("recipes", []) if r.get("capability") == capability]
    mode = result.get("mode")
    base = next((r for r in recipes if mode in r.get("modes", [])), None) if mode else next(
        (r for r in recipes if "dev" in r.get("modes", [])), recipes[0] if recipes else None)
    if base is None:
        return result
    graph = list(result.get("graph") or [n["id"] for n in base.get("standard_plus", {}).get("nodes", [])
                                          if not _frame_node(n)])
    known = {row.get("capability") for row in registry.get("recipes", [])}
    catalog = TOPO.part_catalog(registry).get("parts", {})
    end = len(graph)
    for index, token in enumerate(graph):
        head, sep, tail = str(token).partition(":")
        if sep and head in known:
            part_id = f"{head}:{tail.partition(':')[0]}"
        else:
            part_id = f"{capability}:{head}"
        if (catalog.get(part_id) or {}).get("start_approval"):
            end = index
            break
    result["graph"] = graph[:end]
    return result


def declared_start_approvals(leg, registry=None):
    """`(start_approval, part id)` pairs a proposal leg declares in the part catalogue.

    Read from the catalogue alone (no compile, no probe), so anything rendered from it is the same on
    every replay. Only a staged leg carries stage parts.
    """
    registry = registry or TOPO.load_registry()
    capability = leg["capability"]
    recipes = [r for r in registry["recipes"] if r["capability"] == capability and capability != ROUTE_FRAME_CAPABILITY]
    if not recipes:
        return []
    mode = leg.get("mode")
    base = (next((r for r in recipes if "dev" in r["modes"]), recipes[0]) if mode is None
            else next((r for r in recipes if mode in r["modes"]), None))
    if base is None:
        return []
    if RESOURCE_RESUME.selected(capability, mode or "setup", leg.get("graph")):
        return []
    if leg.get("shape") != "staged":
        # A one-shot recipe still contains these internal approval-scoped parts.
        # Keep the same catalogue keys so the entry choice can carry its scope.
        internal = {
            ("autopilot-lab", "setup"): ("full-run", "autopilot-lab:full-run"),
            ("autopilot-ship", "default"): ("deploy", "autopilot-ship:deploy"),
            ("autopilot-refine", None): ("preview", "autopilot-refine:transaction"),
            ("autopilot-apply", None): ("handback", "autopilot-apply:handback"),
        }
        selected = internal.get((capability, mode)) or internal.get((capability, None))
        return [selected] if selected and TOPO.part_row(registry, selected[1]).get("start_approval") else []
    known = {r["capability"] for r in registry["recipes"]} - {ROUTE_FRAME_CAPABILITY}
    tokens = leg.get("graph") or [n["id"] for n in base["standard_plus"]["nodes"] if not _frame_node(n)]
    rows = []
    for token in tokens:
        head, _, rest = token.partition(":")
        part = f"{head}:{rest.partition(':')[0]}" if rest and head in known else f"{base['capability']}:{head}"
        approval = TOPO.part_row(registry, part).get("start_approval")
        if approval:
            rows.append((approval, part))
    return rows


COMPOSE_CAMPAIGN_LIST_CAP = 12


def compose_campaign_summaries(artifact_root):
    """Active campaigns of the root, newest first.  A fresh root has none
    (empty list); a root that cannot be read returns None so the caller says
    "unavailable" rather than mislabelling a join as a creation."""
    try:
        import artifact_producer
        return artifact_producer.list_campaign_summaries(
            Path(artifact_root), active_only=False, include_dispositions=False)
    except (OSError, ImportError, ValueError):
        return None


CWD_CAMPAIGN_SCAN_ROUTES = 100
CWD_CAMPAIGN_KEYS_SHOWN = 3
_CAMPAIGN_ROWS_UNSET = object()


def cwd_campaign_keys(artifact_root, cwd, rows=_CAMPAIGN_ROWS_UNSET):
    """The active work streams of this folder, newest first: the campaign keys of the newest
    routes under the artifact root that were sealed for `cwd` (at most `CWD_CAMPAIGN_SCAN_ROUTES`
    route files are read, and the scan stops at `CWD_CAMPAIGN_KEYS_SHOWN` keys)."""
    rows = compose_campaign_summaries(artifact_root) if rows is _CAMPAIGN_ROWS_UNSET else rows
    active = {r.get("key") for r in rows or [] if r.get("state") == "active"} - {None, "_unassigned"}
    here, keys = os.path.realpath(str(cwd)), []
    try:
        with os.scandir(canonical_routes_dir(artifact_root)) as entries:
            files = sorted((entry for entry in entries if entry.name.startswith("rt-") and entry.name.endswith(".json")),
                           key=lambda entry: entry.stat().st_mtime, reverse=True)[:CWD_CAMPAIGN_SCAN_ROUTES]
    except OSError:
        return []
    for path in files:
        if len(keys) >= CWD_CAMPAIGN_KEYS_SHOWN:
            break
        try:
            route = json.loads(Path(path.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = route.get("campaign_key") if isinstance(route, dict) else None
        if (key in active and key not in keys and isinstance(route.get("cwd"), str)
                and os.path.realpath(route["cwd"]) == here):
            keys.append(key)
    return keys


def first_campaign_key(artifact_root, cwd):
    """The work stream a root without any active one opens: named after the project folder.

    With nothing to choose from, the refusal only made the first person to start work in a
    new repository type `--campaign-key <folder>` (W6-E). A root that already has an active
    stream, or whose campaigns cannot be read, returns None and keeps the refusal that lists
    them. The folder is the one that owns the artifact root (the primary checkout for a
    linked worktree), else `cwd`; the word is the route presence gate's `--campaign-key`."""
    rows = compose_campaign_summaries(artifact_root)
    if rows is None or any(r.get("state") == "active" and r.get("key") not in (None, "_unassigned")
                           for r in rows):
        return None
    root = Path(artifact_root)
    folder = root.parent if root.name in (".agent_reports", ".claude_reports") else Path(cwd)
    key = re.sub(r"[^a-z0-9]+", "-", folder.name.lower()).strip("-")[:40] or "work"
    # A closed stream of that name reopens, as `--campaign-key <folder>` would; one that cannot
    # be joined (abandoned, superseded, ambiguous) keeps the refusal, which lists the streams.
    try:
        import artifact_producer
        if artifact_producer.classify_campaign_key(rows, key).get("mode") == "blocked":
            return None
    except (ImportError, AttributeError, TypeError, ValueError):
        return None
    return key


def compose_campaign_hint(artifact_root, cwd=None):
    rows = compose_campaign_summaries(artifact_root)
    if rows is None:
        shown = "unavailable (campaign scan failed)"
    else:
        keyed = [r for r in rows if r.get("key") not in (None, "_unassigned")]
        shown = ", ".join(f"{r['key']}({r['cycle_count']})" for r in keyed[:COMPOSE_CAMPAIGN_LIST_CAP])
        if len(keyed) > COMPOSE_CAMPAIGN_LIST_CAP:
            shown += f", … +{len(keyed) - COMPOSE_CAMPAIGN_LIST_CAP}"
    here = cwd_campaign_keys(artifact_root, cwd, rows) if cwd is not None and rows is not None else []
    return ((f"this folder's streams: {', '.join(here)}; " if here else "")
            + f"name the work stream with --campaign-key <existing|new> (active: {shown or 'none'}); "
            "--unassigned keeps the work in the root's degraded _unassigned container")


def compose_campaign_selection(route):
    """What the sealed campaign choice means in this artifact root (read-only)."""
    key = route.get("campaign_key")
    parent = route.get("parent_cycle_id")
    rows = compose_campaign_summaries(route["artifact_root"])
    unavailable = rows is None
    rows = rows or []
    active = [r for r in rows if r.get("state") == "active" and r.get("key") not in (None, "_unassigned")]
    selection = {"key": key, "active_count": len(active),
                 "active_keys": [r["key"] for r in active[:COMPOSE_CAMPAIGN_LIST_CAP]],
                 "active_keys_unavailable": unavailable}
    if key is not None:
        import artifact_producer
        choice = artifact_producer.classify_campaign_key(rows, key)
        match = next((r for r in rows if r.get("campaign_id") == choice.get("campaign_id")), None)
        mode = "unresolved" if unavailable else choice["mode"]
        selection.update(mode=mode, campaign_id=match["campaign_id"] if match else None,
                         title=match["title"] if match else None,
                         **({"blocked_reason": choice["code"]} if choice["mode"] == "blocked" else {}))
    elif parent is not None:
        selection.update(mode="parent", parent_cycle_id=parent)
    else:
        selection.update(mode="unassigned", explicit=route.get("campaign_unassigned") is True)
    return selection


def compose_resolve_campaign_key(artifact_root, key):
    """`(key, given)`: a key no campaign carries, but that names exactly one active
    campaign by its folder, id or spelling, joins that campaign (`given` keeps what
    was typed); any other key is returned unchanged with `given` None.

    Only an exact key used to join, so a folder name (`2026-09-04_prd-v25`) or a
    near spelling quietly opened a duplicate stream (BC 2026-09-04)."""
    rows = compose_campaign_summaries(artifact_root)
    if not rows or any(r.get("key") == key for r in rows):
        return key, None

    def norm(value):
        try:
            return ARTIFACT_LOCATOR.slugify(ARTIFACT_LOCATOR.strip_leading_date(str(value or "")))[0]
        except ARTIFACT_LOCATOR.LocatorError:
            return ""

    wanted = norm(key)
    matches = {r["key"] for r in rows
               if r.get("state") == "active" and r.get("key") not in (None, "_unassigned")
               and (key in (r.get("locator"), r.get("campaign_id"))
                    or (wanted and wanted in (norm(r.get("key")), norm(r.get("locator")))))}
    return (matches.pop(), key) if len(matches) == 1 else (key, None)


COMPOSE_SLUG_WORDS = 6


def compose_default_slug(task_text, *, capability=None, shape=None):
    """The slug a compose names from its own task, so `--slug` is optional.

    The readable stem comes from request wording. A digest of the complete
    nonempty request remains at the end, even when the display stem is clipped,
    so similar and non-Latin requests retain separate deterministic names."""
    text = str(task_text or "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10] if text.strip() else ""
    words = []
    ignored = {"a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "into",
               "is", "it", "of", "on", "or", "the", "to", "with", "work", "task", "please"}
    for line in text.splitlines()[:80]:
        candidate = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)+", "", line)
        candidate = candidate.lstrip("# ").strip()
        if (not candidate or re.match(r"^\d{4}-\d{2}-\d{2}(?:\b|_)", candidate)
                or any(token in candidate for token in (".agent_reports/", ".claude_reports/", "/home/"))):
            continue
        found = re.findall(r"[A-Za-z][A-Za-z0-9]*", candidate)
        words.extend(word for word in found
                     if word.lower() not in ignored and (len(word) > 1 or word.isupper()))
        if len(words) >= COMPOSE_SLUG_WORDS:
            break
    base = ARTIFACT_LOCATOR.slugify("-".join(words[:COMPOSE_SLUG_WORDS]))[0] if words else ""
    if capability or shape:
        prefix = ARTIFACT_LOCATOR.slugify(str(capability or shape))[0]
        stem = "-".join(part for part in (prefix, base) if part)
    else:
        stem = base or "work"
    if not stem:
        stem = "work"
    if not digest:
        return ARTIFACT_LOCATOR.slugify(stem)[0]
    suffix = "-" + digest
    max_stem = ARTIFACT_LOCATOR.MAX_SLUG_LENGTH - len(suffix)
    stem = re.sub(r"-+", "-", stem.lower()).strip("-")[:max_stem].rstrip("-") or "work"
    return ARTIFACT_LOCATOR.slugify(stem + suffix)[0]


def _compose_campaign_line(selection, t=None):
    t = t or UI_TEXT.translator()
    mode = selection["mode"]
    if mode == "join":
        text = t("route.campaign.join", key=selection["key"])
    elif mode == "create":
        text = t("route.campaign.create", key=selection["key"])
    elif mode == "reopen":
        text = t("route.campaign.reopen", key=selection["key"])
    elif mode == "blocked":
        text = t("route.campaign.blocked", key=selection["key"], reason=selection["blocked_reason"])
    elif mode == "parent":
        text = t("route.campaign.parent", parent=selection["parent_cycle_id"])
    elif mode == "unresolved":
        text = t("route.campaign.unresolved", key=selection["key"])
    else:
        text = t("route.campaign.unassigned")
    shown = ", ".join(selection["active_keys"][:6])
    if selection["active_count"] > 6:
        shown += " …"
    return "  " + t("route.campaign.line", text=text, count=selection["active_count"]) + (f": {shown}" if shown else "")


def route_start_approvals(route, registry=None):
    """Start-approval marks of the parts a route carries (SD-165; stage-dispatch 13.63.7).

    A declaration for the card, the frame catalog and the interview -- never a
    gate, fence or wait. A borrowed node seals its mark; a host's own stage is
    looked up in the part catalog as `<capability>:<stage>`.
    """
    registry = registry or TOPO.load_registry()
    rows, seen = [], set()
    for node in route.get("nodes") or []:
        borrowed = node.get("part")
        part = borrowed or f"{route.get('capability')}:{node.get('parallel_anchor') or node.get('id')}"
        approval = node.get("start_approval") if borrowed else TOPO.part_row(registry, part).get("start_approval")
        if approval and part not in seen:
            seen.add(part)
            rows.append({"node": node.get("parallel_anchor") or node.get("id"), "part": part,
                         "start_approval": approval, "borrowed": bool(borrowed)})
    if (route.get("selection") or {}).get("shape") in ("direct", "solo"):
        internal = {
            ("autopilot-lab", "setup"): ("full-run", "autopilot-lab:full-run"),
            ("autopilot-ship", "default"): ("deploy", "autopilot-ship:deploy"),
            ("autopilot-refine", None): ("preview", "autopilot-refine:transaction"),
            ("autopilot-apply", None): ("handback", "autopilot-apply:handback"),
        }
        key, part = internal.get((route.get("capability"), route.get("capability_mode"))) or internal.get(
            (route.get("capability"), None), (None, None))
        if key and part not in seen and TOPO.part_row(registry, part).get("start_approval"):
            rows.append({"node": "one-shot", "part": part, "start_approval": key, "borrowed": False})
    return rows


def compose_observations(route, t=None):
    """Display facts already known by compose; never changes routing or approval."""
    from session_identity import identity
    who = identity()
    origin = _turn_peer_source(who.harness, who.session_id, route.get("cwd")) if who.known and who.session_id else "unattributed"
    shape = route.get("selection", {}).get("shape") or shape_for_intensity(route["effective_intensity"])
    peer = origin.startswith("peer:") and shape in {"direct", "solo"}
    external = route.get("capability") == "autopilot-ship" and route.get("entry_execution_scope") != "report"
    confirmation = {"method": "peer-notice" if peer and not external else "user-card", "origin": origin,
                    "meaning": "display only; existing approval and destructive/external/user-requested card exceptions remain"}
    nodes = [n for n in route.get("nodes", []) if not _frame_node(n)]
    lab_run = any(n.get("kind") == "resource-runner" and
                  (route.get("capability") == "autopilot-lab" or
                   str(n.get("part", "")).startswith("autopilot-lab:")) for n in nodes)
    selection = route.get("selection") or {}
    evidence = route.get("dispatch_evidence") or {}
    native = evidence.get("native_subagent")
    t = t or UI_TEXT.translator()
    seen, unknown = t("route.obs.seen"), t("route.obs.unknown")
    native_label = seen if native else unknown
    headless = bool(evidence.get("tuples") or route.get("registered_headless_candidates"))
    answers = [
        (t("route.q.main_capability"), route["capability"]),
        (t("route.q.new_measurement"), t("route.a.lab_declared") if lab_run else t("route.a.depends_on_work")),
        ("standard+", t("route.a.yes") if route["effective_intensity"] in {"standard", "strong", "thorough", "adversarial"} else t("route.a.no")),
        (t("route.q.split_stages"), t("route.a.stages_declared", count=len(nodes)) if shape == "staged" else t("route.frame.pending") if shape == "framed" else t("route.a.single_run")),
        (t("route.q.inline_exception"), str(selection.get("inline_reason") or selection.get("selection_basis") or unknown) if shape == "direct" else t("route.a.not_applicable")),
        (t("route.q.delegation_surface"), t("route.a.surface", native=native_label, headless=seen if headless else unknown)),
        ("lineage·RUNLOG", t("route.a.lineage")),
    ]
    return {"confirmation": confirmation,
            "pre_execution_answers": [{"question": i, "name": name, "answer": value} for i, (name, value) in enumerate(answers, 1)]}


def compose_omission_lines(route):
    """Existing compose diagnostics only; no stage, profile or gate changes."""
    recipe = route.get("composed_recipe") or {}
    omissions = (recipe.get("compose") or {}).get("omitted_parallel_presets", [])
    if not omissions:
        return []
    lines = []
    for row in omissions:
        group = None
        try:
            registry = TOPO.load_registry()
            base = TOPO.resolve_recipe(registry, route["capability"], route["capability_mode"])
            anchor = next((n for n in route["nodes"] if n["id"] == row["id"]), {"id": row["id"]})
            source, node = TOPO.part_recipe(registry, anchor["part"]) if anchor.get("part") else (base, anchor)
            group = next((g for g in source["standard_plus"].get("parallel_groups", [])
                          if g["node"] == node["id"]), None)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # A historical registry gap cannot block compose/start.
        if group is None:
            continue  # Older diagnostic rows have no intensity hint; do not guess.
        if ORDER[route["effective_intensity"]] < ORDER[group["min_intensity"]]:
            continue  # This intensity did not select the declared preset.
        legs = " legs " + ",".join(row["legs"]) if row.get("legs") else ""
        lines.append(f"  {route['effective_intensity']} group {row['id']}{legs} dropped by --graph: {row['reason']}")
    return lines


def compose_decision_lines(route, non_git_decision=None, t=None):
    """Read only the sealed selection/nodes; same display for every harness."""
    import route_plan as RP

    t = t or UI_TEXT.translator()
    basis = {row.get("axis"): row for row in
             (route.get("selection") or {}).get("selection_basis", [])}
    intensity = basis.get("compose-intensity", {})
    source = intensity.get("source", "sealed-route")
    requested = intensity.get("signal", route.get("requested_intensity"))
    detail = t("route.intensity.unspecified") if source == "shape-default" else ""
    if requested and requested != route["effective_intensity"]:
        detail += f", requested={requested}"
    lines = [f"  intensity={route['effective_intensity']} (source={source}{detail})"]
    shape = (route.get("selection") or {}).get("shape") or shape_for_intensity(route["effective_intensity"])
    if source == "shape-default" and shape != "framed":
        lines.append("  " + t("route.intensity.hint"))
    capability = basis.get("compose-capability", {})
    if capability.get("source") == "compose-default":
        lines.append("  " + t("route.capability.compose_default", capability=route['capability']))
    elif capability.get("source") == "non-git-unresolved":
        lines.append("  " + t("route.capability.non_git_unresolved", capability=route['capability']))
    elif capability.get("source") == "non-git-task-classification":
        lines.append(f"  capability={route['capability']} (source=non-git-task-classification)")
    elif capability.get("source") == "frame-shape":
        lines.append("  " + t("route.capability.frame_pending"))
    elif not capability:
        label = t("route.frame.pending") if shape == "framed" else route["capability"]
        lines.append(f"  capability={label} (source=sealed-route)")
    profiles = {}
    nodes = route.get("nodes", [])
    for node in nodes:
        if _no_model_node(node):
            continue
        anchor = node.get("parallel_anchor") or node["id"]
        profile = (t("route.tier.inline") if node.get("execution_surface") == "inline"
                   else node.get("model_profile") or "unknown")
        profiles.setdefault(anchor, []).append(profile)
    lines.append("  tiers: " + (" ".join(f"{node}={'+'.join(values)}" for node, values in profiles.items()) or t("route.none")))
    workers = sum(n.get("dispatch_depth") == 2 or _frame_node(n) for n in nodes)
    owners = int(shape != "framed" and route["effective_intensity"] != "direct")
    resources = [n["id"] for n in nodes if n.get("kind") == "resource-runner"]
    size = "  " + t("route.size", nodes=len(nodes), workers=workers, owners=owners,
                      resources=",".join(resources) or t("route.none"))
    if shape == "framed":
        frame_basis = basis.get("frame-legs")
        if frame_basis:
            count = frame_basis.get("signal")
            source = frame_basis.get("source")
            reason = frame_basis.get("reason")
            label = t("route.size.frame_default") if source == "default" else reason
            size += t("route.size.frame_one" if str(count) == "1" else "route.size.frame_two", label=label)
        else:
            count = len(RP.frame_legs(route))
            size += t("route.size.frame_one" if count == 1 else "route.size.frame_two",
                      label=t("route.size.frame_legacy"))
        supported = ROUTE_AUTHORITY.depth1_supported_harnesses(route, worker_type="frame")
        pin = (((route.get("selection_pins") or {}).get("frame") or {}).get("harness"))
        if len(supported) == 1:
            size += t("route.size.same_only", harness=next(iter(supported)))
        elif pin and pin in supported:
            size += t("route.size.same_pinned", pin=pin)
    lines.append(size)
    for row in ((route.get("composed_recipe") or {}).get("compose") or {}).get("auto_completed_nodes", []):
        purpose = "owner closing summary" if row["reason"] == "terminal-anchor" else "registry consumer"
        lines.append(f"  {row['intensity']} group {','.join(row['anchors'])}: "
                     f"auto-added {row['id']} ({row['reason']}; {purpose})")
    return lines + compose_omission_lines(route)


def compose_card(route, plan=None, plan_source=None, *, owner_harness=None, route_plan_unreadable=False,
                 campaign_selection=None, include_decisions=True, non_git_decision=None, intent_original=None):
    """One-line `[경로]` notice the acting session pastes instead of a card."""
    shape = route.get("selection", {}).get("shape") or shape_for_intensity(route["effective_intensity"])
    ids = [node["id"] for node in route["nodes"]]
    framed = shape == "framed"
    graph = "→".join(ids) if ids else "-"
    t = UI_TEXT.translator()
    gates = ",".join(sorted({row["gate"] for row in route.get("human_gate_bindings") or []})) or t("route.none")
    card = (
        t("route.card.header", capability=route['capability'], shape=shape,
          intensity=route['effective_intensity'], graph=graph, route_id=route['route_id'], gates=gates)
        + f"\n  cwd {route['cwd']} · slug {route.get('slug', '-')}\n"
        + _compose_campaign_line(campaign_selection if campaign_selection is not None
                                 else compose_campaign_selection(route), t)
    )
    import work_intent
    for line in work_intent.card_lines(route, original=intent_original, t=t):
        card += "\n" + line
    observed = compose_observations(route, t)
    if include_decisions:
        card += "\n" + "\n".join(compose_decision_lines(route, non_git_decision, t))
    if non_git_decision and non_git_decision.get("kind") == "direct":
        card += "\n  " + t("route.card.non_git_direct")
        if non_git_decision.get("capability_unresolved"):
            card += "\n  " + t("route.card.capability_unresolved")
    card += "\n  " + t("route.card.confirmation", method=observed['confirmation']['method'],
                          origin=observed['confirmation']['origin'])
    for answer in observed["pre_execution_answers"]:
        card += f"\n  {answer['question']}. {answer['name']}: {answer['answer']}"
    if framed:
        pair = len([n for n in route.get("nodes", []) if _frame_node(n)]) == 2
        card += ("\n  " + t("route.card.frame_proposes")
                 + "\n  " + t("route.card.cost_two" if pair else "route.card.cost_one"))
    sourced = {}
    for node in route["nodes"]:
        for name, source in (node.get("input_sources") or {}).items():
            sourced.setdefault(name, source)
    if sourced:  # SD-163: inputs filled from a prior cycle
        source_dir = os.path.commonpath([str(Path(source["path"]).parent) for source in sourced.values()])
        card += "\n  " + t("route.card.inputs", names='·'.join(sourced),
                              cycles=','.join(sorted({s['cycle_id'] for s in sourced.values()})),
                              path=Path(route['artifact_root']) / source_dir)
    borrowed = list(dict.fromkeys(node["part"] for node in route["nodes"] if node.get("part")))
    if borrowed:  # SD-165: parts taken from another recipe, written under parts/<capability>/<stage>/
        card += "\n  " + t("route.card.borrowed", parts='·'.join(borrowed))
    if valid_entry_execution_scope_marker(route.get("entry_scope_contract_version"),
                                         route.get("entry_execution_scope")) and route.get("entry_scope_contract_version") == 1:
        card += "\n  " + t("route.card.scope", scope=route['entry_execution_scope'])
    else:
        for row in route_start_approvals(route):
            card += "\n  " + t("route.card.start_approval", approval=row['start_approval'], part=row['part'])
    from artifact_producer import route_cycle_for, cycle_dir, default_bucket, ProducerError
    try:
        record = route_cycle_for(Path(route["artifact_root"]), route)
    except (ProducerError, OSError, ValueError):
        record = None
    if record is not None:
        folder = cycle_dir(Path(route["artifact_root"]), record["campaign_id"], record["cycle_id"], record)
        card += "\n  " + t("route.card.output", path=folder.resolve() / 'artifacts' / default_bucket(route['capability']))
    if plan:
        suffix = t("route.card.plan_inherited") if plan_source == "inherited" else ""
        card += "\n  " + t("route.card.plan", plan=' › '.join(plan), suffix=suffix)
    leg_plan = ROUTE_PLAN.leg_plan(route)
    parallel = (leg_plan or {}).get("leg", {}).get("parallel") or []
    if parallel:
        card += "\n  " + t("route.card.split", items=" · ".join(parallel[:3]))
        if len(parallel) > 3:
            card += t("route.card.split_more", count=len(parallel) - 3)
    if route_plan_unreadable:
        card += "\n  " + t("route.card.plan_unreadable")
    notice = _compose_spec_read_notice(route, t)
    if notice:
        card += "\n" + notice
    for advisory in OWNER_WRITE_ADVISORY.advisories(route, owner_harness=owner_harness):
        card += "\n  " + advisory["message"]
    return card
