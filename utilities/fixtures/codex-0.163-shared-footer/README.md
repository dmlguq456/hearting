# Codex 0.163.0 shared-daemon footer after `/clear`

Native-region text of two real screens: Codex 0.163.0 with a shared daemon inside
the session shell under a private Herdr 0.9.5, after a real `/clear`. The status line
is Hearting's `["thread-id", "thread-title", ...]`. An unnamed thread draws its
id as the title, and no help row is shown.

- `side-133x30-native91.txt`: 133×30 side layout. The native part is the first 91
  cells of each row, split by cell width.
- `bottom-80x24.txt`: 80×24 bottom layout. These are the rows above the Hearting panel.

Source: review RV-PR1470-1111 evidence `head-shared-{133,80}-herdr-tidy-codex.screen.txt`
(sha256 `bbad7ccd…0e86`, `c68d1f59…2338`). `session_shell_selection.test.py` feeds
both screens to the Python footer reader and to the host selection frame.
