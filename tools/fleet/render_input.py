"""Fleet render: prompt buttons and the curses input reader.

Split out of `render.py`, which re-exports every name defined here.
"""
import time

from .render_cells import (
    _cw, _dw, curses,
)


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


def _getmouse_xy():
    """Extract getmouse() coords, or (None, None) on failure. A bare `except: my = None`
    (the pre-v9 shape) still calls the mouse handler with a stale/unbound `mx` from a PRIOR
    getmouse() (or unbound entirely on the first call) → NameError crashes the TUI. Returning
    a matched pair makes "couldn't read the event" and "read it" mutually exclusive states."""
    mx, my, _bstate = _getmouse_event()
    return mx, my


def _getmouse_event():
    """Return the current mouse coordinates and bstate, or a matched empty event."""
    try:
        _, mx, my, _mz, bstate = curses.getmouse()
    except Exception:
        return None, None, 0
    return mx, my, bstate


def _base_navigation(ch, mouse_event):
    """Only viewport movement is safe to handle without an intervening paint."""
    if ch in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE,
              curses.KEY_HOME, curses.KEY_END, ord("k"), ord("j"), ord("g"), ord("G")):
        return True
    if ch == curses.KEY_MOUSE and mouse_event is not None:
        state = mouse_event[2]
        return state in (getattr(curses, "BUTTON4_PRESSED", 0),
                         getattr(curses, "BUTTON5_PRESSED", 0)) and state != 0
    return False


class _CursesInput:
    """Read keypad-off curses bytes and decode complete keys and SGR mouse packets.

    ncurses' keypad mouse decoder can wait indefinitely after an SGR prefix. Decoding
    bounded available bytes here lets an unfinished packet yield without delaying refresh.
    """

    _SGR_PREFIX = b"\x1b[<"
    _FOCUS = {b"\x1b[I", b"\x1b[O"}
    _ESC_DELAY = 0.025
    _SGR_TIMEOUT = 0.20
    _MAX_SEQUENCE = 64

    def __init__(self, curses_mod, mouse_enabled=True, explorer_mode=False):
        self.curses = curses_mod
        self.mouse_enabled = mouse_enabled
        self.explorer_mode = bool(explorer_mode)
        self.pending = bytearray()
        self.pending_since = None
        self.discarding_sgr = False
        self.ready = []
        self.key_sequences = {}
        for capability, key_name in (
                ("kcuu1", "KEY_UP"), ("kcud1", "KEY_DOWN"),
                ("kcub1", "KEY_LEFT"), ("kcuf1", "KEY_RIGHT"),
                ("kpp", "KEY_PPAGE"), ("knp", "KEY_NPAGE"),
                ("khome", "KEY_HOME"), ("kend", "KEY_END")):
            key = getattr(curses_mod, key_name, None)
            try:
                sequence = curses_mod.tigetstr(capability)
            except Exception:
                sequence = None
            if key is not None and sequence:
                self.key_sequences[bytes(sequence)] = key
        for sequence, key_name in (
                (b"\x1b[A", "KEY_UP"), (b"\x1bOA", "KEY_UP"),
                (b"\x1b[B", "KEY_DOWN"), (b"\x1bOB", "KEY_DOWN"),
                (b"\x1b[C", "KEY_RIGHT"), (b"\x1bOC", "KEY_RIGHT"),
                (b"\x1b[D", "KEY_LEFT"), (b"\x1bOD", "KEY_LEFT"),
                (b"\x1b[5~", "KEY_PPAGE"), (b"\x1b[6~", "KEY_NPAGE"),
                (b"\x1b[H", "KEY_HOME"), (b"\x1b[1~", "KEY_HOME"),
                (b"\x1b[7~", "KEY_HOME"), (b"\x1bOH", "KEY_HOME"),
                (b"\x1b[F", "KEY_END"), (b"\x1b[4~", "KEY_END"),
                (b"\x1b[8~", "KEY_END"), (b"\x1bOF", "KEY_END")):
            key = getattr(curses_mod, key_name, None)
            if key is not None:
                self.key_sequences[sequence] = key

    def _clear(self):
        self.pending.clear()
        self.pending_since = None
        self.discarding_sgr = False

    def has_buffered_input(self):
        """Whether a key, packet, discard tail, or ready byte remains undecided."""
        return bool(self.pending or self.discarding_sgr or self.ready)

    def _ready_event(self):
        return self.ready.pop(0) if self.ready else (self.curses.ERR, None)

    def _fallback_bytes(self, raw):
        self._clear()
        self.ready.extend((value, None) for value in raw[1:])
        return (raw[0], None) if raw else (self.curses.ERR, None)

    def _mouse_state(self, button, released):
        # xterm SGR supports button 0..2, modifiers 4/8/16, motion 32,
        # vertical wheel 64/65, and release through the `m` final. Fleet
        # intentionally acts only on buttons 0..2 and vertical wheel presses.
        motion = bool(button & 32)
        if button < 0 or button & ~0x7f:
            return 0
        if motion and (button & 64 or not self.explorer_mode):
            return 0
        if button & 64:
            if released or (button & 3) not in (0, 1):
                return 0
            name = "BUTTON5_PRESSED" if button & 1 else "BUTTON4_PRESSED"
            state = getattr(self.curses, name, 0)
        else:
            number = button & 3
            if number == 3:
                return 0
            if motion:
                if number != 0 or released:
                    return 0
                state = getattr(self.curses, "BUTTON1_PRESSED", 0)
                state |= _EXPLORER_MOUSE_MOTION
                return state
            if released:
                state = getattr(self.curses, "BUTTON%d_RELEASED" % (number + 1), 0)
            else:
                state = (getattr(self.curses, "BUTTON%d_PRESSED" % (number + 1), 0)
                         or getattr(self.curses, "BUTTON%d_CLICKED" % (number + 1), 0))
        if not state:
            return 0
        for bit, name in ((4, "BUTTON_SHIFT"), (8, "BUTTON_ALT"), (16, "BUTTON_CTRL")):
            if button & bit:
                modifier = getattr(self.curses, name, 0)
                if not modifier:
                    return 0
                state |= modifier
        return state

    def _discard_sgr(self, now):
        self.discarding_sgr = True
        self.pending_since = now

    def _decode_sgr(self, raw):
        body = raw[len(self._SGR_PREFIX):]
        if not body:
            return None
        if body[-1] not in (ord("M"), ord("m")):
            if (len(raw) > self._MAX_SEQUENCE or body.count(b";") > 2
                    or any(byte not in b"0123456789;" for byte in body)):
                self._discard_sgr(time.monotonic())
                return self.curses.ERR, None
            return None
        fields = body[:-1].split(b";")
        try:
            if len(fields) != 3:
                raise ValueError("expected button;x;y")
            button, x, y = (int(value) for value in fields)
            if button < 0 or button > 0x7f:
                raise ValueError("button out of range")
            if x < 1 or y < 1 or x > 65535 or y > 65535:
                raise ValueError("coordinate out of range")
        except ValueError:
            self._clear()
            return self.curses.ERR, None
        state = self._mouse_state(button, body[-1] == ord("m"))
        self._clear()
        if not state or not self.mouse_enabled:
            return self.curses.ERR, None
        return getattr(self.curses, "KEY_MOUSE"), (x - 1, y - 1, state)

    def _decode(self, now):
        raw = bytes(self.pending)
        if not raw:
            return self._ready_event()
        if raw in self._FOCUS:
            self._clear()
            return self.curses.ERR, None
        if raw.startswith(self._SGR_PREFIX):
            return self._decode_sgr(raw)
        key = self.key_sequences.get(raw)
        if key is not None:
            self._clear()
            return key, None
        if (self._SGR_PREFIX.startswith(raw)
                or any(sequence.startswith(raw) for sequence in self.key_sequences)
                or any(sequence.startswith(raw) for sequence in self._FOCUS)):
            return None
        if raw.startswith(b"\x1b["):
            if raw[-1] < 0x40 or raw[-1] > 0x7e:
                return None
            self._clear()  # unknown complete CSI sequences, including focus reports, are inert
            return self.curses.ERR, None
        if raw.startswith(b"\x1bO") and len(raw) < 3:
            return None
        if raw.startswith(b"\x1bO"):
            self._clear()
            return self.curses.ERR, None
        if len(raw) == 1 and now - self.pending_since < self._ESC_DELAY:
            return None
        return self._fallback_bytes(raw)

    def _expire(self, now):
        if not self.pending:
            return self._ready_event()
        raw = bytes(self.pending)
        if self.discarding_sgr or raw.startswith(self._SGR_PREFIX):
            if now - self.pending_since < self._SGR_TIMEOUT:
                return self.curses.ERR, None
            self._clear()
            return self.curses.ERR, None
        if raw.startswith((b"\x1b[", b"\x1bO")):
            if now - self.pending_since < self._ESC_DELAY:
                return self.curses.ERR, None
            self._clear()
            return self.curses.ERR, None
        if now - self.pending_since < self._ESC_DELAY:
            return self.curses.ERR, None
        if raw.startswith(b"\x1b"):
            self._clear()
            self.ready.extend((value, None) for value in raw[1:])
            return 27, None
        return self._fallback_bytes(raw)

    def read(self, stdscr, *, timeout_ms=100):
        result = self._read_one(stdscr)
        if result[0] != self.curses.ERR or not (self.pending or self.discarding_sgr):
            return result
        # A complete arrow already in curses' buffer should not pay for a full
        # render after each byte. Never wait for a missing tail or consume a
        # second complete action before its predecessor has been dispatched.
        try:
            stdscr.timeout(0)
            for _ in range(self._MAX_SEQUENCE):
                code = stdscr.getch()
                if code == self.curses.ERR:
                    break
                result = self._read_one(stdscr, code)
                if result[0] != self.curses.ERR or not (self.pending or self.discarding_sgr):
                    break
            return result
        finally:
            stdscr.timeout(timeout_ms)

    def _read_one(self, stdscr, code=None):
        if self.ready:
            return self._ready_event()
        if code is None:
            code = stdscr.getch()
        now = time.monotonic()
        if code == self.curses.ERR:
            return self._expire(now)
        if code < 0 or code > 255:
            if self.pending:
                if (self.discarding_sgr
                        or bytes(self.pending).startswith(self._SGR_PREFIX)):
                    # A native resize is not a packet boundary. Deliver it now,
                    # but consume the SGR tail against the original deadline.
                    if now - self.pending_since < self._SGR_TIMEOUT:
                        self.discarding_sgr = True
                    else:
                        self._clear()
                else:
                    self._clear()
            return code, None
        if self.discarding_sgr:
            if now - self.pending_since >= self._SGR_TIMEOUT:
                self._clear()
            else:
                if code == 27:
                    self._clear()
                    self.pending_since = now
                    self.pending.append(code)
                elif code in (ord("M"), ord("m")):
                    self._clear()
                return self.curses.ERR, None
        if self.pending and bytes(self.pending).startswith(self._SGR_PREFIX):
            if now - self.pending_since >= self._SGR_TIMEOUT:
                self._clear()
                if code != 27:
                    return code, None
        if not self.pending and code != 27:
            return code, None
        if code == 27 and self.pending:
            raw = bytes(self.pending)
            if (raw.startswith(self._SGR_PREFIX)
                    or raw.startswith((b"\x1b[", b"\x1bO"))):
                self._clear()
                self.pending_since = now
                self.pending.append(code)
                return self.curses.ERR, None
            return self._fallback_bytes(bytes(self.pending) + bytes((code,)))
        if not self.pending:
            self.pending_since = now
        self.pending.append(code)
        if len(self.pending) > self._MAX_SEQUENCE:
            if bytes(self.pending).startswith(self._SGR_PREFIX):
                self._discard_sgr(now)
                return self.curses.ERR, None
            self._clear()
            return self.curses.ERR, None
        decoded = self._decode(now)
        return self._ready_event() if decoded is None else decoded


def _close_job_row_if_registry(entry):
    """prd.md:255 — after a successful kill of a registry JOB row, close it. Sessions never
    touch the registry."""
    from . import control
    if entry.get("kind") != "job" or entry.get("source") != "jobs":
        return
    if entry.get("status") not in {"open", "running"} or not entry.get("attempt_id"):
        return
    try:
        from .collectors import dispatch as _dispatch
        for jobs_path in _dispatch._candidate_jobs_paths(None):
            if control.close_registry_row(jobs_path, entry["attempt_id"]):
                control.log_action(action="close_row", pid=entry.get("pid"), sid=None,
                                   state=entry.get("state"), approval="single",
                                   result="ok", reason=entry["slug"])
                return
    except Exception:
        pass


_MOUSE_HINT_MIN_WIDTH = 100   # R2-3: mouse is opt-in (needs `set -g mouse on` in tmux); only
                              # advertise it where there is slack to spare — keyboard stays
                              # primary and unconditional (prd.md:88·280).


_PROCESS_HINT_MIN_WIDTH = 80  # F-30 (v10) — the base footer is already tight at 60 cols (§5.1
_EXPLORER_MOUSE_MOTION = 1 << 30  # private marker carried beside curses' button state
