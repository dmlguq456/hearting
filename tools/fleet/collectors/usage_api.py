"""Account usage via the official OAuth endpoint — the same source as claude's /usage screen.

Why this exists (2026-07-02): per-model buckets ("Fable only" weekly limit) are visible in
/usage but are NOT written to any on-disk artifact the passive taps read — the statusline
stdin (v2.1.193/198) still ships only five_hour/seven_day, and stats-cache.json has no rate
data. The only source is `GET /api/oauth/usage` (bundle: "fetchUtilization"), so this is the
one deliberate exception to the disk-only observer rule: a READ-ONLY call with the user's own
token (identical to what the harness itself does), TTL-cached so the 2s render tick never
hammers the API. Failure of any kind → None → the usage bar falls back to the tap values.

Response shape (probed 2026-07-02): top-level five_hour/seven_day {utilization, resets_at}
plus a `limits[]` array — kind=session (5h) / weekly_all (7d) / weekly_scoped with
scope.model.display_name (e.g. "Fable") → the per-model bucket.
"""
import json
import os
import time
import urllib.request

_TTL = 180.0              # the oauth endpoint 429s under 60s polling (probed 2026-07-02)
_STALE_MAX = 900.0        # keep serving last-good through transient failures up to 15min
_cache = {"ts": 0.0, "ok_ts": 0.0, "ms_ts": 0.0, "data": None}


def _home():
    # Claude credentials belong to Claude Code, never to Hearting's packaged
    # source root pinned by the Fleet launcher.
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def _epoch(v):
    """resets_at → epoch seconds; accepts epoch numbers or ISO-8601 strings, else None."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


def _token():
    try:
        with open(os.path.join(_home(), ".credentials.json")) as f:
            return (json.load(f).get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


def _fetch():
    tok = _token()
    if not tok:
        return None
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={"Authorization": "Bearer " + tok,
                 "anthropic-beta": "oauth-2025-04-20",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            d = json.load(r)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    out = {"rl_5h": None, "rl_7d": None, "rl_ms": [], "rs_5h": None, "rs_7d": None}
    for lim in (d.get("limits") or []):
        if not isinstance(lim, dict) or not isinstance(lim.get("percent"), (int, float)):
            continue
        pct = round(lim["percent"])
        kind = lim.get("kind")
        if kind == "session":
            out["rl_5h"] = pct
            out["rs_5h"] = _epoch(lim.get("resets_at"))
        elif kind == "weekly_all":
            out["rl_7d"] = pct
            out["rs_7d"] = _epoch(lim.get("resets_at"))
        elif kind == "weekly_scoped":
            name = (((lim.get("scope") or {}).get("model") or {}).get("display_name")) or "model"
            lbl = name.split()[0].lower()
            if not any(x[0] == lbl for x in out["rl_ms"]):
                out["rl_ms"].append([lbl, pct])
    # fallback to the top-level objects if limits[] was missing/partial
    for key, fld, rs in (("five_hour", "rl_5h", "rs_5h"), ("seven_day", "rl_7d", "rs_7d")):
        if out[fld] is None and isinstance((d.get(key) or {}).get("utilization"), (int, float)):
            out[fld] = round(d[key]["utilization"])
            out[rs] = _epoch(d[key].get("resets_at"))
    if out["rl_5h"] is None and out["rl_7d"] is None and not out["rl_ms"]:
        return None
    return out


def account_usage():
    """TTL-cached account usage {rl_5h, rl_7d, rl_ms} for the claude account, or None.

    Serve stale data on failure so a single timeout does not make usage flicker:
    timeout used to overwrite last-good with None, blanking the per-model buckets for a
    whole TTL. Now a failed refresh keeps the previous payload up to _STALE_MAX; only a
    15min-long outage drops to None (honest fallback to the tap values)."""
    now = time.time()
    if now - _cache["ts"] > _TTL:
        d = _fetch()
        _cache["ts"] = now              # failures throttle too — no retry storm inside the TTL
        if d is not None:
            # Carry prior values forward on partial success to avoid flicker.
            # the endpoint sometimes returns 200 WITHOUT the limits[] array — 5h/7d survive via
            # the top-level fallback but the per-model buckets (and resets) arrive empty and
            # would overwrite last-good as if valid. Carry the previous bucket/reset values
            # through such partial responses, bounded by the same staleness ceiling.
            prev = _cache["data"]
            if prev and now - _cache["ms_ts"] <= _STALE_MAX:
                if not d["rl_ms"] and prev.get("rl_ms"):
                    d["rl_ms"] = prev["rl_ms"]
                for k in ("rs_5h", "rs_7d"):
                    if d.get(k) is None and prev.get(k) is not None:
                        d[k] = prev[k]
            if d["rl_ms"] and (not prev or d["rl_ms"] is not (prev or {}).get("rl_ms")):
                _cache["ms_ts"] = now   # bucket actually seen fresh (not carried) → new anchor
            _cache["data"] = d
            _cache["ok_ts"] = now
        elif now - _cache["ok_ts"] > _STALE_MAX:
            _cache["data"] = None
    return _cache["data"]
