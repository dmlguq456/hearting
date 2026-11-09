"""Render layer — curses cwd-project-group TUI (live) + plain snapshot (--once). PRD §4 v3.

Both paths build the same flat segment-line list ([(text, color_key), ...] per line, None =
blank line) via `_build_lines` — the plain renderer joins the text (for piping / smoke tests,
no ANSI), the curses renderer paints each segment through a scrollable viewport. Missing cells
render as '—' (never blank). Layout: one group per project (cwd); each session (🛰️ command-center
icon if it spawned children) is followed immediately by its nested `└▸🚀` child dispatch jobs
(joined via `parent_sid`/`CLAUDE_CODE_SESSION_ID`); jobs with no on-screen parent surface as
project-level `(orphan)` rows, cron loop jobs surface flat with no orphan marker, and a group
with no live sessions and no dispatch jobs folds to a single `+N folded` summary (toggle via
`a`/click, same as `+N hidden`). Responsive: narrow (<~70 cols) drops low-priority fields;
badge/slug/liveness never drop.

Module-global state invariants (single-process / single-thread only — no concurrent `_draw`):
  - `_OFFSET` (scroll offset) is READ in exactly ONE place: `_draw` (the viewport slice
    `lines[_OFFSET:_OFFSET+body_h]`). `_build_lines` must NEVER read `_OFFSET` — this is what
    guarantees the plain/`--once` path (which calls `_build_lines` directly) can never drop
    top lines.
  - Resize safety = re-clamp `_OFFSET` against the new `body_h` on every wake via
    `_clamp_offset`, NOT reset. Do NOT reset `_OFFSET` on KEY_RESIZE — that would destroy the
    user's scroll position on every resize.
  - `reset_scroll()` (public, called by fleet.py) sets `_OFFSET=0` — belt-and-suspenders for
    the single-process-per-launch model (a fresh process already starts at 0; only load-bearing
    if `run_live` were ever called twice in one process).
  - `_TOGGLE_ROWS` is reset at the TOP of `_draw`, before any early-return / short-circuit, so a
    stale toggle map never survives to the next click.

Module map. This file keeps the module-global UI state listed above (scroll
offset, click/toggle/fold maps, colour tables, explorer state, blink phase, ...)
and every function that reads or rebinds it, including `_build_lines`, `_draw`
and `_loop`. Stateless row and text builders live in sibling modules in this
package; each imports only from the ones above it in this list, and this file
re-exports every name they define:

    render_cells     glyphs, constants, shared row cells
    render_stages    stage segments
    render_rows      dispatch/session rows, frame lines, diagnostics rows
    render_select    peer links, resource rows
    render_status    execution detail, context chips, version segments
    render_gpu       GPU process and run rows
    render_explorer  explorer remote observation, question bindings
    render_routes    route cards, availability text, usage headers, legend
    render_input     prompt buttons, curses input

A test that replaces a name with `mock.patch` must patch the module whose code
looks the name up; patching it here reaches only the code in this file and
callers that go through `render.<name>`. `split_module_fixture.patch_split`
replaces a name in this file and every part that binds it.
"""
import glob
import json
import math
import os
import re
import shlex
import sys
import threading
import time
import unicodedata
from collections import Counter, deque
from collections.abc import Mapping
from .display import label as _user_label, project as _user_project, gpu_owner as _gpu_owner_label
from .display import codex_credits

from .model import (fmt_min, dash, project_of, exec_child_is_wait,
                    current_background_shell_jobs, session_parent_visible,
                    current_attempt_rank)
from . import gitinfo, titles
from .config import ui_text
from .palette import _HUE_OF, hue_colors
from .work_titles import readable as _readable_name, resource_name as _resource_name, resource_title_key, subject_command
from .collectors import compute_hosts as _compute_hosts
from .refresh import LiveSnapshot, RefreshPump, background_read, as_snapshot
from .session_handle import sanitize_title as _sanitize_session_title
from .session_handle import current_session_subject as _current_session_subject
from .session_handle import _cell_width as _session_handle_cell_width
from .session_handle import clip_cells as _clip_cells
from .session_handle import resolve_tag as _resolve_session_tag
from .session_handle import session_id_for_address as _session_id_for_address
from .session_handle import session_id_for_derived_name as _session_id_for_derived_name


# Re-exported so every name this module defined before the split stays an
# attribute of `render` (callers, older releases and tests use them).
from .render_cells import (  # noqa: F401
    _A_B, _A_BOLD, _A_D, _A_DIM, _A_REVERSE, _ALIAS_FAMILIES, _ALIAS_MODEL_CACHE, _badge_cell,
    _BADGE_KEY, _BADGE_TEXT, _BAR_EMPTY, _BAR_FULL, _blink_phase, _BR_CACHE, _BR_TTL,
    _BRANCH_SUFFIX_W, _catalog_model_records, _claude_catalog_paths, _clean_model, _clip_w,
    _context_gauge_track, _COOL_FILLED, _COOL_RING, _COOL_WINDOW_MIN, _CTX_TRACK_MAX,
    _CTX_TRACK_WINDOW, _cw, _DEFAULT_EFFORT_MARK, _DETACHED_GLYPH, _DIAL_MAX,
    _dispatch_display_model, _drop_past_stages, _dw, _eff_key, _EFF_SHORT, _effort_text, _EFW,
    _ELAPSED_GLYPH, _EMPTY_EFFORT, _FAMILIES, _FAMILY_PAIR_START, _flat_level, _gauge_segs,
    _GAUGE_W, _git_branch, _glyph, _GLYPH_KEY, _half_up, _harness_model_cell, _HDR_BAR_PAIR,
    _HDR_BLK_PAIR, _HDR_WARN_PAIR, _HMW, _HW, _INLINE_ARTIFACT_STAGES, _INTERACTION_LABEL,
    _IO_UNSET, _job_display_model, _JOB_LIVE_RANK, _LIVE_GLYPH, _live_key, _LIVE_RANK,
    _LOC_CONTENT_W, _location_prefix_segs, _LOOPS_KEYS, _MODEL_CATALOG_MAX_BYTES, _model_cell,
    _model_family, _MODEL_ID_RE, _model_key, _MODEL_TIER_RE, _MODEL_VERSION_RE, _MUTED_256,
    _MW, _NAME2_MAX, _NAME_COL, _NAME_GAP, _NAME_HUE, _NAME_KEY, _NAME_KEY_DIM, _NAME_WIDE_MAX,
    _NARROW_CUTOFF, _NARROW_L2_STAGE_COL, _narrow_session_cell_budget, _native_context_gauge,
    _NW_S, _OPTW, _pad, _pad_to_column, _palette_fg, _pct_key, _PIPE_STAGES, _QA_INT,
    _resolved_alias_model, _resolved_alias_models, _RICHER_RGB_1000, _ROLE_MODEL_NAME,
    _route_current_index, _ROUTE_STAGE_ZONE_MAX, _route_zone_width, _SESSION_CELL_MAX,
    _session_routing_column, _short_model_id, _shorten_model_suffix, _SPIN, _STAGE_RESERVE,
    _STAGE_ROLE, _stage_role_label, _STAGE_ZONE_MAX, _state_key, _STEWARD_PAIR, _TAG_W,
    _TINT_PAIR_START, _TINT_RGB_1000, _TITLE_MAX, _TWO_LINE_CUTOFF, _USAGE_BADGE_TEXT, _WIDE,
    _WIDE_STAGE_GAP, _WIDE_TIME_GAP, _wt_count, curses, NAME_KEYS,
)
from .render_stages import (  # noqa: F401
    _aged_text, _clip_age_tail, _collapse_parallel_nodes, _collapse_replica_nodes,
    _compact_dispatch_name, _DISPATCH_NAME_MAX, _dispatch_stage_color_index,
    _dispatch_stage_label, _display_session_subject, _ICON_STEWARD, _interaction_badge,
    _interaction_chip, _is_owner_mode_row, _is_plugin_agent, _LEGACY_STAGE_COLOR_INDEX,
    _one_shot_route, _plugin_phase, _projection_route_seq, _projection_stage_for_dispatch,
    _projection_stage_text, _route_rides_the_rail, _ROUTE_STATE_MARK, _session_name,
    _session_tag_chip, _session_tag_slot, _spec_phase_seq, _stage_zone_segs,
    _stall_attention_stage_segs, _steward_flag, _subject_name_key, _subject_title, _text_age,
    _text_with_age, _untagged_parallel_groups, _unused_badge,
)
from .render_rows import (  # noqa: F401
    _BOT_LABEL_MIN_LEAD, _BOT_LABEL_TAIL, _CARD_EDGE_GAP, _CARD_INSET, _CARD_TAG, _clip_segs,
    _delivery_pending_rows, _depth1_rail_color_index, _dispatch_box_bottom,
    _dispatch_box_divider, _dispatch_model_profile, _dispatch_prefix, _dispatch_profile,
    _dispatch_rail_label_layout, _dispatch_role_suffix, _display_capability_mode, _entry_skill,
    _fmt_exec_age, _frame_dispatch_line, _G_CASE_PREFIX, _GOVERNOR_QUIET_FRACTION,
    _governor_segs, _group_activity_rank, _group_key_job, _group_key_session,
    _group_last_result, _group_running_resources, _group_sort_key, _LEVEL_SHORT,
    _live_session_identity, _LiveOrderState, _mem_change_rows, _mem_divider,
    _MEM_DIVIDER_MARGIN, _mem_event_rows, _MEM_REPO_ROW_LIMIT, _mem_repo_rows, _mem_row,
    _memory_event_groups, _memory_event_key, _mode_axis_conflict, _orphan_divider,
    _ORPHAN_DIVIDER_LABEL, _overwrite_rail_cell, _overwrite_rail_text, _OWNER_CARD_INSET,
    _owner_route_shape, _periodic_curate_row, _PROFILE_MAX, _project_fallback,
    _project_for_display, _RAIL_BOT, _RAIL_BOT_RIGHT, _RAIL_CHARS, _RAIL_COL, _RAIL_DIV_LEFT,
    _RAIL_DIV_RIGHT, _RAIL_MID, _RAIL_RIGHT, _RAIL_SOLO, _RAIL_TOP, _RAIL_TOP_RIGHT,
    _reconcile_live_order, _reserve_age_tail, _reserve_frame_prefix, _RFLUSH, _ROLE_SHORT,
    _SESSION_DETAIL_COL, _short_level, _short_role, _sort_group_jobs, _sort_group_sessions,
    _stack_split, _steward_hierarchy, _STEWARD_LINE_COL, _STEWARD_LINE_MARK, _strip_autopilot,
    _SYSTEM_GROUP, _under_id_connector, _unique_managed_parents, _visible_group_jobs,
    _WORKER_RAIL_COL, bottom_label_budget,
)
from .render_select import (  # noqa: F401
    _conn_indent, _DETACHED_FINISHED_LIVENESS, _drawn_gpu_processes, _finished_frame_fold_ids,
    _fit_strip, _FOLD_CHILD_LIVENESS, _FOLD_CHILD_ROUTE_STATES, _fold_completed_child,
    _fold_finished_frame_child, _gpu_resources_for_session, _gpu_safe_text, _gpu_session_keys,
    _ICON_PEER, _ICON_SUBAGENT, _job_gpu_resources, _peer_endpoint_segs, _peer_half_segs,
    _peer_inline_fit, _peer_inline_options, _peer_link_strip, _PEER_NAME_HEX_RE,
    _peer_name_is_readable, _PEER_NAME_MAX_CELLS, _PEER_NAME_PANE_RE, _PEER_NAME_UUID_RE,
    _peer_obligation_strip, _peer_unique_sid_suffix, _PeerTagMap, _RELATION_GAP,
    _report_verification_detail_row, _resource_gpu_resources, _resource_gpu_suffix,
    _resource_progress_tail, _resource_verified_process_location, _ROUTE_CHAIN_MARK,
    _route_chain_node_knobs, _ROUTE_CHAIN_WINDOW, _route_chain_window, _select_entry,
    _select_entry_job, _selectable_session, _session_for_job, _session_repo_label,
    _subagent_elapsed_min, _subagent_idle_min, _SUBAGENT_IND, _SUMMARY_FALLBACK_W,
    _summary_row,
)
from .render_status import (  # noqa: F401
    _background_shell_tail, _clip_tail_w, _compact_context_gauge_width, _COMPUTE_HOST_INTERVAL,
    _CONTEXT_INDENT_W, _context_lead_chip, _CONTEXT_NOW_GAP, _CONTEXT_VALUE_W, _CTX_CHIP_W,
    _CTX_CORNER_MARK, _CTX_LABEL_W, _CTX_OFF_TEXT, _CTX_ON_TEXT, _CTX_TMUX_TEXT,
    _exec_detail_segs, _EXEC_GLYPH, _gpu_clip_command, _gpu_command_room, _gpu_command_text,
    _gpu_command_words, _gpu_display_command, _gpu_display_model, _GPU_FOLD_ALL, _gpu_fold_key,
    _gpu_gib, _gpu_model_family, _gpu_model_key, _GPU_MODEL_PATTERNS, _gpu_path_argument,
    _gpu_path_collisions, _gpu_process_rows, _gpu_processes_by_memory,
    _hearting_version_segments, _orphan_resource_groups, _refresh_age_label,
    _resource_command_label, _resource_location, _resource_log_age, _split_w_exact,
    _VERSION_BUILD_RE, _VERSION_RELEASE_RE, _WAIT_GLYPH, _WAIT_LABEL,
)
from .render_gpu import (  # noqa: F401
    _cpu_active_threads, _CPU_GAUGE_MAX, _CPU_GAUGE_MIN, _cpu_gauge_segs, _cpu_gauge_track,
    _cpu_thread_text, _cpu_thread_values, _EXPLORER_QUESTION_QUEUE_LOCK, _ExplorerView,
    _gpu_activity_summary, _gpu_activity_tail, _GPU_CAPACITY_COLUMN_W, _gpu_lease_segs,
    _GPU_MODEL_COLUMN_W, _gpu_on_session_lines, _gpu_process_label, _gpu_process_session_key,
    _gpu_reservation_rows, _GPU_RUN_COMMAND_MIN_W, _gpu_run_pairs, _gpu_run_rows,
    _gpu_session_key, _gpu_session_label, _gpu_state, _gpu_status_text, _gpu_token,
    _host_memory_pair, _resource_gauge_segs, _RESOURCE_GAUGE_W, _resource_level_key,
    _resource_pct_segs, _subdued_ratio_key, _vram_gauge_track,
)
from .render_explorer import (  # noqa: F401
    _ESC, _explorer_attach_remote_observation, _explorer_gesture_binding, _EXPLORER_INTERVALS,
    _explorer_key_name, _explorer_lite_view, _explorer_opencode_form_binding,
    _explorer_opencode_form_ui_supported, _explorer_output_reader, _explorer_question_envelope,
    _explorer_question_has_workflow_binding, _explorer_question_status,
    _explorer_remote_host_observation, _explorer_reveal, _explorer_row_y,
    _explorer_same_question_request, _EXPLORER_SPIN_KEYS, _explorer_state_for,
)
from .render_routes import (  # noqa: F401
    _append_segment, _BAR_ROLES, _clamp_offset, _click_target_excluded, _collect_governor,
    _collect_memory, _collect_memory_observed, _collect_project_status, _entry_id, _GATE_MARK,
    _gpu_strip_keys, _HFILL, _hit_at, _INSET, _is_fill, _jobs_unconfirmed,
    _known_parentless_support_job, _malformed, _observation_lines, _opencode_availability_text,
    _PAD_IN, _painted_hit_spans, _plain, _reading_legend, _route_card_l1, _route_job_row,
    _ROW_BOLD, _snapshot_line, _subagents_for_job, _TINT_BODY, _TINT_BODY_COOL, _TINT_BODY_HOT,
    _TINT_CAP, _TINT_CAP_HOT, _TINT_CHARS, _TINT_INTEL, _TINT_LVL, _usage_header_rows,
    _wrap_legend, _wrap_route_node, live_harnesses,
)
from .render_input import (  # noqa: F401
    _base_navigation, _close_job_row_if_registry, _CursesInput, _EXPLORER_MOUSE_MOTION,
    _getmouse_event, _getmouse_xy, _MOUSE_HINT_MIN_WIDTH, _PROCESS_HINT_MIN_WIDTH,
    _prompt_button_segs, _prompt_hit_boxes, _prompt_segs, _prompt_variants,
)

# Moved classes report the module they were defined in before the split, so
# tracebacks, reprs and pickles read as before (`<package>.render.<Class>`).
for _moved_class in (
    _CursesInput, _ExplorerView, _LiveOrderState, _PeerTagMap,
):
    if _moved_class.__module__.rpartition(".")[2].startswith("render_"):
        _moved_class.__module__ = __name__
del _moved_class
                           # wide 1-line grid now needs room for the options column too, so the
                           # dead-zone between the old 110 cutoff and wide's real ~138-col need
                           # is gone — 2-line cards are the PRIMARY layout, wide 1-line is rare)
_LAYOUT = "auto"            # 'auto' (width decides) | 'wide' | 'narrow' | 'stack' — `w` key cycles
def _cycle_layout():
    global _LAYOUT
    _LAYOUT = {"auto": "wide", "wide": "narrow", "narrow": "stack", "stack": "auto"}[_LAYOUT]


def _layout_mode(w):
    """wide (1-line grid) / narrow (2-line cards) / stack (ultra-narrow, fields stacked
    vertically. Auto lets the terminal width decide.
    F-15a P0-3: <70 stack · <138 narrow (2-line, the PRIMARY layout — most real terminals are
    ≤120 cols) · ≥138 wide (1-line, needs room for the options column too)."""
    if _LAYOUT != "auto":
        return _LAYOUT
    if w < _NARROW_CUTOFF:
        return "stack"
    if w < _TWO_LINE_CUTOFF:
        return "narrow"
    return "wide"

_COLOR = {}   # color_key → curses attr (filled by _init_colors); empty ⇒ plain mode
_TINT_OK = False   # 256-color tint pairs initialized (round-5 panels); False ⇒ rail+gap fallback
_TINT_PAIR = {}    # (tint_char, hue_char) → curses attr — the (fg, tint_bg) composed pairs


def _key_attr(key, tint=None):
    """Attr for a color_key, composed with the row's tint background when active (spec §5.3)."""
    if tint is None or not _TINT_OK:
        return _COLOR.get(key, 0)
    hue, attr = _HUE_OF.get(key, ("d", 0))
    pair = _TINT_PAIR.get((tint, hue))
    if pair is None:
        return _COLOR.get(key, 0)
    return pair | attr


def _init_colors():
    _COLOR.clear()
    try:
        curses.start_color()
        curses.use_default_colors()
        bg = -1
    except Exception:
        bg = curses.COLOR_BLACK
    # Apply optional palette refinements before creating pairs.  Unsupported
    # terminals keep the checked xterm-256 colors without losing any semantics.
    try:
        if curses.COLORS >= 256 and curses.can_change_color():
            for name, rgb in _RICHER_RGB_1000.items():
                curses.init_color(_MUTED_256[name], *rgb)
            for index, rgb in _TINT_RGB_1000.items():
                curses.init_color(index, *rgb)
    except Exception:
        pass
    # color discipline (design review 2026-07-01): one meaning per color.
    #   green/yellow/red = status + level ONLY · cyan/magenta/blue = harness identity ONLY
    #   soft-white bold = the row's single focal point (session name) · dim = all metadata
    fallback_spec = {
        "green": curses.COLOR_GREEN, "yellow": curses.COLOR_YELLOW, "red": curses.COLOR_RED,
        "h_claude": curses.COLOR_CYAN, "h_codex": curses.COLOR_MAGENTA, "h_opencode": curses.COLOR_BLUE,
        "soft": curses.COLOR_WHITE, "vanilla": curses.COLOR_YELLOW,
        "orange": curses.COLOR_YELLOW,
    }
    palette_name = {
        "h_claude": "cyan", "h_codex": "magenta", "h_opencode": "blue",
    }
    spec = {
        key: _palette_fg(palette_name.get(key, key), fallback)
        for key, fallback in fallback_spec.items()
    }
    n = 1
    for key, fg in spec.items():
        try:
            curses.init_pair(n, fg, bg)
            _COLOR[key] = curses.color_pair(n)
            n += 1
        except Exception:
            _COLOR[key] = 0
    # status dots — working "blinks" via a manual on/off toggle in the loop (A_BLINK is stripped
    # by tmux/herdr, so we animate it ourselves: g_work bright ↔ g_work_off dim each ~500ms)
    _COLOR["g_work"] = _COLOR.get("green", 0) | curses.A_BOLD
    _COLOR["g_work_off"] = _COLOR.get("green", 0) | curses.A_DIM
    # Working spinners use vanilla without changing the green working accents
    # elsewhere in a row. Dispatch spinners keep the quieter dim weight.
    _COLOR["g_spin"] = _COLOR.get("vanilla", _COLOR.get("yellow", 0))
    _COLOR["g_spin_dim"] = _COLOR["g_spin"] | curses.A_DIM
    _COLOR["g_idle"] = _COLOR.get("yellow", 0)
    _COLOR["g_stale"] = curses.A_DIM
    _COLOR["g_dead"] = _COLOR.get("red", 0) | curses.A_BOLD
    # F-60 (user 2026-08-04, "중단 표시가 눈에 잘 안 보인다"): blocked moves off the yellow
    # `g_idle` onto red. Its own key, not `g_dead`'s, so the two red states stay independently
    # tunable. The chip is the SAME key plus A_REVERSE — the `hdr_bar`/`hdr_warn` fallback
    # idiom — and it is what carries the emphasis; no blink, because the working dot's 2 Hz
    # toggle already means "in progress" and blocked is a static wait for input.
    # v47 (user 2026-08-05, "blocked가 볼드더라"): no bold on the glyph/word — the chip owns
    # the emphasis. Without color the keys still differ: blocked reads plain and its badge
    # reads reverse, while dead is bold and never gets a chip.
    _COLOR["g_blocked"] = _COLOR.get("red", 0)
    _COLOR["g_blocked_chip"] = _COLOR["g_blocked"] | curses.A_REVERSE
    # unused (F-26): dim yellow — distinct from idle's dim GREEN (live-but-quiet) and from
    # stale's colorless dim. The shape carries the meaning on its own; color only reinforces.
    _COLOR["g_unused"] = _COLOR.get("yellow", 0) | curses.A_DIM
    _COLOR["g_unused_b"] = _COLOR.get("yellow", 0)
    # level bars (ctx / usage): green <50 / yellow <80 / red ≥80 (red bold = alarm)
    _COLOR["lvl_g"] = _COLOR.get("green", 0)
    _COLOR["lvl_y"] = _COLOR.get("yellow", 0)
    _COLOR["lvl_r"] = _COLOR.get("red", 0) | curses.A_BOLD
    _COLOR["lvl_r_flat"] = _COLOR.get("red", 0)          # F-61: usage header, no alarm weight
    _COLOR["lvl_g_dim"] = _COLOR.get("green", 0) | curses.A_DIM
    _COLOR["lvl_y_dim"] = _COLOR.get("yellow", 0) | curses.A_DIM
    _COLOR["lvl_r_dim"] = _COLOR.get("red", 0) | curses.A_DIM
    _COLOR["resource_active"] = _COLOR.get("soft", 0)
    _COLOR["base_host"] = _COLOR.get("green", 0) | curses.A_BOLD
    # per-MODEL family colors, in TWO intensities (2026-07-02: main↔dispatch contrast = whole-row
    # brightness): fam_* = BRIGHT (main session rows) / famd_* = DIM (dispatch rows recede).
    _hue = {h: _COLOR.get("h_" + h, 0) for h in ("claude", "codex", "opencode")}
    # The three named Codex models are separate families, not one `gpt` bucket (user
    # 2026-10-27: "codex 모델들 전부 같은 컬러"). Sol keeps the old gpt yellow, Astra
    # takes the unused orange, and Luna takes soft white; a bare gpt-* id still lands
    # on the shared `gpt` key. Names stay the primary identifier — the 8-color
    # fallback folds orange into yellow — but the 256-color board separates them.
    _fam = {"opus": _palette_fg("cyan", curses.COLOR_CYAN),
            "sonnet": _palette_fg("blue", curses.COLOR_BLUE),
            "haiku": _palette_fg("green", curses.COLOR_GREEN),
            "fable": _palette_fg("magenta", curses.COLOR_MAGENTA),
            "sol": _palette_fg("yellow", curses.COLOR_YELLOW),
            "astra": _palette_fg("orange", curses.COLOR_YELLOW),
            "luna": _palette_fg("soft", curses.COLOR_WHITE),
            "gpt": _palette_fg("yellow", curses.COLOR_YELLOW)}
    n_pair = _FAMILY_PAIR_START                     # pairs 1-9 reserved above; families from 10
    for fam, fg in _fam.items():
        try:
            curses.init_pair(n_pair, fg, bg)
            hue = curses.color_pair(n_pair)
            n_pair += 1
        except Exception:
            hue = 0
        _COLOR["fam_" + fam] = hue
        _COLOR["famd_" + fam] = hue | curses.A_DIM
    _COLOR["fam_other"] = 0                        # unknown family → default fg
    _COLOR["famd_other"] = curses.A_DIM
    # GPU architecture/model families reuse the same restrained identity palette.
    # Idle summaries stay DIM; exact current process evidence promotes the same
    # hue to normal intensity (F-93). Text remains the primary identifier.
    for fam, hue_name in {
        "rtx6000": "h_opencode", "rtx4090": "h_claude", "rtx5090": "green",
        "blackwell": "green", "hopper": "h_codex", "ada": "h_claude",
        "ampere": "h_opencode", "turing": "yellow", "legacy": "orange",
    }.items():
        hue = _COLOR.get(hue_name, 0)
        _COLOR["gpu_" + fam] = hue | curses.A_DIM
        _COLOR["gpu_" + fam + "_active"] = hue
    _COLOR["gpu_other"] = curses.A_DIM
    _COLOR["gpu_other_active"] = 0
    # branch: normal on main session rows, dim on dispatch rows (same brightness axis)
    _COLOR["branch_s"] = 0
    for h, hue in _hue.items():
        # bright harness color = a TOP-LEVEL session / account; a dispatch job keeps the DIM
        # harness (h_<h>) → main↔spawned weight is carried by font-color intensity (no bg fill).
        _COLOR["hb_" + h] = hue
    _COLOR["hb_other"] = 0
    # F-100a/b badges: soft white, plain weight — no fill, no bold.
    _COLOR["tag"] = _COLOR.get("soft", 0)
    _COLOR["tag_dim"] = curses.A_DIM
    try:
        curses.init_pair(_STEWARD_PAIR, _palette_fg("steward_pink", curses.COLOR_MAGENTA), bg)
        _COLOR["tag_steward"] = curses.color_pair(_STEWARD_PAIR) | curses.A_BOLD
    except Exception:
        _COLOR["tag_steward"] = (_COLOR.get("h_codex", 0) & ~curses.A_DIM) | curses.A_BOLD
    _COLOR["tag_steward_dim"] = (_COLOR["tag_steward"] & ~curses.A_BOLD) | curses.A_DIM
    _COLOR["herdr_on"] = _COLOR.get("soft", 0)
    _COLOR["grp"] = _COLOR.get("soft", 0) | curses.A_BOLD  # group card title
    _COLOR["grp_live"] = _COLOR.get("green", 0)
    _COLOR["grp_hot"] = _COLOR.get("green", 0) | curses.A_BOLD   # active card title (working)
    _COLOR["grp_cool"] = _COLOR.get("yellow", 0) | curses.A_DIM  # Cooling indicator, name, and elapsed time.
    _COLOR["grp_cold"] = curses.A_DIM                            # Cold inactive group: grey ring.
    # harness identity = dim colored text (color lives ONLY here for identity)
    for h in ("claude", "codex", "opencode"):
        _COLOR["h_" + h] = _COLOR.get("h_" + h, 0) | curses.A_DIM
    # Hearting identity is persistent metadata, not the screen's focal point.  Keep the
    # release in a quiet blue-grey and make checkout/build details recede further.  Dirty
    # remains barely warm so it is discoverable without reading like an alert.
    _COLOR["version_release"] = _COLOR.get("h_opencode", 0) | curses.A_DIM
    _COLOR["version_build"] = curses.A_DIM
    _COLOR["version_dirty"] = _COLOR.get("yellow", 0) | curses.A_DIM
    _COLOR["version_method"] = curses.A_DIM
    # session name = THE left pillar of every row (design r2): bright bold for any live session —
    # the eye lands here first; only stale/dead recede. working is distinguished by its dot blink.
    _COLOR["nm_dead"] = _COLOR.get("soft", 0) | curses.A_DIM
    _COLOR["hearting_name"] = (_COLOR.get("vanilla", _COLOR.get("yellow", 0))
                               | curses.A_BOLD)
    _COLOR["name_dim"] = _COLOR.get("soft", 0) | curses.A_DIM
    # gate words · cost alarm · structure
    _COLOR["gate_t"] = _COLOR.get("green", 0) | curses.A_DIM
    _COLOR["gate_u"] = _COLOR.get("yellow", 0) | curses.A_DIM
    _COLOR["cost_hi"] = _COLOR.get("soft", 0) | curses.A_BOLD
    # qa rigor ramp (dispatch tag after the name): quick dim … adversarial bold
    for lvl, it in _QA_INT.items():
        _COLOR["qa_" + lvl] = it
    # Effort ramp v3: avoid excess yellow while distinguishing high and xhigh.
    # low/medium = no hue (weight only) < high = BLUE < xhigh = MAGENTA (bold) < max = RED
    # (bold, the one true alarm — unchanged). No yellow anywhere in the ramp.
    _COLOR["eff_low"] = curses.A_DIM
    _COLOR["eff_medium"] = 0
    _COLOR["eff_high"] = _COLOR.get("h_opencode", 0) & ~curses.A_DIM   # plain blue
    _COLOR["eff_xhigh"] = (_COLOR.get("h_codex", 0) & ~curses.A_DIM) | curses.A_BOLD  # bold magenta
    _COLOR["eff_max"] = _COLOR.get("red", 0) | curses.A_BOLD
    # dispatch-row effort: the SAME hue ramp at dim weight (user 2026-07-20: 분사 행에도
    # 컬러 — a flat grey effort read as part of the grey subtitle). Bold is stripped so a
    # dim row never carries a bolder cell than its parent session's.
    for _e in ("low", "medium", "high", "xhigh", "max"):
        _COLOR["effd_" + _e] = (_COLOR.get("eff_" + _e, 0) & ~curses.A_BOLD) | curses.A_DIM
    # htop chrome: the one background pair on screen — black on
    # muted neutral full-width bars wrapping the board (column-header bar + footer key bar).
    # They stay structural without the glare of stock white.
    # Structural, one-shot — NOT a per-item classification color, so the fg color-axis budget
    # The no-rainbow-noise rule remains; keycaps use bold because dim vanishes on the bar.
    try:
        curses.init_pair(_HDR_BAR_PAIR, curses.COLOR_BLACK,
                         _palette_fg("chrome", curses.COLOR_WHITE))
        _COLOR["hdr_bar"] = curses.color_pair(_HDR_BAR_PAIR)
    except Exception:
        _COLOR["hdr_bar"] = curses.A_REVERSE
    _COLOR["hdr_key"] = _COLOR["hdr_bar"] | curses.A_BOLD
    # F-27 warning bar: the SAME structural bar as hdr_bar, in red. It exists because a footer
    # warning must still BE a bar — reusing the body glyph role `g_dead` for a footer head made
    # the row fail _addline's bar test, so the two warning prompts lost their band and rendered
    # as a red-text/black-tail fragment while the benign prompt got a clean full-width bar. That
    # inverts the hierarchy the double-confirm ladder depends on: the live-session prompt must
    # read as MORE serious, not like a render glitch.
    try:
        curses.init_pair(_HDR_WARN_PAIR, _palette_fg("soft", curses.COLOR_WHITE),
                         _palette_fg("warning", curses.COLOR_RED))
        _COLOR["hdr_warn"] = curses.color_pair(_HDR_WARN_PAIR) | curses.A_BOLD
    except Exception:
        _COLOR["hdr_warn"] = curses.A_REVERSE | curses.A_BOLD
    # The one key the user must press, advertised on top of the red bar.
    _COLOR["hdr_warn_key"] = _COLOR["hdr_warn"] | curses.A_REVERSE
    # bar BLANKS are drawn as muted-neutral █ blocks on the DEFAULT bg (hdr_blk), not as bg-colored
    # spaces: ncurses collapses blank runs into ECH/EL erase sequences, and on terminals without
    # working BCE the erased cells come out BLACK — the bar broke between words and after the
    # text. A block glyph is a real character, so it is
    # physically written every time and matches the muted bar background cell.
    try:
        curses.init_pair(_HDR_BLK_PAIR, _palette_fg("chrome", curses.COLOR_WHITE), bg)
        _COLOR["hdr_blk"] = curses.color_pair(_HDR_BLK_PAIR)
    except Exception:
        _COLOR["hdr_blk"] = 0
    # Subtle panel tints in 256-color mode: seven hues by tint level.
    # text keeps its fg INSIDE a tinted band. Low-chroma slate backgrounds — no new fg axis.
    # Failure of any init → _TINT_OK False → rail+gap fallback (spec §4, zero regression).
    global _TINT_OK
    _TINT_OK = False
    _TINT_PAIR.clear()
    try:
        if curses.COLORS >= 256:
            hues = hue_colors(curses.COLORS)
            n_pair = _TINT_PAIR_START
            for tch, lvl in _TINT_LVL.items():
                for hch, fg in hues.items():
                    curses.init_pair(n_pair, fg, lvl)
                    _TINT_PAIR[(tch, hch)] = curses.color_pair(n_pair)
                    n_pair += 1
            _TINT_OK = True
    except Exception:
        _TINT_OK = False
        _TINT_PAIR.clear()
    # stage breadcrumb — each pipeline stage a DISTINCT color (user); the CURRENT stage is BOLD
    # Bright current stages stand out; past and pending stages use the same hue dimmed.
    _stage_raw = [_hue.get("opencode", 0), _hue.get("claude", 0), _COLOR.get("green", 0),
                  _COLOR.get("yellow", 0), _hue.get("codex", 0)]  # blue · cyan · green · yellow · magenta
    for i, base in enumerate(_stage_raw):
        _COLOR["stg%d_on" % i] = base | curses.A_BOLD
        _COLOR["stg%d_off" % i] = base | curses.A_DIM
        # F-68d/F-72: one frame key per stage hue, always dim and never bold. The
        # blink is gone (F-70) and the brightness with it (F-72) — the box carries
        # hue for identity only, so it stops out-inking the main session row.
        _COLOR["frm%d" % i] = base | curses.A_DIM
    _COLOR["frm_idle"] = curses.A_DIM
    _COLOR["dim"] = curses.A_DIM
    _COLOR["now_main"] = _COLOR.get("soft", 0) | curses.A_BOLD
    _COLOR["now_sub"] = _COLOR.get("soft", 0) | curses.A_DIM
    # F-76b: name keys mirror the harness pair exactly — bright `hb_*`, dim `h_*`.
    for h in _NAME_HUE:
        _COLOR["nm_" + h] = _COLOR.get("hb_" + h, 0)
        _COLOR["nmd_" + h] = _COLOR.get("h_" + h, 0)
    _COLOR["nm_other"] = _COLOR.get("soft", 0) | curses.A_BOLD
    _COLOR["nmd_other"] = _COLOR.get("soft", 0) | curses.A_DIM
    _COLOR["head"] = curses.A_DIM
    _COLOR["section_head"] = _COLOR.get("soft", 0) | curses.A_BOLD
    _COLOR["unknown"] = curses.A_DIM


def _attr(key):
    return _COLOR.get(key, 0)


_BLINK_ON = True     # shared manual blink: one second on, one second off
_GIT_TELEMETRY = True
                              # options column (plan✓ › exec › test = 19 + gap) — without this
                              # the name column absorbs all slack and the breadcrumb clips at
                              # the panel edge (user 2026-07-15: 분사 세션 단계가 잘림).
                              # 22→20 (2026-07-20): the redundant 'code: ' prefix left the
                              # breadcrumb (entry skill leads the options column now), so the
                              # freed cells flow to the name/ctx slack ledger.


def _wide_slack(term_width):
    """Terminal slack available to the wide-layout name column past its fixed
    columns and framing, or None when `term_width` is unknown (hermetic callers).
    Consumed by `_wide_name_width` below — the one place the fixed_row/framing
    reservation is computed."""
    if not term_width:
        return None
    # F-33 (v11): no more separate model column (_MW) — model/effort now ride inside
    # _NAME_COL's harness-model field (_HMW), so the column merge's freed width flows
    # straight into this reservation and out to the name slack below.
    # F-57 (v41): the dead `_CTX_W` (24) inline-gauge term is gone from this sum, so every
    # terminal width now reports 24 more cells of real slack than it did through v40.
    fixed_row = (_NAME_COL + _BRANCH_SUFFIX_W + _WIDE_STAGE_GAP + _WIDE_TIME_GAP
                 + 5 + _STAGE_RESERVE)
    if _TINT_OK:
        framing = (_INSET + _PAD_IN) + _INSET + (2 + _PAD_IN) + 6 + 2
    else:
        framing = 1 + 6 + 2
    return int(term_width) - fixed_row - framing


def _wide_name_width(term_width):
    """Responsive wide-layout name column, between `_NW_S` and `_NAME_WIDE_MAX`
    (F-22 minor, v8). Every cell of slack past the `_NW_S` floor goes to the name
    until the cap; slack past the cap stays as end-of-row padding (F-22).

    F-57 (v41) removed the `_CTX_BOOST` (12) step that used to spend the first
    surplus cells on the inline gauge reservation before the name grew at all.
    With both that boost and the `_CTX_W` (24) reservation gone the name reaches
    its 40-cell cap 36 columns earlier than it did through v40 (176 → 140)."""
    raw = _wide_slack(term_width)
    if raw is None:
        return _NW_S
    surplus = max(0, raw - _NW_S)
    return _NW_S + min(_NAME_WIDE_MAX - _NW_S, surplus)


def _branch_suffix_segs(cwd, branch, dim=True, optional=False,
                        ahead=_IO_UNSET, behind=_IO_UNSET,
                        location_kind=None, location_repo=None):
    """Integrated `` (branch)`` suffix for the session column.

    Wide rows reserve a fixed amount for this suffix; narrow/stack rows use only
    its visible width. Branch text keeps the former brightness distinction while
    the punctuation recedes.
    """
    snapshot_owned = ahead is not _IO_UNSET or behind is not _IO_UNSET
    br = branch or (None if snapshot_owned else _git_branch(cwd))
    if not br and optional:
        return []                                  # UNCHANGED — must stay BEFORE location work,
                                                   # so a location token never appears alone
    loc_segs, loc_w = _location_prefix_segs(location_kind, location_repo, _LOC_CONTENT_W)
    # The title side reserves `_NAME_GAP`, so the visible suffix may use one cell
    # beyond its nominal reserve without moving the next column.
    shown = _clip_w(str(br or "—"), max(1, _LOC_CONTENT_W - loc_w))
    base = [(" (", "dim")] + loc_segs + [(shown, "dim" if dim else "branch_s")]
    close = [(")", "dim")]
    if snapshot_owned:
        counts = ((ahead or 0), (behind or 0)) if (ahead or behind) else None
    else:
        counts = gitinfo.ahead_behind(cwd) if _GIT_TELEMETRY and br and cwd else None
    if not counts:
        return base + close
    ahead, behind = counts
    ahead_seg = [(" ↑%d" % ahead, "lvl_g")] if ahead else []
    behind_seg = [(" ↓%d" % behind, "lvl_r")] if behind else []
    result = base + ahead_seg + behind_seg + close
    # Preserve the branch identity; drop behind first, then ahead, when the fixed suffix
    # budget cannot carry both telemetry values.
    if sum(_dw(text) for text, _key in result) > _BRANCH_SUFFIX_W:
        result = base + ahead_seg + close
    if sum(_dw(text) for text, _key in result) > _BRANCH_SUFFIX_W:
        result = base + close
    return result


def _route_stage_segs(route_seq, working, max_width):
    """F-28b route-aware breadcrumb (prd.md:303). `route_seq` = [(node_id, state), ...] in the
    route's own DAG order (flattened level order — route.py's `view["nodes"]` is already in
    that order, one entry per node). `state` comes from `route.py`'s §3.3 single judge
    (active/done/reconciling/failed/pending) — this function ONLY renders what was already decided
    (SD-F2: node lit-ness is the child's live evidence, never re-derived here)."""
    def _cur_key(i):
        if working and not _BLINK_ON:
            return "stg%d_off" % (i % 5)
        return "stg%d_on" % (i % 5)
    cur_i = _route_current_index(route_seq)
    items = []
    # F-77 (user 2026-08-14 "체크나 X 표시랑 글자사이에 간격 하나 띄워줘"): one space between
    # a node name and its state mark. Flush (`execute✕`) the glyph read as part of the word.
    for i, (nid, st) in enumerate(route_seq):
        if st == "failed":
            items.append((i, nid + " ✕", "lvl_r"))
        elif st == "attention":
            items.append((i, nid + " !", "lvl_y"))
        elif st == "degraded":
            items.append((i, nid + " ◐", "lvl_y"))
        elif st in ("reconciling", "recovering"):
            items.append((i, nid + " …", "lvl_y"))
        elif st == "done":
            items.append((i, nid + " ✓", "stg%d_off" % (i % 5)))
        elif st == "active":
            items.append((i, nid, _cur_key(i)))
        else:
            # pending (and any residual skipped that a caller did not filter) — the name in the
            # dim off-hue, no glyph. Kept glyph-free on purpose: ✓/✕ are the only markers, and a
            # skip glyph like ⊘ is missing from many terminal fonts (user 2026-07-24 icon error).
            items.append((i, nid, "stg%d_off" % (i % 5)))
    if max_width is not None:
        items = _drop_past_stages(items, cur_i, max_width)
        # F-68 floor: when even the folded remainder (typically the lone current
        # token) exceeds the budget, shorten the LAST label with an ellipsis
        # instead of letting a hard border tail-cut it mid-glyph.
        def _total():
            return (sum(_dw(t) for _i2, t, _k2 in items)
                    + max(0, len(items) - 1) * 3)
        if items and _total() > max_width:
            over = _total() - max_width
            widest = max(range(len(items)), key=lambda n: _dw(items[n][1]))
            w_i, w_label, w_key = items[widest]
            keep = max(1, _dw(w_label) - over - 1)
            items[widest] = (w_i, w_label[:keep] + "…", w_key)
    out = []
    for n, (_i, label, key_) in enumerate(items):
        if n:
            out.append((" › ", "dim"))
        out.append((label, key_))
    return out


def _stage_segs(key, stage, working=False, max_width=None, route_seq=None):
    """Process viz — the pipeline lifecycle as a breadcrumb: each stage a DISTINCT color, the rest
    of the sequence the same hue but DIM. The CURRENT stage is bold/bright and, when the job is
    actively `working`, BLINKS in sync with the working dot (shared `_BLINK_ON`, ~2 Hz) so the eye
    is drawn to where work is happening right now. Unknown pipeline/stage → a single lit token.

    `max_width` (D3, v9): when given and the full breadcrumb would exceed it, past stages fold
    via `_drop_past_stages` before anything is emitted — the cap lives in the assembly, not as
    a post-hoc truncation, so a dropped stage never leaves a half-drawn ✓ or separator.

    `route_seq` (F-28b, v10): when given (a resolved route's node list — see
    `_route_stage_segs`), it REPLACES `_PIPE_STAGES.get(key)` entirely — record-derived nodes,
    not the hardcoded 3-stage table. `None` (the default) is the entire pre-v10 behavior,
    unchanged (prd.md:303 — record-less jobs keep the existing breadcrumb)."""
    if route_seq is not None:
        return _route_stage_segs(route_seq, working, max_width)

    def _cur_key(i):
        # working → pulse on/off with the dot; idle/other → steady bright
        if working and not _BLINK_ON:
            return "stg%d_off" % (i % 5)
        return "stg%d_on" % (i % 5)
    seq = _PIPE_STAGES.get(key)
    if seq and stage in seq:
        cur_i = seq.index(stage)
        items = []
        for i, st in enumerate(seq):
            # P1-5: a stage BEFORE the current one is done — a bright ✓ marker makes the
            # "past stages are folded into this breadcrumb" contract visible (F-15b).
            label = st + ("✓" if i < cur_i else "")
            items.append((i, label, _cur_key(i) if st == stage else "stg%d_off" % (i % 5)))
        if max_width is not None:
            items = _drop_past_stages(items, cur_i, max_width)
        out = []
        for n, (_i, label, key_) in enumerate(items):
            if n:
                out.append((" › ", "dim"))
            out.append((label, key_))
        return out
    if seq and stage in ("", key, "open", "running"):
        # F-42a: a known capability key is not evidence that its concrete route/stages already
        # exist.  Keep a truly unstarted registry row queued; otherwise render one honest boot
        # state until a stage or sealed route arrives.  Never preview the hardcoded track as if
        # it had already been configured.
        if stage == "open" and not working:
            return [("queued", "stg0_off")]
        preparing_key = ("stg0_on" if _BLINK_ON else "stg0_off") if working else "stg0_off"
        return [("preparing…", preparing_key)]
    if stage:
        # F-11: no known pipeline track for this key (seq is None) — jobs.log raw status vocab
        # ("open"/"running") shouldn't leak onto the board as-is. "open" humanizes to "queued";
        # "running" (no track to light up) renders dim rather than as a bright lit token that
        # would misleadingly imply an active named stage. jobs.log status vocabulary itself is
        # unchanged — display layer only.
        if stage == "open":
            return [("queued", _cur_key(0))]
        if stage == "running":
            preparing_key = ("stg0_on" if _BLINK_ON else "stg0_off") if working else "stg0_off"
            return [("preparing…", preparing_key)]
        # F-68: the single unknown-stage token honors the zone budget too — inside
        # a framed card the border is a hard edge and would tail-cut it mid-glyph.
        if max_width is not None and _dw(stage) > max_width:
            stage = stage[:max(1, max_width - 1)] + "…"
        return [(stage, _cur_key(0))]
    return [("-", "dim")]


def _route_compact_segs(j, route_seq, working, max_width=None):
    """F-75: the depth-1 owner row's stage slot, now that the full breadcrumb rides the card's
    close rail. Short form `<node><mark> <done>/<total>` — the same shape a main session's row
    has always shown (`_projection_stage_text`), so both altitudes read alike.

    Shares `_route_current_index` and the per-state hue table with `_route_stage_segs`: the
    owner row and the rail below it can never disagree about which node is current."""
    cur_i = _route_current_index(route_seq)
    nid, st = route_seq[cur_i]
    mark, state_key = _ROUTE_STATE_MARK.get(st, ("", None))
    if state_key is not None:
        key = state_key
    elif st == "done":
        key = "stg%d_off" % (cur_i % 5)
    elif st == "active" and working and not _BLINK_ON:
        key = "stg%d_off" % (cur_i % 5)
    elif st == "active":
        key = "stg%d_on" % (cur_i % 5)
    else:
        key = "stg%d_off" % (cur_i % 5)
    progress = getattr(getattr(j, "work_projection", None), "progress", None)
    if progress is not None:
        done, total = progress.done, progress.total
    else:
        done = sum(1 for _nid, state in route_seq if state == "done")
        total = len(route_seq)
    node_seg = (nid + (" " + mark if mark else ""), key)
    count_seg = ("%d/%d" % (done, total), "dim")
    # F-9(c) whole-component fold, never a mid-token tail-cut: when the slot cannot hold
    # both, the COUNT survives alone — the node name is still lit on the close rail below,
    # while `7/13` exists nowhere else on the card.
    if max_width is not None and _dw(node_seg[0]) + 1 + _dw(count_seg[0]) > max_width:
        return [count_seg]
    return [node_seg, (" ", None), count_seg]


def _dispatch_stage_segs(j, key, stage, slug_name, working=False, route_seq=None,
                         route_zone=None, compact_route=False):
    if getattr(j, "source", None) == "plugin-queue":
        # F-50e: a plugin-queue row has no fleet pipeline at all — its micro-status is the
        # plugin's verbatim phase (same display rank as a dispatch-depth-2 worker's "running"), and
        # nothing else may occupy this slot.
        phase = _plugin_phase(j)
        return [(phase, "dim")] if phase else []
    depth = max(1, int(getattr(j, "depth", 1) or 1))
    intensity = getattr(j, "intensity", None) or ""
    projection = getattr(j, "work_projection", None)
    if projection is not None and getattr(projection, "ambiguity", None):
        return []
    if depth >= 2:
        # P0-1: a dispatch-depth-2 stage worker never repeats its parent conductor's full
        # breadcrumb — its identity already rode the name zone (label above); this slot
        # is its own micro-status only. route_seq is a dispatch-depth-1 CONDUCTOR concern only —
        # never consulted here, unconditionally (F-28b plan §4.2, unchanged from pre-v10).
        if j.liveness == "working":
            color_i = _dispatch_stage_color_index(j, key, stage)
            return [("running", "stg%d_on" % color_i if _BLINK_ON
                                else "stg%d_off" % color_i)]
        if stage and stage not in ("open", "running"):
            # F-78 (measured: `code-execute(strong) / … : code-execute`): a non-live worker
            # falls back to its raw `stage`, which for a registered stage worker IS its
            # assigned contract — the same token the options dial already leads with. Echoing
            # it says nothing and costs the row real width, so the slot stays empty and the
            # dial keeps the identity (same same-row-duplicate rule the SD-F1 prefix follows).
            if _entry_skill(j) == stage:
                return []
            return [(stage, "stg0_off")]
        return []
    if route_seq:
        # F-75: inside a card the whole pipeline now rides the close rail, so this slot keeps
        # only "where now" — that is what freed the owner row from clipping to `re…` at 140
        # columns. Outside a card (orphan/unframed rows) there is no rail to move it to, so
        # the full breadcrumb stays here exactly as F-28b/2026-07-21 established.
        if compact_route:
            return _route_compact_segs(j, route_seq, working, max_width=route_zone)
        # F-28b (v10): a resolved route replaces the whole breadcrumb — record nodes are the
        # real pipeline shape, not a role-label prefix over the hardcoded 3-stage table.
        # user 2026-07-21: CONDUCTOR breadcrumb shows the whole pipeline (wider bound than the
        # shared stage zone) so no stage — plan especially — is silently dropped.
        return _stage_segs(key, stage, working=working,
                           max_width=route_zone or _ROUTE_STAGE_ZONE_MAX,
                           route_seq=route_seq)
    if depth == 1 and intensity == "quick":
        return [("quick/exec", "stg0_on" if working and _BLINK_ON else "stg0_off")]
    if key and key != slug_name and not _entry_skill(j):
        # SD-F1: a dispatch-depth-2 stage worker's `key` IS its capability (code-plan/code-execute/
        # code-test/code-report) — reuse _stage_role_label (same helper the F-13 legend
        # uses) to humanize it instead of leaking the raw capability token onto the board.
        # When `_entry_skill` already leads the options column with this very token (user
        # 2026-07-20: "code가 표시가 되어있는데"), the prefix is a same-row duplicate — skip.
        # F-68: a framed card's remaining width (route_zone) caps every fallback
        # budget too — the border is a hard edge, unlike the open-row flush.
        zone_max = min(_STAGE_ZONE_MAX, route_zone) if route_zone else _STAGE_ZONE_MAX
        role_label, role_suffix = _stage_role_label(key)
        prefix_text = (role_label + role_suffix) if role_label else key
        prefix_seg = (prefix_text + ": ", "name_dim")
        body = _stage_segs(key, stage, working=working,
                           max_width=max(0, zone_max - _dw(prefix_seg[0])))
        segs = [prefix_seg] + body
        if sum(_dw(t) for t, _k in segs) > zone_max:
            # F-9(c): past stages already folded as far as they can (SD-F2 keeps the active
            # stage) — the next whole component to drop is the role-label prefix itself.
            return body
        return segs
    return _stage_segs(key, stage, working=working,
                       max_width=(min(_STAGE_ZONE_MAX, route_zone)
                                  if route_zone else _STAGE_ZONE_MAX))


def _suppressed_stage_segs(entity, working, max_width, tag_by_key=None):
    """The stage cell when an owner card renders under the session (D3): the card already
    shows that route's stage and dial, so the capability tag steps aside — but the route
    chain is the session's own sequence, not the card's stage, and it has no other surface."""
    chain_cell = _route_chain_cell(getattr(entity, "route_chain", None),
                                   min(max_width, _SESSION_CELL_MAX), tag_by_key, working)
    return chain_cell if chain_cell is not None else [("-", "dim")]


def _session_stage_segs(entity, working, max_width, tag_by_key=None):
    """Stage-zone segments for a session row. A spec-grounding projection renders a lit phase
    breadcrumb in the EXACT dispatch-row syntax (user 2026-07-24 "code에 맞춰서 소괄호에 넣는
    걸로"): ``spec(mode·intensity) : spec✓ › dev●`` — entry ``spec`` in the dim name_dim hue, the
    behaviour knobs (mode; intensity when the spec recorded one — inline specs usually do not) in
    a dim paren group joined by ``·`` exactly as ``_opts_segs`` does, then the dim ` : `
    breadcrumb lead-in (``_stage_zone_segs``). Deferred / n·a phases are dropped (not part of this
    project's flow; also avoids a skip glyph absent from many fonts). Otherwise the flat label."""
    # User 2026-09-18 ("그걸 그냥 옆쪽에 - 있던 자리에 바로 하는건 별로인가?"): a visible
    # route chain (two or more routes, or a declared plan) takes this cell, as a fixed
    # window of its last three nodes; there is no separate chain line.
    chain_cell = _route_chain_cell(getattr(entity, "route_chain", None),
                                   min(max_width, _SESSION_CELL_MAX), tag_by_key, working)
    if chain_cell is not None:
        return chain_cell
    cap = getattr(entity, "cap_grounding", None) or {}
    cap_intensity = _short_level(cap.get("intensity"))
    seq = _spec_phase_seq(entity)
    if seq:
        backing = getattr(entity.work_projection, "_route_view", None) or {}
        shown = [(n, st) for n, st in seq if st != "skipped"] or seq
        mode = backing.get("spec_mode")
        knob_items = []
        if mode:
            knob_items.append(mode.replace(",", "·"))   # multiple modes ride the same axis
        if cap_intensity:                                # intensity from the grounding marker
            knob_items.append(cap_intensity)
        prefix = [("spec", "name_dim")]
        if knob_items:
            prefix += [("(", "dim"), ("·".join(knob_items), "dim"), (")", "dim")]
        prefix += [(" : ", "dim")]
        used = sum(_dw(text) for text, _key in prefix)
        body = _route_stage_segs(shown, working, max(4, max_width - used))
        return prefix + body
    if cap.get("capability"):
        # Inline entry work with no route/dispatch/spec projection: `capability(mode·intensity)`,
        # the same syntax the dispatch options column uses (user 2026-07-24 "다른 인라인도 메인
        # 세션에 떠야").
        #
        # F-77 (user 2026-08-14: "정확히는 무슨 skill 지금 쓰는 중인지 정도로만 … 이미 stages의
        # 위치가 가지는 의미가 fleet에서 모호해졌고, depth=1,2도 그 자리에 stage라기 보다는
        # unit, capa, node 정보를 남고 있으니까"): this cell stopped being a STAGE column some
        # time ago — depth-1 shows `code(dev·thr·owner)`, depth-2 shows `code-execute(thr)`, so
        # what it actually answers is "which unit of work is this". A main session answers the
        # same question with the capability it is inside, and stops there. No inferred stage
        # trails it: the only inline stage signal available was artifact FILENAMES, whose
        # vocabulary is the code cycle's, so an autopilot-spec session rendered `spec(direct) :
        # exec` — a code-pipeline stage word under a spec capability (observed live). A tag that
        # can contradict its own capability is worse than no tag.
        name = cap["capability"].replace("autopilot-", "")
        if not _SHOW_ALL:
            name = _user_label(name)
        knob_items = [k for k in (cap.get("mode"), cap_intensity) if k]
        # User 2026-09-18 ("메인세션에서 라우팅 스킬도 회색인데?"): on a main row this tag is
        # the ONLY sign of the work it is in — no breadcrumb follows it, unlike the owner card
        # and the spec branch above — so it must not recede like a dial label. Same status rule
        # as the projection fallback below: work hue while working, plain text otherwise; the
        # knobs stay dim.
        segs = [(name, "g_work" if working else None)]
        if knob_items and _SHOW_ALL:
            segs += [("(", "dim"), ("·".join(knob_items), "dim"), (")", "dim")]
        return segs
    text = _projection_stage_text(entity, max_width=max_width)
    return [(text or "-", "g_work" if text and working else "dim")]


def _session_row(s, narrow, is_parent=False, child_count=0, name_width=None,
                 show_projection_stage=True, stage_zone=None, tag_by_key=None):
    live = s.liveness
    slug = s.slug or (s.cwd.rsplit("/", 1)[-1] if s.cwd else "?")
    dim_tel = live in ("stale", "dead") or s.app_server or s.detached
    dead_stale = live in ("stale", "dead")   # F-13: telemetry gone, replaced with a single age cell
    gch, gkey = _glyph(live)
    if s.detached and live not in ("stale", "dead"):
        gch, gkey = _DETACHED_GLYPH, "g_work_off"   # detached: loading axis, dim-green
    # main↔spawned weight = font-color intensity (no bg fill — the reverse badge read as weird):
    # a live top-level session gets the BRIGHT harness color; muted (stale/dead/app-server) drops
    # to dim. Dispatch rows use the DIM harness color (see _dispatch_row).
    hkey = (_BADGE_KEY.get(s.harness, "dim") if dim_tel
            else ("hb_" + s.harness if s.harness in _BADGE_TEXT else "hb_other"))
    # F-76b (user 2026-08-14 "메인세션은 세션 명이 흰색이고 서브 세션은 컬러네? 이거 톤을
    # 맞춰야겠네"): a dispatch row already paints its harness cell and its NAME with one key;
    # only the session row split them — harness hue on the left, plain white in the name zone
    # — so the two altitudes read as different color SYSTEMS rather than two weights of one.
    # The name takes the harness hue on its OWN key (`nm_*`/`nmd_*`, same color as `hkey`),
    # which leaves the main↔spawned hierarchy exactly where it already lived: bright harness
    # hue for a live session, dim for a dispatch row.
    name_key = (_NAME_KEY_DIM.get(s.harness, "nmd_other") if dim_tel
                else _NAME_KEY.get(s.harness, "nm_other"))
    # F-33 (v11): harness field carries model/effort as a parenthetical — a dead/stale row has
    # no live telemetry to show (F-13), so it renders the bare harness name only.
    chip = _session_tag_chip(s, dim=dim_tel)          # F-100a — inside the _HMW field
    chip_w = sum(_dw(text) for text, _key in chip)
    segs = [("  ", None), (gch, gkey), (" ", None)] + chip
    segs += _harness_model_cell(s.harness, None if dead_stale else s.model,
                                None if dead_stale else s.effort, _HMW - chip_w,
                                hkey, dim=dim_tel,
                                effort_default=not dead_stale and bool(getattr(s, "effort_default", False)))

    # F-22: reserve identity suffixes first, then let the title consume the
    # responsive name column. Calls without a terminal-derived width retain the
    # legacy 24-cell cap for hermetic/backward-compatible row construction.
    avail = max(3, name_width or _NW_S)
    name_txt = _display_session_subject(s)
    suffix = []
    suffix_w = 0
    if is_parent and child_count:
        child_tag = " ▾%d" % child_count
        if _dw(child_tag) < avail:
            # Ownership is hierarchy, not telemetry. Keep the compact shape but give it
            # the same harness hue as the main-session name so it remains legible beside
            # the deliberately dim dispatch card without adding a badge or another row.
            suffix.append((child_tag, name_key))
            suffix_w += _dw(child_tag)
    # F-26: the unused badge outranks the provenance tag — it is the whole reason the
    # row is surfaced, so it is the last identity tag to drop, not the first.
    unused_at = None
    if live == "unused":
        ub = _unused_badge(s)
        if suffix_w + _dw(ub) < avail:
            unused_at = len(suffix)
            suffix.append((ub, "g_unused_b"))
            suffix_w += _dw(ub)
    elif live == "blocked":
        chip, chip_w = _interaction_chip(s)
        if chip and suffix_w + chip_w < avail:
            suffix.extend(chip)
            suffix_w += chip_w
    peer_segs, peer_w = _peer_inline_fit(
        s, tag_by_key, max(0, avail - suffix_w - _NAME_GAP - 1))
    if peer_segs:
        suffix.extend(peer_segs)
        suffix_w += peer_w
    # Degradation ladder, tightest-last (the F-22 40-cell cap makes this reachable at every
    # width, so it cannot be left to chance): provenance drops first, then the badge's age,
    # and only then does the name itself clip. The name is what identifies the row — F-26
    # exists to stop anonymous rows (prd.md:247), so it yields last.
    prov = getattr(s, "provenance", None)
    if prov:
        ptag = " %s" % prov
        # best-effort by contract → only shown when the name keeps its full width anyway.
        if avail - suffix_w - _dw(ptag) - _NAME_GAP >= _dw(name_txt):
            suffix.append((ptag, "dim"))
            suffix_w += _dw(ptag)
    # F-99c's dim ` · hearting-46` companion is gone (F-100a): it competed with the title
    # for the same cells and was the first thing to yield. The tag chip carries it now.
    if unused_at is not None and avail - suffix_w - _NAME_GAP < _dw(name_txt):
        short = (_unused_badge(s, compact=True), "g_unused_b")
        suffix_w -= _dw(suffix[unused_at][0]) - _dw(short[0])
        suffix[unused_at] = short
    title_budget = max(1, avail - suffix_w - _NAME_GAP)
    if name_width is None:
        title_budget = min(title_budget, _TITLE_MAX)
    shown = _clip_w(name_txt, title_budget)
    segs.append((shown, _subject_name_key(s, name_key)))
    used = _dw(shown)
    for text, key in suffix:
        segs.append((text, key))
        used += _dw(text)
    branch_segs = _branch_suffix_segs(
        s.cwd, s.branch, dim=dim_tel,
        ahead=getattr(s, "branch_ahead", None),
        behind=getattr(s, "branch_behind", None))
    segs += branch_segs
    used += sum(_dw(text) for text, _key in branch_segs)
    session_width = avail + _BRANCH_SUFFIX_W
    if used < session_width:
        segs.append((" " * (session_width - used), None))

    segs = _pad_to_column(segs, _session_routing_column("wide", name_width))
    if show_projection_stage:
        # Responsive stage zone (2026-07-24): the old flat label was hard-clipped at 24 cells and
        # truncated to `…` even on a wide terminal with a mostly-empty zone (user "stage 폭 엄청
        # 길잖아 근데 왜 금방 … 표시로 줄이는지"). Use the same terminal-aware budget the conductor
        # breadcrumb got, so a spec phase breadcrumb / long label fills the real space first.
        segs += _session_stage_segs(s, live == "working", stage_zone or _STAGE_ZONE_MAX,
                                    tag_by_key)
    else:
        segs += _suppressed_stage_segs(s, live == "working", stage_zone or _STAGE_ZONE_MAX,
                                       tag_by_key)
    if s.app_server:
        segs.append(("  app-server", "dim"))
    if s.orphan:
        segs.append(("  worktree-gone", "g_dead"))

    # F-68c (user "time 없애라고 했는데 왜 계속 뜨는건데", "다른 곳에는 다 남아있잖아"):
    # the right-flushed uptime COLUMN is retired board-wide, not only inside cards.
    # Elapsed rides inline as a dim bare `<t>` value right after the row's content, the
    # same grammar the summary rows and the in-card dispatch rows use.
    segs += [("  ", None), (_ELAPSED_GLYPH + fmt_min(s.elapsed_min), "dim")]
    if dead_stale:
        segs.append(("  done · " + _text_age(s.mtime), "dim"))
    return segs


def _dispatch_box_width(term_width, layout=None):
    """Raw segment width whose right edge lands inside the group tint band."""
    # No-width calls are hermetic component tests, not a real viewport. Keep the
    # historical no-clipping behavior while still returning a deterministic box.
    width = int(term_width) if term_width else 200
    # Tinted curses rows gain the outer inset and inner padding in `_addline`, so a
    # raw index lands at screen col `_INSET + _PAD_IN + index` and the tint band's
    # last paintable column is `width - _INSET - 1`. F-68d (user "우측은 시종일관
    # 그대로"): the earlier budget put the right border ON that last band column
    # while the left border sat four cells inside it — the asymmetry the user kept
    # seeing. Pull the border two cells off the band edge so both sides breathe.
    # F-68d (user "우측은 시종일관 그대로"): the original contract put the right border
    # ON the tint band's last column — flush against the card edge while the left
    # side breathed. The border now stops `_CARD_EDGE_GAP` cells short of that edge.
    return max(_RAIL_COL + 4,
               width - (_INSET + _PAD_IN + 2 + _CARD_EDGE_GAP) if _TINT_OK
               else width - 1)   # plain/8-color path keeps its one-cell edge guard


def _opts_segs(j, max_width=None):
    if not _SHOW_ALL:
        # Keep internal mode/profile/shape contracts in the existing all/JSON
        # detail surfaces. The normal row says what the worker is doing.
        action = _user_label(_entry_skill(j) or getattr(j, "key", None))
        owner = _is_owner_mode_row(j) and action != "owner"
        suffix = " owner" if owner else ""
        if max_width is not None:
            action = _clip_w(action, max(1, max_width - _dw(suffix)))
            if _dw(action + suffix) > max_width:
                suffix = ""
        segs = [(action, "name_dim")] if action else []
        if suffix:
            segs.append((suffix, "dim"))
        return segs, _dw(action + suffix)
    """F-15a options column — HIERARCHICAL dial (user 2026-07-20: "계층적으로
    code (mode inten) / boot 순"). Three axes, three visual levels instead of the flat
    '·' chain that mixed them: the entry skill heads the dial, its behaviour knobs
    (mode·intensity) ride in a dim paren group, and the environment tail
    (profile home / role suffix) is set off by ' / '. A dispatch-depth-2 worker names its
    assigned stage skill and keeps only worker-local intensity/profile: inherited
    owner mode and legacy personas (qa/development/maker) are not child identity.
    qa left this dial with the retired qa axis (user 2026-07-16 — rigor derives
    from intensity, CONVENTIONS §1.1)."""
    depth = max(1, int(getattr(j, "depth", 1) or 1))
    stage_worker = depth >= 2 and not _is_owner_mode_row(j)
    entry = _entry_skill(j)
    if stage_worker:
        knob_items = [t for t in (_short_level(getattr(j, "intensity", None)),) if t]
    else:
        knob_items = [t for t in (
            _display_capability_mode(j),
            _short_level(getattr(j, "intensity", None)),
        ) if t]
    # F-<next>: a compose-shaped owner route names its shape as a fourth behaviour knob
    # (mode·intensity·shape·role, plan §3 C-4.4) — owner rows only, and only when the
    # bound route was actually composed (never a compile-shaped preset route).
    shape = None if stage_worker else _owner_route_shape(j)
    if shape:
        knob_items.append(shape)
    # the worker ROLE is a behaviour knob too (who the worker acts as), not environment —
    # it rides the paren group's last slot (user 2026-07-20: "owner의 위치가 애매").
    role = "" if stage_worker else _dispatch_role_suffix(
        j, max_width=max(0, _PROFILE_MAX - sum(len(t) + 1 for t in knob_items)))
    if role:
        knob_items.append(role)
    knobs = "·".join(knob_items)
    if (shape and max_width is not None
            and (len(entry or "") + len(knobs) + 2) > max_width):
        # K-7: shape is the first thing this dial drops when narrow.
        knob_items = [t for t in knob_items if t != shape]
        knobs = "·".join(knob_items)
    # F-78 (user 2026-08-14 "unit, node, profile 등이 좋긴 좋은데 … 전부 다 뜰때 가로로 너무
    # 길어지는데"). Measured at 50-54 cells. Two of those cells' worth of content was not
    # information:
    #  · the MODEL PROFILE is the policy that picked a model, and the model it picked is
    #    already on this very row, in the harness cell (`codex (gpt-5.6-luna·medium)`).
    #    A derived value sitting beside its own result earns nothing, so it goes.
    #  · the `unit:`/`mp:` prefixes cost 5-6 cells to say what the shape already says —
    #    a unit is always `<group>/<name>`.
    # `_dispatch_profile` (the profile HOME, not the model profile) stays: nothing else on
    # the row carries it.
    env_tail = [value for value in (_dispatch_profile(j),) if value]
    unit = getattr(j, "unit", None)
    projected_unit = unit or (
        None if _is_owner_mode_row(j) else getattr(j, "worker_mode", None)
    )
    if projected_unit:
        display_unit = str(projected_unit)
        if display_unit.startswith("_"):
            display_unit = display_unit[1:]
        group, _sep, leaf = display_unit.partition("/")
        entry_words = entry.split("-") if entry else []
        # A unit whose LEAF merely restates a knob is the row saying the same thing twice
        # (`_kernel/owner` beside `·owner`), so it drops whole. A unit whose GROUP restates
        # a knob or the entry skill keeps only its leaf (`code-plan … plan/frame` -> `frame`).
        if leaf and leaf in knob_items:
            display_unit = ""
        elif leaf and (group in knob_items or group in entry_words):
            display_unit = leaf
        if display_unit:
            env_tail.append(_compact_dispatch_name(display_unit, _PROFILE_MAX))
    warn_tail = []
    if _mode_axis_conflict(j):
        warn_tail.append("mode!")
    contract_status = getattr(j, "attempt_contract_status", None)
    if contract_status == "legacy-read-only":
        warn_tail.append("legacy")
    elif contract_status and contract_status != "current":
        warn_tail.append("contract!")
    # F-68: inside a framed card the dial folds to whole components instead of
    # being tail-cut mid-token at the box border — environment components drop
    # from the end (unit: first, then the profile pair), warnings never drop.
    if max_width is not None:
        def _dial_w(env):
            body = (len(entry) if entry else 0) + (len(knobs) + 2 if knobs else 0)
            parts_ = env + warn_tail
            return body + sum(len(v) + 3 for v in parts_)
        while env_tail and _dial_w(env_tail) > max_width:
            env_tail.pop()
    tail = " / ".join(env_tail + warn_tail) or None
    parts = []
    w = 0

    def _add(text, key):
        nonlocal w
        parts.append((text, key))
        w += len(text)

    if entry:
        _add(entry, "name_dim")
    if knobs:
        if entry:
            # flush paren (user 2026-07-20: "code 뒤에 괄호 여백 지우고") — same idiom as
            # the F-33 flush '·'.
            _add("(", "dim"); _add(knobs, "dim"); _add(")", "dim")
        else:
            _add(knobs, "dim")
    if tail:
        if parts:
            _add(" / ", "dim")
        _add(tail, "dim")
    return parts, w


def _dispatch_row(j, orphan=False, parent_model=None, parent_harness=None, is_last=True,
                  parent_effort=None, stage_override=None, name_width=None, route_seq=None,
                  route_zone=None, in_card=False, unit_working=None,
                  card_interior=None, card_rail_col=None):
    """A dispatch job rendered as a session-ANALOGUE, mirroring the session columns 1:1:
      harness (model · effort)  |  [stage label] session (branch)  |  stage breadcrumb  |  time
    F-33 (v11): model/effort fold into the harness field (no more separate model column).
    F-15a: the name zone is identity-only (no more parenthetical mode/qa tag — that moved to
    its own options column). A dispatch-depth-2 stage worker's identity is its stage label + slug
    (P0-1); its breadcrumb slot shows its own micro-status instead of repeating the parent
    conductor's full breadcrumb.
    """
    key = j.key or "?"
    depth = max(1, int(getattr(j, "depth", 1) or 1))
    in_card_time = None
    stage = stage_override if stage_override is not None else (j.stage or "")
    # F-64: the registry word is terminal but the row's own pid identity is alive —
    # keep the verbatim word and mark the contradiction instead of hiding either side.
    if getattr(j, "row_terminal_mismatch", False) and stage:
        stage = stage + " ⚠alive"
    # The dispatched session's own haiku sidecar title is its identity when present
    # (user 2026-07-16: the summary agent attaches to every dispatched session); the
    # slug stays the fallback — same title → name → slug chain as session rows.
    slug_name = _subject_title(j) or j.slug or key
    gch, gkey = _glyph(j.liveness, dim=True)
    afterglow_j = bool(getattr(j, "afterglow", False))
    if afterglow_j:
        gch, gkey = _LIVE_GLYPH["done"], "dim"   # F-46: dim ✓, never the live green
    # F-46 rides the same "no live telemetry" lane as dead/stale (F-13): a finished row has
    # no model/effort/stage left worth tinting.
    dead_stale_j = j.liveness in ("dead", "stale") or afterglow_j
    # SD-F3: the job's own effort is first-class; when it's absent (proc-scan rows — env
    # doesn't export it yet), fall back to the parent's effort, shown plain (user
    # 2026-07-16: the `~` derived-value marker is retired — qa left the display with the
    # retired qa axis at the same time). F-64b (v49 정정, user 2026-08-05 "done이 되면 앞에
    # 뜨던 정보들이 없어지고 - 만 남아서"): model/effort are the attempt's static IDENTITY,
    # not live telemetry — a finished (afterglow/stale) row keeps them dim; only a DEAD row
    # still wipes them (F-13's honest-collapse survives where the row actually crashed).
    eff = None if j.liveness == "dead" else (j.effort or parent_effort or None)

    # DIFFERENTIAL indent (harness 2 cols deeper than a session) with a ↳ spawn arrow off the
    # parent's dot column (user pick over ├─/└─ tree bars); the harness field is narrowed by 2 so
    # the NAME still lands at the shared _NAME_COL — name onward aligns with sessions. DIM =
    # spawned. F-33 (v11): the widened field also carries the job's own model/effort as a
    # parenthetical (SD-F3).
    prefix = _dispatch_prefix(j, orphan=orphan, in_card=in_card, card_rail_col=card_rail_col)
    segs = [("  ", None), (prefix, "dim"), (gch, gkey), (" ", None)]
    segs += _harness_model_cell(j.harness,
                                None if j.liveness == "dead" else
                                _job_display_model(j, parent_model),
                                eff, max(1, _HMW - len(prefix)),
                                _BADGE_KEY.get(j.harness, "dim"), dim=True, unknown="—")
    avail = max(3, name_width or _NW_S)
    registry_split = getattr(j, "note", None) == "registry-split"
    otag = "  (registry-split)" if registry_split else "  (orphan)" if orphan else ""
    label = _dispatch_stage_label(j)
    # F-64 (v49): the dispatch name uses the REAL responsive zone like session rows do —
    # the old unconditional `_TITLE_MAX` clamp ellipsized names at 24 cells and left the
    # rest of the column as dead space before ` (branch)` (user 2026-08-05 "줄임 표시가
    # 너무 널널한데? 공간이 떠"). Hermetic/legacy callers (no name_width) keep the clamp.
    name_room = max(3, avail - len(otag) - _NAME_GAP)
    if name_width is None:
        name_room = min(_TITLE_MAX, name_room)
    if label:
        # P2-6 composed cap: slug shares the same budget as its stage label.
        slug_room = max(1, name_room - len(label) - 1)
        nm = label + " " + _compact_dispatch_name(slug_name, slug_room)
    else:
        nm = _compact_dispatch_name(slug_name, name_room)
    used = _dw(nm)
    # user 2026-07-20: 분사 행 제목도 컬러 — the dim HARNESS hue, so the title row stays
    # dark but no longer reads identical to its grey subtitle line underneath. Dead/stale
    # rows keep the colorless dim (F-13: no live telemetry, nothing to tint).
    # F-75b (user 2026-08-14 "done worker의 session에 컬러는 유지하는게 좋겠어 회색이니까
    # 설명이랑 구분이 안되네"): a FINISHED row keeps its harness hue for exactly the reason
    # the hue was introduced — a colorless title reads identical to the grey summary line
    # under it. This is F-64b's rule applied to the name: afterglow/stale keep the attempt's
    # static identity, and only a DEAD row collapses to colorless dim (F-13 honest-collapse,
    # where the row actually crashed and there is nothing left to identify).
    name_key_j = ("nm_dead" if j.liveness == "dead"
                  else _NAME_KEY_DIM.get(j.harness, "nmd_other"))
    segs.append((nm, _subject_name_key(j, name_key_j)))
    segs.extend(_interaction_chip(j)[0])
    if otag and used + len(otag) <= avail:
        segs.append((otag, "g_dead" if registry_split else "gate_u")); used += len(otag)
    branch_segs = _branch_suffix_segs(
        "" if key in _LOOPS_KEYS else j.cwd, j.branch, optional=key in _LOOPS_KEYS,
        ahead=getattr(j, "branch_ahead", None),
        behind=getattr(j, "branch_behind", None),
        location_kind=None if key in _LOOPS_KEYS else getattr(j, "location_kind", None),
        location_repo=getattr(j, "location_repo", None))
    segs += branch_segs
    used += sum(_dw(text) for text, _key in branch_segs)
    session_width = avail + _BRANCH_SUFFIX_W
    if used < session_width:
        segs.append((" " * (session_width - used), None))

    if _route_rides_the_rail(j, route_seq, in_card):
        segs = _pad_to_column(segs, _session_routing_column("wide", name_width))
        segs += _opts_segs(j, max_width=_DIAL_MAX)[0]
    elif j.liveness == "dead":
        segs.append((" " * _WIDE_STAGE_GAP, None))
        if getattr(j, "note", None) == "dead-parent-orphaned":
            # SD-64/71: distinct from the generic dead-conductor cell — never blank, and
            # always names the exact node a dispatch-depth-0 decision would resume from.
            boundary = getattr(j, "resume_boundary", None) or "-"
            segs.append(("⚠ ORPHANED resume=%s" % boundary, "g_dead"))
        elif getattr(j, "_dead_terminal_owner", False):
            # F-83: the supervisor's typed closure word plus the resume node, so the ✕ card
            # says why the owner left and where a depth-0 decision would pick the route up.
            boundary = getattr(j, "resume_boundary", None) or "-"
            segs.append(("✕ %s resume=%s" % (getattr(j, "note", None) or "dead", boundary),
                         "g_dead"))
        else:
            # P2-11: a dead job's last-known stage replaces the redundant "last seen <age>"
            # (the time column already shows elapsed) — "dead @exec" tells you WHERE it died.
            last_stage = stage if stage not in (None, "", "open", "running") else key
            segs.append(("dead @%s" % last_stage, "g_dead"))
    elif afterglow_j or j.liveness == "stale":
        # F-46 afterglow / F-13 stale, both corrected by F-64b (v49, user 2026-08-05 "done이
        # 되면 앞에 뜨던 정보들이 없어지고 - 만 남아서"): the options dial is static identity
        # like model/effort above, so the finished row keeps it dim instead of collapsing to
        # a bare `-`. Only the STAGE SLOT changes to a steady done token — never a blinking
        # frame. F-64a: dispatch-depth-2 workers show a bare `✓ done` (elapsed already rides the
        # right-flushed time column); dispatch-depth-1 afterglow keeps its counting-up elapsed (F-46)
        # and dispatch-depth-1 stale its age (`done <age>`, the v49 "last seen" successor).
        segs.append((" " * _WIDE_STAGE_GAP, None))
        opt_segs, optw = _opts_segs(j, max_width=_DIAL_MAX)
        segs += opt_segs
        if optw < _OPTW:
            segs.append((" " * (_OPTW - optw), None))
        # user 2026-08-05 "done 앞에 콜론은 그대로 있어야지 체크 표시는 done 뒤로": the
        # done token rides the same ` : ` stage-zone lead-in the running token uses, and
        # the check TRAILS the word — `: done ✓`, mirroring `: running`.
        # F-78 (measured 2026-08-14 at 200 columns: `: done ✓ 8m  8m`): every row now ends
        # with F-68's inline elapsed tag, and these tokens still carried the SAME
        # `j.elapsed_min` from back when only F-46's depth-1 rows had a clock of their own.
        # One elapsed per row — the tag — so the token is the state word alone. At 140
        # columns the duplicate had been costing the token itself, clipping it to `done …`.
        token = "done %s" % _LIVE_GLYPH["done"]
        segs += _stage_zone_segs([(token, "dim")])
    else:
        # F-15a options column (fixed-ish gap, dim mode/qa/profile) — a declutter move OUT of
        # the name zone, not a new axis. model/effort now live in the harness field (F-4/SD-F3).
        segs.append((" " * _WIDE_STAGE_GAP, None))
        opt_segs, optw = _opts_segs(j, max_width=_DIAL_MAX)
        segs += opt_segs
        if optw < _OPTW:
            segs.append((" " * (_OPTW - optw), None))

        # F-68: a framed row has no elastic right-flush — the border is a hard
        # edge. The open-row zone ledger is tuned for 168 columns and let the
        # breadcrumb spill into the flush area invisibly; inside a card the real
        # budget is whatever remains before the border, measured off the segs
        # actually emitted so far.
        def _stage_part(budget):
            if _route_rides_the_rail(j, route_seq, in_card):
                return []
            stall = _stall_attention_stage_segs(j, budget)
            if stall is not None:
                return stall
            residue = getattr(j, "residue_pids", None)
            if residue:
                return _stage_zone_segs([("⚠ worker left pid %s running" % ",".join(map(str, residue)), "gate_u")])
            bc = _dispatch_stage_segs(j, key, stage, slug_name,
                                      working=(unit_working if unit_working is not None
                                               else j.liveness == "working"),
                                      route_seq=route_seq, route_zone=budget,
                                      compact_route=in_card)
            # F-81 unresolved-owner fallback: a standard+ owner card whose route never
            # resolved (including a wholly absent owner binding) would otherwise fall through to
            # the generic `preparing…`/empty-breadcrumb fallback. Show a dim `—`
            # instead, so an unresolved owner card does not claim a stage it cannot
            # prove. Quick/legacy non-owner rows and rows outside a card keep the
            # pre-existing fallback.
            if (in_card and depth == 1
                    and getattr(j, "worker_type", None) == "owner"
                    and (getattr(j, "intensity", None) or "") != "quick"
                    and not route_seq):
                return _stage_zone_segs([("—", "dim")])
            return _stage_zone_segs(bc)
        if in_card and card_interior:
            consumed = sum(_dw(t) for t, _k in segs)
            # F-68b (user): the box keeps its elapsed, but as an inline bare `<t>` value
            # right after the content — summary-row style — never a reserved
            # column. Reserve its cells in the same ledger.
            in_card_time = _ELAPSED_GLYPH + fmt_min(j.elapsed_min)
            time_w = _dw(in_card_time) + 2
            # Rough fold budget only — the frame's tag-aware elastic clip is the
            # final authority, so a path with a wider lead-in degrades to an
            # ellipsis instead of losing the tag or the border.
            segs += _stage_part(max(8, card_interior - consumed - 5 - time_w))
        else:
            # F-83: the open-row zone ledger (`_route_zone_width`) is tuned for
            # 168 columns, so below that an unframed conductor row (orphans —
            # the `··` prefix rows outside every card) kept the fixed 42-cell
            # breadcrumb zone and overflowed the terminal (measured 159/164
            # cells at 140 columns). Clamp to the cells actually left before
            # the shared card right edge, the same consumed-based ledger the
            # framed branch above uses; on a ≥168-column terminal the original
            # zone is the smaller bound and nothing changes.
            budget = route_zone
            if card_interior:
                consumed = sum(_dw(t) for t, _k in segs)
                tail_w = _dw(_ELAPSED_GLYPH + fmt_min(j.elapsed_min)) + 2
                open_budget = max(8, card_interior - consumed - tail_w)
                budget = min(route_zone, open_budget) if route_zone else open_budget
            segs += _stage_part(budget)

    # F-68 (user 2026-08-14 "time은 빼고 설명 요약쪽처럼 일관성", "열 자체를 없애라",
    # "박스 내부에도 time 표시는 하고"): a framed row keeps no right-flushed time
    # COLUMN — the elapsed rides inline as a dim bare `<t>` value right after the row's
    # content, summary-row style, and the border stays the hard right terminus.
    if in_card:
        if in_card_time is None and card_interior:
            in_card_time = _ELAPSED_GLYPH + fmt_min(j.elapsed_min)
        if in_card_time is not None:
            segs += [(_CARD_TAG, None), (in_card_time, "dim")]
    else:
        segs += [("  ", None), (_ELAPSED_GLYPH + fmt_min(j.elapsed_min), "dim")]
    return segs


# ---------- 2-line cards (round-4 responsive narrow mode) ----------
# L1 = identity (dot · harness · session (branch) · ▾N · gate) / L2 = telemetry (model · effort ·
# bracket gauge · cost · ⏱). model keeps its fixed width so gauges align vertically across cards
# (the nvtop column feel). Same segment parts as the 1-line rows — zero new color keys.
def _session_row_2line(s, is_parent=False, child_count=0, _split=False, term_width=None,
                       show_projection_stage=True, tag_by_key=None):
    live = s.liveness
    slug = s.slug or (s.cwd.rsplit("/", 1)[-1] if s.cwd else "?")
    dim_tel = live in ("stale", "dead") or s.app_server or s.detached
    gch, gkey = _glyph(live)
    if s.detached and live not in ("stale", "dead"):
        gch, gkey = _DETACHED_GLYPH, "g_work_off"
    hn = _BADGE_TEXT.get(s.harness, "?")
    hkey = (_BADGE_KEY.get(s.harness, "dim") if dim_tel
            else ("hb_" + s.harness if s.harness in _BADGE_TEXT else "hb_other"))
    name_key = (_NAME_KEY_DIM.get(s.harness, "nmd_other") if dim_tel
                else _NAME_KEY.get(s.harness, "nm_other"))   # F-76b, see `_session_row`
    chip = _session_tag_chip(s, dim=dim_tel)          # F-100a — inside the _HW field
    chip_w = sum(_dw(text) for text, _key in chip)
    l1 = ([("  ", None), (gch, gkey), (" ", None)] + chip
          + [(_badge_cell(hn, _HW - chip_w), hkey)])   # word-boundary clip, last cell blank
    suffix = []
    if is_parent and child_count:
        suffix.append((" ▾%d" % child_count, name_key))
    unused_at = None
    if live == "unused":                       # F-26 parity with the wide row
        unused_at = len(suffix)
        suffix.append((_unused_badge(s), "g_unused_b"))
    elif live == "blocked":
        suffix.extend(_interaction_chip(s)[0])
    # provenance is optional here: in the narrow/stack layouts every suffix cell
    # is taken straight out of the name, so a 9-cell tag can clip a real name down to "age…".
    # A name the user can read outranks knowing who launched it — drop the tag instead.
    # Position is fixed here (identity tags, before branch) to match the wide row; whether it
    # is actually inserted is decided below, once the name's budget is known.
    prov_seg = (" %s" % s.provenance, "dim") if getattr(s, "provenance", None) else None
    prov_pos = len(suffix)
    br_segs = _branch_suffix_segs(
        s.cwd, s.branch, dim=dim_tel, optional=True,
        ahead=getattr(s, "branch_ahead", None),
        behind=getattr(s, "branch_behind", None))
    if not _split:
        suffix.extend(br_segs)
    if s.app_server:
        suffix.append(("  app-server", "dim"))
    if s.orphan:
        suffix.append(("  worktree-gone", "g_dead"))
    name_txt = _display_session_subject(s)
    if term_width:
        # A tinted L1 has 4 left cells (inset+padding) and a 2-cell right
        # inset. Reserve those six cells plus every suffix before clipping.
        prefix_w = sum(_dw(text) for text, _key in l1)
        avail = int(term_width) - 6 - prefix_w
        suffix_w = sum(_dw(text) for text, _key in suffix)
        # Shed the badge's age before shedding the name (see _unused_badge). L2 carries the
        # elapsed time directly beneath, so nothing is lost — it is the same value twice.
        if unused_at is not None and avail - suffix_w < _dw(name_txt):
            short = (_unused_badge(s, compact=True), "g_unused_b")
            suffix_w -= _dw(suffix[unused_at][0]) - _dw(short[0])
            suffix[unused_at] = short
        # Add provenance only if the name still gets its full width afterwards.
        if prov_seg and avail - suffix_w - _dw(prov_seg[0]) >= _dw(name_txt):
            suffix.insert(prov_pos, prov_seg)
            suffix_w += _dw(prov_seg[0])
        # Communication shares L1 with the session identity. Endpoint labels
        # shed to arrows while preserving both directions and four title cells.
        min_name_w = min(4, _dw(name_txt))
        peer_options = _peer_inline_options(s, tag_by_key)
        peer_min_w = (sum(_dw(text) for text, _key in peer_options[-1])
                      if peer_options else 0)
        peer_budget = max(0, avail - suffix_w - min_name_w)
        if peer_min_w > peer_budget and prov_seg in suffix:
            suffix.remove(prov_seg)
            suffix_w -= _dw(prov_seg[0])
            peer_budget = max(0, avail - suffix_w - min_name_w)
        if peer_min_w > peer_budget and not _split:
            for branch_seg in br_segs:
                if branch_seg in suffix:
                    suffix.remove(branch_seg)
                    suffix_w -= _dw(branch_seg[0])
            peer_budget = max(0, avail - suffix_w - min_name_w)
        peer_segs, peer_w = _peer_inline_fit(s, tag_by_key, peer_budget)
        if peer_segs:
            suffix.extend(peer_segs)
            suffix_w += peer_w
        title_budget = max(4, avail - suffix_w)
    else:
        if prov_seg:
            suffix.insert(prov_pos, prov_seg)
        peer_segs, _peer_w = _peer_inline_fit(s, tag_by_key, None)
        if peer_segs:
            suffix.extend(peer_segs)
        title_budget = min(_NAME2_MAX, _TITLE_MAX)
    l1.append((_clip_w(name_txt, title_budget), _subject_name_key(s, name_key)))
    l1.extend(suffix)

    # L2: elapsed time sits UNDER the harness column (fills the old empty indent — user
    # Put time under the harness, model under the name, and gauge immediately after.
    # indent / no far-right flush).
    l2 = [(" " * _SESSION_DETAIL_COL, None),
          (_pad(fmt_min(s.elapsed_min), 4 + _HW - _SESSION_DETAIL_COL), "dim")]
    l2 += _model_cell(s.model, s.effort, _MW, dim=dim_tel)
    # Same cell as the wide row (capability tag, route chain, spec breadcrumb): this card
    # used to call the bare projection text and showed `-` for inline work the wide row named.
    l2 = _pad_to_column(l2, _session_routing_column("narrow"))
    stage_fn = _session_stage_segs if show_projection_stage else _suppressed_stage_segs
    l2 += stage_fn(s, live == "working", _narrow_session_cell_budget(term_width), tag_by_key)
    if live in ("stale", "dead"):
        l2.append(("  done · " + _text_age(s.mtime), "dim"))
    # v16: context is emitted by _context_detail_row beneath the complete card.
    if _split:
        return l1, l2, br_segs
    return l1, l2


def _session_row_stack(s, is_parent=False, child_count=0, term_width=None,
                       show_projection_stage=True, tag_by_key=None):
    """v16 ultra-narrow card: identity and telemetry, with detail row emitted separately."""
    l1, l2 = _session_row_2line(
        s, is_parent, child_count, term_width=term_width,
        show_projection_stage=show_projection_stage, tag_by_key=tag_by_key)
    return [l1, l2]


def _dispatch_row_stack(j, orphan=False, parent_model=None, parent_effort=None, stage_override=None,
                        route_seq=None, in_card=False, unit_working=None, card_rail_col=None):
    l1, l2 = _dispatch_row_2line(j, orphan=orphan, parent_model=parent_model,
                                 parent_effort=parent_effort, stage_override=stage_override,
                                 route_seq=route_seq, in_card=in_card,
                                 unit_working=unit_working, card_rail_col=card_rail_col)
    if _route_rides_the_rail(j, route_seq, in_card):
        return [l1, l2]
    gi = _stack_split(l2)
    return [l1, l2[:gi], [(" " * (4 + _HW), None)] + l2[gi:]]


def _dispatch_row_2line(j, orphan=False, parent_model=None, parent_effort=None, _split=False,
                        stage_override=None, route_seq=None, in_card=False, unit_working=None,
                        card_rail_col=None):
    """F-15a narrow card — L1 = identity ONLY (stage label + slug, no mode/qa tag); L2 =
    elapsed · model · options (relocated from L1) · breadcrumb/micro-status."""
    key = j.key or "?"
    depth = max(1, int(getattr(j, "depth", 1) or 1))
    # The dispatched session's own haiku sidecar title is its identity when present
    # (user 2026-07-16: the summary agent attaches to every dispatched session); the
    # slug stays the fallback — same title → name → slug chain as session rows.
    slug_name = _subject_title(j) or j.slug or key
    gch, gkey = _glyph(j.liveness, dim=True)
    afterglow_j = bool(getattr(j, "afterglow", False))
    if afterglow_j:
        gch, gkey = _LIVE_GLYPH["done"], "dim"   # F-46 parity with the wide row
    hn = _BADGE_TEXT.get(j.harness, "—") if j.harness else "—"

    prefix = _dispatch_prefix(j, orphan=orphan, in_card=in_card, card_rail_col=card_rail_col)
    label = _dispatch_stage_label(j)
    if label:
        slug_room = max(1, _DISPATCH_NAME_MAX - len(label) - 1)
        shown_name = label + " " + _compact_dispatch_name(slug_name, slug_room)
    else:
        shown_name = _compact_dispatch_name(slug_name)
    # user 2026-07-20: 분사 행 제목도 컬러 (dim harness hue) — same rule as the wide row,
    # including F-75b's finished-row carve-out (only DEAD collapses to colorless).
    name_key_j = ("nm_dead" if j.liveness == "dead"
                  else _NAME_KEY_DIM.get(j.harness, "nmd_other"))
    l1 = [("  ", None), (prefix, "dim"), (gch, gkey), (" ", None),
          (_badge_cell(hn, max(9, _HW - len(prefix))), _BADGE_KEY.get(j.harness, "dim")), (shown_name, _subject_name_key(j, name_key_j))]
    l1.extend(_interaction_chip(j)[0])
    if orphan:
        l1.append(("  (orphan)", "gate_u"))
    br_segs = _branch_suffix_segs(
        "" if key in _LOOPS_KEYS else j.cwd, j.branch, optional=True,
        ahead=getattr(j, "branch_ahead", None),
        behind=getattr(j, "branch_behind", None),
        location_kind=None if key in _LOOPS_KEYS else getattr(j, "location_kind", None),
        location_repo=getattr(j, "location_repo", None))
    if not _split:
        l1.extend(br_segs)

    stage = stage_override if stage_override is not None else (j.stage or "")
    if _route_rides_the_rail(j, route_seq, in_card):
        # Preserve the MAIN model/routing anchors while reserving blank rail cells.
        lead = _RAIL_COL + 3
        eff = j.effort or parent_effort or None
        l2 = [(" " * lead, None), (_pad(fmt_min(j.elapsed_min), 4 + _HW - lead), "dim")]
        l2 += _model_cell(_job_display_model(j, parent_model), eff, _MW, dim=True)
        l2 = _pad_to_column(l2, _session_routing_column("narrow"))
        l2 += _opts_segs(j)[0]
    elif afterglow_j:
        # F-46 as corrected by F-64b: the L2 line keeps the model cell (static identity,
        # not telemetry) and swaps only the stage slot for a steady `: done ✓` token
        # (check trailing, same lead-in as `: running`). L2 already leads with the
        # elapsed cell, so no depth needs it repeated (F-64a).
        eff = j.effort or parent_effort or None
        l2 = [("    ", None), (_pad(fmt_min(j.elapsed_min), _HW), "dim")]
        l2 += _model_cell(_job_display_model(j, parent_model), eff, _MW, dim=True)
        l2.append(("    ", None))
        l2 += _stage_zone_segs([("done %s" % _LIVE_GLYPH["done"], "dim")])
    else:
        eff = j.effort or parent_effort or None
        l2 = [("    ", None), (_pad(fmt_min(j.elapsed_min), _HW), "dim")]
        l2 += _model_cell(_job_display_model(j, parent_model), eff, _MW, dim=True)
        l2.append(("    ", None))
        opt_segs, optw = _opts_segs(j)
        l2 += opt_segs
        if optw < _OPTW:
            l2.append((" " * (_OPTW - optw), None))
        if not _route_rides_the_rail(j, route_seq, in_card):
            stall = _stall_attention_stage_segs(j)
            l2 += (stall if stall is not None else _stage_zone_segs(
                _dispatch_stage_segs(j, key, stage, slug_name,
                                     # F-67 parity (77d0c2): the current
                                     # breadcrumb token pulses on UNIT activity
                                     # like the wide row and the frame — an
                                     # idle standard+ owner whose depth-2
                                     # workers are busy still reads live.
                                     working=(unit_working
                                              if unit_working is not None
                                              else j.liveness == "working"),
                                     route_seq=route_seq, compact_route=in_card)))
    if _split:
        return l1, l2, br_segs
    return l1, l2


def _pulse_segs(sessions, jobs, loading=False, observations=None):
    """`  fleet ⠙ N working   ● N idle ...` — whole-board census. Extracted (F-30, v10) so both
    the group view and the process view (§5.1) render the EXACT same row — one source, shared
    by the header helper contract §5.2 asks for, instead of two independently-drifting copies."""
    if loading:
        spin = _SPIN[int(time.time() * 10) % len(_SPIN)]
        return [("  fleet ", "head"), (spin, "g_spin"), (' sessions loading…', "dim")]
    _real = [s for s in sessions if not s.app_server and not getattr(s, "mem_worker", False)]
    n_wk = sum(1 for s in _real if s.liveness == "working")
    n_id = sum(1 for s in _real if s.liveness == "idle")
    n_un = sum(1 for s in _real if s.liveness == "unused")
    n_dt = sum(1 for s in _real if s.detached and s.liveness not in ("stale", "dead"))
    # F-46: an afterglow row is finished work kept on screen for readability — it is never
    # part of the census (working/idle/job count), exactly as a cooling group is not "hot".
    listed_jobs = [j for j in jobs
                   if not _is_plugin_agent(j) and not getattr(j, "afterglow", False)]
    if not _SHOW_ALL:
        listed_jobs = [j for j in listed_jobs if j.liveness != "dead"]
    jw = sum(1 for j in listed_jobs if j.liveness == "working")
    spin = _SPIN[int(time.time() * 10) % len(_SPIN)]
    pulse = [("  fleet ", "head"),
             (spin + " %d" % n_wk, "g_spin"), (' working  ', "dim"),
             ("● %d" % n_id, "g_work_off"), (' idle  ', "dim")]
    # F-26: only when there IS one — a healthy board stays quiet (F-12 contract).
    if n_un:
        pulse += [(_LIVE_GLYPH["unused"] + " %d" % n_un, "g_unused"), (' unused  ', "dim")]
    if n_dt:
        pulse += [(_DETACHED_GLYPH + " %d" % n_dt, "g_work_off"), (' detached  ', "dim")]
    if _jobs_unconfirmed(observations):
        pulse += [('↳ jobs.log unknown', "lvl_y")]
        if listed_jobs:
            pulse += [(" (last seen %d)" % len(listed_jobs), "dim")]
    elif listed_jobs:
        pulse += [("↳ %d" % len(listed_jobs), "dim"),
                  (" jobs (", "dim"), (spin + " %d" % jw, "g_spin"), (")", "dim")]
    return pulse


def _diagnostic_rows(diagnostics, malformed=0, term_width=None):
    """One reader-evidence summary for group/process views; never infer execution failure."""
    rows = []
    if malformed:
        rows.append([("  jobs.log · skipped %d malformed rows" % malformed, "dim")])
    labels = {
        "missing-registry": "missing reference",
        "malformed-registry": 'invalid registry',
        "malformed-run": 'invalid run record',
        "malformed-index": 'invalid index',
        "resource-row-projection": 'projection unknown',
    }
    grouped = []
    by_kind = {}
    for diagnostic in diagnostics or []:
        key = tuple(str(diagnostic.get(field) or "") for field in ("kind", "error", "reason"))
        if diagnostic.get("blocking") is False and key in by_kind:
            by_kind[key][1] += 1
        else:
            group = [diagnostic, 1]
            grouped.append(group)
            if diagnostic.get("blocking") is False:
                by_kind[key] = group
    for diagnostic, count in grouped:
        past = diagnostic.get("kind") == "missing-registry" and diagnostic.get("blocking") is False
        if past and not _SHOW_ALL:
            continue
        label = labels.get(diagnostic.get("kind"), 'observation unknown')
        prefix = '  resource · ' + label if past else '  resource unknown'
        if not past and label != 'observation unknown':
            prefix += " · " + label
        if count > 1:
            prefix += " ×%d" % count
        source = str(diagnostic.get("path") or diagnostic.get("run_id") or "")
        if source:
            # Show the project and the registry location, using only literal path parts.
            source = source.split("/.agent_reports/", 1)
            source = (source[0].rsplit("/", 1)[-1] + "/" + source[1]
                      if len(source) == 2 else source[0])
        impact = " · nonblocking" if diagnostic.get("blocking") is False else (
            " · blocking" if diagnostic.get("blocking") is True else "")
        detail = " · " + str(diagnostic.get("error") or diagnostic.get("reason") or "")
        budget = max(0, term_width - 1) if term_width else 120
        prefix = _clip_w(prefix, max(0, budget - _dw(impact)))
        room = max(0, budget - _dw(prefix + impact))
        context = "" if count > 1 else (" · " + source if source else "") + detail
        rows.append([(prefix + _clip_w(context, room) + impact, "dim")])
    return rows


_SHOW_ALL = False   # --all: reveal stale/dead/app_server sessions (folded by default per group)

# F-27 selectable-row stash. `_build_lines` returns a flat segment-line list and knows nothing
# about which SESSION/JOB produced which line — but a kill cursor must target a row, not a
# screen line. Stashed on the module (same pattern as _TOGGLE_ROWS) instead of changing the
# return signature, so render_once and every existing caller are untouched.
# Reset at the top of _build_lines, before any early return, so a stale map can never be read.
_SELECTABLE = []


def _subagent_strip(subs, depth=0, in_card=False, term_width=None):
    """One horizontal strip per OWNER ROW (session or dispatch job): `⚡<type> (<Model>·<ef>)
    <glyph> <elapsed> · …` — replaces the old one-row-per-subagent `├⚡`/`└⚡` stack
    (adopted from the discarded two-plane demo's `_agents_strip`, prd.md:290-295).
    ⚡ sits flush against the first label (the double-width glyph plus a space read
    as a hole — 사용자 2026-07-16), and the elapsed tail is set off by a double
    space and always dim, floating it apart from the identity the way the clock column
    separates from session rows. Active entries render normal weight with a BLINKING
    green ● (shared `_BLINK_ON`, same g_work/g_work_off pair as session working dots —
    사용자 2026-07-29: the flat white dot read as neither working nor done); completed
    entries fully dim (✓) — the caller only passes completed entries at all when
    `_SHOW_ALL` (F-18b dim-row convention). The model/effort parenthetical shows the
    sub-agent's ACTUAL execution budget when the collector observed one (F-3 honest
    gap: absent budget renders nothing) — model keeps its family color, effort uses
    the `_EFF_SHORT` 2-char form with the heat-ramp color keyed by the full value
    (same F-9(c) idiom as `_harness_model_cell`). A completed entry's elapsed stops
    at its observed completion time and gains a dim `· <age> ago` tail
    since it finished (사용자 2026-07-29 '언제 끝났는지'; no completion evidence →
    no tail). `depth` = the owning dispatch row's
    depth (0 for a session row): each level pushes the strip 2 more cells inward so it
    stays visibly inside its own owner (사용자 2026-07-16 "서브 세션에 서브 에이전트도")."""
    def build(show_budget, show_idle, show_elapsed):
        segs = [(_conn_indent(depth, in_card), None), (_ICON_SUBAGENT, "dim")]
        for i, sa in enumerate(subs):
            if i:
                segs.append((" · ", "dim"))
            label = sa.agent_type or "agent"
            elapsed = _subagent_elapsed_min(sa)
            tail = fmt_min(elapsed) if elapsed is not None else "—"
            segs.append((label, None if sa.active else "dim"))
            model = getattr(sa, "model", None)
            name = _clean_model(_short_model_id(model)) if model else None
            if name and show_budget:
                eff_full = getattr(sa, "effort", None) or ""
                segs.append((" (", "dim"))
                segs.append((name, _model_key(model, dim=not sa.active)))
                if eff_full:
                    segs.append(("·", "dim"))
                    segs.append((_EFF_SHORT.get(eff_full, eff_full),
                                 _eff_key(eff_full, not sa.active) or "dim"))
                segs.append((")", "dim"))
            if sa.active:
                segs.append((" ", None))
                segs.append(("●", "g_work" if _BLINK_ON else "g_work_off"))
            else:
                segs.append((" ✓", "dim"))
            if show_elapsed:
                segs.append(("  " + tail, "dim"))
            idle = _subagent_idle_min(sa)
            if idle is not None and show_idle:
                segs.append((" · " + _text_age(sa.ended_at), "dim"))
        return segs
    return [_fit_strip([lambda: build(True, True, True),
                        lambda: build(False, True, True),
                        lambda: build(False, True, False)], term_width)]


def _route_chain_node_segs(node, with_knobs, tag_by_key, is_current=False, working=False):
    """One node, `label(mode·intensity) glyph` (user 2026-09-18: "세부 (direct, std 등)이 괄호로
    묶이고 컬러는 lab에만 … code쪽에도 그걸 다 기록해주고"). Only the current node is lit —
    work hue while the session works, plain otherwise — past nodes and their details are dim,
    and a planned node is `label ○`. A node handed to another session keeps its dim
    `label◌→[tag]` form (`✓→`/`✕→` once closed)."""
    label = node.get("label") or "?"
    if not _SHOW_ALL:
        label = _user_label(label)
    handoff_to = node.get("handoff_to")
    if handoff_to:
        tag = (tag_by_key or {}).get((handoff_to.get("harness"), handoff_to.get("session_id")))
        tag_text = "[" + str(tag) + "]" if tag else ("→" + (handoff_to.get("harness") or "?"))
        state = node.get("state")
        arrow = "✓→" if state == "done" else ("✕→" if state == "failed" else "◌→")
        return [(label + arrow + tag_text, "dim")]
    state = node.get("state")
    if state == "planned":
        return [(label + " ○", "dim")]
    knobs = _route_chain_node_knobs(node, with_knobs and _SHOW_ALL)
    if is_current:
        lit = None
        if state == "open":
            lit = ("g_work" if _BLINK_ON else "g_work_off") if working else (None if _BLINK_ON else "dim")
        segs = [(label, lit)]
        if knobs:
            segs += [("(", "dim"), (knobs, "dim"), (")", "dim")]
        if state == "open":
            return segs + [(" ●", lit)]
        glyph, key = _ROUTE_CHAIN_MARK.get(state, ("?", "lvl_y"))
        return segs + [(" " + glyph, key if key != "dim" else lit)]
    text = label + ("(" + knobs + ")" if knobs else "")
    if state == "open":
        return [(text + " ●", "dim")]
    glyph, key = _ROUTE_CHAIN_MARK.get(state, ("?", "lvl_y"))
    return [(text, "dim"), (" " + glyph, key)]


def _route_chain_bodies(chain, tag_by_key=None, working=False):
    """Width-ladder bodies of a visible route chain's window, widest first: every node
    with its details, then only the current node's, then none, and last the current node
    alone with `+N✓ +M○` counts of the rest. `chain` is `route_chain.assemble()`'s return
    shape; an invisible or empty chain has no bodies."""
    if not isinstance(chain, dict) or not chain.get("visible"):
        return []
    nodes = chain.get("nodes") or []
    if not nodes:
        return []
    current = chain.get("current")
    current_idx = len(nodes) - 1
    for i, node in enumerate(nodes):
        if node is current:
            current_idx = i
            break
    start, window = _route_chain_window(nodes, current_idx)

    def shown(past_knobs, current_knobs):
        segs = []
        for offset, node in enumerate(window):
            if offset:
                segs.append((" › ", "dim"))
            is_current = start + offset == current_idx
            segs += _route_chain_node_segs(node, current_knobs if is_current else past_knobs,
                                           tag_by_key, is_current=is_current, working=working)
        if chain.get('automatic_records') and any(not node.get('automatic') for node in nodes):
            latest = chain['automatic_records'][-1]
            count = latest.get('changed_files', '?')
            count = f">={count}; total ?" if latest.get('changed_files_exact') is False else count
            segs.append((f" · fix record ({count})", "dim"))
        return segs

    def current_only(with_knobs):
        segs = _route_chain_node_segs(nodes[current_idx], with_knobs, tag_by_key,
                                      is_current=True, working=working)
        counts = {}
        for j, node in enumerate(nodes):
            if j == current_idx:
                continue
            counts[node.get("state")] = counts.get(node.get("state"), 0) + 1
        tail = " ".join(
            "+%d%s" % (counts[state], glyph)
            for state, glyph in (("done", "✓"), ("planned", "○"), ("failed", "✕"), ("unknown", "?"))
            if counts.get(state)
        )
        if tail:
            segs.append((" " + tail, "dim"))
        return segs

    return [shown(True, True), shown(False, True), shown(False, False),
            current_only(True), current_only(False)]


def _route_chain_cell(chain, max_width, tag_by_key=None, working=False):
    """A session stage cell's route-chain segments, or None when the chain is not visible:
    the first ladder body that fits `max_width`, else the narrowest one clipped."""
    bodies = _route_chain_bodies(chain, tag_by_key, working)
    if not bodies:
        return None
    for segs in bodies:
        if sum(_dw(text) for text, _key in segs) <= max_width:
            return segs
    return _clip_segs(bodies[-1], max_width)[0]


def _plugin_agent_row(job, orphan=False, term_width=None):
    """F-73 single-line openai-codex plugin agent; never a session-detail card."""
    if getattr(job, "afterglow", False):
        marker, marker_key = _LIVE_GLYPH["done"], "dim"
    elif getattr(job, "liveness", None) == "working":
        marker = "●"
        marker_key = "g_work" if _BLINK_ON else "g_work_off"
    else:
        marker, marker_key = _glyph(getattr(job, "liveness", None), dim=True)

    elapsed = fmt_min(getattr(job, "elapsed_min", None))
    phase = _plugin_phase(job)
    orphan_text = "  (orphan)" if orphan else ""
    title = _subject_title(job) or getattr(job, "slug", None) or ""
    fixed = (_dw(_SUBAGENT_IND) + _dw(_ICON_SUBAGENT) + _dw("codex task ")
             + _dw(marker) + _dw("  " + elapsed)
             + (_dw(" : " + phase) if phase else 0) + _dw(orphan_text))
    title_room = max(0, (term_width or _SUMMARY_FALLBACK_W) - fixed - 2)

    segs = [(_SUBAGENT_IND, None), (_ICON_SUBAGENT, "dim"),
            ("codex task", None if getattr(job, "liveness", None) == "working" else "dim"),
            (" ", None), (marker, marker_key), ("  " + elapsed, "dim")]
    if title and title_room >= 4:
        segs += [("  ", None), (_clip_w(title, title_room), "dim")]
    if phase:
        segs += [(" : " + phase, "dim")]
    if orphan_text:
        segs += [(orphan_text, "gate_u")]
    return [segs]


def _dispatch_summary_detail_row(job, depth=1, term_width=None, orphan=False, in_card=False):
    """Use the main-session detail grammar for every live model dispatch."""
    if _is_plugin_agent(job):
        return []
    if getattr(job, "liveness", None) in ("stale", "dead"):
        return []
    # opencode belongs here too: its attempt stream carries per-step context tokens, so an
    # opencode job has exactly the gauge evidence claude/codex rows do. Leaving it out of
    # this allowlist dropped the entire detail line — the row rendered as bare identity
    # with no ctx and no NOW, which reads as "nothing is known about this worker" rather
    # than "this worker is live". The collector-side allowlist had the same gap.
    is_model_dispatch = getattr(job, "harness", None) in ("claude", "codex", "opencode")
    if is_model_dispatch and not getattr(job, "afterglow", False):
        # F-65: a missing first-turn denominator is an unknown gauge, not a missing row.
        # Dispatch rows retain their dim visual weight and sit two cells AFTER their own
        # spinner, matching the main row's ``spinner + 2`` relationship without flattening
        # the hierarchy back onto the main-session detail column. Inside a card the box
        # already carries the hierarchy, so every worker gauge shares the owner's column.
        shown_depth = min(depth, 1) if in_card else depth
        indicator_col = _dw("  " + _dispatch_prefix(job, orphan=orphan, in_card=in_card))
        detail_indent = (4 + _HW - _CTX_LABEL_W
                         if _route_rides_the_rail(job, None, in_card) or (in_card and not orphan)
                         else indicator_col + 2)
        content_width = (_dispatch_box_width(term_width) - 2 if in_card and term_width else
                         _context_content_width(term_width))
        return _context_detail_row(
            job, depth=shown_depth, term_width=content_width, dim=True,
            indent_width=detail_indent, muted=True)
    summary = getattr(job, "summary", None)
    if not summary:
        return []
    shown_depth = min(depth, 1) if in_card else depth
    content_width = _dispatch_box_width(term_width) - 2 if in_card and term_width else term_width
    return _summary_row(str(summary), depth=shown_depth, term_width=content_width,
                        start_col=_NAME_COL,
                        summary_ts=getattr(job, "summary_ts", None))


def _resource_process_location(child):
    if child.liveness != "working":
        return ""
    snapshot, _age = _fresh_compute_hosts()
    where, _resources = _resource_location(child, snapshot)
    return "" if where == "host/GPU unknown" else where


def _resource_child_rows(job, term_width=None, depth=1, in_card=False, gpu_children=(),
                         children=None):
    """Observed resource children, never log-parsed progress or model dispatch rows."""
    rows = []
    shown_depth = min(depth, 1) if in_card else depth
    indent = _SUBAGENT_IND + "  " * max(0, shown_depth)
    width = (_dispatch_box_width(term_width) - 2 if in_card else
             _context_content_width(term_width) - (0 if _PROCESS_VIEW else 2)) if term_width else None
    for child in (getattr(job, "resource_children", ()) if children is None else children):
        if any(child is linked for linked in gpu_children):
            continue
        if not _SHOW_ALL and child.liveness != "working":
            continue
        glyph, key = _glyph(child.liveness if child.liveness != "exited" else "done")
        if child.liveness == "working":
            glyph, key = "●", "g_work" if _BLINK_ON else "g_work_off"
        node = _gpu_safe_text(_resource_name(child))
        tail = ("  " + fmt_min(child.elapsed_min) if child.liveness == "working"
                else "  %s  %s" % (child.liveness, fmt_min(child.elapsed_min)))
        # Name and elapsed survive first; placement then counters yield as needed.
        # A long name keeps a useful 24-column prefix before optional details.
        room = (max(0, width - _dw(indent + glyph + " resource " + tail)
                    - min(24, _dw(node))) if width else None)
        location = _resource_process_location(child)
        if location and (room is None or room >= _dw("  " + location)):
            tail += "  " + location
            if room is not None:
                room -= _dw("  " + location)
        progress_room = max(0, room - 2) if room is not None else None
        progress = _resource_progress_tail(child, progress_room)
        if progress:
            tail += "  " + progress
        if width:
            tail = _clip_w(tail, max(0, width - _dw(indent + glyph + " resource ") - 1))
        budget = max(1, width - _dw(indent + glyph + " resource " + tail)) if width else 44
        rows.append([(indent, None), (glyph, key), (" resource ", "dim"),
                     (_clip_w(str(node), budget), "name_dim"), (tail, "dim")])
    return rows


def _owner_gpu_resource_rows(job, session_by_identity, gpu_resources, term_width=None,
                             depth=0, in_card=False):
    """One GPU line plus existing CPU-only resource rows for this exact owner."""
    resources = _job_gpu_resources(job, session_by_identity, gpu_resources)
    return _gpu_and_resource_rows(getattr(job, "resource_children", ()), resources,
                                  term_width, depth, in_card)


def _gpu_and_resource_rows(children, resources=(), term_width=None, depth=0, in_card=False,
                           commands=False, excluded_processes=()):
    """The same observed resource rows under an owner or its project fallback."""
    combined = {(resource["host"], resource["index"]): resource for resource in resources}
    snapshot, _age = _fresh_compute_hosts()
    linked = []
    for child in children:
        matched = _resource_gpu_resources(child, snapshot, commands=commands,
                                         excluded_processes=excluded_processes)
        if matched:
            linked.append(child)
            for resource in matched:
                key = (resource["host"], resource["index"])
                if key not in combined:
                    combined[key] = resource
                    continue
                current = combined[key]
                memory = {**current.get("_process_memory", {}), **resource["_process_memory"]}
                processes = {(p["pid"], p["proc_start"]): p
                             for p in list(current.get("processes") or ())
                             + list(resource.get("processes") or ())}
                current.update(_process_memory=memory, _process_keys=list(memory),
                               processes=list(processes.values()),
                               process_count=len(memory), has_memory=any(v is not None for v in memory.values()),
                               used_memory_mib=sum(max(0, v) for v in memory.values() if v is not None))
    gpu_rows = _gpu_resource_strip([combined[key] for key in sorted(combined)],
        term_width=term_width, depth=depth, in_card=in_card, resource_children=linked)
    cpu_rows = _resource_child_rows(None, term_width=term_width, depth=depth,
                                  in_card=in_card, gpu_children=linked, children=children)
    return gpu_rows, cpu_rows


def _resource_now_text(entity, room=None):
    """Current work of an exact resource-waiting owner, shared by every harness."""
    wait = getattr(entity, "resource_wait", None)
    observed = ((getattr(entity, "state_evidence", None) or {}).get("inputs") or {}).get(
        "observed_liveness") or {}
    parked = (getattr(entity, "liveness", None) == "idle"
              and observed.get("state") == "parked-supervised")
    attached = [child for child in getattr(entity, "resource_children", ())
                if not wait or child.run_id in wait.get("run_ids", ())]
    children = [child for child in attached if child.liveness == "working"]
    summary = getattr(entity, "summary", None)
    tool = getattr(entity, "exec_child", None) or getattr(entity, "exec_tool", None)
    resource_context = (wait or parked
                        or (children and (getattr(entity, "liveness", None) == "idle"
                                          or not summary and not tool)))
    if not children and tool:
        return None
    if not children and (resource_context or not summary):
        terminal = [child for child in attached if child.liveness in ("exited", "dead")
                    and type(getattr(child, "exit_code", None)) is int]
        if terminal:
            child = max(terminal, key=lambda c: getattr(c, "ended_at", None) or 0)
            node = _gpu_safe_text(_resource_name(child))
            command = _resource_command_label(getattr(child, "command", None))
            state = "failed" if child.exit_code else "exited"
            text = "%s · %s(exit %d) · %s" % (
                subject_command(node, command), state, child.exit_code,
                _resource_log_age(child))
            return _clip_age_tail(text, room)
    if not resource_context:
        return None
    if not children:
        return "waiting"
    snapshot, _age = _fresh_compute_hosts()
    child = children[0]
    where, resources = _resource_location(child, snapshot)
    node = _gpu_safe_text(_resource_name(child))
    command = _resource_command_label(getattr(child, "command", None))
    if resources:
        process = next((p for r in resources for p in r.get("processes", ())), None)
        if process:
            label = _gpu_process_label(process["command"])
            command = label if label != process["command"] else _resource_command_label(label)
    progress = _resource_progress_tail(child)
    log = _resource_log_age(child)
    extra = "+%d" % (len(children) - 1) if len(children) > 1 else ""
    facts = " · ".join(part for part in (where, fmt_min(child.elapsed_min), progress, extra, log) if part)
    label = subject_command(node, command)
    if room is not None:
        budget = room - _dw(" · " + facts)
        if budget > 0:
            label = _clip_w(label, budget)
        else:
            # Narrow NOW still identifies the work and its observed location.
            return _clip_age_tail(node + " · " + where + " · " + log, room)
    return _clip_age_tail(label + " · " + facts, room)


def _context_content_width(term_width):
    if not term_width:
        return term_width
    inset = _INSET + _PAD_IN if _TINT_OK and not _PROCESS_VIEW else 0
    return max(1, term_width - inset - 1)


def _context_detail_row(entity, depth=0, term_width=None, dim=False,
                        indent_width=None, muted=False, now_key="now_sub"):
    """One ``<herdr|tty> <gauge> <value>   NOW`` row for every live card.

    The lead cell is the F-100b WHERE word (`herdr` / dim `tty` / blank) in a slot the
    exact width of the retired F-55 state word, so nothing to its right moved.
    The gauge track is window-proportional (F-52b).

    The context block stays under the harness field; F-42c aligns the descriptive NOW text to
    the session column shared with dispatch subtitles.  Both anchors are stable across layouts.
    """
    if getattr(entity, "liveness", None) in ("stale", "dead"):
        return []
    context = getattr(entity, "context", None)
    pct = getattr(context, "used_pct", None) if context is not None else getattr(entity, "ctx_pct", None)
    now_text = getattr(entity, "summary", None)
    previous_summary = None
    if now_text:
        previous_summary = now_text
    resource_now = _resource_now_text(entity)
    if resource_now is not None:
        now_text = resource_now
    elif not now_text and not _exec_detail_segs(entity):
        # A basic observation is real state, but missing detail is not an idle
        # verdict or an empty NOW. Never invent a command/model turn here.
        if getattr(entity, "_details_error", False):
            now_text = "unavailable (detail failed)"
        elif getattr(entity, "_details_pending", False):
            now_text = "loading"
        elif hasattr(entity, "_details_pending"):
            now_text = "no summary yet"
    main_detail = indent_width is None and depth == 0
    if indent_width is None:
        indent_width = _CONTEXT_INDENT_W + 2 * max(0, depth)
    indent = " " * max(0, int(indent_width))
    available = max(0, (term_width or _SUMMARY_FALLBACK_W) - _dw(indent))
    gauge_width = _compact_context_gauge_width(available, depth=depth)
    window = getattr(entity, "context_display_window_tokens", None)
    if pct is None and window is None:
        pct, window = _native_context_gauge(entity)
    track = _context_gauge_track(window)
    if (isinstance(pct, (int, float)) and not isinstance(pct, bool)
            and 0 <= pct <= 100):
        shown_pct = int(round(pct))
    else:
        shown_pct = None
    # F-55b drop order, kept: NOW and the exec badge yield first (they are sized from
    # whatever is left below), and the track length is a MEASUREMENT (F-52b) that must not be
    # shrunk to buy room. The lead word is the last thing to give, and it gives whole
    # (F-100b): only when word + track + value cannot fit the row at all, even with zero NOW.
    lead_width = max(_CTX_LABEL_W, 4 + _HW - _dw(indent)) if main_detail else _CTX_LABEL_W
    degrade = (lead_width + track + _CONTEXT_VALUE_W) > available
    if degrade and term_width:
        indent = " " * min(_dw(indent), max(0, term_width - track - _CONTEXT_VALUE_W))
    segs = [(indent, None)] + _context_lead_chip(
        entity, dim=dim, degrade=degrade, room=lead_width if main_detail else None)
    if main_detail and not degrade:
        # MAIN, OWNER and FRAME gauges share the fixed model-column anchor;
        # the WHERE word sits at the shared detail inset, without moving NOW.
        segs = _pad_to_column(segs, 4 + _HW)
    segs.extend(_gauge_segs(shown_pct, gauge_width, track=track))
    if shown_pct is None:
        value_text = "—"
    else:
        value_text = "%d%%" % shown_pct
    segs.append((value_text.rjust(_CONTEXT_VALUE_W), "dim"))
    from .token_budget import compaction_imminent
    if compaction_imminent(getattr(entity, "compaction_pressure", None)):
        from .config import ui_text
        segs.append((" · " + ui_text("fleet.context.compaction_imminent"), "lvl_r"))
    exec_segs = [] if resource_now is not None else _exec_detail_segs(entity)
    if now_text or exec_segs:
        prefix_width = sum(_dw(text) for text, _key in segs)
        gap = max(_CONTEXT_NOW_GAP, _NAME_COL - prefix_width)
        total_width = term_width or _SUMMARY_FALLBACK_W
        now_room = max(0, total_width - prefix_width - gap)
        tail = []
        # F-47 drop order: the `⚙` badge is the anchor (it is the whole reason the row reads
        # as busy rather than idle) and yields only when the zone cannot hold it at all; the
        # descriptive NOW sentence clips first, exactly as it already did.
        exec_w = sum(_dw(text) for text, _key in exec_segs)
        if exec_segs and exec_w <= now_room:
            tail.extend(exec_segs)
            now_room -= exec_w
        if now_text:
            sep = "  " if tail else ""
            # Reserve the summary age after the clipped prose.
            text_room = now_room - _dw(sep)
            if resource_now is not None:
                now_text = _resource_now_text(entity, max(0, text_room))
            clipped = (_aged_text(now_text, getattr(entity, "summary_ts", None), text_room)
                       if previous_summary and resource_now is None else
                       _clip_w(str(now_text), text_room)) if text_room > 0 else ""
            if clipped:
                if sep:
                    tail.append((sep, None))
                tail.append((clipped, "dim" if now_text == "no summary yet" else now_key))
        if tail:
            segs.append((" " * gap, None))
            segs.extend(tail)
    collection_now = time.time()
    base_width = sum(_dw(text) for text, _key in segs)
    shell_column = (max(base_width, _NAME_COL)
                    if not now_text and not exec_segs else base_width)
    separator = "  " if now_text or exec_segs else ""
    shell_start = shell_column + _dw(separator)
    total_width = term_width or _SUMMARY_FALLBACK_W
    shell_text = _background_shell_tail(
        entity, now=collection_now, width=max(0, total_width - shell_start))
    if shell_text:
        segs.append((" " * max(0, shell_start - base_width), None))
        segs.append((shell_text, "g_work_off"))
    if muted:
        # A dispatch detail row is supporting telemetry. Keep its full information density,
        # but collapse every colored segment to the same dim weight as the dispatch identity.
        segs = [(text, "dim" if key is not None else None) for text, key in segs]
    return [segs]


def set_show_all(v):
    global _SHOW_ALL
    _SHOW_ALL = bool(v)


_API_DISABLED = False   # F-51c: FLEET_DISABLE=usage-api / --no-usage-api, set by fleet.py main()


def set_api_disabled(v):
    global _API_DISABLED
    _API_DISABLED = bool(v)


_HEARTING = None   # resolved once by fleet.main; render ticks never inspect installer/git state
_COMPUTE_HOSTS = None  # F-83: atomic last-good from the independent GPU/SSH refresh pump
_COMPUTE_HOSTS_SET_AT = None


def set_hearting(value):
    global _HEARTING
    _HEARTING = dict(value) if isinstance(value, dict) else None


def set_compute_hosts(value):
    global _COMPUTE_HOSTS, _COMPUTE_HOSTS_SET_AT
    _COMPUTE_HOSTS = dict(value) if isinstance(value, dict) else None
    _COMPUTE_HOSTS_SET_AT = time.monotonic() if _COMPUTE_HOSTS is not None else None


# F-71b: the header's own honesty about the two RefreshPump workers (tools/fleet/refresh.py).
# render never reads pump.running/pump.last_error directly — health() is the single surface.
_REFRESH_HEALTH = {}   # {"snapshot": health-dict|None, "compute_hosts": health-dict|None}


def set_refresh_health(snapshot=None, compute_hosts=None, details=None):
    global _REFRESH_HEALTH
    _REFRESH_HEALTH = {
        "snapshot": dict(snapshot) if isinstance(snapshot, dict) else None,
        "compute_hosts": dict(compute_hosts) if isinstance(compute_hosts, dict) else None,
        "details": dict(details) if isinstance(details, dict) else None,
    }


def _refresh_health_segments(narrow=False):
    """Segments the header appends after the install-method word.

    Degrade order under `narrow` (A-4, review minor 1): (1) the error string
    shrinks from 40 to 20 chars, (2) the ` · gpu stalled` suffix is dropped
    entirely. `refreshed <age>` and the state word are never dropped at any
    width — final cell-accurate clipping is `_addline`'s job, not this
    function's.
    """
    health = _REFRESH_HEALTH.get("snapshot") if isinstance(_REFRESH_HEALTH, dict) else None
    if not health:
        return []
    segs = []
    state = health.get("state")
    if state == "stalled":
        segs.append((" · collection delayed", "lvl_y"))
    elif state == "failed":
        error = health.get("last_error") or ""
        limit = 20 if narrow else 40
        segs.append((" · error: " + error[:limit], "lvl_y"))
    detail = _REFRESH_HEALTH.get("details")
    if detail and detail.get("state") in ("failed", "stalled"):
        cause = ("collection delayed" if detail.get("state") == "stalled" else
                 str(detail.get("last_error") or "read failed")[:20 if narrow else 40])
        segs.append((" · detail unknown · " + cause, "lvl_y"))
    compute_hosts = (_REFRESH_HEALTH.get("compute_hosts")
                      if isinstance(_REFRESH_HEALTH, dict) else None)
    # Segment importance, in survival order: ① refreshed <age> ② state word
    # ③ error body ④ ` · gpu stalled`. The gpu suffix is the lowest-priority
    # segment and the first one narrow width drops entirely.
    if compute_hosts and compute_hosts.get("state") == "stalled" and not narrow:
        segs.append((" · gpu stalled", "lvl_y"))
    segs.append((" · " + _refresh_age_label(health.get("age")), "dim"))
    return segs


def _hearting_header_row(narrow=False):
    value = _HEARTING or {}
    version = str(value.get("version") or "unknown")
    method = str(value.get("install_method") or "unmanaged")
    # F-77 (user 2026-08-14 "hearting이라는 글자에 컬러 넣어줘"): the product name is the one
    # piece of identity on the board that is not a session, a harness, or a route — with the
    # wide column header gone it is also the board's first line. It gets its own hue instead
    # of the generic `head` grey; the version and install method stay quiet beside it.
    return ([("  ", None), ("hearting", "hearting_name"), (" ", None)]
            + _hearting_version_segments(version)
            + [(" · ", "dim"), (method, "version_method")]
            + _refresh_health_segments(narrow))


def _gpu_commands_folded(host, index):
    return _ROUTE_FOLD.get(_gpu_fold_key(host, index), _ROUTE_FOLD.get(_GPU_FOLD_ALL, True))


def _toggle_gpu_commands():
    gpu_rows = [entry for entry in _FOLDABLE if isinstance(entry.get("card_key"), tuple)
                and entry["card_key"][:1] == _GPU_FOLD_ALL]
    folded = (any(not entry["folded"] for entry in gpu_rows) if gpu_rows
              else not _ROUTE_FOLD.get(_GPU_FOLD_ALL, True))
    for key in list(_ROUTE_FOLD):
        if isinstance(key, tuple) and key[:1] == _GPU_FOLD_ALL:
            del _ROUTE_FOLD[key]
    _ROUTE_FOLD[_GPU_FOLD_ALL] = folded


def _register_compute_folds(base):
    _FOLDABLE.extend({**entry, "line": base + entry["line"]}
                     for entry in _compute_host_rows.fold_rows)


def _fresh_compute_hosts():
    """`(snapshot, age_s)` of the cached host sample, or `(None, None)` once stale.

    The refresh pump retains its last good host sample during a stall.
    That sample no longer proves the process is still running.
    """
    if _COMPUTE_HOSTS is None or _COMPUTE_HOSTS_SET_AT is None:
        return None, None
    age = time.monotonic() - _COMPUTE_HOSTS_SET_AT
    if age > 3 * _COMPUTE_HOST_INTERVAL:
        return None, None
    return _COMPUTE_HOSTS, max(0.0, age)


def _gpu_session_resources(snapshot=None, excluded_processes=()):
    """Exact full-session-id GPU relations, normalized once for every Fleet view."""
    if snapshot is None:
        snapshot, _age = _fresh_compute_hosts()
        if snapshot is None:
            return {}
    if not isinstance(snapshot, dict):
        return {}
    index = {}
    hosts = [row for row in (snapshot.get("hosts") or ()) if isinstance(row, dict)]
    for host in hosts:
        host_name = _gpu_safe_text(host.get("host") or "?") or "?"
        gpus = [gpu for gpu in (host.get("gpus") or ()) if isinstance(gpu, dict)]
        for gpu in gpus:
            gpu_index = gpu.get("index")
            if not isinstance(gpu_index, int) or isinstance(gpu_index, bool):
                continue
            resource_key = (host_name, gpu_index)
            for process in (gpu.get("processes") or ()):
                if not isinstance(process, dict):
                    continue
                process_key = (host.get("host") or "?", process.get("pid"), str(process.get("proc_start")))
                if process_key in excluded_processes:
                    continue
                owner = process.get("session_owner")
                if (not isinstance(owner, dict) or owner.get("kind") != "session"
                        or owner.get("harness") not in {"claude", "codex", "opencode"}
                        or not isinstance(owner.get("id"), str) or not owner.get("id")):
                    continue
                session_key = (owner["harness"], owner["id"])
                resources = index.setdefault(session_key, {})
                resource = resources.setdefault(resource_key, {
                    "host": host_name, "index": gpu_index,
                    "model": _gpu_safe_text(gpu.get("name")).replace("NVIDIA ", ""),
                    "process_count": 0, "used_memory_mib": 0,
                    "has_memory": False, "processes": [], "_process_keys": [], "_process_memory": {},
                    "telemetry_unknown": bool(gpu.get("observation_source")),
                    "access_only": True,
                })
                resource["access_only"] &= process.get("gpu_placement") == "device-access-only"
                resource["process_count"] += 1
                resource["_process_keys"].append(process_key)
                resource["_process_memory"][process_key] = (
                    process.get("used_memory_mib") if type(process.get("used_memory_mib")) is int else None)
                pid, proc_start = process.get("pid"), process.get("proc_start")
                primary = process.get("owner")
                if ((process.get("observation_source")
                     or (isinstance(primary, dict) and primary.get("kind") == "session"
                        and primary.get("source") == "persistent-claim+ancestry"
                        and primary.get("harness") == owner["harness"]
                        and primary.get("id") == owner["id"]))
                        and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
                        and isinstance(proc_start, int) and not isinstance(proc_start, bool)):
                    resource["processes"].append({
                        "pid": pid, "proc_start": proc_start,
                        "command": _gpu_safe_text(process.get("command")
                                                  or process.get("process_name") or "process"),
                    })
                used = process.get("used_memory_mib")
                if isinstance(used, int) and not isinstance(used, bool):
                    resource["used_memory_mib"] += max(0, used)
                    resource["has_memory"] = True
    return {
        session_key: [resources[key] for key in sorted(resources)]
        for session_key, resources in index.items()
    }


def _gpu_resource_strip(resources, term_width=None, depth=0, in_card=False, resource_children=()):
    """Observed run names stay visible; full argv is controlled by command folds."""
    if not resources:
        return []
    indent = _conn_indent(depth, in_card)
    width = max(20, int(term_width or 200)) - 1
    if in_card and term_width:
        width = _dispatch_box_width(term_width) - 1
    bare_gpu_width = _dw(indent + "● GPU %s:%s" % (resources[0]["host"], resources[0]["index"]))
    if len(resources) > 1:
        bare_gpu_width += _dw(" · +%d GPU" % (len(resources) - 1))
    resource_suffix = _resource_gpu_suffix(resource_children, max(0, width - bare_gpu_width))

    labels = {}
    for resource in resources:
        names = []
        seen = set()
        for process in resource.get("processes") or ():
            exact = (process["pid"], process["proc_start"])
            if exact in seen:
                continue
            seen.add(exact)
            command = process["command"]
            name = _gpu_process_label(command)
            dedup_name = name
            if name == command:
                # Unknown argv has no observed run name. Fold only the full
                # command; known names stay on the device line in either mode.
                if _gpu_commands_folded(resource["host"], resource["index"]):
                    continue
                name = _gpu_display_command(command)
                dedup_name = _resource_command_label(command)
            # A shared word or prefix is not evidence of the same observed name.
            if any(dedup_name.casefold() == _resource_name(child).casefold()
                   for child in resource_children):
                continue
            if name not in names:
                names.append(name)
        labels[id(resource)] = ", ".join(names)

    def build(shown, show_model=False, show_memory=False, show_time=False,
              show_tag=False, name_width=None, remaining=0):
        segs = [(indent, None)]
        for position, resource in enumerate(shown):
            if position:
                segs.append((" · ", "dim"))
            identity = "GPU %s:%s" % (resource["host"], resource["index"])
            pulse_key = "g_work" if _BLINK_ON else "g_work_off"
            segs += [("◇" if resource.get("access_only") else "●", pulse_key),
                     (" ", None), (identity, "name_dim")]
            label = labels[id(resource)]
            if name_width is not None:
                label = (_gpu_clip_command(label, name_width) if name_width > 0 else "")
            if label:
                segs += [(" (", "dim"), (label, "name_dim"), (")", "dim")]
            elif _gpu_commands_folded(resource["host"], resource["index"]) and resource.get("processes"):
                segs.append((" ▸ %d" % len(resource["processes"]), "dim"))
            if show_model and resource.get("model"):
                model = _gpu_display_model(resource["model"])
                segs += [(" · ", "dim"),
                         (model, _gpu_model_key(resource["model"]))]
            if show_memory and resource.get("has_memory"):
                segs += [(" · " + _gpu_gib(resource.get("used_memory_mib", 0))
                          + " GB", "dim")]
            elif show_memory and resource.get("telemetry_unknown"):
                segs += [(" · UTIL/VRAM unknown", "dim")]
                if resource.get("access_only"):
                    segs += [(" · check device access", "dim")]
            elapsed = resource.get("elapsed_s")
            if show_time and isinstance(elapsed, int) and not isinstance(elapsed, bool):
                segs += [(" · " + fmt_min(elapsed // 60), "dim")]
            owner = _gpu_owner_label(_gpu_safe_text(resource.get("owner_label")))
            if show_tag and owner:
                segs += [(" · ", "dim"), (owner, "lvl_y")]
        if remaining:
            segs += [(" · +%d GPU" % remaining, "dim")]
        if resource_suffix:
            segs.append((resource_suffix, "dim"))
        return segs

    for model, memory, elapsed, tag in ((True, True, True, True),
                                       (False, True, True, False),
                                       (False, False, False, False)):
        segs = build(resources, model, memory, elapsed, tag)
        if sum(_dw(text) for text, _key in segs) <= width:
            return [segs]
    if (term_width and term_width <= 100 and 1 < len(resources) <= 4
            and len(set(labels.values()) - {""}) > 1):
        # At narrow widths a separate device row preserves each run's identity
        # better than squeezing all names into indistinguishable fragments.
        return [line for i, resource in enumerate(resources)
                for line in _gpu_resource_strip([resource], term_width, depth, in_card,
                                                resource_children if i == 0 else ())]
    shown = list(resources)
    while len(shown) > 1 and sum(_dw(text) for text, _key in
            build(shown, name_width=0, remaining=len(resources) - len(shown))) > width:
        shown.pop()
    remaining = len(resources) - len(shown)
    bare = build(shown, name_width=0, remaining=remaining)
    named = sum(bool(labels[id(resource)]) for resource in shown)
    room = width - sum(_dw(text) for text, _key in bare)
    name_width = max(0, room // max(1, named) - 3)
    return [_clip_segs(build(shown, name_width=name_width, remaining=remaining), width)[0]]


def _gpu_work_strip(entries, term_width=None):
    """F-104 processes retain their raw JSON, but share one card GPU line."""
    grouped = {}
    for entry in entries:
        key = (entry["host"], tuple(entry["gpu_indexes"]))
        resource = grouped.setdefault(key, {
            "host": entry["host"], "index": ",".join(map(str, entry["gpu_indexes"])),
            "model": _gpu_safe_text(entry.get("gpu_name")).replace("NVIDIA ", ""),
            "processes": [], "used_memory_mib": 0, "has_memory": False,
            "elapsed_s": None, "owner_label": entry.get("owner_label") or "unregistered",
            "telemetry_unknown": entry.get("telemetry_unknown", False), "access_only": True,
        })
        resource["access_only"] &= entry.get("gpu_placement") == "device-access-only"
        resource["processes"].append({
            "pid": entry["pid"], "proc_start": entry.get("proc_start"),
            "command": entry.get("command") or entry.get("process_name") or "process",
        })
        if entry.get("used_memory_mib") is not None:
            resource["used_memory_mib"] += entry["used_memory_mib"]
            resource["has_memory"] = True
        if entry.get("elapsed_s") is not None:
            resource["elapsed_s"] = max(resource["elapsed_s"] or 0, entry["elapsed_s"])
    return _gpu_resource_strip([grouped[key] for key in sorted(grouped)], term_width)


def _gpu_work_row(entry, term_width=None):
    """F-104 card row for a live GPU process no run registry or session line shows."""
    indent = _conn_indent(0, False)
    width = max(20, int(term_width or 200)) - 1
    command = _gpu_safe_text(entry.get("command"))
    command_label = False
    if command:
        name = _gpu_process_label(command)
        if name == command:
            command_label = True
            name = _gpu_display_command(command)
    else:
        name = os.path.basename(_gpu_safe_text(entry.get("process_name"))) or "process"
    indexes = ",".join(str(i) for i in entry.get("gpu_indexes") or ())
    identity = "GPU %s:%s" % (_gpu_safe_text(entry.get("host") or "?"), indexes)
    elapsed = entry.get("elapsed_s")
    tag = _gpu_owner_label(_gpu_safe_text(entry.get("owner_label")) or "unregistered")

    def build(show_time, show_tag, label=name):
        pulse_key = "g_work" if _BLINK_ON else "g_work_off"
        segs = [(indent, None), ("●", pulse_key), (" ", None), (identity, "name_dim"),
                (" · ", "dim"), (label, "name_dim")]
        if entry.get("used_memory_mib") is not None:
            segs += [(" · " + _gpu_gib(entry["used_memory_mib"]) + " GB", "dim")]
        if show_time and isinstance(elapsed, int) and not isinstance(elapsed, bool):
            segs += [(" · " + fmt_min(elapsed // 60), "dim")]
        if show_tag and tag:
            segs += [(" · ", "dim"), (tag, "lvl_y")]
        return segs

    # Optional time/tag yield first. A command then loses its middle instead
    # of its script suffix; identifier-based labels keep their existing clip.
    for show_time, show_tag in ((True, True), (True, False), (False, False)):
        segs = build(show_time, show_tag)
        if sum(_dw(text) for text, _key in segs) <= width:
            return segs
    bare = build(False, False, "")
    room = width - sum(_dw(text) for text, _key in bare)
    label = (_gpu_clip_command(name, max(1, room)) if command_label
             else _clip_w(name, max(1, room)))
    return _clip_segs(build(False, False, label), width)[0]


def _gpu_session_line_runs(drawn_strip_keys):
    """``(host, pid, proc_start)`` of every run a GPU line drawn on this screen names."""
    index = _gpu_session_resources()
    return {(resource["host"], process["pid"], str(process["proc_start"]))
            for key in drawn_strip_keys or ()
            for resource in index.get(key, ())
            for process in resource.get("processes") or ()}


def _compute_host_rows(term_width=None, sessions=None, resources=None, display_snapshot=None,
                       drawn_strip_keys=None):
    """``drawn_strip_keys``: the GPU lines this screen actually draws (folded cards and repos
    excluded). Only a GPU whose runs those lines name stays folded here (GH#184)."""
    _compute_host_rows.fold_rows = []
    snapshot = _COMPUTE_HOSTS
    if not isinstance(snapshot, dict):
        return []
    width = max(20, int(term_width or 200))
    hosts = [row for row in (snapshot.get("hosts") or ()) if isinstance(row, dict)]
    if not snapshot.get("configured") and not hosts:
        # Seeded-but-empty or absent inventory: one guidance line so an operator
        # learns where the file is instead of wondering why the panel is gone.
        status = _gpu_safe_text(snapshot.get("status") or "missing")
        hint = _gpu_safe_text(snapshot.get("hint") or "")
        path = _gpu_safe_text(snapshot.get("path") or "")
        rows = [[("  compute resources", "section_head"), ("  " + status, "dim")],
                [("    not configured · ", "lvl_y"),
                 (_clip_w(" — ".join(part for part in (hint, path) if part),
                          max(1, width - 22)), "dim")]]
        return [_clip_segs(row, width)[0] for row in rows]
    up = sum(row.get("reachable") is True for row in hosts)
    title = [("  compute resources", "section_head"), ("  %d/%d" % (up, len(hosts)), "dim")]
    if not snapshot.get("configured"):
        title.append(("  " + _gpu_safe_text(snapshot.get("status") or "template"), "dim"))
    rows = [title]
    if snapshot.get("error"):
        detail = _gpu_safe_text(snapshot["error"])
        rows.append([("    unavailable · ", "lvl_y"),
                     (_clip_w(detail, max(1, width - 18)), "dim")])
        return [_clip_segs(row, width)[0] for row in rows]
    if not hosts:
        rows.append([("    no hosts", "dim")])
        return rows

    if not snapshot.get("configured"):
        path = _gpu_safe_text(snapshot.get("path") or "")
        guidance = "remote hosts and detached runs require inventory configuration"
        if path:
            guidance += " · " + path
        rows.append([("    ", None), (_clip_w(guidance, max(1, width - 4)), "dim")])

    max_host = max((_dw(_gpu_safe_text(row.get("host") or "?")) for row in hosts), default=1)
    host_width = min(max_host, 16 if width >= 100 else 10)
    session_runs = _gpu_session_line_runs(drawn_strip_keys)
    gpu_indexes = [gpu.get("index")
                   for host in hosts
                   for gpu in (host.get("gpus") or ())
                   if isinstance(gpu, dict)]
    gpu_index_width = max((len(str(index)) for index in gpu_indexes
                           if isinstance(index, int) and not isinstance(index, bool)), default=1)
    wide_vram_slot = max(
        (_vram_gauge_track(gpu.get("memory_total_mib"))
         for host in hosts if host.get("reachable") is True
         for gpu in (host.get("gpus") or ()) if isinstance(gpu, dict)),
        default=_RESOURCE_GAUGE_W,
    )
    for host in hosts:
        name = _clip_w(_gpu_safe_text(host.get("host") or "?"), host_width)
        if host.get("self"):
            host_row = [("  ", None), ("⌂", "base_host"), (" ", None),
                        (_pad(name, host_width), "grp"), ("  ", None)]
        else:
            host_row = [("  ", None), ("◇", "dim"), (" ", None),
                        (_pad(name, host_width), "head"), ("  ", None)]
        prefix_width = sum(_dw(text) for text, _key in host_row)
        if host.get("reachable") is not True:
            detail = _gpu_safe_text(host.get("detail") or "unreachable")
            row = host_row + [("down", "lvl_r_flat")]
            if detail:
                row += [(" · ", "dim"), (detail, "dim")]
            rows.append(_clip_segs(row, width)[0])
            continue

        cpu = host.get("cpu_utilization_pct")
        cpu_count = host.get("cpu_count")
        cpu_threads = host.get("cpu_thread_utilization_pct")
        active_threads = _cpu_active_threads(cpu, cpu_count, cpu_threads)
        active = isinstance(active_threads, int) and active_threads >= 1
        summary_key = "resource_active" if active else "dim"
        cpu_key = _resource_level_key(cpu, active)
        thread_text = _cpu_thread_text(active_threads, cpu_count)
        memory = _host_memory_pair(host.get("memory_used_mib"),
                                   host.get("memory_total_mib"),
                                   "GB" if width >= 100 else "G")
        swap = _host_memory_pair(host.get("swap_used_mib"),
                                 host.get("swap_total_mib"),
                                 "GB" if width >= 100 else "G")
        memory_key = _subdued_ratio_key(host.get("memory_used_mib"),
                                        host.get("memory_total_mib"))
        swap_key = _subdued_ratio_key(host.get("swap_used_mib"),
                                      host.get("swap_total_mib"))
        if width >= 100:
            suffix = [("  ", None), (thread_text, cpu_key),
                      ("  · ", "dim"), ("MEM ", memory_key), (memory, memory_key),
                      ("  · ", "dim"), ("SWP ", swap_key), (swap, swap_key)]
        else:
            suffix = [("  ", None), (thread_text, cpu_key),
                      ("  M ", memory_key), (memory, memory_key),
                      ("  S ", swap_key), (swap, swap_key)]
        fixed = 4 + sum(_dw(text) for text, _key in suffix)
        track = _cpu_gauge_track(cpu_count, width - prefix_width - fixed)
        if track == 0:
            suffix[0] = (" ", None)
        host_row += [("CPU ", summary_key)]
        host_row += _cpu_gauge_segs(active_threads, cpu_count, track, cpu_key)
        host_row += suffix
        rows.append(_clip_segs(host_row, width)[0])

        gpus = [gpu for gpu in (host.get("gpus") or ()) if isinstance(gpu, dict)]
        status = host.get("gpu_status")
        if isinstance(status, dict) and status.get("summary"):
            indent = " " * prefix_width
            text = _gpu_safe_text(_gpu_status_text(status))
            # Wrap diagnostics at terminal cells, keeping the cause/count visible
            # even in narrow views. Raw query errors remain in full JSON only.
            room = max(1, width - prefix_width - 1)
            while text:
                part = _clip_w(text, room, ellipsis="")
                if len(part) < len(text) and " " in part:
                    part = part.rsplit(" ", 1)[0]
                if not part:
                    break
                rows.append([(indent, None), (part, "lvl_y")])
                text = text[len(part):].lstrip()
        rows.extend(_gpu_reservation_rows(host.get("unplaced_gpu_reservations") or (),
                                         " " * prefix_width, width, unplaced=True, sessions=sessions,
                                         display_snapshot=display_snapshot))
        if not gpus:
            if isinstance(status, dict) and status.get("summary"):
                continue
            state = "GPU unavailable" if host.get("detail") else "no gpu"
            row = [(" " * prefix_width, None),
                   (state, "lvl_y" if host.get("detail") else "dim")]
            rows.append(_clip_segs(row, width)[0])
            continue

        for gpu in sorted(gpus, key=lambda value: (value.get("index") is None,
                                                    value.get("index") or 0)):
            indent = " " * prefix_width
            count = sum(isinstance(process, dict) for process in gpu.get("processes") or ())
            folded = _gpu_commands_folded(host.get("host"), gpu.get("index"))
            if (not folded and _gpu_on_session_lines(host, gpu, session_runs)
                    and _gpu_fold_key(host.get("host"), gpu.get("index")) not in _ROUTE_FOLD):
                # GH#184: the session's own GPU line is where this GPU's runs are shown;
                # here it keeps its summary row and owner. Opening this one row still works.
                folded = True
            chip = [(" %s %d" % ("▸" if folded else "▾", count), "dim")] if count else []
            chip_width = sum(_dw(text) for text, _key in chip)
            token = _gpu_token(gpu, max(12, width - _dw(indent) - chip_width), show_name=width >= 100,
                               sessions=sessions, index_width=gpu_index_width,
                               vram_slot=wide_vram_slot)
            if count or gpu.get("reservations"):
                _compute_host_rows.fold_rows.append({"line": len(rows),
                    "card_key": _gpu_fold_key(host.get("host"), gpu.get("index")), "folded": folded})
            row = [(indent, None)] + token + chip
            if folded:
                groups, warnings = _gpu_activity_summary(
                    gpu, host, sessions, display_snapshot)
                room = width - 1 - sum(_dw(text) for text, _key in row)
                row += _gpu_activity_tail(groups, warnings, room)
            rows.append(_clip_segs(row, width)[0])
            if not folded:
                rows.extend(_gpu_run_rows(gpu, indent, width, sessions, display_snapshot))
    return rows


_compute_host_rows.fold_rows = []


def _top_rows(term_width=None, narrow=False):
    return [_hearting_header_row(narrow), None]


# --- F-30 (v10, prd.md:304-310) — process view: pipeline-centric regrouping, `p` toggle ---
_PROCESS_VIEW = False        # False = group view (default, unchanged) | True = process view
_ROUTE_FOLD = {}             # {card_key: bool} — True = explicitly folded. User action ONLY;
                             # default fold state (§5.4 table) is computed fresh every build and
                             # never written here (so a card that becomes newly-failed re-expands
                             # on its own unless the user folded it by hand).
_FOLDABLE = []                # [{"line": idx, "card_key": ...}] — line-index map, `_build_process_
                              # lines` fills it fresh every call (`_SELECTABLE` precedent, F-27).
_REPO_FOLD = {}               # repo identity -> explicit user-folded bool; independent of route/GPU folds.
_REPO_FOLDABLE = []           # [{"line": idx, "repo_key": ..., "folded": bool}] for drawn repo titles.
_LAYOUT_META = {"fixed_end": 0, "body_start": 0, "body_end": 0, "legend_start": 0,
                "fixed_priorities": {}}
_PROCESS_LAYOUT_META = {}
_EXPLORER_ENABLED = False
_EXPLORER_STATE = None
_EXPLORER_FRAME = None
_EXPLORER_NAV_ROWS = ()
_EXPLORER_ALL_ROWS = ()      # every navigation row, folded or not: the s/x target map is built from these
_EXPLORER_RESULT = None      # the accepted selected-scope result; never one read for another session
_EXPLORER_MODEL = None       # the view model of the last frame: it names the session a route page belongs to
_EXPLORER_COMPOSE_MODEL = None
_EXPLORER_COMPOSE_SNAPSHOT = None
_EXPLORER_COMPOSE_DETAIL = None
_EXPLORER_COMPOSE_GENERATION = None
_EXPLORER_NATIVE_DISPLAY = None
_EXPLORER_ORDER = []         # main-session ids in first-seen order, so the navigation does not reshuffle
_EXPLORER_SIZE = None
_EXPLORER_EARLIER = False    # the output pane was scrolled past its first row: read further back once
_EXPLORER_RUNTIME = None
_EXPLORER_GENERATION = 0
_EXPLORER_READINESS = {"ready": True}
_EXPLORER_SHOWN = False      # a complete frame was drawn: later pending sources never blank the screen again
_EXPLORER_PUBLISHED_MODEL = None
_EXPLORER_REPLAY_ACTIVE = False
_EXPLORER_DETAIL_CONTINUATION = None
_EXPLORER_REMOTE_OBSERVATION = None
_EXPLORER_QUESTION_QUEUE = None
_EXPLORER_QUESTION_PUMP = None
_EXPLORER_QUESTION_GENERATION = 0
_EXPLORER_QUESTION_REFRESH_AT = 0.0


def set_explorer_mode(enabled=True):
    """Select the cell-based Fleet explorer for live and once entry points."""
    global _EXPLORER_ENABLED, _EXPLORER_DRAW_CACHE
    _EXPLORER_ENABLED = bool(enabled)
    _EXPLORER_DRAW_CACHE = None


def _explorer_view(snapshot):
    """What the explorer providers read: the snapshot, with no file/DB/git read behind any value."""
    from .explorer import nav
    extras = {}
    if getattr(snapshot, "compute_hosts", None) is None:
        hosts, age = _fresh_compute_hosts()
        if hosts is not None:
            extras["compute_hosts"] = hosts
            if getattr(snapshot, "unregistered_gpu", None) is None:
                try:
                    extras["unregistered_gpu"] = _compute_hosts.unregistered_gpu(
                        hosts, getattr(snapshot, "resources", ()) or (), frozenset(), age or 0.0)
                except Exception:
                    extras["unregistered_gpu"] = ()
    if getattr(snapshot, "session_order", None) is None:
        mains = [nav._get(s, "session_id", "sid", "id") for s in (getattr(snapshot, "sessions", ()) or ())
                 if nav.is_main(s)]
        present = {str(sid) for sid in mains if sid}
        order = [sid for sid in _EXPLORER_ORDER if sid in present]
        order += [str(sid) for sid in mains if sid and str(sid) not in set(order)]
        extras["session_order"] = tuple(order)
    display = getattr(snapshot, "display_snapshot", None)
    if display is not None and getattr(snapshot, "project_of", None) is None:
        extras["project_of"] = lambda cwd: _project_for_display(cwd, display)
    if getattr(snapshot, "hearting", None) is None and isinstance(_HEARTING, dict):
        extras["hearting"] = dict(_HEARTING)         # the install info the default board's header uses
    return _ExplorerView(snapshot, **extras) if extras else snapshot


def _explorer_runtime():
    global _EXPLORER_RUNTIME
    if _EXPLORER_RUNTIME is None:
        from .explorer.runtime import ExplorerRuntime
        _EXPLORER_RUNTIME = ExplorerRuntime()
    return _EXPLORER_RUNTIME


def _explorer_targets(nav_rows):
    """The s/x target map: the navigation's session and worker rows, through the existing entry
    builders (``_selectable_session`` / ``_select_entry`` / ``_select_entry_job``). Requests, steps,
    ledger, output, relations, duties, events and the system section never get one, and a row whose
    process cannot be read is not a target (fail closed)."""
    if _EXPLORER_REPLAY_ACTIVE:
        return []
    targets = []
    for index, row in enumerate(nav_rows):
        target = row.target_ref
        if target is None:
            continue
        try:
            if row.kind == "session" and getattr(target, "pid", None) and _selectable_session(target) and (
                    _SHOW_ALL or session_parent_visible(target)):
                entry = _select_entry(target, index)
            elif row.kind == "worker" and getattr(target, "pid", None):
                entry = _select_entry_job(target, index)
            else:
                continue
        except Exception:
            continue
        entry["nav_id"] = row.identity
        targets.append(entry)
    return targets


def _explorer_requests(snapshot, state, model):
    """What the open screen needs read, as ``{kind: (FocusKey, expanded refs, wants)}``.

    Only what is open is read: the open session's own seat, cards, visible pane and conversation; an open
    route's owner output; the seats of the list being looked at; the model configuration on MODELS; and
    the pane of every session (one cheap list).
    """
    from .explorer import live
    from .explorer.types import FocusKey
    state = _explorer_state_for(state, 0)
    kind, _, ident = str(state.scope or "-").partition(":")
    requests = {"panes": (FocusKey("*", "panes"), (), ())}
    if kind not in {"session", "route", "repo"} and state.view not in {"HISTORY", "PEERS"}:
        if state.view == "MODELS":
            requests["models"] = (FocusKey("*", "models"), (), ())
        return requests
    snapshot = _explorer_lite_view(snapshot)
    index = live.session_index(snapshot)

    def ref(tag):
        info = index.get(tag)
        return f"{info['harness']}:{info['sid']}" if info and info["sid"] else None

    def session_request(tag, wants):
        info, tag_ref = index.get(tag), ref(tag)
        if tag_ref:
            requests["session"] = (FocusKey(info["harness"], info["sid"]), (tag_ref,), tuple(wants))
            if any(w in ("seat", "cards") or w.startswith("dialogue:") for w in wants):
                # Metadata/pane latency belongs to the existing auxiliary pump.
                requests["session"] = (FocusKey(info["harness"], info["sid"]), (tag_ref,),
                                       tuple(w for w in wants if w != "pane"))
                requests["panes"] = (FocusKey("*", "panes"), (),
                                     ("aux:" + tag_ref, "metadata", *(w for w in wants if w == "pane")))

    def generation_want(tag):
        picked = state.opened_for("ui:gen:" + tag)
        return "dialogue:" + (picked if picked.isdigit() else "last")

    if kind == "session":
        session_request(ident, ("seat", "cards", "pane", generation_want(ident)))
    elif kind == "route":
        route = next((r for r in (getattr(_EXPLORER_MODEL, "routes", ()) or ())
                      if str(r.get("id")) == ident), None)
        if route is not None and route.get("session_tag"):
            gathered = live._collect_routes(snapshot).get(ident)
            owner = live._owner_job(gathered["jobs"]) if gathered else None
            attempt = getattr(owner, "attempt_id", None) or ""
            session_request(str(route["session_tag"]), (f"raw:{ident}:{attempt}",))
    elif kind == "repo":
        refs = tuple(r for tag, info in index.items() if info["repo"] == ident for r in (ref(tag),) if r)
        if refs:
            requests["seats"] = (FocusKey("*", "seats"), refs, ())
    elif state.view == "HISTORY":
        refs = tuple(r for tag in index for r in (ref(tag),) if r)
        if refs:
            # HISTORY alone also lists the seats whose session already ended.
            requests["seats"] = (FocusKey("*", "seats"), refs, ("ended",))
        opened = state.opened_for("hopen:HISTORY")
        if opened:
            session_request(opened, ("seat", "cards", generation_want(opened)))
    elif state.view == "MODELS":
        requests["models"] = (FocusKey("*", "models"), (), ())
    if (state.view == "PEERS" and kind != "route") or kind == "session":
        # Reuse the list selection worker; no global readiness or full optional pass.
        refs = tuple(r for tag in index for r in (ref(tag),) if r)
        requests["seats"] = (FocusKey("*", "peers"), refs, ("peers", state.scope))
    return requests


class _ExplorerFeed:
    """Viewer state -> the selection pumps. Each kind of read has one single-flight pump; a changed
    request selects and forces a read, every tick polls, the curses thread never waits. The newest
    accepted result of each kind stays until a newer one arrives (last good)."""

    def __init__(self, reader, interval):
        self.reader, self.interval = reader, interval
        self.pumps, self.selected, self.results, self.marks = {}, {}, {}, {}
        self.token, self.last = None, None

    @property
    def pump(self):
        return self._pump("session")

    def _pump(self, kind):
        from .explorer.focus import SelectionPump
        if kind not in self.pumps:
            self.pumps[kind] = SelectionPump(self.reader, _EXPLORER_INTERVALS[kind] or self.interval)
        return self.pumps[kind]

    def update(self, state, generation, now_mono, snapshot=None):
        from .explorer import live
        from .explorer.types import ExplorerDetail
        requests = _explorer_requests(snapshot, state, _EXPLORER_MODEL) if snapshot is not None else {}
        bindings = live.detail_bindings(snapshot) if snapshot is not None else {}
        for kind in _EXPLORER_INTERVALS:
            request = requests.get(kind)
            if request is None:
                if kind == "session":
                    self.selected.pop(kind, None)
                    self.results["session"] = None
                continue
            pump = self._pump(kind)
            key, expanded, wants = request
            refs = expanded if kind != "panes" else tuple(bindings)
            observed = tuple((ref, bindings[ref]) for ref in refs if ref in bindings)
            selection = (*request, observed)
            if self.selected.get(kind) != selection:
                self.selected[kind] = selection
                self.results.pop(kind, None)
                pump.select(key, generation, expanded, wants, observed)
                pump.request(force=True)
            else:
                pump.request_due(now=now_mono)
            result = pump.poll()
            if result is not None:
                previous = self.results.get(kind)
                if (kind == "panes" and "session" in self.selected
                        and result.source_records.get("panes")
                        and (previous is None or previous.source_records.get("panes") !=
                             result.source_records.get("panes"))):
                    # Fill physical-seat history once the independent pane lookup finishes.
                    self._pump("session").request(force=True)
                if previous is not None and previous.request == result.request:
                    from .explorer.focus import retain_focus_last_good
                    result = retain_focus_last_good(previous, result)
                self.results[kind] = result
        token = tuple(self._mark(kind) for kind in _EXPLORER_INTERVALS)
        changed = token != self.token
        self.token = token
        if changed:
            def records(kind, name, default):
                result = self.results.get(kind)
                value = (result.source_records or {}).get(name) if result is not None else None
                return default if value is None else value
            self.last = ExplorerDetail(
                focus=self.results.get("session"), seats=records("seats", "seats", {}),
                config=records("models", "models", None), panes=records("panes", "panes", {}),
                results=dict(self.results))
        return self.last, changed

    def _mark(self, kind):
        """What identifies the content of a kind's newest result: a read that found the same thing again
        is not a change (it would drop the click map for nothing)."""
        result = self.results.get(kind)
        if result is None:
            return None
        known = self.marks.get(kind)
        if known is None or known[0] is not result:
            records = result.source_records or {}
            shown = {name: (dict(value, at=None) if name == "pane_read" and isinstance(value, dict) else value)
                     for name, value in records.items()
                     if name in ("seat", "dialogue", "pane_read", "raw", "seats", "models", "panes", "aux", "peers")}
            peers = records.get("peers")
            if peers is not None:
                from .details import _PEER_FIELDS, identity
                shown["peers"] = (peers.snapshot.observations.get("optional:peers"),
                    tuple((identity(row), *(getattr(row, field, None) for field in _PEER_FIELDS))
                          for row in peers.snapshot.sessions))
            request = result.request
            identity = (request.key, request.epoch, request.wants, request.expanded_sessions, request.bindings) if request else result.key
            known = self.marks[kind] = (result, hash((identity, repr(shown), repr(result.events),
                                                      result.health.state)))
        return known[1]

    def stop(self):
        for pump in self.pumps.values():
            pump.stop()


def explorer_read_detail(snapshot, state):
    """The same reads the live feed would do for ``state``, made one after the other (``--once`` and the
    capture tool): it asks the reader directly, so the screen shows what the live view shows."""
    from .explorer.focus import FocusReader
    from .explorer.types import ExplorerDetail, FocusRequest
    reader = FocusReader(live_jobs=lambda: list(getattr(snapshot, "jobs", ()) or ()),
                         snapshot=lambda: snapshot, output_reader=_explorer_output_reader())
    results = {}
    for kind, (key, expanded, wants) in _explorer_requests(snapshot, state, _EXPLORER_MODEL).items():
        try:
            from .explorer import live
            bindings = tuple(sorted(live.detail_bindings(snapshot).items()))
            results[kind] = reader(FocusRequest(key, 1, 0, tuple(expanded), tuple(wants), bindings))
        except Exception:
            results[kind] = None

    def records(kind, name, default):
        result = results.get(kind)
        value = (result.source_records or {}).get(name) if result is not None else None
        return default if value is None else value
    return ExplorerDetail(focus=results.get("session"), seats=records("seats", "seats", {}),
                          config=records("models", "models", None), panes=records("panes", "panes", {}),
                          results=results)


def _explorer_native_display(model):
    return _EXPLORER_NATIVE_DISPLAY.apply(model) if _EXPLORER_NATIVE_DISPLAY is not None else model


def _explorer_compose(snapshot, cols, rows, state=None, focus_result=None, last_good=None, now=None,
                      providers=None, draw_frame=True):
    """Project collected facts through the immutable view model into the cell renderer."""
    from .explorer import live
    global _SELECTABLE, _EXPLORER_NAV_ROWS, _EXPLORER_STATE, _EXPLORER_ALL_ROWS
    global _EXPLORER_GENERATION, _EXPLORER_MODEL, _EXPLORER_READINESS, _EXPLORER_ORDER
    global _EXPLORER_COMPOSE_MODEL, _EXPLORER_COMPOSE_SNAPSHOT
    global _EXPLORER_COMPOSE_DETAIL, _EXPLORER_COMPOSE_GENERATION
    state = _explorer_state_for(state, cols)
    same_inputs = (_EXPLORER_COMPOSE_MODEL is not None
                   and _EXPLORER_COMPOSE_SNAPSHOT is snapshot
                   and _EXPLORER_COMPOSE_DETAIL is focus_result
                   and _EXPLORER_COMPOSE_GENERATION == _EXPLORER_GENERATION)
    if same_inputs:
        model = _EXPLORER_COMPOSE_MODEL
    else:
        view = _explorer_view(snapshot)
        model = live.build_view_model(view, accepted_detail=focus_result, now=now,
                                      previous=_EXPLORER_COMPOSE_MODEL)
    runtime = _explorer_runtime()
    model, readiness = runtime.prepare(
        snapshot, focus_result, vm=model, generation=_EXPLORER_GENERATION, now=now)
    _EXPLORER_COMPOSE_SNAPSHOT = snapshot
    _EXPLORER_COMPOSE_DETAIL = focus_result
    _EXPLORER_COMPOSE_GENERATION = _EXPLORER_GENERATION
    _EXPLORER_COMPOSE_MODEL = model
    _EXPLORER_READINESS = readiness
    _EXPLORER_MODEL = model
    frame = (runtime.draw_frame(model, state, cols, rows, beat=int(_BLINK_ON))
             if draw_frame else None)
    _EXPLORER_NAV_ROWS = _EXPLORER_ALL_ROWS = ()
    _SELECTABLE = []
    return frame


def _explorer_frame_from_model(model, state, cols, rows, *, beat=1):
    """Redraw a frozen VM at current geometry with this process's validated install identity."""
    install = _HEARTING if isinstance(_HEARTING, dict) else {}
    identity = {
        "version": install.get("version") if isinstance(install.get("version"), str)
        and install.get("version") else "unknown",
        "install_method": install.get("install_method") if isinstance(install.get("install_method"), str)
        and install.get("install_method") else "unmanaged",
    }
    if (not isinstance(model.hearting, Mapping)
            or any(model.hearting.get(key) != value for key, value in identity.items())):
        model = model.with_updates(hearting=identity)
    return _explorer_runtime().draw_frame(
        model, _explorer_state_for(state, cols), cols, rows, beat=beat)


def _explorer_keys_provider(width):
    """The key row while a confirmation or the kill selection is open; ``None`` keeps the default row."""
    if _PROMPT is not None:
        text = _snapshot_line(_prompt_segs(_PROMPT, width))
    elif _SELECT_MODE:
        text = ui_text("fleet.keys.kill_select")
    else:
        return None
    return lambda rect: (text,)


def _explorer_hit_geometry(frame, snapshot, size):
    """Publish the click map of exactly this painted cell frame."""
    global _CLICK_ROWS, _CLICK_SPANS, _PROMPT_HITS, _HIT_VALID, _HIT_SIZE, _HIT_SNAPSHOT_ID
    _CLICK_ROWS, _CLICK_SPANS = {}, {}
    for hit in frame.hits:
        if hit.y >= 0:
            _CLICK_ROWS.setdefault(hit.y, []).append(hit)
    _PROMPT_HITS = []
    _HIT_SIZE = size
    _HIT_SNAPSHOT_ID = id(snapshot) if snapshot is not None else None
    _HIT_VALID = (frame.rows, frame.cols) == tuple(size)


def _explorer_click_current(size, snapshot):
    """A click is adopted for the paint it was aimed at: the frame still on screen, same size.

    A snapshot, detail or focus result that lands after that paint does not age it. Explorer
    hits carry identities and the reducer resolves them against `_EXPLORER_FRAME`, the frame
    the person saw and clicked, so dropping the click there only loses it.
    """
    frame = _EXPLORER_FRAME
    return bool(frame is not None and _HIT_SIZE == tuple(size)
                and (getattr(frame, "rows", None), getattr(frame, "cols", None)) == tuple(size))


def _explorer_open_shell_panel(effect, snapshot, vm):
    if (_EXPLORER_REPLAY_ACTIVE or snapshot is None
            or getattr(_EXPLORER_STATE, "scope", "") != "session:" + str(effect[1])):
        return None
    selected = _explorer_requests(snapshot, _EXPLORER_STATE, vm).get("session")
    if selected is None:
        return None
    from .explorer.focus import utility
    key = selected[0]
    return utility("session_shell").open_panel(key.harness, key.sid)


def _explorer_apply_effects(effects, vm, *, snapshot=None):
    """Run typed explorer effects and publish immediate action feedback."""
    from dataclasses import replace as _replace_state
    global _EXPLORER_STATE, _EXPLORER_MODEL, _EXPLORER_PUBLISHED_MODEL
    global _EXPLORER_COMPOSE_MODEL, _EXPLORER_RESULT

    effects = tuple(effects or ())
    for effect in effects:
        if effect and effect[0] == "shell.panel":
            _explorer_open_shell_panel(effect, snapshot, vm)
    effects = tuple(effect for effect in effects if not effect or effect[0] != "shell.panel")
    question_effects = tuple(effect for effect in effects
                             if effect and isinstance(effect[0], str)
                             and effect[0].startswith("questions."))
    effects = tuple(effect for effect in effects
                    if not (effect and isinstance(effect[0], str)
                            and effect[0].startswith("questions.")))
    for effect in question_effects:
        _explorer_queue_question_effect(effect)
    model_actions = tuple(effect for effect in effects
                          if effect and effect[0] in ("models.apply", "alloc.apply"))
    if _EXPLORER_REPLAY_ACTIVE:
        effects = tuple(effect for effect in effects
                        if not effect or effect[0] not in ("models.apply", "alloc.apply"))
        result = None
        if effects:
            result = _explorer_runtime().apply_effects(effects, vm)
        if model_actions:
            result = {"model_action": True, "status": "read-only",
                      "message": "MODELS is read-only in replay", "models": None}
    else:
        result = _explorer_runtime().apply_effects(effects, vm)
    reorders = tuple(effect[:5] for effect in effects if effect and effect[0] == "reorder" and len(effect) >= 5)
    if reorders:
        from .explorer.live import reorder_view_model
        _EXPLORER_MODEL = reorder_view_model(_EXPLORER_MODEL, reorders)
        _EXPLORER_PUBLISHED_MODEL = reorder_view_model(_EXPLORER_PUBLISHED_MODEL, reorders)
        _EXPLORER_COMPOSE_MODEL = None
    if not isinstance(result, Mapping) or not result.get("model_action"):
        return result

    state = _explorer_state_for(_EXPLORER_STATE, 0)
    _EXPLORER_STATE = _replace_state(state, status=str(result.get("message") or "model set finished"))
    refreshed = result.get("models")
    if isinstance(refreshed, Mapping):
        for name in ("_EXPLORER_MODEL", "_EXPLORER_PUBLISHED_MODEL", "_EXPLORER_COMPOSE_MODEL"):
            current = globals()[name]
            if current is not None:
                try:
                    globals()[name] = _replace_state(current, models=refreshed)
                except (TypeError, ValueError):
                    pass
        if _EXPLORER_RESULT is not None:
            if isinstance(_EXPLORER_RESULT, Mapping):
                _EXPLORER_RESULT = dict(_EXPLORER_RESULT, config=refreshed)
            elif hasattr(_EXPLORER_RESULT, "config"):
                try:
                    _EXPLORER_RESULT = _replace_state(_EXPLORER_RESULT, config=refreshed)
                except (TypeError, ValueError):
                    pass
    return result


def _explorer_queue_question_effect(effect):
    """Queue one exact selected-request action on the bounded background pump."""
    global _EXPLORER_STATE, _EXPLORER_QUESTION_QUEUE
    from dataclasses import replace as _replace_state
    state = _explorer_state_for(_EXPLORER_STATE, 0)
    overlay = dict(state.question_overlay) if isinstance(state.question_overlay, Mapping) else {}
    if _EXPLORER_REPLAY_ACTIVE or os.environ.get("FLEET_DEMO"):
        overlay.update(state="unknown", pending=False,
                       status="Direct answers are disabled in demo or replay; use a live native prompt.")
        _EXPLORER_STATE = _replace_state(state, question_overlay=overlay)
        return
    if _EXPLORER_QUESTION_QUEUE is None or _EXPLORER_QUESTION_PUMP is None:
        overlay.update(state="unknown", pending=False,
                       status="Direct answer worker is unavailable; use the native prompt.")
        _EXPLORER_STATE = _replace_state(state, question_overlay=overlay)
        return
    if effect[0] == "questions.draft":
        overlay["status"] = "Saving private draft…"
        _EXPLORER_STATE = _replace_state(state, question_overlay=overlay)
    with _EXPLORER_QUESTION_QUEUE_LOCK:
        if effect[0] == "questions.draft":
            _EXPLORER_QUESTION_QUEUE = deque(
                queued for queued in _EXPLORER_QUESTION_QUEUE
                if not (queued[0] == "questions.draft" and len(queued) > 1
                        and isinstance(queued[1], Mapping)
                        and _explorer_same_question_request(queued[1], effect[1])))
        if len(_EXPLORER_QUESTION_QUEUE) >= 16:
            if effect[0] == "questions.submit":
                overlay.update(state="open", pending=False,
                               status="Action queue is busy; no response was sent. Submit again when ready.")
            elif effect[0] == "questions.read":
                overlay.update(state="unknown", pending=False,
                               status="Action queue is busy; use the native prompt.")
            else:
                overlay.update(status="Draft save is delayed; edit remains on screen.")
            _EXPLORER_STATE = _replace_state(state, question_overlay=overlay)
            return
        _EXPLORER_QUESTION_QUEUE.append(tuple(effect))
    _EXPLORER_QUESTION_PUMP.request(force=True)


def _explorer_question_worker():
    if _EXPLORER_QUESTION_QUEUE is None:
        return None
    with _EXPLORER_QUESTION_QUEUE_LOCK:
        if not _EXPLORER_QUESTION_QUEUE:
            return None
        effect = _EXPLORER_QUESTION_QUEUE.popleft()
    ref = effect[1] if len(effect) > 1 and isinstance(effect[1], Mapping) else None
    state = _explorer_state_for(_EXPLORER_STATE, 0)
    # A draft is a local save for its original envelope, even after navigation.
    # Reads and responses still require the current visible selection.
    if (_EXPLORER_REPLAY_ACTIVE or os.environ.get("FLEET_DEMO") or ref is None
            or (effect[0] != "questions.draft" and (
                state.view != "QUESTIONS"
                or state.opened_for("qsel:QUESTIONS", "") != ref.get("id")
                or not _explorer_question_ref_current(ref)))):
        return {"effect": effect, "result": {
            "state": "unknown", "reason": "selected request changed before the action ran"}}
    from .explorer.runtime import ExplorerRuntime
    return {"effect": effect, "result": ExplorerRuntime.execute_question_action(effect)}


def _explorer_question_ref_current(ref):
    model = _EXPLORER_MODEL
    rows = getattr(model, "questions", ()) if model is not None else ()
    row = next((item for item in rows if item.get("id") == ref.get("id")), None)
    if row is None:
        return False
    current = row.get("ref")
    if not isinstance(current, Mapping):
        return False
    selected_row = ref.get("_fleet_row_ref")
    selected_row = selected_row if isinstance(selected_row, Mapping) else ref
    for key in ("harness", "session_id", "request_id", "turn_id", "route_id",
                "question_id", "gate_epoch"):
        old, new = selected_row.get(key), current.get(key)
        if ((old is not None or new is not None)
                and (old != new or (key == "request_id" and type(old) is not type(new)))):
            return False
    return True


def _explorer_accept_question_result(update):
    """Adopt a private response only while its exact row and selection are current."""
    global _EXPLORER_STATE, _EXPLORER_QUESTION_REFRESH_AT
    if not isinstance(update, Mapping) or not isinstance(update.get("effect"), (tuple, list)):
        return
    effect = update["effect"]
    result = update.get("result")
    if len(effect) < 2 or not isinstance(effect[1], Mapping):
        return
    ref = dict(effect[1])
    state = _explorer_state_for(_EXPLORER_STATE, 0)
    overlay = dict(state.question_overlay) if isinstance(state.question_overlay, Mapping) else {}
    current_ref = overlay.get("ref")
    if (not isinstance(current_ref, Mapping) or current_ref.get("id") != ref.get("id")
            or state.opened_for("qsel:QUESTIONS", "") != ref.get("id")
            or not _explorer_question_ref_current(ref)):
        if isinstance(current_ref, Mapping) and dict(current_ref) == ref:
            overlay.update(state="unknown", detail=None, pending=False,
                           status="The selected request changed; reopen its current native prompt.")
            from dataclasses import replace
            _EXPLORER_STATE = replace(state, question_overlay=overlay)
        return
    operation = effect[0]
    envelope = _explorer_question_envelope(ref, result)
    if operation == "questions.read":
        if envelope is None:
            overlay.update(state="unknown", detail=None, pending=False,
                           status=_explorer_question_status(
                               result, "Exact current request unavailable; use the native prompt."))
        else:
            native_ref = envelope.get("native_ref")
            if (envelope.get("harness") == "opencode" and envelope.get("turn_id") is None
                    and isinstance(native_ref, Mapping)
                    and native_ref.get("transport") == "opencode-http-v2"
                    and envelope.get("state") == "open"
                    and not _explorer_opencode_form_ui_supported(envelope)):
                envelope = dict(envelope)
                support = envelope.get("support")
                support = dict(support) if isinstance(support, Mapping) else {}
                support.update({"supported": False,
                                "reason": "opencode-form-editor-capability-unavailable"})
                envelope["support"] = support
                envelope["actions"] = []
            from .explorer.state import question_draft_from_backend, question_ui_detail
            draft = question_draft_from_backend(envelope, envelope.get("draft"))
            if draft is None:
                draft = overlay.get("draft")
            envelope = question_ui_detail(envelope, draft)
            overlay.update(detail=envelope, draft=draft, state=envelope["state"],
                           pending=False, editing=False,
                           status=_explorer_question_status(
                               result, "Selected request is ready." if envelope["state"] == "open"
                               else "Request state: " + envelope["state"]))
    elif operation == "questions.draft":
        if envelope is not None and envelope.get("state") == "open":
            from .explorer.state import question_draft_from_backend, question_ui_detail
            draft = question_draft_from_backend(envelope, envelope.get("draft"))
            if draft is None:
                draft = overlay.get("draft")
            envelope = question_ui_detail(envelope, draft)
            overlay.update(detail=envelope, draft=draft,
                           state="open", status="Private draft saved; submit explicitly to send.")
        else:
            overlay.update(state="unknown", pending=False,
                           status=_explorer_question_status(
                               result, "Draft remains on screen; persistence was not confirmed."))
    elif operation == "questions.submit":
        if envelope is not None:
            from .explorer.state import question_draft_from_backend, question_ui_detail
            draft = question_draft_from_backend(envelope, envelope.get("draft"))
            if draft is None:
                draft = overlay.get("draft")
            envelope = question_ui_detail(envelope, draft)
            overlay.update(detail=envelope, draft=draft,
                           state=envelope["state"], pending=False, editing=False,
                           status=_explorer_question_status(
                               result, "Request state: " + envelope["state"]))
            if isinstance(result, Mapping) and result.get("action_id"):
                overlay["action_id"] = str(result["action_id"])
        else:
            overlay.update(state="unknown", pending=False, editing=False,
                           status=_explorer_question_status(
                               result, "Delivery is unknown; it will not be sent again automatically."))
    from dataclasses import replace
    _EXPLORER_STATE = replace(state, question_overlay=overlay)
    _EXPLORER_QUESTION_REFRESH_AT = (
        time.monotonic() + 1.0 if overlay.get("state") in ("submitted", "received") else 0.0)
    if (operation == "questions.submit" and envelope is not None
            and envelope.get("state") in ("submitted", "received", "applied")):
        _explorer_queue_question_effect(("questions.read", ref))


def _explorer_question_pump_tick():
    global _EXPLORER_QUESTION_GENERATION, _EXPLORER_QUESTION_REFRESH_AT
    pump = _EXPLORER_QUESTION_PUMP
    if pump is None:
        return
    update = pump.poll(_EXPLORER_QUESTION_GENERATION)
    if update is not None:
        _EXPLORER_QUESTION_GENERATION, value = update
        _explorer_accept_question_result(value)
    with _EXPLORER_QUESTION_QUEUE_LOCK:
        pending = bool(_EXPLORER_QUESTION_QUEUE)
    if pending:
        pump.request(force=True)
    else:
        state = _explorer_state_for(_EXPLORER_STATE, 0)
        overlay = state.question_overlay if isinstance(state.question_overlay, Mapping) else {}
        if state.view != "QUESTIONS" or overlay.get("state") not in ("submitted", "received"):
            _EXPLORER_QUESTION_REFRESH_AT = 0.0
            return
        now = time.monotonic()
        if not _EXPLORER_QUESTION_REFRESH_AT:
            _EXPLORER_QUESTION_REFRESH_AT = now + 1.0
        if now >= _EXPLORER_QUESTION_REFRESH_AT and not pump.running:
            from .explorer.state import _question_action_ref
            ref = _question_action_ref(overlay)
            _EXPLORER_QUESTION_REFRESH_AT = now + 1.0
            if _explorer_question_ref_current(ref):
                _explorer_queue_question_effect(("questions.read", ref))


def _explorer_stop_question_pump():
    """Drain local drafts at shutdown without executing queued native actions."""
    global _EXPLORER_QUESTION_QUEUE, _EXPLORER_QUESTION_PUMP, _EXPLORER_QUESTION_REFRESH_AT
    if _EXPLORER_QUESTION_PUMP is not None:
        _EXPLORER_QUESTION_PUMP.stop(join_timeout=1.0)
    with _EXPLORER_QUESTION_QUEUE_LOCK:
        drafts = [effect for effect in (_EXPLORER_QUESTION_QUEUE or ())
                  if effect[0] == "questions.draft"]
        _EXPLORER_QUESTION_QUEUE = _EXPLORER_QUESTION_PUMP = None
    _EXPLORER_QUESTION_REFRESH_AT = 0.0
    if _EXPLORER_REPLAY_ACTIVE or os.environ.get("FLEET_DEMO"):
        return []
    from .explorer.state import _question_action_ref, _question_draft_for_backend
    state = _explorer_state_for(_EXPLORER_STATE, 0)
    overlay = state.question_overlay if isinstance(state.question_overlay, Mapping) else {}
    if isinstance(overlay.get("draft"), Mapping) and isinstance(overlay.get("detail"), Mapping):
        drafts.append(("questions.draft", _question_action_ref(overlay),
                       _question_draft_for_backend(overlay["draft"], overlay["detail"])))
    latest = []
    for effect in drafts:
        latest = [old for old in latest
                  if not _explorer_same_question_request(old[1], effect[1])]
        latest.append(effect)
    from .explorer.runtime import ExplorerRuntime
    return [ExplorerRuntime.execute_question_action(effect) for effect in latest]


def _draw_explorer(stdscr, snapshot, frame=None):
    global _EXPLORER_FRAME, _EXPLORER_STATE, _EXPLORER_SIZE, _CURSOR_ID
    h, w = stdscr.getmaxyx()
    if h <= 0 or w <= 0:
        return
    state = _explorer_state_for(_EXPLORER_STATE, w)
    prior_state = state
    if _EXPLORER_SIZE is not None and _EXPLORER_SIZE != (h, w) and state.drag:
        from dataclasses import replace
        state = replace(state, drag=())
    _EXPLORER_SIZE = (h, w)
    if frame is None:
        frame = _explorer_compose(snapshot, w, h, state, _EXPLORER_RESULT)
    # Only the VM actually being painted can cancel a gesture or change input ordering.
    from dataclasses import replace
    from .explorer.runtime import tag_cycle
    global _EXPLORER_ORDER
    model = _EXPLORER_MODEL
    ordering = getattr(model, "ordering", {}) or {}
    drag = state.drag
    if len(drag) >= 3 and drag[0] == "tree":
        identities = ordering.get("repo_keys", ()) if drag[1] == "repo" else ordering.get("repo_by_tag", {})
        if drag[2] not in identities:
            state = replace(state, drag=())
        elif drag[1] == "session" and len(drag) > 9 and drag[9] != _explorer_gesture_binding(model, drag[2]):
            state = replace(state, drag=())
    if state != prior_state or (
            _EXPLORER_SIZE != (getattr(frame, "rows", h), getattr(frame, "cols", w))):
        if model is not None:
            frame = _explorer_frame_from_model(model, state, w, h, beat=int(_BLINK_ON))
    cycle = tag_cycle(ordering)
    if cycle:
        _EXPLORER_ORDER = list(cycle)
    scrolls = dict(state.scroll)
    for slot, layout in (("pane", frame.pane_scroll), ("tree", frame.tree_scroll)):
        if len(layout) >= 5 and int(layout[4]):
            scrolls[slot] = int(layout[4])
        else:
            scrolls.pop(slot, None)
    _EXPLORER_STATE = replace(state, scroll=tuple(sorted(scrolls.items())))
    _draw_explorer_frame(stdscr, frame, snapshot, stale=_EXPLORER_REPLAY_ACTIVE)


_EXPLORER_PAINT_CACHE = None
_EXPLORER_DRAW_CACHE = None
_EXPLORER_PAINT_ROW_MAX_BYTES = 40 * 1024


def _explorer_paint_runs(frame, height, width):
    """Compile one immutable cell frame; animation and attributes stay live."""
    from itertools import groupby
    from operator import itemgetter
    from sys import getsizeof
    from .explorer.grid import cell_width, run_safe
    global _EXPLORER_PAINT_CACHE
    cached = _EXPLORER_PAINT_CACHE
    if cached is not None and cached[0] is frame and cached[1:3] == (height, width):
        return cached[3]
    previous_rows = (cached[4] if cached is not None and len(cached) > 4
                     and cached[1:3] == (height, width) else {})
    compiled_rows = {}
    row_bytes = 128
    runs = []
    for y, row in enumerate(frame.cells[:height]):
        visible = row[:width]
        previous = previous_rows.get(y)
        if previous is not None and previous[0] == visible:
            runs.extend(previous[1])
            charge = getsizeof(previous[1]) + 128 + (getsizeof(visible) if visible is not row else 0)
            if row_bytes + charge <= _EXPLORER_PAINT_ROW_MAX_BYTES:
                compiled_rows[y] = (visible, previous[1])
                row_bytes += charge
            continue
        first_run = len(runs)
        x = 0
        for style, group in groupby(visible, key=itemgetter(1, 2)):
            glyphs = [cell[0] for cell in group]
            start, x = x, x + len(glyphs)
            text = "".join(glyphs)
            if not text:
                continue
            lead = 0
            while glyphs[lead] == "":
                lead += 1
            if (len(text) == len(glyphs) - lead and text.isascii() and text.isprintable()) or (
                    all(run_safe(char) for char in text)
                    and sum(cell_width(char) for char in text) == len(glyphs) - lead):
                runs.append((y, start + lead, text, style))
            else:
                runs.extend((y, start + offset, glyph, style)
                            for offset, glyph in enumerate(glyphs) if glyph)
        row_runs = tuple(runs[first_run:])
        charge = getsizeof(row_runs) + 128 + (getsizeof(visible) if visible is not row else 0)
        if row_bytes + charge <= _EXPLORER_PAINT_ROW_MAX_BYTES:
            compiled_rows[y] = (visible, row_runs)
            row_bytes += charge
    result = tuple(runs)
    _EXPLORER_PAINT_CACHE = (frame, height, width, result, compiled_rows, row_bytes)
    return result


def _draw_explorer_frame(stdscr, frame, snapshot, *, stale=False):
    global _EXPLORER_FRAME, _EXPLORER_DRAW_CACHE
    _EXPLORER_FRAME = frame
    h, w = stdscr.getmaxyx()
    # The composed frame keeps the approved still glyph; working marks turn here, like Fleet's.
    spin = _SPIN[int(time.time() * 10) % len(_SPIN)]
    if frame.cells:
        dim = getattr(curses, "A_DIM", 0) if stale else 0
        attrs = {}
        runs = _explorer_paint_runs(frame, h, w)
        for style in dict.fromkeys(run[3] for run in runs):
            try:
                attrs[style] = _key_attr(*style) | dim
            except Exception:
                attrs[style] = False
        previous = _EXPLORER_DRAW_CACHE
        reuse = (previous is not None and previous[0] is stdscr and previous[1] is frame
                 and previous[2:5] == (h, w, stale) and previous[5] == attrs
                 and all(attr is not False for attr in attrs.values())
                 and all(attr is not False for attr in previous[5].values()))
        if reuse:
            paint = previous[6]
        else:
            stdscr.erase()
            paint = runs
        animated = (previous[6] if reuse else tuple(run for run in runs
                    if not stale and run[3][0] in _EXPLORER_SPIN_KEYS and "⠏" in run[2]))
        for y, x, text, style in paint:
            attr = attrs[style]
            if attr is False:
                continue
            if not stale and style[0] in _EXPLORER_SPIN_KEYS and "⠏" in text:
                text = text.replace("⠏", spin)
            try:
                stdscr.addstr(y, x, text, attr)
            except Exception:
                pass
        _EXPLORER_DRAW_CACHE = (stdscr, frame, h, w, stale, attrs, animated)
    else:
        _EXPLORER_DRAW_CACHE = None
        stdscr.erase()
        for y, line in enumerate(frame.lines[:h]):
            try:
                stdscr.addstr(y, 0, line)
            except Exception:
                pass
    _explorer_hit_geometry(frame, snapshot, (h, w))
    refresh = getattr(stdscr, "refresh", None)
    if callable(refresh):
        refresh()


def set_process_view(v):
    global _PROCESS_VIEW
    _PROCESS_VIEW = bool(v)


def _route_node_text(n):
    """(text, color_key, mark) for one DAG node in a card's L2 flow (§5.3). State comes straight
    from `route.py`'s §3.3 judge — this only formats it. `●` nodes carry model/effort (prd.md:307
    — "전 노드에 달면 줄이 터진다", so only the active one does).

    `mark` is `_GATE_MARK` or "" and the caller emits it as its OWN segment in `gate_t` (the
    green-dim key the spec-gate word already uses), never folded into `text`: prd.md:308 makes
    gate-passed a dimension INDEPENDENT of the ✓●…○✕ state glyph, and most passed nodes are
    `done` (= all-dim) nodes, where a dim mark would melt into the node's own dim phrase — the
    exact merge render.py:101 warns about. `gate_passed` is True|None only, so there is no
    "not passed" mark to render: absence of evidence draws nothing."""
    st = n["state"]
    from . import route
    nid = _user_label(route.node_display_label(n))
    unit = n.get("unit")
    if unit and _SHOW_ALL:
        nid = "%s[%s]" % (nid, _compact_dispatch_name(unit, _PROFILE_MAX))
    elapsed = n.get("elapsed_min")
    mark = _GATE_MARK if n.get("gate_passed") else ""
    parents = [str(parent) for parent in (n.get("depends_on") or ())]
    deps = " ←{%s}" % ",".join(_user_label(p) for p in parents) if parents else ""
    if st == "done":
        tail = fmt_min(elapsed) if elapsed is not None else ""
        return "%s ✓%s%s" % (nid, tail, deps), "dim", mark
    if st == "attention":
        stall = n.get("stall_observation")
        if isinstance(stall, dict):
            label = "! stalled" if stall.get("state") == "stalled" else "! check"
            try:
                age = max(0, int((float(stall["observed_at"])
                                  - float(stall["last_progress_at"])) / 60))
            except (KeyError, TypeError, ValueError, OverflowError):
                age = None
            tail = (" " + fmt_min(age)) if age is not None else ""
            return "%s %s%s%s" % (nid, label, tail, deps), "lvl_y", mark
        return "%s ! check%s" % (nid, deps), "lvl_y", mark
    if st == "active":
        tail = (" " + fmt_min(elapsed)) if elapsed is not None else ""
        extra = ""
        model = _clean_model(dash(n.get("model"))) if n.get("model") else None
        if model and model != "—":
            extra = " (%s%s)" % (model, ("·" + n["effort"]) if n.get("effort") else "")
        return "%s ●%s%s%s" % (nid, tail, extra, deps), ("g_work" if _BLINK_ON else "g_work_off"), mark
    if st == "reconciling":
        tail = (" " + fmt_min(elapsed)) if elapsed is not None else ""
        return "%s …exit wait%s%s" % (nid, tail, deps), "lvl_y", mark
    if st == "recovering":
        # user 2026-08-13: a crashed attempt with a staged relaunch must not read as ✕. The
        # label names the exact registry reason route.py decided, never a guess made here.
        tail = (" " + fmt_min(elapsed)) if elapsed is not None else ""
        reason = "recovery"
        return "%s …%s%s%s" % (nid, reason, tail, deps), "lvl_y", mark
    if st == "failed":
        tail = (" " + fmt_min(elapsed)) if elapsed is not None else ""
        return "%s ✕%s%s" % (nid, tail, deps), "lvl_r", mark
    if st == "degraded":
        degradation = n.get("degradation") or {}
        hop = degradation.get("fallback_hop") or "?"
        reason = degradation.get("reason") or "degraded"
        tail = (" · " + _text_age(degradation.get("ts"))) if degradation.get("ts") else ""
        return "%s ◐ (%s·%s)%s%s" % (nid, hop, reason, deps, tail), "lvl_y", mark
    return "%s ○%s" % (nid, deps), "dim", mark


def _route_card_l2(view, max_width=None):
    """Full DAG rows: keep the established tree shape and wrap without omission.

    Singleton levels form the horizontal ``›`` flow. Parallel same-level nodes
    retain the established ``├``/``└`` branch rows. Every node includes its
    explicit parent set, so composed and asymmetric DAG joins stay unambiguous.
    """
    levels = {}
    for node in view.get("nodes") or []:
        levels.setdefault(node["level"], []).append(node)
    ordered = [levels[level] for level in sorted(levels)]
    out_lines = []
    flow_nodes = []

    def flush_flow(prefix_needed):
        if not flow_nodes:
            return
        current = []
        used = 0
        relation_before = bool(prefix_needed)

        def flush_current():
            nonlocal current, used
            if current:
                out_lines.append(current)
            current = []
            used = 0

        from . import route
        for node, text, key, mark in flow_nodes:
            separator = ("› " if relation_before else "") if not current else " › "
            node_width = _dw(separator) + _dw(text) + _dw(mark)
            if current and max_width is not None and used + node_width > max_width:
                flush_current()
                separator = "› "
                node_width = _dw(separator) + _dw(text) + _dw(mark)
            if max_width is not None and node_width > max_width:
                flush_current()
                out_lines.extend(_wrap_route_node(
                    separator, text, key, mark, max_width, continuation="  "))
            else:
                _append_segment(current, separator, "dim")
                _append_segment(current, text, key)
                _append_segment(current, mark, "gate_t")
                used += node_width
                evidence = route.revision_evidence_text(node)
                evidence_segment = "  " + evidence if evidence else None
                if (evidence_segment
                        and (max_width is None
                             or used + _dw(evidence_segment) <= max_width)):
                    _append_segment(current, evidence_segment, "dim")
                    used += _dw(evidence_segment)
            relation_before = True
        flush_current()
        flow_nodes.clear()

    need_prefix = False
    for level in ordered:
        if len(level) == 1:
            flow_nodes.append((level[0], *_route_node_text(level[0])))
            continue
        flush_flow(need_prefix)
        need_prefix = False
        for index, node in enumerate(level):
            branch = "└" if index == len(level) - 1 else "├"
            text, key, mark = _route_node_text(node)
            rows = _wrap_route_node(
                "  " + branch + " ", text, key, mark, max_width, continuation="    ")
            from . import route
            evidence = route.revision_evidence_text(node)
            evidence_segment = "  " + evidence if evidence else None
            if (rows and evidence_segment
                    and (max_width is None
                         or sum(_dw(token) for token, _kind in rows[-1])
                         + _dw(evidence_segment) <= max_width)):
                _append_segment(rows[-1], evidence_segment, "dim")
            out_lines.extend(rows)
        need_prefix = True
    flush_flow(need_prefix)
    return out_lines


def _route_card(view, session_by_identity, term_width, now, gpu_resources=None, resource_owners=(), display_owner=None):
    """One F-30 card. Returns (out_lines, meta) — meta = {"card_key", "fold_line" (index into
    out_lines of the header row), "job_rows": [(index_into_out_lines, DispatchJob), ...]}. The
    caller (`_build_process_lines`) owns translating these to ABSOLUTE line indices for
    `_FOLDABLE`/`_SELECTABLE` — this function stays a pure line-list builder (no module-global
    writes), so it is directly unit-testable."""
    nodes = view.get("nodes") or []
    done = sum(1 for n in nodes if n["state"] == "done")
    total = len(nodes)
    any_failed = any(n["state"] == "failed" for n in nodes)
    all_done = total > 0 and done == total
    elapsed_candidates = [n["elapsed_min"] for n in nodes if n.get("elapsed_min") is not None]
    route_elapsed = max(elapsed_candidates) if elapsed_candidates else None

    cap = view.get("capability") or "?"
    try:
        from .collectors import dispatch as _dispatch_mod
        cap = _dispatch_mod._strip_autopilot_prefix(cap) or cap
    except Exception:
        pass
    tag_bits = [cap]
    if view.get("capability_mode"):
        tag_bits.append(view["capability_mode"])
    if view.get("effective_intensity"):
        tag_bits.append(view["effective_intensity"])
    rid_full = view.get("route_id") or "?"
    # §5.3 L1 spec: "route_id 단축 = rt- + 앞 8자" — the full value stays available in --json
    # (route.summary()'s route_id is never shortened); only the card label abbreviates.
    rid = (rid_full if not rid_full.startswith("rt-") or len(rid_full) <= 11
          else rid_full[:11])

    card_key = view["key"]
    # §5.4 default fold table: failed → auto-expand (handled by simply never defaulting to
    # folded when any_failed); all-done → 1-line fold; otherwise (active) → expand. A prior
    # EXPLICIT user fold (_ROUTE_FOLD) always wins over this default (user intent > default).
    default_fold = all_done and not any_failed
    folded = _ROUTE_FOLD.get(card_key, default_fold)
    # ★ collapse/expand glyph only — NEVER the words "folded"/"hidden" (§5.4 B2): `_draw`'s
    # existing single-segment-row substring check would silently hijack this row into
    # `_TOGGLE_ROWS` (the `a`-toggle map) instead of `_FOLD_ROWS`.
    arrow = "▸" if folded else "▾"

    route_jobs = list(resource_owners) + [n["job"] for n in nodes if n.get("job") is not None]
    work = display_owner or next((j for j in route_jobs if _is_owner_mode_row(j)), None)
    work = work or (route_jobs[0] if route_jobs else None)
    cwd = getattr(work, "cwd", None) or view.get("cwd")
    title = (getattr(work, "title", None) or getattr(work, "parent_slug", None) or getattr(work, "slug", None)
             or view.get("slug") or "work unknown")
    # The collector owns parent identity. In particular, a rejected edge must
    # never be reconstructed from the registered SID in the renderer.
    edge_sid = getattr(work, "_parent_edge_sid", None)
    parents = [s for s in session_by_identity.values() if edge_sid
               and edge_sid == s.session_id and session_parent_visible(s)
               and not getattr(s, "is_child", False)]
    parent = parents[0] if len(parents) == 1 else None
    fleet = getattr(parent, "session_tag", None)
    identity = _user_project(cwd) + (" [%s]" % fleet if fleet else "")
    # The internal route ID remains in JSON; the fold header names the actual
    # project/work and responsible Fleet session before optional timing.
    l1 = _route_card_l1([identity], str(title), done, total, route_elapsed, any_failed, arrow, term_width)

    out = [l1]
    if folded:
        return out, {"card_key": card_key, "fold_line": 0, "job_rows": [], "folded": folded}

    max_width = max(20, term_width - 6) if term_width else None
    for l2_line in _route_card_l2(view, max_width):
        out.append([("    ", None)] + l2_line)

    job_rows = []
    # A parked owner has no live model stage job. Keep its exact resource edge
    # visible in process view too, anchored to the owner rather than the route title.
    for owner in resource_owners:
        out.append(_route_job_row(owner, max_width=term_width))
        job_rows.append((len(out) - 1, owner))
        out.extend(_dispatch_summary_detail_row(owner, term_width=term_width))
        gpu_rows, cpu_rows = _owner_gpu_resource_rows(owner, session_by_identity, gpu_resources, term_width)
        out.extend(gpu_rows + cpu_rows)
    # route._record_view already preserves sealed record order within each
    # topological level; never sort opaque node ids here.
    active_nodes = [n for n in nodes if n["state"] == "active" and n.get("job") is not None]
    for n in active_nodes:
        job = n["job"]
        out.append(_route_job_row(job, max_width=term_width))
        job_rows.append((len(out) - 1, job))
        detail = _dispatch_summary_detail_row(job, depth=1, term_width=term_width)
        if detail:
            out.extend(detail)
        subs = [sa for sa in (_subagents_for_job(session_by_identity, job) or [])
                if sa.active or _SHOW_ALL]
        if subs:
            out.extend(_subagent_strip(subs))
        gpu_rows, cpu_rows = _owner_gpu_resource_rows(job, session_by_identity, gpu_resources, term_width)
        out.extend(gpu_rows + cpu_rows)

    if _SHOW_ALL:
        # prd.md:310 — completion gates stay behind the `a` toggle, never on the base screen.
        # Each name carries `⊸` iff a canonical marker PROVES it passed (prd.md:308, v10 minor
        # #2 — the evidence source the v10 cycle had to leave as an honest gap). A gate with no
        # marker prints its bare name: no-claim, NOT a failure mark.
        gate_bits = [(n["gate"], bool(n.get("gate_passed"))) for n in nodes if n.get("gate")]
        if gate_bits:
            segs = [("      gates: ", "dim")]
            for i, (name, passed) in enumerate(gate_bits):
                if i:
                    segs.append((", ", "dim"))
                segs.append((_user_label(name), "dim"))
                if passed:
                    segs.append((_GATE_MARK, "gate_t"))
            out.append(segs)

    return out, {"card_key": card_key, "fold_line": 0, "job_rows": job_rows, "folded": folded}


def _degrade_candidates(jobs, covered_slugs=()):
    """Dispatch-depth-1 jobs on a recognizable `_PIPE_STAGES` pipeline with NO resolved route_id — the
    degrade card's population (prd.md:310 — record absence is a summary card, never a blank).
    Deliberately excludes: dispatch-depth-2 stage workers (those nest under their conductor's card, same
    as the group view); any job that already has a route_id itself (that route already has a
    real card, even if the record failed to load — route.py's `_heuristic_view` covers that
    case inside `route_views_by_id`, not here); and `covered_slugs` — the dispatch-depth-1 CONDUCTOR of a
    route whose route_id lives on one of ITS children (§3.2 — the route link is attached to the
    stage worker, not the top job) would otherwise show up a SECOND time as a bare degrade card
    right next to its own real route card."""
    pool = jobs if _SHOW_ALL else [j for j in jobs if j.liveness != "dead"]
    seen = set()
    out = []
    for j in pool:
        if _is_plugin_agent(j):
            continue
        if getattr(j, "route_id", None):
            continue
        if max(1, int(getattr(j, "depth", 1) or 1)) != 1:
            continue
        if j.key not in _PIPE_STAGES and not (j.registered_worker and j.worker_type == "support"):
            continue
        if j.slug in covered_slugs:
            continue
        if j.slug in seen:
            continue
        seen.add(j.slug)
        out.append(j)
    out.sort(key=lambda j: j.slug or "")
    return out


def _degrade_card(job, session_by_identity, term_width, gpu_resources=None):
    """§5.3's degrade card — `source: heuristic`, existing `_PIPE_STAGES` breadcrumb, no DAG
    (there is no record to derive one from). No job-row entry (the card key IS the job)."""
    cap = _user_label(job.key) or "?"
    tag_bits = [cap]
    capability_mode = _display_capability_mode(job)
    if capability_mode:
        tag_bits.append(capability_mode)
    if _mode_axis_conflict(job):
        tag_bits.append("mode!")
    tag = "·".join(tag_bits)
    card_key = job.slug or job.key or "?"
    folded = _ROUTE_FOLD.get(card_key, False)
    arrow = "▸" if folded else "▾"
    slug = job.slug or "?"
    if term_width is not None:
        fixed_w = _dw("  " + arrow + " ") + _dw("[%s] " % tag) + _dw(" — no route record")
        slug = _clip_w(slug, max(4, term_width - fixed_w))
    pending = getattr(job, "_details_pending", False)
    label = ' — route linking' if pending else ' — route unknown'
    if job.registered_worker and job.worker_type == "support":
        tag = _user_project(job.caller_cwd or job.cwd)
        label = ' — project support'
    l1 = [("  " + arrow + " ", "dim"), ("[%s] " % tag, "name_dim"),
          (slug, "dim"), (label, "dim")]
    out = [l1]
    if folded:
        return out, {"card_key": card_key, "fold_line": 0, "job_rows": [], "folded": folded}
    if pending:
        out.append(_route_job_row(job, max_width=term_width))
    breadcrumb = _stage_segs(job.key, _projection_stage_for_dispatch(job), working=(job.liveness == "working"),
                             max_width=_STAGE_ZONE_MAX)
    out.append([("    ", None)] + breadcrumb)
    detail = _dispatch_summary_detail_row(
        job, depth=max(1, int(getattr(job, "depth", 1) or 1)), term_width=term_width)
    if detail:
        out.extend(detail)
    # F-29 — the degraded job's session's own active sub-agents, the same strip the route card
    # draws (2060-2064); silent when the pid resolves to no session or no active sub-agent.
    subs = [sa for sa in (_subagents_for_job(session_by_identity, job) or [])
            if sa.active or _SHOW_ALL]
    if subs:
        out.extend(_subagent_strip(subs))
    gpu_rows, cpu_rows = _owner_gpu_resource_rows(job, session_by_identity, gpu_resources, term_width)
    out.extend(gpu_rows + cpu_rows)
    return out, {"card_key": card_key, "fold_line": 0, "job_rows": [], "folded": folded}


def _build_process_lines(sessions, jobs, route_views_by_id, malformed, memory, term_width, layout,
                         node_evidence=None, governor=_IO_UNSET, resources=None, loading=False,
                         observations=None, resource_diagnostics=None, display_snapshot=None):
    """F-30 (prd.md:304-310) — the process view: one card per ACTIVE route (pipeline-centric
    regrouping) instead of the group view's per-project regrouping. Returns the SAME flat
    segment-line contract as `_build_lines` ([[(text,key),...]|None]) — `_draw`/`render_once`/
    scroll/`_clamp_offset` are all reused unmodified (§5.2). Side effect: refreshes the
    module-level `_FOLDABLE` stash (`_SELECTABLE` precedent, F-27) and appends to the (already
    freshly-reset, by `_build_lines`) `_SELECTABLE`.

    `node_evidence` (code-test verification_round_2.md §10): the SAME terminal-row evidence
    defect 1's fix threads into route resolution — the covered-conductor exclusion below has the
    identical "a route's only surviving trace may be terminal, not live" problem defect 1 fixed
    for record lookup, and needs the identical fix for the SAME reason."""
    global _FOLDABLE, _PROCESS_LAYOUT_META
    _FOLDABLE = []
    fixed_priorities = {0: 0}
    lines = [_pulse_segs(sessions, jobs, loading=loading, observations=observations)]
    _governor = _governor_segs(governor)
    if _governor is not None:
        fixed_priorities[len(lines)] = 0
        lines.append(_governor)
    diagnostic_start = len(lines)
    lines.extend(_diagnostic_rows(resource_diagnostics, malformed, term_width))
    for i in range(diagnostic_start, len(lines)):
        fixed_priorities[i] = 0
    divider_start = len(lines)
    lines.append([(_HFILL, None)])
    compute_rows = _compute_host_rows(term_width, sessions, resources, display_snapshot)
    _register_compute_folds(len(lines))
    lines.extend(compute_rows)
    if compute_rows:
        lines.append([(_HFILL, None)])
    for i in range(divider_start, len(lines)):
        fixed_priorities[i] = 3 if i < divider_start + 1 + len(compute_rows) else 8
    lines.append(None)
    lines.append([('  PROCESS VIEW', "head"), (_RFLUSH, None), ('p group view  ', "head")])
    fixed_priorities[len(lines) - 2] = 8
    fixed_priorities[len(lines) - 1] = 4
    fixed_end = len(lines)

    session_by_identity = {(s.pid, getattr(s, "proc_start", None)): s
                           for s in sessions if s.pid is not None and s.proc_start is not None}
    gpu_resources = _gpu_session_resources()
    now = time.time()

    real_views = sorted((v for v in route_views_by_id.values() if v.get("nodes")),
                        key=lambda v: v.get("route_id") or "")
    # A dispatch-depth-1 conductor whose CHILD carries the route_id (§3.2 — the env/pipe route link is
    # attached to the stage worker, not the top job) already has a real card via that child;
    # exclude its own bare slug from the degrade pool so it never shows up a second time.
    # ★ code-test verification_round_2.md §10 — `jobs` (live only) under-covers the SAME way
    # defect 1's `resolve_records` did: once the route-carrying child goes terminal
    # (done/killed/cancelled), `_scan_jobs_log` drops its row before a live DispatchJob is ever
    # built for it, so `jobs` alone can never see that child's `parent_slug` again — the
    # conductor stops being excluded and a valid record's OWN conductor re-appears as a
    # contradicting "no route record" card 2 lines below its real one. `node_evidence`'s
    # `parent` field (dispatch.py's `_scan_route_nodes`, same pipe row already parsed) is the
    # terminal-surviving half of this same fact.
    covered_slugs = {getattr(j, "parent_slug", None) for j in jobs
                     if getattr(j, "route_id", None) in route_views_by_id
                     and getattr(j, "parent_slug", None)}
    for rid, nodes in (node_evidence or {}).items():
        if rid not in route_views_by_id:
            continue
        for node_ev in (nodes or {}).values():
            parent = (node_ev or {}).get("parent")
            if parent:
                covered_slugs.add(parent)
    degrade_jobs = _degrade_candidates(jobs, covered_slugs)
    # Basic rows already have exact identity/liveness/NOW, while route records
    # are still pending. Show those rows even for depth-2 route-bound workers;
    # no DAG, empty progress, or completed/idle claim can be inferred yet.
    pending_jobs = [j for j in jobs if getattr(j, "_details_pending", False)
                    and not getattr(getattr(j, "work_projection", None), "_route_view", None)
                    and (getattr(j, "route_id", None) or getattr(j, "owner_route_id", None))
                        not in route_views_by_id
                    and not _is_plugin_agent(j) and (j.liveness != "dead" or _SHOW_ALL)
                    and j not in degrade_jobs]
    degrade_jobs.extend(pending_jobs)

    # F-29/F-73 — plain top-level sessions owning native or plugin sub-agents. Process view is
    # route-centric and emits no plain-session row, so collect one minimal owner anchor here.
    plugin_by_parent = {}
    plugin_orphans = []
    for job in jobs:
        if not _is_plugin_agent(job):
            continue
        if getattr(job, "parent_sid", None):
            plugin_by_parent.setdefault(job.parent_sid, []).append(job)
        else:
            plugin_orphans.append(job)
    agent_sessions = []
    for s in sessions:
        if (getattr(s, "app_server", False) or getattr(s, "is_child", False)
                or getattr(s, "mem_worker", False)):
            continue
        s_subs = [sa for sa in (getattr(s, "subagents", None) or []) if sa.active or _SHOW_ALL]
        plugin_subs = plugin_by_parent.pop(getattr(s, "session_id", None), [])
        session_resources = _gpu_resources_for_session(s, gpu_resources)
        if s_subs or session_resources or plugin_subs:
            agent_sessions.append((s, s_subs, session_resources, plugin_subs))
    for remaining in plugin_by_parent.values():
        plugin_orphans.extend(remaining)

    if (not real_views and not degrade_jobs and not agent_sessions and not plugin_orphans
            and not _orphan_resource_groups(resources, (), display_snapshot)):
        # prd.md:310 — an honest "nothing is running" statement, never a blank screen.
        message = ('  sessions loading…' if loading else '  jobs.log unknown'
                   if _jobs_unconfirmed(observations) else "  no observed work")
        lines.append([(message, "dim")])
        legend_start = len(lines)
        lines.extend(_reading_legend(term_width))
        _PROCESS_LAYOUT_META = {"fixed_end": fixed_end, "body_end": legend_start,
                                "legend_start": legend_start,
                                "fixed_priorities": fixed_priorities}
        return lines

    seen_keys = set()
    _seen_glyphs = set()
    covered_pids = set()
    drawn_gpu_jobs = []
    first = True
    for view in real_views:
        if not first:
            lines.append(None)
        first = False
        base = len(lines)
        parent_slugs = {getattr(n.get("job"), "parent_slug", None) for n in view.get("nodes", ())}
        display_owners = [j for j in jobs if getattr(j, "depth", 1) == 1
                          and (j.slug in parent_slugs
                               or getattr(getattr(j, "work_projection", None), "route_id", None) == view.get("route_id"))]
        card_lines, meta = _route_card(
            view, session_by_identity, term_width, now, gpu_resources=gpu_resources,
            display_owner=display_owners[0] if len(display_owners) == 1 else None,
            resource_owners=[j for j in jobs
                             if getattr(getattr(j, "work_projection", None), "route_id", None)
                             == view.get("route_id")
                             and getattr(j, "worker_type", None) == "owner"
                             and any(r.liveness == "working" or _SHOW_ALL
                                     for r in getattr(j, "resource_children", ()))])
        lines.extend(card_lines)
        drawn_gpu_jobs.extend(job for _row, job in meta["job_rows"])
        _FOLDABLE.append({"line": base + meta["fold_line"], "card_key": meta["card_key"],
                          "folded": meta["folded"]})
        for rel_idx, job in meta["job_rows"]:
            if job.pid:
                _SELECTABLE.append(_select_entry_job(job, base + rel_idx))
        # F1 — cover every route node's session pid regardless of fold state: a folded card
        # yields no job_rows, but its sessions are still "on a route", not routeless, and must
        # not re-appear as a routeless anchor below.
        for n in (view.get("nodes", []) or []):
            nj = n.get("job") if isinstance(n, dict) else None
            if nj is not None and getattr(nj, "pid", None):
                covered_pids.add(nj.pid)
            if isinstance(n, dict) and n.get("state") == "degraded":
                _seen_glyphs.add("degraded")
            if isinstance(n, dict) and n.get("state") == "recovering":
                _seen_glyphs.add("recovering")
        seen_keys.add(meta["card_key"])

    for job in degrade_jobs:
        if not first:
            lines.append(None)
        first = False
        base = len(lines)
        card_lines, meta = _degrade_card(
            job, session_by_identity, term_width, gpu_resources=gpu_resources)
        lines.extend(card_lines)
        if not meta["folded"]:
            drawn_gpu_jobs.append(job)
        _FOLDABLE.append({"line": base + meta["fold_line"], "card_key": meta["card_key"],
                          "folded": meta["folded"]})
        if job.pid:
            covered_pids.add(job.pid)
        seen_keys.add(meta["card_key"])

    snapshot, _age = _fresh_compute_hosts()
    excluded = _drawn_gpu_processes(drawn_gpu_jobs, session_by_identity, gpu_resources, snapshot)
    for name, children in sorted(_orphan_resource_groups(
            resources, drawn_gpu_jobs, display_snapshot).items()):
        if not first:
            lines.append(None)
        first = False
        lines.append([("  ● ", "g_work"), (name + "/", "grp_hot")])
        gpu_rows, resource_rows = _gpu_and_resource_rows(
            children, term_width=term_width, commands=True, excluded_processes=excluded)
        lines.extend(gpu_rows + resource_rows)
        for child in children:
            for gpu in _resource_gpu_resources(child, snapshot):
                excluded.update(gpu["_process_keys"])
    parent_gpu_resources = (_gpu_session_resources(snapshot, excluded)
                            if excluded else gpu_resources)
    # Routeless sessions with active sub-agents — one minimal owner anchor + the same strip,
    # skipping any pid already shown under a route/degrade card above (no double draw).
    for s, s_subs, session_resources, plugin_subs in agent_sessions:
        session_resources = _gpu_resources_for_session(s, parent_gpu_resources)
        if not (s_subs or session_resources or plugin_subs):
            continue
        covered = bool(s.pid and s.pid in covered_pids)
        if covered and not plugin_subs:
            continue
        if not first:
            lines.append(None)
        first = False
        name_w = 40 if term_width is None else max(8, min(40, term_width - 6))
        fleet = " [%s]" % s.session_tag if s.session_tag else ""
        identity = _user_project(s.cwd) + fleet + " · " + _display_session_subject(s)
        anchor = [("  ● ", "dim"), (_clip_w(identity, name_w), "name_dim")]
        if getattr(s, "model", None):
            anchor.append(("  " + str(s.model), "dim"))
        lines.append(anchor)
        if s_subs:
            lines.extend(_subagent_strip(s_subs, term_width=term_width))
        if session_resources and not covered:
            lines.extend(_gpu_resource_strip(session_resources, term_width=term_width))
        for plugin_job in plugin_subs:
            lines.extend(_plugin_agent_row(plugin_job, term_width=term_width))

    for plugin_job in plugin_orphans:
        if not first:
            lines.append(None)
        first = False
        lines.extend(_plugin_agent_row(plugin_job, orphan=True, term_width=term_width))

    # StateTracker.sweep() precedent — a card key not seen this tick must not leak its fold
    # flag into a future, unrelated card that happens to reuse the same route_id/slug.
    for k in [k for k in _ROUTE_FOLD if k not in seen_keys
              and not (isinstance(k, tuple) and k[:1] == _GPU_FOLD_ALL)]:
        del _ROUTE_FOLD[k]

    legend_start = len(lines)
    lines.extend(_reading_legend(term_width))
    _PROCESS_LAYOUT_META = {"fixed_end": fixed_end, "body_end": legend_start,
                            "legend_start": legend_start,
                            "fixed_priorities": fixed_priorities}
    return lines


def _current_attempt_jobs(jobs):
    """Hide superseded exact route attempts unless full history is requested."""
    if _SHOW_ALL:
        return jobs
    latest_attempt = {}
    for index, job in enumerate(jobs):
        key = (getattr(job, "route_id", None), getattr(job, "route_node", None))
        if not all(key) or not getattr(job, "attempt_id", None):
            continue
        rank = current_attempt_rank(job) + (index,)
        if key not in latest_attempt or rank > latest_attempt[key][0]:
            latest_attempt[key] = (rank, job)
    return [
        job for job in jobs
        if not (getattr(job, "route_id", None) and getattr(job, "route_node", None)
                and getattr(job, "attempt_id", None)
                and latest_attempt.get((job.route_id, job.route_node), (None, job))[1] is not job)
    ]


def _shown_group_sessions(group_sessions):
    return (group_sessions if _SHOW_ALL else
            [s for s in group_sessions if session_parent_visible(s)])


def _classify_group_jobs(name, group_jobs, shown):
    """Where each emitted dispatch job of a group is drawn (pure, no drawing).

    The group loop draws exactly this classification, and `_drawn_group_jobs` derives the
    F-104 dedup keys from the same result, so the two can never disagree.
    """
    shown_sids = set(s.session_id for s in shown if s.session_id)
    shown_cwds = {}
    ambiguous_cwds = set()
    for s in shown:
        if not s.cwd or not s.session_id:
            continue
        key_cwd = os.path.realpath(s.cwd)
        if key_cwd in shown_cwds:
            ambiguous_cwds.add(key_cwd)
        else:
            shown_cwds[key_cwd] = s.session_id
    for key_cwd in ambiguous_cwds:
        shown_cwds.pop(key_cwd, None)

    # pre-assemble session -> child-jobs and job -> sub-job maps before emitting rows.
    # Dispatch-depth-1 jobs can nest under an on-screen parent session; dispatch-depth-2 jobs nest under
    # their capability-owner job via parent_slug. This keeps main-session context light
    # while fleet still shows cross-harness orchestration shape.
    children = {}      # session_id -> [jobs] (nested under an on-screen parent)
    job_children = {}  # parent dispatch slug -> [dispatch-depth-2 jobs]
    orphans = []       # project-level fallback (parent dead/off-screen/no-env)
    loops_jobs = []    # no-parent-is-normal (cron loops) — no orphan marker
    grace_jobs = []    # F-80 L2: confirmed edge, parent filtered off-screen this tick
                        # (stale/app_server/absent) within grace — no orphan marker
    recovered_session_ids = set()
    visible_parent_slugs = {
        j.slug for j in group_jobs
        if j.slug and max(1, int(getattr(j, "depth", 1) or 1)) < 2
    }
    # A `drill:<case>` group is a self-contained regression fixture: the harness roots every
    # case under a synthetic sentinel session id (`drill-<harness>-parent-session`) so it never
    # depends on the launching session. Such a root's parent is UNRESOLVABLE by design, not lost,
    # so a would-be-orphan here is the case's intended root — surface it as a standalone tree row
    # (loops_jobs: "no-parent-is-normal", no `(orphan)` marker) instead of the orphan divider
    # (user 2026-07-24: drill runs "orphan으로 잡히고 메인 세션은 연결도 안되고" — noise, since the
    # fixture decouples on purpose). Non-drill groups keep the exact prior classification.
    is_drill_case = str(name).startswith("drill:")
    for j in group_jobs:
        if getattr(j, "parent_slug", None) and getattr(j, "depth", 1) >= 2:
            if j.parent_slug in visible_parent_slugs:
                job_children.setdefault(j.parent_slug, []).append(j)
            elif is_drill_case:
                loops_jobs.append(j)
            elif not _SHOW_ALL and j.liveness in _DETACHED_FINISHED_LIVENESS:
                # 사용자 2026-08-19 ("done된것도 원래 박스 안에서 떴다니까"): while its owner
                # was on screen this row lived inside that owner's card; the owner then
                # aged off the board and only the finished child leaked back out, hours
                # stale, floating between cards. Recovery exists so a LIVE depth-2 row
                # cannot vanish behind a broken parent edge — it is not a reason to
                # resurrect finished work whose owner already left. When the owner goes,
                # its finished children go with it; `a`/--all still reveals them.
                pass
            elif getattr(j, "_parent_edge_promoted_orphan", False):
                # impl-review gap (round 1 G1 close-out): the ledger's verdict is
                # authoritative over shown_sids — a promoted orphan stays an orphan even
                # when --all puts its dead parent on screen this tick.
                orphans.append(j)
            elif getattr(j, "_parent_edge_sid", None):
                edge_sid = j._parent_edge_sid
                if edge_sid in shown_sids:
                    children.setdefault(edge_sid, []).append(j)
                    recovered_session_ids.add(edge_sid)
                else:
                    # F-80 L2/C6: collector confirmed this exact edge and it is inside
                    # grace — consume that verdict rather than re-deriving orphan status
                    # from shown_sids. The parent row itself is filtered off-screen this
                    # tick.
                    grace_jobs.append(j)
            elif (not hasattr(j, "_parent_edge_sid") and getattr(j, "is_child", False) and j.parent_sid
                  and j.parent_sid in shown_sids):
                # No ledger verdict reached this row (collector never ran
                # resolve_parent_edges against it) — fall back to the pre-ledger check.
                children.setdefault(j.parent_sid, []).append(j)
                recovered_session_ids.add(j.parent_sid)
            else:
                # A malformed/stale parent edge must not make a live dispatch-depth-2 row
                # disappear from Fleet. Surface it as a project-level orphan.
                orphans.append(j)
        elif getattr(j, "_parent_edge_promoted_orphan", False):
            orphans.append(j)
        elif getattr(j, "_parent_edge_sid", None):
            edge_sid = j._parent_edge_sid
            if edge_sid in shown_sids:
                children.setdefault(edge_sid, []).append(j)
            else:
                # F-80 L2/C6: same grace consumption as above, for the depth-1 nesting path.
                # Takes priority over the parent_managed_dir/parent_cwd fallbacks below so a
                # confirmed edge is never re-resolved through a less precise cwd match.
                grace_jobs.append(j)
        elif not hasattr(j, "_parent_edge_sid") and j.is_child and j.parent_sid and j.parent_sid in shown_sids:
            # No ledger verdict reached this row — fall back to the pre-ledger check.
            children.setdefault(j.parent_sid, []).append(j)
        elif getattr(j, "source", None) == "plugin-queue":
            # F-50c: a plugin-queue job nests ONLY on an exact `sessionId` ==
            # `Session.session_id` match (the branch above). Its `parent_cwd` is the
            # plugin workspace root — nesting on it would attach the job to whatever
            # session happens to sit in that directory, which is the misattribution the
            # separate-surface rule exists to prevent. Unmatched → orphan.
            orphans.append(j)
        elif not hasattr(j, "_parent_edge_sid") and j.is_child and getattr(j, "parent_cwd", None):
            sid = shown_cwds.get(os.path.realpath(j.parent_cwd))
            if sid:
                children.setdefault(sid, []).append(j)
            elif j.key in _LOOPS_KEYS or is_drill_case:
                loops_jobs.append(j)
            else:
                orphans.append(j)
        elif _known_parentless_support_job(j):
            loops_jobs.append(j)
        elif j.key in _LOOPS_KEYS or is_drill_case:
            loops_jobs.append(j)
        else:
            orphans.append(j)
    if not _SHOW_ALL:
        # F-83: the route-open dead owner exemption above exists so a LIVE session keeps
        # its card; an owner whose session is gone is ordinary dead history and stays
        # behind `a`/--all like every other dead row.
        orphans = [j for j in orphans
                   if not (j.liveness == "dead" and getattr(j, "_dead_terminal_owner", False))]

    return {"children": children, "job_children": job_children, "orphans": orphans,
            "loops_jobs": loops_jobs, "grace_jobs": grace_jobs,
            "recovered_session_ids": recovered_session_ids}

def _drawn_group_jobs(classified, shown):
    """Dispatch jobs the group loop draws a row for, from `_classify_group_jobs`.

    Walks the same buckets in the same way the loop does: a job under a shown
    non-automation session, an orphan/loop/grace row, and the unfolded depth-2 children of
    a drawn owner. A dropped row (dead owner without a session, a plugin row, a folded
    child, a session dedup'd away) is not in the result.
    """
    drawn = []

    def _walk(job):
        drawn.append(job)
        if max(1, int(getattr(job, "depth", 1) or 1)) != 1:
            return
        for sub in classified["job_children"].get(job.slug, []):
            if (max(1, int(getattr(sub, "depth", 1) or 1)) >= 2 and not _SHOW_ALL
                    and _fold_completed_child(sub)):
                continue  # absorbed into the conductor breadcrumb
            _walk(sub)

    claimed = set()
    for s in shown:
        if getattr(s, "mem_worker", False):
            continue  # a mem row draws no child jobs
        if s.session_id in claimed:
            continue  # ambiguous duplicate id: only the first row draws the dispatch tree
        if s.session_id:
            claimed.add(s.session_id)
        session_kids = classified["children"].get(s.session_id, [])
        folded_frame_ids = (set() if _SHOW_ALL
                            else _finished_frame_fold_ids(session_kids))
        for kid in session_kids:
            if not _is_plugin_agent(kid):  # a plugin row is drawn without a GPU strip
                if id(kid) in folded_frame_ids:
                    continue  # F-105: finished route group, folded with its siblings
                _walk(kid)
    for job in classified["orphans"]:
        if not _is_plugin_agent(job):
            _walk(job)
    for job in classified["loops_jobs"] + classified["grace_jobs"]:
        _walk(job)
    return drawn


def _emitted_group_jobs(g, show_sessions, show_jobs):
    group_jobs = _visible_group_jobs(g["jobs"], show_sessions, show_jobs)
    if not _SHOW_ALL:
        group_jobs = [j for j in group_jobs
                      if j.liveness != "dead" or getattr(j, "_dead_terminal_owner", False)]
    return group_jobs


def _group_drawn_strip_keys(name, g, show_sessions, show_jobs, gpu_resources, session_by_identity):
    """F-104 dedup keys of one group, from the classification its emitter draws."""
    shown = _shown_group_sessions(g["sessions"]) if show_sessions else []
    classified = _classify_group_jobs(
        name, _emitted_group_jobs(g, show_sessions, show_jobs), shown)
    return _gpu_strip_keys(shown, gpu_resources, _drawn_group_jobs(classified, shown),
                           session_by_identity)


def _group_emission(g, show_sessions, show_jobs, gpu_resources=None,
                    session_by_identity=None, name=""):
    """Single source of truth for the session rows that the group loop emits.

    The earlier visible_order calculation is a conservative card-anchor approximation;
    it is intentionally separate from this exact emit predicate.
    """
    group_sessions = g["sessions"] if show_sessions else []
    group_jobs = _emitted_group_jobs(g, show_sessions, show_jobs)
    gpu = g.get("gpu") or []
    resources = (g.get("resources") or []) if show_jobs else []
    empty = not group_sessions and not group_jobs and not gpu and not resources
    if empty:
        return {"group_sessions": group_sessions, "group_jobs": group_jobs,
                "empty": True, "fold": False, "shown": [], "hidden": 0,
                "gpu": [], "resources": [], "gpu_strip_keys": set(), "classified": None}
    live_sessions = [s for s in group_sessions
                     if s.liveness not in ("stale", "dead") and not s.app_server]
    must_show_jobs = any(not getattr(j, "afterglow", False) for j in group_jobs)
    shown = _shown_group_sessions(group_sessions)
    classified = _classify_group_jobs(name, group_jobs, shown)
    strip_keys = _gpu_strip_keys(shown, gpu_resources or {},
                                 _drawn_group_jobs(classified, shown), session_by_identity)
    # GPU work is live work: a card holding a GPU row or a drawn session/job GPU
    # strip is never folded away.
    fold = ((not _SHOW_ALL) and (not live_sessions) and (not must_show_jobs)
            and (not gpu) and (not resources) and (not strip_keys))
    return {"group_sessions": group_sessions, "group_jobs": group_jobs,
            "empty": False, "fold": fold, "shown": shown,
            "hidden": len(group_sessions) - len(shown),
            "gpu": gpu, "resources": resources,
            "gpu_strip_keys": strip_keys, "classified": classified}


def _build_lines(sessions, jobs, section, narrow, malformed, layout="wide", memory=None,
                 term_width=None, live_order=None, resources=None, usage_snapshots=None,
                 governor=_IO_UNSET, loading=False, node_evidence=None,
                 route_entities=None, observations=None, resource_diagnostics=None,
                 pending_delivery=None, display_snapshot=None):
    """One cell budget for plain output and curses, preserving row/map indexes."""
    if term_width and layout == "wide" and term_width < _TWO_LINE_CUTOFF:
        layout = "stack" if term_width < _NARROW_CUTOFF else "narrow"
    # Each producer owns its framing budget (boxes already reserve their edge).
    # Context/NOW reserves its actual paint inset before clipping the sentence.
    lines = _build_unbounded_lines(sessions, jobs, section, narrow, malformed, layout, memory,
                                    term_width, live_order, resources, usage_snapshots, governor, loading,
                                    node_evidence=node_evidence, route_entities=route_entities,
                                    observations=observations, resource_diagnostics=resource_diagnostics,
                                    pending_delivery=pending_delivery,
                                    display_snapshot=display_snapshot)
    if term_width is None:
        return lines
    width = max(1, term_width - 1)  # curses reserves its rightmost cell
    bounded = []
    for line in lines:
        if line is None:
            bounded.append(None)
            continue
        tinted = bool(_TINT_OK and line and _is_fill(line[0][0]) and line[0][0][1] in _TINT_CHARS)
        row_width = width - (_INSET + _PAD_IN if tinted else 0)
        # Framing is out-of-band. Keep a final observation age inside the actual
        # paint budget before the common plain/curses clipping pass.
        if tinted:
            line = line[:1] + _reserve_age_tail(line[1:], row_width)
        else:
            line = _reserve_age_tail(line, row_width)
        out, used = [], 0
        for text, key in line:
            if _is_fill(text):
                out.append((text, key))
                continue
            piece = _clip_w(text, max(0, row_width - used), ellipsis="")
            if piece:
                out.append((piece, key))
                used += _dw(piece)
        bounded.append(out)
    return bounded


def _build_unbounded_lines(sessions, jobs, section, narrow, malformed, layout="wide", memory=None,
                 term_width=None, live_order=None, resources=None, usage_snapshots=None,
                 governor=_IO_UNSET, loading=False, node_evidence=None,
                 route_entities=None, observations=None, resource_diagnostics=None,
                 pending_delivery=None, display_snapshot=None):
    """Return a flat list of segment-lines for the whole screen (None = blank line).

    Side effect: refreshes the module-level `_SELECTABLE` stash (F-27) — see its definition.

    Same contract consumed by BOTH `render_once` (plain, full output) and `_draw` (viewport
    slices this same list) — `_OFFSET` must never be read here (see module docstring).

    `memory` = F-19 collectors.memory.collect() result (or None — memory rows simply omitted;
    tests default to None so every pre-F-19 call site keeps working unchanged).
    """
    global _SELECTABLE, _FOLDABLE, _REPO_FOLDABLE, _LAYOUT_META
    _SELECTABLE = []     # reset before any early return — a stale target map must never survive
    _FOLDABLE = []
    _REPO_FOLDABLE = []
    _LAYOUT_META = {"fixed_end": 0, "body_start": 0, "body_end": 0, "legend_start": 0,
                    "fixed_priorities": {}}
    _node_evidence = node_evidence or {}
    _projection_entities = list(sessions) + list(jobs) + list(route_entities or [])
    # v16: route authority is attached by collectors/projection.py before this surface.
    # Rendering never reopens a route file or calls dispatch-only stage discovery.
    _route_views_by_id = {}
    for _entity in _projection_entities:
        _projection = getattr(_entity, "work_projection", None)
        _backing = getattr(_projection, "_route_view", None) if _projection else None
        if not _projection or not _projection.route_id or not _backing:
            continue
        _record = _backing.get("record") or {}
        _nodes = []
        for _node in _backing.get("nodes") or ():
            if hasattr(_node, "to_dict"):
                _node = _node.to_dict()
            _node = dict(_node)
            _node.setdefault("job", _entity if getattr(_entity, "route_node", None) == _node.get("id") else None)
            _nodes.append(_node)
        _route_views_by_id.setdefault(_projection.route_id, {
            "route_id": _projection.route_id, "route_hash": _projection.route_hash,
            "source": _projection.source, "capability": _record.get("capability"),
            "capability_mode": _record.get("capability_mode"),
            "execution_topology": _record.get("execution_topology"),
            "unit_catalog_digest": _record.get("unit_catalog_digest"),
            "composed": bool(_record.get("composed")),
            "effective_intensity": _record.get("effective_intensity"),
            "progress": (_backing.get("view") or {}).get("progress",
                _projection.progress.to_dict() if _projection.progress else None),
            "nodes": _nodes, "key": _projection.route_id,
        })
    display_jobs = _current_attempt_jobs(jobs)
    if _PROCESS_VIEW:
        # F-30 (§5.2) — the ONE branch point, right after the _SELECTABLE reset and the route
        # resolution both views need. `_build_process_lines` honors the exact same return
        # contract ([[(text,key),...]|None]) so _draw/render_once/scroll/_clamp_offset are all
        # reused unmodified below this branch. `_node_evidence` is threaded through too (code-test
        # verification_round_2.md §10) — the degrade-pool's covered-conductor exclusion needs it,
        # the same way route resolution itself needed it for defect 1.
        process_lines = _build_process_lines(
            sessions, display_jobs, _route_views_by_id, malformed, memory,
            term_width, layout, node_evidence=_node_evidence, governor=governor,
            resources=resources, loading=loading, observations=observations,
            resource_diagnostics=resource_diagnostics,
            display_snapshot=display_snapshot)
        top_rows = (_top_rows(term_width, narrow) + _observation_lines(observations, term_width)
                    + _delivery_pending_rows(pending_delivery))
        for entry in _FOLDABLE + _SELECTABLE:
            entry["line"] += len(top_rows)
        process_meta = _PROCESS_LAYOUT_META or {}
        fixed_end = len(top_rows) + process_meta.get("fixed_end", len(process_lines))
        legend_start = len(top_rows) + process_meta.get("legend_start", len(process_lines))
        body_end = len(top_rows) + process_meta.get("body_end", len(process_lines))
        priorities = {i: 4 for i in range(len(top_rows))}
        priorities.update({i: 0 for i in range(len(_top_rows(term_width, narrow)),
                                                min(fixed_end, len(top_rows)))})
        for index, priority in process_meta.get("fixed_priorities", {}).items():
            priorities[len(top_rows) + int(index)] = priority
        _LAYOUT_META = {"fixed_end": fixed_end, "body_start": fixed_end,
                        "body_end": body_end, "legend_start": legend_start,
                        "fixed_priorities": priorities}
        return top_rows + process_lines
    # F-18b: mem-worker (distiller/curator/F-17 refresher) census — computed on the ORIGINAL
    # session list, before is_child/mem filtering, so folded/mem-only groups still surface a
    # total in the legend even when no group header badge fires.
    wide_name_width = _wide_name_width(term_width) if layout == "wide" else None
    wide_route_zone = (_route_zone_width(term_width, wide_name_width)
                       if layout == "wide" else None)
    # F-66 width ledger: the complete box consumes the former two-cell slack at
    # the right edge. Spend that loss in the dispatch name zone; keep F-65's
    # breadcrumb tail-fold budget based on the original terminal ledger.
    card_name_width = (max(3, wide_name_width - 2)
                       if wide_name_width is not None else None)
    n_mem_total = sum(1 for s in sessions if getattr(s, "mem_worker", False))
    mem_by_group = {_SYSTEM_GROUP: n_mem_total} if n_mem_total else {}
    # F-19 repo rows: session-id -> display title, resolved on the ORIGINAL (unfiltered) list
    # so a mem event's source session still resolves even after mem-worker/child filtering.
    sid_titles = {s.session_id: (s.title or s.slug) for s in sessions
                  if s.session_id and (s.title or s.slug)}
    # F-29 — a dispatched child session's own sub-agents survive the is_child filter below:
    # they re-attach as a strip under the dispatch row representing the child. Exact
    # attempt-owned evidence lives on the job; legacy persistent sessions fall back to
    # the pid/start join (사용자 2026-07-16 "서브 세션에 서브 에이전트도").
    child_subs_by_pid = {s.pid: s.subagents for s in sessions
                         if getattr(s, "is_child", False) and getattr(s, "subagents", None)}
    session_by_identity = {(s.pid, getattr(s, "proc_start", None)): s
                           for s in sessions if s.pid is not None and s.proc_start is not None}
    gpu_resources = _gpu_session_resources()
    # headless dispatch children are shown as dispatch rows under their parent — never as
    # top-level sessions (the same headless process would otherwise double-show as session+job).
    # Automation workers inherit a parent's cwd, so never attribute them to a project card.
    # They remain visible by default in one dedicated system card; --all does not duplicate
    # or relocate them.
    sessions = [s for s in sessions if not s.is_child]
    groups = {}
    session_groups = {}
    for s in sessions:
        gk = (_SYSTEM_GROUP if getattr(s, "mem_worker", False)
              else _group_key_session(s, display_snapshot))
        groups.setdefault(gk, {"sessions": [], "jobs": []})["sessions"].append(s)
        if s.session_id:
            session_groups[s.session_id] = gk
    managed_session_groups = {
        managed_dir: _group_key_session(session, display_snapshot)
        for managed_dir, session in _unique_managed_parents(sessions).items()
    }
    job_groups = {}
    # A route node has one current attempt. Keep older exact attempts in history evidence,
    # but suppress their rows by default so retries do not appear as
    # concurrent Fleet sessions. ``a`` restores the complete attempt history.
    top_jobs = [j for j in display_jobs if not (getattr(j, "parent_slug", None) and getattr(j, "depth", 1) >= 2)]
    depth_jobs = [j for j in display_jobs if getattr(j, "parent_slug", None) and getattr(j, "depth", 1) >= 2]
    for j in top_jobs + depth_jobs:
        gk = _group_key_job(
            j,
            session_groups=session_groups,
            job_groups=job_groups,
            managed_session_groups=managed_session_groups,
            display_snapshot=display_snapshot,
        )
        groups.setdefault(gk, {"sessions": [], "jobs": []})["jobs"].append(j)
        if j.slug:
            job_groups[j.slug] = gk

    show_sessions = section in ("fleet", "both")
    show_jobs = section in ("dispatch", "both")

    # F-104: live GPU processes neither a registered run nor a drawn session/job GPU
    # strip shows land on their cwd's project card.
    gpu_work = {}
    parent_gpu_resources = gpu_resources
    if show_jobs:
        strip_keys = set()
        resource_processes = set()
        drawn_jobs = []
        snapshot, age_s = _fresh_compute_hosts()
        for gname, g in groups.items():
            strip_keys |= _group_drawn_strip_keys(
                gname, g, show_sessions, show_jobs, gpu_resources, session_by_identity)
            shown = _shown_group_sessions(g["sessions"]) if show_sessions else []
            classified = _classify_group_jobs(gname, _emitted_group_jobs(g, show_sessions, show_jobs), shown)
            drawn_jobs.extend(_drawn_group_jobs(classified, shown))
            resource_processes.update(_drawn_gpu_processes(
                _drawn_group_jobs(classified, shown), session_by_identity, gpu_resources, snapshot))
        for name, children in _orphan_resource_groups(
                resources, drawn_jobs, display_snapshot).items():
            group = groups.setdefault(name, {"sessions": [], "jobs": []})
            group["resources"] = children
            group["resource_excluded_processes"] = set(resource_processes)
            for child in children:
                for gpu in _resource_gpu_resources(child, snapshot):
                    resource_processes.update(gpu["_process_keys"])
        for entry in _compute_hosts.unregistered_gpu(
                snapshot, resources or (), strip_keys, age_s or 0.0):
            if (entry["host"], entry["pid"], str(entry.get("proc_start"))) in resource_processes:
                continue
            gpu_work.setdefault(entry["project"], []).append(entry)
        if resource_processes:
            parent_gpu_resources = _gpu_session_resources(snapshot, resource_processes)
    for gk, entries in gpu_work.items():
        groups.setdefault(gk, {"sessions": [], "jobs": []})["gpu"] = entries

    order = sorted(groups.keys(), key=lambda name: (
        0 if name == _SYSTEM_GROUP else 1, _group_sort_key(name, groups[name])))

    # Anchor only project cards that will actually be visible.  This mirrors the
    # existing section/dead-job/fold decisions below, so hidden cards are pruned.
    if live_order is not None:
        visible_order = []
        for name in order:
            g = groups[name]
            group_sessions = g["sessions"] if show_sessions else []
            group_jobs = _visible_group_jobs(g["jobs"], show_sessions, show_jobs)
            if not _SHOW_ALL:
                group_jobs = [j for j in group_jobs if j.liveness != "dead"]
            group_gpu = g.get("gpu") or []
            group_resources = g.get("resources") or []
            if not group_sessions and not group_jobs and not group_gpu and not group_resources:
                continue
            live_sessions = [s for s in group_sessions
                             if s.liveness not in ("stale", "dead") and not s.app_server]
            if ((not _SHOW_ALL) and not live_sessions and not group_jobs and not group_gpu
                    and not group_resources
                    and not _group_drawn_strip_keys(
                        name, g, show_sessions, show_jobs, gpu_resources, session_by_identity)):
                continue
            visible_order.append(name)
        # Reconcile only card anchors, but retain folded/empty groups in the
        # render input so the unchanged loop below can still aggregate the
        # inactive folded summary.  Non-card groups stay in snapshot order and
        # never become live anchors.
        visible_names = set(visible_order)
        non_card_order = [name for name in order if name not in visible_names]
        visible_tiers = {name: _group_activity_rank(groups[name]) for name in visible_order}
        order = live_order.reconcile_groups(visible_order, visible_tiers) + non_card_order

    emission_by_group = {
        name: _group_emission(groups[name], show_sessions, show_jobs, gpu_resources,
                              session_by_identity, name)
        for name in order}
    tag_by_key = _PeerTagMap()
    for emission in emission_by_group.values():
        for s in emission["shown"]:
            sid = getattr(s, "session_id", None)
            if not sid:
                continue
            harness = str(getattr(s, "harness", "") or "").lower()
            # A resumed session is one row wearing one badge, but the ledger may still
            # address it by the id it had before the resume — so every alias points at
            # the same badge and the relation is labelled instead of going anonymous.
            for identity in [sid] + list(getattr(s, "session_aliases", None) or []):
                identity_key = (harness, identity)
                tag = getattr(s, "session_tag", None)
                if tag or identity_key not in tag_by_key:
                    tag_by_key[identity_key] = tag
                tag_by_key.canonical_by_key[identity_key] = (harness, sid)
    # The product identity is a distinct, quiet title block. Keep one breathing row
    # before account usage begins instead of letting the two metadata zones touch.
    top_rows = _top_rows(term_width, narrow)
    observation_rows = _observation_lines(observations, term_width)
    lines = top_rows + observation_rows
    fixed_priorities = {i: 4 for i in range(len(top_rows))}
    fixed_priorities.update({i: 0 for i in range(len(top_rows), len(lines))})
    _seen_glyphs = set()
    # F-98b's "both endpoints on screen" rule is retired (user 2026-09-09): an off-screen
    # peer now gets the same line, labelled by name or `<harness>:<sid8>` instead of a tag.
    # F-12(c) legend glyph-appearance tracking — LOCAL to this call (never module/global state,
    # _OFFSET invariant R3): which of the conditional legend glyphs actually got emitted this
    # build. working/idle/dispatch/`~` stay unconditional (always relevant vocabulary); the
    # rest (detached/stale/dead/child-jobs/worktrees) only show up in the legend when at least
    # one row used them.
    usage_start = len(lines)
    lines.extend(_usage_header_rows(sessions, layout=layout, api_disabled=_API_DISABLED,
                                    usage_snapshots=usage_snapshots, term_width=term_width))
    for i in range(usage_start, len(lines)):
        fixed_priorities[i] = 1
    # fleet pulse — htop's "Tasks: N, M running" analogue: whole-board census + live spend Σ
    # Show the row by default; counts skip app-server companions. Extracted into _pulse_segs
    # (F-30, v10) so the process view (§5.1) shares this EXACT row instead of a second copy.
    pulse_start = len(lines)
    lines.append(_pulse_segs(sessions, display_jobs, loading=loading, observations=observations))
    _governor = _governor_segs(governor)       # F-28c — snapshot-owned in the live loop
    if _governor is not None:                  # counts (I8); None = source absent or quiet.
        lines.append(_governor)
    for i in range(pulse_start, len(lines)):
        fixed_priorities[i] = 0
    if _SHOW_ALL:
        _repo_ids = set()
        for name in order:
            if emission_by_group[name]["empty"] or emission_by_group[name]["fold"]:
                continue
            _events = (memory or {}).get("by_repo", {}).get(name, [])
            _shown_keys = {_memory_event_key(e) for e, _count in
                           _memory_event_groups(_events)[:_MEM_REPO_ROW_LIMIT]}
            _repo_ids.update(e["id"] for e in _events if e.get("id")
                             and _memory_event_key(e) in _shown_keys)
        _mem_events = _mem_event_rows(memory, term_width=term_width, excluded_ids=_repo_ids)
        if _mem_events:
            memory_start = len(lines)
            lines.extend(_mem_events)
            for i in range(memory_start, len(lines)):
                fixed_priorities[i] = 5
            _seen_glyphs.add("mem")

    # usage/intel zone = PLAIN bg + a full-width dim rule below it (user 2026-07-03: intel
    # Keep tint directory-only so the intelligence zone is not confused with active cards.
    compute_start = len(lines)
    lines.append([(_HFILL, None)])
    drawn_strip_keys = set()
    for name, emission in emission_by_group.items():
        if (emission["empty"] or emission["fold"]
                or (name not in ("loops", _SYSTEM_GROUP) and _REPO_FOLD.get(name, False))):
            continue
        drawn_strip_keys |= emission["gpu_strip_keys"]
    compute_rows = _compute_host_rows(term_width, sessions, resources, display_snapshot,
                                      drawn_strip_keys=drawn_strip_keys)
    _register_compute_folds(len(lines))
    lines.extend(compute_rows)
    if compute_rows:
        lines.append([(_HFILL, None)])
    for i in range(compute_start, len(lines)):
        fixed_priorities[i] = 3 if i < compute_start + 1 + len(compute_rows) else 8
    fixed_end = len(lines)

    # header bar REPLACES the `──` zone divider — htop separates meters from the process list
    # with its bar, not a rule. Leave one blank line above it so
    # the top intel zone and the bar don't touch. Narrow mode's 2-line cards have no single
    # column mapping → the bar degrades to a zone label + current-mode hint.
    # Column header uses plain dim labels; tinted panels carry the visual grouping.
    body_start = fixed_end
    lines.append(None)
    _sh = " " * (_INSET + _PAD_IN) if _TINT_OK else ""   # shift matches panel content columns
    if layout != "wide":
        # narrow/stack keeps its band: that line is not a column header — it names the
        # layout and tells the reader how to change it.
        lines.append([(_sh + "  SESSIONS", "head"), (_RFLUSH, None),
                      ("%s · press w to cycle  " % layout, "head")])
    # F-77 (user 2026-08-14 "아니 헤더 자체를 통째로 날려"): the WIDE board carries no column
    # header row at all. It named three columns and by now only two were columns — F-68c
    # retired the time column, and F-77 left the third holding a unit/capability/node token
    # that no single word covers. What remained labelled the two self-evident fields (a
    # harness name, a session name) and spent a line of every screen doing it. The group
    # cards carry the visual grouping.
    first = True
    folded_groups = []       # dormant dirs — aggregated into ONE line at the bottom (user: the
                             # stack of per-dir folded rules at the bottom was visual noise)
    for name in order:
        group_selectable_start = len(_SELECTABLE)
        g = groups[name]
        emission = emission_by_group[name]
        group_sessions = emission["group_sessions"]
        group_jobs = emission["group_jobs"]
        if emission["empty"]:
            continue    # empty-group suppression per --section: no dangling header

        # group fold decision (R4) — computed BEFORE emitting anything for this group.
        fold = emission["fold"]
        live_sessions = [s for s in group_sessions
                         if s.liveness not in ("stale", "dead") and not s.app_server]

        if fold:
            folded_groups.append((name, len(group_sessions)))
            continue
        repo_folded = (name not in ("loops", _SYSTEM_GROUP)
                       and bool(_REPO_FOLD.get(name, False)))

        if not first:
            lines.append(None)
        first = False

        # F-80 L2a: the exact predicate collect_all's ParentEdgeTracker confirms edges
        # against (model.session_parent_visible) — single definition, not a parallel copy.
        shown = emission["shown"]
        hidden = emission["hidden"]
        classified = emission["classified"]
        children = classified["children"]      # session_id -> [jobs] (nested under an on-screen parent)
        job_children = classified["job_children"]  # parent dispatch slug -> [dispatch-depth-2 jobs]
        orphans = classified["orphans"]        # project-level fallback (parent dead/off-screen/no-env)
        loops_jobs = classified["loops_jobs"]  # no-parent-is-normal (cron loops) — no orphan marker
        grace_jobs = classified["grace_jobs"]  # F-80 L2: confirmed edge, parent filtered off-screen
        recovered_session_ids = classified["recovered_session_ids"]

        gcwd = "" if name in ("loops", _SYSTEM_GROUP) else (group_sessions[0].cwd if group_sessions else
                (group_jobs[0].cwd if group_jobs else
                 (emission["resources"][0].cwd if emission["resources"] else "")))
        # The section title has no indicator glyph; the title itself carries active state.
        # while the group works, plain bold otherwise. Doubles with the active card tint.
        n_work = sum(1 for s in live_sessions if s.liveness == "working") + \
                 sum(1 for j in group_jobs if j.liveness == "working") + \
                 len(emission["gpu"]) + len(_group_running_resources(g))
        # With no active work, a recent transcript write marks file activity only.
        # Completion is shown separately from a recorded result and its event time.
        _last_act = max((s.mtime for s in group_sessions if s.mtime), default=None)
        _cool_min = None
        if not n_work and _last_act is not None:
            _age = (time.time() - _last_act) / 60.0
            if 0 <= _age <= _COOL_WINDOW_MIN:
                _cool_min = int(_age)
        # trailing slash = the universal "this is a directory" marker (ls convention — user
        # Keep the folder marker dim so the repository name remains the focal point.
        # The blinking green ● now lives HERE (directory level = "work happening inside") —
        # sessions animate a spinner instead, so the dot no longer collides with row vocabulary.
        head_segs = []
        if n_work:
            head_segs += [("●", "g_work" if _BLINK_ON else "g_work_off"), (" ", None)]
        elif _cool_min is not None:
            # Recent file activity uses a filled grey dot, distinct from dead and stale.
            head_segs += [(_COOL_FILLED, "grp_cool"), (" ", None)]
        else:
            # Long inactivity uses a grey ring.
            head_segs += [(_COOL_RING, "grp_cold"), (" ", None)]
        # Cooling names share the dim-yellow indicator color; cold keeps the default title color.
        _name_key = "grp_hot" if n_work else ("grp_cool" if _cool_min is not None else "grp")
        head_segs += [(name, _name_key)]
        if name != _SYSTEM_GROUP:
            head_segs.append(("/", "dim"))
        _nwt = max((getattr(entity, "worktree_count", 0)
                    for entity in group_sessions + group_jobs), default=0)
        if _nwt:
            # Match statusline notation for parallel or leftover worktrees.
            head_segs += [(" 🚧 %d" % _nwt, "g_idle")]
            _seen_glyphs.add("wt")
        _nmem = mem_by_group.get(name, 0)
        if _nmem:
            head_segs += [(" 🧠 %d" % _nmem, "dim")]
            _seen_glyphs.add("mem")
        last_result = _group_last_result(group_sessions, group_jobs, emission["resources"])
        if last_result:
            lead = "✓ " if last_result["result"] == "success" else "✕ "
            available = _context_content_width(term_width) or 160
            age_width = _dw(" · " + _text_age(last_result["at"]))
            result_room = min(_dw(lead + last_result["name"]) + age_width,
                              max(age_width + _dw(lead) + 1, available // 2))
            head_segs = _clip_segs(head_segs, max(0, available - result_room - 2))[0]
            room = max(0, available - sum(_dw(t) for t, _ in head_segs) - 2)
            label = _aged_text(lead + last_result["name"], last_result["at"], room)
            if label:
                head_segs.append(("  " + label, "dim"))
        elif _cool_min is not None:
            head_segs += [("  " + _text_age(_last_act), "dim")]
        if name not in ("loops", _SYSTEM_GROUP):
            head_segs = [("▸ " if repo_folded else "▾ ", "dim")] + head_segs
            _REPO_FOLDABLE.append({"line": len(lines), "repo_key": name,
                                   "folded": repo_folded})
        # The group header is the card's first title row.
        # tinted row of the panel, ▍ anchor on the card's padding edge; no floating label.
        _g0 = len(lines)                # panel start (title INCLUDED in the tint range)
        lines.append(head_segs)
        if name == _SYSTEM_GROUP:
            curate_row = _periodic_curate_row(memory, display_snapshot)
            if curate_row is not None:
                lines.append(curate_row)
        _rail_key = "grp_live" if n_work else "dim"
        if n_work:
            _body_tint = _TINT_BODY_HOT       # Active: midnight-blue tint.
        elif _cool_min is not None:
            _body_tint = _TINT_BODY_COOL      # Cooling: middle level between active and inactive.
        else:
            _body_tint = _TINT_BODY           # Inactive: dark grey.

        if repo_folded:
            if _TINT_OK:
                lines[_g0] = [(_body_tint, None)] + lines[_g0]
                lines.insert(_g0 + 1, [(_body_tint, None), ("  ", None)])
                lines.append([(_body_tint, None), ("  ", None)])
            continue

        # rows stay tight (no blank line — that spread them too far apart); the mid-line gauge
        # glyph (━/─) is what keeps the stacked context bars from merging into a solid wall.
        _srow = {"wide": None, "narrow": _session_row_2line, "stack": _session_row_stack}[layout]
        _jrow = {"wide": None, "narrow": _dispatch_row_2line, "stack": _dispatch_row_stack}[layout]
        # LIVE main-session lines get bold text (user 2026-07-03, after a whole-row tint-
        # Use font weight rather than brightening the entire row background.
        # carries the distinction). Excludes stale/dead/app-server/detached (already faded dim).
        _sess_bold_ids = set()

        def _emit_dispatch_tree(job, parent_model=None, parent_harness=None, parent_effort=None,
                                orphan=False, is_last=True, in_card=False, detached_root=False,
                                card_rail_col=None):
            # Row authority is the attached WorkProjection.  No first-child or
            # first-route selection is allowed in this render-local tree walk.
            block_start = len(lines)   # F-64c: the dispatch-depth-1 rail spans everything emitted below
            depth = max(1, int(getattr(job, "depth", 1) or 1))
            # `detached_root`: a dispatch-depth-2 row whose OWN depth-1 owner is not on screen,
            # recovered onto its session so a broken parent edge cannot make it vanish. It is a
            # tree root here even though its depth says otherwise, so it owns a card like any
            # depth-1 owner does. Without this it rendered frameless between the session row and
            # the next card — a depth-2 worker floating outside every box (user 2026-08-19).
            owns_card = not orphan and (depth == 1 or detached_root or _is_owner_mode_row(job))
            in_card = in_card or owns_card
            row_rail_col = (_RAIL_COL if card_rail_col is None else card_rail_col) if in_card else None
            stage_override = _projection_stage_for_dispatch(job)
            route_seq = _projection_route_seq(job)
            if job.liveness == "stale":
                _seen_glyphs.add("stale")
            elif job.liveness == "dead":
                _seen_glyphs.add("dead")
            # A child may cross harnesses (for example Codex -> Claude). Never fill an
            # unknown Claude model/effort with the Codex parent's telemetry.
            same_runtime = not job.harness or not parent_harness or job.harness == parent_harness
            row_parent_model = parent_model if same_runtime else None
            row_parent_effort = parent_effort if same_runtime else None
            # F-67 (user 2026-08-14 실측): the card pulses on UNIT activity. A standard+
            # owner idles while its depth-2 stage workers do the work, so gating the
            # frame and the current breadcrumb token on the owner's own liveness froze
            # an actively working card. Owner OR any nested descendant working = unit works.
            unit_working = None
            # Keyed on DEPTH, not on card ownership: an orphan depth-1 row draws no frame but
            # still owns a breadcrumb, and gating this on `owns_card` dropped its live hue.
            if owns_card or depth == 1:
                unit_working = (job.liveness == "working"
                                or any(s2.liveness == "working"
                                       for s2 in job_children.get(job.slug, []))
                                or any(r.liveness == "working"
                                       for r in getattr(job, "resource_children", ())))
            # F-27: a job row is a target only with an exact pid to verify (prd.md:253).
            if job.pid:
                _SELECTABLE.append(_select_entry_job(job, len(lines)))
            _dispatch_lines_before = len(lines)
            if _jrow:
                lines.extend(_jrow(job, orphan=orphan, parent_model=row_parent_model,
                                   parent_effort=row_parent_effort, stage_override=stage_override,
                                   route_seq=route_seq, in_card=in_card,
                                    unit_working=unit_working, card_rail_col=row_rail_col))
            else:
                lines.append(_dispatch_row(job, orphan=orphan, parent_model=row_parent_model,
                                           parent_harness=parent_harness,
                                           parent_effort=row_parent_effort, is_last=is_last,
                                           stage_override=stage_override,
                                           name_width=(wide_name_width if _route_rides_the_rail(
                                               job, route_seq, in_card) else
                                               card_name_width if in_card else wide_name_width),
                                           route_seq=route_seq, route_zone=wide_route_zone,
                                           in_card=in_card, unit_working=unit_working,
                                           # F-83: unframed rows need the same right-edge
                                           # ledger to clamp their open-row breadcrumb zone.
                                            card_interior=_dispatch_box_width(term_width, layout) - 1,
                                            card_rail_col=row_rail_col))
            # F-97b legend gate: _seen_glyphs is local to this call (R3), and
            # _branch_suffix_segs runs inside a helper with no closure over it, so the
            # loc_repo/loc_wt glyph is inferred here from the row it just emitted.
            for _line in lines[_dispatch_lines_before:]:
                if any(_k == "loc_repo" for _t, _k in _line):
                    _seen_glyphs.add(
                        "loc_foreign" if any(_t.startswith("→") for _t, _k in _line)
                        else "loc_wt"
                    )
            # 2026-07-24: a dispatch card's second line is its live log summary ONLY —
            # the same "identity row + fresh NOW" shape as a main-session card. The
            # pipeline rides the row's own breadcrumb; dedicated stage rows are a
            # session-card surface.
            detail = _dispatch_summary_detail_row(
                job, depth=max(1, int(getattr(job, "depth", 1) or 1)), term_width=term_width,
                orphan=orphan, in_card=in_card)
            if detail:
                lines.extend(detail)
            report_detail = _report_verification_detail_row(
                job, depth=max(1, int(getattr(job, "depth", 1) or 1)),
                term_width=term_width, in_card=in_card)
            if report_detail:
                lines.extend(report_detail)
            # F-29 — the child session's own sub-agents, one strip directly under the
            # dispatch row that represents it (depth-indented; active always, completed
            # only with `a` — the same convention as session-owned strips above).
            job_subs = getattr(job, "subagents", None)
            if job_subs is None:
                job_subs = child_subs_by_pid.get(job.pid) if job.pid else None
            shown_job_subs = [sa for sa in (job_subs or []) if sa.active or _SHOW_ALL]
            if shown_job_subs:
                lines.extend(_subagent_strip(
                    shown_job_subs, depth=depth, in_card=in_card,
                    term_width=term_width))
            gpu_rows, resource_rows = _owner_gpu_resource_rows(
                job, session_by_identity, gpu_resources, term_width, depth=depth, in_card=in_card)
            resource_children = getattr(job, "resource_children", ())
            if not resource_children:
                lines.extend(gpu_rows)
            # Everything emitted above belongs to the owner itself (identity row, its
            # NOW line, its own sub-agent strip). Descendants start here, so this index
            # is where the header divider goes once the frame is drawn.
            header_end = len(lines)
            if resource_children:
                lines.extend(gpu_rows)
            lines.extend(resource_rows)
            # The map contains depth-2 workers keyed by their depth-1 owner's
            # slug. A worker can reuse that slug (or another owner's), so looking
            # it up from a leaf would revisit itself or a sibling tree forever.
            # Only depth-1 owners expand children; depth-2 rows remain leaves.
            descendants = job_children.get(job.slug, []) if depth == 1 else []
            for sub in _sort_group_jobs(descendants):
                # F-15b P0-2: only a completed stage is absorbed into the
                # conductor breadcrumb. A registered queued/idle/unknown child
                # is still execution identity and must keep its own row.
                if (max(1, int(getattr(sub, "depth", 1) or 1)) >= 2 and not _SHOW_ALL
                        and _fold_completed_child(sub)):
                    continue
                _emit_dispatch_tree(sub, parent_model=job.model or parent_model,
                                    parent_harness=job.harness or parent_harness,
                                    parent_effort=parent_effort, orphan=False,
                                    in_card=in_card, card_rail_col=card_rail_col)
            # F-66: a depth-1 owner and all descendants form one complete box. The
            # frame itself carries hierarchy, so descendants share the owner's body
            # column and every row reaches the same right edge.
            if owns_card:
                if unit_working or job.liveness == "working":
                    color_i = _depth1_rail_color_index(
                        getattr(job, "key", None),
                        stage_override or getattr(job, "stage", None), route_seq)
                    # F-70 (user 2026-08-14 "테두리 점멸은 좀 부산스럽긴 하네. 끄자"):
                    # the frame is STEADY. An active unit still owns its stage hue —
                    # the liveness signal lives in the row glyphs and the breadcrumb
                    # token, which already blink; a whole box outline joining them was
                    # too busy. `_BLINK_ON` is intentionally not consulted here.
                    # F-72: and that hue is drawn DIM, so an active unit reads as
                    # active without the box out-inking the session that owns it.
                    rail_key = "frm%d" % color_i
                    run_key = rail_key
                else:
                    rail_key = "frm_idle"
                    run_key = rail_key
                box_width = _dispatch_box_width(term_width, layout)
                # Every box must clear the under-id column; worker model/state
                # columns stay put while only its frame/prefix takes the inset.
                rail_col = _RAIL_COL if card_rail_col is None else card_rail_col
                for idx in range(block_start, len(lines)):
                    # A nested OWNER has already framed its rows and closed its own stage.
                    if any(isinstance(key, str) and key.startswith("frm")
                           and any(mark in value for mark in ("╭", "╰", "├", "│"))
                           for value, key in lines[idx]):
                        continue
                    lines[idx] = _frame_dispatch_line(
                        lines[idx], box_width, "top" if idx == block_start else "mid",
                        rail_key, run_key=run_key, rail_col=rail_col,
                        owner_identity=(idx < header_end and _route_rides_the_rail(job, route_seq, True)))
                # F-81: every OWNER stage/status shares the header divider when
                # children exist, otherwise the close rail. Keep the label once.
                route_label = None
                if _route_rides_the_rail(job, route_seq, in_card=True):
                    budget = max(1, bottom_label_budget(box_width, rail_col))
                    residue = getattr(job, "residue_pids", None)
                    if residue:
                        warning = "⚠ worker left pid %s running" % ",".join(map(str, residue))
                        route_label = [(_clip_w(warning, budget), "gate_u")]
                    elif job.liveness == "dead":
                        boundary = getattr(job, "resume_boundary", None) or "-"
                        if getattr(job, "note", None) == "dead-parent-orphaned":
                            token = "⚠ ORPHANED resume=%s" % boundary
                        elif getattr(job, "_dead_terminal_owner", False):
                            token = "✕ %s resume=%s" % (job.note or "dead", boundary)
                        else:
                            token = "dead @%s" % (stage_override or job.stage or job.key)
                        route_label = [(_clip_w(token, budget), "g_dead")]
                    elif route_seq:
                        route_label = _route_stage_segs(
                            route_seq, unit_working or job.liveness == "working", budget)
                    elif (getattr(job, "afterglow", False) or job.liveness == "done"
                          or (job.liveness == "stale" and not unit_working)):
                        route_label = [("done ✓", "dim")]
                    elif (getattr(job, "worker_type", None) == "owner"
                          and (job.intensity or "") != "quick" and not stage_override):
                        route_label = [("—", "dim")]
                    else:
                        route_label = _dispatch_stage_segs(
                            job, job.key, stage_override, job.slug,
                            working=bool(unit_working), route_zone=budget)
                    if getattr(job, "row_terminal_mismatch", False) and not residue:
                        warning = " ⚠alive"
                        route_label = _clip_segs(route_label, max(1, budget - _dw(warning)))[0]
                        route_label.append((warning, "gate_u"))
                # Retain the divider when raw children exist, even if they folded away.
                has_children = bool(job_children.get(job.slug) or resource_children or resource_rows)
                if has_children:
                    lines.insert(header_end,
                                 _dispatch_box_divider(box_width, rail_key, run_key=run_key,
                                                       label_segs=route_label, rail_col=rail_col))
                lines.append(_dispatch_box_bottom(box_width, rail_key, run_key=run_key,
                                                  label_segs=None if has_children else route_label,
                                                  rail_col=rail_col))

        shown = _sort_group_sessions(shown)
        if live_order is not None:
            shown = live_order.reconcile_sessions(name, shown)
        shown, steward_edges = _steward_hierarchy(shown)
        session_starts, session_identity_rows = {}, set()
        rendered_parent_sids = set()  # ambiguous enrichment must not duplicate a dispatch tree
        for s in shown:
            if getattr(s, "mem_worker", False):
                # Automation rows belong only to the dedicated system card.
                lines.extend(_mem_row(s, layout))
                _seen_glyphs.add("mem")
                continue
            kids = _sort_group_jobs(
                children.get(s.session_id, [])
            )
            if s.session_id in rendered_parent_sids:
                kids = []
            elif s.session_id:
                rendered_parent_sids.add(s.session_id)
            plugin_kids = [kid for kid in kids if _is_plugin_agent(kid)]
            # F-105: finished frame legs fold away in whole route groups (see
            # `_finished_frame_fold_ids`). Filter them out of the drawn kids
            # once, so the child count, the card check below, and the
            # dispatch-tree walk all see the same visible set.
            folded_frame_ids = (set() if _SHOW_ALL
                                else _finished_frame_fold_ids(kids))
            dispatch_kids = [kid for kid in kids
                             if not _is_plugin_agent(kid)
                             and id(kid) not in folded_frame_ids]
            nested_n = (len(dispatch_kids)
                        + sum(len(job_children.get(k.slug, [])) for k in dispatch_kids))
            if s.liveness == "stale":
                _seen_glyphs.add("stale")
            elif s.liveness == "dead":
                _seen_glyphs.add("dead")
            elif s.liveness == "unused":
                _seen_glyphs.add("unused")
            elif s.liveness == "blocked":
                _seen_glyphs.add("blocked")
            if s.detached and s.liveness not in ("stale", "dead"):
                _seen_glyphs.add("detached")
            if getattr(s, "session_tag", None):
                _seen_glyphs.add("tag")                       # F-100a
            if getattr(s, "steward", False):
                _seen_glyphs.add("steward")                   # F-100c
            if getattr(s, "multiplexer", None) == "tmux":
                _seen_glyphs.add("tmux")                      # aedd50
            elif getattr(s, "herdr_attached", None) is True:
                _seen_glyphs.add("herdr")                     # F-100b
            elif getattr(s, "herdr_attached", None) is False:
                _seen_glyphs.add("tty")
            if getattr(s, "pane_corner", None) is False:
                _seen_glyphs.add("corner")                    # aedd50
            if nested_n:
                _seen_glyphs.add("child")
            if getattr(s, "subagents", None):
                _seen_glyphs.add("subagent")
            if plugin_kids:
                _seen_glyphs.add("subagent")
            # D3: suppression follows whether a depth-1 owner card actually renders under
            # this session, not whether the session and a child agree on an exact route.
            # Every direct dispatch_kids child owns a card (`orphan=False` and either
            # depth == 1 or the depth>=2 detached-root recovery both satisfy
            # `_emit_dispatch_tree`'s `owns_card`) — a card is present whenever any exist,
            # so a lost route binding stops synthesizing a duplicate session-row stage
            # without leaving both the card and the session stage-less.
            card_owner_present = bool(dispatch_kids)
            recovered_session_owner = s.session_id in recovered_session_ids
            suppress_session_stage = card_owner_present or recovered_session_owner
            _n0 = len(lines)
            session_starts[id(s)] = _n0
            session_identity_rows.add(_n0)
            if _selectable_session(s):
                _SELECTABLE.append(_select_entry(s, _n0))    # F-27 target map
            if _srow:
                lines.extend(_srow(s, is_parent=bool(nested_n), child_count=nested_n,
                                   term_width=term_width,
                                   show_projection_stage=not suppress_session_stage,
                                   tag_by_key=tag_by_key))
            else:
                lines.append(_session_row(s, narrow, is_parent=bool(nested_n),
                                          child_count=nested_n,
                                          name_width=wide_name_width,
                                          show_projection_stage=not suppress_session_stage,
                                          stage_zone=wide_route_zone,
                                          tag_by_key=tag_by_key))
            if not (s.liveness in ("stale", "dead") or s.app_server or s.detached):
                _sess_bold_ids.update(range(_n0, len(lines)))
            # F-69: main-session rows get the brighter, bold NOW sentence; dispatch
            # subtitles keep the dim supporting weight (see _dispatch_summary_detail_row).
            detail = _context_detail_row(s, term_width=_context_content_width(term_width), now_key="now_main")
            if detail:
                lines.extend(detail)
            # F-103: the route chain lives in the session's stage cell (last three nodes);
            # it has no line of its own.
            # F-101a: relation strips follow the session detail rows, not the 44-column subtitle.
            _peer_last = getattr(s, "peer_last_recv", None)
            # The session has no full-route detail surface. Route progress belongs
            # to its owner card and process view, even when that card is absent.
            # F-29 (v9) — sub-agent rows, directly under the parent session's own row(s).
            # Active always shown; completed only surface with `a` (F-18b dim-row convention).
            shown_subs = [sa for sa in (getattr(s, "subagents", None) or [])
                         if sa.active or _SHOW_ALL]
            if shown_subs:
                lines.extend(_subagent_strip(shown_subs, term_width=term_width))
            session_resources = _gpu_resources_for_session(s, parent_gpu_resources)
            if session_resources:
                lines.extend(_gpu_resource_strip(session_resources, term_width=term_width))
            # Peer communication occupies the identity row; accepted follow-through
            # remains a separate obligation signal below the existing detail rows.
            lines.extend(_peer_obligation_strip(getattr(s, "peer_obligations", None),
                                                term_width=term_width))
            for plugin_job in plugin_kids:
                lines.extend(_plugin_agent_row(plugin_job, term_width=term_width))
            for i, cj in enumerate(dispatch_kids):
                _emit_dispatch_tree(cj, parent_model=s.model, parent_harness=s.harness,
                                    parent_effort=s.effort, orphan=False,
                                    is_last=(i == len(dispatch_kids) - 1),
                                    detached_root=(
                                        max(1, int(getattr(cj, "depth", 1) or 1)) >= 2
                                    ), card_rail_col=_RAIL_COL)
        # Draw each supervisor interval after its complete session/card block exists.
        # The line ends above the last watched session: none of that session's rows
        # carry it (user 2026-10-14).
        connector_rows = set()
        for parent, targets in steward_edges.items():
            if parent in session_starts and targets:
                stop = session_starts.get(id(targets[-1]))
                if stop is not None:
                    connector_rows.update(range(session_starts[parent] + 1, stop))
        for idx in sorted(connector_rows - session_identity_rows):
            if lines[idx]:
                lines[idx] = _under_id_connector(lines[idx])
        if group_sessions and hidden:
            lines.append([("     +%d stale/companion hidden" % hidden, "dim")])

        # orphans / loops: project-level fallback (standalone tree rows)
        if orphans:
            lines.append(_orphan_divider(term_width))
        for oj in _sort_group_jobs(orphans):
            if _is_plugin_agent(oj):
                _seen_glyphs.add("subagent")
                lines.extend(_plugin_agent_row(oj, orphan=True, term_width=term_width))
            else:
                _emit_dispatch_tree(oj, orphan=show_sessions)
        for lj in _sort_group_jobs(loops_jobs):
            if getattr(lj, "worker_type", None) == "support":
                lines.append(None)
                caller = (" · caller " + lj.caller_pane) if lj.caller_pane else ""
                lines.append([('  project support' + caller, "dim")])
            _emit_dispatch_tree(lj, orphan=False)
        # F-80 L2c: a grace-held edge stays a standalone tree row in the SAME group, no
        # `(orphan)` marker, no divider — nesting under the parent is unavailable because
        # that row is itself filtered off-screen this tick, but its card/group membership
        # is preserved exactly as the confirmed edge, not re-derived as project-level loss.
        for gj in _sort_group_jobs(grace_jobs):
            _emit_dispatch_tree(gj, orphan=False)

        # F-104: live GPU work that no registry run or session GPU strip shows.
        if emission["resources"]:
            gpu_rows, resource_rows = _gpu_and_resource_rows(
                emission["resources"], term_width=term_width, commands=True,
                excluded_processes=g.get("resource_excluded_processes", ()))
            lines.extend(gpu_rows + resource_rows)
        lines.extend(_gpu_work_strip(emission["gpu"], term_width))

        # F-19 repo rows (사용자 결정 2026-11-02, replacing the 2026-07-16 today-only rule):
        # this card's own recent mem events (latest K per repo, dated), below a subtle
        # in-band divider — entirely silent when the repo has none (healthy-silent,
        # §4.7 F-19 convention). Rides the same body-tint loop below, unmodified.
        repo_events = (memory or {}).get("by_repo", {}).get(name) if memory else None
        if repo_events:
            lines.append(_mem_divider(term_width))
            lines.extend(_mem_repo_rows(repo_events, sid_titles, term_width=term_width))

        # group BODY (round-5): every row of the group rides the body tint — the whole directory
        # The whole directory block is one panel, brighter when active.
        # Fallback (_TINT_OK False → 8-color): the previous ▍ rail marks the block instead.
        for _i in range(_g0, len(lines)):
            ln = lines[_i]
            if not ln or _is_fill(ln[0][0]):
                continue
            if _TINT_OK:
                lines[_i] = [(_body_tint, None)] + ln
            elif ln[0][1] in (None, "dim") and ln[0][0].startswith(" "):
                lines[_i] = [("▍", _rail_key), (ln[0][0][1:], ln[0][1])] + ln[1:]
        for _i in _sess_bold_ids:
            ln = lines[_i]
            if not ln:
                continue
            if _is_fill(ln[0][0]) and ln[0][0][1] in _TINT_CHARS:
                lines[_i] = [ln[0], (_ROW_BOLD, None)] + list(ln[1:])
            else:
                lines[_i] = [(_ROW_BOLD, None)] + list(ln)
        if _TINT_OK:
            # breathing row below the title + bottom padding (title-top padding tried and
            # Insert one sentinel after the tint loop.
            lines.insert(_g0 + 1, [(_body_tint, None), ("  ", None)])
            lines.append([(_body_tint, None), ("  ", None)])
            for entry in _SELECTABLE[group_selectable_start:]:
                if entry["line"] > _g0:
                    entry["line"] += 1
        for entry in _SELECTABLE[group_selectable_start:]:
            entry["repo_key"] = name

    # dormant dirs — one aggregated line, clearly set apart from the active board (blank + dim).
    # Contains the word 'folded' so the click-toggle map and `a` both still reveal them.
    if folded_groups:
        names = " · ".join(n for n, _c in folded_groups)
        total = sum(c for _n, c in folded_groups)
        lines.append(None)
        lines.append(([(" " * (_INSET + _PAD_IN), None)] if _TINT_OK else []) + [("· ", "dim"),
                      ("inactive  +%d folded   " % total, "dim"),
                      (names[:90] + ("…" if len(names) > 90 else ""), "dim")])

    if not order:
        message = ('  sessions loading…' if loading else '  jobs.log unknown'
                   if _jobs_unconfirmed(observations) else
                   '  no observed sessions/jobs')
        lines.append([(message, "dim")])

    diagnostic_rows = _diagnostic_rows(resource_diagnostics, malformed, term_width)
    diagnostic_rows.extend(_delivery_pending_rows(pending_delivery))
    if diagnostic_rows:
        lines.append(None)
        lines.extend(diagnostic_rows)

    # legend — status dots (columns are labelled by the header row). F-12(c): working/idle/
    # dispatch/`~` are always-relevant vocabulary and stay unconditional; the rest only appear
    # when this build actually used them (_seen_glyphs, tracked above — local, not global).
    legend_start = len(lines)
    lines.append(None)
    legend = [
        ('  session: ', None), ("⠹", "g_spin"), (' working   ', "dim"),
        ("●", "g_work_off"), (' idle   ', "dim"),
    ]
    if "unused" in _seen_glyphs:
        legend += [(_LIVE_GLYPH["unused"], "g_unused"), (' unused   ', "dim")]
    if "detached" in _seen_glyphs:
        legend += [(_DETACHED_GLYPH, "g_work_off"), (' detached   ', "dim")]
    if "stale" in _seen_glyphs:
        legend += [("·", "g_stale"), (" stale   ", "dim")]
    if "dead" in _seen_glyphs:
        legend += [("✕", "g_dead"), (" exited   ", "dim")]
    if "degraded" in _seen_glyphs:
        legend += [("◐", "lvl_y"), (' stage unknown   ', "dim")]
    if "recovering" in _seen_glyphs:
        legend += [("…", "lvl_y"), (" recovery   ", "dim")]
    if "blocked" in _seen_glyphs:
        legend += [("◑", "g_blocked"), (" input needed   ", "dim")]
    if "tag" in _seen_glyphs:
        legend += [("[", "dim"), ('id', "tag"), ("]", "dim"), (' session   ', "dim")]  # F-100a
    if "steward" in _seen_glyphs:
        legend += [("[", "dim"), ('id', "tag_steward"), ("]", "dim"), (' steward   ', "dim")]  # F-100c
    if "herdr" in _seen_glyphs:
        legend += [(_CTX_ON_TEXT, "herdr_on"), (' pane   ', "dim")]                  # F-100b
    if "tmux" in _seen_glyphs:
        legend += [(_CTX_TMUX_TEXT, "herdr_on"), (' pane   ', "dim")]               # aedd50
    if "corner" in _seen_glyphs:
        legend += [(_CTX_CORNER_MARK, "lvl_y"), (' ⚑ not top-left: pane tidy   ', "dim")]
    if "tty" in _seen_glyphs:
        legend += [(_CTX_OFF_TEXT, "dim"), (' plain terminal   ', "dim")]
    if "child" in _seen_glyphs:
        legend += [("▾N", "dim"), (' child jobs   ', "dim")]
    if "subagent" in _seen_glyphs:
        legend += [(_ICON_SUBAGENT, "dim"), (' sub-agent   ', "dim")]
    if any(not _is_plugin_agent(job) for job in jobs):
        legend += [("↳", "dim"), (' dispatch   ', "dim")]
    if "wt" in _seen_glyphs:
        legend += [("🚧 N", "dim"), (' worktrees   ', "dim")]
    if n_mem_total or "mem" in _seen_glyphs:
        # Always expose the board-wide memory total in the legend, even when memory-only groups fold.
        legend += [("🧠 %d" % n_mem_total, "dim"), (' mem   ', "dim")]
    if "loc_foreign" in _seen_glyphs:
        legend += [("→", "loc_repo"), (' foreign repo   ', "dim")]
    if "loc_wt" in _seen_glyphs:
        legend += [("⌂wt", "loc_repo"), (' worktree   ', "dim")]
    # F-9(d) `~ derived/inherited value` retired with the marker itself (user 2026-07-16:
    # inherited effort now shows plain — the tilde read as noise).
    lines.extend(_wrap_legend(legend, term_width))
    lines.extend(_reading_legend(term_width, session=False))

    _LAYOUT_META = {"fixed_end": fixed_end, "body_start": body_start,
                    "body_end": legend_start, "legend_start": legend_start,
                    "fixed_priorities": fixed_priorities}

    return lines


def _explorer_once_frame(snapshot, cols, rows, state=None, reader=None, detail=False):
    """The once screen uses the same snapshot-to-cells projection as the live view. ``detail`` also
    makes the reads the live view would do for this state (never for the hermetic ``--demo`` rows)."""
    global _EXPLORER_RESULT
    state = _explorer_state_for(state if state is not None else _EXPLORER_STATE, cols)
    _EXPLORER_RESULT = None
    frame = _explorer_compose(snapshot, cols, rows, state)
    if detail and not os.environ.get("FLEET_DEMO"):
        _EXPLORER_RESULT = explorer_read_detail(snapshot, state)
        frame = _explorer_compose(snapshot, cols, rows, state, _EXPLORER_RESULT)
    return frame


def render_once(collect_all, hfilter, section, *, compute_hosts_refresh=None):
    global _GIT_TELEMETRY
    governor_read = background_read(_collect_governor)
    hosts_read = (background_read(compute_hosts_refresh)
                  if callable(compute_hosts_refresh) else None)
    try:
        snapshot = as_snapshot(collect_all(harness_filter=hfilter))
        sessions, jobs = snapshot
    except BaseException:
        # Drain both reads. Preserve the original serial failure priority:
        # hosts before sessions before governor (normally fail-soft).
        try:
            governor_read.result()
        except BaseException:
            pass
        if hosts_read is not None:
            set_compute_hosts(hosts_read.result())
        raise
    else:
        try:
            governor_snapshot = governor_read.result()
        except Exception as exc:
            governor_snapshot = None
            snapshot.observations["governor"] = {
                "state": "failed", "last_error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            if hosts_read is not None:
                try:
                    set_compute_hosts(hosts_read.result())
                except Exception as exc:
                    snapshot.observations["compute hosts"] = {
                        "state": "failed", "last_error": "%s: %s" % (type(exc).__name__, exc)}
    resources = snapshot.resources
    usage_snapshots = snapshot.usage_snapshots
    malformed = snapshot.malformed
    resource_diagnostics = snapshot.resource_diagnostics
    mem_snapshot = _collect_memory()
    if _EXPLORER_ENABLED:
        import shutil
        size = shutil.get_terminal_size((100, 45))
        snapshot.governor, snapshot.memory = governor_snapshot, mem_snapshot
        snapshot.project_status = _collect_project_status(sessions)
        gitinfo.enrich_entities(list(sessions) + list(jobs), schedule_ahead=False)
        frame = _explorer_once_frame(snapshot, size.columns, size.lines, detail=True)
        sys.stdout.write("\n".join(frame.text_rows()).rstrip() + "\n")
        return 0
    gitinfo.enrich_entities(list(sessions) + list(jobs), schedule_ahead=False)
    try:
        import shutil
        tw = shutil.get_terminal_size().columns
    except Exception:
        tw = 200
    previous_git_telemetry = _GIT_TELEMETRY
    _GIT_TELEMETRY = False
    try:
        lines = _build_lines(sessions, jobs, section, narrow=False, malformed=malformed,
                             layout=_layout_mode(tw), memory=mem_snapshot, term_width=tw,
                             resources=resources, usage_snapshots=usage_snapshots,
                             governor=governor_snapshot, node_evidence=snapshot.node_evidence,
                             route_entities=snapshot.route_entities, observations=snapshot.observations,
                             resource_diagnostics=resource_diagnostics,
                             pending_delivery=snapshot.pending_delivery,
                             display_snapshot=getattr(snapshot, "display_snapshot", None))
    finally:
        _GIT_TELEMETRY = previous_git_telemetry
    colored = bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
    colored = colored and os.environ.get("TERM") != "dumb"
    colors = 8
    if colored:
        try:
            curses.setupterm()
            colors = curses.tigetnum("colors")
        except Exception:
            colors = 256 if "256color" in os.environ.get("TERM", "") else 8
    out = "\n".join(_snapshot_line(l, colored=colored, colors=colors) for l in lines) + "\n"
    # Write UTF-8 bytes directly so the snapshot's box/braille glyphs survive a
    # non-UTF-8 console codepage (e.g. Windows cp949), which would otherwise raise
    # UnicodeEncodeError. Falls back to text stdout when buffer is unavailable.
    try:
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.buffer.flush()
    except (AttributeError, ValueError):
        sys.stdout.write(out)
    return 0


def _addline(stdscr, row, segs, w):
    if segs is None:
        return []
    painted = []
    # leading row-tint sentinel (round-5 panels) — strip it FIRST so the fill scan below never
    # mistakes it for a fill sentinel; it sets the whole row's background level.
    tint = None
    if segs and _is_fill(segs[0][0]) and segs[0][0][1] in _TINT_CHARS:
        tint = segs[0][0][1] if _TINT_OK else None
        segs = segs[1:]
    # row-bold marker (user: main-session rows are entirely bold) — strip next, same reason.
    row_bold = False
    if segs and _is_fill(segs[0][0]) and segs[0][0][1] == "!":
        row_bold = True
        segs = segs[1:]
    # tinted panels are INSET two columns on both sides AND their content shifts inward with
    # them; near-black tint against the default background alone is
    # imperceptible; the margin must move the content). Band starts at col 2; the row's own
    # leading blanks become inner padding, so text sits at col 4+ while cols 0-1 stay default.
    start_col = _INSET if tint is not None else 0
    if tint is not None:
        segs = [(" " * _PAD_IN, None)] + list(segs)   # inner-left padding inside the band
    fillch = None
    left, right = segs, []
    for i, (t, _c) in enumerate(segs):
        if _is_fill(t):
            fillch = t[1]
            left, right = segs[:i], segs[i + 1:]
            break

    def _draw(seglist, start, lim=None, record_hits=False):
        edge = (w - 1) if lim is None else lim
        col = start
        for text, color in seglist:
            if col >= edge:
                break
            avail = edge - col
            piece = ""
            pw = 0
            for ch in text:                                   # clip by display width, not len
                cw = _cw(ch)
                if pw + cw > avail:
                    break
                piece += ch
                pw += cw
            if piece:
                attr = _key_attr(color, tint)
                if row_bold:
                    attr |= curses.A_BOLD
                try:
                    stdscr.addstr(row, col, piece, attr)
                    if record_hits and pw and piece.strip():
                        painted.append((col, col + pw, piece))
                except curses.error:
                    pass
            col += pw
        return col

    endcol = _draw(left, start_col, record_hits=True)
    # band lines (muted neutral bars = full width · round-5 tint panels = inset cards) paint their
    # background across; tint rows stop at w-1 so the right margin stays on the default bg.
    bar = bool(segs) and segs[0][1] in _BAR_ROLES
    band = bar or tint is not None
    band_lim = w if bar else (w - _INSET)
    # The bar inherits its color from the leading role, so a warning bar paints red across the
    # full width exactly as the normal bar paints neutral — same structure, different severity.
    fill_key = segs[0][1] if bar else None     # tint rows fill with the default-hue tint pair
    if fillch is not None:              # right may be EMPTY (a bare full-width rule line) — the
        rw = sum(_dw(t) for t, _ in right)   # fill itself must still draw (bug: divider invisible)
        rpad = (2 + _PAD_IN) if tint is not None else 0   # right-flushed text sits in from the band edge
        rcol = max(endcol + (0 if fillch == "─" else 2), (band_lim if band else w - 1) - rw - rpad)
        if fillch == "─" and rcol > endcol:
            _draw([("─" * (rcol - endcol), "head")], endcol)  # fill the gap to make a full-width rule
        elif band and band_lim > endcol:
            # paint the ENTIRE gap to the band edge first, then draw `right` over it — glyph
            # width disagreements (⏱ = 2 cells in our table, 1 by wcwidth/tmux) otherwise leave
            # an unpainted hole immediately before the time.
            _draw([(" " * (band_lim - endcol), fill_key)], endcol, lim=band_lim)
        if right:
            _draw(right, rcol, lim=band_lim if band else None, record_hits=True)
    elif band and endcol < band_lim:
        _draw([(" " * (band_lim - endcol), fill_key)], endcol, lim=band_lim)
    return painted


_OFFSET = 0                 # scroll offset — READ only in _draw (see module docstring)
_RESUME_ORDER = None         # private state inherited only by this viewer's replacement
_RELOAD_FRAME = None         # last rows remain visible until the first new observation
_RELOAD_FRAME_AT = None      # replay never renews the underlying evidence lifetime
_RELOAD_FRAME_COMPUTE_AT = None
_RELOAD_LAYOUT = None        # versioned fixed/body/legend geometry for viewer handoff
_SHELL_TTY_MODE = None       # original shell mode, rather than the old curses mode
_TOGGLE_ROWS = {}            # screen_y -> True, reset at the top of every _draw (mouse click map)
_TOGGLE_SPANS = {}           # screen_y -> actual painted x spans
_CLICK_ROWS = {}             # screen_y -> _SELECTABLE entry (F-27 v9 row click map, §4.2.1 —
                              # filled from _SELECTABLE, NOT _live_targets(): base mode's first
                              # click would otherwise never see a target, and gating on
                              # _live_targets() would call control.is_excluded() at ~10fps)
_FOLD_ROWS = {}               # screen_y -> card_key (F-30, v10) — filled from `_FOLDABLE` the
                              # same way `_CLICK_ROWS` is filled from `_SELECTABLE`. Reset at the
                              # top of every `_draw`, same as `_TOGGLE_ROWS`/`_CLICK_ROWS`.
_FOLD_SPANS = {}              # actual painted cell spans for route/GPU folds
_REPO_FOLD_ROWS = {}          # screen_y -> repo fold entry
_REPO_FOLD_SPANS = {}         # actual painted cell spans for repo titles
_CLICK_SPANS = {}             # actual painted cell spans for session/job actions
_HIT_VALID = False            # only the latest complete paint may receive a mouse event
_HIT_SIZE = None              # terminal geometry of that paint
_HIT_SNAPSHOT_ID = None       # snapshot identity used for that paint
_CURRENT_BODY_H = 0
_CURRENT_BODY_TOP = 0
_CURRENT_BODY_COUNT = 0
_PROMPT_HITS = []             # [(screen_y, x0, x1, "kill"|"cancel")] — footer button hitboxes.
                              # Reset+rebuilt every _draw call (§4.4.1): a click 2 that lands
                              # before the next _draw must never see a stale (pre-transition)
                              # map, or the confirm→confirm2 coordinate inversion (§4.4) is
                              # silently defeated.

# --- F-27 selection mode (see _SELECTABLE stash contract in _build_lines) ---
# A MODED cursor, not a bare ↑↓ cursor: ↑↓ is already bound to scroll (a v2 contract, spec
# §3 key table), so taking it would regress scrolling for everyone who never kills anything.
# `s`/`x` enter, ↑↓/jk then move the cursor, Esc/s leave. Only the "enter" key differs from
# spec F-27's wording; the operating model (row cursor, ↑↓ move, x kill, confirm) is intact.
_SELECT_MODE = False
# The cursor is an IDENTITY (pid, proc_start), not a list index and not a screen row. An index
# silently re-aims when the board rebuilds under it: rows come and go every tick, so index 0
# can mean a different session one tick later — the user aims at A and the prompt names B.
# Anchoring on identity means a rebuild either finds the same target or finds nothing (and the
# selection drops), which are both honest outcomes.
_CURSOR_ID = None            # (pid, proc_start) | None
_PROMPT = None               # None | {"stage": confirm|confirm2|escalate, "entry": {...}}
_PENDING_KILL = None         # {"entry":..., "since": ts} — SIGTERM sent, grace running
_LAST_ACTION = None          # transient footer feedback string


# ---------------------------------------------------------------------------
# F-27 selection mode helpers. Kept out of _loop so they are testable without curses.
# ---------------------------------------------------------------------------

def _live_targets():
    """Selectable rows minus the ones that may never be signalled. The exclusion runs HERE,
    before anything can be selected — so fleet itself, its ancestry, and the driving session
    are never even reachable by a prompt (prd.md:253)."""
    from . import control
    out = []
    for e in _SELECTABLE:
        if not e.get("pid"):
            continue
        try:
            if control.is_excluded(e["pid"]):
                continue
        except Exception:
            continue                     # unresolvable → not a target (fail closed)
        out.append(e)
    return out


def _cursor_index(targets):
    """Where the cursor identity currently sits, or None if that target is gone."""
    if _CURSOR_ID is None:
        return None
    for i, e in enumerate(targets):
        if _entry_id(e) == _CURSOR_ID:
            return i
    return None


def _enter_select(targets):
    global _SELECT_MODE, _CURSOR_ID
    if not targets:
        return False
    _SELECT_MODE = True
    if _cursor_index(targets) is None:
        _CURSOR_ID = _entry_id(targets[0])
    return True


def _exit_select():
    global _SELECT_MODE, _PROMPT
    _SELECT_MODE = False
    _PROMPT = None


def reset_selection():
    """Public: drop all selection/prompt state (tests + fleet.py belt-and-suspenders)."""
    global _SELECT_MODE, _CURSOR_ID, _PROMPT, _PENDING_KILL, _LAST_ACTION
    _SELECT_MODE = False
    _CURSOR_ID = None
    _PROMPT = None
    _PENDING_KILL = None
    _LAST_ACTION = None


def _set_action(msg):
    global _LAST_ACTION
    _LAST_ACTION = msg


def _handle_select_key(ch):
    """Selection-mode keys. True = handled here (do not fall through to scroll)."""
    global _CURSOR_ID, _PROMPT
    targets = _live_targets()
    if ch in (_ESC, ord("s"), ord("S")):
        _exit_select()
        return True
    if not targets:
        _exit_select()
        return True
    i = _cursor_index(targets)
    if i is None:
        # The target under the cursor vanished (it finished, or it was killed). Re-anchor at
        # the top rather than guessing which row "replaced" it.
        _CURSOR_ID = _entry_id(targets[0])
        i = 0
        if ch in (ord("x"), ord("X")):
            return True                  # swallow this press: do not aim `x` at a row the
                                         # user never chose
    if ch in (curses.KEY_UP, ord("k")):
        _CURSOR_ID = _entry_id(targets[max(0, i - 1)])
        return True
    if ch in (curses.KEY_DOWN, ord("j")):
        _CURSOR_ID = _entry_id(targets[min(len(targets) - 1, i + 1)])
        return True
    if ch in (ord("x"), ord("X")):
        _PROMPT = {"stage": "confirm", "entry": targets[i]}
        return True
    return False        # q / r / a / w still work from selection mode


def _handle_base_key(ch, body_h):
    """Base-mode keys (scroll/a/w). True = handled. Kept out of _loop so scroll can be
    tested without curses — the F-27 regression budget is 0 and an untestable budget is
    not a budget."""
    global _OFFSET
    if ch in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE,
              curses.KEY_HOME, curses.KEY_END, ord("k"), ord("j"), ord("g"), ord("G")) \
            and body_h <= 0:
        return True
    if ch in (curses.KEY_UP, ord("k")):
        _OFFSET -= 1
    elif ch in (curses.KEY_DOWN, ord("j")):
        _OFFSET += 1
    elif ch == curses.KEY_PPAGE:
        _OFFSET -= body_h
    elif ch == curses.KEY_NPAGE:
        _OFFSET += body_h
    elif ch in (curses.KEY_HOME, ord("g")):
        _OFFSET = 0
    elif ch in (curses.KEY_END, ord("G")):
        _OFFSET = 1 << 30    # clamp in _draw resolves this to maxoff
    elif ch in (ord("a"), ord("A")):
        set_show_all(not _SHOW_ALL)
    elif ch in (ord("c"), ord("C")):
        _toggle_gpu_commands()
    elif ch in (ord("w"), ord("W")):
        _cycle_layout()
    elif ch in (ord("p"), ord("P")):
        # F-30 (prd.md:305) — process view toggle. Deliberately orthogonal to `w` (layout
        # cycle keeps working inside the process view, same as the group view).
        set_process_view(not _PROCESS_VIEW)
    elif ch == ord(" "):
        if _SELECT_MODE or _PROCESS_VIEW or not _HIT_VALID:
            return True
        for row in sorted(_REPO_FOLD_ROWS):
            entry = _REPO_FOLD_ROWS[row]
            if _REPO_FOLD_SPANS.get(row):
                _set_repo_fold(entry["repo_key"], not entry["folded"])
                break
    else:
        return False
    return True


def _set_repo_fold(repo_key, folded):
    global _CURSOR_ID
    _REPO_FOLD[repo_key] = bool(folded)
    if folded and _SELECT_MODE:
        selected = next((e for e in _SELECTABLE if _entry_id(e) == _CURSOR_ID), None)
        if selected is not None and selected.get("repo_key") == repo_key:
            _exit_select()
            _CURSOR_ID = None


def _handle_mouse(mx, my):
    """Mouse is the FIRST-CLASS F-27 path (prd.md:279). Returns True = handled.

    Precedence, in order — each rung is a different mode, and a click means a different
    thing in each:
      1. _PROMPT up  → only the [kill]/[cancel] hit-boxes act. A click anywhere ELSE on
         screen is swallowed (NOT a cancel): a stray click must never resolve a kill
         prompt in either direction. This mirrors _handle_prompt_key's "any other key is
         NOT consent" (render.py:2393).
      2. my in _TOGGLE_ROWS → the existing `+N hidden`/`folded` toggle. Checked before the
         row map because a toggle row is not a selectable row; the two maps never overlap.
      3. my in _FOLD_ROWS   → F-30 (v10) card/node fold-toggle — `_ROUTE_FOLD[card_key]` flips.
         Inserted here (its own rung, between the `a`-toggle and the F-27 row map) so a fold
         click can never be misread as either — I7/§5.4 B2: `_FOLD_ROWS` is disjoint from BOTH
         `_CLICK_ROWS` and `_TOGGLE_ROWS` by construction (`_draw` builds them from disjoint
         line sets, see its row loop).
      4. my in _CLICK_ROWS  → row click:
           · same identity as _CURSOR_ID → kill REQUEST → _PROMPT = {"stage": "confirm"}
           · different row              → move selection (_CURSOR_ID = id, _SELECT_MODE = True)
      5. otherwise → click outside any row → _exit_select()  (deselect, prd.md:279)

    The kill/cancel hit-boxes do not call control.kill_target directly — they replay the
    matching keyboard keystroke through _handle_prompt_key, so the mouse and keyboard share
    the exact same one decision path (§4.1: "kill 결정 경로는 하나").
    """
    global _CURSOR_ID, _SELECT_MODE, _PROMPT
    if not _HIT_VALID:
        return True
    if _PROMPT is not None:
        for row, x0, x1, action in _PROMPT_HITS:
            if my == row and x0 <= mx < x1:
                if action == "kill":
                    _handle_prompt_key(ord("y") if _PROMPT["stage"] == "confirm" else ord("Y"))
                else:
                    _handle_prompt_key(_ESC)
                break
        return True                      # rung 1: every other click while prompted is swallowed
    if my in _TOGGLE_ROWS and _hit_at(_TOGGLE_SPANS.get(my), mx):
        set_show_all(not _SHOW_ALL)
        return True
    if my in _REPO_FOLD_ROWS and _hit_at(_REPO_FOLD_SPANS.get(my), mx):
        entry = _REPO_FOLD_ROWS[my]
        _set_repo_fold(entry["repo_key"], not entry["folded"])
        return True
    if my in _FOLD_ROWS and _hit_at(_FOLD_SPANS.get(my), mx):
        entry = _FOLD_ROWS[my]
        # Invert whatever was ACTUALLY drawn (entry["folded"] is the resolved state — default
        # OR a prior explicit choice, §5.4), never a re-guessed default — otherwise a card whose
        # default happens to be folded would need two clicks before anything visibly moves.
        _ROUTE_FOLD[entry["card_key"]] = not entry["folded"]
        return True
    if my in _CLICK_ROWS and _hit_at(_CLICK_SPANS.get(my), mx):
        entry = _CLICK_ROWS[my]
        reclick = _SELECT_MODE and _entry_id(entry) == _CURSOR_ID
        if _click_target_excluded(entry):
            return True                  # excluded rows are unreachable by click (§4.2.1)
        if reclick:
            _PROMPT = {"stage": "confirm", "entry": entry}
        else:
            _SELECT_MODE = True
            _CURSOR_ID = _entry_id(entry)
        return True
    _exit_select()
    return True


def _handle_mouse_event(mx, my, bstate):
    """Route one ncurses event; only a primary press clicks, wheel never selects."""
    global _OFFSET, _CURSOR_ID
    if mx is None or my is None or not _HIT_VALID:
        return True
    primary = getattr(curses, "BUTTON1_PRESSED", 0)
    if not primary:
        primary = getattr(curses, "BUTTON1_CLICKED", 0)
    if _PROMPT is not None:
        if primary and bstate & primary:
            _handle_mouse(mx, my)
        return True
    wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
    wheel_down = getattr(curses, "BUTTON5_PRESSED", 0)
    if wheel_up and bstate & wheel_up:
        if _CURRENT_BODY_H > 0 and _CURRENT_BODY_TOP <= my < _CURRENT_BODY_TOP + _CURRENT_BODY_H:
            _exit_select()
            _CURSOR_ID = None
            _OFFSET = _clamp_offset(_OFFSET - 1, _CURRENT_BODY_COUNT, _CURRENT_BODY_H)
        return True
    if wheel_down and bstate & wheel_down:
        if _CURRENT_BODY_H > 0 and _CURRENT_BODY_TOP <= my < _CURRENT_BODY_TOP + _CURRENT_BODY_H:
            _exit_select()
            _CURSOR_ID = None
            _OFFSET = _clamp_offset(_OFFSET + 1, _CURRENT_BODY_COUNT, _CURRENT_BODY_H)
        return True
    if primary and bstate & primary:
        _handle_mouse(mx, my)
    return True


def _drain_navigation(reader, stdscr, ch, mouse_event, body_h, timeout_ms):
    """Apply a bounded burst in order; put the first action back for next paint.

    Call only with a current painted viewport in base mode. Clamping each step
    preserves reversals at either boundary, including Home/End and page moves.
    """
    global _OFFSET
    deadline = time.monotonic() + .004
    size = stdscr.getmaxyx()
    try:
        stdscr.timeout(0)
        for _ in range(128):
            if ch == curses.KEY_MOUSE:
                _handle_mouse_event(*mouse_event)
            else:
                _handle_base_key(ch, body_h)
                _OFFSET = _clamp_offset(_OFFSET, _CURRENT_BODY_COUNT, body_h)
            if time.monotonic() >= deadline:
                break
            next_ch, next_mouse = reader.read(stdscr, timeout_ms=0)
            if next_ch == curses.ERR:
                break  # incomplete packets stay in the reader; never wait for a tail
            if (not _base_navigation(next_ch, next_mouse)
                    or stdscr.getmaxyx() != size):
                reader.ready.insert(0, (next_ch, next_mouse))
                break
            ch, mouse_event = next_ch, next_mouse
        else:
            # The final iteration already consumed its successor. Preserve it.
            reader.ready.insert(0, (ch, mouse_event))
    finally:
        stdscr.timeout(timeout_ms)


def _handle_prompt_key(ch):
    """Confirmation keys. The ONLY path in fleet that reaches control.kill_target."""
    global _PROMPT, _PENDING_KILL
    from . import control
    prompt, entry = _PROMPT, _PROMPT["entry"]
    stage = prompt["stage"]

    if ch in (_ESC, ord("n"), ord("N")):
        _PROMPT = None
        if stage == "escalate":
            _PENDING_KILL = None        # user declined SIGKILL → stop asking
        _set_action("cancelled")
        return
    if ch == -1:
        return                          # timeout tick, not a keypress — keep asking

    if stage == "confirm":
        if ch != ord("y"):
            return                      # any other key is NOT consent
        if control.requires_double_confirm(entry.get("state"), entry.get("status")):
            _PROMPT = {"stage": "confirm2", "entry": entry}   # live target → ask again
            return
        _PROMPT = None
        _do_kill(entry, "single")
        return
    if stage == "confirm2":
        # Deliberately a DIFFERENT key from stage 1: holding `y` cannot walk through both.
        if ch != ord("Y"):
            return
        _PROMPT = None
        _do_kill(entry, "double")
        return
    if stage == "escalate":
        # Capital `Y`, like confirm2 and for the same reason: SIGKILL is the most destructive
        # act here, so it must not be reachable by the same keystroke that started the SIGTERM.
        if ch != ord("Y"):
            return
        _PROMPT = None
        r = control.kill_target(entry["pid"], entry.get("proc_start"), entry.get("sid"),
                                entry.get("state"), "escalated",
                                registry_status=entry.get("status"),
                                is_worker=entry.get("is_worker", False),
                                kind=entry.get("kind", "session"))
        _PENDING_KILL = None
        _set_action("SIGKILL %s: %s" % (entry.get("label"), r))


def _do_kill(entry, approval):
    """SIGTERM + start the grace window. Never escalates on its own."""
    global _PENDING_KILL
    from . import control
    r = control.kill_target(entry["pid"], entry.get("proc_start"), entry.get("sid"),
                            entry.get("state"), approval,
                            registry_status=entry.get("status"),
                            is_worker=entry.get("is_worker", False),
                            kind=entry.get("kind", "session"))
    _set_action("SIGTERM %s: %s" % (entry.get("label"), r))
    if r == "ok":
        _PENDING_KILL = {"entry": entry, "since": time.time()}
        _close_job_row_if_registry(entry)


def _poll_pending_kill():
    """Non-blocking grace check, called once per wake so the curses loop never stalls.

    When the grace expires and the target is still alive, this does NOT escalate — it raises
    a fresh prompt. Automatic escalation is exactly what prd.md:253 forbids.
    """
    global _PENDING_KILL, _PROMPT
    from . import control
    if not _PENDING_KILL or _PROMPT is not None:
        return
    entry = _PENDING_KILL["entry"]
    if time.time() - _PENDING_KILL["since"] < control.KILL_GRACE_SEC:
        return
    if not control.verify_target(entry["pid"], entry.get("proc_start")):
        _PENDING_KILL = None            # gone (or no longer provably the same process) → done
        _set_action("%s terminated" % entry.get("label"))
        return
    _PROMPT = {"stage": "escalate", "entry": entry}
                              # "60열 footer가 이미 빡빡하다"); this one short segment gets its
                              # own (lower than the mouse hint's 100) width floor rather than
                              # sharing _MOUSE_HINT_MIN_WIDTH, which is about a DIFFERENT
                              # capability (mouse opt-in) and would tie the two together for no
                              # reason.


def _footer_segs(select_mode, parts, width=None):
    """Fit every keyboard control before optional layout/mouse/runtime detail."""
    if select_mode:
        keys = [("↑↓/jk", "move"), ("x", "kill"), ("Esc", "cancel"), ("q", "quit")]
    else:
        wlbl = "wide/narrow/stack" if _LAYOUT == "auto" else "%s!" % _LAYOUT
        keys = [("q", "quit"), ("r", "refresh"), ("a", "all"), ("c", "GPU cmd"),
                ("w", wlbl), ("p", "group" if _PROCESS_VIEW else "process"),
                ("jk", "scroll"), ("s", "select"), ("g/G", "top/end")]
    budget = max(1, width - 1) if width else 200
    def build(labels=True):
        segs = [(" ", "hdr_bar")]
        for i, (key, meaning) in enumerate(keys):
            if i:
                segs.append((" ", "hdr_bar"))
            segs.append((key, "hdr_key"))
            if labels:
                segs.append((" " + meaning, "hdr_bar"))
        return segs
    segs = build()
    if sum(_dw(t) for t, _k in segs) > budget:
        segs = build(False)
    for extra in (["click row"] if width and width >= _MOUSE_HINT_MIN_WIDTH else []) + list(parts):
        text = " · " + str(extra)
        if sum(_dw(t) for t, _k in segs) + _dw(text) <= budget:
            segs.append((text, "hdr_bar"))
    return segs


def reset_scroll():
    global _OFFSET, _RESUME_ORDER, _RELOAD_FRAME, _SHELL_TTY_MODE
    global _RELOAD_FRAME_AT, _RELOAD_FRAME_COMPUTE_AT, _RELOAD_LAYOUT
    _OFFSET = 0
    _RESUME_ORDER = _RELOAD_FRAME = None
    _RELOAD_FRAME_AT = _RELOAD_FRAME_COMPUTE_AT = None
    _RELOAD_LAYOUT = None
    _SHELL_TTY_MODE = None
    global _EXPLORER_STATE, _EXPLORER_RESULT, _EXPLORER_FRAME, _EXPLORER_SIZE
    global _EXPLORER_PUBLISHED_MODEL, _EXPLORER_REPLAY_ACTIVE, _EXPLORER_DETAIL_CONTINUATION
    global _EXPLORER_COMPOSE_MODEL, _EXPLORER_COMPOSE_SNAPSHOT
    global _EXPLORER_COMPOSE_DETAIL, _EXPLORER_COMPOSE_GENERATION
    global _EXPLORER_REMOTE_OBSERVATION
    _EXPLORER_STATE = _EXPLORER_RESULT = _EXPLORER_FRAME = _EXPLORER_SIZE = None
    _EXPLORER_PUBLISHED_MODEL = _EXPLORER_DETAIL_CONTINUATION = None
    _EXPLORER_COMPOSE_MODEL = _EXPLORER_COMPOSE_SNAPSHOT = _EXPLORER_COMPOSE_DETAIL = None
    _EXPLORER_COMPOSE_GENERATION = None
    _EXPLORER_REPLAY_ACTIVE = False
    _EXPLORER_REMOTE_OBSERVATION = None


def viewer_state(live_order):
    from .explorer.state import serialize
    layout = dict(_RELOAD_LAYOUT) if isinstance(_RELOAD_LAYOUT, dict) else {}
    frame = _RELOAD_FRAME
    if _EXPLORER_ENABLED:
        layout = {"schema_version": 3}
        frame = None
    return {"offset": _OFFSET, "process_view": _PROCESS_VIEW, "show_all": _SHOW_ALL,
            "explorer_state": serialize(_EXPLORER_STATE),
            "layout": _LAYOUT,
            "terminal_modes": _SHELL_TTY_MODE,
            "folds": list(_ROUTE_FOLD.items()), "select_mode": _SELECT_MODE,
            "repo_folds": list(_REPO_FOLD.items()),
            "cursor": _CURSOR_ID, "frame": frame,
            "frame_layout": layout,
            "frame_at": _RELOAD_FRAME_AT, "frame_compute_at": _RELOAD_FRAME_COMPUTE_AT,
            "order": {"groups": live_order.groups, "tiers": live_order.group_tiers,
                      "sessions": live_order.sessions}}


def restore_viewer_state(value):
    global _OFFSET, _PROCESS_VIEW, _SHOW_ALL, _ROUTE_FOLD, _SELECT_MODE, _CURSOR_ID
    global _RESUME_ORDER, _RELOAD_FRAME, _SHELL_TTY_MODE, _RELOAD_LAYOUT
    global _RELOAD_FRAME_AT, _RELOAD_FRAME_COMPUTE_AT
    global _LAYOUT, _REPO_FOLD, _SELECTABLE, _FOLDABLE, _REPO_FOLDABLE
    global _EXPLORER_STATE
    if _EXPLORER_ENABLED:
        _EXPLORER_STATE = _explorer_state_for(value.get("explorer_state", value), 140)
    else:
        from .explorer.state import restore
        _EXPLORER_STATE = restore(value.get("explorer_state", value))

    def tuples(item):
        return tuple(tuples(part) for part in item) if isinstance(item, list) else item

    _OFFSET = max(0, int(value.get("offset", 0)))
    _PROCESS_VIEW = bool(value.get("process_view"))
    _SHOW_ALL = bool(value.get("show_all"))
    _SHELL_TTY_MODE = value.get("terminal_modes")
    layout = value.get("layout", "auto")
    _LAYOUT = layout if layout in ("auto", "wide", "narrow", "stack") else "auto"
    _ROUTE_FOLD = {tuples(key): bool(folded) for key, folded in value.get("folds", [])}
    _REPO_FOLD = {str(key): bool(folded) for key, folded in value.get("repo_folds", [])}
    _SELECT_MODE = bool(value.get("select_mode"))
    _CURSOR_ID = tuples(value.get("cursor"))
    _RELOAD_FRAME = [[tuple(segment) for segment in row] if row else row
                     for row in value.get("frame") or []]
    layout_meta = value.get("frame_layout")
    _RELOAD_LAYOUT = dict(layout_meta) if isinstance(layout_meta, dict) else None
    if _EXPLORER_ENABLED:
        _RELOAD_FRAME = None                    # only view state is persisted for the explorer
        _RELOAD_LAYOUT = {"schema_version": 3}
    _SELECTABLE = []
    _FOLDABLE = []
    _REPO_FOLDABLE = []
    _HIT_VALID = False
    # Older handoffs have no age: preserve their UI state but rebuild their facts.
    _RELOAD_FRAME_AT = value.get("frame_at")
    if not isinstance(_RELOAD_FRAME_AT, (int, float)) or isinstance(_RELOAD_FRAME_AT, bool):
        _RELOAD_FRAME_AT = None
    _RELOAD_FRAME_COMPUTE_AT = value.get("frame_compute_at")
    order = value.get("order") or {}
    _RESUME_ORDER = _LiveOrderState()
    _RESUME_ORDER.groups = list(order.get("groups") or [])
    _RESUME_ORDER.group_tiers = dict(order.get("tiers") or {})
    _RESUME_ORDER.sessions = {key: [tuples(identity) for identity in identities]
                              for key, identities in (order.get("sessions") or {}).items()}


def _draw(stdscr, sessions, jobs, section, malformed, memory=None, live_order=None,
          resources=None, usage_snapshots=None, governor=None, loading=False, snapshot=None,
          resource_diagnostics=None):
    global _OFFSET, _TOGGLE_ROWS, _TOGGLE_SPANS, _CLICK_ROWS, _CLICK_SPANS
    global _FOLD_ROWS, _FOLD_SPANS, _REPO_FOLD_ROWS, _REPO_FOLD_SPANS
    global _PROMPT_HITS, _CURSOR_ID, _RELOAD_FRAME, _RELOAD_LAYOUT
    global _RELOAD_FRAME_AT, _RELOAD_FRAME_COMPUTE_AT, _HIT_VALID, _HIT_SIZE
    global _HIT_SNAPSHOT_ID, _CURRENT_BODY_H, _CURRENT_BODY_TOP, _CURRENT_BODY_COUNT
    global _LAYOUT_META, _EXPLORER_SHOWN
    global _EXPLORER_MODEL, _EXPLORER_PUBLISHED_MODEL, _EXPLORER_REPLAY_ACTIVE
    global _EXPLORER_DETAIL_CONTINUATION, _EXPLORER_STATE

    _TOGGLE_ROWS, _TOGGLE_SPANS = {}, {}
    _CLICK_ROWS, _CLICK_SPANS = {}, {}
    _FOLD_ROWS, _FOLD_SPANS = {}, {}
    _REPO_FOLD_ROWS, _REPO_FOLD_SPANS = {}, {}
    _PROMPT_HITS = []
    _HIT_VALID = False
    h, w = stdscr.getmaxyx()
    if h <= 0 or w <= 1:
        return
    if _EXPLORER_ENABLED:
        current = snapshot or LiveSnapshot(sessions=sessions, jobs=jobs,
                                           usage_snapshots=usage_snapshots or {})
        state = _explorer_state_for(_EXPLORER_STATE, w)
        _EXPLORER_STATE = state
        runtime = _explorer_runtime()
        # Generation zero has no live rows to compose. Paint a persisted VM (or
        # the loading frame) directly; later generations still compose first so
        # their readiness evidence is current before a publication decision.
        if loading and _EXPLORER_PUBLISHED_MODEL is not None:
            from .explorer.live import current_claude_usage
            _EXPLORER_MODEL = _explorer_native_display(
                current_claude_usage(_EXPLORER_PUBLISHED_MODEL, current, time.time()))
            frame = _explorer_frame_from_model(_EXPLORER_MODEL, state, w, h, beat=1)
            _EXPLORER_SHOWN = True
        elif loading:
            frame = runtime.loading_frame(w, h)
        else:
            # Always advance the complete base, even while selected local data is available.
            _explorer_compose(current, w, h, state, _EXPLORER_RESULT, draw_frame=False)
            candidate_model = _EXPLORER_MODEL
            readiness = _EXPLORER_READINESS
            complete = runtime.publication_ready(readiness)
            if complete:
                _EXPLORER_REPLAY_ACTIVE = False
                _EXPLORER_PUBLISHED_MODEL = candidate_model
                _EXPLORER_MODEL = _explorer_native_display(candidate_model)
                _EXPLORER_SHOWN = True
                runtime.mark_published(readiness)
                frame = runtime.draw_frame(_EXPLORER_MODEL, state, w, h, beat=int(_BLINK_ON))
                if runtime.all_new(readiness):
                    runtime.persist_complete_async(candidate_model, readiness,
                                                   _EXPLORER_DETAIL_CONTINUATION)
            elif _EXPLORER_PUBLISHED_MODEL is not None:
                from .explorer.live import selected_detail, current_claude_usage
                base = _EXPLORER_PUBLISHED_MODEL
                # Replay lacks exact live bindings; it remains responsive without guessing them.
                shown = selected_detail(base, _EXPLORER_RESULT, peer_model=candidate_model)
                shown = _explorer_native_display(current_claude_usage(shown, current, time.time()))
                _EXPLORER_MODEL = shown
                from .explorer.chrome import _clock_freshness
                clock = time.time()
                live_model = (shown if _EXPLORER_REPLAY_ACTIVE
                              or _clock_freshness(shown.clock) == _clock_freshness(clock)
                              else shown.with_updates(clock=clock))
                frame = _explorer_frame_from_model(live_model, state, w, h,
                                                   beat=1 if _EXPLORER_REPLAY_ACTIVE else int(_BLINK_ON))
                _EXPLORER_SHOWN = True
            else:
                _EXPLORER_MODEL = None
                frame = runtime.loading_frame(w, h)
        _draw_explorer(stdscr, current, frame=frame)
        return
    stdscr.erase()
    now = time.monotonic()
    replay_fresh = (_RELOAD_FRAME_AT is not None
                    and 0 <= now - _RELOAD_FRAME_AT <= 3 * _COMPUTE_HOST_INTERVAL)
    same_gpu_sample = (_COMPUTE_HOSTS_SET_AT is None
                       or _COMPUTE_HOSTS_SET_AT == _RELOAD_FRAME_COMPUTE_AT)
    frame_layout = _RELOAD_LAYOUT if isinstance(_RELOAD_LAYOUT, dict) else {}
    replay_geometry_ok = (frame_layout.get("schema_version") == 1
                          and frame_layout.get("width") == w
                          and frame_layout.get("process_view") is bool(_PROCESS_VIEW)
                          and frame_layout.get("layout") == _layout_mode(w))
    cached = bool(loading and _RELOAD_FRAME and replay_fresh and same_gpu_sample
                  and replay_geometry_ok)
    if cached:
        lines = list(_RELOAD_FRAME)
        # Cached terminal segments have no quota admission either. Replace only
        # the Claude usage row, preserving the cached body/geometry and hit policy.
        priorities = frame_layout.get("fixed_priorities") or {}
        for index, row in enumerate(lines):
            if (priorities.get(index, priorities.get(str(index))) == 1 and row
                    and any(key == "hb_claude" for _text, key in row)):
                snapshots = usage_snapshots if usage_snapshots is not None else getattr(snapshot, "usage_snapshots", {})
                fresh_row = _usage_header_rows([], now=time.time(),
                    usage_snapshots={"claude": (snapshots or {}).get("claude")}, term_width=w)[0]
                lines[index] = row[:1] + fresh_row[1:]
        _LAYOUT_META = {
            "fixed_end": frame_layout.get("fixed_end", 0),
            "body_start": frame_layout.get("body_start", 0),
            "body_end": frame_layout.get("body_end", len(lines)),
            "legend_start": frame_layout.get("legend_start", len(lines)),
            "fixed_priorities": {int(k): v for k, v in
                                 (frame_layout.get("fixed_priorities") or {}).items()},
        }
    else:
        lines = _build_lines(sessions, jobs, section, w < _NARROW_CUTOFF, malformed,
                             layout=_layout_mode(w), memory=memory, term_width=w,
                             live_order=live_order, resources=resources,
                             usage_snapshots=usage_snapshots, governor=governor,
                             loading=loading,
                             node_evidence=snapshot.node_evidence if snapshot else None,
                             route_entities=snapshot.route_entities if snapshot else None,
                             observations=snapshot.observations if snapshot else None,
                             resource_diagnostics=(snapshot.resource_diagnostics if snapshot
                                                   else resource_diagnostics),
                             pending_delivery=snapshot.pending_delivery if snapshot else None,
                             display_snapshot=getattr(snapshot, "display_snapshot", None))
        _RELOAD_FRAME = lines
        _RELOAD_FRAME_AT = min(now, _COMPUTE_HOSTS_SET_AT) if _COMPUTE_HOSTS_SET_AT is not None else now
        _RELOAD_FRAME_COMPUTE_AT = _COMPUTE_HOSTS_SET_AT
        _RELOAD_LAYOUT = dict(_LAYOUT_META, schema_version=1, width=w,
                              process_view=bool(_PROCESS_VIEW), layout=_layout_mode(w))

    meta = _LAYOUT_META
    fixed_end = max(0, min(len(lines), int(meta.get("fixed_end", 0))))
    body_start = max(fixed_end, min(len(lines), int(meta.get("body_start", fixed_end))))
    body_end = max(body_start, min(len(lines), int(meta.get("body_end", len(lines)))))
    legend_start = max(body_end, min(len(lines), int(meta.get("legend_start", len(lines)))))
    body_count = body_end - body_start
    priorities = meta.get("fixed_priorities") or {}
    min_body = 1 if h >= 3 and body_count else 0
    fixed_capacity = max(0, h - 1 - min_body)
    if fixed_end <= fixed_capacity:
        fixed_indices = list(range(fixed_end))
    else:
        chosen = sorted(range(fixed_end), key=lambda i: (priorities.get(i, 9), i))[:fixed_capacity]
        fixed_indices = sorted(chosen)
    remaining = max(0, h - 1 - len(fixed_indices))
    body_h = min(body_count, remaining)
    legend_count = len(lines) - legend_start
    legend_h = min(legend_count, max(0, remaining - body_h))

    cur_line = None
    targets = _live_targets() if _SELECT_MODE else []
    if _SELECT_MODE and targets:
        i = _cursor_index(targets)
        if i is None:
            i = 0
            _CURSOR_ID = _entry_id(targets[0])
        cur_line = targets[i]["line"]
        cursor_body = cur_line - body_start
        if body_h > 0:
            if cursor_body < _OFFSET:
                _OFFSET = cursor_body
            elif cursor_body >= _OFFSET + body_h:
                _OFFSET = cursor_body - body_h + 1
    _OFFSET = _clamp_offset(_OFFSET, body_count, body_h)

    sel_by_line = {e["line"]: e for e in _SELECTABLE}
    fold_by_line = {e["line"]: e for e in _FOLDABLE}
    repo_by_line = {e["line"]: e for e in _REPO_FOLDABLE}
    logical_rows = [(i, lines[i]) for i in fixed_indices]
    body_first_y = len(logical_rows)
    logical_rows.extend((i, lines[i]) for i in range(body_start + _OFFSET,
                                                      body_start + _OFFSET + body_h))
    logical_rows.extend((i, lines[i]) for i in range(legend_start, legend_start + legend_h))
    logical_to_screen = {}

    for row, (line_index, segs) in enumerate(logical_rows):
        logical_to_screen[line_index] = row
        painted = _addline(stdscr, row, segs, w)
        if cached:
            continue
        spans = _painted_hit_spans(painted)
        if not spans:
            continue
        if line_index in repo_by_line:
            _REPO_FOLD_ROWS[row] = repo_by_line[line_index]
            _REPO_FOLD_SPANS[row] = spans
        elif line_index in fold_by_line:
            _FOLD_ROWS[row] = fold_by_line[line_index]
            _FOLD_SPANS[row] = spans
        elif segs is not None and len(segs) == 1 and (
                "hidden" in segs[0][0] or "folded" in segs[0][0]):
            _TOGGLE_ROWS[row] = True
            _TOGGLE_SPANS[row] = spans
        elif line_index in sel_by_line:
            _CLICK_ROWS[row] = sel_by_line[line_index]
            _CLICK_SPANS[row] = spans

    if cur_line is not None and cur_line in logical_to_screen:
        _highlight_row(stdscr, logical_to_screen[cur_line], w)

    above = _OFFSET
    below = max(0, body_count - body_h - _OFFSET)
    parts = []
    if above:
        parts.append("↑%d" % above)
    if below:
        parts.append("↓%d" % below)
    if _REPO_FOLD_ROWS and not _PROCESS_VIEW:
        parts.append("Space fold")
    fsegs = _prompt_segs(_PROMPT, w) if _PROMPT else _footer_segs(_SELECT_MODE, parts, w)
    footer_paint = _addline(stdscr, h - 1, fsegs, w)
    footer_spans = _painted_hit_spans(footer_paint)
    if _PROMPT and not cached:
        _PROMPT_HITS = [hit for hit in _prompt_hit_boxes(fsegs, h - 1, w)
                        if any(x0 <= hit[1] and hit[2] <= x1 for x0, x1 in footer_spans)]
    stdscr.noutrefresh()
    curses.doupdate()
    _CURRENT_BODY_H = body_h
    _CURRENT_BODY_TOP = body_first_y
    _CURRENT_BODY_COUNT = body_count
    _HIT_SIZE = (h, w)
    _HIT_SNAPSHOT_ID = id(snapshot) if snapshot is not None else None
    _HIT_VALID = not cached


def _highlight_row(stdscr, y, w):
    """Reverse-video the cursor row. Painted over the already-drawn line so the row keeps its
    own colors and nothing about row assembly has to know about selection."""
    try:
        stdscr.chgat(y, 0, w, curses.A_REVERSE)
    except Exception:
        pass


def _configure_input(curses_mod, env):
    """Set up terminal input so nothing the terminal sends can hold the frame loop.

    Split out of `_loop` only so it can be tested without a real screen — the freeze this
    prevents is not reproducible in a unit test, so the settings themselves are what gets
    asserted.

    **Escape delay.** ncurses waits its 1000ms default for the rest of a sequence it has
    only partly seen: ten dropped frames for one stray ESC, and the floor under the freeze
    below.

    **Mouse reporting is enabled by default**; explicitly set non-`1` values keep opt-out.
    TUI in two different terminals — under herdr (2026-07-01) and in a VS Code terminal
    (2026-09-09). The second was measured while stuck: 0 CPU, zero read and write syscalls,
    one thread, sleeping on the tty, and the terminal's own output queue empty, so it was
    waiting for input rather than blocked writing. The user reported that it freezes on
    clicking away to another window and that any keypress advances it exactly one frame,
    which is what a half-assembled mouse/focus escape sequence does: ncurses holds for bytes
    that never arrive and the next keystroke terminates the parse.

    Earlier freeze reports came from incomplete mouse/focus sequences. The current default
    keeps press-only mouse events. Both mouse modes bypass ncurses' blocking keypad
    sequence decoder and assemble keys and SGR packets incrementally; opt-out consumes
    mouse packets without dispatching them. PTY evidence records tested terminal limits.
    """
    if hasattr(curses_mod, "set_escdelay"):
        try:
            curses_mod.set_escdelay(25)
        except Exception:
            pass
    if "FLEET_MOUSE" not in env or env.get("FLEET_MOUSE") == "1":
        button1_pressed = getattr(curses_mod, "BUTTON1_PRESSED", 0)
        button1 = button1_pressed or getattr(curses_mod, "BUTTON1_CLICKED", 0)
        mask = button1 | getattr(curses_mod, "BUTTON4_PRESSED", 0)
        mask |= getattr(curses_mod, "BUTTON5_PRESSED", 0)
        # A zero interval disables ncurses click synthesis. That's appropriate
        # when listening to press events (the normal path), but would suppress
        # the legacy click-only fallback on older curses builds.
        if button1_pressed and hasattr(curses_mod, "mouseinterval"):
            try:
                curses_mod.mouseinterval(0)
            except Exception:
                pass
        if mask:
            try:
                curses_mod.mousemask(mask)
            except Exception:
                pass


_EXPLORER_TEXT = bytearray()   # a partly received UTF-8 character typed into the / search


def _explorer_searching():
    return bool(_EXPLORER_ENABLED and getattr(_EXPLORER_STATE, "searching", False))


def _explorer_text_key(ch):
    """'text:<c>' for a byte typed into the search; multi-byte characters arrive one byte at a time."""
    global _EXPLORER_TEXT
    if isinstance(ch, int) and 32 <= ch < 127:
        _EXPLORER_TEXT = bytearray()
        return "text:" + chr(ch)
    if isinstance(ch, int) and 128 <= ch < 256:
        _EXPLORER_TEXT.append(ch)
        try:
            text = bytes(_EXPLORER_TEXT).decode("utf-8")
        except UnicodeDecodeError:
            if len(_EXPLORER_TEXT) >= 4:
                _EXPLORER_TEXT = bytearray()
            return None
        _EXPLORER_TEXT = bytearray()
        return "text:" + text
    return None


def _explorer_question_refs():
    from .explorer.state import older_waits_shown
    questions = getattr(_EXPLORER_MODEL, "questions", ()) if _EXPLORER_MODEL is not None else ()
    older_open = older_waits_shown(_EXPLORER_STATE)
    return tuple(dict(item.get("ref") or {}) for item in questions
                 if isinstance(item, Mapping) and isinstance(item.get("ref"), Mapping)
                 and (not item.get("older") or older_open))


def _explorer_question_editing():
    overlay = getattr(_EXPLORER_STATE, "question_overlay", None)
    return bool(getattr(_EXPLORER_STATE, "view", None) == "QUESTIONS"
                and isinstance(overlay, Mapping) and overlay.get("editing"))


def _explorer_text_input():
    """Typed characters are text, not commands: a question is being edited or the MODELS list filtered."""
    from .explorer.state import models_finding
    return _explorer_question_editing() or models_finding(_EXPLORER_STATE)


def _explorer_unmoved_tree_release(bstate, state):
    """The release of an unmoved tree press is adopted even after a refresh aged the hit map.

    The reducer confirms it from the identity the press grabbed (its own cell or a hit of that
    identity), so a snapshot tick between press and release cannot drop the click.
    """
    release = getattr(curses, "BUTTON1_RELEASED", 0)
    other = (getattr(curses, "BUTTON4_PRESSED", 0) | getattr(curses, "BUTTON5_PRESSED", 0)
             | _EXPLORER_MOUSE_MOTION)
    drag = tuple(getattr(state, "drag", ()) or ())
    return bool(release and bstate & release and not bstate & other
                and len(drag) > 8 and drag[0] == "tree" and not drag[8])


def _explorer_handle(ch, mouse_event=None, size=None, snapshot=None):
    """Apply one decoded event to the explorer's view-local reducer."""
    global _EXPLORER_STATE, _EXPLORER_TEXT
    from .explorer.state import reduce
    state = _explorer_state_for(_EXPLORER_STATE, (size or (0, 100))[1])
    if ch == curses.KEY_MOUSE:
        mx, my, bstate = mouse_event if mouse_event is not None else _getmouse_event()
        if mx is not None and size is not None and (
                _explorer_click_current(size, snapshot)
                or _explorer_unmoved_tree_release(bstate, state)):
            if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
                events = [{"type": "wheel", "direction": 1, "x": mx, "y": my}]
            elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                events = [{"type": "wheel", "direction": -1, "x": mx, "y": my}]
            elif bstate & _EXPLORER_MOUSE_MOTION:
                events = [{"type": "mouse", "phase": "press", "button": 0,
                           "motion": True, "x": mx, "y": my}]
            elif bstate & (getattr(curses, "BUTTON1_RELEASED", 0)):
                events = [{"type": "mouse", "phase": "release", "button": 0,
                           "x": mx, "y": my}]
            elif bstate & (getattr(curses, "BUTTON1_PRESSED", 0) |
                           getattr(curses, "BUTTON1_CLICKED", 0)):
                click = getattr(curses, "BUTTON1_CLICKED", 0)
                pressed = getattr(curses, "BUTTON1_PRESSED", 0)
                events = [{"type": "mouse", "phase": "press", "button": 0,
                           "x": mx, "y": my}]
                # Some curses implementations report a complete click without
                # separate press/release events. Tree rows start a drag on press,
                # so close that same hit on release before another paint can age it.
                if click and bstate & click and not (pressed and bstate & pressed):
                    events.append({"type": "mouse", "phase": "release", "button": 0,
                                   "x": mx, "y": my})
            else:
                return
            from .explorer.runtime import tag_cycle
            order = tag_cycle(getattr(_EXPLORER_MODEL, "ordering", None))
            ordering = getattr(_EXPLORER_MODEL, "ordering", None) or {}
            _effects = []
            for event in events:
                state, effects = reduce(
                    state, {**event, "order": order, "ordering": ordering}, _EXPLORER_FRAME)
                if (event.get("phase") == "press" and not event.get("motion")
                        and state.drag[:2] == ("tree", "session")):
                    from dataclasses import replace
                    state = replace(state, drag=(*state.drag,
                        _explorer_gesture_binding(_EXPLORER_MODEL, state.drag[2])))
                _effects.extend(effects)
            _EXPLORER_STATE = state
            _explorer_apply_effects(_effects, _EXPLORER_MODEL, snapshot=snapshot)
        return
    from .explorer.state import models_finding
    editing = (state.view == "QUESTIONS" and _explorer_question_editing()) or models_finding(state)
    if editing and isinstance(ch, int) and (32 <= ch < 127 or 128 <= ch < 256):
        key = _explorer_text_key(ch)
    else:
        key = _explorer_key_name(ch)
    if key:
        from .explorer.runtime import tag_cycle
        order = tag_cycle(getattr(_EXPLORER_MODEL, "ordering", None))
        event = {"type": "key", "key": key, "order": order,
                 "question_refs": _explorer_question_refs()}
        if key == "e" and state.view == "QUESTIONS":
            _EXPLORER_TEXT = bytearray()
        if key == "end" and _EXPLORER_MODEL is not None and size is not None:
            from .explorer.frame import measure_pane_max
            event["max_scroll"] = measure_pane_max(
                _EXPLORER_MODEL, state, int(size[1]), int(size[0]))
        _EXPLORER_STATE, _effects = reduce(state, event, _EXPLORER_FRAME)
        _explorer_apply_effects(_effects, _EXPLORER_MODEL, snapshot=snapshot)


def _explorer_note_earlier(before, key):
    """Scrolling up in the raw-output pane while it already shows its first row asks for older records."""
    global _EXPLORER_EARLIER
    if before.active_region != "output" or key not in ("up", "k", "pageup", "home"):
        return
    slot = f"output:{before.tab}"
    shown = dict(before.scrolls).get(slot)
    limit = dict(getattr(_EXPLORER_FRAME, "scroll_max", ()) or ()).get(slot)
    if shown is None:
        shown = limit
    if shown is not None and int(shown) == 0 and getattr(_EXPLORER_RESULT, "output_more", False):
        _EXPLORER_EARLIER = True


def _explorer_detail_callback(collector, *, optional=False):
    """Choose the required fast pass before the first complete screen."""
    if not optional:
        callback = getattr(collector, "required_detail_refresh", None)
        if callable(callback):
            return callback, True
    callback = getattr(collector, "detail_refresh", None)
    return (callback, False) if callable(callback) else (None, False)


def _explorer_optional_detail_key(identity, state):
    """One optional pass per base generation, plus selected MEMORY scope."""
    if identity is None:
        return None
    state = _explorer_state_for(state, 0)
    view = state.view if state.view == "MEMORY" else None
    scope = state.scope if view is not None else "-"
    return identity, view, scope


def _explorer_resume_continuation(collector, source, payload, details):
    """Resume valid rows and refresh misses with the full current snapshot as context."""
    restore = getattr(details, "restore_continuation", None)
    merge = getattr(details, "merge_continuation", None)
    if not callable(restore) or not callable(merge):
        return None
    continued = merge(source, restore(payload))
    if isinstance(continued, tuple) and continued:
        continued = continued[0]
    if not hasattr(continued, "snapshot"):
        return None
    if getattr(continued, "continuation_complete", False):
        return continued
    if not getattr(continued, "continued_count", 0):
        return None
    # The callback retains neighbourhood and association evidence from all rows;
    # continuation markers limit required-detail writes to the unvalidated rows.
    callback, _required_only = _explorer_detail_callback(collector, optional=False)
    if callable(callback):
        try:
            refreshed = callback(continued.snapshot)
        except Exception:
            refreshed = None
        if refreshed is not None:
            return refreshed
    return continued


def _loop(stdscr, collect_all, hfilter, section, interval):
    global _OFFSET, _BLINK_ON, _RESUME_ORDER, _RELOAD_FRAME, _HIT_VALID, _EXPLORER_RESULT, _EXPLORER_EARLIER
    global _EXPLORER_GENERATION, _EXPLORER_MODEL, _EXPLORER_PUBLISHED_MODEL
    global _EXPLORER_REPLAY_ACTIVE, _EXPLORER_SHOWN, _EXPLORER_DETAIL_CONTINUATION
    global _EXPLORER_READINESS, _EXPLORER_REMOTE_OBSERVATION
    global _EXPLORER_QUESTION_QUEUE, _EXPLORER_QUESTION_PUMP, _EXPLORER_QUESTION_GENERATION
    global _EXPLORER_NATIVE_DISPLAY
    curses.curs_set(0)
    _init_colors()
    if _EXPLORER_ENABLED:
        global _EXPLORER_COMPOSE_MODEL, _EXPLORER_COMPOSE_SNAPSHOT
        global _EXPLORER_COMPOSE_DETAIL, _EXPLORER_COMPOSE_GENERATION
        _EXPLORER_COMPOSE_MODEL = _EXPLORER_COMPOSE_SNAPSHOT = _EXPLORER_COMPOSE_DETAIL = None
        _EXPLORER_COMPOSE_GENERATION = None
        _EXPLORER_REMOTE_OBSERVATION = None
        runtime = _explorer_runtime()
        runtime.configure_persistence(hfilter, strict_readiness=not bool(os.environ.get("FLEET_DEMO")))
        _EXPLORER_GENERATION = 0
        _EXPLORER_SHOWN = False
        _EXPLORER_PUBLISHED_MODEL = runtime.load_last_complete(hfilter)
        _EXPLORER_MODEL = _EXPLORER_PUBLISHED_MODEL
        _EXPLORER_REPLAY_ACTIVE = _EXPLORER_PUBLISHED_MODEL is not None
        _EXPLORER_DETAIL_CONTINUATION = runtime.cached_continuation
        _EXPLORER_READINESS = {"ready": False, "fully_fresh": False,
                               "all_new": False, "pending": ("basic",)}
    live_order = _RESUME_ORDER or _LiveOrderState()
    _RESUME_ORDER = None
    mouse_enabled = "FLEET_MOUSE" not in os.environ or os.environ.get("FLEET_MOUSE") == "1"
    _configure_input(curses, os.environ)
    # Keep curses' raw byte reads in both mouse modes. The incremental reader
    # consumes malformed SGR packets safely even when mouse actions are opted out.
    keypad = getattr(stdscr, "keypad", None)
    if callable(keypad):
        keypad(False)
    input_reader = _CursesInput(curses, mouse_enabled=mouse_enabled,
                                explorer_mode=_EXPLORER_ENABLED)

    from . import details
    latest_basic = [None]
    optional_detail_source = [None]
    optional_detail_identity = [None]
    detail_generation = 0

    def collect_snapshot():
        options = {"harness_filter": hfilter}
        if _EXPLORER_ENABLED:
            runtime = _explorer_runtime()
            options.update(runtime.collection_options(first_complete=not runtime.has_complete))
        else:
            options["fast_first"] = True
        snapshot = as_snapshot(collect_all(**options))
        if _EXPLORER_ENABLED:
            snapshot = _explorer_attach_remote_observation(
                snapshot, _EXPLORER_REMOTE_OBSERVATION)
        snapshot.hearting = dict(_HEARTING) if isinstance(_HEARTING, dict) else None
        # Publication makes these rows immutable to the detail worker. Every
        # basic tick stays cheap even while that worker is delayed on NAS.
        latest_basic[0] = snapshot
        return snapshot

    def collect_details():
        optional_source = optional_detail_source[0]
        optional_detail_source[0] = None
        optional_pass = optional_source is not None
        source = optional_source if optional_pass else latest_basic[0]
        if source is None:
            return (None, None) if _EXPLORER_ENABLED else None
        detail_refresh = getattr(collect_all, "detail_refresh", None)
        result = None
        continuation = None
        required_only_pass = False
        if _EXPLORER_ENABLED:
            runtime = _explorer_runtime()
            if not optional_pass:
                payload = runtime.clear_cached_continuation()
                restore_continuation = getattr(details, "restore_continuation", None)
                merge_continuation = getattr(details, "merge_continuation", None)
                if payload is not None and callable(restore_continuation) and callable(merge_continuation):
                    try:
                        resumed = _explorer_resume_continuation(
                            collect_all, source, payload, details)
                        if resumed is not None:
                            result = resumed
                            required_only_pass = True
                    except Exception:
                        result = None
            if result is None:
                callback, required_only_pass = _explorer_detail_callback(
                    collect_all, optional=optional_pass)
                if callable(callback):
                    result = callback(source)
        else:
            if callable(detail_refresh):
                result = detail_refresh(source)
        if result is not None:
            value = result.snapshot if hasattr(result, "snapshot") else source
        elif callable(detail_refresh):
            value = source
            required_only_pass = False
            if optional_pass:
                result = details.DetailSnapshot(details.state_key(source), value)
        else:
            import copy
            value = copy.deepcopy(source)
            result = details.DetailSnapshot(details.state_key(source), value)
        if not required_only_pass:
            governor = _collect_governor()
            gitinfo.enrich_entities(value.sessions + value.jobs, schedule_ahead=True)
            value.memory, memory_state = _collect_memory_observed(include_backlog=_EXPLORER_ENABLED)
            value.governor = governor
            value.project_status = _collect_project_status(value.sessions)
            observations = dict(getattr(value, "observations", None) or {})
            observations["optional:memory"] = {"state": memory_state,
                                                "observed_at": time.time()}
            database = value.memory.get("database") if isinstance(value.memory, Mapping) else None
            database_state = (database.get("state") if isinstance(database, Mapping)
                              else "unobserved" if not _EXPLORER_ENABLED else memory_state)
            observations["optional:memory-db"] = {"state": database_state,
                                                   "observed_at": time.time()}
            value.observations = observations
            try:
                from .display_snapshot import extend
                value.display_snapshot = extend(value.display_snapshot, memory=value.memory)
            except Exception:
                pass
            hearting_refresh = getattr(collect_all, "hearting_refresh", None)
            if callable(hearting_refresh):
                try:
                    value.hearting = hearting_refresh()
                except Exception:
                    pass
        if _EXPLORER_ENABLED:
            export_continuation = getattr(details, "export_continuation", None)
            if not optional_pass and callable(export_continuation):
                try:
                    continuation = export_continuation(result)
                except Exception:
                    continuation = None
            return result, continuation
        return result

    # Draw a loading frame until the first complete snapshot is adopted. Empty
    # sessions here are unobserved, not a successful census with zero sessions.
    snapshot = LiveSnapshot(hearting=dict(_HEARTING) if isinstance(_HEARTING, dict) else None)
    generation = 0
    pump = RefreshPump(collect_snapshot, interval, adaptive_cadence=_EXPLORER_ENABLED)
    follower = getattr(collect_all, "install_follower", None)
    install_pump = RefreshPump(follower.target, interval, name="fleet-install-refresh") if follower else None
    install_generation, install_target = 0, None
    # Existing coalescing/last-good semantics, independent of basic refresh.
    detail_pump = RefreshPump(collect_details, interval, name="fleet-detail-refresh")
    if _EXPLORER_ENABLED:
        _EXPLORER_QUESTION_QUEUE = deque()
        _EXPLORER_QUESTION_PUMP = RefreshPump(
            _explorer_question_worker, 0.1, name="fleet-question-interaction")
        _EXPLORER_QUESTION_GENERATION = 0
    else:
        _EXPLORER_QUESTION_QUEUE = _EXPLORER_QUESTION_PUMP = None
        _EXPLORER_QUESTION_GENERATION = 0
    basic_snapshot = snapshot
    latest_detail = None
    compute_host_refresh = getattr(collect_all, "compute_hosts_refresh", None)
    compute_host_generation = 0
    compute_host_pump = None
    if callable(compute_host_refresh):
        compute_host_interval = os.environ.get("FLEET_COMPUTE_HOST_INTERVAL",
                                               str(_COMPUTE_HOST_INTERVAL))
        try:
            compute_host_interval = max(1.0, float(compute_host_interval))
        except (TypeError, ValueError):
            compute_host_interval = _COMPUTE_HOST_INTERVAL
        compute_host_pump = RefreshPump(
            compute_host_refresh, compute_host_interval, name="fleet-gpu-refresh")
    latest_view = [snapshot]
    display_pump, display_generation, display_events = None, 0, None
    if _EXPLORER_ENABLED:
        from .native_refresh import NativeDisplayReader, NativeDisplayLayer
        from .native_events import NativeDisplayEvents
        display_reader = NativeDisplayReader()
        display_events = NativeDisplayEvents()
        _EXPLORER_NATIVE_DISPLAY = NativeDisplayLayer()
        def collect_native_display():
            display_reader.invalidate()
            value = display_reader.read(latest_view[0])
            display_events.configure(*display_reader.event_sources())
            return value
        # No periodic request_due: kernel notifications and adopted snapshots
        # alone request this bounded reader. Normal census cadence stays intact.
        display_pump = RefreshPump(collect_native_display, interval,
                                   name="fleet-native-display", adaptive_cadence=True)
    else:
        _EXPLORER_NATIVE_DISPLAY = None
    selection_feed = None
    sessions, jobs = snapshot.sessions, snapshot.jobs
    resources = snapshot.resources
    usage_snapshots = snapshot.usage_snapshots
    malformed = snapshot.malformed
    mem_snapshot = snapshot.memory
    governor_snapshot = snapshot.governor
    set_refresh_health(
        snapshot=pump.health(time.monotonic()),
        details=detail_pump.health(time.monotonic()),
        compute_hosts=(compute_host_pump.health(time.monotonic())
                       if compute_host_pump is not None else None))

    background_started = False

    def start_background_pumps():
        nonlocal background_started
        if background_started:
            return
        for background in (pump, install_pump, compute_host_pump):
            if (background is not None and background.generation == 0
                    and not background.running):
                background.start()
        background_started = True

    def ensure_selection_feed():
        nonlocal selection_feed
        if _EXPLORER_ENABLED and selection_feed is None:
            from .explorer.focus import FocusReader
            selection_feed = _ExplorerFeed(
                FocusReader(live_jobs=lambda: list(getattr(latest_view[0], "jobs", ()) or ()),
                            snapshot=lambda: latest_view[0],
                            output_reader=_explorer_output_reader()), interval)

    stdscr.timeout(200)                     # getch blocks ≤200ms → responsive keys
    _draw(stdscr, sessions, jobs, section, malformed, memory=mem_snapshot,
          live_order=live_order, resources=resources, usage_snapshots=usage_snapshots,
          governor=governor_snapshot, loading=(generation == 0), snapshot=snapshot)
    # The first input read gets an uncontended opportunity after a cached paint.
    # With no input, the normal 100 ms wake starts background collection below.
    if not _EXPLORER_ENABLED:
        start_background_pumps()

    def queue_optional_details():
        if not _EXPLORER_ENABLED or optional_detail_source[0] is not None:
            return
        if not (callable(getattr(collect_all, "required_detail_refresh", None))
                and callable(getattr(collect_all, "detail_refresh", None))):
            return
        runtime = _explorer_runtime()
        readiness = _EXPLORER_READINESS
        if not runtime.publication_ready(readiness):
            return
        identity = readiness.get("generation_identity") if isinstance(readiness, Mapping) else None
        request_key = _explorer_optional_detail_key(identity, _EXPLORER_STATE)
        source = latest_view[0]
        if request_key is None or request_key == optional_detail_identity[0] or source is None:
            return
        observations = getattr(source, "observations", None)
        basic = observations.get("basic") if isinstance(observations, Mapping) else None
        detail = observations.get("required-details") if isinstance(observations, Mapping) else None
        if (not isinstance(basic, Mapping) or not isinstance(detail, Mapping)
                or basic.get("generation_identity", basic.get("identity")) != identity
                or detail.get("generation_identity", detail.get("identity")) != identity):
            return
        optional_detail_identity[0] = request_key
        optional_detail_source[0] = source
        detail_pump.request(force=True)

    if _EXPLORER_ENABLED:
        try:
            os.write(sys.stdout.fileno(), b"\x1b[?1002h\x1b[?1006h")
        except (OSError, AttributeError):
            pass
    try:
        while True:
            # Wake at the next 0.1s spinner frame while staying key-responsive. Collection
            # is independent, so a slow ps/dispatch/git call cannot pause this cadence.
            _nb = (int(time.time() * 10) + 1) / 10.0
            timeout_ms = max(20, min(100, int((_nb - time.time()) * 1000) + 1))
            stdscr.timeout(timeout_ms)
            if input_reader is not None:
                ch, mouse_event = input_reader.read(stdscr, timeout_ms=timeout_ms)
            else:
                ch, mouse_event = stdscr.getch(), None
            now = time.time()
            now_mono = time.monotonic()
            if background_started:
                pump.request_due(now=now_mono)
                if (display_pump is not None and generation > 0
                        and display_events.take_changed()):
                    display_pump.request(force=True)
            if install_pump is not None and background_started:
                install_pump.request_due(now=now_mono)
                install_update = install_pump.poll(install_generation)
                if install_update is not None:
                    install_generation, install_target = install_update
            if latest_basic[0] is not None and not _EXPLORER_ENABLED:
                detail_pump.request_due(now=now_mono)
            if compute_host_pump is not None and background_started:
                compute_host_pump.request_due(now=now_mono)
            set_refresh_health(
                snapshot=pump.health(now_mono),
                details=detail_pump.health(now_mono),
                compute_hosts=(compute_host_pump.health(now_mono)
                               if compute_host_pump is not None else None))
            update = pump.poll(generation)
            if update is not None:
                generation, basic_snapshot = update
                if _EXPLORER_ENABLED:
                    detail_pump.request(force=True)
            detail_update = detail_pump.poll(detail_generation)
            if detail_update is not None:
                detail_generation, next_detail = detail_update
                if (_EXPLORER_ENABLED and isinstance(next_detail, tuple)
                        and len(next_detail) == 2):
                    next_detail, _EXPLORER_DETAIL_CONTINUATION = next_detail
                if (_EXPLORER_ENABLED and latest_detail is not None and next_detail is not None):
                    next_detail = details.retain_optional_last_good(latest_detail, next_detail)
                latest_detail = next_detail
            _explorer_question_pump_tick()
            _EXPLORER_GENERATION = generation
            if update is not None or detail_update is not None:
                snapshot = details.merge(basic_snapshot, latest_detail)
                latest_view[0] = snapshot
                if display_pump is not None and generation > 0:
                    display_pump.request(force=True)
                _HIT_VALID = False
                sessions, jobs = snapshot.sessions, snapshot.jobs
                resources = snapshot.resources
                usage_snapshots = snapshot.usage_snapshots
                malformed = snapshot.malformed
                mem_snapshot = snapshot.memory
                governor_snapshot = snapshot.governor
                if snapshot.hearting is not None:
                    set_hearting(snapshot.hearting)
            if display_pump is not None:
                display_update = display_pump.poll(display_generation)
                if display_update is not None:
                    display_generation, display_values = display_update
                    if _EXPLORER_NATIVE_DISPLAY.update(display_values):
                        _HIT_VALID = False
            if compute_host_pump is not None:
                compute_host_update = compute_host_pump.poll(compute_host_generation)
                if compute_host_update is not None:
                    compute_host_generation, compute_host_snapshot = compute_host_update
                    set_compute_hosts(compute_host_snapshot)
                    _HIT_VALID = False
                    if _EXPLORER_ENABLED:
                        _EXPLORER_REMOTE_OBSERVATION = _explorer_remote_host_observation(
                            compute_host_snapshot)
                        latest_basic[0] = _explorer_attach_remote_observation(
                            latest_basic[0], _EXPLORER_REMOTE_OBSERVATION)
                        basic_snapshot = _explorer_attach_remote_observation(
                            basic_snapshot, _EXPLORER_REMOTE_OBSERVATION)
                        snapshot = _explorer_attach_remote_observation(
                            snapshot, _EXPLORER_REMOTE_OBSERVATION)
                        latest_view[0] = snapshot
            _BLINK_ON = _blink_phase(now)
            if selection_feed is not None:
                focus_result, focus_changed = selection_feed.update(
                    _EXPLORER_STATE, generation, now_mono, latest_view[0])
                if focus_changed:
                    _EXPLORER_RESULT = focus_result
                    _HIT_VALID = False

            question_editing = _explorer_text_input()
            if (ch in (ord("q"), ord("Q")) and not _explorer_searching()
                    and not question_editing):
                return 0
            # Idle input boundary only: never discard a key or an in-flight action.
            if (install_target is not None and ch == curses.ERR
                    and not input_reader.has_buffered_input()
                    and _PROMPT is None and _PENDING_KILL is None):
                if _EXPLORER_ENABLED:
                    _explorer_runtime().wait_for_writes(timeout=0.25)
                follower.restart(install_target, getattr(collect_all, "restart_argv", []),
                                 viewer_state(live_order),
                                 stderr_fd=getattr(collect_all, "restart_stderr_fd", None))
                install_target = None  # a raced/failed exec is retried by the next observation
            h, w = stdscr.getmaxyx()
            hit_current = (_HIT_VALID and _HIT_SIZE == (h, w)
                           and _HIT_SNAPSHOT_ID == id(snapshot))
            if not hit_current:
                _HIT_VALID = False
            body_h = _CURRENT_BODY_H if hit_current else 0
            # --- F-27: a pending confirmation swallows ALL keys. Nothing else can happen while
            # the user is being asked, and only an explicit yes proceeds. ---
            if _PROMPT is not None:
                # §4.4.1 — _handle_mouse MUST be called from inside this block (not before it with
                # its own `continue`): the _draw two lines below is what repopulates _PROMPT_HITS
                # for the CURRENT stage before the next getch can land a click. Placing the mouse
                # call ahead of this block would let a click 2 read a stale (pre-transition) map
                # and silently defeat the confirm→confirm2 coordinate inversion (§4.4).
                if ch == curses.KEY_MOUSE:
                    mx, my, bstate = (mouse_event if mouse_event is not None
                                      else _getmouse_event())
                    if mx is not None and (hit_current or not _EXPLORER_ENABLED):
                        _handle_mouse_event(mx, my, bstate)
                else:
                    _handle_prompt_key(ch)
                _draw(stdscr, sessions, jobs, section, malformed, memory=mem_snapshot,
                      live_order=live_order, resources=resources, usage_snapshots=usage_snapshots,
                      governor=governor_snapshot, loading=(generation == 0), snapshot=snapshot)
                start_background_pumps()
                if generation > 0:
                    ensure_selection_feed()
                queue_optional_details()
                continue
            if _EXPLORER_ENABLED:
                _explorer_handle(ch, mouse_event, (h, w), snapshot)
                if _EXPLORER_EARLIER:
                    _EXPLORER_EARLIER = False
                    if selection_feed is not None:
                        selection_feed.pump.load_earlier()
                if (ch in (ord("r"), ord("R")) and not _explorer_searching()
                        and not question_editing):
                    pump.request(force=True)
                    if latest_basic[0] is not None:
                        detail_pump.request(force=True)
                    if compute_host_pump is not None:
                        compute_host_pump.request(force=True)
                    if selection_feed is not None:
                        selection_feed.pump.request(force=True)
                _poll_pending_kill()
                _draw(stdscr, sessions, jobs, section, malformed, memory=mem_snapshot,
                      live_order=live_order, resources=resources,
                      usage_snapshots=usage_snapshots, governor=governor_snapshot,
                      loading=(generation == 0), snapshot=snapshot)
                start_background_pumps()
                if generation > 0:
                    ensure_selection_feed()
                queue_optional_details()
                continue
            if _SELECT_MODE:
                if _handle_select_key(ch):
                    _draw(stdscr, sessions, jobs, section, malformed, memory=mem_snapshot,
                          live_order=live_order, resources=resources,
                          usage_snapshots=usage_snapshots, governor=governor_snapshot,
                          loading=(generation == 0), snapshot=snapshot)
                    continue
            elif ch in (ord("s"), ord("S"), ord("x"), ord("X")):
                # Enter selection mode. `x` doubles as the enter shortcut so the "press x to kill"
                # intent works from a cold start; it selects, it never kills on the first press.
                if not _enter_select(_live_targets()):
                    _set_action("no selectable rows")
                _draw(stdscr, sessions, jobs, section, malformed, memory=mem_snapshot,
                      live_order=live_order, resources=resources, usage_snapshots=usage_snapshots,
                      governor=governor_snapshot, loading=(generation == 0), snapshot=snapshot)
                continue

            # --- base mode: scroll keys UNCHANGED (F-27 regression budget = 0) ---
            if (hit_current and not _SELECT_MODE and _PENDING_KILL is None
                    and _base_navigation(ch, mouse_event)):
                _drain_navigation(input_reader, stdscr, ch, mouse_event, body_h, timeout_ms)
            elif _handle_base_key(ch, body_h):
                pass
            elif ch == curses.KEY_MOUSE:
                mx, my, bstate = (mouse_event if mouse_event is not None
                                  else _getmouse_event())
                if mx is not None:
                    _handle_mouse_event(mx, my, bstate)
            # KEY_RESIZE: no special handling needed — _draw's clamp re-clamps against the new
            # body_h below; do NOT reset _OFFSET here (would destroy scroll position).

            if ch in (ord("r"), ord("R")):
                pump.request(force=True)
                if latest_basic[0] is not None:
                    detail_pump.request(force=True)
                if compute_host_pump is not None:
                    compute_host_pump.request(force=True)
            _poll_pending_kill()     # F-27 grace window — non-blocking; may raise a re-prompt
            # Redraw every wake for the spinner/blink; curses doupdate emits only changed cells.
            _draw(stdscr, sessions, jobs, section, malformed, memory=mem_snapshot,
                  live_order=live_order, resources=resources, usage_snapshots=usage_snapshots,
                  governor=governor_snapshot, loading=(generation == 0), snapshot=snapshot)
    finally:
        if _EXPLORER_ENABLED:
            # The screen guard's writer may still be sending a frame: these bytes go
            # after it, and a terminal that stopped reading cannot hold the quit.
            settle = getattr(stdscr, "settle", None)
            if callable(settle):
                settle()
            try:
                os.write(sys.stdout.fileno(), b"\x1b[?1006l\x1b[?1002l")
            except (OSError, AttributeError):
                pass
        if selection_feed is not None:
            selection_feed.stop()
        _explorer_stop_question_pump()
        if display_pump is not None:
            display_pump.stop(join_timeout=1.0)
        if display_events is not None:
            display_events.stop(join_timeout=1.0)
        _EXPLORER_NATIVE_DISPLAY = None
        pump.stop(join_timeout=1.0)
        detail_pump.stop(join_timeout=1.0)
        if compute_host_pump is not None:
            compute_host_pump.stop(join_timeout=1.0)
        if install_pump is not None:
            install_pump.stop(join_timeout=0.1)
        close_observation = getattr(collect_all, "close", None)
        if callable(close_observation):
            try:
                close_observation()
            except Exception:
                pass


def _stderr_log_path():
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(xdg, "agent-fleet", "fleet-stderr.log")


class _StderrToLog:
    """While the TUI owns the terminal, send fd 2 (Python writes and inherited child
    stderr alike) to a log file: any diagnostic a collector or library prints would
    otherwise land on top of the curses screen and break it. Restored on exit, so a
    crash traceback still reaches the terminal."""

    def __init__(self, path):
        self.path = path
        self.saved = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if os.path.getsize(self.path) > 4 * 1024 * 1024:
                os.replace(self.path, self.path + ".1")
        except OSError:
            pass
        try:
            sys.stderr.flush()
            log = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError:
            return self
        self.saved = os.dup(2)
        os.dup2(log, 2)
        os.close(log)
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            try:
                sys.stderr.flush()
            except Exception:
                pass
            os.dup2(self.saved, 2)
            os.close(self.saved)
            self.saved = None
        return False


def run_live(collect_all, hfilter, section, interval):
    if curses is None:
        sys.stderr.write("fleet: the live TUI needs curses (unavailable here; on native "
                         "Windows run `pip install windows-curses`, or use WSL). "
                         "Use --once (snapshot) or --json meanwhile.\n")
        return 1
    if not sys.stdout.isatty():
        sys.stderr.write("fleet: stdout is not a TTY — use --once (snapshot) or --json.\n")
        return 1
    try:
        _prepare_terminal()
        with _StderrToLog(_stderr_log_path()) as stderr_log:
            collect_all.restart_stderr_fd = stderr_log.saved
            return curses.wrapper(_loop, collect_all, hfilter, section, interval)
    except KeyboardInterrupt:
        return 0
    except Exception as e:  # pragma: no cover
        sys.stderr.write("fleet: curses failed: %s\n" % e)
        return 1


def _prepare_terminal():
    """Let the new curses instance save the original shell's terminal mode.

    The previous viewer execs while still in curses mode to keep its screen.
    Without this, the replacement saves that raw mode as its shell mode and
    leaves the terminal raw after q. Restore immediately before initscr,
    retaining the screen and pending input throughout import/startup.
    """
    global _SHELL_TTY_MODE
    try:
        import termios
    except ImportError:
        return
    try:
        fd = sys.stdin.fileno()
        if _SHELL_TTY_MODE is None:
            modes = termios.tcgetattr(fd)
            # JSON supports ints; tcsetattr accepts either ints or one-byte values.
            _SHELL_TTY_MODE = modes[:-1] + [[value[0] if isinstance(value, bytes) else value
                                           for value in modes[-1]]]
        else:
            termios.tcsetattr(fd, termios.TCSANOW, _SHELL_TTY_MODE)
    except (termios.error, OSError, ValueError):
        pass  # non-TTY input retains the existing curses behavior
