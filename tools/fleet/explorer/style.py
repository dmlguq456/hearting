"""Explorer style aliases that resolve only through Fleet's existing palette."""

KEY_ALIASES = {
    "text": "loc_repo", "body": "loc_repo", "selected": "loc_repo", "primary": "section_head",
    "muted": "dim", "label": "dim", "faint": "name_dim", "head": "section_head",
    "now_sub": "name_dim",
    "now_sub": "name_dim",
    "soft": "loc_repo", "title_dim": "name_dim", "line": "dim", "accent": "hearting_name",
    "danger": "g_blocked", "blocked": "g_blocked", "warning": "lvl_y_dim", "success": "grp_live",
    "idle": "g_work_off", "done": "grp_live", "dead": "g_dead", "work": "g_work",
    "hearting": "hearting_name", "ver": "version_release", "spin": "g_spin",
    "tag_b": "dim", "steward": "tag_steward", "host": "herdr_on", "key": "section_head",
    "gpu_5090": "gpu_rtx5090", "gpu_4090": "gpu_rtx4090",
    "claude": "claude", "codex": "codex", "opencode": "opencode",
    "colhead": "section_head", "sect": "section_head",
    "lvl_g": "grp_live", "lvl_y": "g_idle", "lvl_r": "g_dead",
    "lvl_r_flat": "g_blocked", "chip_on": "hearting_name", "eff_medium": "branch_s",
    # `fam_*` model keys pass through unchanged: one key per family keeps every viewer
    # on the same hue. A local alias here is how Sol/Astra/Luna once shared one yellow.
    "bold": "section_head", "code": "fam_sonnet", "tool": "g_work",
}

REFERENCE_STYLES = {
    ("d", 0, 1): "dim", ("d", 0, 0): "branch_s",
    ("w", 0, 0): "loc_repo", ("w", 0, 1): "name_dim", ("w", 1, 0): "section_head",
    ("r", 0, 0): "g_blocked", ("r", 0, 1): "lvl_r_dim", ("r", 1, 0): "g_dead",
    ("g", 0, 0): "grp_live", ("g", 0, 1): "g_work_off", ("g", 1, 0): "g_work",
    # "c" here is the stage palette's cyan (`_stage_hue`), not the Claude harness hue (orange, 11-18).
    ("c", 0, 0): "fam_opus", ("c", 0, 1): "famd_opus", ("c", 1, 0): "fam_opus",
    # "m" and "l" are the stage palette's magenta and blue (the Fable and Sonnet families' hues), not the Codex and OpenCode harness colors (11-18).
    ("m", 0, 0): "fam_fable", ("m", 0, 1): "famd_fable", ("m", 1, 0): "eff_xhigh",
    ("l", 0, 0): "fam_sonnet", ("l", 0, 1): "version_release", ("l", 1, 0): "fam_sonnet",
    ("v", 0, 0): "g_spin", ("v", 0, 1): "g_spin_dim", ("v", 1, 0): "hearting_name",
    ("y", 0, 0): "g_idle", ("y", 0, 1): "lvl_y_dim", ("y", 1, 0): "stg3_on",
    ("p", 1, 0): "tag_steward",
}


# A FINISHED route row (dead or done) recedes one rung on the SAME palette: every value
# here is an existing key, so one state keeps one weight on every screen. Prefix pairs
# (`fam_*→famd_*`, `eff_*→effd_*`, `hb_*→h_*`) cover the families; the explicit entries
# are the tabled keys a route row actually paints. A key absent here is already dim and
# stays put — a bright gap is allowed to remain, a new colour is not invented.
RECEDE_KEYS = {
    "loc_repo": "name_dim",              # titles and soft body
    "branch_s": "dim",                   # plain-weight separators
    "section_head": "name_dim",          # defensive: headers stay outside the row
    "tag_steward": "tag_dim",            # a finished steward tag loses the pink emphasis
    "g_work": "g_work_off", "grp_live": "g_work_off",
    "g_idle": "lvl_y_dim", "lvl_y": "lvl_y_dim", "stg3_on": "lvl_y_dim",
    "g_spin": "g_spin_dim",
    "g_dead": "lvl_r_dim", "lvl_r": "lvl_r_dim", "lvl_r_flat": "lvl_r_dim",
    "g_blocked": "lvl_r_dim", "g_blocked_chip": "lvl_r_dim",
}

_HARNESS_DIM = {"claude": "h_claude", "codex": "h_codex", "opencode": "h_opencode"}


def normalize_style(key=None, tint=None):
    if key is None:
        return None, tint
    if isinstance(key, (tuple, list)) and len(key) == 3:
        key = REFERENCE_STYLES.get((str(key[0]), int(bool(key[1])), int(bool(key[2]))), "unknown")
    canonical = KEY_ALIASES.get(str(key), str(key))
    if canonical.startswith("hb_") and canonical[3:] not in ("claude", "codex", "opencode", "other"):
        canonical = "hb_other"          # a harness Fleet does not name wears the neutral hue
    return canonical, tint


def style_for(key, tint=None):
    return normalize_style(key, tint)


def steward_flag_key(session, lit=True):
    """Pulse the existing live supervisor role independently of its model turn."""
    if session.get("steward") and session.get("state") not in ("dead", "stale") and not lit:
        return "tag_steward_dim"
    return "tag_steward"


def recede_key(key):
    """The dim rung of one painted key: the glyph, tint and hue family keep their meaning."""
    canonical, _tint = normalize_style(key)
    if canonical is None:
        return None
    mapped = RECEDE_KEYS.get(canonical)
    if mapped is not None:
        return mapped
    if canonical.startswith("hb_"):
        return _HARNESS_DIM.get(canonical[3:], "dim")
    if canonical.startswith("fam_"):
        return "famd_" + canonical[4:]
    if canonical.startswith("eff_"):
        return "effd_" + canonical[4:]
    # 11-18: a finished route's kind word and bracket keep their field's hue, one rung down (dim).
    if canonical.startswith(("kind_", "kinds_", "kindb_")):
        return "kindq_" + canonical.split("_", 1)[1]
    return canonical
