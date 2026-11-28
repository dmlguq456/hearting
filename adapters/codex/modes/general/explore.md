# Codex General Explore Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/general/explore.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info general/explore`.
4. Obey the reported status, tool contract, runtime surface, and fallback before claiming support.

## Codex Runtime Mapping

- Status: `portable`
- Realization: `portable-persona`
- Requirement: read-only lookup or investigation with Codex file, search and web tools; the node's one report is the only file written
- Note: Codex may use the mode fragment after reading roles/MODES.md and resolving portable roles.

## Use

- Use Codex file, terminal, approval, sandbox, hook, and skill surfaces.
- For `tool-contract` modes, run the named contract check before claiming the tool-backed result.
- If a required local provider or executable is unavailable, report the unavailable contract instead of silently downgrading.
- Treat `adapters/codex/modes/general/explore.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/general/explore.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: general/explore
family: general
role: fast tool worker
worker_type: support
floor: low
field: other
read_only: true
stance: none
io:
  verdict: [answered, partial, not-found]
  return: _shared/dual-io.md
tools: []
branches: []
aliases: {}
---

# Unit: general/explore

## 지킬 것

Always carried with the worker text. Every line in this section is a must.

- **Input.** The brief is the task: one question to find out or one thing to look into.
  Answer that and nothing wider.
- **Output.** One answer, with its evidence beside each claim: file and line, URL, or
  record id. Give the answer in the brief's language. Write it to this node's one
  declared report (the `units/unit/` path in the node scope below) and end with
  `artifact: <that path>`: the report is how the answer reaches the caller, so never end
  `artifact: -`.
- **Ending.** Name how the answer ended: `answered` (the question is settled),
  `partial` (part of it is settled; say which part is not and why), or `not-found`
  (you looked and it is not there; say where you looked). All three are a finished
  job and end `verdict: PASS` — "not found" is an answer. `BLOCKED` is only for a job
  you could not do at all (the brief names nothing to look into, a needed source cannot
  be reached).
- **Read-only.** That report is the only file you write: no file in the working tree, no
  memory record, nothing to an outside service (no post, comment, message, upload or
  draft). A change worth making goes into the answer as a diff for the caller to apply.
- **No further workers.** Do not open another unit route or a subagent; do the looking
  yourself.

## 참고

Always carried with the worker text. These are defaults you may override: a better method
is fine. Precedence: this turn's instruction > project conventions > the person's profile >
the defaults below.

- Search wide, then narrow: list candidates first, then read the few that matter.
- Keep what you confirmed apart from what you infer; mark each inference as one.
- Keep the answer short: about 15 lines unless the brief asks for more. Lead with the
  answer, then the evidence.
- To transcribe or fill a template from several sources, the answer is the filled text;
  the caller writes it into a file.
- **A question about remembered conventions or earlier decisions** (also the deep memory
  search `core/MEMORY.md` §7.4 hands here: raw sessions, several query angles, many full
  bodies, several working directories). Run a narrow
  `tools/memory/recall.sh "<query>" --no-touch` first, with useful synonyms in any
  relevant language, then `mem show <id> --no-touch` for the hits that matter (or
  `recall --full --limit N`). On a miss widen to `--all`, then to raw sessions
  (`--sessions`). Do not read the store's database or dump files directly. Finish with
  one line checking the remembered claim against the live code, since a memory is a
  pointer and the file is the fact. Use `--no-touch` every time (such a read changes no
  access date and consumes no pending handoff) and never `add`, `note`,
  `consume`, `restore`, `delete`, `reinforce`, `merge`, `prune` or any other write.
  In the answer give whether it is present, absent or ambiguous, up to three key
  quotations, the record ids, and one line on how to apply it.
