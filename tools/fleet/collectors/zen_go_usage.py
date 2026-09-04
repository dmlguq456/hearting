"""OpenCode Go account usage via the official `GET /zen/go/v1/usage` endpoint.

Why this exists (2026-09-04): the Go plan's rolling-5h/weekly/monthly limits were
console-only ("no usage api — plan quota is console-only") until upstream shipped the
public usage endpoint (anomalyco/opencode #31084, PR #2879; #16017 tracker). Probed live
2026-09-04: the plain inference API key (auth.json `opencode-go` entry) reads it — no
browser OAuth, unlike the workspace console. Same observer posture as usage_api.py:
a READ-ONLY GET with the user's own key, TTL-cached, failure → None → the usage header
falls back to "no usage api" (an opencode-go key missing stays an honest absence).

Response shape (probed 2026-09-04): {"usage": {"rolling" | "weekly" | "monthly":
{"status", "percent", "resetsAt"(ISO-8601)}}}. rolling maps to the legacy 5h slot;
weekly is a Mon 00:00 window, NOT rolling 7d, so it must not claim the rl_7d slot —
it rides rl_windows with its true label.
"""
import json
import os
import time
import urllib.error
import urllib.request

_TTL = 180.0
_STALE_MAX = 900.0
_cache = {"ts": 0.0, "ok_ts": 0.0, "data": None}

_ENDPOINT = "https://opencode.ai/zen/go/v1/usage"
_USER_AGENT = "opencode-fleet-usage/1"


def _auth_path():
    return os.environ.get("OPENCODE_AUTH") or os.path.join(
        os.environ.get("OPENCODE_DATA_DIR")
        or os.path.expanduser("~/.local/share/opencode"), "auth.json")


def _token():
    try:
        with open(_auth_path(), encoding="utf-8") as f:
            entry = json.load(f).get("opencode-go")
    except Exception:
        return None
    if isinstance(entry, dict) and entry.get("key"):
        return entry["key"]
    return None


def _epoch(v):
    """resetsAt → epoch seconds; accepts epoch numbers or ISO-8601 strings, else None."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


def _fetch():
    tok = _token()
    if not tok:
        return None
    # Cloudflare in front of opencode.ai bans the default `Python-urllib/3.x`
    # fingerprint with 403 "error code: 1010" (measured 2026-09-04: same key,
    # same machine — 403 with the default UA, 200 with any explicit UA). An
    # explicit User-Agent is therefore load-bearing here, not cosmetic.
    req = urllib.request.Request(
        _ENDPOINT, headers={"Authorization": "Bearer " + tok,
                            "Content-Type": "application/json",
                            "User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # key-present-but-rejected (rotated/expired/wrong scope) — a distinct
            # state from a transient outage; the render shows "key rejected".
            return {"error": "auth", "detail": e.code}
        return None
    except Exception:
        return None
    windows_in = (d if isinstance(d, dict) else {}).get("usage")
    if not isinstance(windows_in, dict):
        return None
    out = {"rl_5h": None, "rl_7d": None, "rl_ms": [], "rs_5h": None, "rs_7d": None,
           "rl_windows": []}
    for key, label in (("rolling", "5h"), ("weekly", "wk"), ("monthly", "mo")):
        w = windows_in.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("percent"), (int, float)):
            continue
        pct = round(w["percent"])
        reset = _epoch(w.get("resetsAt"))
        out["rl_windows"].append([label, pct, reset])
        if key == "rolling":
            out["rl_5h"], out["rs_5h"] = pct, reset
    if not out["rl_windows"]:
        return None
    return out


def account_usage():
    """TTL-cached Go plan usage {rl_5h, rl_7d, rl_windows} for the opencode-go plan, or None.

    Serve stale data on failure so one timeout does not blank the gauges: a failed refresh
    keeps the previous payload up to _STALE_MAX; only a 15min-long outage drops the
    honest state. Auth-reject payloads ({"error": "auth"} — key rotated/expired/rejected)
    are served only after the same staleness ceiling, so a rejected key keeps showing the
    last-known gauges (dimmed stale) before flipping to the explicit rejection state."""
    now = time.time()
    if now - _cache["ts"] > _TTL:
        d = _fetch()
        _cache["ts"] = now              # failures throttle too — no retry storm inside the TTL
        if isinstance(d, dict) and "error" not in d:
            _cache["data"] = d
            _cache["ok_ts"] = now
        elif now - _cache["ok_ts"] > _STALE_MAX:
            _cache["data"] = d           # None (no key / transient) or the error marker
    return _cache["data"]
