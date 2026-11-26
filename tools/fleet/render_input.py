"""Fleet render: prompt buttons and the curses input reader.

Split out of `render.py`, which re-exports every name defined here.
"""
import re
import time

from .render_cells import (
    _cw, _dw, curses,
)

# 11-26: keys the explorer's reader decodes beyond curses' own codes, above every Unicode code point so no
# character or curses key is one. A written answer takes them (the shell's question popup reads the same keys).
_SYNTHETIC = 0x110000
KEY_NEWLINE = _SYNTHETIC + 1      # Alt/Option+Enter, a modified Enter the terminal reports (kitty, modifyOtherKeys)
KEY_PASTE = _SYNTHETIC + 2        # one whole bracketed paste; its text rides in the event's second slot
KEY_WORD_LEFT = _SYNTHETIC + 3    # Alt/Ctrl+←, Alt+B
KEY_WORD_RIGHT = _SYNTHETIC + 4   # Alt/Ctrl+→, Alt+F
KEY_WORD_BACK = _SYNTHETIC + 5    # Alt+Backspace (Ctrl+W is its own byte)
KEY_WORD_DELETE = _SYNTHETIC + 6  # Alt+D, Ctrl/Alt+Delete
KEY_TEXT_START = _SYNTHETIC + 7   # Ctrl+Home
KEY_TEXT_END = _SYNTHETIC + 8     # Ctrl+End
KEY_ALT_OTHER = _SYNTHETIC + 9    # any other Alt combination while an answer is written: dropped
KEY_PASTE_PART = _SYNTHETIC + 10  # what came of a paste that stalled (it goes on); its text in the second slot
_NEWLINE_CSI = re.compile(rb"^\x1b\[(?:13;(?:[2-9]|1\d)u|27;(?:[2-9]|1\d);13~|106;5u)$")
_TEXT_KEYS = {b"\x1b[1;3D": KEY_WORD_LEFT, b"\x1b[1;5D": KEY_WORD_LEFT,
              b"\x1b[1;3C": KEY_WORD_RIGHT, b"\x1b[1;5C": KEY_WORD_RIGHT,
              b"\x1b[3;3~": KEY_WORD_DELETE, b"\x1b[3;5~": KEY_WORD_DELETE,
              b"\x1b[1;5H": KEY_TEXT_START, b"\x1b[1;5F": KEY_TEXT_END}
_ALT_TEXT_KEYS = {ord("b"): KEY_WORD_LEFT, ord("f"): KEY_WORD_RIGHT, ord("d"): KEY_WORD_DELETE,
                  0x7f: KEY_WORD_BACK, 0x08: KEY_WORD_BACK}
_PASTE_START, _PASTE_END = b"\x1b[200~", b"\x1b[201~"
_PASTE_LIMIT = 65536
# A paste whose bytes stop this long has stalled; review 1861: it is not over then (``_CursesInput._expire``).
_PASTE_IDLE = 1.0
# 11-26: an escape sequence inside a paste (a coloured log): `ESC [`, then anything up to its final byte, as
# the shell's question popup reads one (it gives up after 62 bytes without a final one, those bytes dropped).
_PASTED_CSI = re.compile(rb"\x1b\[(?:[^\x40-\x7e]{0,62}[\x40-\x7e]|[^\x40-\x7e]{63})")


# The keys a written answer edits with besides its characters (the shell's question popup reads the same):
# Ctrl+A/E, Ctrl+U/K and Ctrl+W are their own bytes.
_EDIT_KEYS = {1: "ctrl-a", 5: "ctrl-e", 21: "ctrl-u", 11: "ctrl-k", 23: "word-back",
              KEY_WORD_LEFT: "word-left", KEY_WORD_RIGHT: "word-right", KEY_WORD_BACK: "word-back",
              KEY_WORD_DELETE: "word-delete", KEY_TEXT_START: "text-start", KEY_TEXT_END: "text-end",
              KEY_ALT_OTHER: ""}


def answer_key(ch, payload=None, *, writable=False, editing=False, finding=False, cr_enter=False):
    """The explorer key one input stands for where answers are written (11-26): a key name, ``""`` for
    no key at all, or ``None`` when the input is none of these and goes on to the explorer's other keys.

    ``writable``: the question shown takes a written answer; ``editing``: one is being written;
    ``finding``: the model filter takes text; ``cr_enter``: Enter arrives as CR, so LF is Ctrl+J."""
    if ch in (KEY_PASTE, KEY_PASTE_PART):
        # A paste is text only where text goes: a written answer takes it whole (its line breaks are line
        # breaks, never Enter), the model filter as one line; anywhere else it is no key at all. What came of a
        # stalled paste is ``paste-part:`` (more of the same paste may come).
        text = payload if isinstance(payload, str) else ""
        lead = "paste-part:" if ch == KEY_PASTE_PART else "text:"
        return (lead + text if text and writable else
                "text:" + " ".join(text.split()) if text.strip() and finding else "")
    if ch == KEY_NEWLINE or (ch == 10 and cr_enter):
        # Alt+Enter, a reported Shift/Ctrl+Enter and Ctrl+J put a line break in a written answer; elsewhere
        # Ctrl+J is Enter as before and the others nothing.
        return "newline" if writable else "enter" if ch == 10 else ""
    if ch in _EDIT_KEYS or ch == getattr(curses, "KEY_DC", -1):
        # 11-22: a typed answer has a place in it; these keys only mean something while it is typed.
        return _EDIT_KEYS.get(ch, "delete") if editing else ""
    return None


def paste_text(raw):
    """A pasted text as an answer takes it: line breaks (CR, LF, CR LF) one LF, a tab kept, an escape
    sequence (``ESC [`` … its final byte) left out whole and any other control character left out (the
    delivery to a session refuses them). The shell's question popup reads the same bytes to the same text."""
    raw = _PASTED_CSI.sub(b"", bytes(raw))
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    return "".join(char for char in text
                   if char in "\n\t" or not (ord(char) < 32 or 0x7f <= ord(char) < 0xa0))


def _paste_cut(raw):
    """How much of a stalled paste goes in now: all but what may still join the bytes after it (an Esc or an escape
    sequence not ended, a character's first bytes, the line breaks it ends with), so the parts read as the whole."""
    cut = len(raw)
    esc = raw.rfind(b"\x1b", 0, cut)
    if esc >= 0:
        whole = _PASTED_CSI.match(raw, esc)
        if not (whole and whole.end() <= cut) and not (esc + 1 < cut and raw[esc + 1] != 0x5b):
            cut = esc
    lead = next((i for i in range(cut - 1, max(-1, cut - 4), -1) if raw[i] >= 0xc0), None)
    if lead is not None and cut - lead < (2 if raw[lead] < 0xe0 else 3 if raw[lead] < 0xf0 else 4):
        cut = lead
    while cut and raw[cut - 1] in (10, 13):
        cut -= 1
    return cut


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
        # 11-26: a bracketed paste being read (its bytes), when its last byte came, and the UTF-8 tail of an
        # Alt+character still to drop. ``alt_keys``: the caller says an answer is being written, so an Alt
        # combination is the answer's (a word key, or dropped) rather than Esc and then a key.
        self.paste = None
        self.paste_since = None
        # Review 1861: a paste whose bytes stopped for ``_PASTE_IDLE`` has stalled (what came went in); a byte that
        # may be a key pressed then (Esc, Enter; Ctrl+] at any time) waits to see whether more comes right after it;
        # after a pressed key the rest of that paste is dropped (``paste_discard``) up to its end mark or a second.
        self.paste_stalled = False
        self.paste_key = None
        self.paste_key_since = None
        self.paste_discard = False
        self.timeout_ms = 100
        self.skip_utf8 = 0
        self.alt_keys = False
        self.key_sequences = {}
        for capability, key_name in (
                ("kcuu1", "KEY_UP"), ("kcud1", "KEY_DOWN"),
                ("kcub1", "KEY_LEFT"), ("kcuf1", "KEY_RIGHT"),
                ("kpp", "KEY_PPAGE"), ("knp", "KEY_NPAGE"),
                ("khome", "KEY_HOME"), ("kend", "KEY_END"),
                *((("kdch1", "KEY_DC"),) if explorer_mode else ())):
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
                (b"\x1b[8~", "KEY_END"), (b"\x1bOF", "KEY_END"),
                # Delete: the explorer's written answer deletes at its place (11-22); it had no decoding here.
                *(((b"\x1b[3~", "KEY_DC"),) if explorer_mode else ())):
            key = getattr(curses_mod, key_name, None)
            if key is not None:
                self.key_sequences[sequence] = key

    def _clear(self):
        self.pending.clear()
        self.pending_since = None
        self.discarding_sgr = False

    def has_buffered_input(self):
        """Whether a key, packet, discard tail, paste, or ready byte remains undecided."""
        return bool(self.pending or self.discarding_sgr or self.ready or self.paste is not None)

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
        if self.explorer_mode:
            text_key = self._text_key(raw, now)
            if text_key is not None:
                return text_key
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

    def _text_key(self, raw, now):
        """The explorer's own keys for a written answer (11-26), or ``None``."""
        if raw == _PASTE_START:
            self._clear()
            self.paste, self.paste_since = bytearray(), now
            return self.curses.ERR, None
        if raw in (b"\x1b\r", b"\x1b\n") or _NEWLINE_CSI.match(raw):
            self._clear()
            return KEY_NEWLINE, None
        if raw in _TEXT_KEYS:
            self._clear()
            return _TEXT_KEYS[raw], None
        if (self.alt_keys and len(raw) == 2 and raw[0] == 27
                and raw[1] not in (ord("["), ord("O"), 27)):
            self._clear()
            if raw[1] >= 0xc0:
                self.skip_utf8 = 1 if raw[1] < 0xe0 else 2 if raw[1] < 0xf0 else 3
            return _ALT_TEXT_KEYS.get(raw[1], KEY_ALT_OTHER), None
        return None

    def _paste_byte(self, code, now):
        """One byte of a bracketed paste: the whole text comes as one ``KEY_PASTE`` at its end marker (after what a
        stall let in, ``KEY_PASTE_PART``)."""
        self.paste_since = now
        self.paste.append(code)
        if self.paste_discard:
            # The rest of a paste a pressed key ended: dropped, up to and with its end mark.
            if self.paste.endswith(_PASTE_END):
                self.paste, self.paste_discard = None, False
            elif len(self.paste) > 16:
                del self.paste[:-16]
            return self.curses.ERR, None
        self.paste_stalled = False
        if len(self.paste) > _PASTE_LIMIT + 16:
            del self.paste[_PASTE_LIMIT:-16]
        if self.paste.endswith(_PASTE_END):
            text = paste_text(self.paste[:-len(_PASTE_END)][:_PASTE_LIMIT])
            self.paste = None
            return KEY_PASTE, text
        return self.curses.ERR, None

    def _paste_key_pressed(self, now):
        """A key pressed in a paste (Enter or Esc after it stalled, Ctrl+] at any time): the paste ends as what came,
        then that key; the rest of the paste, if more comes, is dropped up to its end mark or a second with no byte."""
        code, self.paste_key = self.paste_key, None
        text = paste_text(bytes(self.paste)[:_PASTE_LIMIT])
        self.paste, self.paste_since, self.paste_stalled, self.paste_discard = bytearray(), now, False, True
        self.ready.append((code, None))
        return KEY_PASTE, text

    def _expire(self, now):
        if self.paste is not None:
            if self.paste_discard:
                if now - self.paste_since >= _PASTE_IDLE:
                    self.paste, self.paste_discard = None, False
                return self.curses.ERR, None
            if self.paste_key is not None:
                # Nothing came after it (an Esc waits its usual 25 ms): a key pressed.
                if self.paste_key != 27 or now - self.paste_key_since >= self._ESC_DELAY:
                    return self._paste_key_pressed(now)
                return self.curses.ERR, None
            if self.paste_stalled or now - self.paste_since < _PASTE_IDLE:
                return self.curses.ERR, None
            # 11-26 (review 1861): a paste whose bytes stop for a second (its end marker not come) has stalled, not
            # ended: what came goes in now, and what comes after is still its text (a line break is never Enter).
            self.paste_stalled = True
            cut = _paste_cut(self.paste)
            text = paste_text(bytes(self.paste[:cut])[:_PASTE_LIMIT])
            del self.paste[:cut]
            return (KEY_PASTE_PART, text) if text else (self.curses.ERR, None)
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

    def _undecided(self):
        return bool(self.pending or self.discarding_sgr or self.paste is not None)

    def read(self, stdscr, *, timeout_ms=100):
        self.timeout_ms = timeout_ms
        result = self._read_one(stdscr)
        if result[0] != self.curses.ERR or not self._undecided():
            return result
        # A complete arrow already in curses' buffer should not pay for a full
        # render after each byte. Never wait for a missing tail or consume a
        # second complete action before its predecessor has been dispatched.
        # A paste's bytes are read on to its end marker (or what is there now).
        try:
            stdscr.timeout(0)
            count = 0
            while count < (_PASTE_LIMIT + 64 if self.paste is not None else self._MAX_SEQUENCE):
                count += 1
                code = stdscr.getch()
                if code == self.curses.ERR:
                    break
                result = self._read_one(stdscr, code)
                if result[0] != self.curses.ERR or not self._undecided():
                    break
            if result[0] == self.curses.ERR and self.paste_key not in (None, 27):
                # Nothing came right after it: a key pressed, not the paste's byte (an Esc waits its 25 ms).
                return self._paste_key_pressed(time.monotonic())
            return result
        finally:
            stdscr.timeout(timeout_ms)

    def _read_one(self, stdscr, code=None):
        if self.ready:
            return self._ready_event()
        first = code is None
        if code is None:
            code = stdscr.getch()
        now = time.monotonic()
        if self.paste is not None:
            if code == self.curses.ERR:
                return self._expire(now)
            if code < 0 or code > 255:
                return code, None
            if self.paste_key is not None:
                if self.paste_key == 27 and now - self.paste_key_since >= self._ESC_DELAY:
                    result = self._paste_key_pressed(now)
                    self._paste_byte(code, now)
                    return result
                # What came right after it: it was the paste's own byte.
                held, self.paste_key = self.paste_key, None
                self._paste_byte(held, now)
            elif (first and not self.paste_discard
                  and (code == 29 or self.paste_stalled and code in (13, 27))):
                self.paste_key, self.paste_key_since = code, now
                return self.curses.ERR, None
            return self._paste_byte(code, now)
        if self.skip_utf8 and 0x80 <= code < 0xc0:
            self.skip_utf8 -= 1
            return self.curses.ERR, None
        self.skip_utf8 = 0
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
