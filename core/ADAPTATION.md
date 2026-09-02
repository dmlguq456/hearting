# Adaptation Contract

This document defines how the neutral harness becomes a runtime-specific setting.
It is the boundary contract for `claude_setting/`, `codex_setting/`,
`opencode_setting/`, and future runtime projections.

## 1. Source Categories

Every file in this repo must fall into one category.

| Category | Meaning | Examples | Runtime projection rule |
|---|---|---|---|
| Portable source | Runtime-neutral semantics. Describes what must happen, not how a vendor runtime invokes it. | `core/`, portable parts of `tools/`, portable guard algorithms | May be symlinked into adapters if the runtime can read plain files |
| Adapter source | Runtime-specific representation of portable semantics. | `adapters/claude/CLAUDE.md`, `adapters/claude/settings.json`, `adapters/claude/commands/` | Projected into that runtime home |
| Adapter projection | Versioned mirror that exposes adapter source under runtime-expected names. | `claude_setting/`, `codex_setting/`, `opencode_setting/` | Symlink or generated output only; no independent semantics |
| Compatibility reference | Historical source kept for parity/drift checks after an adapter-owned realization exists. | `skills/` byte-equivalent to `adapters/claude/skills/` | Not projected as portable source; guarded against drift |
| Compatibility passthrough | Legacy file still consumed directly by a runtime before a true portable/adapted split exists. | Mixed shared hooks or utilities not yet split into invariant + adapter wrapper | Allowed only with an explicit debt note in the adapter |
| Runtime state | Tool-owned mutable local state. | `<runtime-home>/projects`, credentials, session logs, caches, DB files | Never committed to this repo |
| Improvement evidence state | Incidents, candidate fixtures, proposal evidence, approval references, and version-bound realization records. It is not active harness source. | `${XDG_STATE_HOME:-~/.local/state}/hearting/improvement` | Never projected or runtime-discovered; adopted source changes use a separate spec/code/release cycle |
| Continuity state | Cross-project agent worklog/notes data that survives sessions but is not harness source. | `<agent-notes-root>/cards`, `_layer2`, `_triage`, `digests`, `oncall`, `study` | Never committed to this repo; may be versioned in a separate notes/data repo |
| Local board app state | Worklog-board local app workspace, generated output, DB/cache, dispatch logs, and worktrees. | `<worklog-board-app>/.cache`, `.next`, `.dispatch`, `.env*`, `node_modules`, `<worklog-board-app>-wt/` | Never committed to this repo |

## 2. Adapter Rule

An adapter must not claim support for a surface unless it provides one of:

1. A native adapter file.
2. A generated file with a documented source.
3. An explicit compatibility reference or passthrough entry and the reason it is safe.

Plain symlinks are acceptable only as a projection mechanism. They are not proof
that adaptation is complete.

### 2.0. Sibling-Adapter Completion

**A portable change touches all adapters at once. Never deliver, commit, or
report a portable change for one adapter and leave the siblings for "later" — a
single-adapter split is the failure this section exists to prevent, not a normal
increment.** `core/`, `capabilities/`, and `roles/` are the semantic source.
Claude, Codex, OpenCode, and future adapters are equal sibling realizations below
that source; no adapter is the reference implementation or parent of another adapter.

A shared change follows one transaction:

1. update or confirm the portable invariant;
2. generate or edit every applicable sibling realization in the SAME unit of work
   (`generate.py` projects all three — never hand-mirror one and skip the rest);
3. verify each runtime's active discovery surface and required fallback
   (`check-adaptation-boundary.sh` audits all three adapters, not just Claude);
4. report the overall result as `PARTIAL` while any applicable row is deferred,
   unsupported without fallback, or unverified; report `GREEN` only after all
   applicable rows pass.

A sibling that reaches the portable source directly — e.g. Codex/OpenCode invoke
`$AGENT_HOME/utilities/<tool>` through their preflight wrapper instead of holding
an adapter-owned mirror — is a *covered* row, but only when that reachability is
measured, never assumed. State per-adapter coverage from evidence: never tell the
user one adapter is done and the others are "separate work" without having
checked all three first.

Generated output is not exempt from semantic, discovery, or footprint checks.
Runtime syntax may differ, but observable behavior, quality floors, and failure
reporting must remain equivalent.

## 2.1 Runtime Distribution Seam

Installing or exposing the harness in a runtime is its own adaptation seam. A
runtime surface is supported only when the adapter can name the runtime-native
entrypoint and prove that the runtime will discover it.

Use this order when adding a runtime surface:

1. Define the portable invariant in `core/`, `capabilities/`, or `roles/`.
2. Describe the runtime surface as data: kind, destination, invocation syntax,
   conversion rule, hook/config surface, and unsupported fallback.
3. Generate or maintain adapter-owned concrete output from the portable source.
4. Verify runtime discoverability or explicitly mark the surface unsupported.

An adapter must fail closed for unknown or undocumented runtime features. Do not
assume a Claude Code surface exists elsewhere because the purpose is similar.
For example, a runtime with native status, command, skill, hook, or plugin
support should use that native surface first; harness-specific gaps should be
bridged by adapter wrappers.

External reference: GSD Core
(`https://github.com/open-gsd/gsd-core`) uses the same seam shape: canonical
workflow files are transformed into runtime-specific artifacts, while Claude
plugin manifests and Codex skills are concrete runtime projections rather than
portable source. This repo should follow the pattern, not the exact file layout.

## 2.2 Runtime Currentness and Parity Claims

Before answering, planning, or editing adapter projection behavior for modern
runtime surfaces, verify the current runtime documentation and recent practice
instead of inferring from another adapter or from local harness state.

- **Claude Code / Codex surface questions require fresh external research**:
  read the current official documentation first, then inspect local adapter
  realization. Use community posts, issues, or examples only as secondary
  evidence for real-world gaps or practices, and label them as such.
- **Separate existence from parity**: if a runtime supports a feature in some
  form, still state whether it is equivalent to the other adapter's feature.
  Include concrete parity gaps such as model pinning, tool restriction,
  permission inheritance, session/worktree isolation, hook lifecycle, discovery,
  UI visibility, and noninteractive/headless behavior.
- **Plan with verification**: when a projection change depends on a runtime
  capability, the implementation plan must include a current-doc citation or
  note, a local runtime/projection check, and a fallback if the feature is
  unavailable, buggy, or unsupported in this adapter.

## 2.3 Proposal-Gated Runtime Improvement

An improvement proposal adopts a portable invariant, not a permanent runtime
implementation. The evidence loop may observe, reproduce, draft, and compare a
candidate, but it must not edit active source, generated projections, installed
plugins, or runtime-owned config. Adoption is a separate spec/code/release
cycle.

Each runtime realization is version-bound. A runtime, plugin, documentation, or
active-provider fingerprint change requires revalidation; it does not inherit a
past approval. If a native feature satisfies the fixture, retire the custom
realization while preserving the portable invariant and any required fallback.
Semantic conflicts are reviewed, never auto-merged. The operational state
contract is `loops/improvement.md`.

## 3. Portable Role Model

Portable docs use role names, not vendor model names:

| Portable role | Meaning |
|---|---|
| `fast reviewer` | Broad, low-latency review: coverage, style, cross-reference, formatting, simple consistency |
| `fast fact-checker` | Narrow source comparison: citations, years, metrics, verbatim matching |
| `fast writer` | Assembly from verified artifacts |
| `fast implementer` | Routine implementation and refactoring |
| `deep reviewer` | Architecture, methodology, safety, domain correctness, high-risk review |
| `deep maker` | High-judgment creation: planning, synthesis, visual/editorial craft |
| `deep orchestrator` | High-judgment conductor: stage gates, failover, and evidence synthesis for `standard+` dispatch-depth-1 work |
| `external adversary` | Independent reviewer with different model/runtime/process assumptions |
| `orchestrator` | Balanced mechanical coordination of already-decided tooling, paths, and report assembly; not a deep-conductor alias |

Adapters map two independent portable axes: `model_role` describes behavior, while `model_profile` (`deep|balanced-deep|light|mini`) describes the execution budget. Each adapter declares concrete models, effort/variant projections, profile granularity, and interactive-main-only families in `adapters/<adapter>/config/models.conf`; every resolver, wrapper, generated agent, lifecycle worker, and documentation table derives from that single source. A route-bound job carries both sealed axes and rejects trailing model/effort replacement. `mini` is unavailable to substantive registered dispatch-depth-1/2 owners, stages, and reviewers. A profile may share another profile's concrete model as long as the resulting execution points stay distinct; the ladder is a set of operating points, not a set of models. An adapter lacking a verified effort/variant distinction may collapse `balanced-deep` into `deep` and `mini` into `light` only with explicit reduced-granularity metadata naming which profiles collapsed. Non-route surfaces may retain checked explicit selection or inheritance when the resulting model is execution-surface eligible; main-only or unprovable inheritance is a typed deny.

Adapter and projection edits are derived core-first: change the portable invariant in
`core/` first, read that governing core document in the current session, then update
the adapter realization and generated projection. A runtime marker proves the read
gate only; it does not replace this source-order review.

## 4. Capability Model

A portable capability describes:

- trigger semantics;
- required inputs and artifact roots;
- output contract;
- Verification rigor (intensity-derived) semantics;
- delegation roles using the portable role model;
- deterministic guards and side effects;
- recovery and audit requirements.

A runtime skill/slash command/native instruction describes:

- how that runtime invokes the capability;
- which tools are available;
- how subagents or reviewers are spawned;
- how confirmation, pause, and user input work;
- how hook events are attached;
- runtime-specific file formats and frontmatter.

Current `skills/*/SKILL.md` files are compatibility references. Claude Code
consumes adapter-owned concrete files under
`adapters/claude/skills/*/SKILL.md`. Portable capability meaning belongs in
`capabilities/`.

## 5. Hook Model

Portable hook semantics are named by invariant:

| Invariant | Portable meaning |
|---|---|
| artifact order | New artifacts must be created in the allowed dependency order |
| git state safety | Do not edit during merge/rebase/cherry-pick/detached unsafe states |
| spec read gate | Spec-backed work must read the current blueprint before changing code/spec |
| core first gate | Adapter edits must be grounded in an actual current-session read of the relevant core contract |
| memory write guard | Runtime-native memory files must not bypass the unified memory store |
| memory recall/inject/distill | Inject relevant memory and optionally distill session deltas |
| worklog state signal | Surface the configured notes root and board app status without moving or mutating data |
| peer-session steering ledger | Write one append-only, body-free `peer_message_v1` record per outbound and inbound cross-session message (`OPERATIONS §5.14`) |

Adapters decide whether each invariant is enforced by native hook, wrapper,
manual preflight, or unsupported fallback. Realized (steward role, `OPERATIONS §5.14`,
v51 herdr-unified): Claude — `PostToolUse(SendMessage)` + `UserPromptSubmit` (measured,
`hooks/peer-message-record.py`) for the ledger, and `utilities/peer-steward.py wait` →
`herdr agent wait` (measured) for no-poll watching. Codex — `herdr agent wait` watching is
measured; steward-side `--timeout` foreground wait + next-turn reload is unmeasured
(P-7). The managed gateway's `steer`/`watch-idle` ops are **not implemented**
(closed-by-decision, P-1 through P-5 retired, not pending). OpenCode — `unknown`, pending
probe P-6.

## 6. Projection Invariant

Runtime homes keep their expected names. Common docs describe this generically;
adapter docs own the concrete runtime-home paths and bootstrap filenames:

```text
<runtime-home>/<adapter-bootstrap>
<runtime-home>/<runtime-settings>
<runtime-home>/<runtime-command-or-skill-surface>/
```

Those paths may symlink into versioned projection directories such as
`claude_setting/`, `codex_setting/`, or `opencode_setting/`. The projection
directory must make it clear whether each entry is native adapter output,
portable passthrough, or compatibility debt.

**Projection completeness**: a cross-adapter guard that checks whether every
portable source item has a corresponding adapter-side projection must
**enumerate the source domain** (iterate the actual current entries) rather
than assert a hardcoded list fixed at authoring time — a hardcoded list stops
catching new entries the moment the source domain grows, silently reopening
the exact gap the guard exists to close. This applies at minimum to agents,
hook events, tools, utilities, and scaffolds. Any intentional exclusion from
projection belongs in an explicit exemption or name-mapping list next to the
guard, never as a silent omission, so every excluded entry is a declared
decision rather than an accidental leak.

### 6.1. Active Context Budget

Progressive disclosure applies to runtime bootstraps and discovery metadata,
not only Skill bodies. The bootstrap is a router: source order, hard invariants,
and runtime entrypoints stay resident; detailed lifecycle explanations,
examples, and edge cases live in adapter README/ADAPTATION documents or command
help loaded on demand.

- Each always-loaded adapter bootstrap is at most `16,384` UTF-8 bytes.
- Each active Skill metadata discovery surface is at most `7,000` characters,
  including its concrete local Skill paths. Regression baselines normalize
  those paths relative to the surface root so checkout location is not source
  growth.
- Activating two surfaces with the same Skill names is a duplicate-discovery
  failure, not extra assurance.
- The same always-loaded bootstrap must reach a session exactly once. When a
  runtime both auto-loads a bootstrap filename and reads a configured
  instruction list, an adapter picks one carrier and keeps the other empty;
  runtimes commonly dedupe instruction sources by resolved path, so one file
  behind two absolute paths — a symlink and its target, or two projections of
  it — is injected twice rather than deduped. Verification asserts the number
  of carriers, not merely that some carrier exists.
- A stored surface baseline rejects growth greater than five percent unless the
  same change records a reviewed rationale and updates the budget.
- Ordinary, unknown, and repeated hook states inject zero bytes. A verified
  pressure-band transition may emit one compact directive of at most 240 UTF-8
  bytes.

These are footprint controls, not token or billing estimators. Static bytes,
code lines, directive counts, and monotonic runtime counters must not be
converted into savings claims. A production savings claim requires at least 30
paired real sessions and separates input, cache creation, output, and billable
cost. Synthetic fixtures prove regression behavior only.
