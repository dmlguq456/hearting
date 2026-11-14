"""Non-blocking main-session notices from the latest request's input usage.

500K/700K are absolute token thresholds, not historical usage or window ratios.
Each threshold speaks once while the session stays above it; a session jumping
across both gets the urgent line and both are consumed. A latest request at or
below 400K -- the session's own compaction, by session-tidy or the runtime, or
a new native session ID -- clears them, so the next crossing speaks once more;
a small dip under 500K (old tool results trimmed) is not a compaction.
A zero or unknown count observes nothing. Adapters own user/model presentation.
These work-preservation notices do not promise headroom before native compaction.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile

THRESHOLDS = (500_000, 700_000)
# A request this small means the context was compacted, not merely trimmed.
REARM_BELOW = THRESHOLDS[0] * 4 // 5
# The 700K line says what the 2026-10-07 decision asks for: a default tidy at the
# next work boundary, and that 정리만 does not shrink the context.
URGENT = "하던 일 하나를 넘긴 뒤 /session-tidy 기본 정리('정리만'은 문맥을 줄이지 않음)"


def main_session(env=None) -> bool:
    env = os.environ if env is None else env
    from session_tidy import is_worker
    return not is_worker(env) and env.get("AGENT_SESSION_ROLE", "").lower() not in {
        "owner", "worker", "auxiliary", "reviewer",
    }


def input_tokens(usage: object, keys: tuple[str, ...]) -> int | None:
    if not isinstance(usage, dict) or keys[0] not in usage:
        return None
    values = [usage.get(key, 0) for key in keys]
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 for value in values):
        return None
    return int(sum(values))


def claude_usage(transcript: object, session_id: str) -> int | None:
    """Read the bounded tail, never another session or sidechain's usage."""
    if not isinstance(transcript, str) or not transcript:
        return None
    try:
        path = Path(transcript)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return None
        with path.open("rb") as handle:
            start = max(0, info.st_size - 8 * 1024 * 1024)
            handle.seek(start)
            if start:
                handle.readline()
            lines = handle.read().splitlines()
        for raw in reversed(lines):
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if (isinstance(row, dict) and row.get("type") == "system"
                    and row.get("subtype") == "compact_boundary"
                    and row.get("sessionId", session_id) == session_id):
                return None  # No request in the new context yet; pre-compact usage is stale.
            if (not isinstance(row, dict) or row.get("type") != "assistant"
                    or row.get("isSidechain") or row.get("isMeta")
                    or row.get("sessionId", session_id) != session_id):
                continue
            message = row.get("message")
            # Missing latest usage is unknown; an older request is not current.
            return input_tokens(message.get("usage") if isinstance(message, dict) else None, (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    except OSError:
        pass
    return None


def codex_usage(transcript: object, session_id: str) -> int | None:
    """Read the exact main-session Codex rollout's latest input usage."""
    if not isinstance(transcript, (str, os.PathLike)) or not transcript or not session_id:
        return None
    try:
        path = Path(transcript)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024 * 1024:
            return None
        with path.open("rb") as handle:
            head = handle.read(min(info.st_size, 64 * 1024)).splitlines()
            meta = None
            for raw in head:
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(row, dict) and row.get("type") == "session_meta":
                    meta = row.get("payload")
                    break
            if not isinstance(meta, dict) or meta.get("id") != session_id:
                return None
            source = meta.get("source")
            if isinstance(source, dict) and source.get("subagent"):
                return None
            start = max(0, info.st_size - 8 * 1024 * 1024)
            handle.seek(start)
            if start:
                handle.readline()
            lines = handle.read().splitlines()
        for raw in reversed(lines):
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            if row.get("type") == "compacted":
                return None
            payload = row.get("payload")
            if row.get("type") != "event_msg" or not isinstance(payload, dict) \
                    or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            usage = info.get("last_token_usage") if isinstance(info, dict) else None
            return input_tokens(usage, ("input_tokens",))
    except (OSError, ValueError, TypeError):
        return None
    return None


def codex_notice(payload: dict) -> str:
    """Return the shared once-per-session notice for a Codex main prompt."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
        return ""
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid or not main_session():
        return ""
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        try:
            from fleet.token_budget import find_codex_rollout
            home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            transcript, reason = find_codex_rollout(home, sid)
            if reason != "ok":
                return ""
        except Exception:
            return ""
    tokens = codex_usage(transcript, sid)
    text = notice("codex", sid, tokens)
    if not text:
        return ""
    try:
        from fleet.token_budget import tail_codex_token_count, codex_compaction_pressure, compaction_imminent
        line = tail_codex_token_count(Path(transcript))
        pressure = codex_compaction_pressure(sid, transcript, line)
        if compaction_imminent(pressure):
            text += " · 자동 압축 임박(native 추정)"
    except Exception:
        pass  # Missing diagnostic evidence never changes prompt admission.
    return text


def notice(harness: str, session_id: str, tokens: int | None, *, state_dir=None, env=None) -> str:
    """Atomically consume newly exceeded thresholds; at or below 400K, clear them.

    Any unavailable state is silent and leaves the record as it is."""
    if (not session_id or not isinstance(tokens, int) or isinstance(tokens, bool)
            or tokens <= 0):
        return ""  # Zero is a runtime's empty or interrupted request, not a smaller context.
    crossed = [threshold for threshold in THRESHOLDS if tokens > threshold]
    base = Path(state_dir) if state_dir else Path(
        os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
    ) / "hearting/context-alert"
    digest = hashlib.sha256(f"{harness}\0{session_id}".encode()).hexdigest()
    target = base / (digest + ".json")
    if not crossed and (tokens > REARM_BELOW or not os.path.lexists(target)):
        return ""  # Nothing crossed and nothing to clear: no state is touched.
    if not main_session(env):
        return ""
    temporary = None
    try:
        base.mkdir(parents=True, exist_ok=True)
        with target.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                seen = json.loads(target.read_text())
            except FileNotFoundError:
                seen = []
            if not isinstance(seen, list) or any(item not in THRESHOLDS for item in seen):
                return ""
            if not crossed:
                # Back at or below 400K: the context was compacted. Both may speak again.
                target.unlink()
                return ""
            fresh = [threshold for threshold in crossed if threshold not in seen]
            if not fresh:
                return ""
            fd, temporary = tempfile.mkstemp(prefix=".context-alert-", dir=base)
            with os.fdopen(fd, "w") as handle:
                json.dump(sorted(set(seen + crossed)), handle)
            os.replace(temporary, target)
            temporary = None
        suffix = (URGENT if max(fresh) == THRESHOLDS[1]
                  else "/session-tidy로 작업 상태 저장 권장")
        return f"입력 문맥 약 {tokens / 1000:.0f}K — {suffix} · 자동 압축 여유 보장 아님"
    except (OSError, ValueError, TypeError):
        return ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def claude_notice(payload: dict) -> str:
    if (payload.get("hook_event_name") != "UserPromptSubmit"
            or payload.get("agent_id") or not main_session()):
        return ""
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return ""
    return notice("claude", sid, claude_usage(payload.get("transcript_path"), sid))


def main() -> None:
    # OpenCode supplies only the latest assistant's token fields, never bodies.
    try:
        raw = sys.stdin.readline(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("session_id"), str):
            return
        usage = payload.get("tokens")
        if not isinstance(usage, dict):
            return
        cache = usage.get("cache", {})
        if not isinstance(cache, dict):
            return
        count = input_tokens({"input": usage.get("input"), "read": cache.get("read", 0),
                              "write": cache.get("write", 0)}, ("input", "read", "write"))
        text = notice("opencode", payload["session_id"], count)
        if text:
            print(text)
    except (OSError, ValueError, TypeError):
        pass


if __name__ == "__main__":
    main()
