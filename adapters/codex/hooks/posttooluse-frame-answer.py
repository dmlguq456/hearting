#!/usr/bin/env python3
"""Retired Codex PostToolUse(request_user_input) bridge: intentionally does nothing.

A registered frame question is answered on the Hearting question board, so the native reply is
no longer kept. The entry stays in hooks.json because Codex trusts each entry by its current hash and the
entries keep their order (hooks/spec_sync_nudge_wiring.test.py); removing it would move the later entries."""

if __name__ == "__main__":
    raise SystemExit(0)
