"""Incremental explorer-only key and SGR mouse decoder."""
from __future__ import annotations

import re

_SGR = re.compile(rb"^\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_KEYS = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
         b"\x1b[Z": "shift-tab", b"\t": "tab", b"\r": "enter", b"\n": "enter",
         b"\x1b": "escape"}


class ExplorerInput:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data=b"", final=False):
        self.buffer.extend(data or b"")
        events = []
        while self.buffer:
            raw = bytes(self.buffer)
            match = _SGR.match(raw)
            if match:
                code, x, y = map(int, match.groups()[:3])
                release = match.group(4) == b"m"
                del self.buffer[:match.end()]
                if code & 64:
                    events.append({"type": "wheel", "direction": -1 if code & 1 else 1, "x": x - 1, "y": y - 1})
                else:
                    events.append({"type": "mouse", "button": code & 3, "phase": "release" if release else "press",
                                   "x": x - 1, "y": y - 1, "motion": bool(code & 32)})
                continue
            exact = next((key for key in _KEYS if raw.startswith(key)), None)
            if exact is not None and (exact != b"\x1b" or final):
                del self.buffer[:len(exact)]
                events.append({"type": "key", "key": _KEYS[exact]})
                continue
            prefix = any(key.startswith(raw) for key in (*_KEYS,)) or raw.startswith(b"\x1b[<")
            if prefix and not final:
                break
            if raw.startswith(b"\x1b"):
                del self.buffer[:1]
                events.append({"type": "key", "key": "escape"})
                continue
            first = self.buffer[0]
            if first < 0x80:
                del self.buffer[:1]
                events.append({"type": "key", "key": chr(first)})
                continue
            length = (2 if 0xC2 <= first <= 0xDF else
                      3 if 0xE0 <= first <= 0xEF else
                      4 if 0xF0 <= first <= 0xF4 else 1)
            if len(self.buffer) < length and not final:
                break
            raw_char = bytes(self.buffer[:length])
            del self.buffer[:length]
            try:
                char = raw_char.decode("utf-8")
            except UnicodeDecodeError:
                continue
            events.append({"type": "key", "key": char})
        if final and self.buffer:
            self.buffer.clear()
        return tuple(events)
