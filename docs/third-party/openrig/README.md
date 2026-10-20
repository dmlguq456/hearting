# OpenRig attribution

Parts of `utilities/terminal_surface.py` are adapted from OpenRig, licensed
under the Apache License, Version 2.0 (full text in `LICENSE` beside this file).

- Upstream: OpenRig v0.6.13, commit `9eeb2e42ca6dcfb8da60a8517dc39c7d83b84082`
- Upstream files, all under `packages/daemon/src/domain/terminal/`:

| Upstream | What Hearting took | How it was changed |
|---|---|---|
| `terminal-provider.ts` | A provider places, labels and lists panes but never composes the pane command; one honest partial result shape (`opened`/`absent`/`degraded`, plus `notes`). | Rewritten in Python as `HerdrSurface` and `TmuxSurface` with an injected synchronous runner, opening one pane instead of a multi-seat view. The result is `{surface, pane, placement, notes, view_command}`; `surface="none"` stands for absent, and a note stands for degraded. |
| `view-composer.ts` | Pane-command composition as one pure function shared by every provider, with every word shell-quoted. | Rewritten as `compose_command(argv, cwd, env)` using `shlex`; ssh, attach and paging rules were not taken. |
| `herdr-adapter.ts` (`exitedSeats`) | One pane-list read right after opening, with no wait; it can show a pane already gone and otherwise says the listing could not confirm it. | Matched by pane id instead of label (`pane-exited`, `pane-list-unconfirmed`). Layout application, focus changes, retries and workspace creation were not taken. |

The 2×2 placement rule (`next_cell`), its fallthrough order and the workspace
lock are Hearting's own, moved unchanged from `utilities/peer-steward.py`.
The source copy contains no `NOTICE` file; the upstream copyright and license
text are kept in `LICENSE`.
