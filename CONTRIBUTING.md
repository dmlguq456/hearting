# Contributing to Hearting

## Start with the shared source

Start shared behavior changes in `core/`, `capabilities/`, and `roles/`. These
own the common meaning. Claude Code, Codex, and OpenCode are equal adapters;
none is the reference implementation for the others. For an overview, read
[MANUAL.md](MANUAL.md).

Keep [README.ko.md](README.ko.md) in step with [README.md](README.md) in the
same pull request. The English file is canonical; the Korean file is its
translation, and the Korean-language screens and regression inputs stay
supported.

## Generated files

Do not edit generated Skill, agent, mode, command, or plugin projections as the
source of a shared change. Change their canonical inputs, then run
`tools/generate.py` to refresh the projections. Adapter-owned hooks and
wrappers are edited where they live; not every file under `adapters/` is
generated. The `claude_setting/`, `codex_setting/`, and `opencode_setting/`
directories are symlink projections that the installer reads; do not rename or
edit through them.

## Maintenance principles

[AGENTS.md](AGENTS.md) holds the principles for changing Hearting itself. In
short: finish the user's task without needless intervention, protect user
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
python3 tools/run-tests.py --select tools/install/install_notice.test.py
```

For installation changes, `./tools/install/runtime-activation.test.sh` is the
activation suite. Packaged activation refuses a dirty working tree, so run the
installation lifecycle suites from a clean checkout. A small prose change does
not call for every suite; say what you ran and what you did not.

## Open a pull request

- Write commit messages and the pull request in English. Explain the problem and
  the resulting behavior first, then verification and remaining limits.
- Keep the diff to one purpose; separate unrelated changes into their own pull
  requests.
- Use neutral host names and paths in fixtures (`gpu-a`, `192.0.2.x`,
  `/home/user`). Do not include secrets, credentials, or real conversations.
- Releases follow [RELEASE_POLICY.md](RELEASE_POLICY.md); a contribution adds no
  approval or signing step of its own.
