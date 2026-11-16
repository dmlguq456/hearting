# Contributing to Hearting

## Start with the shared source

Start shared behavior changes in `core/`, `capabilities/`, and `roles/`. These
own the common meaning. Claude Code, Codex, and OpenCode are equal adapters;
none is the reference implementation for the others. For an overview, read
[MANUAL.md](MANUAL.md).

Keep [README.ko.md](README.ko.md), [QUICKSTART.ko.md](QUICKSTART.ko.md), and
[FAQ.ko.md](FAQ.ko.md) in step with their English files in the same pull
request. The English file is canonical; the Korean file is its translation, and
the Korean-language screens and regression inputs stay supported.

Text that Hearting prints for people goes in the shared catalog
`tools/i18n/messages.json`, with an English and a Korean entry, read through
`tools/i18n/ui_text.py`. Do not add a translation table of your own.

## Generated files

Do not edit generated Skill, agent, mode, command, or plugin projections as the
source of a shared change. Change their canonical inputs, then run
`tools/generate.py` to refresh the projections. Adapter-owned hooks and
wrappers are edited where they live; not every file under `adapters/` is
generated. The `claude_setting/`, `codex_setting/`, and `opencode_setting/`
directories are symlink projections that the installer reads; do not rename or
edit through them.

## Maintenance principles

[AGENTS.md](AGENTS.md) holds Hearting's purpose and the principles for changing
Hearting itself. In short: finish the user's task without needless intervention, protect user
changes and running work, keep shared behavior in one place, and remove a
repeated blocker instead of adding another step.

## Set up and validate

```sh
git clone https://github.com/dmlguq456/hearting.git ~/hearting
cd ~/hearting
git config core.hooksPath tools/git-hooks
```

Choose the existing checks that cover your change. After changing a shared
definition or a generated projection, regenerate and check:

```sh
python3 tools/generate.py
python3 tools/generate.py --check
./tools/check-adaptation-boundary.sh
./tools/skill-conformance/check.sh
```

Run only the tests that touch your change. Test files end in `.test.py` or
`.test.sh`, or start with `test_`; the runner isolates the home directory and
configuration for each suite:

```sh
python3 tools/run-tests.py --select tools/install/installer_runtime.test.py
```

For installation changes, `./tools/install/runtime-activation.test.sh` is the
activation suite. Packaged activation refuses a dirty working tree, so run the
installation lifecycle suites from a clean checkout. A small prose change does
not call for every suite; say what you ran and what you did not.

If you change what a Fleet or panel screen shows, redraw the README pictures from their
demo rows and commit them with the change:

```sh
python3 tools/fleet/tests/readme_captures.py
python3 tools/render-fleet-svg.py
```

`tools/fleet/tests/test_readme_captures.py` checks their size and that
`docs/fleet.svg` is current, and keeps private paths and addresses off them.

## Open a pull request

- Write commit messages and the pull request in English. Explain the problem and
  the resulting behavior first, then verification and remaining limits.
- Keep the diff to one purpose; separate unrelated changes into their own pull
  requests.
- Use neutral host names and paths in fixtures (`gpu-a`, `192.0.2.x`,
  `/home/user`). Do not include secrets, credentials, or real conversations.
- Releases follow [RELEASE_POLICY.md](RELEASE_POLICY.md); a contribution adds no
  approval or signing step of its own.
