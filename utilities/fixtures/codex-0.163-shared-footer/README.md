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

## A UUID-shaped branch before the thread-id

- `branch-first-133x30.txt`, `branch-first-80x24.txt`: Codex 0.163.0 real PTY,
  `--no-daemon`, private HOME, user-configured
  `status_line=["git-branch","context-used","thread-id"]` and a synthetic
  UUID-shaped branch name. The first UUID is the branch, the last one (whole at
  133 columns, clipped at 80) is the real thread. Both readers must answer
  `none`: two different ids are a conflict, never the first one.

Source: review RV-PR1499-1111 evidence `idle133.txt`, `idle80.txt`
(sha256 `e6fac3ce0a9b…`, `4166830308e4…`; trailing blanks trimmed).
