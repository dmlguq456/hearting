# Mattermost Integration (`mm`)

Personal-access-token CLI for Mattermost REST API v4, shared by every runtime
(Claude, Codex, OpenCode) through an ordinary `PATH` launcher. Python 3 stdlib
only — no dependencies.

## Launcher

Installed as `~/.local/bin/mm -> <release>/tools/integrations/mattermost/mm.py`
via the standard tool-launcher registration in `tools/install/distribution.py`
(`TOOL_LAUNCHERS`) and `tools/install/bootstrap.py` (`LAUNCHERS`).

## Credentials (runtime-owned, never in this repo)

```text
~/.config/mattermost/env   # chmod 600
  MM_URL=<server base URL>
  MM_TOKEN=<personal access token>
```

Environment variables `MM_URL` / `MM_TOKEN` override the file. Token issuance:
Mattermost profile > Security > Personal Access Tokens (the feature and the
account permission must be enabled by a system admin first).
Reference: <https://developers.mattermost.com/integrate/reference/personal-access-token/>

## Default write block

Write commands (`post`, `reply`, `dm`, and non-GET `mm api` methods) are
**blocked by default** so an agent cannot post without an explicit user
request. Override per invocation with `MM_ALLOW_WRITE=1`, or permanently with
`MM_ALLOW_WRITE=1` in the credentials file. Read-only search POST endpoints
(`/search`, `/users/ids`, `/users/search`) are exempt.

## Commands

```text
mm me | teams | channels [team]
mm read <channel> [N]
mm search <terms> | users <term>
mm post <channel> <msg> | reply <post_id> <msg> | dm <user> <msg>   # gated
mm api <METHOD> </path> [json]                                       # non-GET gated
```

Channels accept the URL slug or the display name; disambiguate with
`team:channel` when the name exists in multiple teams.
