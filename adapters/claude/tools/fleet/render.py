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
"""
try:
    import curses
except ImportError:  # native Windows without windows-curses: plain --once / --json still work
    curses = None
import glob
import math
import os
import re
import sys
import time

from .model import (fmt_min, dash, project_of, exec_child_is_wait,
                    LIVENESS_STATES, PLUGIN_QUEUE_STATES)
from . import gitinfo

# curses attribute constants — real values when curses is present, harmless 0 fallbacks
# otherwise, so this module imports (and the plain --once path runs) with no curses at all.
# The live TUI (run_live) still requires curses and guards for its absence explicitly.
_A_BOLD = getattr(curses, "A_BOLD", 0)
_A_DIM = getattr(curses, "A_DIM", 0)
_A_REVERSE = getattr(curses, "A_REVERSE", 0)

# Low-chroma xterm-256 palette. Semantic axes stay green/yellow/red and
# cyan/magenta/blue, but the stock primaries are replaced with softer midtones.
# Eight-color terminals keep their native colors as a checked fallback.
_MUTED_256 = {
    "soft": 253,       # #dadada — focal text, below pure white
    "green": 150,     # #afd787 — richer sage
    "yellow": 186,    # #d7d787 — warm beige
    "red": 217,       # #ffafaf — soft coral
    "cyan": 116,      # #87d7d7 — clear teal
    "magenta": 176,   # #d787d7 — clear mauve
    "blue": 147,      # #afafff — clear periwinkle
    "vanilla": 230,   # #ffffd7 — pale vanilla spinner
    "chrome": 252,    # #d0d0d0 — header/footer bands
    "warning": 131,   # #af5f5f — warning-band background
}

# On terminals that allow palette redefinition, open the semantic hues by only
# a few RGB points.  The xterm indices above remain the stable fallback, while
# these values add a barely perceptible amount of saturation at equal brightness.
_RICHER_RGB_1000 = {
    "green": (678, 859, 506),    # #addb81
    "yellow": (859, 859, 506),   # #dbdb81
    "red": (1000, 663, 663),     # #ffa9a9
    "cyan": (506, 859, 859),     # #81dbdb
    "magenta": (859, 506, 859),  # #db81db
    "blue": (663, 663, 1000),    # #a9a9ff
}

# Keep each panel rung dark and restrained while giving the near-black
# backgrounds a slightly richer shared slate cast.
_TINT_RGB_1000 = {
    233: (63, 71, 102),    # #10121a
    234: (108, 127, 188),  # #1c2030
    236: (169, 182, 235),  # #2b2e3c
}


def _palette_fg(name, fallback):
    if curses is not None and getattr(curses, "COLORS", 0) >= 256:
        return _MUTED_256[name]
    return fallback


# harness = dim lowercase word in its identity color (no bracket chip, no reverse-video)
_BADGE_TEXT = {"claude": "claude code", "codex": "codex", "opencode": "opencode"}
_BADGE_KEY = {"claude": "h_claude", "codex": "h_codex", "opencode": "h_opencode"}
_LIVE_RANK = {"working": 0, "idle": 1, "blocked": 2, "done": 3, "stale": 4, "dead": 5, "unknown": 6}
_JOB_LIVE_RANK = {"working": 0, "queued": 1, "stale": 2, "dead": 3, "unknown": 4}
# effort → 2-char suffix after the model (design review r2: the effort column repeated 'xhigh'
# everywhere and burned a column; a dim suffix keeps the info without the noise)
# qa rigor ramp (a dispatch job's analogue of effort) — quick recedes, adversarial stands out
_QA_INT = {"quick": _A_DIM, "light": _A_DIM, "standard": 0,
           "thorough": _A_BOLD, "adversarial": _A_BOLD}
_NARROW_CUTOFF = 70
_TWO_LINE_CUTOFF = 138     # width below which sessions render as 2-line cards (F-15a P0-3: the
                           # wide 1-line grid now needs room for the options column too, so the
                           # dead-zone between the old 110 cutoff and wide's real ~138-col need
                           # is gone — 2-line cards are the PRIMARY layout, wide 1-line is rare)
_LAYOUT = "auto"            # 'auto' (width decides) | 'wide' | 'narrow' | 'stack' — `w` key cycles
_LOOPS_KEYS = ("oncall", "note", "study", "drill")
_ALERT_TAIL = re.compile(r"-\d{8,}-\d+$")   # loop job `<case>-<ts>-<pid>` tail (F-10 alert humanize)


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

# fg color_key → (hue, attr) decomposition (spec §5.2) — the basis for composing (fg, tint_bg)
# pairs. hue: d=default w=soft white g=green y=yellow v=vanilla r=red c=cyan
# m=magenta l=blue.
# Keys absent here render as default-hue plain text under tint (safe degradation).
_A_B, _A_D = _A_BOLD, _A_DIM
_HUE_OF = {
    None: ("d", 0), "dim": ("d", _A_D), "head": ("d", _A_D), "unknown": ("d", _A_D),
    "name_work": ("w", _A_B), "name_idle": ("w", _A_B), "name_dim": ("w", _A_D),
    "grp": ("w", _A_B), "branch_s": ("d", 0), "cost_hi": ("w", _A_B),
    "qa_quick": ("d", _A_D), "qa_light": ("d", _A_D), "qa_standard": ("d", 0),
    "qa_thorough": ("d", _A_B), "qa_adversarial": ("d", _A_B),
    "g_work": ("g", _A_B), "g_work_off": ("g", _A_D),
    "g_spin": ("v", 0), "g_spin_dim": ("v", _A_D), "g_idle": ("y", 0),
    "g_stale": ("d", _A_D), "g_dead": ("r", _A_B), "g_unused": ("y", _A_D),
    # F-60: `blocked` is RED, on its own key. It shares a hue with `g_dead` but never a key —
    # keeping them separate is what lets one be retuned later without moving the other. The
    # chip variant adds A_REVERSE and is used ONLY on the interaction badge; the glyph and the
    # context lead word stay plain red — no bold (v47, user 2026-08-05): the chip owns the
    # emphasis, and without color the missing bold is what separates blocked from dead (bold).
    "g_blocked": ("r", 0), "g_blocked_chip": ("r", _A_REVERSE),
    # Badge text, NOT the glyph: plain yellow, distinct from the dim g_unused glyph so the
    # ●>○>◌ ink-weight gradient still reads.
    "g_unused_b": ("y", 0),
    "lvl_g": ("g", 0), "lvl_y": ("y", 0), "lvl_r": ("r", _A_B),
    # F-61: the usage header's own red — same hue and threshold as `lvl_r`, without the bold.
    # The header is always-on background information, so one meter crossing 80% must not make it
    # the heaviest ink on screen. Only the header maps onto this key; every other `lvl_r` surface
    # (context gauge, git divergence, failed route nodes) keeps the alarm weight.
    "lvl_r_flat": ("r", 0),
    "grp_live": ("g", 0), "grp_hot": ("g", _A_B), "gate_t": ("g", _A_D), "gate_u": ("y", _A_D),
    "grp_cool": ("y", _A_D), "grp_cold": ("d", _A_D),   # Cooling is dim yellow; cold is dim grey.
    "eff_low": ("d", _A_D), "eff_medium": ("d", 0), "eff_high": ("l", 0),
    "eff_xhigh": ("m", _A_B), "eff_max": ("r", _A_B),
    "effd_low": ("d", _A_D), "effd_medium": ("d", _A_D), "effd_high": ("l", _A_D),
    "effd_xhigh": ("m", _A_D), "effd_max": ("r", _A_D),
    "h_claude": ("c", _A_D), "h_codex": ("m", _A_D), "h_opencode": ("l", _A_D),
    "hb_claude": ("c", 0), "hb_codex": ("m", 0), "hb_opencode": ("l", 0), "hb_other": ("d", 0),
    "fam_opus": ("c", 0), "fam_sonnet": ("l", 0), "fam_haiku": ("g", 0),
    "fam_fable": ("m", 0), "fam_gpt": ("y", 0), "fam_other": ("d", 0),
    "famd_opus": ("c", _A_D), "famd_sonnet": ("l", _A_D), "famd_haiku": ("g", _A_D),
    "famd_fable": ("m", _A_D), "famd_gpt": ("y", _A_D), "famd_other": ("d", _A_D),
    # stage palette indices 0-4 = blue·cyan·green·yellow·magenta (see _stage_raw)
    "stg0_on": ("l", _A_B), "stg1_on": ("c", _A_B), "stg2_on": ("g", _A_B),
    "stg3_on": ("y", _A_B), "stg4_on": ("m", _A_B),
    "stg0_off": ("l", _A_D), "stg1_off": ("c", _A_D), "stg2_off": ("g", _A_D),
    "stg3_off": ("y", _A_D), "stg4_off": ("m", _A_D),
}


def _key_attr(key, tint=None):
    """Attr for a color_key, composed with the row's tint background when active (spec §5.3)."""
    if tint is None or not _TINT_OK:
        return _COLOR.get(key, 0)
    hue, attr = _HUE_OF.get(key, ("d", 0))
    pair = _TINT_PAIR.get((tint, hue))
    if pair is None:
        return _COLOR.get(key, 0)
    return pair | attr


# ---------- color ----------
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
    # per-MODEL family colors, in TWO intensities (2026-07-02: main↔dispatch contrast = whole-row
    # brightness): fam_* = BRIGHT (main session rows) / famd_* = DIM (dispatch rows recede).
    _hue = {h: _COLOR.get("h_" + h, 0) for h in ("claude", "codex", "opencode")}
    _fam = {"opus": _palette_fg("cyan", curses.COLOR_CYAN),
            "sonnet": _palette_fg("blue", curses.COLOR_BLUE),
            "haiku": _palette_fg("green", curses.COLOR_GREEN),
            "fable": _palette_fg("magenta", curses.COLOR_MAGENTA),
            "gpt": _palette_fg("yellow", curses.COLOR_YELLOW)}
    n_pair = 10                                     # pairs 1-9 reserved above; families from 10
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
    # branch: normal on main session rows, dim on dispatch rows (same brightness axis)
    _COLOR["branch_s"] = 0
    for h, hue in _hue.items():
        # bright harness color = a TOP-LEVEL session / account; a dispatch job keeps the DIM
        # harness (h_<h>) → main↔spawned weight is carried by font-color intensity (no bg fill).
        _COLOR["hb_" + h] = hue
    _COLOR["hb_other"] = 0
    _COLOR["grp"] = _COLOR.get("soft", 0) | curses.A_BOLD  # group card title
    _COLOR["grp_live"] = _COLOR.get("green", 0)
    _COLOR["grp_hot"] = _COLOR.get("green", 0) | curses.A_BOLD   # active card title (working)
    _COLOR["grp_cool"] = _COLOR.get("yellow", 0) | curses.A_DIM  # Cooling indicator, name, and elapsed time.
    _COLOR["grp_cold"] = curses.A_DIM                            # Cold inactive group: grey ring.
    # harness identity = dim colored text (color lives ONLY here for identity)
    for h in ("claude", "codex", "opencode"):
        _COLOR["h_" + h] = _COLOR.get("h_" + h, 0) | curses.A_DIM
    # session name = THE left pillar of every row (design r2): bright bold for any live session —
    # the eye lands here first; only stale/dead recede. working is distinguished by its dot blink.
    _COLOR["name_work"] = _COLOR.get("soft", 0) | curses.A_BOLD
    _COLOR["name_idle"] = _COLOR.get("soft", 0) | curses.A_BOLD
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
        curses.init_pair(15, curses.COLOR_BLACK,
                         _palette_fg("chrome", curses.COLOR_WHITE))
        _COLOR["hdr_bar"] = curses.color_pair(15)
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
        curses.init_pair(17, _palette_fg("soft", curses.COLOR_WHITE),
                         _palette_fg("warning", curses.COLOR_RED))
        _COLOR["hdr_warn"] = curses.color_pair(17) | curses.A_BOLD
    except Exception:
        _COLOR["hdr_warn"] = curses.A_REVERSE | curses.A_BOLD
    # The one key the user must press, advertised on top of the red bar.
    _COLOR["hdr_warn_key"] = _COLOR["hdr_warn"] | curses.A_REVERSE
    # bar BLANKS are drawn as muted-neutral █ blocks on the DEFAULT bg (pair 16), not as bg-colored
    # spaces: ncurses collapses blank runs into ECH/EL erase sequences, and on terminals without
    # working BCE the erased cells come out BLACK — the bar broke between words and after the
    # text. A block glyph is a real character, so it is
    # physically written every time and matches the muted bar background cell.
    try:
        curses.init_pair(16, _palette_fg("chrome", curses.COLOR_WHITE), bg)
        _COLOR["hdr_blk"] = curses.color_pair(16)
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
            hues = {"d": -1, "w": _palette_fg("soft", curses.COLOR_WHITE),
                    "g": _palette_fg("green", curses.COLOR_GREEN),
                    "y": _palette_fg("yellow", curses.COLOR_YELLOW),
                    "v": _palette_fg("vanilla", curses.COLOR_YELLOW),
                    "r": _palette_fg("red", curses.COLOR_RED),
                    "c": _palette_fg("cyan", curses.COLOR_CYAN),
                    "m": _palette_fg("magenta", curses.COLOR_MAGENTA),
                    "l": _palette_fg("blue", curses.COLOR_BLUE)}
            n_pair = 20
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
    _COLOR["dim"] = curses.A_DIM
    _COLOR["head"] = curses.A_DIM
    _COLOR["unknown"] = curses.A_DIM


def _attr(key):
    return _COLOR.get(key, 0)


def _live_key(state):
    return {"working": "g_work", "idle": "g_work_off", "unused": "g_unused",
            "stale": "g_stale", "dead": "g_dead"}.get(state, "dim")


# status dot — SHAPE+SIZE gradient (design r2, a11y): the less active the state, the smaller
# the glyph. Working uses a light-yellow spinner; live idle/detached use the dim-green
# loading axis; stale/dead recede to grey/red. Readable without color.
# F-26 `unused` = ◌ (U+25CC DOTTED CIRCLE). Shape gradient reads ● (filled) > ○ (ring) >
# ◌ (dotted ring = never filled), which is exactly the "started but never prompted" meaning.
# ○ was NOT available: _DETACHED_GLYPH already owns it, and detached (attach axis) vs unused
# (activity-history axis) are unrelated — separating them by color alone would break the
# "Readable without color" contract this table is built on.
_LIVE_GLYPH = {"working": "●", "idle": "●", "unused": "◌", "blocked": "◑", "done": "✓", "degraded": "◐",
               "stale": "·", "dead": "✕", "queued": "◦", "unknown": "·"}
_DETACHED_GLYPH = "○"   # Ring means no attached client; idle uses a filled dim-green dot.
_GLYPH_KEY = {"working": "g_work", "idle": "g_work_off", "unused": "g_unused",
              "blocked": "g_blocked", "done": "green",   # F-60: red, on its own key
              "stale": "g_stale", "dead": "g_dead", "degraded": "lvl_y", "queued": "dim", "unknown": "dim"}
_INTERACTION_LABEL = {
    "decision": "decision",
    "approval": "approval",
    # Claude calls the runtime event a permission prompt while Codex calls the equivalent
    # user gate an approval request. Fleet keeps that producer evidence intact, but presents
    # one user-facing term for the same action.
    "permission": "approval",
    "elicitation": "elicit",
}

# group "cooling" state (user 2026-07-03): a directory with NO active work whose newest session
# A recent transcript write reads as cooling after completion, between hot
# (green ● + green-bold title) and cold (no glyph). It gets a grey ring + time-since-last-activity
# in the header, so a just-finished repo says "done & waiting", not fully dormant. Tune freely.
_COOL_WINDOW_MIN = 180
# Shape-size gradient: recent active states are larger and filled; cold groups use a ring.
_COOL_FILLED = "●"      # Recently completed directory within the cooling window.
_COOL_RING = "○"        # Long-inactive directory.
_COOL_TIME_ICON = "✓"   # Prefix for elapsed time since completion.


_BLINK_ON = True     # manual blink phase (toggled ~2 Hz in the live loop) — drives the spinner too
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # braille loading spinner — working SESSIONS animate (user 2026-07-03);
                     # the blinking green ● moved up to the directory title


def _glyph(state, dim=False):
    """Session/job status glyph. working = a light-yellow braille spinner frame;
    the dispatch variant keeps a quieter weight so main and dispatch differ.
    idle = dim-green FILLED ●, detached = dim-green ring ○."""
    if state == "working":
        return _SPIN[int(time.time() * 10) % len(_SPIN)], _state_key(state, dim=dim)
    return _LIVE_GLYPH.get(state, "·"), _state_key(state, dim=dim)


def _state_key(state, dim=False):
    """The ONE color key for a liveness state, whatever shape draws it.

    F-55 puts the state WORD on the context detail row while the harness row keeps the
    glyph; both must land on the same color or the two cells stop reading as the same
    session. Splitting this decision across two call sites is exactly how that drifts, so
    the glyph producer and the word producer both come through here.
    """
    if state == "working":
        return "g_spin_dim" if dim else "g_spin"
    return _GLYPH_KEY.get(state, "dim")


def _pct_key(v):
    if v is None:
        return "dim"
    return "lvl_r" if v >= 80 else ("lvl_y" if v >= 50 else "lvl_g")


def _flat_level(key):
    """F-61: the usage header's non-bold rendering of a `_pct_key` level.

    Only `lvl_r` carries weight, so this is a single rename rather than a parallel palette —
    thresholds, hue, and every other level key stay exactly as `_pct_key` decided them. Kept as
    a function (not a dict literal at the call site) so the one place that drops the alarm weight
    is greppable from both the header and its test.
    """
    return "lvl_r_flat" if key == "lvl_r" else key


_FAMILIES = ("opus", "sonnet", "haiku", "fable", "gpt")


def _model_family(model):
    """Model-family token from a model/bucket name: 'Opus 4.8'→opus, 'gpt-5.5'→gpt, 'fable'→fable.
    Unknown (glm/deepseek/…) → 'other' (default fg — distinct from the colored families)."""
    m = (model or "").lower()
    for fam in _FAMILIES:
        if fam in m:
            return fam
    return "other"


def _model_key(model, dim=False):
    return ("famd_" if dim else "fam_") + _model_family(model)


# bare model-ROLE tokens (dispatch env exports the portable role, not a concrete model id) →
# proper-noun display. Family word only — never a fabricated version number (the env doesn't
# say WHICH opus the harness resolved).
_ROLE_MODEL_NAME = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "fable": "Fable"}


def _clean_model(name):
    """'Opus 4.8 (1M context)' → 'Opus 4.8' (drop the trailing parenthetical — redundant, ugly
    when truncated); bare role tokens 'opus'/'sonnet' → 'Opus'/'Sonnet' (user 2026-07-20);
    'opencode-go/glm-5.2' → 'glm-5.2' (user 2026-08-07).

    The provider segment is dropped because the harness cell one column over already says
    `opencode`, so the prefix spends 12 of the model cell's 23 characters restating it —
    and it was the name, not the prefix, that got clipped away to pay for it."""
    if not name:
        return name
    name = name.split(" (", 1)[0]
    if "/" in name:
        name = name.split("/", 1)[1]
    return _ROLE_MODEL_NAME.get(name.lower(), name)


# Variant/effort tokens that carry no information: the runtime picked its own default and
# never told us which. Rendering the word costs most of the model cell to say nothing (and
# clipped 'opencode-go/glm-5.2' down to 'open'), so these render as no effort at all —
# the same honest blank a missing effort already gets.
_EMPTY_EFFORT = {"runtime-default", "inherit", "unknown", "default"}


def _effort_text(effort):
    return "" if not effort or str(effort).strip().lower() in _EMPTY_EFFORT else str(effort)


_MODEL_ID_RE = re.compile(r"^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?(?:-\d{8})?$")


def _short_model_id(name):
    """Versioned wire id → display form: 'claude-sonnet-5' → 'Sonnet 5',
    'claude-haiku-4-5-20251001' → 'Haiku 4.5'. Sub-agent transcripts carry the
    raw API model id (unlike the statusline display_name sessions get), so the
    strip needs this one extra normalization; anything unrecognized passes
    through for `_clean_model` to handle."""
    if not name:
        return name
    m = _MODEL_ID_RE.match(name)
    if not m:
        return name
    fam, major, minor = m.group(1).capitalize(), m.group(2), m.group(3)
    return "%s %s" % (fam, major + ("." + minor if minor else ""))


# mid-height bar (━ filled / ─ empty): the glyphs sit at the cell's vertical centre, so gauges on
# adjacent rows keep an above/below gap and never merge into a solid vertical wall (no blank line
# needed). Filled carries the level color; the empty track is dim — the fill reads by color too.
_BAR_FULL, _BAR_EMPTY = "━", "─"
_GAUGE_W = 12                 # usage-header meter: fixed twelve cells. F-59 (v44) replaces the
                              # SIX-cell figure in F-51a and nothing else in it — the glyphs,
                              # the 50/80 color thresholds, the unknown-vs-real-0% split and
                              # F-51b's stale `·` track are all unchanged, and the quantization
                              # generalizes because `_gauge_segs` reads this constant instead of
                              # hardcoding a width. F-52b's context track is a separate axis and
                              # is still not sized from here.
_CTX_TRACK_MAX = 16           # F-52b/F-57b: a 1M context window is 16 cells. Independent of the
                              # harness column — v38 widened this 16→20 for readability, and F-57b
                              # (v41) restores 16 (user 2026-08-04: "게이지 폭을 좀 줄이자").
_CTX_TRACK_WINDOW = 1000000   # …and the track scales linearly against that reference window.
_GIT_TELEMETRY = True


def _half_up(value):
    """Half-up rounding (Python's round() is banker's rounding — 0.5 would fall DOWN)."""
    return int(math.floor(float(value) + 0.5))


def _context_gauge_track(window):
    """F-52b context-gauge track length: `clamp(half_up(16 * window / 1M), 1, 16)`.

    `window` is a MEASURED `context_window_tokens` telemetry value only — there is no model or
    harness lookup table here. With no measurement we cannot claim a proportion, so the track
    falls back to the 16-cell baseline (the percent itself is still known, so the row stays)."""
    if (not isinstance(window, (int, float)) or isinstance(window, bool)
            or window != window or window in (float("inf"), float("-inf")) or window <= 0):
        return _CTX_TRACK_MAX
    return max(1, min(_CTX_TRACK_MAX, _half_up(_CTX_TRACK_MAX * float(window) / _CTX_TRACK_WINDOW)))


def _gauge_segs(pct, width, track=None):
    """Gauge segments using half-up quantization over `track` cells (default: the fixed
    six-cell usage meter). `width` is the legacy available-width argument and never sizes the
    meter — pass `track` to size it (F-52b's window-proportional context gauge)."""
    cells = _GAUGE_W if track is None else max(1, int(track))
    if not isinstance(pct, (int, float)) or isinstance(pct, bool) or pct <= 0:
        filled = 0
    elif pct >= 100:
        filled = cells
    elif cells <= 1:
        # A one-cell track has no cell left to spend on an in-between state: lighting it below
        # 100% would read as full. Under-report rather than over-report.
        filled = 0
    else:
        filled = max(1, min(cells - 1, _half_up(float(pct) * cells / 100.0)))
    return [(_BAR_FULL * filled, _pct_key(pct)), (_BAR_EMPTY * (cells - filled), "dim")]


def _pad(s, w):
    """Pad/truncate ASCII text to exactly w cells (columns align across rows)."""
    s = s or ""
    return s[:w].ljust(w)


_BR_TTL = 15.0
_BR_CACHE = {"ts": 0.0, "map": {}}


_git_branch = gitinfo.branch
_wt_count = gitinfo.worktree_count


# ---------- row builders (return a single segment-line: [(text, color_key), ...]) ----------
# Design pass 2026-07-01 (deep) — a dispatch JOB is a session-analogue, not a lesser row: its
# fields map onto the SAME columns as a session's, so the whole board reads with one grammar:
#     column        session            dispatch job
#     harness       ▐reverse badge▌    dim font       ← weight = main vs spawned
#     name          bright             dim
#     model slot    model              process = pipeline·mode   (e.g. code · dev)
#     effort slot   effort (low→max)   qa (quick→adversarial)    ← both are the "intensity" dial
#     gauge slot    context % bar      stage breadcrumb (plan › exec › test)  ← "how far along"
# main↔dispatch weight is carried by the badge (reverse vs dim font), so the identity columns can
# stay aligned for comparison. Job flow never sits under branch/gate.
_HW = 16                      # Bare harness-badge width — narrow/stack L1 badges and the
                              # dispatch-prefix budget math still use this unmerged value.
_HMW = 40                     # F-33/F-64/F-65: WIDE-layout harness field. The latest small
                              # expansion is 38→40 (user 2026-08-06) so a dispatch-depth-2 spawned row
                              # retains two more cells after paying for its hierarchy prefix.
                              # the labels that actually render are `claude code (Opus 5·xhigh)`
                              # at 26 cells and `codex (gpt-5.6-sol·xhigh)` at 25; at 40 the
                              # 33-cell `opencode (claude-sonnet-4-5·high)` worst case fits even
                              # behind the dispatch-depth-1 prefix (40-5=35) and the dispatch-depth-2 field keeps
                              # 32 cells under the F-64 ladder, while staying below the
                              # blank-heavy 42 that F-58 rolled back. These two cells
                              # shift the whole wide row right and are charged to the _wide_slack
                              # budget; the _NW_S/_NAME_WIDE_MAX allocation rules are unchanged
                              # and the context track (_CTX_TRACK_MAX) is independent of this
                              # field's width. Model/effort stay
                              # folded in as a parenthetical ('claude code (Fable 5
                              # · xhigh)') — replaces the separate model column (_MW) on the wide
                              # session/dispatch rows only; narrow/stack keep _HW plus their own
                              # L2 model cell, unchanged.
_NAME_COL = 4 + _HMW          # absolute col where the NAME starts on a WIDE row — SHARED by both
                              # row types so everything from the name onward aligns (session:
                              # prefix 4 + harness-model _HMW; dispatch: prefix 6 + harness-model
                              # narrowed by 2 — deeper indent, same total).
_NW_S = 28                    # wide-layout name field width (both row types) — a fixed constant
                              # (previously derived from a hardcoded branch column) so growing
                              # _NAME_COL above for the harness/model merge never shrinks it.
_NAME2_MAX = 40               # 2-line name zone tail-cut cap (display cells) — no fixed branch
                              # column there, so an unbounded title could push branch off-draw (F-14)
_NAME_GAP = 1                 # Cell reserved before the integrated `` (branch)`` suffix. Without
                              # it a name+identity suffix that exactly fills the title budget can
                              # collide with the branch parenthesis.
_NAME_WIDE_MAX = 40           # F-22 minor (v8): FIXED upper bound for the wide-layout name zone.
                              # Adjust here and nowhere else. Without it the zone absorbed ALL
                              # remaining slack (measured: 168 cols → 77, 200 → 109), which
                              # stretched the name so far that branch/model/context drifted out
                              # of comfortable scan range. Slack past this cap is NOT
                              # redistributed to other columns — it stays as end-of-row padding.
_TITLE_MAX = 24               # Legacy/fallback title budget and F-15 dispatch-name cap.
                              # F-22 session rows expand beyond this only when terminal width
                              # is known; dispatch labels stay compact inside the wider column.
_OPTW = 18                    # F-15a options column width (dim mode·qa·profile token, sits
                              # between the model cell and the stage breadcrumb — declutters
                              # the name zone, which used to carry this as a parenthetical tag)
_BRANCH_SUFFIX_W = 14         # Space reserved inside the session column for `` (branch)``.
_WIDE_STAGE_GAP = 5           # One-cell wider boundary between session and stages (was 4).
_WIDE_TIME_GAP = 1            # Extra owned spacer before the right-flushed time column.
_MW = 23                      # model cell: name + FULL effort word ('Opus 4.8 xhigh' — no abbrev)
_EFW = 7                      # effort subfield ("medium"=6 +1 gap) — FIXED width so every row's
                              # effort lands in the same column, under its own 'effort' header
# (F-57, v41) `_CTX_W = 24` lived here: the wide main row's INLINE context gauge. F-37a (v16)
# replaced that gauge with the dedicated context detail row, but the reservation stayed in the
# ledger and kept 24 cells of the main row permanently blank. Removed, not zeroed.
_CLOCK = ""                   # Bare elapsed value; icons caused width and readability issues.

# known pipeline stage sequences → the stage breadcrumb (process viz). Unknown keys/stages fall
# back to a single lit stage token (never a fabricated track). Keyed by the dispatch `key`.
_PIPE_STAGES = {
    "code": ["plan", "exec", "test"],
    "review": ["plan", "exec", "test"],
    "spec": ["spec", "design", "dev"],
    "research": ["search", "analyze", "report"],
    "draft": ["draft", "refine", "apply"],
}

# A main interactive session has no dispatch ``key``.  When its only exact
# evidence is one artifact plan directory, retain the historical generic code
# track instead of collapsing the session to ``stage <current>``.  A sealed
# route always outranks this route-absent fallback.
_INLINE_ARTIFACT_STAGES = ["plan", "exec", "test", "report"]

# dispatch-depth-2 stage worker role → human stage label (SD-F1). Code workers use their sub-skill
# names; other pipelines use the portable `stage-<name>` role emitted by dispatch. The label
# route-backed rows consume their attached projection directly.
_STAGE_ROLE = {
    "code-plan": "plan",
    "code-execute": "exec",
    "code-test": "test",
    "code-report": "report",
    "stage-search": "search",
    "stage-analyze": "analyze",
    "stage-report": "report",
}


def _stage_role_label(worker_role):
    """(base_label, suffix) for a dispatch-depth-2 stage worker_role, e.g. 'stage-search:phase-A' ->
    ('search', ':phase-A'). base_label is None when worker_role isn't a known stage sub-skill —
    callers fall back to the existing _ROLE_SHORT/_compact_dispatch_name path."""
    if not worker_role:
        return None, ""
    base, _sep, suffix = worker_role.partition(":")
    label = _STAGE_ROLE.get(base)
    if label is None:
        return None, ""
    return label, (":" + suffix if suffix else "")

# Plain-text column labels without decorative icons.
# 'effort' gets its OWN header over the fixed subcolumn inside the model cell (user 2026-07-02).
_STAGE_RESERVE = 20           # trailing room for the dispatch stage breadcrumb after the
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


def _col_head(name_width):
    # F-33 (v11): the model/effort header folds into the harness column now that the row
    # content does too — no more separate "model" header between branch and the gauge.
    return ("    " + "harness (model·effort)".ljust(_HMW)
            + "session (branch)".ljust(name_width + _BRANCH_SUFFIX_W)
            + " " * _WIDE_STAGE_GAP + "stages" + " " * _WIDE_TIME_GAP)


def _branch_suffix_segs(cwd, branch, dim=True, optional=False):
    """Integrated `` (branch)`` suffix for the session column.

    Wide rows reserve a fixed amount for this suffix; narrow/stack rows use only
    its visible width. Branch text keeps the former brightness distinction while
    the punctuation recedes.
    """
    br = branch or _git_branch(cwd)
    if not br and optional:
        return []
    # The title side reserves `_NAME_GAP`, so the visible suffix may use one cell
    # beyond its nominal reserve without moving the next column.
    shown = _clip_w(str(br or "—"), max(1, _BRANCH_SUFFIX_W - 2))
    base = [(" (", "dim"), (shown, "dim" if dim else "branch_s")]
    close = [(")", "dim")]
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


def _eff_key(effort, dim):
    """Effort heat ramp: low and medium recede,
    high = blue, xhigh = bold magenta, max = bold red. Dim rows (dispatch/stale) keep the
    ramp's HUE at dim weight (user 2026-07-20: 분사 행에도 컬러 — flat grey read as subtitle)."""
    known = effort in ("low", "medium", "high", "xhigh", "max")
    if dim:
        return ("effd_" + effort) if known else "dim"
    return ("eff_" + effort) if known else None


# 2-char effort forms — the F-9(c) middle rung in `_harness_model_cell`'s fit ladder: a
# narrowed dispatch cell shortens the effort word before it would ever clip the model name
# (user 2026-07-20: 'Opus 4.'/'Sonn' 잘림 대신 풀네임).
_EFF_SHORT = {"low": "lo", "medium": "md", "high": "hi", "xhigh": "xh", "max": "mx"}


def _model_cell(model, effort, width, dim=False):
    """Render model and effort together as one flowing phrase, padded to width."""
    name = _clean_model(dash(model)) or "—"
    sfx = _effort_text(effort)
    lkey = _model_key(model, dim=dim)
    if sfx:
        name = name[: max(1, width - len(sfx) - 4)]
        pad = max(0, width - len(name) - len(sfx) - 3)
        return [(name, lkey), (" (" + sfx + ")", _eff_key(sfx, dim)), (" " * pad, None)]
    return [(_pad(name[: width - 1], width), lkey)]


def _harness_model_cell(harness, model, effort, width, hkey, dim=False, unknown="?"):
    """F-33 (v11, 사용자 확정 2026-07-16) — WIDE-layout harness field with model/effort folded
    in as a parenthetical: 'claude code (Fable 5·xhigh)'. The harness text keeps its
    existing hb_*/h_* badge color (`hkey`); the parenthetical reuses `_model_cell`'s
    family/effort colors and the flush '·' stays dim (user 2026-07-16: the spaced
    ' · ' read too wide — the freed cells go to the model name). No model value ->
    the parenthetical is omitted entirely (honest gap, F-3), matching a dead/stale row that
    has no live telemetry to show. Always returns segments summing to exactly `width` cells
    (long names/ids clip the same way `_model_cell` already did, never overflow) — the last
    cell is always left as guaranteed padding so a maxed-out clip never runs the closing `)`
    straight into the name column (the `_NAME_GAP` collision, same idiom, new spot)."""
    hn = _BADGE_TEXT.get(harness, unknown) if harness else unknown
    segs = [(hn, hkey)]
    used = len(hn)
    name = _clean_model(dash(model)) if model else None
    if name and name != "—":
        room = width - used - 4          # " (" prefix + trailing ")" + 1 guaranteed gap
        # F-9(c) fit ladder (user 2026-07-20: 'Opus 4.'/'Sonn' mid-word clips) — the FULL
        # model name outranks the effort word: effort shortens to its 2-char form, then
        # drops whole, before the model name loses a single character. Color keys stay
        # keyed by the full effort value even when the short form is shown.
        eff_full = _effort_text(effort)
        eff = eff_full
        if eff and room < len(name) + 1 + len(eff):
            eff = _EFF_SHORT.get(eff_full, eff_full)
        if eff and room < len(name) + 1 + len(eff):
            eff = ""
        if eff:
            segs += [(" (", "dim"), (name, _model_key(model, dim=dim)),
                     ("·", "dim"), (eff, _eff_key(eff_full, dim)), (")", "dim")]
            used += 2 + len(name) + 1 + len(eff) + 1
        elif room > 0:
            nm = name[: max(1, room)]
            segs += [(" (", "dim"), (nm, _model_key(model, dim=dim)), (")", "dim")]
            used += 2 + len(nm) + 1
    if used < width:
        segs.append((" " * (width - used), None))
    return segs


_STAGE_ZONE_MAX = 30          # D3 (v9) — one constant, one place, same idiom as
                               # _NAME_WIDE_MAX(:523)/_DISPATCH_NAME_MAX(:887)/_PROFILE_MAX(:944).
                               # 168-col zero-overflow was previously incidental (a measured
                               # 5-cell slack, not a bound): a longer conductor/qa label or a
                               # further-along stage re-broke it. Widest real row since the
                               # dispatch-depth-1 entry-skill prefix moved to the options dial
                               # (2026-07-20) is a prefixed dispatch-depth-2 stage worker
                               # ("exec: plan✓ › exec › test" = 25) — 30 keeps headroom
                               # without regressing anything currently on screen.
_ROUTE_STAGE_ZONE_MAX = 42     # user 2026-07-21 ("전부 표시해줘"): a dispatch-depth-1 CONDUCTOR's
                               # route breadcrumb shows the WHOLE pipeline (plan › execute › test
                               # › report), not just "where now". A 4-stage code route is 32 cells,
                               # over _STAGE_ZONE_MAX, so plan was silently folded by
                               # _drop_past_stages; this wider bound is CONDUCTOR-only (route_seq
                               # path, ~:851) so dispatch-depth-2 / narrow-card zones keep the tuned 30.
                               # 42 is the 168-col zero-overflow bound — real terminals wider than
                               # that lend their slack to the breadcrumb via _route_zone_width.


def _route_zone_width(term_width, name_width=None):
    """CONDUCTOR breadcrumb budget for the WIDE layout: the tuned 168-col bound plus
    whatever real terminal slack remains after the responsive name column took its
    growth (one ledger — never double-spend the same cells). A longer strong+ route
    (plan-check, review replicas) folds its early stages at 42; on an actually-wide
    terminal the whole pipeline stays visible (user 2026-07-24: "앞쪽에 pre, plan,
    execute 쪽은 없어지네?")."""
    if not term_width:
        return _ROUTE_STAGE_ZONE_MAX
    name_growth = max(0, (name_width or _NW_S) - _NW_S)
    return _ROUTE_STAGE_ZONE_MAX + max(0, int(term_width) - 168 - name_growth)


def _drop_past_stages(items, cur_i, max_width):
    """SD-F2 (prd.md:164) — a breadcrumb's information value is "where now", not "where
    I've been": fold PAST stages (i < cur_i) first, earliest first, so the active stage (and
    anything after it) survives longest. Whole-component drop (a stage + its separator, F-9(c)
    idiom) — never a mid-token tail-cut."""
    items = list(items)
    def width():
        w = sum(_dw(t) for _i, t, _k in items)
        return w + max(0, len(items) - 1) * 3   # " › " separators, _dw == 3
    while width() > max_width and items and items[0][0] < cur_i:
        items = items[1:]
    return items


def _route_current_index(route_seq):
    """The breadcrumb's CURRENT node index — the single judge every current-hue consumer
    shares (the lit token and the F-64c rail must never disagree on color)."""
    for want in ("active", "degraded", "reconciling"):
        found = next((i for i, (_nid, st) in enumerate(route_seq) if st == want), None)
        if found is not None:
            return found
    done_idx = [i for i, (_nid, st) in enumerate(route_seq) if st == "done"]
    return min((done_idx[-1] + 1) if done_idx else 0, max(0, len(route_seq) - 1))


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
    for i, (nid, st) in enumerate(route_seq):
        if st == "failed":
            items.append((i, nid + "✕", "lvl_r"))
        elif st == "degraded":
            items.append((i, nid + "◐", "lvl_y"))
        elif st == "reconciling":
            items.append((i, nid + "…", "lvl_y"))
        elif st == "done":
            items.append((i, nid + "✓", "stg%d_off" % (i % 5)))
        elif st == "active":
            items.append((i, nid, _cur_key(i)))
        else:
            # pending (and any residual skipped that a caller did not filter) — the name in the
            # dim off-hue, no glyph. Kept glyph-free on purpose: ✓/✕ are the only markers, and a
            # skip glyph like ⊘ is missing from many terminal fonts (user 2026-07-24 icon error).
            items.append((i, nid, "stg%d_off" % (i % 5)))
    if max_width is not None:
        items = _drop_past_stages(items, cur_i, max_width)
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
        return [(stage, _cur_key(0))]
    return [("-", "dim")]


def _plugin_phase(j):
    """F-50e — the openai-codex plugin's own `phase` word, verbatim, or ''.

    The plugin's phase vocabulary is third-party internal ("investigating", "done", …). It is
    NEVER mapped onto a canonical stage, breadcrumb or WorkProjection (F-50a forbids inventing
    meaning for an unknown word); it is shown as-is, dim, in the row's micro-status slot, and
    an absent/blank phase simply shows nothing.
    """
    record = getattr(j, "plugin_job", None)
    phase = record.get("phase") if isinstance(record, dict) else None
    if not isinstance(phase, str) or not phase.strip():
        return ""
    return _clip_w(" ".join(phase.split()), _STAGE_ZONE_MAX)


def _dispatch_stage_segs(j, key, stage, slug_name, working=False, route_seq=None,
                         route_zone=None):
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
            return [(stage, "stg0_off")]
        return []
    if route_seq:
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
        role_label, role_suffix = _stage_role_label(key)
        prefix_text = (role_label + role_suffix) if role_label else key
        prefix_seg = (prefix_text + ": ", "name_dim")
        body = _stage_segs(key, stage, working=working,
                           max_width=max(0, _STAGE_ZONE_MAX - _dw(prefix_seg[0])))
        segs = [prefix_seg] + body
        if sum(_dw(t) for t, _k in segs) > _STAGE_ZONE_MAX:
            # F-9(c): past stages already folded as far as they can (SD-F2 keeps the active
            # stage) — the next whole component to drop is the role-label prefix itself.
            return body
        return segs
    return _stage_segs(key, stage, working=working, max_width=_STAGE_ZONE_MAX)


def _stage_zone_segs(bc):
    """Stage-zone lead-in (user 2026-07-20: "stages 앞에 콜론 등으로 구분감") — a dim ' : '
    separates the breadcrumb from the options dial. The leading space is load-bearing: a
    dial that overflows _OPTW leaves no pad, and a flush colon would read as the dial's own
    label ('layer2: pre'). Skipped when the breadcrumb already opens with its own
    'label: ' prefix (SD-F1 rows) — a second colon would stutter. An empty
    breadcrumb gets an explicit ``-`` instead of leaving the stages slot blank."""
    if not bc:
        return [(" : ", "dim"), ("-", "dim")]
    lead_text, lead_key = bc[0]
    own_prefix = lead_key == "name_dim" and lead_text.endswith(": ")
    return ([("  ", None)] if own_prefix else [(" : ", "dim")]) + bc


def _session_name(s):
    """The session name chain, made explicit (F-26): AI/sidecar title → registry name → slug
    → cwd basename. `registry_name` is a real link in this chain, not decoration: a session
    that has never been prompted has no title, and without the registry name it would render
    as an anonymous cwd basename — which is exactly how the ghost session hid."""
    slug = s.slug or (s.cwd.rsplit("/", 1)[-1] if s.cwd else "?")
    return s.title or getattr(s, "registry_name", None) or slug


def _projection_stage_text(entity, max_width=24):
    """Render only the entity's attached projection, never a guessed child route."""
    projection = getattr(entity, "work_projection", None)
    if projection is None:
        return ""
    if getattr(projection, "ambiguity", None):
        return ""
    label = getattr(projection, "stage_label", None)
    if not label:
        return ""
    progress = getattr(projection, "progress", None)
    suffix = ""
    if progress is not None:
        suffix = " %d/%d" % (progress.done, progress.total)
    return _clip_w("stage %s%s" % (label, suffix), max_width)


def _spec_phase_seq(entity):
    """``[(display, state), ...]`` for a spec-grounding projection's lit phase breadcrumb, else
    None. The sequence is attached by ``projection._spec_marker_projection`` in ``_route_view``."""
    cap = getattr(entity, "cap_grounding", None) or {}
    if cap.get("capability") != "autopilot-spec":
        return None
    projection = getattr(entity, "work_projection", None)
    if not projection or getattr(projection, "source", None) != "artifact-inferred":
        return None
    backing = getattr(projection, "_route_view", None) or {}
    seq = backing.get("spec_phases")
    if not seq:
        return None
    return [(str(a), str(b)) for a, b in seq]


def _session_stage_segs(entity, working, max_width):
    """Stage-zone segments for a session row. A spec-grounding projection renders a lit phase
    breadcrumb in the EXACT dispatch-row syntax (user 2026-07-24 "code에 맞춰서 소괄호에 넣는
    걸로"): ``spec(mode·intensity) : spec✓ › dev●`` — entry ``spec`` in the dim name_dim hue, the
    behaviour knobs (mode; intensity when the spec recorded one — inline specs usually do not) in
    a dim paren group joined by ``·`` exactly as ``_opts_segs`` does, then the dim ` : `
    breadcrumb lead-in (``_stage_zone_segs``). Deferred / n·a phases are dropped (not part of this
    project's flow; also avoids a skip glyph absent from many fonts). Otherwise the flat label."""
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
        # 세션에 떠야"). A known inferred stage, when present, trails after the dim ` : ` lead-in.
        name = cap["capability"].replace("autopilot-", "")
        knob_items = [k for k in (cap.get("mode"), cap_intensity) if k]
        segs = [(name, "name_dim")]
        if knob_items:
            segs += [("(", "dim"), ("·".join(knob_items), "dim"), (")", "dim")]
        projection = getattr(entity, "work_projection", None)
        stage = getattr(projection, "stage_label", None)
        # Defensive F-43 boundary: even a manually injected/mixed projection
        # must not trail stale spec phases after a non-spec capability tag.
        if (getattr(projection, "_route_view", None) or {}).get("spec_phases"):
            stage = None
        if stage:
            segs += [(" : ", "dim"), (_clip_w(str(stage), max(4, max_width - sum(_dw(t) for t, _k in segs) - 3)),
                                      "g_work" if working else "dim")]
        return segs
    text = _projection_stage_text(entity, max_width=max_width)
    return [(text or "-", "g_work" if text and working else "dim")]


def _collapse_parallel_nodes(nodes):
    """Fold parallel legs into one ``<group>(N-way)`` node.

    ``parallel_group`` is canonical; ``replica_group`` remains a read-only
    one-window alias for prior route records.

    The individual legs already render as their own dispatch rows under the
    conductor (user 2026-07-24: "병렬 leg를 굳이 표현을 안해도 되잖아"), so every
    route-projection surface names the group once. State is the group's strictest
    liveness (failed > active > reconciling > all-done > all-pending); downstream
    ``depends_on`` references to a member are rewritten to the merged id."""
    nodes = list(nodes or ())
    groups = {}
    for node in nodes:
        group = node.get("parallel_group") or node.get("replica_group")
        if isinstance(group, str) and group:
            groups.setdefault(group, []).append(node)
    groups = {gid: members for gid, members in groups.items() if len(members) > 1}
    if not groups:
        return nodes
    merged_by_group, merged_id_of = {}, {}
    for gid, members in groups.items():
        states = [m.get("state") for m in members]
        if "failed" in states:
            state = "failed"
        elif "active" in states:
            state = "active"
        elif "reconciling" in states:
            state = "reconciling"
        elif all(s == "done" for s in states):
            state = "done"
        elif all(s == "pending" for s in states):
            state = "pending"
        else:
            state = "active"
        member_ids = {m.get("id") for m in members}
        deps = []
        for m in members:
            for parent in m.get("depends_on") or ():
                if parent not in member_ids and parent not in deps:
                    deps.append(parent)
        elapsed = [m.get("elapsed_min") for m in members
                   if m.get("elapsed_min") is not None]
        merged = dict(members[0])
        merged.update({
            "id": "%s(%d-way)" % (gid, len(members)), "state": state,
            "depends_on": deps,
            "level": min(m.get("level") or 0 for m in members),
            # Legs may sit on different harnesses/models by design — no single
            # unit/model represents the group.
            "unit": None, "unit_choices": [], "model": None, "effort": None,
            "elapsed_min": max(elapsed) if elapsed else None,
            "gate_passed": True if all(m.get("gate_passed") for m in members) else None,
        })
        merged_by_group[gid] = merged
        for m in members:
            if m.get("id"):
                merged_id_of[m["id"]] = merged["id"]
    out, emitted = [], set()
    for node in nodes:
        group = node.get("parallel_group") or node.get("replica_group")
        if group in merged_by_group:
            if group not in emitted:
                emitted.add(group)
                out.append(merged_by_group[group])
            continue
        deps = []
        for parent in node.get("depends_on") or ():
            renamed = merged_id_of.get(parent, parent)
            if renamed not in deps:
                deps.append(renamed)
        if deps != list(node.get("depends_on") or ()):
            node = dict(node)
            node["depends_on"] = deps
        out.append(node)
    return out


# One-window callable compatibility for existing Fleet extensions/tests.
_collapse_replica_nodes = _collapse_parallel_nodes


def _projection_route_seq(entity):
    """Return the attached route's record-order node sequence, if validated."""
    projection = getattr(entity, "work_projection", None)
    if not projection or getattr(projection, "source", None) != "route-exact":
        return None
    backing = getattr(projection, "_route_view", None) or {}
    view = backing.get("view") or {}
    return [(node.get("id"), node.get("state"))
            for node in _collapse_parallel_nodes(view.get("nodes") or ())]


_LEGACY_STAGE_COLOR_INDEX = {
    "code-plan": 0,
    "code-execute": 1,
    "code-test": 2,
    "code-report": 3,
    "stage-search": 0,
    "stage-analyze": 1,
    "stage-report": 2,
}


def _dispatch_stage_color_index(entity, key=None, stage=None):
    """F-42b — color a dispatch-depth-2 micro-status with its own stage.

    A validated route owns the index, including a collapsed parallel group.  Legacy rows fall
    back to their stage contract/key; zero is only the final no-evidence fallback.  The helper
    returns an index rather than a color key so blink only changes brightness, never hue.
    """
    projection = getattr(entity, "work_projection", None)
    if projection and getattr(projection, "source", None) == "route-exact":
        backing = getattr(projection, "_route_view", None) or {}
        view = backing.get("view") or {}
        raw_nodes = list(view.get("nodes") or ())
        selected_id = (getattr(projection, "route_node", None)
                       or getattr(entity, "route_node", None))
        selected = next((node for node in raw_nodes if node.get("id") == selected_id), None)
        selected_group = ((selected or {}).get("parallel_group")
                          or (selected or {}).get("replica_group"))
        for i, node in enumerate(_collapse_parallel_nodes(raw_nodes)):
            node_group = node.get("parallel_group") or node.get("replica_group")
            if node.get("id") == selected_id or (selected_group and node_group == selected_group):
                return i % 5

    identities = (
        getattr(entity, "assigned_contract", None),
        key,
        getattr(entity, "worker_role", None),
    )
    for identity in identities:
        base = str(identity or "").partition(":")[0]
        if base in _LEGACY_STAGE_COLOR_INDEX:
            return _LEGACY_STAGE_COLOR_INDEX[base] % 5

    label = _dispatch_stage_label(entity) or stage
    for seq in _PIPE_STAGES.values():
        if label in seq:
            return seq.index(label) % 5
    return 0


def _projection_stage_for_dispatch(entity):
    """Select a row stage from its projection with a bounded legacy fallback."""
    projection = getattr(entity, "work_projection", None)
    if projection is None:
        return getattr(entity, "stage", None) or ""
    if getattr(projection, "ambiguity", None) or getattr(projection, "source", None) == "registry-exact":
        return ""
    return getattr(projection, "stage_label", None) or (
        getattr(entity, "stage", None) if not getattr(projection, "route_id", None) else ""
    ) or ""


def _unused_badge(s, compact=False):
    """`unused <age>` — F-26's first-class signal (prd.md:248). Says the session was started
    and never prompted, and for how long it has sat that way. Age comes from the process, not
    the registry clock (the registry's updatedAt froze at startup — that IS the finding).

    compact drops the age. Two of F-26's requirements collide on a tight row: the badge is
    specified as `unused <경과>` (prd.md:248) but prd.md:247 also demands no anonymous rows,
    and the badge is what starves the name. The age is the recoverable half — every layout
    already carries elapsed time in its own time cell — so it yields first, and only when the
    name would otherwise be clipped."""
    if compact:
        return " unused"
    return " unused %s" % fmt_min(s.elapsed_min if s.elapsed_min is not None else None)


def _interaction_badge(s):
    state = getattr(s, "interaction_state", None)
    label = _INTERACTION_LABEL.get(state.get("kind")) if isinstance(state, dict) else None
    return " " + label if label else ""


def _interaction_chip(s):
    """F-60: the blocked session's interaction badge, drawn as a REVERSE-VIDEO chip.

    Returns `(segs, width)` — `([], 0)` when the session has no interaction kind to name.

    The chip is where F-60's emphasis lives. The separator space stays OUTSIDE the reversed
    run and the label is padded inside it, so the reversed cells form one solid block instead
    of a run of inverted letters butting against the title. The glyph and the context lead
    word deliberately do NOT get this treatment: they carry the same `g_blocked` red, and a
    second reversed cell on the row would read as two competing chips rather than one.
    """
    label = _interaction_badge(s).strip()
    if not label:
        return [], 0
    chip = " %s " % label
    return [(" ", None), (chip, "g_blocked_chip")], 1 + _dw(chip)


def _session_row(s, narrow, is_parent=False, child_count=0, name_width=None,
                 show_projection_stage=True, stage_zone=None):
    live = s.liveness
    slug = s.slug or (s.cwd.rsplit("/", 1)[-1] if s.cwd else "?")
    dim_tel = live in ("stale", "dead") or s.app_server or s.detached
    dead_stale = live in ("stale", "dead")   # F-13: telemetry gone, replaced with a single age cell
    name_key = ("name_work" if live == "working"
                else ("name_dim" if dim_tel else "name_idle"))
    gch, gkey = _glyph(live)
    if s.detached and live not in ("stale", "dead"):
        gch, gkey = _DETACHED_GLYPH, "g_work_off"   # detached: loading axis, dim-green
    # main↔spawned weight = font-color intensity (no bg fill — the reverse badge read as weird):
    # a live top-level session gets the BRIGHT harness color; muted (stale/dead/app-server) drops
    # to dim. Dispatch rows use the DIM harness color (see _dispatch_row).
    hkey = (_BADGE_KEY.get(s.harness, "dim") if dim_tel
            else ("hb_" + s.harness if s.harness in _BADGE_TEXT else "hb_other"))
    # F-33 (v11): harness field carries model/effort as a parenthetical — a dead/stale row has
    # no live telemetry to show (F-13), so it renders the bare harness name only.
    segs = [("  ", None), (gch, gkey), (" ", None)]
    segs += _harness_model_cell(s.harness, None if dead_stale else s.model,
                                None if dead_stale else s.effort, _HMW, hkey, dim=dim_tel)

    # F-22: reserve identity suffixes first, then let the title consume the
    # responsive name column. Calls without a terminal-derived width retain the
    # legacy 24-cell cap for hermetic/backward-compatible row construction.
    avail = max(3, name_width or _NW_S)
    name_txt = _session_name(s)
    suffix = []
    suffix_w = 0
    if is_parent and child_count:
        child_tag = " ▾%d" % child_count
        if _dw(child_tag) < avail:
            suffix.append((child_tag, "dim"))
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
    if unused_at is not None and avail - suffix_w - _NAME_GAP < _dw(name_txt):
        short = (_unused_badge(s, compact=True), "g_unused_b")
        suffix_w -= _dw(suffix[unused_at][0]) - _dw(short[0])
        suffix[unused_at] = short
    title_budget = max(1, avail - suffix_w - _NAME_GAP)
    if name_width is None:
        title_budget = min(title_budget, _TITLE_MAX)
    shown = _clip_w(name_txt, title_budget)
    segs.append((shown, name_key))
    used = _dw(shown)
    for text, key in suffix:
        segs.append((text, key))
        used += _dw(text)
    branch_segs = _branch_suffix_segs(s.cwd, s.branch, dim=dim_tel)
    segs += branch_segs
    used += sum(_dw(text) for text, _key in branch_segs)
    session_width = avail + _BRANCH_SUFFIX_W
    if used < session_width:
        segs.append((" " * (session_width - used), None))

    segs.append((" " * _WIDE_STAGE_GAP, None))
    if show_projection_stage:
        # Responsive stage zone (2026-07-24): the old flat label was hard-clipped at 24 cells and
        # truncated to `…` even on a wide terminal with a mostly-empty zone (user "stage 폭 엄청
        # 길잖아 근데 왜 금방 … 표시로 줄이는지"). Use the same terminal-aware budget the conductor
        # breadcrumb got, so a spec phase breadcrumb / long label fills the real space first.
        segs += _session_stage_segs(s, live == "working", stage_zone or _STAGE_ZONE_MAX)
    else:
        segs.append(("-", "dim"))
    if dead_stale:
        # F-13: a stale/dead row has no live model/effort/ctx to show — a wall of "—" placeholders
        # read as broken telemetry rather than "this session stopped". One `done <age>` cell
        # replaces the whole model+gauge zone (LIVE rows keep the explicit "—" convention, F-3).
        # F-64 (v49): "last seen" → "done" — the finished state reads symmetric with the live
        # "running" vocabulary instead of sounding like lost telemetry (user 2026-08-05).
        age_min = int((time.time() - s.mtime) / 60) if s.mtime else (s.elapsed_min or 0)
        segs += [("  ", None), ("done %s" % fmt_min(age_min), "dim")]
    if s.app_server:
        segs.append(("  app-server", "dim"))
    if s.orphan:
        segs.append(("  worktree-gone", "g_dead"))

    segs += [(" " * _WIDE_TIME_GAP, None), (_RFLUSH, None)]          # uptime flush right
    # Cost display intentionally omitted.
    segs += [(_CLOCK, "dim"), ("%6s" % fmt_min(s.elapsed_min), "dim")]
    return segs


_DISPATCH_NAME_MAX = 18


def _compact_dispatch_name(name, max_width=_DISPATCH_NAME_MAX):
    if not name or len(name) <= max_width:
        return name or ""
    if max_width <= 1:
        return name[:max_width]
    return name[: max_width - 1] + "…"


def _dispatch_prefix(j, orphan=False):
    # F-64c (v49, user 2026-08-05 연쇄 "depth=1을 조금 앞당겨서" → "세로선은 좀 더 들여쓰게"
    # → "아주 조금만 더 + depth=2의 들여쓰기를 완화"): dispatch-depth-1 sheds its ↳ arrow and sits at
    # a 4-cell inset — the capsule rail (assembler post-pass at `_RAIL_COL`) is what marks
    # the unit. depth≥2 keeps the ↳ spawn arrow (user 2026-07-16: indent-only dispatch-depth-2 rows
    # lost their arrow), TWO cells deeper per level past the dispatch-depth-1 seat — the rail now
    # carries the unit boundary, so the ladder no longer needs the 3-cell step. The harness
    # field absorbs the prefix (_HMW - len: dispatch-depth-1 40-4=36, dispatch-depth-2 40-8=32 ≥ the 26-cell
    # worst-case live label).
    #
    # Project-level orphans (parent dead/off-screen, surfaced as a project fallback) intentionally
    # drop the ↳ tree arrow — a parent-child arrow with no on-screen parent misleads readers into
    # attaching the orphan to whatever live session row happens to sit above it. Replace with a
    # same-width flat `··` "no-hierarchy" mark so the column stays aligned without implying nesting.
    depth = max(1, min(3, int(getattr(j, "depth", 1) or 1)))
    if depth == 1:
        return "··  " if orphan else "    "
    marker = "··" if orphan else "↳ "
    return "    " + "  " * (depth - 1) + marker


# F-64c (v49, user 2026-08-05 "depth=1에서는 화살표를 안쓰고 쭉 세로줄로 … 점멸하도록",
# 정정 "depth=1을 조금 앞당겨서 들여쓰기 폭을 줄이고 세로선은 그 아래 depth=2에 대해서만"):
# a dispatch-depth-1 dispatch unit renders arrowless at a shallow 2-cell inset, and a vertical rail
# hangs UNDER it — through the NOW subtitle, ⚡strips, dispatch-depth-2 rows and their details, but
# never on the owner row itself. The rail BLINKS in the owner's stage hue while the owner
# is working (same _BLINK_ON phase as the running token — blink changes brightness, never
# hue) and sits steady dim otherwise. dispatch-depth-2 rows keep their ↳ arrows — rail + arrow is
# what tells a dispatch unit apart from a native sub-agent strip, which carries neither.
# Wide layout only; narrow/stack cards keep prefixes as-is, and orphan rows keep their
# flat `··` (a rail with no on-screen parent would imply lineage F-26 forbids guessing).
# Rail glyphs (user 2026-08-05 "세로바의 가장 위와 가장 아래만 각각 위와 아래가 짧은
# 바"): the run stays a CONNECTED line — full `┃` through the middle — but its first cell
# is the top-short `╻` and its last the bottom-short `╹`, so the capsule has open ends and
# never fuses with the rows above/below the unit. A one-row run uses the standalone short
# bar `❙` (both ends open; box-drawing has no middle-only heavy segment).
_RAIL_TOP = "╻"
_RAIL_MID = "┃"
_RAIL_BOT = "╹"
_RAIL_SOLO = "❙"
_RAIL_CHARS = (_RAIL_TOP, _RAIL_MID, _RAIL_BOT, _RAIL_SOLO)
_RAIL_COL = 4      # two steps in from the card edge (user 2026-08-05 "세로선은 좀 더
                   # 들여쓰게" → "아주 조금만 더") — the dispatch-depth-1 seat moved to a 4-cell
                   # inset with it, so one cell of air stays between the rail and the
                   # owner's glyph and the capsule still brackets the WHOLE unit, owner
                   # row included


def _depth1_rail_color_index(key, stage, route_seq):
    """F-64c: the rail wears the SAME hue as the conductor breadcrumb's current token —
    route rows share `_route_current_index`, legacy rows the `_PIPE_STAGES` position, and
    everything else (`preparing…`/queued/raw stage) lands on the breadcrumb's stg0."""
    if route_seq is not None and route_seq:
        return _route_current_index(route_seq) % 5
    seq = _PIPE_STAGES.get(key)
    if seq and stage in seq:
        return seq.index(stage) % 5
    return 0


def _overwrite_rail_cell(segs, col, char, key):
    """Overwrite ONE display cell of a row with the rail mark, splitting whatever seg
    covers it. Fail closed: if the target cell holds real content (not a space or the
    dispatch-depth-1 ↳ the rail replaces), return the row untouched."""
    out, pos, replaced = [], 0, False
    for text, k in segs:
        width = _dw(text) if text else 0
        if replaced or pos + width <= col:
            out.append((text, k)); pos += width; continue
        acc, i = 0, 0
        while i < len(text) and pos + acc + _cw(text[i]) <= col:
            acc += _cw(text[i]); i += 1
        if i >= len(text) or text[i] not in (" ", "↳"):
            return segs
        if text[:i]:
            out.append((text[:i], k))
        out.append((char, key))
        if text[i + 1:]:
            out.append((text[i + 1:], k))
        pos += width
        replaced = True
    return out if replaced else segs


_ORPHAN_DIVIDER_LABEL = "  ⌄ orphaned dispatch rows"


def _orphan_divider(term_width=None):
    """One dim in-band rule above a group card's project-level orphan rows.

    Silent by default — only emitted when the group actually has orphaned
    dispatch rows, so live-only groups render unchanged. Mirrors `_mem_divider`'s
    in-band rule convention so the body-tint loop can tint it like any other row.
    """
    width = term_width or 78
    rule = "─" * max(8, width - len(_ORPHAN_DIVIDER_LABEL) - 1)
    return [(_ORPHAN_DIVIDER_LABEL, "dim"), (" ", None), (rule, "dim")]


_LEVEL_SHORT = {
    "direct": "direct",
    "quick": "q",
    "light": "lt",
    "standard": "std",
    "strong": "strong",
    "thorough": "thr",
    "adversarial": "adv",
}

_ROLE_SHORT = {
    "capability-owner": "owner",
    "deep-reviewer": "review",
    "fast-reviewer": "review",
    "fast-implementer": "impl",
    "dev-refactor": "impl",
    "dev-new-lib": "impl",
    "research-survey": "research",
}

# drill/loop case ids (g6_worktree_dispatch, g9_cross_harness_depth2_dispatch, g8b_...) shrink
# to their gN prefix by a GENERAL rule instead of a per-case hardcoded entry above (F-9(b) —
# each new drill case used to need a code change here).
_G_CASE_PREFIX = re.compile(r"^(g\d+[a-z]?)")


def _short_level(value):
    if not value:
        return ""
    prefix = "~" if str(value).startswith("~") else ""
    raw = str(value)[1:] if prefix else str(value)
    return prefix + _LEVEL_SHORT.get(raw, raw)


def _short_role(value):
    if not value:
        return ""
    label, suffix = _stage_role_label(value)
    if label is not None:
        # stage suffix (":phase-A") rides along as part of the same dim profile tag — the
        # whole tag already renders "dim" (see _opts_segs), so no separate color key is needed.
        return _compact_dispatch_name(label + suffix, 14)
    m = _G_CASE_PREFIX.match(value)
    role = _ROLE_SHORT.get(value) or (m.group(1) if m else value.replace("-", "_"))
    return _compact_dispatch_name(role, 14)


_PROFILE_MAX = 28


def _strip_autopilot(name):
    if name and name.startswith("autopilot-"):
        return name[len("autopilot-"):]
    return name


def _is_owner_mode_row(j):
    worker_role = getattr(j, "worker_role", None) or ""
    return bool(
        getattr(j, "worker_type", None) == "owner"
        or getattr(j, "unit", None) == "_kernel/owner"
        or worker_role.endswith("orchestrator")
    )


def _mode_axis_conflict(j):
    legacy = getattr(j, "mode", None) or ""
    worker_mode = getattr(j, "worker_mode", None)
    return bool(
        getattr(j, "mode_axis_conflict", False)
        or (
            _is_owner_mode_row(j)
            and (worker_mode or "/" in legacy)
        )
    )


def _display_capability_mode(j):
    """Return only an honest owner-capability mode for the options dial."""

    current = getattr(j, "capability_mode", None)
    if current:
        return current
    if _mode_axis_conflict(j):
        return None
    legacy = getattr(j, "mode", None)
    if getattr(j, "key", None) in _LOOPS_KEYS:
        return legacy
    return legacy if legacy and "/" not in legacy else None


def _entry_skill(j):
    """Skill identity for the options dial.

    A dispatch-depth-1 row names its entry capability. A dispatch-depth-2 row instead names the
    assigned stage: route node when present, otherwise the registered capability
    key. This prevents inherited owner mode (``dev/refactor``) from masquerading
    as the child's own work.
    """
    if max(1, int(getattr(j, "depth", 1) or 1)) >= 2:
        owner = getattr(j, "capability_owner", None)
        contract = getattr(j, "assigned_contract", None)
        assigned = (
            contract
            if contract and _strip_autopilot(contract) != _strip_autopilot(owner or "")
            else getattr(j, "route_node", None) or getattr(j, "key", None)
        )
        return _compact_dispatch_name(_strip_autopilot(assigned), _PROFILE_MAX)
    cap = _strip_autopilot(getattr(j, "capability_owner", None) or "")
    key = getattr(j, "key", None)
    skill = cap or (key if key in _PIPE_STAGES else None)
    if not skill:
        return None
    projected_mode = (
        getattr(j, "worker_mode", None)
        or getattr(j, "mode", None)
        or ""
    )
    if skill in projected_mode.split("/"):
        return None
    return skill


def _dispatch_role_suffix(j, max_width=None):
    # qa is data-only now (kept in --json): the retired qa axis left the display entirely
    # (user 2026-07-16 — rigor derives from intensity, CONVENTIONS §1.1).
    worker_type = getattr(j, "worker_type", None)
    raw_role = worker_type if worker_type in {"owner", "stage", "review", "support"} else getattr(j, "worker_role", None)
    if getattr(j, "key", None) in _LOOPS_KEYS and raw_role == getattr(j, "slug", None):
        raw_role = None
    # A capability name is not a role (user 2026-07-20: "code가 표시가 되어있는데 굳이
    # autopilot-code가 왜 표시되는지") — a worker_role that restates the job's own key/
    # capability_owner drops, and a '<capability>-<role>' composite ('autopilot-code-owner')
    # keeps only its role tail instead of rendering as 'autopilot_cod…'.
    # Writer-vocabulary normalization (user 2026-07-20: "codex랑 claude가 서로 다르게 뜨는데")
    # — legacy rows may misfile PORTABLE MODEL-ROLE phrases ('deep orchestrator',
    # 'deep maker') and retired team metadata ('plan-team') into worker_role. Current writers
    # leave worker_role unset and use the independent fields above. Legacy harness role tokens
    # are always kebab, so
    # a space marks the model-role phrase: the orchestrator phrase IS the conductor
    # ('owner'), every other model-role/persona restates an axis the row already shows
    # (model cell / name-zone stage label) and drops. Display-only; the registry row keeps
    # the raw value.
    if raw_role and " " in raw_role:
        raw_role = "capability-owner" if raw_role.endswith("orchestrator") else None
    if raw_role and raw_role.endswith("-team"):
        raw_role = None
    if raw_role:
        bare = _strip_autopilot(raw_role)
        dups = [d for d in (_strip_autopilot(getattr(j, "capability_owner", None) or "") or None,
                            getattr(j, "key", None)) if d]
        if bare in dups:
            raw_role = None
        else:
            for d in dups:
                if bare.startswith(d + "-"):
                    raw_role = bare[len(d) + 1:]
                    break
    role = _short_role(raw_role)
    # intensity left this suffix for the dial's paren knob group (user 2026-07-20
    # hierarchical dial, _opts_segs) — it is a BEHAVIOUR axis, not part of the
    # environment tail this function feeds.
    if not role:
        return ""
    if max_width is not None and len(role) > max_width:
        # F-9(c): drop the whole component instead of tail-cutting it mid-token.
        return ""
    return role


def _dispatch_profile(j):
    # environment tail ONLY now — the worker role left for the dial's paren knob group
    # (user 2026-07-20: "owner의 위치가 애매" — 'boot/owner' read as one env path).
    profile = getattr(j, "profile", None)
    return _compact_dispatch_name(profile, _PROFILE_MAX) if profile else None


def _dispatch_model_profile(j):
    profile = getattr(j, "model_profile", None)
    if not profile or profile == "unsealed":
        return None
    reduced = getattr(j, "profile_granularity", None) not in {None, "", "full"}
    label = "mp:" + str(profile) + ("~" if reduced else "")
    return _compact_dispatch_name(label, _PROFILE_MAX)

def _dispatch_stage_label(j):
    """(label_prefix, is_stage_worker) — the dispatch-depth-2 stage-role label ('exec', 'plan'...) that
    now identifies a dispatch-depth-2 child in the NAME zone (F-15a P0-1: identity lives here, not in a
    duplicated breadcrumb). dispatch-depth-1 conductors/orphans have no such label — their identity is
    just their own slug."""
    if max(1, int(getattr(j, "depth", 1) or 1)) < 2:
        return None
    route_node = getattr(j, "route_node", None)
    if route_node:
        return _compact_dispatch_name(route_node, 14)
    label, suffix = _stage_role_label(getattr(j, "assigned_contract", None))
    if label is None:
        label, suffix = _stage_role_label(getattr(j, "key", None))
    if label is None:
        # Legacy rows may only carry the old overloaded worker_role.
        label, suffix = _stage_role_label(getattr(j, "worker_role", None))
    if label is None:
        return None
    return label + suffix


def _opts_segs(j):
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
    entry = _entry_skill(j)
    if depth >= 2:
        knob_items = [t for t in (_short_level(getattr(j, "intensity", None)),) if t]
    else:
        knob_items = [t for t in (
            _display_capability_mode(j),
            _short_level(getattr(j, "intensity", None)),
        ) if t]
    # the worker ROLE is a behaviour knob too (who the worker acts as), not environment —
    # it rides the paren group's last slot (user 2026-07-20: "owner의 위치가 애매").
    role = "" if depth >= 2 else _dispatch_role_suffix(
        j, max_width=max(0, _PROFILE_MAX - sum(len(t) + 1 for t in knob_items)))
    if role:
        knob_items.append(role)
    knobs = "·".join(knob_items)
    tail = " / ".join(
        value for value in (_dispatch_model_profile(j), _dispatch_profile(j)) if value
    ) or None
    unit = getattr(j, "unit", None)
    projected_unit = unit or (
        None if _is_owner_mode_row(j) else getattr(j, "worker_mode", None)
    )
    if projected_unit:
        display_unit = str(projected_unit)
        if display_unit.startswith("_"):
            display_unit = display_unit[1:]
        tail = " / ".join(value for value in (tail, "unit:" + _compact_dispatch_name(display_unit, _PROFILE_MAX)) if value)
    if _mode_axis_conflict(j):
        tail = " / ".join(value for value in (tail, "mode!") if value)
    contract_status = getattr(j, "attempt_contract_status", None)
    if contract_status == "legacy-read-only":
        tail = " / ".join(value for value in (tail, "legacy") if value)
    elif contract_status and contract_status != "current":
        tail = " / ".join(value for value in (tail, "contract!") if value)
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
                  route_zone=None):
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
    stage = stage_override if stage_override is not None else (j.stage or "")
    # The dispatched session's own haiku sidecar title is its identity when present
    # (user 2026-07-16: the summary agent attaches to every dispatched session); the
    # slug stays the fallback — same title → name → slug chain as session rows.
    slug_name = getattr(j, "title", None) or j.slug or key
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
    prefix = _dispatch_prefix(j, orphan=orphan)
    segs = [("  ", None), (prefix, "dim"), (gch, gkey), (" ", None)]
    segs += _harness_model_cell(j.harness,
                                None if j.liveness == "dead" else (j.model or parent_model),
                                eff, max(1, _HMW - len(prefix)),
                                _BADGE_KEY.get(j.harness, "dim"), dim=True, unknown="—")
    avail = max(3, name_width or _NW_S)
    otag = "  (orphan)" if orphan else ""
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
    used = len(nm)
    # user 2026-07-20: 분사 행 제목도 컬러 — the dim HARNESS hue, so the title row stays
    # dark but no longer reads identical to its grey subtitle line underneath. Dead/stale
    # rows keep the colorless dim (F-13: no live telemetry, nothing to tint).
    name_key_j = "name_dim" if dead_stale_j else _BADGE_KEY.get(j.harness, "name_dim")
    segs.append((nm, name_key_j))
    if otag and used + len(otag) <= avail:
        segs.append((otag, "gate_u")); used += len(otag)
    branch_segs = _branch_suffix_segs(
        "" if key in _LOOPS_KEYS else j.cwd, j.branch, optional=key in _LOOPS_KEYS)
    segs += branch_segs
    used += sum(_dw(text) for text, _key in branch_segs)
    session_width = avail + _BRANCH_SUFFIX_W
    if used < session_width:
        segs.append((" " * (session_width - used), None))

    if j.liveness == "dead":
        segs.append((" " * _WIDE_STAGE_GAP, None))
        if getattr(j, "note", None) == "dead-parent-orphaned":
            # SD-64/71: distinct from the generic dead-conductor cell — never blank, and
            # always names the exact node a dispatch-depth-0 decision would resume from.
            boundary = getattr(j, "resume_boundary", None) or "-"
            segs.append(("⚠ ORPHANED resume=%s" % boundary, "g_dead"))
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
        opt_segs, optw = _opts_segs(j)
        segs += opt_segs
        if optw < _OPTW:
            segs.append((" " * (_OPTW - optw), None))
        # user 2026-08-05 "done 앞에 콜론은 그대로 있어야지 체크 표시는 done 뒤로": the
        # done token rides the same ` : ` stage-zone lead-in the running token uses, and
        # the check TRAILS the word — `: done ✓`, mirroring `: running`.
        if int(getattr(j, "depth", 1) or 1) >= 2:
            token = "done %s" % _LIVE_GLYPH["done"]
        elif afterglow_j:
            token = "done %s %s" % (_LIVE_GLYPH["done"], fmt_min(j.elapsed_min))
        else:
            token = "done %s" % fmt_min(j.elapsed_min)
        segs += _stage_zone_segs([(token, "dim")])
    else:
        # F-15a options column (fixed-ish gap, dim mode/qa/profile) — a declutter move OUT of
        # the name zone, not a new axis. model/effort now live in the harness field (F-4/SD-F3).
        segs.append((" " * _WIDE_STAGE_GAP, None))
        opt_segs, optw = _opts_segs(j)
        segs += opt_segs
        if optw < _OPTW:
            segs.append((" " * (_OPTW - optw), None))

        segs += _stage_zone_segs(
            _dispatch_stage_segs(j, key, stage, slug_name, working=(j.liveness == "working"),
                                 route_seq=route_seq, route_zone=route_zone))

    segs += [(" " * _WIDE_TIME_GAP, None), (_RFLUSH, None)]
    segs += [(_CLOCK, "dim"), ("%6s" % fmt_min(j.elapsed_min), "dim")]
    return segs


# ---------- 2-line cards (round-4 responsive narrow mode) ----------
# L1 = identity (dot · harness · session (branch) · ▾N · gate) / L2 = telemetry (model · effort ·
# bracket gauge · cost · ⏱). model keeps its fixed width so gauges align vertically across cards
# (the nvtop column feel). Same segment parts as the 1-line rows — zero new color keys.
def _session_row_2line(s, is_parent=False, child_count=0, _split=False, term_width=None,
                       show_projection_stage=True):
    live = s.liveness
    slug = s.slug or (s.cwd.rsplit("/", 1)[-1] if s.cwd else "?")
    dim_tel = live in ("stale", "dead") or s.app_server or s.detached
    name_key = ("name_work" if live == "working"
                else ("name_dim" if dim_tel else "name_idle"))
    gch, gkey = _glyph(live)
    if s.detached and live not in ("stale", "dead"):
        gch, gkey = _DETACHED_GLYPH, "g_work_off"
    hn = _BADGE_TEXT.get(s.harness, "?")
    hkey = (_BADGE_KEY.get(s.harness, "dim") if dim_tel
            else ("hb_" + s.harness if s.harness in _BADGE_TEXT else "hb_other"))
    l1 = [("  ", None), (gch, gkey), (" ", None), (_pad(hn, _HW), hkey)]
    suffix = []
    if is_parent and child_count:
        suffix.append((" ▾%d" % child_count, "dim"))
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
    br_segs = _branch_suffix_segs(s.cwd, s.branch, dim=dim_tel, optional=True)
    if not _split:
        suffix.extend(br_segs)
    if s.app_server:
        suffix.append(("  app-server", "dim"))
    if s.orphan:
        suffix.append(("  worktree-gone", "g_dead"))
    name_txt = _session_name(s)
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
        title_budget = max(4, avail - suffix_w)
    else:
        if prov_seg:
            suffix.insert(prov_pos, prov_seg)
        title_budget = min(_NAME2_MAX, _TITLE_MAX)
    l1.append((_clip_w(name_txt, title_budget), name_key))
    l1.extend(suffix)

    # L2: elapsed time sits UNDER the harness column (fills the old empty indent — user
    # Put time under the harness, model under the name, and gauge immediately after.
    # indent / no far-right flush).
    l2 = [("    ", None), (_pad(fmt_min(s.elapsed_min), _HW), "dim")]
    l2 += _model_cell(s.model, s.effort, _MW, dim=dim_tel)
    projection_stage = (_projection_stage_text(s, max_width=28)
                        if show_projection_stage else "")
    l2 += [("  ", None), (projection_stage or "-",
                           "g_work" if projection_stage and live == "working" else "dim")]
    # v16: context is emitted by _context_detail_row beneath the complete card.
    if _split:
        return l1, l2, br_segs
    return l1, l2


def _stack_split(l2):
    """Index where the gauge/stage part of a narrow L2 begins (the '[' of the bracket meter,
    a stage-track segment, or the 'key: ' label) — the ultra-narrow card breaks there."""
    for i, (t, k) in enumerate(l2):
        if (t == "[" and k == "dim") or (isinstance(k, str) and k.startswith("stg")) \
                or (k == "name_dim" and t.endswith(": ")):
            return i
    return len(l2)


def _session_row_stack(s, is_parent=False, child_count=0, term_width=None,
                       show_projection_stage=True):
    """v16 ultra-narrow card: identity and telemetry, with detail row emitted separately."""
    l1, l2 = _session_row_2line(
        s, is_parent, child_count, term_width=term_width,
        show_projection_stage=show_projection_stage)
    return [l1, l2]


def _dispatch_row_stack(j, orphan=False, parent_model=None, parent_effort=None, stage_override=None,
                        route_seq=None):
    l1, l2 = _dispatch_row_2line(j, orphan=orphan, parent_model=parent_model,
                                 parent_effort=parent_effort, stage_override=stage_override,
                                 route_seq=route_seq)
    gi = _stack_split(l2)
    return [l1, l2[:gi], [(" " * (4 + _HW), None)] + l2[gi:]]


def _dispatch_row_2line(j, orphan=False, parent_model=None, parent_effort=None, _split=False,
                        stage_override=None, route_seq=None):
    """F-15a narrow card — L1 = identity ONLY (stage label + slug, no mode/qa tag); L2 =
    elapsed · model · options (relocated from L1) · breadcrumb/micro-status."""
    key = j.key or "?"
    depth = max(1, int(getattr(j, "depth", 1) or 1))
    # The dispatched session's own haiku sidecar title is its identity when present
    # (user 2026-07-16: the summary agent attaches to every dispatched session); the
    # slug stays the fallback — same title → name → slug chain as session rows.
    slug_name = getattr(j, "title", None) or j.slug or key
    gch, gkey = _glyph(j.liveness, dim=True)
    afterglow_j = bool(getattr(j, "afterglow", False))
    if afterglow_j:
        gch, gkey = _LIVE_GLYPH["done"], "dim"   # F-46 parity with the wide row
    hn = _BADGE_TEXT.get(j.harness, "—") if j.harness else "—"

    prefix = _dispatch_prefix(j, orphan=orphan)
    label = _dispatch_stage_label(j)
    if label:
        slug_room = max(1, _DISPATCH_NAME_MAX - len(label) - 1)
        shown_name = label + " " + _compact_dispatch_name(slug_name, slug_room)
    else:
        shown_name = _compact_dispatch_name(slug_name)
    # user 2026-07-20: 분사 행 제목도 컬러 (dim harness hue) — same rule as the wide row.
    name_key_j = ("name_dim" if (j.liveness in ("dead", "stale") or afterglow_j)
                  else _BADGE_KEY.get(j.harness, "name_dim"))
    l1 = [("  ", None), (prefix, "dim"), (gch, gkey), (" ", None),
          (_pad(hn, max(1, _HW - len(prefix))), _BADGE_KEY.get(j.harness, "dim")), (shown_name, name_key_j)]
    if orphan:
        l1.append(("  (orphan)", "gate_u"))
    br_segs = _branch_suffix_segs(
        "" if key in _LOOPS_KEYS else j.cwd, j.branch, optional=True)
    if not _split:
        l1.extend(br_segs)

    stage = stage_override if stage_override is not None else (j.stage or "")
    if afterglow_j:
        # F-46 as corrected by F-64b: the L2 line keeps the model cell (static identity,
        # not telemetry) and swaps only the stage slot for a steady `: done ✓` token
        # (check trailing, same lead-in as `: running`). L2 already leads with the
        # elapsed cell, so no depth needs it repeated (F-64a).
        eff = j.effort or parent_effort or None
        l2 = [("    ", None), (_pad(fmt_min(j.elapsed_min), _HW), "dim")]
        l2 += _model_cell(j.model or parent_model, eff, _MW, dim=True)
        l2.append(("    ", None))
        l2 += _stage_zone_segs([("done %s" % _LIVE_GLYPH["done"], "dim")])
    else:
        eff = j.effort or parent_effort or None
        l2 = [("    ", None), (_pad(fmt_min(j.elapsed_min), _HW), "dim")]
        l2 += _model_cell(j.model or parent_model, eff, _MW, dim=True)
        l2.append(("    ", None))
        opt_segs, optw = _opts_segs(j)
        l2 += opt_segs
        if optw < _OPTW:
            l2.append((" " * (_OPTW - optw), None))
        l2 += _stage_zone_segs(
            _dispatch_stage_segs(j, key, stage, slug_name, working=(j.liveness == "working"),
                                 route_seq=route_seq))
    if _split:
        return l1, l2, br_segs
    return l1, l2


# ---------- grouping assembler ----------
def _group_key_session(s):
    return project_of(s.cwd)


def _mem_row(s, layout="wide"):
    """Render a dim one-line memory worker, hidden unless ``a`` is toggled."""
    name = _clip_w(s.title or s.slug or (s.harness or "?"), 40)
    seg = [("  🧠 ", "dim"), ("mem ", "dim"),
           (name, "dim"), ("  ", None),
           ((s.harness or "—"), "dim"), ("  ", None),
           (fmt_min(s.elapsed_min), "dim")]
    return [seg]


_GOVERNOR_QUIET_FRACTION = 0.5   # F-28c "healthy 무음" (prd.md:288/311, plan §6a) — hide the row
                                  # below half the cap. Real observed live state (this cycle):
                                  # 1 active lease / cap 5 = 20% — comfortably below half, so a
                                  # single background lease (the normal steady-state) stays
                                  # silent; the row only earns its place once congestion is
                                  # actually worth a glance.


def _governor_segs():
    """`  ⚙ governor N/cap` — F-28c (prd.md:288/311). Pulse-ADJACENT, never merged into the
    pulse row's own session/job counts (I8 — this is a wholly separate line/collector). `None`
    (source absent OR healthy-quiet) = caller omits the row entirely, same "zero lines when
    healthy" contract the alert strip already uses."""
    try:
        from .collectors import governor
        g = governor.collect()
    except Exception:
        g = None
    if not g:
        return None
    active, cap = g.get("active", 0), g.get("cap", 0)
    if cap <= 0 or active < cap * _GOVERNOR_QUIET_FRACTION:
        return None
    return [("  ⚙ ", "dim"), ("governor %d/%d" % (active, cap), "dim")]


def _pulse_segs(sessions, jobs):
    """`  fleet ⠙ N working   ● N idle ...` — whole-board census. Extracted (F-30, v10) so both
    the group view and the process view (§5.1) render the EXACT same row — one source, shared
    by the header helper contract §5.2 asks for, instead of two independently-drifting copies."""
    _real = [s for s in sessions if not s.app_server and not getattr(s, "mem_worker", False)]
    n_wk = sum(1 for s in _real if s.liveness == "working")
    n_id = sum(1 for s in _real if s.liveness == "idle")
    n_un = sum(1 for s in _real if s.liveness == "unused")
    n_dt = sum(1 for s in _real if s.detached and s.liveness not in ("stale", "dead"))
    # F-46: an afterglow row is finished work kept on screen for readability — it is never
    # part of the census (working/idle/job count), exactly as a cooling group is not "hot".
    listed_jobs = [j for j in jobs if not getattr(j, "afterglow", False)]
    if not _SHOW_ALL:
        listed_jobs = [j for j in listed_jobs if j.liveness != "dead"]
    jw = sum(1 for j in listed_jobs if j.liveness == "working")
    spin = _SPIN[int(time.time() * 10) % len(_SPIN)]
    pulse = [("  fleet ", "head"),
             (spin + " %d" % n_wk, "g_spin"), (" working   ", "dim"),
             ("● %d" % n_id, "g_work_off"), (" idle   ", "dim")]
    # F-26: only when there IS one — a healthy board stays quiet (F-12 contract).
    if n_un:
        pulse += [(_LIVE_GLYPH["unused"] + " %d" % n_un, "g_unused"), (" unused   ", "dim")]
    if n_dt:
        pulse += [(_DETACHED_GLYPH + " %d" % n_dt, "g_work_off"), (" detached   ", "dim")]
    if listed_jobs:
        pulse += [("↳ %d" % len(listed_jobs), "dim"),
                  (" job%s (%d working)" % ("s" if len(listed_jobs) != 1 else "", jw), "dim")]
    return pulse


def _mem_summary_segs(memory):
    """F-19 pulse-adjacent summary row — `🧠 mem  +N added(Nw·Nd) · M expired · K pruned ·
    last distill <elapsed>`. Healthy-silent: None when today's journal is empty AND no alert
    fired (mirrors the alert-strip zero-lines-when-healthy convention)."""
    if not memory:
        return None
    today = memory.get("today") or {}
    added = today.get("added", 0)
    expired = today.get("expired", 0)
    pruned = today.get("pruned", 0)
    alerts = memory.get("alerts") or {}
    alert_active = bool(alerts.get("durable_over")) or bool(alerts.get("distill_stale"))
    if not (added or expired or pruned) and not alert_active:
        return None
    last_min = memory.get("last_distill_min")
    seg = [("  🧠 ", "dim"), ("mem  ", "dim"),
           ("+%d added(%dw·%dd)" % (added, today.get("added_working", 0),
                                     today.get("added_durable", 0)), "dim"),
           (" · ", "dim"), ("%d expired" % expired, "dim"),
           (" · ", "dim"), ("%d pruned" % pruned, "dim"),
           (" · ", "dim"),
           ("last distill %s" % (fmt_min(last_min) if last_min is not None else "—"), "dim")]
    return seg


def _mem_event_rows(memory, limit=8):
    """F-19 `a`-toggle detail — most-recent-first dim rows (F-18b dim-row family): time ·
    action · tier/type · actor · snippet."""
    if not memory:
        return []
    rows = []
    for e in (memory.get("recent") or [])[:limit]:
        ts = (e.get("ts") or "—")
        if "T" in ts:
            ts = ts.split("T", 1)[1]   # HH:MM:SS only — date is always "today or recent"
        tier_type = "%s/%s" % (e.get("tier") or "-", e.get("type") or "-")
        snip = e.get("snippet") or ""
        seg = [("  🧠 ", "dim"), (ts, "dim"), ("  ", None),
               (e.get("action") or "?", "dim"), ("  ", None),
               (tier_type, "dim"), ("  ", None),
               (e.get("actor") or "?", "dim")]
        if snip:
            seg += [("  ", None), (_clip_w(snip, 60), "dim")]
        rows.append(seg)
    return rows


_MEM_DIVIDER_MARGIN = 12   # in-band card-bottom divider inset (both sides), matches the
                            # discarded two-plane demo's r5 rule — a dim `─` ON the tint, not
                            # a full-width chrome bar (F-19 repo rows, 사용자 확정 2026-07-16)
_MEM_REPO_ROW_LIMIT = 2
_MEM_REPO_TITLE_W = 22


def _mem_divider(term_width=None):
    """One dim in-band rule above a group card's per-repo mem rows — the tint prefix is
    applied by the caller's existing group-body tint loop (F-19 repo rows)."""
    return [(" ", None), ("─" * max(8, (term_width or 78) - _MEM_DIVIDER_MARGIN), "dim")]


def _mem_repo_rows(events, sid_titles, limit=_MEM_REPO_ROW_LIMIT):
    """F-19 repo rows — a group card's own today-mem events, most-recent-first, dim:
    `🧠 HH:MM ± tier/type actor ⟵ <source session title> "snippet"`. `+` (add) is green,
    `−` (expire/prune) falls back to dim (the engine's only red key is bold-only, and bold
    is reserved for the main-session row — round-2 precedent in the discarded two-plane
    demo). The source session title is shown only when the journal `sid` resolves against a
    currently-known session (honest omission otherwise, F-3)."""
    if not events:
        return []
    from .collectors.memory import ADDED_ACTIONS, EXPIRED_ACTIONS, PRUNED_ACTIONS
    rows = []
    for e in events[:limit]:
        ts = e.get("ts") or "—"
        if "T" in ts:
            ts = ts.split("T", 1)[1][:5]        # HH:MM
        action = e.get("action")
        if action in ADDED_ACTIONS:
            sign, sign_key = "+", "lvl_g"
        elif action in EXPIRED_ACTIONS or action in PRUNED_ACTIONS:
            sign, sign_key = "−", "dim"
        else:
            sign, sign_key = "·", "dim"
        tier_type = "%s/%s" % (e.get("tier") or "-", e.get("type") or "-")
        seg = [("  🧠 ", "dim"), (ts + " ", "dim"), (sign, sign_key),
               (" %s " % tier_type, "dim"), ((e.get("actor") or "?") + " ", "dim")]
        title = sid_titles.get(e.get("sid")) if e.get("sid") else None
        if title:
            seg.append(("⟵ " + _clip_w(title, _MEM_REPO_TITLE_W) + "  ", "dim"))
        snip = e.get("snippet")
        if snip:
            seg.append(('"%s"' % _clip_w(snip, 60), "dim"))
        rows.append(seg)
    return rows


def _mem_alert_bucket(memory):
    """F-19 alert-strip bucket — durable soft-ceiling + distill-silence, appended LAST in the
    dead > stale > ctx > mem priority order (§4.6)."""
    if not memory:
        return None
    alerts = memory.get("alerts") or {}
    parts = []
    over = alerts.get("durable_over") or []
    if over:
        names = []
        for cwd_origin, count in over[:4]:
            label = str(cwd_origin or "?")
            for prefix in ("git:", "root:", "id:"):
                if label.startswith(prefix):
                    label = label[len(prefix):]
            names.append("%s=%d" % (_clip_w(label, 20), count))
        more = " +%d" % (len(over) - 4) if len(over) > 4 else ""
        parts.append("durable-over %s%s" % ("·".join(names), more))
    if alerts.get("distill_stale"):
        parts.append("distill stale %s" % fmt_min(memory.get("last_distill_min")))
    if not parts:
        return None
    return (" · ".join(parts), "lvl_y")


def _group_key_job(j, session_groups=None, job_groups=None):
    session_groups = session_groups or {}
    job_groups = job_groups or {}
    if getattr(j, "parent_slug", None) and j.parent_slug in job_groups:
        return job_groups[j.parent_slug]
    if getattr(j, "parent_sid", None) and j.parent_sid in session_groups:
        return session_groups[j.parent_sid]
    if getattr(j, "parent_cwd", None):
        return project_of(j.parent_cwd)
    # Drill is fixture-rooted: keep its runner + dispatch-depth-1 owner + dispatch-depth-2 workers
    # together in one /tmp/drill-* card. Other scheduled loops stay in the shared
    # control-plane group.
    if j.key in _LOOPS_KEYS:
        drill_group = project_of(j.cwd)
        if j.key == "drill" and drill_group.startswith("drill:"):
            return drill_group
        return "loops"
    return project_of(j.cwd)


def _group_activity_rank(g):
    members_live = [s.liveness for s in g["sessions"]] + [j.liveness for j in g["jobs"]]
    if "working" in members_live:
        return 0
    elif "idle" in members_live:
        return 1
    return 2


def _group_sort_key(name, g):
    activity_rank = _group_activity_rank(g)
    mtimes = [s.mtime for s in g["sessions"] if s.mtime is not None]
    recency = max(mtimes) if mtimes else None
    # None mtime sorts as oldest (i.e. last) — use a very negative sentinel for the desc sort.
    recency_sort = recency if recency is not None else -1.0
    return (activity_rank, -recency_sort, name)


def _sort_group_sessions(ss):
    def k(s):
        r = _LIVE_RANK.get(s.liveness, 9)
        if s.detached and r < 3:
            r = 3          # Detached sessions sort below working and idle sessions.
        return (r, -(s.elapsed_min or 0))
    return sorted(ss, key=k)


def _live_session_identity(s):
    """Stable, display-independent identity for a live-order anchor."""
    if s.session_id:
        if getattr(s, "app_server", False):
            kind = "app-server"
        elif getattr(s, "mem_worker", False):
            kind = "memory-worker"
        else:
            kind = "session"
        return ("sid", s.harness, s.session_id, kind)
    if s.pid:
        return ("proc", s.harness, s.pid, getattr(s, "proc_start", None))
    return ("fallback", s.harness, s.cwd, s.slug)


def _reconcile_live_order(anchors, current, identity):
    """Keep visible survivors in anchor order, then append current newcomers.

    A list per identity deliberately retains every current object if imperfect
    collection data produces a collision; snapshot order is the tie-breaker.
    """
    buckets = {}
    for item in current:
        buckets.setdefault(identity(item), []).append(item)
    ordered = []
    for key in anchors:
        bucket = buckets.get(key)
        if bucket:
            ordered.append(bucket.pop(0))
    for item in current:
        key = identity(item)
        bucket = buckets.get(key)
        if bucket and bucket[0] is item:
            ordered.append(bucket.pop(0))
    return ordered, [identity(item) for item in ordered]


class _LiveOrderState:
    """Run-local group and session anchors for the live curses renderer."""
    def __init__(self):
        self.groups = []
        self.group_tiers = {}
        self.sessions = {}

    def reconcile_groups(self, names, tiers=None):
        """Stabilize survivors within their current activity tier.

        A tier transition is intentionally treated as a new entry into the
        destination tier: activity changes take effect immediately, while
        mtime churn cannot reshuffle groups that stayed peers.
        """
        current_tiers = ({name: 0 for name in names} if tiers is None
                         else {name: tiers[name] for name in names})
        ordered = []
        anchors = []
        for tier in sorted(set(current_tiers.values())):
            tier_names = [name for name in names if current_tiers[name] == tier]
            tier_anchors = [name for name in self.groups
                            if self.group_tiers.get(name) == tier]
            tier_order, tier_keys = _reconcile_live_order(
                tier_anchors, tier_names, lambda name: name)
            ordered.extend(tier_order)
            anchors.extend(tier_keys)
        self.groups = anchors
        self.group_tiers = current_tiers
        current = set(names)
        self.sessions = {name: anchors for name, anchors in self.sessions.items()
                         if name in current}
        return ordered

    def reconcile_sessions(self, group, rows):
        ordered, self.sessions[group] = _reconcile_live_order(
            self.sessions.get(group, []), rows, _live_session_identity)
        return ordered


def _sort_group_jobs(js):
    return sorted(js, key=lambda j: (_JOB_LIVE_RANK.get(j.liveness, 9), -(j.elapsed_min or 0)))


_SHOW_ALL = False   # --all: reveal stale/dead/app_server sessions (folded by default per group)

# F-27 selectable-row stash. `_build_lines` returns a flat segment-line list and knows nothing
# about which SESSION/JOB produced which line — but a kill cursor must target a row, not a
# screen line. Stashed on the module (same pattern as _TOGGLE_ROWS) instead of changing the
# return signature, so render_once and every existing caller are untouched.
# Reset at the top of _build_lines, before any early return, so a stale map can never be read.
_SELECTABLE = []


def _selectable_session(s):
    """Kill-eligible session rows, per prd.md:253's two grades (and nothing else).

    Grade 1 (single confirm): unused / stale / dead — a row that is demonstrably not doing
    work. Plus an idle WORKER child (a leftover headless session).
    Grade 2 (warning + double confirm): working, or a registry that says busy.

    A plain interactive `idle` session is deliberately NOT selectable: it is somebody's live
    session sitting between prompts, and it appears in neither grade of the spec's list.
    """
    if getattr(s, "app_server", False):
        return False
    if s.liveness in ("unused", "stale", "dead", "working"):
        return True
    if s.liveness == "idle" and (getattr(s, "is_child", False)
                                 or getattr(s, "mem_worker", False)):
        return True
    return s.status == "busy"


def _select_entry(s, line_idx):
    return {"line": line_idx, "kind": "session", "pid": s.pid,
            "proc_start": getattr(s, "proc_start", None),
            "sid": s.session_id, "state": s.liveness,
            "status": s.status, "cwd": s.cwd, "slug": s.slug,
            "label": _session_name(s), "harness": s.harness, "source": None,
            "is_worker": bool(getattr(s, "is_child", False)
                              or getattr(s, "mem_worker", False))}


def _select_entry_job(j, line_idx):
    return {"line": line_idx, "kind": "job", "pid": j.pid,
            "proc_start": getattr(j, "proc_start", None),
            "sid": None, "state": j.liveness, "status": j.status,
            "cwd": j.cwd, "slug": j.slug,
            "label": j.slug or j.key, "harness": j.harness, "source": j.source,
            "is_worker": bool(getattr(j, "is_child", False))}
_FOLD_CHILD_LIVENESS = {"done", "queued", "idle", "unknown"}   # F-15b P0-2: dispatch-depth-2 stage-worker
                                                                # rows folded into the conductor
                                                                # breadcrumb unless working/stale/dead

# F-29 (v9, prd.md:290-295) — sub-agent observation rows. `⚡` is the PRD-specified glyph.
# Reads distinctly from dispatch's `🚀`/`↳` so the two nested-row kinds never visually merge.
# Single point of ASCII-degrade if double-width alignment ever breaks in a real terminal.
_ICON_SUBAGENT = "⚡"
_SUBAGENT_IND = "        "  # strip indent: pure inset, no connector glyph, 8 cells for a
                          # session-owned strip — at/past the dispatch-depth-2 arrow column so the
                          # strip reads as INSIDE the row above, never as a sibling (사용자
                          # 2026-07-16 "들여쓰기 레벨을 충분히 안쪽으로"; the 2-cell then 4-cell
                          # insets both read too shallow). 6→8 with the F-64 (v49) 3-cell dispatch
                          # ladder so the anchor keeps clearing the deeper arrows. Dispatch-owned
                          # strips add 2 more cells per depth (see _subagent_strip). The
                          # per-session ⚡N name-zone badge this used to pair with stays retired.


def _subagent_elapsed_min(sa):
    """Runtime minutes: an active entry keeps counting; a completed entry with an
    observed completion time STOPS there (사용자 2026-07-29 — before this, a done
    sub-agent's elapsed kept growing forever, conflating runtime with idle)."""
    started = getattr(sa, "started_at", None)
    if not started:
        return None
    end = getattr(sa, "ended_at", None) if not sa.active else None
    return max(0, int(((end or time.time()) - started) / 60))


def _subagent_idle_min(sa):
    """Minutes since a completed entry finished — the '잠든 시간' tail. None when
    active or when no completion time was observed (honest gap)."""
    ended = getattr(sa, "ended_at", None)
    if sa.active or not ended:
        return None
    return max(0, int((time.time() - ended) / 60))


def _subagent_strip(subs, depth=0):
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
    at its observed completion time and gains a dim `(<idle>)` tail — minutes asleep
    since it finished (사용자 2026-07-29 '언제 끝났는지'; no completion evidence →
    no tail). `depth` = the owning dispatch row's
    depth (0 for a session row): each level pushes the strip 2 more cells inward so it
    stays visibly inside its own owner (사용자 2026-07-16 "서브 세션에 서브 에이전트도")."""
    segs = [(_SUBAGENT_IND + "  " * max(0, depth), None), (_ICON_SUBAGENT, "dim")]
    for i, sa in enumerate(subs):
        if i:
            segs.append((" · ", "dim"))
        label = sa.agent_type or "agent"
        elapsed = _subagent_elapsed_min(sa)
        tail = fmt_min(elapsed) if elapsed is not None else "—"
        segs.append((label, None if sa.active else "dim"))
        model = getattr(sa, "model", None)
        name = _clean_model(_short_model_id(model)) if model else None
        if name:
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
        segs.append(("  " + tail, "dim"))
        idle = _subagent_idle_min(sa)
        if idle is not None:
            # 잠든 시간 (사용자 2026-07-29): total runtime alone can't say WHEN a
            # completed leg ended — the parenthetical is minutes-asleep since then.
            segs.append((" (" + fmt_min(idle) + ")", "dim"))
    return [segs]


_SUMMARY_FALLBACK_W = 60   # hermetic/no-terminal-width callers (mirrors the dim-snippet clip
                           # convention used elsewhere, e.g. the memory-row snippet cells)


# F-63: a summary younger than this reads as "now" and carries no tag; past it the
# text stays (24h sidecar window, titles.py) and this dim `⏳<elapsed>` age tag keeps
# it honest. 15 minutes is the pre-F-63 freshness cutoff, kept as the live threshold.
_SUMMARY_AGE_TAG_SEC = 15 * 60


def _summary_age_tag(summary_ts, now=None):
    """`⏳<elapsed>` for a summary older than the live window, else None."""
    if not isinstance(summary_ts, (int, float)) or isinstance(summary_ts, bool):
        return None
    now = time.time() if now is None else now
    age = now - summary_ts
    if age < _SUMMARY_AGE_TAG_SEC:
        return None
    return "⏳%s" % fmt_min(int(age // 60))


def _summary_row(summary, depth=0, term_width=None, start_col=None, summary_ts=None):
    """One dim subtitle row directly under a session/dispatch row (F-16/F-17 merge,
    사용자 확정 2026-07-19): the live one-sentence status from the SAME haiku call that
    produced the title. Pure inset — no connector/icon, `_SUBAGENT_IND` + the same
    per-depth ladder `_subagent_strip` uses, so it reads as INSIDE its owner row and
    never collides with the sub-agent strip's own indent. Caller gates presence
    (summary truthy, row not dead/stale — F-13) and ordering (this row before the
    owner's `⚡` strip, never after)."""
    indent = _SUBAGENT_IND + "  " * max(0, depth)
    target_col = int(start_col or 0)
    if term_width:
        target_col = min(target_col, max(_dw(indent), term_width - 1))
    padding = max(0, target_col - _dw(indent))
    used = _dw(indent) + padding
    maxw = max(1, term_width - used) if term_width else _SUMMARY_FALLBACK_W
    segs = [(indent, None)]
    if padding:
        segs.append((" " * padding, None))
    # F-63: the age tag reserves its own width so a long summary clips before the
    # tag disappears — the reader always learns HOW old the sentence is first.
    tag = _summary_age_tag(summary_ts)
    tag_w = (_dw(tag) + 1) if tag else 0
    if tag and maxw > tag_w:
        segs.append((_clip_w(summary, maxw - tag_w), "dim"))
        segs.append((" " + tag, "dim"))
    else:
        segs.append((_clip_w(summary, maxw), "dim"))
    return [segs]


def _dispatch_summary_detail_row(job, depth=1, term_width=None, orphan=False):
    """Use the main-session detail grammar for every live model dispatch."""
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
        # the hierarchy back onto the main-session detail column.
        indicator_col = _dw("  " + _dispatch_prefix(job, orphan=orphan))
        return _context_detail_row(
            job, depth=depth, term_width=term_width, dim=True,
            indent_width=indicator_col + 2, muted=True)
    summary = getattr(job, "summary", None)
    if not summary:
        return []
    return _summary_row(str(summary), depth=depth, term_width=term_width,
                        start_col=_NAME_COL,
                        summary_ts=getattr(job, "summary_ts", None))


# F-55a — the lead cell holds the state WORD, so its width is the longest word this row can
# actually draw. That set is DERIVED, never a typed-in number: it is the union of the two
# classifier vocabularies in model.py minus the states whose row is dropped outright.
#   · `stale`/`dead` never reach here — F-13 omits the whole row (both callers return [] first).
#   · `degraded` is a ROUTE-NODE state, not an entity liveness; neither classify_session nor
#     classify_job can emit it, so its 8 cells must not buy a column. (`_LIVE_GLYPH` carries it
#     for the route surface, which is why the domain is taken from model.py and not from there.)
# Today the max is 7 (`working`/`blocked`/`unknown`). If a longer state is ever added to the
# vocabulary this constant grows with it; if one arrives at RUNTIME from outside the vocabulary
# the word is still printed whole and only that row shifts right (see `_context_lead_cell`).
_CTX_LEAD_OMITTED_STATES = ("stale", "dead")   # F-13: the row itself is suppressed
_CTX_LEAD_STATES = tuple(sorted(
    (set(LIVENESS_STATES) | set(PLUGIN_QUEUE_STATES.values()))
    - set(_CTX_LEAD_OMITTED_STATES)))
_CTX_LEAD_W = max(len(state) for state in _CTX_LEAD_STATES)
_CTX_LABEL_W = _CTX_LEAD_W + 1   # display cells: left-aligned state word + one trailing space.
_CTX_GLYPH_LABEL_W = 2           # the F-55b fallback shape: single-cell glyph + trailing space.
                                 # Every `_LIVE_GLYPH`/`_SPIN` frame is a single-cell BMP glyph
                                 # (no emoji range), which is why 1 is safe here. Both widths are
                                 # plain len()/literals, not `_dw(...)`: this block is evaluated
                                 # at module load, before `_dw`/`_WIDE` are defined below.
_CONTEXT_VALUE_W = 4
_CONTEXT_NOW_GAP = 3          # minimum gap; F-42c widens it until NOW reaches the session column.
_CONTEXT_INDENT_W = 4        # left inset that aligns the row under the HARNESS NAME (user
                               # 2026-07-24 "하네스에서 좌측 정렬"): the session row leads with
                               # ``"  " + glyph + " "`` = 4 cells before the harness field, so the
                               # context bar starts at that same column in every layout.


def _compact_context_gauge_width(available, depth=0):
    """Compatibility shim: the legacy available-width knob no longer sizes any gauge.
    F-52b sizes the context track from the session's measured window (`_context_gauge_track`)."""
    return _GAUGE_W


_EXEC_GLYPH = "⚙"
# F-47 v47 — a wait-primitive child (`sleep` & co) renders as ⏳ and ALWAYS dim: the
# session is waiting on purpose, and the badge must not read as work even when the row
# is working for some other reason (busy status, fresh transcript).
_WAIT_GLYPH = "⏳"


def _fmt_exec_age(seconds):
    """Sub-minute exec ages need seconds; past that reuse `fmt_min`'s one vocabulary."""
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % max(0, seconds)
    return fmt_min(seconds // 60)


def _exec_detail_segs(entity):
    """F-47 `⚙ <command> <elapsed>` detail segments for a session row, or [].

    Two evidence shapes feed one badge: an owned process descendant (`exec_child`, carries
    an elapsed time) and a Codex rollout tool_call still awaiting its output (`exec_tool`,
    carries no clock). Process evidence wins when both exist — it is the stronger signal
    and the only one with a real elapsed. Brightness follows the CLASSIFICATION, not the
    badge: a promoted `working` row gets the live hue, a background child under an idle row
    stays dim (prd.md:263 — the badge never makes a row or its group look hot).
    """
    key = "g_work" if getattr(entity, "liveness", None) == "working" else "dim"
    child = getattr(entity, "exec_child", None)
    if isinstance(child, dict) and child.get("comm"):
        etime = child.get("etime_s")
        glyph = _EXEC_GLYPH
        if exec_child_is_wait(child):            # v47: waiting, never bright
            glyph, key = _WAIT_GLYPH, "dim"
        text = "%s %s" % (glyph, child["comm"])
        if isinstance(etime, (int, float)) and not isinstance(etime, bool):
            text += " %s" % _fmt_exec_age(etime)
        return [(text, key)]
    tool = getattr(entity, "exec_tool", None)
    if isinstance(tool, dict):
        # Tool name first: a rollout `cmd` string usually starts with shell prologue
        # (`set -eu`, an assignment), so its first token would name the wrong thing.
        label = tool.get("name") or tool.get("command")
        if label:
            return [("%s %s" % (_EXEC_GLYPH, label), key)]
    return []


def _context_lead_cell(state, dim=False, degrade=False):
    """F-55 lead cell: the state WORD, left-aligned, in the harness row's color key.

    `degrade=True` is the F-55b fallback — the v36 glyph shape in the SAME color key, used only
    when the word and the gauge track cannot share the row (never a clipped `worki…`).

    A state outside `_CTX_LEAD_STATES` (an unknown word arriving at runtime, or no liveness at
    all) is not truncated and not renamed: a real word prints whole and pushes only its own row
    right, and a missing one falls back to the glyph rather than inventing a state name.
    """
    key = _state_key(state, dim=dim)
    if degrade or not (isinstance(state, str) and state):
        glyph, key = _glyph(state, dim=dim)
        return glyph + " ", key
    return state.ljust(_CTX_LEAD_W) + " ", key


def _context_detail_row(entity, depth=0, term_width=None, dim=False,
                        indent_width=None, muted=False):
    """One ``<liveness> <gauge> <value>   NOW`` row for every live card.

    The lead cell names the session's state in words (F-55) using the SAME `_state_key()` color
    the harness row's glyph uses — the two places always name the same session in the same
    state, so no new vocabulary or color decision is introduced here.  The gauge track is
    window-proportional (F-52b).

    The context block stays under the harness field; F-42c aligns the descriptive NOW text to
    the session column shared with dispatch subtitles.  Both anchors are stable across layouts.
    """
    if getattr(entity, "liveness", None) in ("stale", "dead"):
        return []
    context = getattr(entity, "context", None)
    pct = getattr(context, "used_pct", None) if context is not None else getattr(entity, "ctx_pct", None)
    now_text = getattr(entity, "summary", None)
    if indent_width is None:
        indent_width = _CONTEXT_INDENT_W + 2 * max(0, depth)
    indent = " " * max(0, int(indent_width))
    available = max(0, (term_width or _SUMMARY_FALLBACK_W) - _dw(indent))
    gauge_width = _compact_context_gauge_width(available, depth=depth)
    track = _context_gauge_track(getattr(entity, "context_window_tokens", None))
    if (isinstance(pct, (int, float)) and not isinstance(pct, bool)
            and 0 <= pct <= 100):
        shown_pct = int(round(pct))
    else:
        shown_pct = None
    # F-55b drop order: NOW and the exec badge already yield first (they are sized from
    # whatever is left below), and the track length is a MEASUREMENT (F-52b) that must not be
    # shrunk to buy room. So the word is the last thing to give: it degrades to the glyph only
    # when the word + track + value cannot fit the row at all, even with zero NOW room.
    degrade = (_CTX_LABEL_W + track + _CONTEXT_VALUE_W) > available
    lead_text, lead_key = _context_lead_cell(getattr(entity, "liveness", None),
                                             dim=dim, degrade=degrade)
    segs = [(indent, None), (lead_text, lead_key)]
    segs.extend(_gauge_segs(shown_pct, gauge_width, track=track))
    if shown_pct is None:
        value_text = "—"
    else:
        value_text = "%d%%" % shown_pct
    segs.append((value_text.rjust(_CONTEXT_VALUE_W), "dim"))
    exec_segs = _exec_detail_segs(entity)
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
            # F-63: same reserved-width age tag as the dispatch subtitle row — the
            # tag survives clipping; only when the zone cannot hold tag + any text
            # does it drop and the bare NOW clip behaves exactly as before.
            tag = _summary_age_tag(getattr(entity, "summary_ts", None))
            text_room = now_room - _dw(sep)
            tag_w = (_dw(tag) + 1) if tag else 0
            if tag and text_room > tag_w:
                clipped = _clip_w(str(now_text), text_room - tag_w)
            else:
                tag = None
                clipped = _clip_w(str(now_text), text_room) if text_room > 0 else ""
            if clipped:
                if sep:
                    tail.append((sep, None))
                tail.append((clipped, "dim"))
                if tag:
                    tail.append((" " + tag, "dim"))
        if tail:
            segs.append((" " * gap, None))
            segs.extend(tail)
    if muted:
        # A dispatch detail row is supporting telemetry. Keep its full information density,
        # but collapse every colored segment to the same dim weight as the dispatch identity.
        segs = [(text, "dim" if key is not None else None) for text, key in segs]
    return [segs]


def _split_w_exact(text, max_width):
    """Split without ellipsis or character loss at display-cell boundaries."""
    if not text:
        return [""]
    max_width = max(1, int(max_width))
    chunks, current, used = [], [], 0
    for char in text:
        width = _cw(char)
        if current and used + width > max_width:
            chunks.append("".join(current))
            current, used = [], 0
        current.append(char)
        used += width
    if current:
        chunks.append("".join(current))
    return chunks


def _stage_detail_rows(nodes, depth=0, term_width=None, indent=None):
    """Render every sealed node once, wrapping instead of dropping route history.

    ``|`` joins parallel same-level siblings; ``›`` advances a topological level.
    Explicit parent sets keep asymmetric/partial joins dependency-exact.
    """
    nodes = list(nodes or ())
    if not nodes:
        return []
    indent = (_SUBAGENT_IND + "  " * max(0, depth)) if indent is None else indent
    label = "stage "
    available = max(1, (term_width or _SUMMARY_FALLBACK_W) - _dw(indent) - _dw(label))
    units = []
    previous_level = None
    for index, node in enumerate(nodes):
        state = node.get("state") or "pending"
        mark, key = {
            "done": ("✓", "dim"),
            "active": ("●", "g_work" if _BLINK_ON else "g_work_off"),
            "reconciling": ("…", "lvl_y"),
            "failed": ("✕", "lvl_r"),
            "degraded": ("◐", "lvl_y"),
            "pending": ("○", "dim"),
        }.get(state, ("○", "dim"))
        token = "%s %s" % (node.get("id") or "?", mark)
        parents = [str(parent) for parent in (node.get("depends_on") or ())]
        if parents:
            token += " ←{%s}" % ",".join(parents)
        progress = node.get("progress")
        if isinstance(progress, dict) and progress.get("total") is not None:
            token += " %s/%s" % (progress.get("done", 0), progress.get("total"))
        if node.get("gate_passed"):
            token += _GATE_MARK
        level = node.get("level")
        separator = "" if index == 0 else (" | " if level == previous_level else " › ")
        units.append((separator, token, key))
        previous_level = level

    rows, current, used = [], [], 0

    def flush():
        nonlocal current, used
        if not current:
            return
        prefix = label if not rows else " " * _dw(label)
        rows.append([(indent, None), (prefix, "dim")] + current)
        current, used = [], 0

    for separator, token, key in units:
        unit_width = _dw(separator) + _dw(token)
        if current and used + unit_width > available:
            flush()
            separator = (separator.strip() + " ") if separator else ""
            unit_width = _dw(separator) + _dw(token)
        if unit_width <= available:
            if separator:
                current.append((separator, "dim"))
            current.append((token, key))
            used += unit_width
            continue
        # An opaque node/dependency token may itself be wider than the row. Keep
        # every character by continuing it over as many rows as necessary.
        flush()
        combined = separator + token
        for chunk in _split_w_exact(combined, available):
            current = [(chunk, key)]
            used = _dw(chunk)
            flush()
    flush()
    return rows


def _projection_stage_detail_rows(entity, depth=0, term_width=None):
    """Dedicated full-route rows for one validated projection owner.

    SESSION cards only since 2026-07-24 ("depth=1,2도 메인 세션처럼 두번째 줄에서는
    로그 요약해서 띄우는걸로 통일") — dispatch cards keep their second line for the
    live log summary and carry the pipeline on the row's own breadcrumb."""
    if getattr(entity, "liveness", None) in ("stale", "dead"):
        return []
    projection = getattr(entity, "work_projection", None)
    if not projection or getattr(projection, "ambiguity", None):
        return []
    source = getattr(projection, "source", None)
    if source == "artifact-inferred":
        # An INFERRED inline stage (low-confidence: a lone plan dir, no sealed route) rides the
        # row's own `stage <x>` column ONLY — never a dedicated `plan › exec › test` detail row.
        # A main session must not carry that breadcrumb line (user 2026-07-24 "main 세션에 여전히
        # stage 뜨는 케이스"); this is the artifact-inferred case the route-exact suppression
        # missed. Covers both code (plan/exec/test/report) and spec-grounding labels.
        return []
    if source != "route-exact":
        return []
    backing = getattr(projection, "_route_view", None) or {}
    view = backing.get("view") or {}
    nodes = _collapse_parallel_nodes(view.get("nodes") or ())
    # A fully-complete route needs no detail row on the owning session: the pipeline is
    # done, and a finished (often dead-conductor) route's whole DAG lingering under the
    # live dispatcher session is noise (user 2026-07-24 "stage 설명 여전히 뜨는데 이거
    # 없앴다매?"). The detail row's value is the IN-PROGRESS non-linear process view — a
    # real failure (non-done node) still shows so it can be seen; only all-done is dropped.
    if nodes and all(n.get("state") == "done" for n in nodes):
        return []
    return _stage_detail_rows(nodes, depth=depth, term_width=term_width)


def set_show_all(v):
    global _SHOW_ALL
    _SHOW_ALL = bool(v)


_API_DISABLED = False   # F-51c: FLEET_DISABLE=usage-api / --no-usage-api, set by fleet.py main()


def set_api_disabled(v):
    global _API_DISABLED
    _API_DISABLED = bool(v)


# --- F-30 (v10, prd.md:304-310) — process view: pipeline-centric regrouping, `p` toggle ---
_PROCESS_VIEW = False        # False = group view (default, unchanged) | True = process view
_ROUTE_FOLD = {}             # {card_key: bool} — True = explicitly folded. User action ONLY;
                             # default fold state (§5.4 table) is computed fresh every build and
                             # never written here (so a card that becomes newly-failed re-expands
                             # on its own unless the user folded it by hand).
_FOLDABLE = []                # [{"line": idx, "card_key": ...}] — line-index map, `_build_process_
                              # lines` fills it fresh every call (`_SELECTABLE` precedent, F-27).


def set_process_view(v):
    global _PROCESS_VIEW
    _PROCESS_VIEW = bool(v)


_GATE_MARK = " ⊸"   # prd.md:308 — completion-gate PASSED. Never rendered for no-claim/absent.


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
    nid = n["id"]
    unit = n.get("unit")
    if unit:
        nid = "%s[%s]" % (nid, _compact_dispatch_name(unit, _PROFILE_MAX))
    elapsed = n.get("elapsed_min")
    mark = _GATE_MARK if n.get("gate_passed") else ""
    parents = [str(parent) for parent in (n.get("depends_on") or ())]
    deps = " ←{%s}" % ",".join(parents) if parents else ""
    if st == "done":
        tail = fmt_min(elapsed) if elapsed is not None else ""
        return "%s ✓%s%s" % (nid, tail, deps), "dim", mark
    if st == "active":
        tail = (" " + fmt_min(elapsed)) if elapsed is not None else ""
        extra = ""
        model = _clean_model(dash(n.get("model"))) if n.get("model") else None
        if model and model != "—":
            extra = " (%s%s)" % (model, ("·" + n["effort"]) if n.get("effort") else "")
        return "%s ●%s%s%s" % (nid, tail, extra, deps), ("g_work" if _BLINK_ON else "g_work_off"), mark
    if st == "reconciling":
        tail = (" " + fmt_min(elapsed)) if elapsed is not None else ""
        return "%s …gate%s%s" % (nid, tail, deps), "lvl_y", mark
    if st == "failed":
        tail = (" " + fmt_min(elapsed)) if elapsed is not None else ""
        return "%s ✕%s%s" % (nid, tail, deps), "lvl_r", mark
    if st == "degraded":
        degradation = n.get("degradation") or {}
        hop = degradation.get("fallback_hop") or "?"
        reason = degradation.get("reason") or "degraded"
        tail = (" " + fmt_min(degradation.get("ts") and max(0, int((time.time() - degradation.get("ts")) / 60)))) if degradation.get("ts") else ""
        return "%s ◐%s (%s·%s)%s" % (nid, tail, hop, reason, deps), "lvl_y", mark
    return "%s ○%s" % (nid, deps), "dim", mark


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

        for text, key, mark in flow_nodes:
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
            relation_before = True
        flush_current()
        flow_nodes.clear()

    need_prefix = False
    for level in ordered:
        if len(level) == 1:
            flow_nodes.append(_route_node_text(level[0]))
            continue
        flush_flow(need_prefix)
        need_prefix = False
        for index, node in enumerate(level):
            branch = "└" if index == len(level) - 1 else "├"
            text, key, mark = _route_node_text(node)
            out_lines.extend(_wrap_route_node(
                "  " + branch + " ", text, key, mark, max_width, continuation="    "))
        need_prefix = True
    flush_flow(need_prefix)
    return out_lines


def _route_job_row(job, max_width=None):
    """The active node's owning job, one compact row (prd.md:308's `└▸🚀 <slug> <harness>
    <model> ⏳<elapsed>` shape) — NOT the group view's full `_dispatch_row` grid (a route card
    is not a project group; its columns don't line up with one, and forcing them to would need
    a second name-width negotiation this view doesn't have). `max_width` (§5.5): the slug is
    the one field with no fixed budget elsewhere, so it yields first — same "the variable-width
    field clips, the fixed-shape fields never do" idiom as `_compact_dispatch_name`."""
    hn = _BADGE_TEXT.get(job.harness, "—") if job.harness else "—"
    model_txt = _clean_model(dash(job.model)) or "—"
    eff = ("(%s)" % job.effort) if job.effort else ""
    tail = "⏳%s" % fmt_min(job.elapsed_min) if job.elapsed_min is not None else ""
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
                (" — %d/%d nodes" % (done, total), "dim")]
        if show_elapsed and route_elapsed is not None:
            # design_review_round_1.md 🟡2 / prd.md:307 literal ("<n/m nodes> ⏳<경과>") — the
            # bare `_CLOCK` convention (session/dispatch GRID rows, render.py:540) is the wrong
            # precedent here: `_route_job_row` already established "⏳" as THIS card's own
            # elapsed glyph (the child row below reads `⏳8m`), so the L1 header must match it —
            # a bare "  15m" both drops the spec's glyph AND reads as a stray number glued onto
            # "n/m nodes" (the exact critic misreading).
            segs += [("  ⏳", "dim"), (fmt_min(route_elapsed), "dim")]
        if show_failed and any_failed:
            segs.append((" ⚠ failed node", "lvl_r"))
        return segs

    ladder = [tag_bits]
    for n in range(len(tag_bits) - 1, 0, -1):
        ladder.append(tag_bits[:n])
    variants = [(ladder[0], True, True)]
    for tags in ladder[1:]:
        variants.append((tags, True, True))
    variants.append((ladder[-1], False, False))
    for tags, show_elapsed, show_failed in variants:
        segs = build(tags, show_elapsed, show_failed)
        if term_width is None or sum(_dw(t) for t, _k in segs) <= term_width:
            return segs
    return build(ladder[-1], False, False)


def _session_for_job(session_by_identity, job):
    if not job or getattr(job, "pid", None) is None:
        return None
    key = (job.pid, getattr(job, "proc_start", None))
    return session_by_identity.get(key)


def _subagents_for_job(session_by_identity, job):
    """Prefer attempt-owned job evidence, then the exact pid/start child session."""
    direct = getattr(job, "subagents", None)
    if direct is not None:
        return direct
    session = _session_for_job(session_by_identity, job)
    return getattr(session, "subagents", None) if session is not None else None


def _route_card(view, session_by_identity, term_width, now):
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

    l1 = _route_card_l1(tag_bits, rid, done, total, route_elapsed, any_failed, arrow, term_width)

    out = [l1]
    if folded:
        return out, {"card_key": card_key, "fold_line": 0, "job_rows": [], "folded": folded}

    max_width = max(20, term_width - 6) if term_width else None
    for l2_line in _route_card_l2(view, max_width):
        out.append([("    ", None)] + l2_line)

    job_rows = []
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
                segs.append((name, "dim"))
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
        if getattr(j, "route_id", None):
            continue
        if max(1, int(getattr(j, "depth", 1) or 1)) != 1:
            continue
        if j.key not in _PIPE_STAGES:
            continue
        if j.slug in covered_slugs:
            continue
        if j.slug in seen:
            continue
        seen.add(j.slug)
        out.append(j)
    out.sort(key=lambda j: j.slug or "")
    return out


def _degrade_card(job, session_by_identity, term_width):
    """§5.3's degrade card — `source: heuristic`, existing `_PIPE_STAGES` breadcrumb, no DAG
    (there is no record to derive one from). No job-row entry (the card key IS the job)."""
    cap = job.key or "?"
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
    l1 = [("  " + arrow + " ", "dim"), ("[%s] " % tag, "name_dim"),
          (slug, "dim"), (" — no route record", "dim")]
    out = [l1]
    if folded:
        return out, {"card_key": card_key, "fold_line": 0, "job_rows": [], "folded": folded}
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
    return out, {"card_key": card_key, "fold_line": 0, "job_rows": [], "folded": folded}


def _build_process_lines(sessions, jobs, route_views_by_id, malformed, memory, term_width, layout,
                         node_evidence=None):
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
    global _FOLDABLE
    _FOLDABLE = []
    lines = [_pulse_segs(sessions, jobs)]
    _governor = _governor_segs()
    if _governor is not None:
        lines.append(_governor)
    _mem_summary = _mem_summary_segs(memory)
    if _mem_summary is not None:
        lines.append(_mem_summary)
    if malformed:
        lines.append([("  +%d malformed jobs.log rows skipped" % malformed, "dim")])
    lines.append([(_HFILL, None)])
    lines.append(None)
    lines.append([("  PROCESS VIEW", "head"), (_RFLUSH, None), ("p group view  ", "head")])
    lines.append(None)

    session_by_identity = {(s.pid, getattr(s, "proc_start", None)): s
                           for s in sessions if s.pid is not None and s.proc_start is not None}
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

    # F-29 — plain top-level sessions running a native sub-agent. Process view is route-centric
    # and emits no plain-session row, so without this a session's ⚡ agents are invisible here
    # even though group view shows them in its session loop. Collected up front so they alone
    # can populate the screen when no route/degrade card exists.
    sub_sessions = []
    for s in sessions:
        if (getattr(s, "app_server", False) or getattr(s, "is_child", False)
                or getattr(s, "mem_worker", False)):
            continue
        s_subs = [sa for sa in (getattr(s, "subagents", None) or []) if sa.active or _SHOW_ALL]
        if s_subs:
            sub_sessions.append((s, s_subs))

    if not real_views and not degrade_jobs and not sub_sessions:
        # prd.md:310 — an honest "nothing is running" statement, never a blank screen.
        lines.append([("  no active route", "dim")])
        return lines

    seen_keys = set()
    _seen_glyphs = set()
    covered_pids = set()
    first = True
    for view in real_views:
        if not first:
            lines.append(None)
        first = False
        base = len(lines)
        card_lines, meta = _route_card(view, session_by_identity, term_width, now)
        lines.extend(card_lines)
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
        seen_keys.add(meta["card_key"])

    for job in degrade_jobs:
        if not first:
            lines.append(None)
        first = False
        base = len(lines)
        card_lines, meta = _degrade_card(job, session_by_identity, term_width)
        lines.extend(card_lines)
        _FOLDABLE.append({"line": base + meta["fold_line"], "card_key": meta["card_key"],
                          "folded": meta["folded"]})
        if job.pid:
            covered_pids.add(job.pid)
        seen_keys.add(meta["card_key"])

    # Routeless sessions with active sub-agents — one minimal owner anchor + the same strip,
    # skipping any pid already shown under a route/degrade card above (no double draw).
    for s, s_subs in sub_sessions:
        if s.pid and s.pid in covered_pids:
            continue
        if not first:
            lines.append(None)
        first = False
        name_w = 40 if term_width is None else max(8, min(40, term_width - 6))
        anchor = [("  ● ", "dim"), (_clip_w(_session_name(s), name_w), "name_dim")]
        if getattr(s, "model", None):
            anchor.append(("  " + str(s.model), "dim"))
        lines.append(anchor)
        lines.extend(_subagent_strip(s_subs))

    # StateTracker.sweep() precedent — a card key not seen this tick must not leak its fold
    # flag into a future, unrelated card that happens to reuse the same route_id/slug.
    for k in [k for k in _ROUTE_FOLD if k not in seen_keys]:
        del _ROUTE_FOLD[k]

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
        rank = (
            -(getattr(job, "registry_priority", None)
              if getattr(job, "registry_priority", None) is not None else 0),
            getattr(job, "registry_order", None) if getattr(job, "registry_order", None) is not None else -1,
            -(getattr(job, "elapsed_min", None) if getattr(job, "elapsed_min", None) is not None else 10**9),
            index,
        )
        if key not in latest_attempt or rank > latest_attempt[key][0]:
            latest_attempt[key] = (rank, job)
    return [
        job for job in jobs
        if not (getattr(job, "route_id", None) and getattr(job, "route_node", None)
                and getattr(job, "attempt_id", None)
                and latest_attempt.get((job.route_id, job.route_node), (None, job))[1] is not job)
    ]


def live_harnesses(sessions):
    """Return harnesses with live, top-level sessions eligible for usage refresh."""
    return {s.harness for s in (sessions or ())
            if getattr(s, "liveness", None) not in ("stale", "dead")
            and not getattr(s, "app_server", False)
            and not getattr(s, "is_child", False)
            and not getattr(s, "mem_worker", False)}


def _usage_header_rows(sessions, layout="wide", now=None, api_disabled=False):
    """Build account usage rows independently of the main line builder.

    F-51c: `api_disabled` (user opted out via FLEET_DISABLE=usage-api / --no-usage-api) is
    "the user turned it off" — distinct from opencode's "no usage api" (structurally no
    source). A claude/codex harness with no passive-tap value while opted out renders as an
    unknown gauge (blank track + `—`), never the opencode-only opt-out sentence.
    """
    rl = {}
    for s in sessions or ():
        freshness = getattr(s, "_usage_freshness", None)
        if (s.rl_5h is not None or s.rl_7d is not None or s.rl_ms
                or getattr(s, "rl_windows", None) or freshness):
            cur = rl.get(s.harness)
            if cur is None or (s.mtime or 0) > (cur[3] or 0):
                rl[s.harness] = (s.rl_5h, s.rl_7d, s.rl_ms, s.mtime, s.rl_rs,
                                 getattr(s, "rl_windows", None), freshness)
    live = live_harnesses(sessions)
    if not rl and not live:
        return []
    hs = [h for h in ("claude", "codex", "opencode") if h in rl or h in live]
    rows = []
    for idx, h in enumerate(hs):
        hn = _BADGE_TEXT.get(h, h)
        row = [("  usage " if idx == 0 else "        ", "head"),
               (_pad(hn, 14), "hb_" + h if h in _BADGE_TEXT else "hb_other")]
        if h not in rl:
            if api_disabled and h in ("claude", "codex"):
                # F-51c: opt-out ("user turned it off"), not opencode's structural absence —
                # render the same blank/unknown gauge shape other unknown values already use.
                row.append(("[", "dim"))
                row += [("·" * _GAUGE_W, "dim"), ("   —", "dim")]
                row.append(("]", "dim"))
            else:
                row.append(("no usage api — plan quota is console-only", "dim"))
            rows.append(row)
            continue
        r5, r7, rms, _mt, rrs, rwins, freshness = rl[h]
        rs5, rs7 = (rrs or (None, None))[0], (rrs or (None, None))[1]
        gauges = ([(str(lbl) + " ", pct, reset) for lbl, pct, reset in rwins]
                  if rwins else [("5h ", r5, rs5), ("7d ", r7, rs7)])
        gauges += [(lbl + " ", v, None) for lbl, v in (rms or [])]
        for gi, (lbl, value, reset) in enumerate(gauges):
            row.append(("   ", None) if gi else ("", None))
            row.append((lbl, "dim")); row.append(("[", "dim"))
            if value is None:
                row += [("·" * _GAUGE_W, "dim"), ("   —", "dim")]
            else:
                gauge = _gauge_segs(value, _GAUGE_W)
                if freshness == "stale":
                    empty_cells = _GAUGE_W - _dw(gauge[0][0])
                    gauge[1] = ("·" * empty_cells, "dim")
                row += [(text, _flat_level(key)) for text, key in gauge]
                row.append((" %3d%%" % value, _flat_level(_pct_key(value))))
            row.append(("]", "dim"))
            if reset and reset > (now if now is not None else time.time()):
                row.append((" ↻ " + fmt_min(int((reset - (now if now is not None else time.time())) / 60)), "dim"))
        rows.append(row)
    return rows


def _resource_rows(resources, section):
    if section not in ("dispatch", "both"):
        return []
    visible = [r for r in (resources or [])
               if _SHOW_ALL or getattr(r, "liveness", None) == "working"]
    if not visible:
        return []
    rows = [[("  LAB RESOURCES", "head"), (_RFLUSH, None),
             ("%d visible  " % len(visible), "head")]]
    for project in sorted({getattr(r, "project", "(unknown)") for r in visible}):
        rows.append([("  LAB resource", "lvl_b"), (" · %s" % project, "grp")])
        project_rows = sorted(
            (r for r in visible if getattr(r, "project", "(unknown)") == project),
            key=lambda r: (getattr(r, "run_id", ""), getattr(r, "registry_path", "")))
        for run in project_rows:
            state = getattr(run, "liveness", "stale")
            key = {"working": "working", "exited": "dim", "stale": "lvl_y"}.get(state, "dim")
            marker = {"working": "●", "exited": "✓", "stale": "⚠"}.get(state, "?")
            rows.append([
                ("    %s " % marker, key), ("LAB resource", "lvl_b"),
                ("  %s" % getattr(run, "run_id", "—"), key),
                ("  %s" % state, key), (_RFLUSH, None),
                (fmt_min(getattr(run, "elapsed_min", None)) + "  ", "dim"),
            ])
            route = getattr(run, "route", None) or "—"
            node = getattr(run, "node", None) or "—"
            rows.append([("      project %s · cwd %s" % (
                project, getattr(run, "cwd", None) or "—"), "dim"),
                (_RFLUSH, None), ("route %s · node %s  " % (route, node), "dim")])
            updated = getattr(run, "log_updated_at", None)
            updated_text = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated))
                            if isinstance(updated, (int, float)) else "—")
            rows.append([("      log %s · updated %s" % (
                getattr(run, "log_path", None) or "—", updated_text), "dim")])
            dirty = getattr(run, "source_dirty", None)
            dirty_text = "—" if dirty is None else ("true" if dirty else "false")
            rows.append([("      config %s · sha256 %s · commit %s · dirty %s" % (
                getattr(run, "config_ref", None) or "—",
                getattr(run, "config_sha256", None) or "—",
                getattr(run, "source_commit", None) or "—", dirty_text), "dim")])
            # Tracked-workflow line (OPERATIONS §5.12): a finished process must show why
            # it ended and which registered attempt owns it, not just that it is gone.
            exit_code = getattr(run, "exit_code", None)
            rows.append([("      workflow %s · exit %s · owner %s%s" % (
                getattr(run, "workflow_state", None) or "—",
                "—" if exit_code is None else exit_code,
                getattr(run, "parent_attempt_id", None) or "—",
                (" · failure %s" % run.failure_class)
                if getattr(run, "failure_class", None) else ""), "dim")])
    return rows


def _build_lines(sessions, jobs, section, narrow, malformed, layout="wide", memory=None,
                 term_width=None, live_order=None, resources=None):
    """Return a flat list of segment-lines for the whole screen (None = blank line).

    Side effect: refreshes the module-level `_SELECTABLE` stash (F-27) — see its definition.

    Same contract consumed by BOTH `render_once` (plain, full output) and `_draw` (viewport
    slices this same list) — `_OFFSET` must never be read here (see module docstring).

    `memory` = F-19 collectors.memory.collect() result (or None — panel/alerts simply omitted;
    tests default to None so every pre-F-19 call site keeps working unchanged).
    """
    global _SELECTABLE
    _SELECTABLE = []     # reset before any early return — a stale target map must never survive
    # Direct hermetic callers from pre-v16 tests may construct rows without running the
    # collector boundary.  Use the same shared resolver as the snapshot path; never call
    # live_stage() or a renderer-specific route resolver. Terminal node evidence is the
    # collector's read-only input for routes whose live child has already disappeared.
    _node_evidence = {}
    _degradations = {}
    try:
        from .collectors import dispatch as _dispatch
        _node_evidence = getattr(_dispatch.collect, "last_route_nodes", None) or {}
        _degradations = getattr(_dispatch.collect, "last_degradations", None) or {}
    except Exception:
        _node_evidence = {}
        _degradations = {}
    if jobs and all(getattr(entity, "work_projection", None) is None
                    for entity in list(sessions) + list(jobs)):
        try:
            from .projection import attach_projections
            attach_projections(sessions, jobs, node_evidence=_node_evidence, now=time.time(),
                               degradations=_degradations)
        except Exception:
            pass
    # A completed route can have no live entity at all.  Keep an ephemeral projection
    # carrier so process rendering still consumes the common attached route view; it is
    # never added to display_jobs and therefore cannot create a phantom job row.
    _projection_entities = list(sessions) + list(jobs)
    if not _projection_entities and _node_evidence:
        try:
            from .model import DispatchJob
            from .projection import attach_projections
            for _rid, _nodes in _node_evidence.items():
                _ev = next(iter((_nodes or {}).values()), {})
                _rf = _ev.get("route_file") if isinstance(_ev, dict) else None
                _rn = next(iter((_nodes or {}).keys()), None)
                if not _rf or not _rn:
                    continue
                _carrier = DispatchJob(key="", slug="", route_id=_rid, route_file=_rf,
                                       route_hash=_ev.get("route_hash"), route_node=_rn,
                                       liveness="done")
                attach_projections([], [_carrier], node_evidence=_node_evidence, now=time.time(),
                                   degradations=_degradations)
                _projection_entities.append(_carrier)
        except Exception:
            pass
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
            "progress": _projection.progress.to_dict() if _projection.progress else None,
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
            term_width, layout, node_evidence=_node_evidence)
        resource_lines = _resource_rows(resources, section)
        return resource_lines + ([None] if resource_lines else []) + process_lines
    # F-18b: mem-worker (distiller/curator/F-17 refresher) census — computed on the ORIGINAL
    # session list, before is_child/mem filtering, so folded/mem-only groups still surface a
    # total in the legend even when no group header badge fires.
    wide_name_width = _wide_name_width(term_width) if layout == "wide" else None
    wide_route_zone = (_route_zone_width(term_width, wide_name_width)
                       if layout == "wide" else None)
    n_mem_total = sum(1 for s in sessions if getattr(s, "mem_worker", False))
    mem_by_group = {}
    for s in sessions:
        if getattr(s, "mem_worker", False):
            gk_mem = _group_key_session(s)
            mem_by_group[gk_mem] = mem_by_group.get(gk_mem, 0) + 1
    # F-19 repo rows: session-id -> display title, resolved on the ORIGINAL (unfiltered) list
    # so a mem event's source session still resolves even after mem-worker/child filtering
    # below drops it from the visible rows.
    sid_titles = {s.session_id: (s.title or s.slug) for s in sessions
                  if s.session_id and (s.title or s.slug)}
    # F-29 — a dispatched child session's own sub-agents survive the is_child filter below:
    # they re-attach as a strip under the dispatch row representing the child. Exact
    # attempt-owned evidence lives on the job; legacy persistent sessions fall back to
    # the pid/start join (사용자 2026-07-16 "서브 세션에 서브 에이전트도").
    child_subs_by_pid = {s.pid: s.subagents for s in sessions
                         if getattr(s, "is_child", False) and getattr(s, "subagents", None)}
    # headless dispatch children are shown as dispatch rows under their parent — never as
    # top-level sessions (the same headless process would otherwise double-show as session+job).
    # mem-worker sessions are excluded from grouping/census by default (F-18b) — they inherit
    # parent cwd/env and would otherwise misattribute into drill/project groups; `a` toggle
    # (_SHOW_ALL) restores them as a dedicated dim row (see _mem_row below).
    sessions = [s for s in sessions
                if not s.is_child and not (getattr(s, "mem_worker", False) and not _SHOW_ALL)]
    groups = {}
    session_groups = {}
    for s in sessions:
        gk = _group_key_session(s)
        groups.setdefault(gk, {"sessions": [], "jobs": []})["sessions"].append(s)
        if s.session_id:
            session_groups[s.session_id] = gk
    job_groups = {}
    # A route node has one current attempt. Keep older exact attempts in the alert
    # census/history, but suppress their rows by default so retries do not appear as
    # concurrent Fleet sessions. ``a`` restores the complete attempt history.
    top_jobs = [j for j in display_jobs if not (getattr(j, "parent_slug", None) and getattr(j, "depth", 1) >= 2)]
    depth_jobs = [j for j in display_jobs if getattr(j, "parent_slug", None) and getattr(j, "depth", 1) >= 2]
    for j in top_jobs + depth_jobs:
        gk = _group_key_job(j, session_groups=session_groups, job_groups=job_groups)
        groups.setdefault(gk, {"sessions": [], "jobs": []})["jobs"].append(j)
        if j.slug:
            job_groups[j.slug] = gk

    show_sessions = section in ("fleet", "both")
    show_jobs = section in ("dispatch", "both")

    order = sorted(groups.keys(), key=lambda name: _group_sort_key(name, groups[name]))

    # Anchor only project cards that will actually be visible.  This mirrors the
    # existing section/dead-job/fold decisions below, so hidden cards are pruned.
    if live_order is not None:
        visible_order = []
        for name in order:
            g = groups[name]
            group_sessions = g["sessions"] if show_sessions else []
            group_jobs = g["jobs"] if show_jobs else []
            if not _SHOW_ALL:
                group_jobs = [j for j in group_jobs if j.liveness != "dead"]
            if not group_sessions and not group_jobs:
                continue
            live_sessions = [s for s in group_sessions
                             if s.liveness not in ("stale", "dead") and not s.app_server]
            if (not _SHOW_ALL) and not live_sessions and not group_jobs:
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

    lines = []
    _seen_glyphs = set()
    # F-12(c) legend glyph-appearance tracking — LOCAL to this call (never module/global state,
    # _OFFSET invariant R3): which of the conditional legend glyphs actually got emitted this
    # build. working/idle/dispatch/`~` stay unconditional (always relevant vocabulary); the
    # rest (detached/stale/dead/child-jobs/worktrees) only show up in the legend when at least
    # one row used them.
    lines.extend(_usage_header_rows(sessions, layout=layout, api_disabled=_API_DISABLED))
    # fleet pulse — htop's "Tasks: N, M running" analogue: whole-board census + live spend Σ
    # Show the row by default; counts skip app-server companions. Extracted into _pulse_segs
    # (F-30, v10) so the process view (§5.1) shares this EXACT row instead of a second copy.
    # `_real` stays a LOCAL here too — the alert strip below (ctx_items) still needs it.
    _real = [s for s in sessions if not s.app_server and not getattr(s, "mem_worker", False)]
    lines.append(_pulse_segs(sessions, display_jobs))  # Aggregate cost rollup intentionally removed.
    resource_lines = _resource_rows(resources, section)
    if resource_lines:
        lines.extend(resource_lines)
    _governor = _governor_segs()               # F-28c — pulse-adjacent, never merged into pulse
    if _governor is not None:                  # counts (I8); None = source absent or quiet.
        lines.append(_governor)
    _mem_summary = _mem_summary_segs(memory)
    if _mem_summary is not None:
        lines.append(_mem_summary)
        _seen_glyphs.add("mem")
    if _SHOW_ALL:
        _mem_events = _mem_event_rows(memory)
        if _mem_events:
            lines.extend(_mem_events)
            _seen_glyphs.add("mem")

    # alert strip — CONDITIONAL (zero lines when healthy): compaction-imminent contexts and
    # stalled dispatches (the stealth-death guard §5.10, surfaced on the board instead of only
    # in dispatch-liveness.sh runs). dead jobs = red, warnings = yellow.
    # F-10: names go through the same compaction as dispatch rows (_compact_dispatch_name) with
    # a loop job's `<case>-<ts>-<pid>` tail stripped first (raw timestamps/pids are noise here);
    # same-kind alerts aggregate into one line (`⚠ 2 dead jobs: a·b`), bucketed dead/stale/ctx.
    def _alert_name(name):
        return _compact_dispatch_name(_ALERT_TAIL.sub("", name or "") or (name or "?"), 20)

    def _bucket_text(label, names):
        if not names:
            return None
        if len(names) == 1:
            return "%s %s" % (label, names[0])
        shown = "·".join(names[:4])
        more = " +%d" % (len(names) - 4) if len(names) > 4 else ""
        return "%d %s jobs: %s%s" % (len(names), label, shown, more)

    dead_names = [_alert_name(j.slug or j.key) for j in jobs if j.liveness == "dead"]
    stale_names = [_alert_name(j.slug or j.key) for j in jobs if j.liveness == "stale"]
    ctx_items = [(s.slug or "?", s.ctx_pct) for s in _real
                 if s.ctx_pct is not None and s.ctx_pct >= 80 and s.liveness in ("working", "idle")]

    buckets = []
    dead_text = _bucket_text("dead", dead_names)
    if dead_text:
        buckets.append((dead_text, "lvl_r"))
    stale_text = _bucket_text("stale", stale_names)
    if stale_text:
        buckets.append((stale_text, "lvl_y"))
    if ctx_items:
        worst = max(pct for _n, pct in ctx_items)
        ctx_text = _bucket_text("context-high", [_alert_name(n) for n, _p in ctx_items]) \
            if len(ctx_items) > 1 else "context %d%% %s" % (
                ctx_items[0][1], _alert_name(ctx_items[0][0]))
        buckets.append((ctx_text, "lvl_r" if worst >= 90 else "lvl_y"))
    for _degradation_text in _degradation_alert_rows(_degradations, show_all=_SHOW_ALL):
        buckets.append((_degradation_text, "lvl_y"))
    mem_bucket = _mem_alert_bucket(memory)   # F-19 — last in priority (dead > stale > ctx > mem)
    if mem_bucket:
        buckets.append(mem_bucket)
        _seen_glyphs.add("mem")

    if buckets:
        # priority truncation dead > stale > ctx when the row would overflow — buckets are
        # already in that priority order, so drop from the tail. Budget mirrors the existing
        # dormant-dirs line convention (`names[:90]`) rather than a hardcoded terminal width.
        kept = list(buckets)
        while len(kept) > 1 and sum(len(t) for t, _k in kept) + 3 * (len(kept) - 1) > 100:
            kept.pop()
        arow = [("  alert ", "head")]
        for ai, (txt, akey) in enumerate(kept):
            if ai:
                arow.append(("   ", None))
            arow.append(("⚠ " + txt, akey))
        lines.append(arow)
    # usage/intel zone = PLAIN bg + a full-width dim rule below it (user 2026-07-03: intel
    # Keep tint directory-only so the intelligence zone is not confused with active cards.
    lines.append([(_HFILL, None)])

    # header bar REPLACES the `──` zone divider — htop separates meters from the process list
    # with its bar, not a rule. Leave one blank line above it so
    # the top intel zone and the bar don't touch. Narrow mode's 2-line cards have no single
    # column mapping → the bar degrades to a zone label + current-mode hint.
    # Column header uses plain dim labels; tinted panels carry the visual grouping.
    lines.append(None)
    _sh = " " * (_INSET + _PAD_IN) if _TINT_OK else ""   # shift matches panel content columns
    if layout != "wide":
        lines.append([(_sh + "  SESSIONS", "head"), (_RFLUSH, None),
                      ("%s · press w to cycle  " % layout, "head")])
    else:
        # right-flushed 'time' label sits over the (inset) elapsed-time column — trailing
        # spaces mirror the tint rows' right inset so the label right-aligns with the values.
        lines.append([(_sh + _col_head(wide_name_width or _NW_S), "head"), (_RFLUSH, None),
                      ("time" + " " * (_INSET + _PAD_IN + 1), "head")])
    lines.append(None)                  # Gap below the column header.

    first = True
    folded_groups = []       # dormant dirs — aggregated into ONE line at the bottom (user: the
                             # stack of per-dir folded rules at the bottom was visual noise)
    for name in order:
        g = groups[name]
        group_sessions = g["sessions"] if show_sessions else []
        group_jobs = g["jobs"] if show_jobs else []
        if not _SHOW_ALL:
            group_jobs = [j for j in group_jobs if j.liveness != "dead"]
        if not group_sessions and not group_jobs:
            continue    # empty-group suppression per --section: no dangling header

        # group fold decision (R4) — computed BEFORE emitting anything for this group.
        live_sessions = [s for s in group_sessions
                          if s.liveness not in ("stale", "dead") and not s.app_server]
        # conservative: any LIVE job present blocks the fold (R4's "never hide a dispatch").
        # F-46: an afterglow row is not live work — it must not keep an otherwise dormant
        # group expanded, per the spec's "접힘을 막지 않는다". The group's own cooling glyph
        # (✓ + time since completion) already carries "this dir just finished" at the fold
        # level, so nothing is lost: the afterglow row only ever hides in a group that has
        # no live session and no live job at all.
        must_show_jobs = any(not getattr(j, "afterglow", False) for j in group_jobs)
        fold = (not _SHOW_ALL) and (not live_sessions) and (not must_show_jobs)

        if fold:
            folded_groups.append((name, len(group_sessions)))
            continue

        if not first:
            lines.append(None)
        first = False

        shown = (group_sessions if _SHOW_ALL else
                 [s for s in group_sessions
                  if not (s.liveness in ("stale", "dead") or s.app_server)])
        hidden = len(group_sessions) - len(shown)
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
                elif (getattr(j, "is_child", False) and j.parent_sid
                      and j.parent_sid in shown_sids):
                    children.setdefault(j.parent_sid, []).append(j)
                    recovered_session_ids.add(j.parent_sid)
                else:
                    # A malformed/stale parent edge must not make a live dispatch-depth-2 row
                    # disappear from Fleet. Surface it as a project-level orphan.
                    orphans.append(j)
            elif j.is_child and j.parent_sid and j.parent_sid in shown_sids:
                children.setdefault(j.parent_sid, []).append(j)
            elif getattr(j, "source", None) == "plugin-queue":
                # F-50c: a plugin-queue job nests ONLY on an exact `sessionId` ==
                # `Session.session_id` match (the branch above). Its `parent_cwd` is the
                # plugin workspace root — nesting on it would attach the job to whatever
                # session happens to sit in that directory, which is the misattribution the
                # separate-surface rule exists to prevent. Unmatched → orphan.
                orphans.append(j)
            elif j.is_child and getattr(j, "parent_cwd", None):
                sid = shown_cwds.get(os.path.realpath(j.parent_cwd))
                if sid:
                    children.setdefault(sid, []).append(j)
                elif j.key in _LOOPS_KEYS or is_drill_case:
                    loops_jobs.append(j)
                else:
                    orphans.append(j)
            elif j.key in _LOOPS_KEYS or is_drill_case:
                loops_jobs.append(j)
            else:
                orphans.append(j)

        gcwd = "" if name == "loops" else (group_sessions[0].cwd if group_sessions else
                (group_jobs[0].cwd if group_jobs else ""))
        # The section title has no indicator glyph; the title itself carries active state.
        # while the group works, plain bold otherwise. Doubles with the active card tint.
        n_work = sum(1 for s in live_sessions if s.liveness == "working") + \
                 sum(1 for j in group_jobs if j.liveness == "working")
        # cooling (round-6, user 2026-07-03): no active work, but the newest session transcript
        # A write within the cooling window indicates a directory that just finished.
        # state between hot (green ●) and cold (no glyph): a grey ring + time-since-done, so a
        # just-finished repo reads as "done & waiting" rather than fully dormant. Sessions linger
        # as idle (still within the 48h live window), so the group is not folded (R4).
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
            # Recent completion uses a filled grey dot, distinct from dead and stale.
            head_segs += [(_COOL_FILLED, "grp_cool"), (" ", None)]
        else:
            # Long inactivity uses a grey ring.
            head_segs += [(_COOL_RING, "grp_cold"), (" ", None)]
        # Cooling names share the dim-yellow indicator color; cold keeps the default title color.
        _name_key = "grp_hot" if n_work else ("grp_cool" if _cool_min is not None else "grp")
        head_segs += [(name, _name_key), ("/", "dim")]
        _nwt = _wt_count(gcwd)
        if _nwt:
            # Match statusline notation for parallel or leftover worktrees.
            head_segs += [(" 🚧 %d" % _nwt, "g_idle")]
            _seen_glyphs.add("wt")
        _nmem = mem_by_group.get(name, 0)
        if _nmem:
            head_segs += [(" 🧠 %d" % _nmem, "dim")]
            _seen_glyphs.add("mem")
        if _cool_min is not None:
            # Prefix time since completion with a check mark.
            head_segs += [("  ", None), ("%s %s" % (_COOL_TIME_ICON, fmt_min(_cool_min)), "grp_cool")]
        # The group header is the card's first title row.
        # tinted row of the panel, ▍ anchor on the card's padding edge; no floating label.
        _g0 = len(lines)                # panel start (title INCLUDED in the tint range)
        lines.append(head_segs)
        _rail_key = "grp_live" if n_work else "dim"
        if n_work:
            _body_tint = _TINT_BODY_HOT       # Active: midnight-blue tint.
        elif _cool_min is not None:
            _body_tint = _TINT_BODY_COOL      # Cooling: middle level between active and inactive.
        else:
            _body_tint = _TINT_BODY           # Inactive: dark grey.

        # rows stay tight (no blank line — that spread them too far apart); the mid-line gauge
        # glyph (━/─) is what keeps the stacked context bars from merging into a solid wall.
        _srow = {"wide": None, "narrow": _session_row_2line, "stack": _session_row_stack}[layout]
        _jrow = {"wide": None, "narrow": _dispatch_row_2line, "stack": _dispatch_row_stack}[layout]
        # LIVE main-session lines get bold text (user 2026-07-03, after a whole-row tint-
        # Use font weight rather than brightening the entire row background.
        # carries the distinction). Excludes stale/dead/app-server/detached (already faded dim).
        _sess_bold_ids = set()

        def _emit_dispatch_tree(job, parent_model=None, parent_harness=None, parent_effort=None,
                                orphan=False, is_last=True):
            # Row authority is the attached WorkProjection.  No first-child or
            # first-route selection is allowed in this render-local tree walk.
            block_start = len(lines)   # F-64c: the dispatch-depth-1 rail spans everything emitted below
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
            # F-27: a job row is a target only with an exact pid to verify (prd.md:253).
            if job.pid:
                _SELECTABLE.append(_select_entry_job(job, len(lines)))
            if _jrow:
                lines.extend(_jrow(job, orphan=orphan, parent_model=row_parent_model,
                                   parent_effort=row_parent_effort, stage_override=stage_override,
                                   route_seq=route_seq))
            else:
                lines.append(_dispatch_row(job, orphan=orphan, parent_model=row_parent_model,
                                           parent_harness=parent_harness,
                                           parent_effort=row_parent_effort, is_last=is_last,
                                           stage_override=stage_override,
                                           name_width=wide_name_width, route_seq=route_seq,
                                           route_zone=wide_route_zone))
            # 2026-07-24: a dispatch card's second line is its live log summary ONLY —
            # the same "identity row + fresh NOW" shape as a main-session card. The
            # pipeline rides the row's own breadcrumb; dedicated stage rows are a
            # session-card surface.
            detail = _dispatch_summary_detail_row(
                job, depth=max(1, int(getattr(job, "depth", 1) or 1)), term_width=term_width,
                orphan=orphan)
            if detail:
                lines.extend(detail)
            # F-29 — the child session's own sub-agents, one strip directly under the
            # dispatch row that represents it (depth-indented; active always, completed
            # only with `a` — the same convention as session-owned strips above).
            job_subs = getattr(job, "subagents", None)
            if job_subs is None:
                job_subs = child_subs_by_pid.get(job.pid) if job.pid else None
            shown_job_subs = [sa for sa in (job_subs or []) if sa.active or _SHOW_ALL]
            if shown_job_subs:
                lines.extend(_subagent_strip(
                    shown_job_subs, depth=max(1, int(getattr(job, "depth", 1) or 1))))
            for sub in _sort_group_jobs(job_children.get(job.slug, [])):
                # F-15b P0-2: a dispatch-depth-2 stage worker that is done/queued/idle is already
                # absorbed into the conductor's own breadcrumb (✓/dim future segment) — only
                # working (active) or stale/dead (failed, needs to be seen) children get their
                # own row. `_SHOW_ALL` (the existing `a`-key toggle) restores the folded ones.
                if (max(1, int(getattr(sub, "depth", 1) or 1)) >= 2 and not _SHOW_ALL
                        and sub.liveness in _FOLD_CHILD_LIVENESS):
                    continue
                _emit_dispatch_tree(sub, parent_model=job.model or parent_model,
                                    parent_harness=job.harness or parent_harness,
                                    parent_effort=parent_effort, orphan=False)
            # F-64c: bracket the WHOLE dispatch-depth-1 unit — owner row included (user 2026-08-05
            # "다시 앞으로 당겨서 depth=1까지 묶어주고") — with the capsule rail in the
            # left margin: ╻ on the owner row, ┃ through the middle, ╹ on the last row.
            # A childless one-row unit has nothing to bind and stays unpainted. Working
            # owners blink in their stage hue (brightness only); finished/stale sit dim.
            if (_jrow is None and not orphan
                    and max(1, int(getattr(job, "depth", 1) or 1)) == 1
                    and len(lines) - block_start >= 2):
                if job.liveness == "working":
                    color_i = _depth1_rail_color_index(
                        getattr(job, "key", None),
                        stage_override or getattr(job, "stage", None), route_seq)
                    rail_key = ("stg%d_on" if _BLINK_ON else "stg%d_off") % color_i
                else:
                    rail_key = "dim"
                first, last = block_start, len(lines) - 1
                for idx in range(first, len(lines)):
                    if idx == first:
                        char = _RAIL_TOP
                    elif idx == last:
                        char = _RAIL_BOT
                    else:
                        char = _RAIL_MID
                    lines[idx] = _overwrite_rail_cell(
                        lines[idx], _RAIL_COL, char, rail_key)

        shown = _sort_group_sessions(shown)
        if live_order is not None:
            shown = live_order.reconcile_sessions(name, shown)
        rendered_parent_sids = set()  # ambiguous enrichment must not duplicate a dispatch tree
        for s in shown:
            if getattr(s, "mem_worker", False):
                # Memory rows use a dedicated dim summary and appear only after the ``a`` toggle.
                lines.extend(_mem_row(s, layout))
                _seen_glyphs.add("mem")
                continue
            kids = _sort_group_jobs(children.get(s.session_id, []))
            if s.session_id in rendered_parent_sids:
                kids = []
            elif s.session_id:
                rendered_parent_sids.add(s.session_id)
            nested_n = len(kids) + sum(len(job_children.get(k.slug, [])) for k in kids)
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
            if nested_n:
                _seen_glyphs.add("child")
            if getattr(s, "subagents", None):
                _seen_glyphs.add("subagent")
            session_projection = getattr(s, "work_projection", None)
            session_route = getattr(session_projection, "route_id", None)
            visible_route_owner = (
                bool(session_route)
                and getattr(session_projection, "source", None) == "route-exact"
                and not getattr(session_projection, "ambiguity", None)
                and any(
                    max(1, int(getattr(child, "depth", 1) or 1)) == 1
                    and getattr(getattr(child, "work_projection", None), "route_id", None)
                    == session_route
                    and getattr(getattr(child, "work_projection", None), "source", None)
                    == "route-exact"
                    and not getattr(getattr(child, "work_projection", None), "ambiguity", None)
                    for child in kids
                )
            )
            recovered_session_owner = s.session_id in recovered_session_ids
            suppress_session_stage = visible_route_owner or recovered_session_owner
            _n0 = len(lines)
            if _selectable_session(s):
                _SELECTABLE.append(_select_entry(s, _n0))    # F-27 target map
            if _srow:
                lines.extend(_srow(s, is_parent=bool(nested_n), child_count=nested_n,
                                   term_width=term_width,
                                   show_projection_stage=not suppress_session_stage))
            else:
                lines.append(_session_row(s, narrow, is_parent=bool(nested_n),
                                          child_count=nested_n,
                                          name_width=wide_name_width,
                                          show_projection_stage=not suppress_session_stage,
                                          stage_zone=wide_route_zone))
            if not (s.liveness in ("stale", "dead") or s.app_server or s.detached):
                _sess_bold_ids.update(range(_n0, len(lines)))
            detail = _context_detail_row(s, term_width=term_width)
            if detail:
                lines.extend(detail)
            stage_rows = ([] if suppress_session_stage else
                          _projection_stage_detail_rows(s, term_width=term_width))
            if stage_rows:
                lines.extend(stage_rows)
            # F-29 (v9) — sub-agent rows, directly under the parent session's own row(s).
            # Active always shown; completed only surface with `a` (F-18b dim-row convention).
            shown_subs = [sa for sa in (getattr(s, "subagents", None) or [])
                         if sa.active or _SHOW_ALL]
            if shown_subs:
                lines.extend(_subagent_strip(shown_subs))
            for i, cj in enumerate(kids):
                _emit_dispatch_tree(cj, parent_model=s.model, parent_harness=s.harness,
                                    parent_effort=s.effort, orphan=False,
                                    is_last=(i == len(kids) - 1))
        if group_sessions and hidden:
            lines.append([("     +%d stale/companion hidden" % hidden, "dim")])

        # orphans / loops: project-level fallback (standalone tree rows)
        if orphans:
            lines.append(_orphan_divider(term_width))
        for oj in _sort_group_jobs(orphans):
            _emit_dispatch_tree(oj, orphan=show_sessions)
        for lj in _sort_group_jobs(loops_jobs):
            _emit_dispatch_tree(lj, orphan=False)

        # F-19 repo rows (사용자 확정 2026-07-16): this card's own today-mem events, below a
        # subtle in-band divider — entirely silent when the repo has none (healthy-silent,
        # §4.7 F-19 convention). Rides the same body-tint loop below, unmodified.
        repo_events = (memory or {}).get("by_repo", {}).get(name) if memory else None
        if repo_events:
            lines.append(_mem_divider(term_width))
            lines.extend(_mem_repo_rows(repo_events, sid_titles))

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
        lines.append([("  (no active sessions or dispatch jobs)", "dim")])

    if malformed:
        lines.append(None)
        lines.append([("  +%d malformed jobs.log rows skipped" % malformed, "dim")])

    # legend — status dots (columns are labelled by the header row). F-12(c): working/idle/
    # dispatch/`~` are always-relevant vocabulary and stay unconditional; the rest only appear
    # when this build actually used them (_seen_glyphs, tracked above — local, not global).
    lines.append(None)
    legend = [
        ("  ", None), ("⠹", "g_spin"), (" working   ", "dim"),
        ("●", "g_work_off"), (" idle   ", "dim"),
    ]
    if "unused" in _seen_glyphs:
        legend += [(_LIVE_GLYPH["unused"], "g_unused"), (" unused   ", "dim")]
    if "detached" in _seen_glyphs:
        legend += [(_DETACHED_GLYPH, "g_work_off"), (" detached   ", "dim")]
    if "stale" in _seen_glyphs:
        legend += [("·", "g_stale"), (" stale   ", "dim")]
    if "dead" in _seen_glyphs:
        legend += [("✕", "g_dead"), (" dead     ", "dim")]
    if "degraded" in _seen_glyphs:
        legend += [("◐", "lvl_y"), (" degraded node   ", "dim")]
    if "blocked" in _seen_glyphs:
        legend += [("◑", "g_blocked"), (" blocked session   ", "dim")]
    if "child" in _seen_glyphs:
        legend += [("▾N", "dim"), (" child jobs   ", "dim")]
    if "subagent" in _seen_glyphs:
        legend += [(_ICON_SUBAGENT, "dim"), (" sub-agent   ", "dim")]
    if jobs:
        legend += [("↳", "dim"), (" dispatch   ", "dim")]
    if "wt" in _seen_glyphs:
        legend += [("🚧 N", "dim"), (" worktrees   ", "dim")]
    if n_mem_total or "mem" in _seen_glyphs:
        # Always expose the board-wide memory total in the legend, even when memory-only groups fold.
        legend += [("🧠 %d" % n_mem_total, "dim"), (" mem   ", "dim")]
    # F-9(d) `~ derived/inherited value` retired with the marker itself (user 2026-07-16:
    # inherited effort now shows plain — the tilde read as noise).
    lines.append(legend)

    return lines


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
    # F-19: best-effort — a collector import/read failure must never break the render.
    try:
        from .collectors import memory as memcol
        return memcol.collect()
    except Exception:
        return None


def render_once(collect_all, hfilter, section):
    global _GIT_TELEMETRY
    sessions, jobs = collect_all(harness_filter=hfilter)
    resources = list(getattr(collect_all, "last_resource_jobs", []))
    malformed = _malformed()
    mem_snapshot = _collect_memory()
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
                             resources=resources)
    finally:
        _GIT_TELEMETRY = previous_git_telemetry
    out = "\n".join(_plain(l) for l in lines) + "\n"
    # Write UTF-8 bytes directly so the snapshot's box/braille glyphs survive a
    # non-UTF-8 console codepage (e.g. Windows cp949), which would otherwise raise
    # UnicodeEncodeError. Falls back to text stdout when buffer is unavailable.
    try:
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.buffer.flush()
    except (AttributeError, ValueError):
        sys.stdout.write(out)
    return 0


def _malformed():
    try:
        from .collectors import dispatch
        from .collectors import resource_runs
        return (getattr(dispatch.collect, "last_malformed", 0)
                + getattr(resource_runs.collect, "last_malformed", 0))
    except Exception:
        return 0


# ---------- curses (live) ----------
# display-width aware clipping — emoji/CJK render 2 cells but len()==1, so advancing col by
# len() drew the next segment 1 col early and overwrote the previous field's last char
# (e.g. the directory name lost a char after the 📁). Count real cells instead.
_WIDE = set("🧠✨⏳📁🚀🛰⚡📋⚙📊🐛📈🔬💻⏱↻")


def _cw(ch):
    o = ord(ch)
    if o == 0xFE0F or 0x200B <= o <= 0x200F or o == 0x2060:   # VS16 / zero-width → 0 cells
        return 0
    if ch in _WIDE:
        return 2
    if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3
            or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6
            or 0x1F000 <= o <= 0x1FAFF):                       # CJK / Hangul / fullwidth / emoji
        return 2
    return 1


def _dw(s):
    return sum(_cw(c) for c in s)


def _clip_w(s, maxw, ellipsis="…"):
    """Tail-cut `s` to display width maxw (head preserved), stopping at cell boundaries
    so a double-width char (e.g. Hangul) is never split in half. Appends `ellipsis` (width 1)
    when clipped — mirrors _compact_dispatch_name's ellipsis convention (F-14)."""
    s = s or ""
    if _dw(s) <= maxw:
        return s
    lim = maxw - (_cw(ellipsis) if ellipsis else 0)
    out, w = [], 0
    for ch in s:
        cw = _cw(ch)
        if w + cw > lim:
            break
        out.append(ch); w += cw
    return "".join(out) + (ellipsis if ellipsis else "")


# fill sentinels (3-char \x00<fill>\x00): everything after is right-aligned to the edge; the gap
# is filled with <fill> — space for _RFLUSH (invisible), ─ for _HFILL (a full-width rule).
_INSET = 2                  # Panel outer margin in columns.
_PAD_IN = 2                 # Extra inner padding between tint edge and content.
# Roles that make a line a full-width chrome BAR. A line is a bar when its FIRST segment says
# so — so any new bar variant must be registered here or the band silently never paints.
_BAR_ROLES = ("hdr_bar", "hdr_warn")

_RFLUSH = "\x00 \x00"
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


def _addline(stdscr, row, segs, w):
    if segs is None:
        return
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

    def _draw(seglist, start, lim=None):
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
                except curses.error:
                    pass
            col += pw
        return col

    endcol = _draw(left, start_col)
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
            _draw(right, rcol, lim=band_lim if band else None)
    elif band and endcol < band_lim:
        _draw([(" " * (band_lim - endcol), fill_key)], endcol, lim=band_lim)


_OFFSET = 0                 # scroll offset — READ only in _draw (see module docstring)
_TOGGLE_ROWS = {}            # screen_y -> True, reset at the top of every _draw (mouse click map)
_CLICK_ROWS = {}             # screen_y -> _SELECTABLE entry (F-27 v9 row click map, §4.2.1 —
                              # filled from _SELECTABLE, NOT _live_targets(): base mode's first
                              # click would otherwise never see a target, and gating on
                              # _live_targets() would call control.is_excluded() at ~10fps)
_FOLD_ROWS = {}               # screen_y -> card_key (F-30, v10) — filled from `_FOLDABLE` the
                              # same way `_CLICK_ROWS` is filled from `_SELECTABLE`. Reset at the
                              # top of every `_draw`, same as `_TOGGLE_ROWS`/`_CLICK_ROWS`.
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


def _clamp_offset(off, total, body_h):
    return max(0, min(off, max(0, total - body_h)))


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


def _cursor_index(targets):
    """Where the cursor identity currently sits, or None if that target is gone."""
    if _CURSOR_ID is None:
        return None
    for i, e in enumerate(targets):
        if _entry_id(e) == _CURSOR_ID:
            return i
    return None


def _degradation_alert_rows(degradations, show_all=False):
    """Format failed-leg evidence without aggregating away sibling coordinates."""
    rows = []
    all_events = []
    for key, events in (degradations or {}).items():
        for event in events or ():
            if isinstance(event, dict):
                all_events.append(event)
    unique = {}
    for event in all_events:
        unique[event.get("event_id") or repr(sorted(event.items()))] = event
    grouped = {}
    orphan = []
    for event in unique.values():
        if event.get("_unattributed") or not event.get("route_id"):
            orphan.append(event)
        else:
            grouped.setdefault(event.get("route_id"), []).append(event)
    for route_id, events in grouped.items():
        events.sort(key=lambda item: item.get("ts", 0), reverse=True)
        visible = events if show_all else events[:3]
        for event in visible:
            node = event.get("route_node") or "?"
            if event.get("kind") == "chain-exhausted":
                rows.append("⚠ %s fallback-chain-exhausted · all hops exhausted" % node)
            elif int(event.get("dispatch_depth", 2) or 2) == 1:
                rows.append("⚠ contract violation: quick degradation row · %s · %s · %s" % (
                    route_id, node, event.get("fallback_hop") or "?"))
            else:
                index = event.get("parallel_leg_index")
                count = event.get("parallel_leg_count")
                coord = (" leg %s/%s" % (int(index) + 1, count)
                         if index is not None and count else "")
                rows.append("⚠ %s%s %s ✕ exit=%s %s" % (
                    node, coord, event.get("harness") or "?",
                    event.get("exit_code", "?"), event.get("reason") or "leg-failure"))
        if not show_all and len(events) > 3:
            rows.append("⚠ +%d more failed legs (--json)" % (len(events) - 3))
    for event in orphan:
        rows.append("⚠ unattributed degradation · %s" % (event.get("reason") or event.get("kind") or "event"))
    return rows


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


def _prompt_button_segs(stage, key):
    """[cancel]/[kill] click segments, in stage order (§4.4 coordinate inversion).

    confirm         → cancel-LEFT, kill-RIGHT
    confirm2/escalate → REVERSED: kill-LEFT, cancel-RIGHT

    The reversal is what makes a same-spot double-click fail-safe: confirm's [kill] (right)
    becomes confirm2's [cancel] (right) — the second click of a reflexive double-click lands
    on cancel, never on kill. See §4.4.1 for the staleness invariant this depends on."""
    kill_label = "[KILL]" if stage in ("confirm2", "escalate") else "[kill]"
    kill_seg = (kill_label, key)
    cancel_seg = ("[cancel]", key)
    if stage in ("confirm2", "escalate"):
        return [(" ", key), kill_seg, (" ", key), cancel_seg]
    return [(" ", key), cancel_seg, (" ", key), kill_seg]


def _prompt_variants(text_variants, stage, key):
    """Expand each text rung into a with-buttons rung, followed by a keyboard-only fallback
    at the SAME text width (buttons dropped). §4.5: when the buttons don't fit, the keyboard
    hint (and the name, if this rung still carries it) must not be sacrificed just to keep a
    click target — keyboard stays primary (prd.md:88·280). The final rung is never followed
    by a fallback: the narrowest rung is the guaranteed-fit floor and always carries buttons.
    """
    btn = _prompt_button_segs(stage, key)
    out = []
    last = len(text_variants) - 1
    for i, segs in enumerate(text_variants):
        out.append(list(segs) + btn)
        if i < last:
            out.append(list(segs))
    return out


def _prompt_hit_boxes(fsegs, row, width):
    """Click x-ranges for [kill]/[KILL]/[cancel] segments in fsegs, mirroring _addline's
    per-cell clip (:2126). A segment counts only if it was drawn WHOLE — a hitbox for a
    button that got clipped off the edge of the terminal must never survive (§4.5)."""
    hits = []
    col = 0
    edge = max(0, width - 1) if width else 0
    for text, _style in fsegs:
        if col >= edge:
            break
        avail = edge - col
        pw = 0
        for ch in text:
            cw = _cw(ch)
            if pw + cw > avail:
                break
            pw += cw
        stripped = text.strip()
        if stripped in ("[kill]", "[KILL]", "[cancel]") and pw == _dw(text):
            action = "cancel" if stripped == "[cancel]" else "kill"
            hits.append((row, col, col + pw, action))
        col += pw
    return hits


def _prompt_segs(prompt, width=None):
    """The confirmation bar. Always names the exact target and always shows the keys.

    A prompt is a safety affordance, not decoration: the footer is clipped at the terminal
    edge, so on a narrow screen the full phrasing would lose its tail — and the tail is where
    the keys are. ("press Y (capital) to kill" is 117 cells; at 60 the user would be asked to
    confirm without being told how.) Below the fit threshold every prompt therefore switches
    to a terse form that keeps the two things the user cannot act without: WHICH target, and
    WHICH keys. The pid is what survives on the identity side — it is unambiguous where a
    clipped name is not.
    """
    from . import control
    e = prompt["entry"]
    stage = prompt["stage"]
    who = "%s [pid %s] %s" % (e.get("label") or "?", e.get("pid"), e.get("state"))

    def pick(*variants):
        """First variant that fits. The ladder always ends in a pid-only form that fits any
        sane terminal, so a prompt can never be silently clipped."""
        for segs in variants:
            if width is None or sum(_dw(t) for t, _k in segs) <= width:
                return segs
        return variants[-1]

    if stage == "escalate":
        # SIGKILL — the one signal a process cannot refuse. It shares the warning bar with the
        # live-kill prompts: it is at least as destructive, so it may not read calmer than they do.
        return pick(*_prompt_variants([
            [(" ⚠ SIGTERM ignored for %ds — send SIGKILL to " % control.KILL_GRACE_SEC, "hdr_warn"),
             (who, "hdr_warn_key"), ("? press ", "hdr_warn"),
             ("Y", "hdr_warn_key"), (" (capital) · ", "hdr_warn"),
             ("Esc", "hdr_warn_key"), (" no", "hdr_warn")],
            [(" ⚠ SIGKILL ", "hdr_warn"), (who, "hdr_warn_key"), ("? ", "hdr_warn"),
             ("Y", "hdr_warn_key"), ("/", "hdr_warn"), ("Esc", "hdr_warn_key")],
            [(" ⚠ SIGKILL pid %s? " % e.get("pid"), "hdr_warn"),
             ("Y", "hdr_warn_key"), ("/", "hdr_warn"), ("Esc", "hdr_warn_key")],
        ], stage, "hdr_warn_key"))
    if stage == "confirm2":
        return pick(*_prompt_variants([
            [(" ⚠ LIVE session — confirm again: ", "hdr_warn"),
             (who, "hdr_warn_key"),
             (" — press ", "hdr_warn"), ("Y", "hdr_warn_key"),
             (" (capital) to kill · ", "hdr_warn"),
             ("Esc", "hdr_warn_key"), (" cancel", "hdr_warn")],
            [(" ⚠ LIVE — ", "hdr_warn"), (who, "hdr_warn_key"), (" — ", "hdr_warn"),
             ("Y", "hdr_warn_key"), (" kills · ", "hdr_warn"),
             ("Esc", "hdr_warn_key"), (" no", "hdr_warn")],
            [(" ⚠ LIVE pid %s — " % e.get("pid"), "hdr_warn"),
             ("Y", "hdr_warn_key"), (" kills · ", "hdr_warn"),
             ("Esc", "hdr_warn_key"), (" no", "hdr_warn")],
        ], stage, "hdr_warn_key"))

    warn = control.requires_double_confirm(e.get("state"), e.get("status"))
    if warn:
        return pick(*_prompt_variants([
            [(" ⚠ this session is WORKING — kill ", "hdr_warn"), (who, "hdr_warn_key"),
             ("? ", "hdr_warn"), ("y", "hdr_warn_key"), (" yes · ", "hdr_warn"),
             ("Esc", "hdr_warn_key"), (" cancel", "hdr_warn")],
            [(" ⚠ WORKING — kill ", "hdr_warn"), (who, "hdr_warn_key"), ("? ", "hdr_warn"),
             ("y", "hdr_warn_key"), ("/", "hdr_warn"), ("Esc", "hdr_warn_key")],
            [(" ⚠ kill pid %s (working)? " % e.get("pid"), "hdr_warn"),
             ("y", "hdr_warn_key"), ("/", "hdr_warn"), ("Esc", "hdr_warn_key")],
        ], "confirm", "hdr_warn_key"))
    # Benign target (unused/stale/dead). The middle rung matters: the full form overshoots 60
    # by ~3 cells, and dropping straight to pid-only would throw the NAME away while leaving
    # ~27 cells unused. Trim the decoration, keep the identity.
    return pick(*_prompt_variants([
        [(" kill ", "hdr_bar"), (who, "hdr_key"), ("? ", "hdr_bar"),
         ("y", "hdr_key"), (" yes · ", "hdr_bar"), ("Esc", "hdr_key"), (" cancel", "hdr_bar")],
        [(" kill ", "hdr_bar"), (who, "hdr_key"), ("? ", "hdr_bar"),
         ("y", "hdr_key"), ("/", "hdr_bar"), ("Esc", "hdr_key")],
        [(" kill pid %s (%s)? " % (e.get("pid"), e.get("state") or "?"), "hdr_bar"),
         ("y", "hdr_key"), ("/", "hdr_bar"), ("Esc", "hdr_key")],
    ], "confirm", "hdr_key"))


_ESC = 27


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
    elif ch in (ord("w"), ord("W")):
        _cycle_layout()
    elif ch in (ord("p"), ord("P")):
        # F-30 (prd.md:305) — process view toggle. Deliberately orthogonal to `w` (layout
        # cycle keeps working inside the process view, same as the group view).
        set_process_view(not _PROCESS_VIEW)
    else:
        return False
    return True


def _getmouse_xy():
    """Extract getmouse() coords, or (None, None) on failure. A bare `except: my = None`
    (the pre-v9 shape) still calls the mouse handler with a stale/unbound `mx` from a PRIOR
    getmouse() (or unbound entirely on the first call) → NameError crashes the TUI. Returning
    a matched pair makes "couldn't read the event" and "read it" mutually exclusive states."""
    try:
        _, mx, my, _mz, _bstate = curses.getmouse()
    except Exception:
        return None, None
    return mx, my


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
    if _PROMPT is not None:
        for row, x0, x1, action in _PROMPT_HITS:
            if my == row and x0 <= mx < x1:
                if action == "kill":
                    _handle_prompt_key(ord("y") if _PROMPT["stage"] == "confirm" else ord("Y"))
                else:
                    _handle_prompt_key(_ESC)
                break
        return True                      # rung 1: every other click while prompted is swallowed
    if my in _TOGGLE_ROWS:
        set_show_all(not _SHOW_ALL)
        return True
    if my in _FOLD_ROWS:
        entry = _FOLD_ROWS[my]
        # Invert whatever was ACTUALLY drawn (entry["folded"] is the resolved state — default
        # OR a prior explicit choice, §5.4), never a re-guessed default — otherwise a card whose
        # default happens to be folded would need two clicks before anything visibly moves.
        _ROUTE_FOLD[entry["card_key"]] = not entry["folded"]
        return True
    if my in _CLICK_ROWS:
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


def _close_job_row_if_registry(entry):
    """prd.md:255 — after a successful kill of a registry JOB row, close it. Sessions never
    touch the registry."""
    from . import control
    if entry.get("kind") != "job" or entry.get("source") != "jobs":
        return
    if entry.get("status") != "open" or not entry.get("slug"):
        return
    try:
        from .collectors import dispatch as _dispatch
        for jobs_path in _dispatch._candidate_jobs_paths(None):
            if control.close_registry_row(jobs_path, entry["slug"], entry.get("cwd") or ""):
                control.log_action(action="close_row", pid=entry.get("pid"), sid=None,
                                   state=entry.get("state"), approval="single",
                                   result="ok", reason=entry["slug"])
                return
    except Exception:
        pass


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


_MOUSE_HINT_MIN_WIDTH = 100   # R2-3: mouse is opt-in (needs `set -g mouse on` in tmux); only
                              # advertise it where there is slack to spare — keyboard stays
                              # primary and unconditional (prd.md:88·280).


_PROCESS_HINT_MIN_WIDTH = 80  # F-30 (v10) — the base footer is already tight at 60 cols (§5.1
                              # "60열 footer가 이미 빡빡하다"); this one short segment gets its
                              # own (lower than the mouse hint's 100) width floor rather than
                              # sharing _MOUSE_HINT_MIN_WIDTH, which is about a DIFFERENT
                              # capability (mouse opt-in) and would tie the two together for no
                              # reason.


def _footer_segs(select_mode, parts, width=None):
    hint = [("click", "hdr_key"), (" row · ", "hdr_bar")] \
        if width is not None and width >= _MOUSE_HINT_MIN_WIDTH else []
    p_hint = [("p", "hdr_key"), (" %s · " % ("group" if _PROCESS_VIEW else "process"), "hdr_bar")] \
        if width is None or width >= _PROCESS_HINT_MIN_WIDTH else []
    if select_mode:
        return [(" ", "hdr_bar"),
                ("↑↓/jk", "hdr_key"), (" move · ", "hdr_bar"),
                ("x", "hdr_key"), (" kill · ", "hdr_bar"),
                ("Esc", "hdr_key"), (" cancel · ", "hdr_bar"),
                ("q", "hdr_key"), (" quit", "hdr_bar"),
                (_RFLUSH, None), (" ".join(parts) + " " if parts else "", "hdr_bar")]
    wlbl = "wide/narrow/stack" if _LAYOUT == "auto" else ("%s!" % _LAYOUT)
    return [(" ", "hdr_bar"),
            ("q", "hdr_key"), (" quit · ", "hdr_bar"),
            ("r", "hdr_key"), (" refresh · ", "hdr_bar"),
            ("a", "hdr_key"), (" all · ", "hdr_bar"),
            ("w", "hdr_key"), (" " + wlbl + " · ", "hdr_bar")] + p_hint + hint + [
            ("jk", "hdr_key"), (" scroll · ", "hdr_bar"),
            ("s", "hdr_key"), (" select · ", "hdr_bar"),
            ("g/G", "hdr_key"), (" top/end", "hdr_bar"),
            (_RFLUSH, None), (" ".join(parts) + " " if parts else "", "hdr_bar")]


def reset_scroll():
    global _OFFSET
    _OFFSET = 0


def _draw(stdscr, sessions, jobs, section, malformed, memory=None, live_order=None,
          resources=None):
    global _OFFSET, _TOGGLE_ROWS, _CLICK_ROWS, _FOLD_ROWS, _PROMPT_HITS, _CURSOR_ID
    # reset before any early-return so a stale map never survives a click (§4.1 pattern) —
    # _PROMPT_HITS in particular must never carry the PRIOR stage's coordinates into this
    # draw (§4.4.1): that staleness is exactly what would defeat the confirm→confirm2
    # coordinate inversion.
    _TOGGLE_ROWS = {}
    _CLICK_ROWS = {}
    _FOLD_ROWS = {}
    _PROMPT_HITS = []
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    narrow = w < _NARROW_CUTOFF
    lines = _build_lines(sessions, jobs, section, narrow, malformed, layout=_layout_mode(w),
                         memory=memory, term_width=w, live_order=live_order,
                         resources=resources)
    body_h = max(1, h - 1)   # reserve 1 footer row

    # F-27: the cursor tracks a ROW, so the viewport follows it (not the reverse). Done before
    # the offset clamp so a cursor scrolled off-screen pulls the view back to itself.
    cur_line = None
    targets = _live_targets() if _SELECT_MODE else []
    if _SELECT_MODE and targets:
        i = _cursor_index(targets)
        if i is None:                    # the selected row is gone → re-anchor, never re-aim
            i = 0
            _CURSOR_ID = _entry_id(targets[0])
        cur_line = targets[i]["line"]
        if cur_line < _OFFSET:
            _OFFSET = cur_line
        elif cur_line >= _OFFSET + body_h:
            _OFFSET = cur_line - body_h + 1
    _OFFSET = _clamp_offset(_OFFSET, len(lines), body_h)

    # F-27 v9 (§4.2.1): the click map is built from _SELECTABLE, NOT _live_targets() — see the
    # module-level _CLICK_ROWS comment for why (base-mode first click / per-tick cost).
    _sel_by_line = {e["line"]: e for e in _SELECTABLE}
    # F-30 (v10, §5.4 Y2): same idiom, from `_FOLDABLE` (line-index map `_build_process_lines`
    # fills) to `_FOLD_ROWS` (screen-row map, offset-applied here — `_FOLDABLE` itself is only
    # ever line indices, exactly like `_SELECTABLE`).
    _fold_by_line = {e["line"]: e for e in _FOLDABLE}

    visible = lines[_OFFSET: _OFFSET + body_h]
    row = 0
    for segs in visible:
        _addline(stdscr, row, segs, w)
        fold_entry = _fold_by_line.get(_OFFSET + row)
        if fold_entry is not None:
            # ★ I7/§5.4 B2: a foldable row is decided FIRST and unconditionally — it never also
            # falls into the `_TOGGLE_ROWS` substring check below, even if its text happened to
            # contain "hidden"/"folded" (the card-label ban in _route_card/_degrade_card is the
            # other half of this invariant; this is the structural half).
            _FOLD_ROWS[row] = fold_entry
        elif segs is not None and len(segs) == 1 and (
                "hidden" in segs[0][0] or "folded" in segs[0][0]):
            _TOGGLE_ROWS[row] = True
        else:
            entry = _sel_by_line.get(_OFFSET + row)
            if entry is not None:
                _CLICK_ROWS[row] = entry
        row += 1
    if cur_line is not None and _OFFSET <= cur_line < _OFFSET + body_h:
        _highlight_row(stdscr, cur_line - _OFFSET, w)

    above = _OFFSET
    below = max(0, len(lines) - body_h - _OFFSET)
    parts = []
    if above:
        parts.append("↑%d" % above)
    if below:
        parts.append("↓%d" % below)
    # htop F-key bar (round-4): CYAN full-width, keycaps BOLD (dim is invisible on CYAN), the
    # scroll indicator rides the right edge. `w` cycles layout auto → narrow → wide.
    # A pending confirmation OWNS the footer: while it is up, the only thing that matters is
    # the decision in front of the user.
    fsegs = _prompt_segs(_PROMPT, w) if _PROMPT else _footer_segs(_SELECT_MODE, parts, w)
    _addline(stdscr, h - 1, fsegs, w)
    if _PROMPT:
        _PROMPT_HITS = _prompt_hit_boxes(fsegs, h - 1, w)
    stdscr.noutrefresh()
    curses.doupdate()


def _highlight_row(stdscr, y, w):
    """Reverse-video the cursor row. Painted over the already-drawn line so the row keeps its
    own colors and nothing about row assembly has to know about selection."""
    try:
        stdscr.chgat(y, 0, w, curses.A_REVERSE)
    except Exception:
        pass


def _loop(stdscr, collect_all, hfilter, section, interval):
    global _OFFSET, _BLINK_ON
    curses.curs_set(0)
    _init_colors()
    live_order = _LiveOrderState()
    # herdr (HERDR_ENV=1) grabs mouse events itself — enabling curses mouse reporting inside it
    # deadlocks/freezes the pane (user-observed freeze 2026-07-01). Keyboard is the primary path,
    # so skip mouse under herdr; mouse click-toggle stays available in a plain terminal.
    if not os.environ.get("HERDR_ENV"):
        try:
            curses.mousemask(curses.BUTTON1_CLICKED)
        except Exception:
            pass
    stdscr.timeout(200)                     # getch blocks ≤200ms → responsive keys
    sessions, jobs = collect_all(harness_filter=hfilter)
    resources = list(getattr(collect_all, "last_resource_jobs", []))
    malformed = _malformed()
    mem_snapshot = _collect_memory()
    last = time.time()
    _draw(stdscr, sessions, jobs, section, malformed, memory=mem_snapshot,
          live_order=live_order, resources=resources)
    while True:
        # wake exactly at the next 0.5s blink boundary (regular period) but stay key-responsive (≤200ms)
        _nb = (int(time.time() * 10) + 1) / 10.0   # 10fps wake — the spinner cadence
        stdscr.timeout(max(20, min(100, int((_nb - time.time()) * 1000) + 1)))
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            return 0
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 1)
        # --- F-27: a pending confirmation swallows ALL keys. Nothing else can happen while
        # the user is being asked, and only an explicit yes proceeds. ---
        if _PROMPT is not None:
            # §4.4.1 — _handle_mouse MUST be called from inside this block (not before it with
            # its own `continue`): the _draw two lines below is what repopulates _PROMPT_HITS
            # for the CURRENT stage before the next getch can land a click. Placing the mouse
            # call ahead of this block would let a click 2 read a stale (pre-transition) map
            # and silently defeat the confirm→confirm2 coordinate inversion (§4.4).
            if ch == curses.KEY_MOUSE:
                mx, my = _getmouse_xy()
                if mx is not None:
                    _handle_mouse(mx, my)
            else:
                _handle_prompt_key(ch)
            _draw(stdscr, sessions, jobs, section, malformed, memory=mem_snapshot,
                  live_order=live_order, resources=resources)
            continue
        if _SELECT_MODE:
            if _handle_select_key(ch):
                _draw(stdscr, sessions, jobs, section, malformed, memory=mem_snapshot,
                      live_order=live_order, resources=resources)
                continue
        elif ch in (ord("s"), ord("S"), ord("x"), ord("X")):
            # Enter selection mode. `x` doubles as the enter shortcut so the "press x to kill"
            # intent works from a cold start; it selects, it never kills on the first press.
            if not _enter_select(_live_targets()):
                _set_action("no selectable rows")
            _draw(stdscr, sessions, jobs, section, malformed, memory=mem_snapshot,
                  live_order=live_order, resources=resources)
            continue

        # --- base mode: scroll keys UNCHANGED (F-27 regression budget = 0) ---
        if _handle_base_key(ch, body_h):
            pass
        elif ch == curses.KEY_MOUSE:
            mx, my = _getmouse_xy()
            if mx is not None:
                _handle_mouse(mx, my)
        # KEY_RESIZE: no special handling needed — _draw's clamp re-clamps against the new
        # body_h below; do NOT reset _OFFSET here (would destroy scroll position).

        force = ch in (ord("r"), ord("R"))
        now = time.time()
        if force or (now - last) >= interval:
            sessions, jobs = collect_all(harness_filter=hfilter)
            resources = list(getattr(collect_all, "last_resource_jobs", []))
            malformed = _malformed()
            mem_snapshot = _collect_memory()
            last = now
        _poll_pending_kill()     # F-27 grace window — non-blocking; may raise a re-prompt
        _BLINK_ON = (int(now * 2) % 2 == 0)     # ~2 Hz working-dot blink (manual — A_BLINK unreliable)
        # redraw every wake (covers KEY_RESIZE, blink and tick) — _draw clamps _OFFSET internally.
        _draw(stdscr, sessions, jobs, section, malformed, memory=mem_snapshot,
              live_order=live_order, resources=resources)


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
        return curses.wrapper(_loop, collect_all, hfilter, section, interval)
    except KeyboardInterrupt:
        return 0
    except Exception as e:  # pragma: no cover
        sys.stderr.write("fleet: curses failed: %s\n" % e)
        return 1
