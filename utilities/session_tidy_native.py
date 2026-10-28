"""Exact native session targets for tidy; no inherited terminal authority.

Claude's official `attach <job-id>` is the transport. The inbox socket only
carries peer text, so it is never used to execute /clear. No token is saved.
Other runtimes retain their existing verified-pane/manual transport.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import termios
import time
import unicodedata

from pane_ownership import _caller_runtime, _process


def live(target):
    """The runtime's current session declaration, bound to this exact process."""
    try:
        pid = int(target["pid"])
        before = _process(pid)
        if not before or before[5] != target["start"]:
            return None
        path = Path(target["home"]) / "sessions" / f"{pid}.json"
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_size > 65536:
            return None
        record = json.loads(path.read_text())
        if (record.get("pid") != pid or str(record.get("procStart")) != before[5]
                or record.get("kind") != "bg" or record.get("jobId") != target["job"]
                or not record.get("sessionId") or _process(pid) != before):
            return None
        return record
    except (OSError, ValueError, KeyError, TypeError):
        return None


def caller_target(harness, env=None):
    if harness != "claude":
        return None
    env = os.environ if env is None else env
    own, service = _caller_runtime(os.getpid(), harness)
    if not own:
        return None
    if service:
        # Claude reuses a prewarmed bg-spare process as a real job without
        # changing its argv. Its *live* kind=bg PID/start/job declaration below
        # proves promotion. A shared daemon or PTY host still has no authority.
        try:
            words = Path(f"/proc/{own[0]}/cmdline").read_bytes().split(b"\0")
            if words[0] != b"claude bg-spare" and b"bg-spare" not in words[1:3]:
                return None
        except OSError:
            return None
    home = env.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    try:
        record = json.loads((Path(home) / "sessions" / f"{own[0]}.json").read_text())
        job = record.get("jobId")
        if not isinstance(job, str) or not re.fullmatch(r"[0-9a-f]{8}", job):
            return None
        target = {"pid": own[0], "start": own[5], "job": job,
                  "home": str(Path(home).resolve()), "binary": os.readlink(f"/proc/{own[0]}/exe")}
        return target if live(target) else None
    except (OSError, ValueError, TypeError):
        return None


def idle_reason(target, sid):
    record = live(target)
    if not record or record["sessionId"] != sid:
        return "target-changed"
    if record.get("waitingFor"):
        return "form-open"
    return "" if record.get("status") == "idle" else "not-idle-native"


def wait_idle(target, sid, timeout_ms):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        reason = idle_reason(target, sid)
        if not reason:
            return "idle"
        if reason != "not-idle-native":
            return reason
        time.sleep(0.25)
    return "timeout"


class Snapshot:
    """Decode the cursor-addressed repaint of a private attachment.

    A fresh full repaint is required; unknown screen-changing controls refuse
    the snapshot. Cells retain SGR faint so a suggestion is never a draft.
    This is deliberately bounded to the attachment's fixed 40x120 terminal.
    """
    TOKEN = re.compile(r"\x1b\[([0-9;?:><=]*)([ -/]*)([@-~])|"
                       r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|"
                       r"\x1b[()][A-Za-z0-9]|\x1b[=>]|(.)", re.S)

    def __init__(self):
        self.rows, self.cols = 40, 120
        self.cells = [[(" ", False)] * self.cols for _ in range(self.rows)]
        self.row = self.col = 0
        self.faint = self.painted = False
        self.valid = True

    def feed(self, raw):
        for m in self.TOKEN.finditer(raw):
            params, intermediate, cmd, char = m.group(1, 2, 3, 4)
            if char is not None:
                if char == "\r": self.col = 0
                elif char == "\n": self.row = min(self.rows - 1, self.row + 1)
                elif char == "\b": self.col = max(0, self.col - 1)
                elif char == "\t": self.col = min(self.cols - 1, (self.col // 8 + 1) * 8)
                elif char == "\x1b": self.valid = False
                elif char >= " ":
                    if unicodedata.combining(char):
                        if self.col: self.cells[self.row][self.col - 1] = (self.cells[self.row][self.col - 1][0] + char, self.faint)
                    else:
                        width = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
                        if self.col + width > self.cols: self.row, self.col = min(self.rows - 1, self.row + 1), 0
                        self.cells[self.row][self.col] = (char, self.faint)
                        if width == 2: self.cells[self.row][self.col + 1] = ("", self.faint)
                        self.col += width
                continue
            if cmd is None: continue  # OSC, charset and keypad declarations
            if params.startswith(("?", ">")) and cmd in ("h", "l", "c", "q", "u"): continue
            if intermediate and cmd == "q": continue  # cursor shape
            try: nums = [int(x or 0) for x in params.split(";")]
            except ValueError:
                self.valid = False; continue
            n = nums[0] or 1
            if cmd == "A": self.row = max(0, self.row - n)
            elif cmd == "B": self.row = min(self.rows - 1, self.row + n)
            elif cmd == "C": self.col = min(self.cols - 1, self.col + n)
            elif cmd == "D": self.col = max(0, self.col - n)
            elif cmd in ("H", "f"):
                self.row = min(self.rows - 1, n - 1)
                self.col = min(self.cols - 1, (nums[1] or 1) - 1) if len(nums) > 1 else 0
            elif cmd == "G": self.col = min(self.cols - 1, n - 1)
            elif cmd == "d": self.row = min(self.rows - 1, n - 1)
            elif cmd == "J" and nums[0] == 2:
                self.cells = [[(" ", False)] * self.cols for _ in range(self.rows)]
                self.painted = True
            elif cmd == "K":
                lo, hi = (0, self.cols) if nums[0] == 2 else (0, self.col + 1) if nums[0] == 1 else (self.col, self.cols)
                self.cells[self.row][lo:hi] = [(" ", False)] * (hi - lo)
            elif cmd == "m":
                codes = iter(nums)
                for num in codes:
                    if num in (38, 48, 58):
                        mode = next(codes, None)
                        for _ in range(3 if mode == 2 else 1 if mode == 5 else 0):
                            next(codes, None)
                        if mode not in (2, 5): self.valid = False
                        continue  # RGB/indexed color operands are not SGR faint
                    if num in (0, 22): self.faint = False
                    elif num == 2: self.faint = True
            elif cmd in ("c", "q"): pass
            else: self.valid = False

    def lines(self):
        return self.cells if self.valid and self.painted else None


@contextlib.contextmanager
def attachment(target):
    """An official, exact-job attacher in its own PTY; close only that client."""
    from session_tidy_runner import clean_env
    if not live(target) or os.readlink(f"/proc/{target['pid']}/exe") != target["binary"]:
        raise ValueError("native target changed")
    env = {k: v for k, v in clean_env().items() if not k.startswith("CLAUDE_CODE_") and k != "CLAUDE_JOB_DIR"}
    # This private terminal needs native faint suggestions to remain distinct
    # from typed drafts even when the worker's piped environment disables color.
    env.pop("NO_COLOR", None)
    env["FORCE_COLOR"] = "1"
    env.update(CLAUDE_CONFIG_DIR=target["home"], TERM="xterm-256color")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    proc = None
    def terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    try:
        proc = subprocess.Popen([target["binary"], "attach", target["job"]],
                                stdin=slave, stdout=slave, stderr=slave,
                                env=env, cwd=target["home"], preexec_fn=terminal)
        os.close(slave); slave = -1
        yield master, proc
    finally:
        os.close(master)
        if slave >= 0: os.close(slave)
        if proc is not None:
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try: proc.wait(timeout=3)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=3)


class View:
    def __init__(self, fd):
        self.fd, self.raw = fd, b""

    def read(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(self.raw) < 1024 * 1024:
            ready, _, _ = select.select([self.fd], [], [], min(0.25, max(0, deadline - time.monotonic())))
            if ready:
                try: part = os.read(self.fd, 65536)
                except OSError: return None
                if not part: return None
                self.raw += part
            elif (self.raw.rfind(b"\x1b[?2026l") > self.raw.rfind(b"\x1b[?2026h")):
                snap = Snapshot(); snap.feed(self.raw.decode("utf-8", "replace"))
                lines = snap.lines()
                # The first repaint is an "Attaching…" splash, not an empty box.
                if lines and any("".join(c for c, _ in row).lstrip().startswith("❯") for row in lines):
                    return lines
        return None


def read_snapshot(fd, timeout=10):
    return View(fd).read(timeout)


def command(req, request_path, nonce, *, continuing, screen_ready):
    """Use the checked attachment for one command to the exact booked Claude seat.

    Clear and exact-native continuation enter here only through ``peer-steward`` after
    booking and screen checks. Other continuation targets use the sealed pending-ref carrier.
    """
    import session_tidy as st
    import session_tidy_clear as clear
    target = (req.get("seat") or {}).get("native")
    sid = req.get("new_session") if continuing else req.get("sid")
    word = "continued" if continuing else "cleared"
    def result(outcome, reason="", new_session=""):
        return {word: outcome, "reason": reason, "new_session": new_session}
    if req.get("harness") != "claude" or not target:
        return result("skipped", "unsupported-harness")
    why = idle_reason(target, sid)
    if why and not (continuing and why == "not-idle-native"):
        return result("skipped", why)
    validator = clear.validate_continue if continuing else clear.validate_request
    try:
        with attachment(target) as (fd, proc):
            view = View(fd)
            # A new native conversation can publish its idle state/card shortly
            # after /clear. These bounded looks never send or reclaim a prompt.
            deadline = time.monotonic() + 10
            while True:
                current, why = validator(request_path, nonce)
                if current is None: return result("skipped", why)
                if current.get("snapshot_state") in ("pending", "deferred"):
                    return result("queued", "snapshot-pending")
                why = idle_reason(target, sid)
                if not why and continuing:
                    seat = st.seat_from_dict(req["seat"])
                    consumed = st.read_json(st._consumed_path(seat)) or {}
                    if (consumed.get("generation") != req.get("card_generation")
                            or not any(str(r).startswith(f"claude:{sid}:") for r in consumed.get("receipts") or [])):
                        why = "card-not-delivered"
                if not continuing or why not in ("not-idle-native", "card-not-delivered") or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            if why: return result("skipped", why)
            lines = view.read()
            why = "screen-unknown" if lines is None else screen_ready("claude", lines)
            if why: return result("skipped", why)
            current, why = validator(request_path, nonce)
            if current is None: return result("skipped", why)
            why = idle_reason(target, sid)
            if why or proc.poll() is not None: return result("skipped", why or "target-changed")
            if continuing:
                claim, why = clear.claim_continue(request_path, nonce)
                if claim is None: return result("skipped", why)
            else:
                claim, why = clear.claim_clear(request_path, nonce)
                if claim is None: return result("skipped", why)
            while True:
                # Keep the exact unsent clear claim during a transient source
                # read failure. Re-read the terminal after that wait as well.
                current, why = clear.reobserve_request(
                    request_path, nonce, sleep=time.sleep, continuing=continuing,
                    _sending_claim=claim if continuing else claim.get("clear_claim"))
                if current is None:
                    if continuing: clear.release_unsent_continue(claim)
                    return result("skipped", why)
                lines = view.read(timeout=1)
                why = "screen-unknown" if lines is None else screen_ready("claude", lines)
                why = why or idle_reason(target, sid)
                if why or proc.poll() is not None:
                    if continuing: clear.release_unsent_continue(claim)
                    return result("skipped", why or "target-changed")
                # An idle screen must not hide a newer prompt, card or booking.
                if continuing:
                    current, why = clear.validate_continue(request_path, nonce, _sending_claim=claim)
                else:
                    current, why = clear.validate_request(request_path, nonce,
                                                         _sending_claim=claim.get("clear_claim"))
                if current is not None:
                    break
                if why == "idle-card-observation-unavailable":
                    continue
                if continuing: clear.release_unsent_continue(claim)
                return result("skipped", why)
            text = clear.followup_text(req) if continuing else "/clear"
            # A single write includes Enter; partial/failed writes are never retried.
            data = (text + "\r").encode()
            if os.write(fd, data) != len(data): return result("unverified", "partial-send")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                record = live(target)
                if not record: return result("unverified", "target-changed")
                if not continuing and record["sessionId"] != sid:
                    return result("true", new_session=record["sessionId"])
                if continuing and record["sessionId"] != sid:
                    return result("unverified", "target-changed")
                if continuing and st.read_prompt_seq(st.seat_from_dict(req["seat"])) > req["prompt_seq"]:
                    return result("true")
                # Drain the PTY so the attacher never stalls on a full output buffer.
                if select.select([fd], [], [], 0.1)[0]:
                    with contextlib.suppress(OSError): os.read(fd, 65536)
            return result("unverified", "arrival-not-observed")
    except (OSError, subprocess.SubprocessError, ValueError):
        return result("failed", "native-attach-unavailable")


def idle_card_request(req, request_path, nonce, *, screen_ready):
    """One exact Claude attach send for an accepted idle booking; never uses an inherited pane."""
    import session_tidy_clear as clear

    def result(outcome, reason=""):
        return {"accepted": outcome, "reason": reason}

    target = (req.get("seat") or {}).get("native")
    if req.get("harness") != "claude" or not target:
        return result("skipped", "unsupported-harness")
    req, why = clear.validate_idle_request(request_path, nonce)
    if req is None:
        return result("skipped", why)
    try:
        with attachment(target) as (fd, proc):
            view = View(fd)
            reason = idle_reason(target, req.get("sid"))
            if reason:
                return result("queued" if reason == "not-idle-native" else "skipped", reason)
            lines = view.read()
            reason = "screen-unknown" if lines is None else screen_ready("claude", lines)
            if reason:
                queued = reason in {"draft", "draft-unknown", "form-open", "screen-unknown"}
                return result("queued" if queued else "skipped", reason)
            req, why = clear.validate_idle_request(request_path, nonce)
            if req is None:
                return result("skipped", why)
            claim, why = clear.claim_idle_card_request(request_path, nonce)
            if claim is None:
                return result("skipped", why)
            lines = view.read(timeout=1)
            reason = "screen-unknown" if lines is None else screen_ready("claude", lines)
            reason = reason or idle_reason(target, claim.get("sid"))
            current, why = clear.validate_idle_request(
                request_path, nonce, _sending_claim=claim.get("idle_request"))
            if reason or current is None or proc.poll() is not None:
                clear.release_unsent_idle_request(claim)
                queued = reason in {"draft", "draft-unknown", "form-open", "screen-unknown"} \
                    or str(reason or why).startswith("not-idle-")
                return result("queued" if queued else "skipped", reason or why or "target-changed")
            data = (clear.idle_card_request_text(claim) + "\r").encode("utf-8")
            try:
                written = os.write(fd, data)
            except OSError as exc:
                clear.finish_idle_card_request(claim, accepted=False, reason=type(exc).__name__)
                return result("unverified", "partial-send")
            if written != len(data):
                clear.finish_idle_card_request(claim, accepted=False, reason="partial-send")
                return result("unverified", "partial-send")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                held = clear.read_reservation(claim["seat"]["key"]) or {}
                arrived = held.get("idle_request") if isinstance(held.get("idle_request"), dict) else {}
                if held.get("nonce") != nonce or held.get("status") != "reserved":
                    return result("skipped", "superseded")
                if arrived.get("state") == "arrived":
                    return result("true") if clear.finish_idle_card_request(
                        claim, accepted=True) else result("unverified", "arrival-not-observed")
                if select.select([fd], [], [], 0.1)[0]:
                    with contextlib.suppress(OSError):
                        os.read(fd, 65536)
            clear.finish_idle_card_request(claim, accepted=False, reason="arrival-not-observed")
            return result("unverified", "arrival-not-observed")
    except (OSError, subprocess.SubprocessError, ValueError):
        return result("failed", "native-attach-unavailable")
