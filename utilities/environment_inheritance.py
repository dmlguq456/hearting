"""What a process Hearting starts on the caller's behalf takes over from the caller.

One rule for every such start: the registered workers (`dispatch_headless_main`
re-exports it for the three wrappers and the Codex same-attempt respawn), the
dispatch summary owner whose title refresh runs a model, and the Claude capacity
probe. It stays this small, with nothing but `os`, so a light path such as the
usage check pays nothing for loading it.

`HEARTING_SHELL_*` (the session-shell window host's socket and token, its display
state and descriptors) belongs to the one window a person works in; whatever is
started from that window sends it no events and paints no status line as if it
were that window. `AGENT_DISPATCH_BROKER_*` are the retired broker bindings.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

NOT_INHERITED_PREFIXES = ("AGENT_DISPATCH_BROKER_", "HEARTING_SHELL_")


def inherited_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A new dict of `environ` (default `os.environ`) without `NOT_INHERITED_PREFIXES`."""
    environ = os.environ if environ is None else environ
    return {key: value for key, value in environ.items() if not key.startswith(NOT_INHERITED_PREFIXES)}
