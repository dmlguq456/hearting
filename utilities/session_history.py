#!/usr/bin/env python3
"""Open a predecessor transcript read-only beside the current terminal pane."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import conversation_reader
import session_tidy
from terminal_surface import HerdrSurface, Near, open_pane


def _named_seat(args):
    """The seat a caller already named with ``--seat-key``; never re-resolved here."""
    key = getattr(args, "seat_key", "") or ""
    if not key:
        return None
    harness = getattr(args, "harness", None) or ""
    sid = getattr(args, "session_id", None) or ""
    seat = session_tidy.Seat("pane", key, getattr(args, "pane", "") or "", harness or "codex", "",
                             endpoint=getattr(args, "endpoint", "") or "",
                             socket_path=getattr(args, "socket_path", "") or "")
    return session_tidy.session_seat(seat, harness, sid) if harness and sid else seat


def _herdr_pane_for_session(harness, sid):
    """``(pane, agent)`` of the one herdr pane whose live agent session is exactly ``sid``."""
    executable = session_tidy._herdr_executable()
    if not executable or not sid:
        return "", ""
    command = [executable]
    server = (os.environ.get("AGENT_HERDR_SESSION") or "").strip()
    if server:
        command += ["--session", server]
    command += ["agent", "list"]
    try:
        done = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=session_tidy.HERDR_LOOKUP_TIMEOUT_SEC, check=False)
        agents = (json.loads(done.stdout or "").get("result") or {}).get("agents")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return "", ""
    if not isinstance(agents, list):
        return "", ""
    found = {(str(agent["pane_id"]), str(agent.get("agent") or "")) for agent in agents
             if isinstance(agent, dict) and agent.get("pane_id")
             and session_tidy._agent_session(agent) == sid
             and (not harness or agent.get("agent") == harness)}
    return next(iter(found)) if len(found) == 1 else ("", "")


def _leads_with(seat, harness, sid):
    """The seat's ledger has this exact session as its current generation."""
    rows = session_tidy.seat_lineage(seat)
    return bool(rows) and (rows[0].get("harness"), rows[0].get("sid")) == (harness, sid)


def _named_target(harness, sid):
    """Resolve another session named by ``--session-id``; never the caller's own seat.

    Its exact herdr pane comes first, then its exact native-session ledger. The
    caller's pane, ``HERDR_ACTIVE_PANE_ID`` and cwd are not evidence of it.
    """
    pane, agent = _herdr_pane_for_session(harness, sid)
    if pane:
        seat = session_tidy.session_seat(session_tidy._pane_seat_of(pane), agent, sid)
        if _leads_with(seat, agent, sid):
            return seat, pane, seat.socket_path
    found = []
    for name in ([harness] if harness else session_tidy.HARNESSES):
        key = session_tidy._native_session_key(name, sid)
        if not key:
            continue
        # The native-session ledger is its own physical file: one exact session.
        seat = session_tidy.session_seat(session_tidy.Seat("project", key, harness=name), name, sid)
        if _leads_with(seat, name, sid):
            found.append(seat)
    if len(found) == 1:
        return found[0], "", ""
    return None, "", ""


def _caller_placement():
    """The caller's own herdr pane and socket: a named target's viewer opens there.

    The target's pane only locates its ledger; splitting it would change another
    session's layout where the person who asked is not looking.
    """
    pane = (os.environ.get("HERDR_PANE_ID") or "").strip()
    socket = (os.environ.get("HERDR_SOCKET_PATH") or "").strip()
    return (pane, socket) if pane and socket else ("", "")


def _identity(args):
    named = _named_seat(args)
    if named is not None:
        return (named, *_caller_placement())
    harness = getattr(args, "harness", None)
    sid = getattr(args, "session_id", None) or ""
    if sid and sid != session_tidy.session_from_env(harness)[1]:
        # Another session (or a caller whose own id is unknown) is a target, not
        # this process: its inherited pane would show the caller's conversation.
        seat, target_pane, _socket = _named_target(harness, sid)
        if seat is None or not target_pane:
            return seat, "", ""      # an exact native ledger is read through local output
        return (seat, *_caller_placement())
    resolved = session_tidy.resolve_caller(
        args.harness, args.session_id, os.getcwd(), project=False)
    if resolved:
        seat, harness, sid = resolved
        if harness in session_tidy.HARNESSES and sid and not sid.startswith("unknown-"):
            seat = session_tidy.session_seat(seat, harness, sid)
            if seat.kind == "pane":
                return seat, seat.pane, seat.socket_path
            return seat, "", ""
    if args.session_id:
        return None, "", ""

    # A keybinding without any native identity keeps its read-only active-pane
    # behavior. It cannot override a native caller selected above.
    pane = (os.environ.get("HERDR_ACTIVE_PANE_ID") or os.environ.get("HERDR_PANE_ID") or "").strip()
    socket = os.environ.get("HERDR_SOCKET_PATH", "")
    if pane and socket:
        endpoint = session_tidy._endpoint_scope({"HERDR_SOCKET_PATH": socket})
        seat = session_tidy._pane_seat_of(pane, endpoint=endpoint, socket_path=socket)
        return seat, pane, socket
    return None, "", ""


_COMMAND_HINT = " · hearting run session_history --harness <harness> --session-id <SID>"


def _render(entry):
    t = session_tidy._ui_translator()
    result = conversation_reader.read_conversation(entry)
    name = f"{entry.get('harness', 'unknown')} {str(entry.get('sid', ''))[:8]}"
    label = entry.get("_label", t("conversation.earlier"))
    if result.status != "available":
        return t("conversation.unavailable", label=label, name=name, status=result.status,
                 reason=result.reason or t("conversation.cannot_read")), result
    header = t("conversation.header", label=label, name=name)
    return header + "\n\n" + (result.text or t("conversation.empty")), result


def _show(text):
    less = shutil.which("less")
    if not less:
        print(text)
        return sys.stdin.isatty()
    env = os.environ.copy()
    for key in ("LESS", "LESSOPEN", "LESSCLOSE", "LESSHISTFILE"):
        env.pop(key, None)
    env["LESSSECURE"] = "1"
    env["LESSHISTFILE"] = "-"
    proc = subprocess.run([less, "+G"], input=text, text=True, env=env, check=False)
    return proc.returncode == 0 and sys.stdin.isatty()


def _view(args):
    # The opener already resolved the seat; this pane's own env names a different one.
    seat = _named_seat(args)
    if seat is None and args.harness and args.session_id:
        seat = session_tidy.session_seat(
            session_tidy.resolve_seat(args.harness, os.getcwd(), sid=args.session_id,
                                      project=False),
            args.harness, args.session_id)
    elif seat is None:
        seat = session_tidy.Seat("pane", args.seat_key, args.pane, "codex", "",
                                 endpoint=args.endpoint, socket_path=args.socket_path)
    rows = session_tidy.seat_lineage(seat)
    return _display(rows, args.generation, args.stdout, clear_before_prompt=True)


def _entries_for_generation(rows, generation):
    t = session_tidy._ui_translator()
    if not rows:
        return [], t("conversation.not_linked")
    current = rows[0]
    current_id = current.get("harness"), current.get("sid")
    confirmed = session_tidy._confirmed_predecessor(
        rows, current.get("harness", ""), current.get("sid", ""))
    if confirmed is None:
        if len(rows) < 2 or current.get("relation") != "chronological":
            return [], t("conversation.no_confirmed") + _COMMAND_HINT
        confirmed = rows[1]
        first_label = t("conversation.label_chronological")
    else:
        first_label = t("conversation.label_confirmed")

    items = [dict(confirmed, _label=first_label)]
    seen = {current_id, (confirmed.get("harness"), confirmed.get("sid"))}
    relation_labels = {
        "confirmed": t("conversation.label_confirmed"),
        "chronological": t("conversation.label_chronological"),
        "unknown": t("conversation.label_unknown"),
    }
    for index, row in enumerate(rows[1:], 1):
        ident = row.get("harness"), row.get("sid")
        if ident in seen:
            continue
        newer = rows[index - 1]
        relation = newer.get("relation")
        predecessor = newer.get("predecessor")
        if relation == "confirmed" and isinstance(predecessor, dict):
            if (predecessor.get("harness"), predecessor.get("sid")) != ident:
                relation = "unknown"
        items.append(dict(row, _label=relation_labels.get(relation, relation_labels["unknown"])))
        seen.add(ident)
    if generation < 1 or generation > len(items):
        return [], t("conversation.out_of_range", count=len(items))
    return items, ""


def _display(rows, generation, stdout=False, clear_before_prompt=False):
    entries, reason = _entries_for_generation(rows, generation)
    if not entries:
        if stdout:
            print(reason)
        else:
            _show(reason)
        return 0
    t = session_tidy._ui_translator()
    index = generation - 1
    boundary_message = ""
    while True:
        output, _ = _render(entries[index])
        if stdout:
            print(output)
        else:
            menu = _show(output)
            if not menu:
                return 0
            if clear_before_prompt:
                print("\x1b[2J\x1b[H", end="")
            try:
                prompt = t("conversation.prompt")
                if boundary_message:
                    prompt += " · " + boundary_message
                choice = input(prompt + ": ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return 0
            boundary_message = ""
            if choice == "q":
                return 0
            if choice == "p" and index + 1 < len(entries):
                index += 1
                continue
            if choice == "n" and index > 0:
                index -= 1
                continue
            if choice == "p":
                boundary_message = t("conversation.no_earlier")
            elif choice == "n":
                boundary_message = t("conversation.no_newer")
            continue
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hearting run session_history")
    parser.add_argument("--in-pane", action="store_true")
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--harness")
    parser.add_argument("--session-id")
    parser.add_argument("--seat-key", default="")
    parser.add_argument("--pane", default="")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--socket-path", default="")
    args = parser.parse_args(argv)
    if args.in_pane:
        return _view(args)
    seat, pane, socket = _identity(args)
    if seat is None:
        print(session_tidy._ui_translator()("conversation.not_linked") + _COMMAND_HINT)
        return 0
    if pane:
        endpoint = seat.endpoint
        # The seat names its own pane and socket; `pane` is only where the viewer opens.
        argv = ["exec", sys.executable, str(Path(__file__).resolve()), "--in-pane", "--seat-key", seat.key,
                "--pane", seat.pane or pane, "--endpoint", endpoint,
                "--socket-path", seat.socket_path or socket,
                "--generation", str(args.generation)]
        named_harness, named_sid = session_tidy.session_identity(seat)
        if named_harness and named_sid:
            # The viewer pane rebuilds this exact logical seat from its key.
            argv += ["--harness", named_harness, "--session-id", named_sid]
        active_cwd = os.environ.get("HERDR_ACTIVE_PANE_CWD") or os.getcwd()
        def runner(command):
            env = os.environ.copy()
            env["HERDR_SOCKET_PATH"] = socket
            return subprocess.run(command, capture_output=True, text=True, timeout=15, env=env, check=False)
        near = Near(HerdrSurface(runner=runner), pane=pane)
        title = session_tidy._ui_translator()("conversation.earlier")
        result = open_pane(argv, title=title, near=near, cwd=active_cwd,
                           env={"HERDR_SOCKET_PATH": socket})
        if result.pane:
            if result.placement == "split":
                layout = near.surface.layout(pane)
                panes = layout.get("panes", []) if isinstance(layout, dict) else []
                rects = {row.get("pane_id"): row.get("rect") for row in panes
                         if isinstance(row, dict)}
                origin_rect, opened_rect = rects.get(pane), rects.get(result.pane)
                if (isinstance(origin_rect, dict) and isinstance(opened_rect, dict)
                        and all(type(rect.get(key)) is int for rect in (origin_rect, opened_rect)
                                for key in ("x", "y", "width", "height"))):
                    ox = origin_rect["x"] + origin_rect["width"] / 2
                    oy = origin_rect["y"] + origin_rect["height"] / 2
                    nx = opened_rect["x"] + opened_rect["width"] / 2
                    ny = opened_rect["y"] + opened_rect["height"] / 2
                    direction = ("right" if nx > ox else "left") if abs(nx - ox) >= abs(ny - oy) \
                        else ("down" if ny > oy else "up")
                    with contextlib.suppress(OSError, subprocess.SubprocessError):
                        runner(["herdr", "pane", "focus", "--pane", pane,
                                "--direction", direction])
            return 0
        print(session_tidy._ui_translator()("conversation.pane_failed",
                                             notes=", ".join(result.notes or [result.placement])))
    rows = session_tidy.seat_lineage(seat)
    return _display(rows, args.generation, args.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
