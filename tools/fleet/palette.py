"""Fleet's shared semantic palette, usable without initializing a screen."""
from .render_cells import _A_B, _A_D, _A_REVERSE, _NAME_HUE, _MUTED_256

_HUE_OF = {
    None: ("d", 0), "dim": ("d", _A_D), "head": ("d", _A_D),
    "section_head": ("w", _A_B), "unknown": ("d", _A_D),
    "version_release": ("l", _A_D), "version_build": ("d", _A_D),
    "version_dirty": ("y", _A_D), "version_method": ("d", _A_D),
    # F-76b retired "name_work"/"name_idle": names wear the harness hue now (`nm_*`).
    # `name_dim` stays, but as the general DIM-LABEL key it always also was (options
    # dial, stage prefixes) — it is not a name key and is absent from `NAME_KEYS`.
    "name_dim": ("w", _A_D), "nm_dead": ("w", _A_D),
    "grp": ("w", _A_B), "branch_s": ("d", 0), "cost_hi": ("w", _A_B),
    # F-97b: one step brighter than the dim branch/parens so a foreign-repo or
    # isolated-worktree token reads as a distinct identity signal, not decoration.
    "loc_repo": ("w", 0),
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
    # F-100b: the `herdr` word leading the context row — soft white, plain weight (the
    # first cut was a reverse chip; user 2026-09-03 "너무 두껍고 거슬리네"), so it reads as a
    # WHERE axis distinct from both the harness hue and the status hues.
    "herdr_on": ("w", 0),
    # F-100a: the `[46]` session-tag badge — brackets dim, the tag itself in the same soft
    # white as `herdr_on` (user 2026-09-03: white, so it separates from the harness hue).
    "tag": ("w", 0), "tag_dim": ("d", _A_D),
    # F-100c: a STEWARD session (depth −1: `steward on`, or it watched/started a session) wears
    # bright pink text: distinct from white/yellow, without a special background.
    "tag_steward": ("p", _A_B),
    "tag_steward_dim": ("p", _A_D),
    # Badge text, NOT the glyph: plain yellow, distinct from the dim g_unused glyph so the
    # ●>○>◌ ink-weight gradient still reads.
    "g_unused_b": ("y", 0),
    "lvl_g": ("g", 0), "lvl_y": ("y", 0), "lvl_r": ("r", _A_B),
    "lvl_g_dim": ("g", _A_D), "lvl_y_dim": ("y", _A_D),
    "lvl_r_dim": ("r", _A_D), "resource_active": ("w", 0),
    "base_host": ("g", _A_B),
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
    "fam_fable": ("m", 0), "fam_sol": ("y", 0), "fam_astra": ("o", 0),
    "fam_luna": ("w", 0), "fam_gpt": ("y", 0), "fam_other": ("d", 0),
    "famd_opus": ("c", _A_D), "famd_sonnet": ("l", _A_D), "famd_haiku": ("g", _A_D),
    "famd_fable": ("m", _A_D), "famd_sol": ("y", _A_D), "famd_astra": ("o", _A_D),
    "famd_luna": ("w", _A_D), "famd_gpt": ("y", _A_D), "famd_other": ("d", _A_D),
    "gpu_rtx6000": ("l", _A_D), "gpu_rtx4090": ("c", _A_D),
    "gpu_rtx5090": ("g", _A_D),
    "gpu_blackwell": ("g", _A_D), "gpu_hopper": ("m", _A_D),
    "gpu_ada": ("c", _A_D), "gpu_ampere": ("l", _A_D),
    "gpu_turing": ("y", _A_D), "gpu_legacy": ("o", _A_D), "gpu_other": ("d", _A_D),
    "gpu_rtx6000_active": ("l", 0), "gpu_rtx4090_active": ("c", 0),
    "gpu_rtx5090_active": ("g", 0),
    "gpu_blackwell_active": ("g", 0), "gpu_hopper_active": ("m", 0),
    "gpu_ada_active": ("c", 0), "gpu_ampere_active": ("l", 0),
    "gpu_turing_active": ("y", 0), "gpu_legacy_active": ("o", 0), "gpu_other_active": ("d", 0),
    # stage palette indices 0-4 = blue·cyan·green·yellow·magenta (see _stage_raw)
    "stg0_on": ("l", _A_B), "stg1_on": ("c", _A_B), "stg2_on": ("g", _A_B),
    "stg3_on": ("y", _A_B), "stg4_on": ("m", _A_B),
    "stg0_off": ("l", _A_D), "stg1_off": ("c", _A_D), "stg2_off": ("g", _A_D),
    "stg3_off": ("y", _A_D), "stg4_off": ("m", _A_D),
    # Box frame: the five stage hues at ONE stroke weight (never bold — box-drawing
    # glyphs change apparent stroke, and in some terminals advance width, under
    # A_BOLD) and now permanently DIMMED. F-72 (user 2026-08-14 "메인 세션이 가장
    # 주인인데 서브 세션이 화려해서 구분감이 줄어든다" → "일단 테두리 dim만 하자"):
    # a dispatch box drew MORE ink than the main session it sits beside, inverting
    # the ownership hierarchy. The hue still says which stage owns the unit; the
    # brightness no longer competes with the session that spawned it.
    # F-68d note kept: tinted rows resolve color through THIS table (never _COLOR),
    # so a frame key missing here silently collapses to the default dim hue.
    "frm0": ("l", _A_D), "frm1": ("c", _A_D), "frm2": ("g", _A_D),
    "frm3": ("y", _A_D), "frm4": ("m", _A_D),
    "frm_idle": ("d", _A_D),
    # F-76c (user 2026-08-14 "메인세션의 설명을 밝은 흰색으로 하라는거야"): a MAIN
    # session's second line is BRIGHT white. F-70 had dimmed it ("볼드만 유지하고 다시
    # 어둡게해") because it read as loud beside the dispatch rows — but that was before
    # F-72 dimmed the card frames and F-76b moved names onto the harness hue. With the
    # surroundings quieted, the owner's own sentence is the thing that should carry ink.
    "now_main": ("w", _A_B),
    # F-76b (user 2026-08-14 "설명을 흰색으로 바꾸고"): the subtitle/NOW sentence is
    # WHITE-hued like the main session's own second line, not the default grey. With
    # row names now wearing the harness hue (F-76b above), color separates identity
    # from prose and WEIGHT separates main (bold-dim `now_main`) from spawned.
    "now_sub": ("w", _A_D),
    # F-77: the product name. The palette reserves its hues by MEANING —
    # green/yellow/red are status, cyan/magenta/blue are harness identity, soft-white
    # bold is a row's focal point — so the board's own name takes `v` (vanilla), the one
    # hue on no such axis. It shares that hue with the working spinner, which is a
    # per-row glyph and can never be confused with a word on the first line.
    "hearting_name": ("v", _A_B),
}
for _h, _hue_ch in _NAME_HUE.items():
    _HUE_OF["nm_" + _h] = (_hue_ch, 0)
    _HUE_OF["nmd_" + _h] = (_hue_ch, _A_D)
_HUE_OF["nm_other"] = ("w", _A_B)      # unknown harness keeps the historical white name
_HUE_OF["nmd_other"] = ("w", _A_D)


# The same hue/fallback pairs paint curses tint rows and the session shell.
_HUE_PALETTE = {
    "d": (None, -1), "w": ("soft", 7), "g": ("green", 2),
    "y": ("yellow", 3), "v": ("vanilla", 3), "r": ("red", 1),
    "c": ("cyan", 6), "m": ("magenta", 5), "o": ("orange", 3),
    "p": ("steward_pink", 5), "l": ("blue", 4),
}


def hue_colors(colors=256):
    return {hue: (_MUTED_256[name] if colors >= 256 and name else fallback)
            for hue, (name, fallback) in _HUE_PALETTE.items()}


def style_components(key, *, colors=256, no_color=False):
    """Shared semantic style as bounded data for a non-curses terminal painter."""
    from .explorer.style import normalize_style
    key, _tint = normalize_style(key)
    hue, attrs = _HUE_OF.get(key, ("d", 0))
    return {"foreground": None if no_color else hue_colors(colors).get(hue, -1),
            "bold": bool(attrs & _A_B) and not no_color,
            "dim": bool(attrs & _A_D) and not no_color,
            "reverse": bool(attrs & _A_REVERSE) and not no_color}
