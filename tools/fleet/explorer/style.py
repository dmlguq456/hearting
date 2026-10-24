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
    "fam_opus": "hb_claude", "fam_gpt": "g_idle", "bold": "section_head", "code": "hb_opencode", "tool": "g_work",
}

REFERENCE_STYLES = {
    ("d", 0, 1): "dim", ("d", 0, 0): "branch_s",
    ("w", 0, 0): "loc_repo", ("w", 0, 1): "name_dim", ("w", 1, 0): "section_head",
    ("r", 0, 0): "g_blocked", ("r", 0, 1): "lvl_r_dim", ("r", 1, 0): "g_dead",
    ("g", 0, 0): "grp_live", ("g", 0, 1): "g_work_off", ("g", 1, 0): "g_work",
    ("c", 0, 0): "hb_claude", ("c", 0, 1): "h_claude", ("c", 1, 0): "hb_claude",
    ("m", 0, 0): "hb_codex", ("m", 0, 1): "h_codex", ("m", 1, 0): "eff_xhigh",
    ("l", 0, 0): "hb_opencode", ("l", 0, 1): "version_release", ("l", 1, 0): "hb_opencode",
    ("v", 0, 0): "g_spin", ("v", 0, 1): "g_spin_dim", ("v", 1, 0): "hearting_name",
    ("y", 0, 0): "g_idle", ("y", 0, 1): "lvl_y_dim", ("y", 1, 0): "stg3_on",
    ("p", 1, 0): "tag_steward",
}


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
