"""Bounded OAuth usage GET using an exact, private credential snapshot.

Fleet's usage_cache owns last-good data, retry timing and single-flight. This
module never refreshes credentials or infers identity from adjacent profiles.
"""
import hashlib
import http.client
import json
import math
import os
import socket
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime


class _Credential:
    # No token or fingerprint in repr/logs. Only usage_cache's private runtime
    # metadata persists the opaque generation, never the public snapshot.
    __slots__ = ("token", "generation", "reason")

    def __init__(self, token=None, generation=None, reason=None):
        self.token, self.generation, self.reason = token, generation, reason


def _home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def _credential():
    path = os.path.join(_home(), ".credentials.json")
    try:
        with open(path, encoding="utf-8") as fh:
            document = json.load(fh)
        oauth = document.get("claudeAiOauth") if isinstance(document, dict) else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        if not isinstance(token, str) or not token.strip():
            return _Credential(reason="credential-invalid")
        # Account/profile files cannot prove that a newly rotated token has the
        # same quota. Revalidate on every different credential, even in one home.
        generation = hashlib.sha256(
            (os.path.realpath(path) + "\0" + token).encode("utf-8")).hexdigest()
        return _Credential(token, generation)
    except FileNotFoundError:
        return _Credential(reason="credential-not-found")
    except (ValueError, TypeError):
        return _Credential(reason="credential-invalid")
    except OSError:
        return _Credential(reason="credential-store-read-failed")


def _token():
    return _credential().token


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            from datetime import datetime
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            return result if math.isfinite(result) else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _retry_at(value, now):
    """RFC 9110 Retry-After: integer seconds or HTTP date; otherwise 60s."""
    try:
        value = value.strip()
        if value.isascii() and value.isdecimal():
            result = now + int(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return now + 60.0
            result = date.timestamp()
        if math.isfinite(result):
            return max(now, result)
    except (AttributeError, ValueError, TypeError, OverflowError, OSError):
        pass
    return now + 60.0


def _error(error, reason, retry_after=None):
    return {"error": error, "reason": reason, "account_scope": "current-credential",
            "retry_at": _retry_at(retry_after, time.time())}


def _percent(value):
    return (round(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 100 else None)


def _quota(document):
    if not isinstance(document, dict):
        return None
    out = {"rl_5h": None, "rl_7d": None, "rl_ms": [], "rs_5h": None, "rs_7d": None}
    limits = document.get("limits")
    for limit in limits if isinstance(limits, list) else []:
        if not isinstance(limit, dict):
            continue
        percent = _percent(limit.get("percent"))
        if percent is None:
            continue
        kind = limit.get("kind")
        if kind in ("session", "weekly_all"):
            suffix = "5h" if kind == "session" else "7d"
            out["rl_" + suffix] = percent
            out["rs_" + suffix] = _epoch(limit.get("resets_at"))
        elif kind == "weekly_scoped":
            scope = limit.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            name = model.get("display_name") if isinstance(model, dict) else None
            label = name.split()[0].lower() if isinstance(name, str) and name.strip() else "model"
            if not any(row[0] == label for row in out["rl_ms"]):
                out["rl_ms"].append([label, percent])
    for key, suffix in (("five_hour", "5h"), ("seven_day", "7d")):
        bucket = document.get(key)
        if out["rl_" + suffix] is None and isinstance(bucket, dict):
            out["rl_" + suffix] = _percent(bucket.get("utilization"))
            out["rs_" + suffix] = _epoch(bucket.get("resets_at"))
    return out if (out["rl_5h"] is not None or out["rl_7d"] is not None or out["rl_ms"]) else None


def _fetch(credential=None):
    credential = _credential() if credential is None else credential
    if not credential.token:
        return _error("missing-credential", credential.reason or "credential-not-found")
    # Use exactly the credential captured by the controller, not a second read
    # that could send B and label its response A during an A→B→A transition.
    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={"Authorization": "Bearer " + credential.token,
                 "anthropic-beta": "oauth-2025-04-20", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            document = json.load(response)
    except urllib.error.HTTPError as error:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        status = error.code
        error.close()  # Never read or expose the response body or message.
        if status in (401, 403):
            return _error("auth", "authentication-rejected", retry_after)
        if status == 429:
            return _error("rate-limit", "usage-rate-limited", retry_after)
        return _error("unknown", "usage-http-error", retry_after)
    except (TimeoutError, socket.timeout):
        return _error("timeout", "usage-timeout")
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return _error("timeout", "usage-timeout")
        return _error("network", "usage-unreachable")
    except (ValueError, TypeError, http.client.HTTPException):
        return _error("response-invalid", "usage-response-invalid")
    except OSError:
        return _error("network", "usage-unreachable")
    quota = _quota(document)
    return quota if quota is not None else _error("response-invalid", "usage-response-invalid")


def account_usage():
    """Compatibility quota dict/None via Fleet's one cache and refresh lease."""
    from . import usage_cache
    snapshot = usage_cache.account_usage("claude", usage="refresh")
    payload = snapshot.get("payload")
    return payload if isinstance(payload, dict) and "error" not in payload else None
