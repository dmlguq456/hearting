"""Fleet render: route cards, availability text, usage headers, and the legend.

Split out of `render.py`, which re-exports every name defined here.
"""
import os
import time
from collections.abc import Mapping
from .display import codex_credits
from .model import fmt_min, dash

from .render_cells import (
    _BADGE_TEXT, _clean_model, _clip_w, _cw, _dw, _ELAPSED_GLYPH, _flat_level, _gauge_segs,
    _GAUGE_W, _job_display_model, _pad, _pct_key, _USAGE_BADGE_TEXT,
)

from .render_stages import (
    _aged_text,
)

from .render_rows import (
    _clip_segs,
)

from .render_select import (
    _gpu_resources_for_session, _gpu_session_keys, _session_for_job,
)


_GATE_MARK = " ⊸"   # prd.md:308 — completion-gate PASSED. Never rendered for no-claim/absent.


def _append_segment(segs, text, key):
    """Append one styled fragment while keeping deliberate color boundaries intact."""
    if not text:
        return
    if segs and segs[-1][1] == key:
        segs[-1] = (segs[-1][0] + text, key)
    else:
        segs.append((text, key))


def _wrap_route_node(prefix, text, key, mark, max_width, continuation="  "):
    """Cell-safe, lossless wrapping for one opaque route node label.

    The gate marker remains an independent ``gate_t`` segment even when the node
    itself needs more than one line.
    """
    if max_width is None:
        row = []
        _append_segment(row, prefix, "dim")
        _append_segment(row, text, key)
        _append_segment(row, mark, "gate_t")
        return [row]

    width = max(1, int(max_width))
    rows = []
    row = []
    used = 0
    payload_chars = 0

    def start(line_prefix):
        nonlocal row, used, payload_chars
        row = []
        used = 0
        payload_chars = 0
        _append_segment(row, line_prefix, "dim")
        used += _dw(line_prefix)

    def flush():
        nonlocal row, used, payload_chars
        if row:
            rows.append(row)
        row = []
        used = 0
        payload_chars = 0

    start(prefix)
    for char in text:
        char_width = _cw(char)
        if payload_chars and used + char_width > width:
            flush()
            start(continuation)
        _append_segment(row, char, key)
        used += char_width
        payload_chars += 1

    if mark and used + _dw(mark) > width and payload_chars:
        # Keep the independent marker attached to at least one character of its
        # node instead of marooning it on a marker-only continuation line.
        node_text, node_key = row[-1]
        moved = node_text[-1]
        remaining = node_text[:-1]
        used -= _cw(moved)
        payload_chars -= 1
        if remaining:
            row[-1] = (remaining, node_key)
        else:
            row.pop()
        if payload_chars:
            flush()
        else:
            row = []
            used = 0
        start(continuation)
        _append_segment(row, moved, key)
        used += _cw(moved)
        payload_chars = 1
    _append_segment(row, mark, "gate_t")
    flush()
    return rows


def _route_job_row(job, max_width=None):
    """The active node's owning job, one compact row (prd.md:308's `└▸🚀 <slug> <harness>
    <model> <elapsed>` shape) — NOT the group view's full `_dispatch_row` grid (a route card
    is not a project group; its columns don't line up with one, and forcing them to would need
    a second name-width negotiation this view doesn't have). `max_width` (§5.5): the slug is
    the one field with no fixed budget elsewhere, so it yields first — same "the variable-width
    field clips, the fixed-shape fields never do" idiom as `_compact_dispatch_name`."""
    hn = _BADGE_TEXT.get(job.harness, "—") if job.harness else "—"
    model_txt = _clean_model(dash(_job_display_model(job))) or "—"
    eff = ("(%s)" % job.effort) if job.effort else ""
    tail = _ELAPSED_GLYPH + fmt_min(job.elapsed_min) if job.elapsed_min is not None else ""
    prefix = "     └▸🚀 "
    slug = job.slug or job.key or "?"
    fixed_bits = [b for b in (hn, model_txt, eff, tail) if b]
    if max_width is not None:
        fixed_w = _dw(prefix) + (2 * len(fixed_bits)) + sum(_dw(b) for b in fixed_bits)
        slug = _clip_w(slug, max(4, max_width - fixed_w))
    bits = [b for b in (slug, hn, model_txt, eff, tail) if b]
    return [(prefix + "  ".join(bits), "name_dim")]


def _route_card_l1(tag_bits, rid, done, total, route_elapsed, any_failed, arrow, term_width):
    """§5.5's L1 width ladder — "60열: L1 태그가 이미 20열이다 → intensity를 먼저 떨어뜨리는
    사다리" — same pick-first-fit idiom as `_prompt_variants` (render.py, F-27): try progressively
    shorter tag/detail combinations, keep the first that fits `term_width`. `tag_bits` drops
    right-to-left (intensity, then mode, capability always survives — it's the identity)."""
    def build(tags, show_elapsed, show_failed):
        segs = [("  " + arrow + " ", "dim"), ("[%s] " % "·".join(tags), "name_dim"),
                (rid, "lvl_r" if any_failed else "dim"),
                (' — %d/%d stages' % (done, total), "dim")]
        if show_elapsed and route_elapsed is not None:
            # prd.md:307 wrote this as "<n/m nodes> ⏳<경과>", and the v10 critic's worry
            # was that a bare "  15m" reads as a stray number glued onto "n/m nodes".
            # F-76 resolves that without the glyph: `_ELAPSED_GLYPH` is board-wide, so this
            # header now matches every other elapsed instead of matching a card-local icon,
            # and `fmt_min`'s spaced units ("5h 20m") are what mark it as a duration.
            segs += [("  " + _ELAPSED_GLYPH, "dim"), (fmt_min(route_elapsed), "dim")]
        if show_failed and any_failed:
            segs.append((' ⚠ stage failed', "lvl_r"))
        return segs

    ladder = [tag_bits]
    for n in range(len(tag_bits) - 1, 0, -1):
        ladder.append(tag_bits[:n])
    variants = [(ladder[0], True, True)]
    for tags in ladder[1:]:
        variants.append((tags, True, True))
    variants.append((ladder[-1], False, True))
    for tags, show_elapsed, show_failed in variants:
        segs = build(tags, show_elapsed, show_failed)
        if term_width is None or sum(_dw(t) for t, _k in segs) <= term_width:
            return segs
    # Failure is actionable even on a folded route. Reserve its suffix before
    # reducing the work name; elapsed is optional only when it does not fit.
    fixed = build(ladder[-1], False, True)
    fixed_w = sum(_dw(t) for t, _k in fixed) - _dw(rid)
    room = max(1, (term_width or fixed_w + _dw(rid)) - fixed_w)
    fixed[2] = (_clip_w(rid, room), fixed[2][1])
    return fixed


def _subagents_for_job(session_by_identity, job):
    """Prefer attempt-owned job evidence, then the exact pid/start child session."""
    direct = getattr(job, "subagents", None)
    if direct is not None:
        return direct
    session = _session_for_job(session_by_identity, job)
    return getattr(session, "subagents", None) if session is not None else None


def live_harnesses(sessions):
    """Return harnesses with live, top-level sessions eligible for usage refresh."""
    return {s.harness for s in (sessions or ())
            if getattr(s, "liveness", None) not in ("stale", "dead")
            and not getattr(s, "app_server", False)
            and not getattr(s, "is_child", False)
            and not getattr(s, "mem_worker", False)}


def _opencode_availability_text(snapshot):
    rows = snapshot.get("providers") if isinstance(snapshot, dict) else None
    rows = list(rows) if isinstance(rows, list) else []
    payload = snapshot.get("payload") if isinstance(snapshot, dict) else None
    extra = payload.get("provider_availability") if isinstance(payload, dict) else None
    if isinstance(extra, dict):
        rows.extend({"provider": key, **value} for key, value in extra.items()
                    if isinstance(value, dict))
    latest = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider")
        state = row.get("state")
        reason = row.get("reason")
        stamp = row.get("observed_at") or 0
        if provider and (provider not in latest or stamp >= (latest[provider].get("observed_at") or 0)):
            latest[provider] = row
    labels = {}
    for provider, row in latest.items():
        state, reason = row.get("state"), row.get("reason")
        if provider == "opencode-go":
            label = ("Go subscription missing" if reason == "subscription-missing" else
                     "Go credential expired" if reason == "credential-expired" else
                     "Go key rejected" if state == "unavailable" else
                     "Go credential valid" if state == "usable" else "Go unknown")
            labels["go"] = label
        elif provider == "opencode-go-entitlement":
            label = ("Go subscription missing" if reason == "subscription-missing" else
                     "Go entitlement valid" if state == "usable" else
                     "Go entitlement rejected" if state == "unavailable" else
                     "Go entitlement unknown")
            labels["entitlement"] = label
        elif provider in {"opencode", "openai"}:
            family = "Zen" if provider == "opencode" else "OpenAI"
            suffix = ("valid" if state == "usable" else
                      "unavailable" if state == "unavailable" else "unknown")
            labels[provider] = f"{family} {suffix}"
    for key, value in (("go", "Go unknown"), ("entitlement", "Go entitlement unknown"),
                       ("opencode", "Zen unknown"), ("openai", "OpenAI unknown")):
        labels.setdefault(key, value)
    return "; ".join(labels[key] for key in ("go", "entitlement", "opencode", "openai"))


def _usage_header_rows(sessions, layout="wide", now=None, api_disabled=False,
                       usage_snapshots=None, term_width=None):
    """Build account usage rows independently of the main line builder.

    F-51c: `api_disabled` (user opted out via FLEET_DISABLE=usage-api / --no-usage-api) is
    "the user turned it off" — distinct from unknown provider/account availability. A
    codex harness with no passive-tap value while opted out
    renders as an unknown gauge (blank track + `—`), never the source-absence sentence.
    Claude quota always comes from its admitted current-credential snapshot.
    """
    rl = {}
    for s in sessions or ():
        if s.harness == "claude":
            continue
        freshness = getattr(s, "_usage_freshness", None)
        if (s.rl_5h is not None or s.rl_7d is not None or s.rl_ms
                or getattr(s, "rl_windows", None) or freshness
                or getattr(s, "_usage_error", None)):
            cur = rl.get(s.harness)
            if cur is None or (s.mtime or 0) > (cur[3] or 0):
                rl[s.harness] = (s.rl_5h, s.rl_7d, s.rl_ms, s.mtime, s.rl_rs,
                                 getattr(s, "rl_windows", None), freshness,
                                 getattr(s, "_usage_error", None), None,
                                 getattr(s, "_usage_error_reason", None))
    # A quota snapshot is account-scoped.  It must remain visible when there is no current
    # process row for that harness (for example immediately after session cleanup).
    for harness, snapshot in (usage_snapshots or {}).items():
        if harness == "claude":
            continue
        if not isinstance(snapshot, dict):
            continue
        payload = snapshot.get("payload")
        if not isinstance(payload, dict):
            if harness == "opencode" and (snapshot.get("providers") or snapshot.get("error_observation")):
                payload = {}
            else:
                continue
        r5 = payload.get("rl_5h")
        r7 = payload.get("rl_7d")
        rms = payload.get("rl_ms")
        error_observation = snapshot.get("error_observation")
        err = payload.get("error") or (error_observation or {}).get("error")
        rwins = payload.get("rl_windows")
        if rwins is None:
            rwins = payload.get("windows")
        providers = snapshot.get("providers") or []
        if r5 is None and r7 is None and not rms and not rwins and not err and not providers:
            continue
        observed_at = snapshot.get("observed_at")
        rl[harness] = (
            r5, r7, rms, observed_at,
            (payload.get("rs_5h"), payload.get("rs_7d")), rwins,
            snapshot.get("freshness"), err, providers,
            payload.get("reason") or (error_observation or {}).get("reason"),
        )
    live = live_harnesses(sessions)
    claude_present = "claude" in (usage_snapshots or {}) or any(
        s.harness == "claude" for s in sessions or ())
    if not rl and not live and not claude_present:
        return []
    hs = [h for h in ("claude", "codex", "opencode") if h in rl or h in live or h == "claude" and claude_present]
    rows = []
    now = now if now is not None else time.time()
    claude_annotation = ""
    for idx, h in enumerate(hs):
        hn = _USAGE_BADGE_TEXT.get(h) or _BADGE_TEXT.get(h, h)
        row = [("  usage " if idx == 0 else "        ", "head"),
               (_pad(hn, 14), "hb_" + h if h in _BADGE_TEXT else "hb_other")]
        if h == "claude":
            from .explorer.live import _claude_usage_payload, _claude_usage_unavailable, _claude_usage_annotation
            snapshot = (usage_snapshots or {}).get("claude")
            payload, freshness, observed = _claude_usage_payload(snapshot, now)
            if payload is None:
                status = _claude_usage_unavailable(snapshot, now)
                row.append((status["text"].removeprefix("claude "), "dim"))
                rows.append((row, None))
                continue
            claude_annotation = _claude_usage_annotation(snapshot, now, freshness, observed)
            rl[h] = (payload["rl_5h"], payload["rl_7d"], payload["rl_ms"], observed,
                     (payload["rs_5h"], payload["rs_7d"]), None, freshness, None, None, None)
        if h not in rl:
            if api_disabled and h in ("claude", "codex"):
                # F-51c: opt-out ("user turned it off"), not opencode's structural absence —
                # render the same blank/unknown gauge shape other unknown values already use.
                row.append(("[", "dim"))
                row += [("·" * _GAUGE_W, "dim"), ("   —", "dim")]
                row.append(("]", "dim"))
            else:
                row.append(("Go usage unavailable" if h == "opencode"
                            else "no usage api — plan quota is console-only", "dim"))
            rows.append((row, None))
            continue
        r5, r7, rms, _mt, rrs, rwins, freshness, err, provider_rows, error_reason = rl[h]
        if (err in {"auth", "subscription-missing", "missing-credential", "unknown"}
                or h == "opencode" and err):
            if h == "opencode":
                reason = error_reason
                clause = {
                    "missing-credential": "Go credential not found",
                    "credential-not-found": "Go credential not found",
                    "credential-expired": "Go key expired",
                    "authentication-rejected": "Go key rejected",
                    "subscription-missing": "Go subscription missing",
                    "credential-store-unavailable": "credential store unreadable",
                }.get(reason, "usage unreadable")
                row.append(("Go usage unavailable · " + clause, "dim"))
                rows.append((row, None))
                continue
            detail = _opencode_availability_text({"providers": provider_rows})
            if err == "auth" and error_reason == "credential-expired" and "Go key expired" not in detail:
                detail = "Go key expired; " + detail
            elif err == "auth" and "Go key expired" not in detail and "Go key rejected" not in detail:
                detail = "Go key rejected; " + detail
            elif err == "subscription-missing" and "Go subscription missing" not in detail:
                detail = "Go subscription missing; " + detail
            elif err == "missing-credential":
                detail = "Go credential not found; " + detail
            row.append(("usage api — " + detail, "dim"))
            rows.append((row, None))
            continue
        rs5, rs7 = (rrs or (None, None))[0], (rrs or (None, None))[1]
        gauges = (list(rwins) if rwins else
                  [("5h", r5, rs5), ("week", r7, rs7)])
        if not rwins and h == "codex" and r5 is None:
            gauges = gauges[1:]
        gauges += [(lbl, v, None) for lbl, v in (rms or [])]
        if h == "opencode":
            gauges = [(lbl, value, reset) for lbl, value, reset in gauges
                      if {"7d": "week", "wk": "week", "mo": "month"}.get(
                          str(lbl).strip(), str(lbl).strip()) in {"5h", "week", "month"}]
            if not any(value is not None for _lbl, value, _reset in gauges):
                row.append(("Go usage unavailable", "dim"))
                rows.append((row, None))
                continue
        cells = [[], [], []]
        for lbl, value, reset in gauges:
            lbl = str(lbl).strip()
            lbl = {"7d": "week", "wk": "week", "mo": "month"}.get(lbl, lbl)
            slot = 0 if lbl == "5h" else 1 if lbl == "week" else 2
            cell = cells[slot]
            if cell:
                cell.append(("   ", None))
            cell.append((lbl + " ", "dim")); cell.append(("[", "dim"))
            if value is None:
                cell += [("·" * _GAUGE_W, "dim"), ("   —", "dim")]
            else:
                gauge = _gauge_segs(value, _GAUGE_W)
                if freshness == "stale":
                    empty_cells = _GAUGE_W - _dw(gauge[0][0])
                    gauge[1] = ("·" * empty_cells, "dim")
                cell += [(text, _flat_level(key)) for text, key in gauge]
                cell.append((" %3d%%" % value, _flat_level(_pct_key(value))))
            cell.append(("]", "dim"))
            if reset and reset > now:
                cell.append((" ↻ " + fmt_min(int((reset - now) / 60)), "dim"))
        rows.append((row, cells))
    # User 2026-10-07: align 5h | week | other across accounts, including empty
    # slots. Reset text shares each column's measured width; meters keep their shape.
    widths = [_GAUGE_W + 10, _GAUGE_W + 12, 0]
    for _row, cells in rows:
        if cells is not None:
            for slot, cell in enumerate(cells):
                widths[slot] = max(widths[slot], sum(_dw(t) for t, _k in cell))
    out = []
    for row, cells in rows:
        credit = None
        if any(key == "hb_codex" for _text, key in row):
            snapshot = (usage_snapshots or {}).get("codex")
            credit = codex_credits(snapshot.get("payload") if isinstance(snapshot, Mapping) else None)
        credit_tail = credit
        if cells is not None:
            if credit and _dw(credit) <= widths[0]:
                annotation = credit
                if cells[0]:
                    percent = next((text.strip() for text, _key in cells[0] if "%" in text), "—")
                    summary = "5h " + percent + " " + credit
                    if _dw(summary) <= widths[0]:
                        annotation = summary
                cells[0] = [(annotation, "dim")]
                credit_tail = None
            last = max((slot for slot, cell in enumerate(cells) if cell), default=-1)
            for slot in range(last + 1):
                cell = cells[slot]
                row += cell
                if slot < last:
                    used = sum(_dw(t) for t, _k in cell)
                    row.append((" " * (widths[slot] - used + 3), None))
            if claude_annotation and any(key == "hb_claude" for _text, key in row):
                row.append(("   " + claude_annotation, "dim"))
        if credit_tail:
            suffix = "   " + credit_tail
            if term_width is not None:
                row = _clip_segs(row, max(0, term_width - 1 - _dw(suffix)))[0]
            row.append((suffix, "dim"))
        # Only quota columns use the viewport fit; source-absence/error messages
        # keep their existing plain output (F-101 ledger-absent goldens).
        if cells is not None and term_width is not None:
            row = _clip_segs(row, term_width)[0]
        out.append(row)
    return out


def _known_parentless_support_job(job):
    """Registered support without a confirmed parent is project support.

    Classification uses the common worker role, never a particular helper/unit.
    Existing execution parent/route evidence keeps its normal classification.
    """
    metadata = getattr(job, "_registry_metadata", None) or {}
    return (getattr(job, "registered_worker", False)
            and getattr(job, "worker_type", None) == "support"
            and not any(getattr(job, key, None) for key in (
                "parent_sid", "parent_slug", "parent_cwd", "parent_attempt_id",
                "parent_managed_dir", "route_id", "owner_route_id"))
            and not any(metadata.get(key) for key in (
                "parent_sid", "parent_session_id", "parent", "parent_slug", "parent_cwd",
                "parent_worktree", "parent_attempt_id", "parent_managed_dir", "managed_sidecar_log",
                "route_id", "route_file", "owner_route_id", "owner_route_file")))


def _gpu_strip_keys(shown, gpu_resources, drawn_jobs=(), session_by_identity=None):
    """Entities whose F-88 GPU strip is actually drawn (F-104 dedup source).

    `shown` are the drawn sessions; `drawn_jobs` are the dispatch jobs the group loop
    draws (`_drawn_group_jobs`), whose strip is keyed by `_runtime_session_id` (or the
    exact pid/start child session) even when that hidden child session is not on the board.
    """
    keys = set()
    for s in shown:
        if getattr(s, "mem_worker", False):
            continue  # a mem row is drawn by _mem_row alone, without a GPU strip
        if _gpu_resources_for_session(s, gpu_resources):
            keys.update(key for key in _gpu_session_keys(s) if gpu_resources.get(key))
    for job in drawn_jobs:
        owner = job if _gpu_resources_for_session(job, gpu_resources) else None
        if owner is None and session_by_identity:
            job_session = _session_for_job(session_by_identity, job)
            if job_session is not None and _gpu_resources_for_session(job_session, gpu_resources):
                owner = job_session
        if owner is not None:
            keys.update(key for key in _gpu_session_keys(owner) if gpu_resources.get(key))
    return keys


def _jobs_unconfirmed(observations):
    return (observations or {}).get("jobs", {}).get("state") in ("failed", "stalled")


def _observation_lines(observations, term_width=None):
    lines = []
    for source, health in (observations or {}).items():
        if health.get("state") not in ("failed", "stalled"):
            continue
        name = "jobs.log" if source == "jobs" else source
        age = health.get("age")
        if health.get("last_success_at") is not None:
            age = time.monotonic() - health["last_success_at"]
        error = health.get("last_error") or "collection delayed"
        error = {"herdr 조회 불가": "herdr unavailable",
                 "pane 조회 불가": "pane unavailable"}.get(error, error)
        text = "  %s unknown · %s" % (name, error)
        lines.append([(_aged_text(text, time.time() - age if age is not None else None,
                                  term_width), "lvl_y")])
    return lines


def _reading_legend(term_width, session=True):
    rows = []
    if session:
        rows.extend(_wrap_legend([('  session: ', "dim"), ('⠹ working  ● idle  ◑ blocked  ✕ dead', "dim")], term_width))
    for text in ('  project: ● active',
                 '  resource: ● running · ◇ unknown · UTIL/VRAM · ↳ command',
                 "  stage: ● current  ✓ done  ✕ failed  ○ planned"):
        rows.extend(_wrap_route_node("", text, "dim", "", term_width, continuation="  "))
    return rows


def _wrap_legend(legend, term_width):
    """F-97d follow-up (c) — wrap the legend across multiple lines at `term_width`
    instead of appending it as one flat, silently-overflowing line. Breaks only
    between whole glyph+label pairs (never mid-pair), indents continuation lines
    with the same leading `'  '` the first line opens with, and rstrips the
    trailing separator spaces at each line's end. `term_width=None` (hermetic/
    legacy callers with no terminal) keeps the single-line legacy shape."""
    if not term_width or not legend:
        return [legend] if legend else []
    lead = legend[0]
    pairs, i = [], 1
    while i < len(legend):
        # A Fleet-number label uses four color segments but remains one entry.
        size = 4 if i + 3 < len(legend) and legend[i][0] == "[" and legend[i + 2][0] == "]" else 2
        pairs.append(legend[i:i + size])
        i += size
    wrapped = []
    current = [lead]
    current_w = _dw(lead[0])
    for pair in pairs:
        pair_w = sum(_dw(t) for t, _k in pair)
        if len(current) > 1 and current_w + pair_w > int(term_width):
            wrapped.append(current)
            current = [("  ", None)]
            current_w = 2
        current.extend(pair)
        current_w += pair_w
    wrapped.append(current)
    if len(wrapped) == 1:
        # No wrap actually happened — byte-identical to the pre-wrap single line
        # (the common case at 100/120/168 cols and at 60 cols for the primary
        # legend, which measures 52 ≤ 60 and never needed to break at all).
        return wrapped
    out = []
    for line in wrapped:
        last_text, last_key = line[-1]
        stripped = last_text.rstrip()
        if stripped:
            line = line[:-1] + [(stripped, last_key)]
        else:
            line = line[:-1]
        out.append(line)
    return out


# ---------- plain (--once) ----------
def _plain(segs):
    if segs is None:
        return ""
    out = []
    for t, _ in segs:
        if _is_fill(t):
            if t[1] in _TINT_CHARS or t[1] == "!":
                continue                       # tint/bold sentinel — no visible text
            out.append("─────" if t == _HFILL else "   ")
        else:
            out.append(t)
    return "".join(out)


def _collect_memory():
    return _collect_memory_observed()[0]


def _collect_memory_observed(*, include_backlog=False):
    # F-19: best-effort — source health is separate from a successful empty read.
    try:
        if os.environ.get("FLEET_DEMO"):
            from . import demo
            value = demo.memory_snapshot()
        else:
            from .collectors import memory as memcol
            value = memcol.collect(include_summary=False, include_backlog=include_backlog)
        return value, "success" if value is not None else "absent"
    except Exception:
        return None, "failed"


def _collect_project_status(sessions):
    """Status documents for the sessions' projects (collectors.project_status); None in demo or on failure."""
    if os.environ.get("FLEET_DEMO"):
        return None
    try:
        from .collectors import project_status
        return project_status.collect(getattr(session, "cwd", None) for session in sessions or ())
    except Exception:
        return None


def _collect_governor():
    try:
        from .collectors import governor
        return governor.collect()
    except Exception:
        return None


def _snapshot_line(segs, colored=False, colors=256):
    if not colored or segs is None:
        return _plain(segs)
    out = []
    for text, key in segs:
        piece = _plain([(text, key)])
        if key == "tag_steward":
            style = "\033[1;38;5;219m" if colors >= 256 else "\033[1;35m"
            piece = style + piece + "\033[0m"
        elif key in {"gpu_legacy", "gpu_legacy_active"}:
            style = "\033[38;5;137m" if colors >= 256 else "\033[33m"
            piece = style + piece + "\033[0m"
        out.append(piece)
    return "".join(out)


def _malformed():
    try:
        from .collectors import dispatch
        return getattr(dispatch.collect, "last_malformed", 0)
    except Exception:
        return 0


# fill sentinels (3-char \x00<fill>\x00): everything after is right-aligned to the edge; the gap
# is filled with <fill> — space for _RFLUSH (invisible), ─ for _HFILL (a full-width rule).
_INSET = 2                  # Panel outer margin in columns.
_PAD_IN = 2                 # Extra inner padding between tint edge and content.
# Roles that make a line a full-width chrome BAR. A line is a bar when its FIRST segment says
# so — so any new bar variant must be registered here or the band silently never paints.
_BAR_ROLES = ("hdr_bar", "hdr_warn")
_HFILL = "\x00─\x00"

# row-tint sentinels (round-5 — herdr-style panel tints): a LEADING sentinel marks the whole
# row's background level. b/c = group body/cap · B/C = the ACTIVE-group variants (brighter,
# Active directories receive the stronger tint; ``i`` marks the intelligence zone.
_TINT_BODY, _TINT_CAP = "\x00b\x00", "\x00c\x00"
_TINT_BODY_HOT, _TINT_CAP_HOT = "\x00B\x00", "\x00C\x00"
_TINT_BODY_COOL = "\x00k\x00"    # Cooling body between active blue and inactive grey.
_TINT_INTEL = "\x00i\x00"
_TINT_CHARS = {"b", "c", "B", "C", "k", "i"}

# row-bold marker (user 2026-07-03, after the whole-row tint-brightening attempt was rejected —
# Main-session rows use bold rather than brightening the entire background.
# any other row in the card, font weight carries the distinction). Inserted AFTER any tint
# sentinel (so tint detection in _addline is unaffected) by the group-loop post-pass.
_ROW_BOLD = "\x00!\x00"
# 256-color background levels per sentinel char. Base panels retain the
# established range; the whole ladder moves down another restrained step while the
# cap remains visible and hot stays slightly above the base body.
_TINT_LVL = {"b": 233, "c": 236, "B": 234, "C": 234, "k": 233, "i": 233}


def _is_fill(t):
    return len(t) == 3 and t[0] == "\x00" and t[2] == "\x00"


def _painted_hit_spans(painted):
    """Return only non-space cells from successful _addline writes."""
    spans = []
    for x0, x1, text in painted or ():
        leading = len(text) - len(text.lstrip())
        trailing = len(text) - len(text.rstrip())
        start = x0 + _dw(text[:leading])
        end = x1 - (_dw(text[len(text) - trailing:]) if trailing else 0)
        if start < end:
            spans.append((start, end))
    return spans


def _hit_at(spans, x):
    return any(x0 <= x < x1 for x0, x1 in (spans or ()))


def _clamp_offset(off, total, body_h):
    return max(0, min(off, max(0, total - body_h)))


def _entry_id(e):
    return (e.get("pid"), e.get("proc_start"))


def _click_target_excluded(e):
    """§4.2.1 — the mouse path's exclusion check. `_CLICK_ROWS` is filled from `_SELECTABLE`
    (unfiltered), so exclusion is applied HERE, once, at click time — not every tick. The
    render.py:2207 contract ("filtered before a prompt") still holds: a click IS the selection
    attempt, so an excluded row is refused before it can ever reach `_PROMPT`."""
    from . import control
    pid = e.get("pid")
    if not pid:
        return True
    try:
        return control.is_excluded(pid)
    except Exception:
        return True                      # unresolvable → fail closed, same as _live_targets()
