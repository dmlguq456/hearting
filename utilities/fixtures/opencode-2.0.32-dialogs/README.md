# OpenCode 2.0.32 model dialog over a session

Text of two real screens: OpenCode 2.0.32 `--standalone` (a private server, not the
background service) with a private HOME and XDG directories in a private tmux, on a session
that already has messages, after `ctrl+x m` opened the model dialog. The dialog stands over
the transcript and the composer box (`┃`) stays visible below it, empty.

- `models-120x50.txt`: a tall 120×50 window.
- `models-120x24.txt`: a low 120×24 window.

The dialog's own text (`Select model … esc`, `Search`) holds none of the words the
form readers look for (`Esc to cancel`, `Enter to select`), and the composer below reads
empty: the screen alone reads ready while the dialog has the focus. The command palette
(`ctrl+p`), the session list (`ctrl+x l`) and the rename prompt (`ctrl+r`) draw the same
title row with `esc` at its right; OpenCode's terminal pane (`ctrl+x t`) takes the focus
with no dialog text at all. `session_shell_local_command.test.py` paints these screens as
the synthetic OpenCode's `/models` menu.

Source: captures of 2026-11-16; the private directory path is replaced and trailing
blanks are trimmed. The model names, the addresses and the transcript lines (two earlier
lines the private session was sent and the error text they drew) are replaced with
placeholder text of the same width (`Model A Free`, `first prompt`, `https://example.org/go`):
the names and addresses are placeholders, not the real list. The dialog frame, the line
count and the column places are as captured; no account or credential was present.
