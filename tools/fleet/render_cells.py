"""Fleet render: glyphs, constants and the shared row cells (liveness, model, harness).

Split out of `render.py`, which re-exports every name defined here.
"""
import glob
import json
import math
import os
import re
import time
import unicodedata
from collections.abc import Mapping
from .model import dash
from . import gitinfo


try:
    import curses
except ImportError:  # native Windows without windows-curses: plain --once / --json still work
    curses = None

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
    "steward_pink": 219,
    "orange": 137,
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
# Usage-header labels only (user 2026-09-29: "상단 opencode→opencode go"). The quota in that
# row is the opencode-GO account, not the bare opencode runtime, and 'claude code' already
# does exactly this disambiguation one row up. Scoped to the usage header on purpose: the
# session card and dispatch rows say `opencode` because those name the runtime itself, and
# renaming them there would mislabel what they describe.
_USAGE_BADGE_TEXT = {"opencode": "opencode go"}
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
_LOOPS_KEYS = ("oncall", "note", "study", "drill")

# fg color_key → (hue, attr) decomposition (spec §5.2) — the basis for composing (fg, tint_bg)
# pairs. hue: d=default w=soft white g=green y=yellow v=vanilla r=red c=cyan
# m=magenta l=blue.
# Keys absent here render as default-hue plain text under tint (safe degradation).
_A_B, _A_D = _A_BOLD, _A_DIM

# F-76b — the NAME zone wears the harness hue, but on its OWN keys rather than reusing
# `hb_*`/`h_*`. The color is identical; the separate key is what keeps a row's name
# distinguishable from its harness cell, which several consumers (`_stack_split`, the
# width suites) resolve by color key alone. Bright = a live top-level session, dim =
# a dispatch row or lost telemetry — the same brightness axis the harness cell uses.
_NAME_HUE = {"claude": "c", "codex": "m", "opencode": "l"}
_NAME_KEY = {h: "nm_" + h for h in _NAME_HUE}
_NAME_KEY_DIM = {h: "nmd_" + h for h in _NAME_HUE}
# Every key a row's NAME zone can carry. Consumers that locate the name segment by color
# key (the width/title suites, `_stack_split`) read THIS instead of listing keys inline —
# F-76b broke a dozen such lists at once by moving names onto the harness hue.
NAME_KEYS = frozenset(list(_NAME_KEY.values()) + list(_NAME_KEY_DIM.values())
                      + ["nm_other", "nmd_other", "nm_dead"])


# ---------- color ----------
# curses pair numbers. Model families take 10-17 (eight families since Sol/Astra/Luna
# split), so the structural bars must sit above the steward badge, not inside the family
# block: when the bars reused 15-17, `fam_astra/luna/gpt` text inherited the header's
# chrome background (user 2026-10-27: "astra 모델 컬러가 왜이래.. 왜 틴트가 있어").
# Keep everything except the 256-color tints below 64 so 8-color terminals still get bars.
_FAMILY_PAIR_START = 10
_STEWARD_PAIR = 18
_HDR_BAR_PAIR = 19
_HDR_WARN_PAIR = 20
_HDR_BLK_PAIR = 21
_TINT_PAIR_START = 22


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
    "decision": "answer needed",
    "approval": "approval needed",
    # Claude calls the runtime event a permission prompt while Codex calls the equivalent
    # user gate an approval request. Fleet keeps that producer evidence intact, but presents
    # one user-facing term for the same action.
    "permission": "approval needed",
    "elicitation": "input needed",
}

# group "cooling" state (user 2026-07-03): a directory with NO active work whose newest session
# A recent transcript write reads as cooling after completion, between hot
# (green ● + green-bold title) and cold (no glyph). It gets a grey ring + time-since-last-activity
# in the header, so a just-finished repo says "done & waiting", not fully dormant. Tune freely.
_COOL_WINDOW_MIN = 180
# Shape-size gradient: recent active states are larger and filled; cold groups use a ring.
_COOL_FILLED = "●"      # Recently completed directory within the cooling window.
_COOL_RING = "○"        # Long-inactive directory.


def _blink_phase(now):
    return int(now) % 2 == 0
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


_FAMILIES = ("sol", "astra", "luna", "opus", "sonnet", "haiku", "fable", "gpt")


def _model_family(model):
    """Model-family token from a model/bucket name: 'Opus 4.8'→opus, 'gpt-5.5'→gpt, 'fable'→fable.
    The named Codex models keep their own tokens ('gpt-6.1-sol'→sol, 'Astra'→astra,
    'gpt-6-luna'→luna) so each gets one stable identity colour in every view; any
    other gpt id stays on the shared 'gpt' token. Unknown (glm/deepseek/…) → 'other'
    (default fg — distinct from the colored families)."""
    m = (model or "").lower()
    for fam in _FAMILIES:
        if fam in ("sol", "astra", "luna"):
            matched = re.search(r"\b%s\b" % fam, m)
        elif fam == "gpt":
            matched = "gpt" in m
        else:
            matched = fam in m
        if matched:
            return fam
    return "other"


_MODEL_HARNESSES = ("claude", "codex", "opencode")


def _model_key(model, dim=False, harness=None):
    """A model name's colour key: its family's, else the hue of the harness running it.

    A model with no family of its own (glm/deepseek/… under OpenCode) wears that harness's
    existing ``hb_*`` (``h_*`` dim) key instead of the default foreground, so the palette
    gains no per-model colour. Without a known harness the ``other`` key stays."""
    family = _model_family(model)
    if family == "other" and harness in _MODEL_HARNESSES and str(model or "").strip():
        return ("h_" if dim else "hb_") + harness
    return ("famd_" if dim else "fam_") + family


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
    role = _ROLE_MODEL_NAME.get(name.lower())
    if role:
        # Only the bare lowercase alias the configs/dispatch env actually export is
        # resolved against the catalog; a proper-noun value already came from an
        # observed display name and is not rewritten by today's catalog.
        name = (_resolved_alias_model(name) if name == name.lower() else None) or role
    return _shorten_model_suffix(name)


# Variant/effort tokens that carry no information: the runtime picked its own default and
# never told us which. Rendering the word costs most of the model cell to say nothing (and
# clipped 'opencode-go/glm-5.2' down to 'open'), so these render as no effort at all —
# the same honest blank a missing effort already gets.
_EMPTY_EFFORT = {"runtime-default", "inherit", "unknown", "default"}

# What a session that reported the runtime's own default effort shows in place of a
# level: an explicit, dimmed '(·default)'. Distinct from the blank an unobserved effort
# gets, without claiming any ramp position — the ramp (`_eff_key`) is for real levels only.
_DEFAULT_EFFORT_MARK = "default"


def _effort_text(effort):
    return "" if not effort or str(effort).strip().lower() in _EMPTY_EFFORT else str(effort)


_MODEL_ID_RE = re.compile(r"^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?(?:-\d{8})?$")
# Non-claude ids carry a tier qualifier and/or a version that the display name does not
# need: 'muse-spark-1.3-contributor' → 'muse-spark', 'space-bunny-free' → 'space-bunny'
# (user 2026-09-29). A version is dropped ONLY when a hyphen survives in front of it, so
# the single-token families keep the version they were always shown with — 'glm-5.2'
# stays 'glm-5.2' (the 2026-08-07 decision), and 'gpt-5.6' stays 'gpt-5.6'.
_MODEL_TIER_RE = re.compile(r"-(?:contributor|free)$")
_MODEL_VERSION_RE = re.compile(r"-\d+\.\d+$")


def _shorten_model_suffix(name):
    """Drop a trailing tier qualifier ('-contributor'/'-free'), then a version segment —
    but the version only when a multi-token name would still be left standing."""
    if not name:
        return name
    name = _MODEL_TIER_RE.sub("", name)
    remainder = _MODEL_VERSION_RE.sub("", name)
    return remainder if "-" in remainder else name


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
        return _shorten_model_suffix(name)
    fam, major, minor = m.group(1).capitalize(), m.group(2), m.group(3)
    return "%s %s" % (fam, major + ("." + minor if minor else ""))


# A bare role alias names a family, not one model. The Claude runtime's own catalog is
# the checked source for which concrete id this account currently resolves that family
# to; a version is never written into this file. A missing, malformed, or ambiguous
# catalog keeps the family word (the honest fallback this table always had).
_ALIAS_FAMILIES = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku", "fable": "fable"}
_MODEL_CATALOG_MAX_BYTES = 16 * 1024 * 1024
_ALIAS_MODEL_CACHE = {"stamp": None, "models": {}}


def _claude_catalog_paths():
    """The runtime's already-written catalog caches, in the same root order the explorer uses."""
    config = (os.environ.get("HEARTING_MODEL_CONFIG_HOME")
              or os.environ.get("CLAUDE_CONFIG_DIR")
              or os.path.join(os.path.expanduser("~"), ".claude"))
    return tuple(sorted(glob.glob(os.path.join(config, "cache", "model-catalog", "*.json"))))


def _catalog_model_records(document):
    """The model records in the runtime catalogs' bounded public shapes."""
    if not isinstance(document, dict):
        return ()
    catalog = document.get("catalog")
    config = catalog.get("config") if isinstance(catalog, dict) else None
    records = config.get("models") if isinstance(config, dict) else None
    if not isinstance(records, list):
        records = document.get("models")
    if not isinstance(records, list):
        return ()
    return tuple(item for item in records if isinstance(item, dict))


def _resolved_alias_models():
    """Role family → the one concrete id the account's runtime catalog marks ``main``.

    Reads only the cache files the runtime already wrote. A family with no record,
    zero ``main`` entries, or two different ``main`` ids across accounts stays
    unresolved, so display never guesses. Cached by each file's (mtime, size) so a
    frame never re-reads an unchanged catalog.
    """
    paths = _claude_catalog_paths()
    stamps = []
    for path in paths:
        try:
            info = os.stat(path)
        except OSError:
            continue
        stamps.append((path, info.st_mtime_ns, info.st_size))
    stamp = tuple(stamps)
    cached = _ALIAS_MODEL_CACHE
    if cached["stamp"] == stamp:
        return cached["models"]
    found = {}
    for path, _mtime, size in stamps:
        if size > _MODEL_CATALOG_MAX_BYTES:
            continue
        try:
            with open(path, encoding="utf-8") as stream:
                document = json.load(stream)
        except (OSError, ValueError, UnicodeError, TypeError):
            continue
        for item in _catalog_model_records(document):
            if item.get("section") != "main":
                continue
            runtime = item.get("runtime")
            family = runtime.get("family") if isinstance(runtime, Mapping) else None
            model_id = item.get("id")
            if not isinstance(family, str) or not isinstance(model_id, str) or not model_id:
                continue
            found.setdefault(family.lower(), []).append(model_id)
    resolved = {}
    for family, ids in found.items():
        distinct = tuple(dict.fromkeys(ids))
        if family in _ALIAS_FAMILIES.values() and len(distinct) == 1:
            resolved[family] = distinct[0]
    cached["stamp"] = stamp
    cached["models"] = resolved
    return resolved


def _resolved_alias_model(name):
    """Versioned display for a bare role alias, or None when the catalog cannot confirm it."""
    family = _ALIAS_FAMILIES.get(str(name).strip().lower())
    if family is None:
        return None
    model_id = _resolved_alias_models().get(family)
    if not model_id:
        return None
    return _short_model_id(model_id)


def _dispatch_display_model(name):
    if not name:
        return name
    raw = str(name).strip()
    token = raw.lower()
    role = _ROLE_MODEL_NAME.get(token)
    if role:
        return (_resolved_alias_model(raw) if raw == token else None) or role
    return _short_model_id(name)


def _job_display_model(job, parent_model=None):
    return _dispatch_display_model(getattr(job, "resolved_model", None) or job.model or parent_model)


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


def _half_up(value):
    """Half-up rounding (Python's round() is banker's rounding — 0.5 would fall DOWN)."""
    return int(math.floor(float(value) + 0.5))


def _native_context_gauge(entity):
    """Without an applied compaction trigger, draw the native gauge: raw window and percent.

    An unknown or disabled policy still has a measured window and usage; showing nothing would
    hide what every session showed before applied triggers existed."""
    from collections.abc import Mapping
    field = entity.get if isinstance(entity, Mapping) else lambda key: getattr(entity, key, None)
    policy = field("context_policy")
    if isinstance(policy, Mapping) and policy.get("state") == "applied":
        return None, None
    window = field("context_window_tokens")
    if type(window) is not int or window <= 0:
        window = None
    raw = policy.get("raw_percentage") if isinstance(policy, Mapping) else None
    if (isinstance(raw, (int, float)) and not isinstance(raw, bool)
            and math.isfinite(raw) and raw >= 0):
        return min(99, round(raw)), window
    active = field("active_context_tokens")
    if field("harness") == "codex":
        from .token_budget import codex_context_used_pct
        return codex_context_used_pct(active, window), window
    if (type(active) is int and active >= 0 and type(window) is int and window > 0):
        return min(99, round(100 * active / window)), window
    return None, window


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


def _badge_cell(hn, width):
    """Harness badge padded to exactly `width` cells with the LAST cell always a
    space — the guaranteed-gap idiom `_harness_model_cell` documents, applied to
    the narrow/stack badge whose field is narrowed by the row prefix. Without it
    a badge that exactly fills (or overflows) the narrowed field ran straight
    into the stage label / session name: `claude codeclaim-b`, `claude cexecute`
    (F-84, pre-dates F-63 — visible since the arrow-indent era). A badge that
    cannot fit whole falls back to its first word (`claude`) rather than a
    mid-word cut."""
    hn = hn or ""
    width = max(2, width)
    if len(hn) >= width:
        first = hn.split(" ")[0]
        hn = first if len(first) < width else hn[:width - 1]
    return _pad(hn, width)


_BR_TTL = 15.0
_BR_CACHE = {"ts": 0.0, "map": {}}


_git_branch = gitinfo.branch
_wt_count = gitinfo.worktree_count
_IO_UNSET = object()


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
_TAG_W = 5                    # F-100a: `[46] ` — the session-tag badge: the 2-hex tag in dim
                              # brackets plus its own gap cell, between the status glyph and
                              # the harness text (the reverse chip carried its gap inside the
                              # run; the outline badge shipped without one — `[bc]claude code`,
                              # user-visible 2026-09-03). A tagless row draws the same 5 blank
                              # cells so the harness column never moves. Charged INSIDE the
                              # harness field (_HMW / _HW): wide keeps 35 for a 33-cell worst
                              # case; narrow keeps 11, where `claude code` no longer fits whole
                              # and `_badge_cell` falls back to `claude` exactly as the narrow
                              # dispatch rows already do. No other ledger — _NAME_COL included —
                              # changes.
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
_ELAPSED_GLYPH = ""           # F-76 (user 2026-08-14 "경과 시간을 나타내는 아이콘이 좀 정신
                              # 사나운데"): the elapsed value carries NO glyph. This restores
                              # the original `_CLOCK` convention ("icons caused width and
                              # readability issues") that F-68b broke when it gave the in-card
                              # elapsed a `⏳` tag and the rest of the board followed: a
                              # 129-line demo board printed 44 of them, two cells and a color
                              # emoji each. Crucially it also un-overloads `_WAIT_GLYPH` — ⏳
                              # meant "this session is deliberately WAITING" (F-47) long before
                              # it meant "elapsed", and with elapsed silent the badge is a
                              # signal again instead of wallpaper. `fmt_min`'s unit spacing
                              # ('5h 20m') is what keeps the bare value reading as a duration.

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


_LOC_CONTENT_W = _BRANCH_SUFFIX_W - 2      # 12 — content budget inside "( )"


def _location_prefix_segs(kind, repo, budget):
    """(segs, cells). Never exceeds `budget`; identity(repo) outranks branch (F-97b)."""
    if kind == "foreign-repo" and repo:
        # repo is allocated FIRST and clipped only against the hard floor that keeps
        # 1 cell for the arrow, 1 for the gap and >=1 for the branch.
        repo_max = max(1, budget - 3)
        shown_repo = _clip_w(str(repo), repo_max)
        return ([("→", "loc_repo"), (shown_repo, "loc_repo"), (" ", None)],
                1 + _dw(shown_repo) + 1)
    if kind == "isolated-wt":
        if budget < 5:            # "⌂wt" + gap + >=1 branch cell does not fit
            return [], 0          # narrow layouts keep branch identity, drop the token
        return [("⌂wt", "loc_repo"), (" ", None)], 4
    return [], 0                  # primary / unknown / missing repo -> no token at all


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


def _model_cell(model, effort, width, dim=False, harness=None):
    """Render model and effort together as one flowing phrase, padded to width."""
    name = _clean_model(dash(model)) or "—"
    sfx = _effort_text(effort)
    lkey = _model_key(model, dim=dim, harness=harness)
    if sfx:
        name = name[: max(1, width - len(sfx) - 4)]
        pad = max(0, width - len(name) - len(sfx) - 3)
        return [(name, lkey), (" (" + sfx + ")", _eff_key(sfx, dim))] + [(" " * pad, None)]
    return [(_pad(name[: width - 1], width), lkey)]


def _harness_model_cell(harness, model, effort, width, hkey, dim=False, unknown="?",
                       effort_default=False):
    """F-33 (v11, 사용자 확정 2026-07-16) — WIDE-layout harness field with model/effort folded
    in as a parenthetical: 'claude code (Fable 5·xhigh)'. The harness text keeps its
    existing hb_*/h_* badge color (`hkey`); the parenthetical reuses `_model_cell`'s
    family/effort colors and the flush '·' stays dim (user 2026-07-16: the spaced
    ' · ' read too wide — the freed cells go to the model name). No model value ->
    the parenthetical is omitted entirely (honest gap, F-3), matching a dead/stale row that
    has no live telemetry to show. Always returns segments summing to exactly `width` cells
    (long names/ids clip the same way `_model_cell` already did, never overflow) — the last
    cell is always left as guaranteed padding so a maxed-out clip never runs the closing `)`
    straight into the name column (the `_NAME_GAP` collision, same idiom, new spot).

    `effort_default` marks "the runtime reported an effort token, but it was its own
    uninformative default". That is not an effort level, so it never claims a real one —
    it renders a small dim '(default)' instead, distinguishing it from a session that
    reported no effort at all (both would otherwise be the same blank)."""
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
            segs += [(" (", "dim"), (name, _model_key(model, dim=dim, harness=harness)),
                     ("·", "dim"), (eff, _eff_key(eff_full, dim)), (")", "dim")]
            used += 2 + len(name) + 1 + len(eff) + 1
        elif effort_default and room >= len(name) + 1 + len(_DEFAULT_EFFORT_MARK):
            segs += [(" (", "dim"), (name, _model_key(model, dim=dim, harness=harness)),
                     ("·", "dim"), (_DEFAULT_EFFORT_MARK, "dim"), (")", "dim")]
            used += 2 + len(name) + 1 + len(_DEFAULT_EFFORT_MARK) + 1
        elif room > 0:
            nm = name[: max(1, room)]
            segs += [(" (", "dim"), (nm, _model_key(model, dim=dim, harness=harness)), (")", "dim")]
            used += 2 + len(nm) + 1
    if used < width:
        segs.append((" " * (width - used), None))
    return segs


_DIAL_MAX = 34                # F-78 (user 2026-08-14 "뜰때 안뜰때가 일관성이 없고 …
                              # 전부 다 뜰때 가로로 너무 길어지는데"): ONE options-dial budget
                              # for every dispatch row. It used to be `28 if in_card else None`
                              # — framed rows folded their env components away while unframed
                              # ones printed everything, so the same information appeared on
                              # one row and not the row above it, for a reason invisible to the
                              # reader. Measured cost of the unbounded side: a parentless
                              # depth-2 row at 140 columns rendered 177 cells wide.
                              # 34, not the old in-card 28: the widest real dial that still
                              # carries its unit is `code-execute(strong) / dev/backend`,
                              # and 28 folded that unit away on every row that had one.
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


# User 2026-09-18 ("최대 30칸을 좀 더 늘려 40칸 정도로"): a main session's stage cell holds its
# route-chain window up to this many cells; past that, details fold, then the current node
# stands alone with counts.
_SESSION_CELL_MAX = 40
# The narrow/stack card's L2 before its stage cell: 4-cell inset, the elapsed cell under the
# harness column (_HW), the model cell (_MW), and the 2-cell gap — `_session_row_2line` builds
# exactly this prefix, pinned by test.
_NARROW_L2_STAGE_COL = 4 + _HW + _MW + 2


def _session_routing_column(layout, name_width=None):
    """Shared raw display-column ledger for MAIN routes and OWNER capabilities."""
    if layout != "wide":
        return _NARROW_L2_STAGE_COL
    return _NAME_COL + (name_width or _NW_S) + _BRANCH_SUFFIX_W + _WIDE_STAGE_GAP


def _pad_to_column(segs, column):
    used = sum(_dw(text) for text, _key in segs)
    return segs + [(" " * max(0, column - used), None)]


def _narrow_session_cell_budget(term_width):
    """The stage-cell budget of a narrow/stack session card: the historical 28 cells as a
    floor (a stack card never showed less), growing into what L2 has left after its prefix
    and the tinted row's six edge cells, capped like the wide cell."""
    if not term_width:
        return 28
    return max(28, min(_SESSION_CELL_MAX, int(term_width) - 6 - _NARROW_L2_STAGE_COL))


def _drop_past_stages(items, cur_i, max_width):
    """SD-F2 (prd.md:164) — a breadcrumb's information value is "where now", not "where
    I've been": fold PAST stages (i < cur_i) first, earliest first, so the active stage (and
    anything after it) survives longest. Whole-component drop (a stage + its separator, F-9(c)
    idiom) — never a mid-token tail-cut.

    Tail fold (F-65): an EARLY-stage long route has no past to fold — a 7-node strong
    route at its first node overflowed its zone by ~45 cells, the wide row wrapped in the
    terminal, and the wrap shifted every following screen line so the card's ╰─── close
    rail visually vanished (user 2026-08-14 "경계선이 하단에 안 떠서"). When past-folding
    alone cannot fit, drop FUTURE stages from the END — never the current one or its
    immediate successor — and account a dim `+N` counter in the same width ledger."""
    items = list(items)
    def width(extra_cells=0):
        w = sum(_dw(t) for _i, t, _k in items) + extra_cells
        n = len(items) + (1 if extra_cells else 0)
        return w + max(0, n - 1) * 3   # " › " separators, _dw == 3
    while width() > max_width and items and items[0][0] < cur_i:
        items = items[1:]
    dropped_tail = 0
    def marker_cells():
        return _dw("+%d" % (dropped_tail + 1)) if dropped_tail or width() > max_width else 0
    while (width(marker_cells()) > max_width and len(items) > 1
           and items[-1][0] > cur_i):
        items.pop()
        dropped_tail += 1
    if dropped_tail:
        items.append((items[-1][0] + 1, "+%d" % dropped_tail, "dim"))
    return items


def _route_current_index(route_seq):
    """The breadcrumb's CURRENT node index — the single judge every current-hue consumer
    shares (the lit token and the F-64c rail must never disagree on color)."""
    for want in ("attention", "active", "degraded", "recovering", "reconciling"):
        found = next((i for i, (_nid, st) in enumerate(route_seq) if st == want), None)
        if found is not None:
            return found
    done_idx = [i for i, (_nid, st) in enumerate(route_seq) if st == "done"]
    return min((done_idx[-1] + 1) if done_idx else 0, max(0, len(route_seq) - 1))


# ---------- curses (live) ----------
# display-width aware clipping — emoji/CJK render 2 cells but len()==1, so advancing col by
# len() drew the next segment 1 col early and overwrote the previous field's last char
# (e.g. the directory name lost a char after the 📁). Count real cells instead.
_WIDE = set("🧠✨⏳📁🚀🛰⚡📋⚙📊🐛📈🔬💻⏱↻")


def _cw(ch):
    o = ord(ch)
    if (unicodedata.combining(ch) or o == 0xFE0F or 0x200B <= o <= 0x200F
            or o == 0x2060):                                    # combining / VS16 / zero-width
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
