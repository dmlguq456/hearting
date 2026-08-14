# Conventions — Family-Wide Operational Rules

> This is the single source of truth for operational rules and definitions across the autopilot family. `DESIGN_PRINCIPLES.md` owns architectural design such as orchestrator, Skill, and agent separation; this document owns QA definitions, portable model roles, artifact conventions, and family-wide flags.
>
> Runtime adapter bootstraps list this file as a source of truth. The main agent reads it when QA, model-role, artifact, or family-wide flag work needs the definitions.

> `tools/build-manifest.py --check` and adapter `sync-native-* --check` verify manifests and projections; `tools/check-adaptation-boundary.sh` verifies adapter boundaries; `tools/skill-conformance/check.sh` verifies quantitative Skill rules; and `harness verify` checks installed surfaces. Human review owns semantic prose consistency.

---

## §1. Pipeline Intensity, Stage Graph, and Assurance (canonical)

Pipeline intensity controls which orchestration shape an autopilot entry uses. Verification rigor—how much assurance selected checks receive—is derived from the same intensity through §1.1 rather than selected as a separate axis. There is no user-facing `--qa` selector to reconcile with the pipeline graph.

| Stage | Meaning | Typical realization |
|---|---|---|
| `intake` | Parse request, mode, constraints, risk, and intensity | Route/capability preflight, spec significance, target selection |
| `orient` | Gather only the context needed for the selected intensity | Read spec, source, or material artifacts; `orient-lite` for quick |
| `plan` | Choose the work path before production | Absent for direct, inline micro-plan for quick, durable plan for standard+ |
| `plan-check` | Check that the plan can safely feed production | Required for quick+; depth scales with intensity |
| `produce` | Create or modify the artifact | Code, draft, report, design, spec, or note |
| `verify` | Run a concrete checker | Tests, visual harness, claim verification, compile, consistency, or drift check |
| `synth` | Merge independent perspectives into one path | Only when perspective workers ran |
| `report` | Return outcome, evidence, artifact paths, and remaining risk | Summary, handoff, or user-facing report |

| Intensity | Stage graph | Plan and check policy | Dispatch | Assurance |
|---|---|---|---|---|
| `direct` | `intake → produce → sanity/report` | No plan, plan check, or durable plan; final sanity only | Inline | none/light |
| `quick` | `intake → orient-lite → micro-plan → plan-check-lite → produce → verify-lite → report` | One dispatch-depth-1 session; 3–4 focused plan questions and one concrete sanity check | One-shot conductor; no dispatch depth 2 | quick |
| `standard` | `intake → orient → declared framing group → synth/owner-plan → plan-check → optional verifier/planner → produce → verify → report` | Durable plan where the capability owns a work cycle; a declared framing anchor normally opens two asymmetric legs and merges them before planning | Thin conductor dispatches each durable stage as dispatch depth 2 with file-only handoff | standard |
| `strong` | Standard plus every declared strong-tier group and an optional fix loop | Retain framing breadth and open the declared plan-committal, implementation-review, or other risk groups; a registry `width_by_intensity` may widen a high-value group to three legs | Stage dispatch plus bounded parallel-group fan-out/fan-in; exact width is route-sealed | standard/thorough |
| `thorough` | Strong groups plus deeper synthesis/verification | A declared group may add a third implementation-risk, failure-mode, or contrarian leg; unchanged groups remain width two | The base recipe realizes only registry-declared 2–4-way siblings; composed routes may add other bounded dispatch-depth-2 perspectives | thorough |
| `adversarial` | Thorough plus adversarial failure-mode/security verification | Use the route-declared width and adversarial perspective; no undeclared fan-out | Bounded declared group or composed adversary/verifier; dispatch depth remains at most 2 | adversarial |

Stage-local gates stay cheap and ask only whether output can feed the next stage. An independent QA pass uses another harness, execution profile, perspective, or model family and runs only where selected intensity calls for it. A declared `cross-harness` group must realize at least two eligible harnesses; N greater than the harness count gains additional independence through profile and perspective diversity. Final verification remains capability-specific. Every non-direct graph includes at least a small plan check because a bad plan corrupts every downstream stage.

Dispatch depth is portable route topology, not process ancestry, runtime-native
agent nesting, or proof of registry membership. Dispatch dispatch depth 0 is user-facing
main ownership; dispatch depth 1 owns the capability pipeline; dispatch depth 2
serves bounded review, perspective, and pipeline-stage nodes. Direct stays inline
at dispatch depth 0. Quick is semantically one registered-headless one-shot
conductor at dispatch depth 1 and opens no child node. Its transport/schema
compatibility identity remains `worker_type=owner`, `unit=_kernel/owner`,
`owner_model_profile`, and `one-shot-owner`; those names do not make it a
standard+ capability owner. Standard+ fallback attempts retain their node's
dispatch depth even when the execution surface changes from registered headless
to a runtime-native subagent or inline. Dispatch dispatch depth 3 or greater is forbidden.
Resource runners and Claude agent-team teammate sessions are separate lifecycle
surfaces and carry no dispatch depth.

Conditional capability follow-ups are owner postconditions, not dispatch
nodes. `capabilities/topologies.json` declares their activation condition,
terminal anchors, and source-output references; the route compiler seals the
effective direct, quick, or standard+ anchors. A false readiness predicate
produces its declared typed skip and does not add dispatch depth or invalidate
the primary artifact. For the note follow-up, only a bounded live remote-DB
probe activates the postcondition; hooks and environment-variable presence
alone are insufficient.

### §1.1. Verification Rigor Tiers

Rigor is an assurance budget inside the graph selected by intensity. It does not create stages, choose topology, or grant dispatch depth 2. Reviewer counts are upper bounds for a selected pass rather than automatic fan-out after every stage.

| Rigor | Derived from | Plan check | Selected independent pass | Final verification | Retry budget |
|---|---|---|---|---|---|
| `quick` | `quick` | Self-check or 3–4 focused questions | None by default; the self-check itself carries the adversarial stance below | One concrete sanity check | None automatically |
| `light`/none | `direct` | Focused self-check if present, held to the adversarial stance below | At most one fast reviewer at an already selected review point | Focused command, render, or source check | One pass |
| `standard` | `standard`, `strong` | Lightweight independent review where planning exists | A framing group normally starts at width two; `strong` opens every declared strong-tier group and may widen selected high-value anchors according to the sealed registry policy | Normal capability verification; source check when relevant | At most one correction |
| `thorough` | `thorough` | Deeper or multi-axis review | Keep declared groups and realize their thorough width, commonly adding an implementation-risk, failure-mode, or contrarian third leg | Broader evidence and adequacy review | Up to two corrections |
| `adversarial` | `adversarial` | Hostile owner-plan critique | Realize the adversarial route width and any selected security, contradiction, or failure-mode perspective | Verification plus adversarial evidence | Two corrections plus one selected adversary pass |

Two properties cut across every rigor tier and do not scale away at low intensity:

1. **Adversarial stance is universal (all tiers, including `direct` and `quick`).** Any review or self-check that runs adopts a refute-by-default posture: it actively tries to falsify the artifact's correctness claims, enumerates the concrete failure modes it can substantiate, and treats inadequate evidence as *not proven* rather than a pass. This is a stance inside whatever check already runs, not an added stage, so it adds no dispatch at `direct`/`quick`. It is what makes review adversarial before any separate adversary *pass* exists.
 2. **Independent exploration is bounded, asymmetric, and cross-harness first.** Registry-v6 `parallel_groups` declare exactly which direction, plan, review, or verification anchors fan out, their intensity-specific width, and ordered execution-profile/perspective legs. Width is 2–4 and is never inferred from reviewer count. `cross-harness` means the group realizes at least two eligible harnesses; `model-profile` and `perspective` axes reduce correlated failure when N exceeds available harness families. For the code track, framing starts at width two for `standard` and adds a deep contrarian leg at `strong+`; plan and implementation review start at width two for `strong` and add a light implementation-risk or deep failure-mode leg at `thorough+`. Other capabilities keep their migrated width-two groups unless their own registry/spec explicitly widens them. All legs are blind siblings at dispatch depth 2, write disjoint artifacts, and join before continuation; evidence synthesis is not a majority vote. When only one harness is available, an explicitly requested cross-harness pass fails loudly; an auto-selected group may use typed same-harness degradation while retaining profile/perspective diversity. `direct`/`quick` stay single-session but keep the adversarial stance from (1).

 3. **Leg class is a gate-authority axis, separate from the model-profile budget axis.** Every leg declares `leg_class: peer` or `leg_class: auxiliary`. A **peer** leg carries part of the group's gate authority — at least one realized peer leg must land on a quality-peer harness (SD-100 ①) — and is where cross-harness independence is demanded. An **auxiliary** leg is advisory: it widens the group with a closed narrow check (assumption, edge-case, failure-mode, simplicity, test-gap) on the `light` budget, cannot block the stage by itself (its unit verdict enum carries no blocking token), and its findings feed the arbiter's `auxiliary_findings_considered`. `model_profile` remains the execution-budget axis and never selects gate authority; a peer leg's budget and an auxiliary leg's advisory role are orthogonal. Existing `light` scout legs on `frame`/`plan`/`impl-review` stay `peer` — only newly declared narrow-check legs are `auxiliary`.

Track rules:

- Code has no fact-checker; ground truth is code, tests, runtime behavior, API/CLI surface, and selected security review.
- Document, research, refinement, and note tracks fact-check only when claims, citations, cards, or external truth are in scope.
- Design, apply, and ship require executable render, build, compile, or deployment evidence; reviewer prose never substitutes for it.
- Spec review checks coherence and downstream API/data/UI impact; factual citations may additionally require source checking.

Intensity resolves in this order: explicit `--intensity`, capability default, then request shape in `WORKFLOW §1.1`. Rigor then maps deterministically: direct to none/light, quick to quick, standard or strong to standard, thorough to thorough, and adversarial to adversarial.

An external adversary is required only when an adversarial graph actually selects that pass. The adapter must prove that a different reviewer, engine, or harness ran. If an explicitly requested adversarial pass is unavailable, fail loudly; if routing auto-escalated, fall back to thorough and report it. Runtime wrapper names are not portable semantics.

`--no-fact-check` and `--no-style-audit` remain orthogonal and appear only on capabilities that expose those checks. `code-plan` realizes durable plan and plan-check for standard+; quick keeps an inline micro-plan. `code-refine` corrects an existing durable plan and is not automatic in quick. `code-test` scales concrete verification with rigor. `code-report` reports and synthesizes without adding QA.

### §1.2. Token and Context Pressure

The portable invariant is **token pressure ⊥ intensity**. Pressure is an observed response-shaping signal, not a pipeline selector or assurance budget. It may shorten user-facing explanation and defer unrequested optional extras, but never changes graph, depth, dispatch, model role/profile, effort, plan check, parallel-group width, reviewer budget, verification, retry contract, or definition of done.

Portable telemetry distinguishes active context, cumulative session counters, and a response-policy score. Never reuse a generic adapter field with different runtime meaning. Unknown, stale, malformed, unsupported, or decreasing counters fail open to the selected pipeline and report degraded availability. Forks and subagents have separate denominators.

Pressure cannot reduce validation, tests, error and data-loss handling, security, auth, permissions, accessibility, spec and plan gates, sandbox and approval, git/write/hook/liveness guards, required tools, or input context. Automatic pruning stays off. An adapter may inject a compact output directive only on a verified pressure-band transition; normal, unknown, unsupported, native-owned, and repeated bands inject zero bytes. Runtime-owned budget config is read-only unless the user explicitly chooses a separately verified native opt-in.

## §2. Portable Model Roles

Shared contracts use model roles rather than concrete model names. Vendor-specific models are adapter implementation values.

| Role | Meaning | Typical use |
|---|---|---|
| `fast reviewer` | Low-cost, low-latency broad review with known ground truth or surface-heavy checks | quick/light QA, style, coverage, cross-reference, verbatim matching |
| `deep reviewer` | High-reasoning domain and methodology judgment | methodology, safety/security, architecture risk, standard+ quality review |
| `fast fact-checker` | Narrow comparison of claims against source artifacts with limited creativity | citation, venue, year, metric, lineage, table values |
| `fast writer` | Low-cost assembly of verified artifacts into a user-facing summary | Final report and short synthesis |
| `deep maker` | Generation requiring aesthetic, strategic, architectural, or domain judgment | Planning, research synthesis, visual design, editorial rewrite |
| `deep orchestrator` | High-judgment conductor for stage gates, failover, and evidence synthesis | Standard+ dispatch-depth-1 capability owner |
| `external adversary` | Hostile review through an independent engine or runtime | Adversarial verification |
| `orchestrator` | Balanced mechanical coordination of already decided calls, paths, and states | Wrappers, dispatch mechanics, report assembly |

### §2.1. Dispatch Routing

The default role for a standard+ conductor is `deep orchestrator`. Do not alias the retained balanced `orchestrator` to it. A role names behavior and responsibility, not execution budget. Dispatch selection order is explicit route profile, hard eligibility, stage affinity, required group diversity, then capacity, cost, and latency. Portable core records role, model profile, and any required harness-family axis separately. Adapters own exact model IDs, effort/variant realization, runtime probes, and eligibility.

Harness capacity is evaluated only inside user-declared quality bands. A fresh
headroom signal may reorder quality peers and may promote a declared relief band
below that profile's threshold; it never makes a lower-quality harness a default
peer or lowers the sealed model profile. Missing gauges stay `unknown` and recent
exact attempts are only a deterministic tie-breaker. A main agent should explicitly
choose an allowed relief harness for low-risk, independently verifiable work when
the weaker output cannot silently become final; it records that semantic judgment
instead of encoding a permanent vendor-capacity claim in
portable core. User-local policy owns enabled harnesses, band membership, and
promotion thresholds; installation creates it once from available runtimes and
subsequent updates preserve it.

`utilities/dispatch-route.sh` is read-only and emits stable key/value trace, rejected, fallback, and unknown records. It does not register, launch, or mutate caches or worktrees. Without an adapter probe, OpenCode remains `unknown` rather than guessed.

### §2.2. Adapter Mapping

Every adapter maps portable roles and execution profiles to runtime models, tools, and prompt profiles as a quality-reproduction contract. A route-bound dispatch always carries both axes; wrappers do not infer the execution budget from role wording or silently inherit the interactive model. Update and read core before changing adapter maps or generated agents.

The portable execution profiles are:

| Profile | Intent | Portable default | Registered topology |
|---|---|---|---|
| `deep` | Highest-confidence convergence, critical planning, failure-mode/security judgment | deep tier at `xhigh` | allowed |
| `balanced-deep` | Deep-model judgment at a lower coordination budget | deep tier at `medium` | allowed |
| `light` | Low-latency/cost production, structured checking, and broad exploration | light tier at `medium` | allowed |
| `mini` | Lifecycle, classification, title, or explicitly micro-semantic help | mini tier at `low` | forbidden for substantive registered dispatch-depth-1/2 owner, stage, and review nodes |

The `quick` one-shot conductor uses `balanced-deep`; every `standard|strong|thorough|adversarial` capability owner uses `deep`. A standard+ node that is both `kind=capability-owner` and `unit=_kernel/owner` carries that same owner budget because it owns a conductor transaction, handback, or synthesis gate. Node meaning and risk—not dispatch depth or role wording alone—select the profile, so ordinary makers, implementers, reviewers, resource runners, and parallel legs retain their task-appropriate budgets. `balanced-deep` remains the ordinary deep-judgment subordinate profile rather than collapsing into `deep`. Route compilation seals `owner_model_profile` and every node/parallel leg `model_profile`; a caller cannot replace a sealed profile with a trailing concrete model or effort. Capacity failover may choose a checked eligible substitute while preserving and reporting the profile intent. Claude and Codex must distinguish all four mappings. Distinctness is a property of the operating point, not of the model: two profiles may resolve to the same concrete model when their effort differs. An adapter without a verified effort/variant axis may collapse `balanced-deep` into `deep` and `mini` into `light` only while reporting which profiles its granularity metadata collapsed; it must not claim four-step parity.

Effort labels are model-relative budgets, not portable performance scores. `deep/xhigh`, `balanced-deep/deep-medium`, `light/medium`, and `mini/light-low` are deliberate distinct operating points. `high|max` remain explicit checked overrides or fallback values, not hidden default tiers.

A tier is not obliged to reach for the smallest available model. `mini` names the cheapest *operating point*, and an adapter may realize it by lowering effort on the light-tier model instead of dropping to a smaller one — the right choice when the tier's output feeds later stages, where a weaker model costs more downstream than the effort step saves.

### §2.3. Unit Catalog and Role Binding

The former runtime team agents are re-homed (2026-07-22, user decision: 승격+재홈) into the
portable **unit catalog** at `roles/units/<family>/<unit>.md`. A unit is the single
declaration of one dispatchable behavior atom; its frontmatter binds the portable role
name, worker type, floor, and I/O semantics (`roles/units/_schema.md` is the authoring
contract). `family` is a grouping label only — no runtime team agent exists on any
harness; per-harness native agents are reduced to kernel helpers (e.g. `memory-scout`).

Role binding rules:

- Every topology node references a catalog unit; the node's `role` must equal the unit's
  `role` frontmatter. The topology separately declares `model_profile`, and concrete
  models resolve per adapter through `models.conf` — a unit never names a model or profile.
- Cross-harness review (including the hostile external-adversary pass) is realized by
  dispatching the relevant review unit to a different harness through the standard
  transport; there is no separate wrapper-team agent.

For standard+ code stage dispatch, role and profile are explicit: ordinary frame/plan use `deep maker + balanced-deep`, execute uses `fast implementer + light`, test/report use their fast roles with `light`, and route-selected high-risk or parallel legs override only the profile declared for that leg. Strong plan convergence uses `deep`; thorough implementation-risk exploration may deliberately use `light` while a failure-mode review uses `deep`.

---

## §3. Hard Cross-Document Invariants

1. Intensity selects graph and depth; §1.1 derives assurance from intensity. There is no user-facing `--qa` axis, and rigor alone cannot open dispatch depth 2 or a full pipeline.
2. Quick means one-session micro-plan, plan-check-lite, and verify-lite carrying the adversarial stance (§1.1). Requiring a durable plan, an added independent pass, or parallel/cross-harness reviewer fan-out for a small `direct`/`quick` task is still drift; the universal adversarial stance is a posture inside the existing check, not a new stage or session.
3. Adversarial means thorough plus a selected external adversary, failure-mode, security, or claim-verification pass. `standard + external/Codex` is not the definition.
4. Code has no fact-checker.
5. Do not hardcode code-test to thorough or parallel QA on every call; scale final verification from intensity-derived rigor. Registry-v6 `parallel_groups` alone declare an anchor's intensity-specific width, profile, perspective, join, and independence axes. Width stays 2–4, is selective rather than universal, and never applies to `direct`/`quick`.
6. `--no-fact-check` and `--no-style-audit` must not leak to unrelated capabilities.
7. An external review wrapper is not the reviewer; separate the independent engine from the mechanical orchestrator.
8. New or strengthened instructions, rules, and hooks preserve why, including the motivating incident and date, inline or in the commit message. Drills are the strongest executable preservation of intent.
9. Never reduce a semantic requirement to token or regex rules without verifying that meaning is preserved; see `DESIGN_PRINCIPLES §0.7`.
10. Token pressure is orthogonal to intensity and cannot reduce graph, depth, dispatch, model role/profile, assurance, required guards, or input context.
11. Primary routing is semantic (`WORKFLOW §0.2`): new empirical work keeps the execution capability primary, and secondary capabilities never substitute for it. Native sub-agent restrictions and registered headless-dispatch restrictions are separate delegation surfaces (`OPERATIONS §5.10`); extending one to the other requires verified runtime evidence, and the fallback is inline execution with the reason recorded.
12. Two assurance properties are intensity-independent: (a) every review that actually runs carries the refute-by-default adversarial **stance** of §1.1; (b) an independent pass declares and records its actual independence axes. Cross-harness remains primary, while model-profile and perspective asymmetry generalize dual-model direction exploration into bounded N-way groups. Only the registry may select or widen a group, and neither property converts `direct`/`quick` into added sessions.
13. Conditional follow-ups are route-sealed owner postconditions. They cannot be
    inferred from hooks, silently omitted while their readiness condition is
    true, or represented as an extra dispatch depth. A false condition records
    the declared skip without changing primary completion.
14. Process exit is not workflow completion (`WORKFLOW §0.6`). One portable
    state machine governs every tracked workflow; a capability declares stage
    graph, terminal nodes, and human gates but never redefines continuation or
    completion. `COMPLETE` is reachable only after every declared terminal node
    holds its completion gate, `BLOCKED_HUMAN_GATE` never advances
    automatically, and no failed stage advances a downstream one.
15. Every non-terminal stage declares exactly one continuation — `inline-next`,
    `supervised`, `human-gate`, or `monitor`. A detached resource node must be
    `supervised` and may never be terminal. A graph that violates this is
    rejected at route compile and at launch, not repaired at runtime. The
    continuation supervisor is one shared implementation
    (`OPERATIONS §5.12`); exactly-once advance is claim-based and restart-safe,
    and no model sleep loop or arbitrary detached shell substitutes for it.

Token-budget accounting is observation, not attribution. Hook invocations,
zero/emission outcomes, exact inserted-directive UTF-8 bytes, and monotonic
exact-session runtime counter deltas remain separate fields; none may be named
or derived as savings, billing cost, or ROI. Directive token counts remain
unknown unless an exact tokenizer for the actual payload is recorded with
runtime/model/version provenance. Accounting state must be content-free,
hashed by session, atomically updated under a bounded lock, bounded to 8 KiB per
file / 256 files / 2 MiB total with oldest-first aggregate pruning, and always
fail open without changing hook output. L2 diagnostics are on-demand only.

Dynamic token-pressure policy is an isolated experiment surface. Production
reinjection remains the static transition-only policy; production hooks and
runtime config must not import, activate, or fit an offline candidate. Paired
control/static/dynamic evaluation keeps input, model effort, intensity,
dispatch/depth, QA, required checks, and safety gates identical, performs no
input pruning or online/RL fitting, and may return at most
`eligible_for_user_review`. Adoption requires explicit user review and a later
spec/code cycle.

When adding an invariant, add its mechanically expressible portion to deterministic tooling or regression tests. Human source review owns semantic and wording consistency.

## §4. Cross-Document Verification Ownership

- Manifest, name, and path drift: `python3 tools/build-manifest.py --check`
- Runtime-native projections: each adapter's `sync-native-* --check`
- Canonical-to-adapter boundary: `tools/check-adaptation-boundary.sh`
- Skill structure and invocation: `tools/skill-conformance/check.sh`
- Installed runtime surface: `harness verify`
- Value proposition, information order, and semantic equivalence: human review; no automatic prose fix

### §4.0. Report Bundle Publication Contract

New published reports use `capabilities/report-bundle-manifest.schema.json`
schema v2. It is content-neutral: prose-only research reports are valid when
`index.html`, `REPORT.md`, every other internal document/asset, and their
SHA-256 values form a closed inventory. Local HTML, Markdown, and CSS links
must remain inside the report root and resolve; symlinks, missing or unlisted
files, root escapes, and hash mismatches fail closed.

The v1 `capabilities/report-manifest.schema.json` remains the compatibility
contract for 48 kHz media reports. In v2, media evidence is conditional: an
empty media set is valid, but once a sample is declared it requires the 1:1
audio/waveform/spectrogram/playback set, actual decode/playback validation,
and inventory-bound hashes. Publisher and dry-run backfill receive explicit
project, experiment, and version values and never infer version from paths or
timestamps.

Schema v2 stays exact: experiment logs, report documents, and media are ordinary
members of `files[]`; adding them never adds manifest properties or creates a
new schema version. A publishable experiment bundle keeps original logs under
`logs/`, canonical report/navigation documents at the report root, and evidence
under `media/`. Turso receives only stable bundle/document identifiers and a
manifest snapshot digest. It never receives original log, report body, media,
or an absolute bundle path. Receipt v2 is likewise IDs-only.

Published HTML is scriptless. The verifier rejects every `<script>` element,
active embedding elements, inline event handlers, `srcdoc`, script URL schemes,
and refresh redirects in HTML/SVG/XML. Consumers serve verified pages with CSP
`script-src 'none'; form-action 'none'`; only `<a href>` may be remote
navigation, while every other resource-bearing `href`/`src` stays in the closed
inventory. A playback page binds declared media through actual DOM media/link
elements. Declared audio is WAV, MP3, or OGG and must expose a decodable audio
stream. Waveform and spectrogram evidence is PNG, JPEG, GIF, or WebP with valid
magic and a decodable image stream. Every format uses the same bounded,
shell-free `ffmpeg -xerror` path. Missing ffmpeg, duplicate sample kinds, or a
decode/playback failure is an integrity failure. A serialized manifest is at
most 1,048,576 bytes; `files` and `media` are each capped at 10,000 rows.

Publication copies into a sibling staging directory, hashes each regular
single-link file through one descriptor, verifies the closed staged inventory,
and uses an atomic same-filesystem no-replace rename. Existing identical
versions are unchanged; collisions fail closed. Consumers mount the bundle root
read-only. A periodic full verifier records per-bundle state only on a health
transition (`healthy|broken|checking` with machine reason codes); unchanged
health causes zero per-bundle writes. One separate bounded global heartbeat per
run proves monitor liveness and freshness. Transient root/NFS unavailability is
not rewritten as integrity loss.

Existing-note backfill uses the authoritative 38-bundle census and emits only
ordered `document_id` to existing `note_id` mappings. It validates canonical
source path, manifest hash, hierarchy, unique note identity, and canonical
project-root device/inode before producing an IDs-only dry-run request. Missing,
extra, duplicate, ambiguous, aliased, or order-mismatched rows reject the whole
candidate. Apply belongs to Cairn and may write only bundle/document/link/health
rows; the `l2_notes` row count, IDs, bodies, revisions, cards, parents, and pages
remain byte-identical. Link rewriting is consumer-owned presentation only and
never weakens the producer's exact source hash proof.

### §4.1. Report Figure Evidence Contract

Report spectrograms separate the computation contract from the communication
contract. A metric may use a narrow analysis interval such as
`METRIC_BAND_HZ = (20, 1000)` while the report figure independently uses
`FIGURE_BAND_HZ = (0, 24000)` for 48 kHz audio. Analysis crops and metric helper
defaults must never flow implicitly into plotting functions; figure-band
arguments are explicit at the report boundary.

Every report spectrogram has a machine-readable manifest entry with
`sample_rate_hz`, `min_hz`, `max_hz`, `dynamic_range_db`,
`shared_scale_per_figure`, and `colormap`. The 48 kHz full-band report profile
is fail-closed: it requires exactly `sample_rate_hz=48000`, `min_hz=0`,
`max_hz=24000`, and `shared_scale_per_figure=true`. Missing or mismatched
metadata prevents completion.

Band-sensitive claims, including full-band, broadband, and high-frequency
language, must name figure or metric evidence whose recorded range contains the
claimed range. High-frequency prose must attach an explicit Hz/kHz range to
the high-frequency term (for example, `high-frequency (8–24 kHz)`) and match
its manifest range; this avoids trusting an unrelated or unobservable annotation.
A low-band metric cannot support a full-band claim. The report
figure verifier scans prose as a discovery backstop and checks the registered
claim-to-evidence mapping; this deterministic gate does not replace human
semantic review.

After generation, visually inspect at least one representative PNG per
spectrogram figure group and record evidence for a 0–24 kHz y-axis, readable
ticks and labels, a colorbar, and a shared comparison scale. The verifier also
checks that this evidence is present and positive; it cannot infer visual
truth from file existence alone. The portable checker is
`tools/figure-semantic-verify.py`, exposed by runtime-native figure-generation
tool contracts where supported.

### §4.2. Verification Command Shell Portability

Verification and scan commands must not depend on the invoking login shell's
dialect. The interactive default here is zsh: it does not word-split unquoted
variable expansions, so a newline-joined file list silently collapses into one
path, and Bash-only builtins (`mapfile`/`readarray`, `<<<` here-strings) fail
outright. Grounded by the 2026-07-16 diagnosis where a worker's `mapfile` lint
and a newline-expansion static scan both produced false verification verdicts.

- Pass file lists null-delimited — `find … -print0 | xargs -0 …` — or through
  the canonical helper `utilities/verify-files.sh` (POSIX sh, safe under
  direct zsh/bash execution, deterministic C-locale order, prune/name-glob
  filters, xargs exit-status propagation).
- Do not use Bash-only syntax in inline tool calls or in snippets that agents
  copy into their shell. A script that genuinely needs Bash declares a
  `#!/usr/bin/env bash` (or `/bin/bash`) shebang and is executed, never
  sourced into the calling shell.
- A multi-file verification helper must pass an explicit dual-shell smoke:
  byte-identical output when executed by sh, bash, and zsh
  (`utilities/verify-files.test.sh` is the reference pattern).

### §4.3. Worktree Build Residue Hygiene

A build run inside a linked worktree must not leave untracked artifacts that
pollute `git status` and block guarded cleanup. Grounded by the 2026-07-16
diagnosis where dependency-tracing stubs appeared under a worktree on every
webpack build and required manual deletion before cleanup.

- The essential fix belongs to the project's build configuration (for
  example, pinning the dependency-tracing root to the primary checkout so
  stubs are never written into the worktree). Prefer it whenever available.
- The deterministic defense layer is `utilities/worktree-residue.py`. The
  project declares residue globs in `<worktree>/.agent-build-residue` (or the
  orchestrator passes `--glob`); `--check` reports, `--clean` removes.
- The helper is fail-closed: only untracked, non-ignored, pattern-matched,
  worktree-contained paths are removable; symlinks are unlinked and never
  followed; zero patterns refuses to clean; every removal is appended to
  `<agent-home>/.dispatch/build-residue.jsonl`.
- `--clean` is an explicit orchestrator action run before
  `worktree-cleanup.py`; it does not change guarded-cleanup eligibility
  semantics, it only replaces the manual deletion step.

## §5. Skill Output Convention — T1/T2/T3

Every autopilot capability and `analyze-project` follows this artifact structure. Existing artifacts keep their legacy flat layout; new invocations use this convention.

### §5.1. Workspace Assumption

Skills run from the project root. Resolve the project-wide write surface with
`utilities/artifact-root.sh`. In a linked task worktree this selects the
primary worktree's canonical `.agent_reports/`, not the tracked local
snapshot; legacy `.claude_reports/` is selected only when it already exists
at the canonical project root and the new root does not. `analyze-project`
reads the current source checkout, `autopilot-code` mutates code there, and
draft/research/refine read and write persistent inputs only below the canonical
artifact root. Cross-project work changes cwd and uses another session.

Non-Git cwd resolution never inherits a strict ancestor's root by mere
discovery. The cwd's own root (`.agent_reports/` or legacy `.claude_reports/`)
is used first when present — self is not inheritance and needs no marker.
Beyond that, a strict ancestor's root is inherited only when that ancestor
directory also holds an `.agent-workspace` marker file; an empty file is
sufficient, and an optional `scope:` comment is for human readers only. Absent
both, the root is `<cwd>/.agent_reports/`. This Git-worktree resolution path —
primary worktree wins — is unchanged by the marker rule.

Artifact directories are gitignored in every tracked repository, including
`<agent-home>` (2026-07-31 policy change for the public v2.0 release: the
former agent-home exception that committed artifact history is retired;
pre-2.0 history remains reachable in git history). Add `.agent_reports/` to
`.gitignore` on first creation; treat legacy `.claude_reports/` similarly.
Runtime grounding state (`.capability-grounding/`, `.route-grounding/`,
`.spec-grounding/`, `.core-grounding/`) is likewise never tracked. Linked
worktrees resolve the primary checkout's canonical root; durable writes still
target it. Transient locks and untracked markers remain ignored.

Inputs come from persistent project artifacts. External raw material is first normalized through `analyze-project --mode paper|doc`; the family has no flag for arbitrary external artifact directories.

### §5.2. Tier Definitions

| Tier | Meaning | Location |
|---|---|---|
| **T1 primary** | Core index and deliverable that users routinely see | Artifact directory root |
| **T2 secondary** | Chapters, strategy, analysis, logs, and supporting assets read as needed | Named subdirectories |
| **T3 tertiary** | Reviews, raw metadata, and version snapshots rarely read directly | `_internal/` |

The underscore keeps internal data visible but de-emphasized; a dot directory would hide it too strongly.

### §5.3. Standard Shape

```text
<artifact-dir>/
├── pipeline_summary.md
├── <T1 deliverables>
├── <T2 subdirectories>
└── _internal/
    ├── <capability-specific review directories>
    ├── <raw metadata>
    └── versions/v{N}/<changed files>
```

For spec updates and route-backed document/research refinement, snapshots are
machine-prepared from the exact current bytes before the first canonical write.
A model must not allocate a version or copy a snapshot by hand. One route
reuses one `v{N}` directory for every changed file in the same artifact.

### §5.4. Capability Mappings

#### §5.4.1. Research

`<artifact-root>/research/<topic>/` contains T1 `pipeline_summary.md`, `pipeline_state.yaml`, `00_briefing.md`, and numerically ordered report chapters; T2 `analysis_summary.md`, `cards/`, `code_resources/`, and `figures/`; and T3 search results, batches, access classification, chaining, code search, prefetch, reviews, and versions under `_internal/`. Keep chapters at root because numeric prefixes already group them.

#### §5.4.2. Documents

`<artifact-root>/documents/<date>_<name>/` contains T1 pipeline state and the latest `draft/`; T2 latest `strategy/`, `analysis/`, and `assets/`; and T3 metadata, strategy/draft reviews, audits, discarded variants, and major-refine snapshots under `_internal/versions/v{N}/`. Direct minor edits remain snapshot-free and are recorded in `pipeline_summary.md`. Retire sibling `_v{N}.md` files for new output but preserve them in legacy artifacts.

#### §5.4.3. Code Track — Flat `spec/` Plus Repeated `plans/`

One repository normally has one flat `spec/`. Only a monorepo with independently delivered components and separate PRDs uses `spec/<component>/` and `plans/<component>/<cycle>/`.

```text
spec/
├── prd.md
├── ship.md
├── stack.md
├── design/
├── pipeline_state.yaml
└── _internal/versions/v{N}/prd.md

plans/<date>_<slug>/
├── pipeline_summary.md
├── plan/                 # plan.md, optional localized variant, checklist.md
├── dev_logs/
├── test_logs/
└── _internal/            # plan, dev, and test reviews
```

`prd.md` is always current. Every transaction that changes an existing
`prd.md` snapshots its exact pre-image before the transaction command can
overwrite it; initial creation and no-op updates create no version. Minor edits
still append to pipeline history, and five accumulated minors trigger an audit
alert. Code history uses git rather than `autopilot-refine` by default.

#### §5.4.4. Project Analysis

`analysis_project/code/` and `analysis_project/paper/` are flat and cumulative per project. `analysis_project/doc/<name>/` is per task because document inputs vary by reviewer, template, patent, or other source set. Each mode keeps user-facing overview and analysis at T1/T2 and raw scans or QA under `_internal/`.

### §5.5. Legacy Compatibility

For a new or empty directory, create the modern layout. On re-entry, the presence of `_internal/` selects modern behavior; otherwise main-level review directories or sibling `_v{N}.md` files select legacy behavior. Preserve the detected shape. Migrate only on an explicit user request through a one-off helper.

New route-owned output may be created only in the capability buckets listed in
§6.5. `autopilot-refine`'s `target-artifact` scope resolves only to
`documents/<artifact>/**` or `research/<artifact>/**`; an ad-hoc top-level
folder such as `rebuttal/` is input or legacy state, never a new output target.

### §5.6. Authoring `SKILL.md`

This section applies to orchestrator-level capabilities that create artifact directories, not sub-capabilities operating inside them.

- Express output locations through tiers and convention-relative directories rather than brittle absolute paths.
- Include one pointer to this section.
- Use exactly one `## Reference Index` table with file, load timing, and obligation columns. Do not split required reads from a reference map or weaken a mandatory resource into a filename-only pointer.

### §5.6a. Quantitative Skill-Design Rules

| Rule | Requirement | Scan columns |
|---|---|---|
| `SKILL.md` body | Under 500 lines | `body_lines`, `line_ok` |
| `references/` | One level, no nested directories | `ref_dir`, `ref_depth_ok` |
| Invocation frontmatter | Manual-only uses `disable-model-invocation: true`; parent/pipeline or subagent-preloaded Skills remain model-invoked; entry routers remain model-invoked and include a concrete English “Use when” trigger plus a “Not for” boundary | `disable_model`, `invocation`, `use_when` |

The 13 manifest `entry-router` Skills are compact pre-approval routers. Each
router is limited to 4,096 UTF-8 bytes, their aggregate is limited to 53,248
bytes, and its single `## Reference Index` exposes exactly one post-approval
owner edge. Procedure detail belongs in that one-level owner reference. Report
static bytes only; do not infer token, billing, cost, savings, or ROI.

`harness-manifest.json` owns each capability's invocation class, positive
trigger, and exclusion boundary. `tools/skill-conformance/invocation-policy.tsv`
is a generated registry projection, and `tools/skill-conformance/check.sh`
compares every adapter realization with it before merge. Generic or circular
triggers such as `Use when needed` and `Use when invoking the portable ...
capability` fail conformance. `disable-model-invocation: true` is a hard boundary
that also blocks programmatic Skill calls and subagent preload, not a
recommendation-strength knob. `user-invocable: false` controls menu exposure
separately. The 13 current parent-invoked sub-Skills remain model-invoked but
identify their owning parent and top-level exclusion; model-support Skills are
model-visible helpers, not primary entry candidates. `DESIGN_PRINCIPLES §10`
owns the qualitative design tenets.

The conformance gate enumerates the portable capability domain and checks every
active Claude, Codex, and OpenCode Skill realization. Runtime-specific
frontmatter is interpreted by an explicit adapter rule; it never permits one
adapter's successful scan to stand in for another. Bootstrap, discovery, and
hook budgets are separate and canonical in `ADAPTATION §6.1`.

### §5.7. Backward-Compatible Detection

```bash
test -d "<artifact-dir>/_internal" && CONVENTION=modern || CONVENTION=legacy
if [[ $CONVENTION == legacy ]]; then
  REVIEWS_DIR="<artifact-dir>/strategy_reviews"
  VERSIONS_PATTERN="_v{N}.md sibling"
else
  REVIEWS_DIR="<artifact-dir>/_internal/reviews"
  VERSIONS_PATTERN="_internal/versions/v{N}/"
fi
```

Always create `_internal/` for a new artifact, even when empty, to mark modern layout.

## §5.8–§5.11. Operations

Pipeline lock, git preflight, worktree dispatch, and `<agent-home>` push policy moved to `OPERATIONS.md` on 2026-06-23 with numbering preserved.

## §6. Autopilot Flow Matrix

`WORKFLOW.md` owns detailed routing. This section preserves family-wide operational boundaries.

### §6.1. Work-Nature Matrix

| Work | Prior research/analysis | New intent | Asset work |
|---|---|---|---|
| Documents | `autopilot-research` plus paper/doc analysis | `autopilot-draft` | `autopilot-refine` |
| Code in any product shape | Research plus code analysis | `autopilot-spec` with PRD, architecture, and skeleton | `autopilot-code` adds logic over the scaffold |
| One-shot ML prototype | Code-analysis experiment inputs plus prior RUNLOG | No spec for the fast cycle | Iterative `autopilot-lab`, graduating to code |
| Visual design | — | `autopilot-design` | Repeat design cycles |
| User profile | — | `analyze-user --mode init` | `analyze-user --mode update` |

Project experiment conventions are the first source for coding behavior; `mem profile 07_coding_convention` is the cross-project fallback.

### §6.2. Common Invocation Shapes

```text
# Research and experiment
autopilot-research? → analyze-project(code)? → autopilot-spec(research/cli) → autopilot-code → autopilot-lab ↻

# Library and CLI productization
analyze-project → autopilot-spec(library/cli) → autopilot-code ↻

# Documents
autopilot-research? → analyze-project(paper/doc)? → autopilot-draft → autopilot-refine ↻

# Apps
autopilot-research? → analyze-project(code)? → autopilot-spec(app) → autopilot-design? → autopilot-code ↻ → autopilot-ship
```

### §6.3. Separation by Work Nature

Draft and refinement are separate because refinement may compare across prior documents. New and existing code share one implementation flow because only code state changes. Spec and code remain separate because product decisions and skeleton generation differ from logic implementation, while app/library/api/cli/research remain modes of the same spec capability.

### §6.3a. Atomic PRD Updates

Update every affected textual contract and architecture diagram in one transaction.

| Change | Bundle |
|---|---|
| API endpoint, body, or error | API contract, Component, optional Sequence |
| DB entity or field | Data model, backend Component, optional ER |
| UI flow | UI flow, frontend Component, optional Activity |
| External service | Auth contract, Deployment, deploy record, `.env.example` |
| Stack replacement | Stack decision, Component, Deployment |
| State model | Data model, optional State |
| Public library API | Public API, examples, compatibility and semver, module Component |
| CLI command or option | Command, option, exit code, README example, command-tree Component |

App and API modes include Component and Deployment by default. Library Component is optional. ER, Sequence, Activity, State, and Class appear only for complexity or explicit request.

### §6.4. Context Auto-Detection

Code, spec, lab, research, and design inspect their state file to distinguish new work from re-entry, classify the requested stage from the prompt, and present one concise confirmation surface where the capability contract requires it. Each capability's `Context Auto-Detection` section is the source for its stage names. Draft and refine remain separate work types rather than automatic stages of one capability.

### §6.4-staleness. Analysis Refresh

After code changes, `autopilot-code` Step 7 directly updates a small one-module or signature change in `analysis_project/code/`. New modules, model directories, cleanup, or experiment-input changes invoke incremental `/analyze-project --mode code --skip-qa`. Incremental analysis reads `_last_run.yaml` and reanalyzes changed files by default; `--full` redoes everything. Explicit `--no-analyze-update` skips the step.

### §6.4-legacy. Code Context

When `spec/pipeline_state.yaml` exists, read it and activate every applicable app, library, API, CLI, or research mode rule. Without it, infer only lightweight context from cwd signals. App adds design critique, guarded migrations, and deployment awareness; library adds semver/export/example checks; API adds contract and auth security checks; CLI adds command/I/O/exit-code checks; research adds reproducibility and expected-metric checks.

### §6.5. Output Locations

| Capability | Output |
|---|---|
| research | `research/<topic>/` |
| project analysis | `analysis_project/{code,paper,doc}/` |
| spec | `spec/` |
| ship | `spec/ship.md` plus runtime source config at project root |
| standalone design | `designs/<name>/` decision record; live app file is the sole token contract |
| spec-owned design | `spec/design/` decision record |
| code | `plans/<date>_<slug>/` |
| lab | `experiments/<date>_<slug>/` plus `_RUNLOG.md` |
| draft | `documents/<date>_<name>/` |
| refine | Target artifact plus `_internal/versions/v{N}/` for a major refine; direct minor edits update history only |
| note | Run logs in artifact root plus routed cards and digests under the configured notes target |
| apply | Real source outside artifact root; git branch and commit provide versions, with apply logs under the cheatsheet artifact |

### §6.5-anchors. Anchor Resolution

Every track's scopes above resolve against exactly one bucket domain:
cycle-relative (code, lab, draft, research: relative to that capability's
`<date>_<slug>` or `<topic>` cycle directory), artifact-root-relative
(project analysis, spec, and design: relative to a fixed sub-path under the
artifact root), or outside the artifact root (apply's real source target,
symbol tokens the compiler never substitutes). Standalone design anchors at
`designs/<name>/`; spec-owned design anchors at `spec/design/`; spec-owned
byproducts (research shards, review shards) anchor at `spec/_internal/`
(or `spec/<component>/_internal/` for a component spec). A parallel leg's
suffix (e.g. `-alternative`) may only appear as a subdirectory inside the
owning anchor, never by escaping it.

route lifecycle records live in a single hidden runtime location:
`<artifact-root>/.runtime/routes/<route_id>.json`, with the sole terminal
sidecar `<route_id>.outcome.json` beside it. A slug, capability, date, node, or
attempt suffix is not a valid new basename even inside the canonical directory.
`compile --output` outside that exact path is a typed rejection; omitting
`--output` defaults to it. Existing aliases in the canonical directory and
records in four legacy locations (root-level `*-route.json`, `routes/`,
`_routes/`, `.routes/`) remain readable and closeable so they are not stranded;
`status` reports them as `alias_basename` or location drift. Only new writes to
aliases and legacy locations are blocked.

`.runtime/` is the only bucket name for artifact-root-scoped runtime state.
Legacy `_runtime/` is recognized read-only and must never be freshly created.
File-based memory is a retired mechanism: the memory database is the sole
source of truth, and harness code must not create new `NOTES.md`, `memo.md`,
or `.claude/agent-memory/` inside an artifact root (existing files are left in
place, not deleted). cwd-scoped grounding state (`.route-grounding/` and
siblings) is not an artifact and is unaffected by this section.

### §6.6. Autopilot Intake Gate

Immediately after entry, if irreversible choices are genuinely under-specified, ask one structured round before production. This is semantic agent judgment, not a keyword or hard-hook classifier.

The round:

1. provides enumerated options for each question;
2. always permits free-form input or proceeding with a recommended default;
3. runs once at entry and never repeatedly;
4. covers only expensive-to-change choices such as stack, public API, deployment target, tone, or brand—not reversible implementation details.

Question banks:

| Track | High-cost choices |
|---|---|
| Documents | Audience, length/page limit, paper/slide/prose form, tone, deadline and constraints |
| Research | Depth, citation/year cutoff, domain boundary, comparison priority, decision purpose |
| App spec | Stack, auth model, persistence, deployment target, core entities |
| Library/CLI spec | Public exports, semver policy, command/options, runtime/package manager, compatibility |
| Design | Visual direction, target device, design-system availability, brand constraints, standalone versus project output |

Code uses the bank for its spec mode. Skip automatically when adapter-native arguments already specify the choice, the user already said it, the work is explicitly throwaway, or a state file captures it on re-entry. `--no-clarify` exists only for draft and research.

If a non-blocking intake question receives no answer, proceed with the recommended default and report one line. Runtime adapters may provide a scheduled wake-up for a genuinely long wait, but ordinary unanswered intake does not pause the pipeline.

Draft Step 0 and research Step 1.5 are the existing track-specific instances. Spec, code, and design use this common gate.

## §7. Memory

Unified memory moved to `MEMORY.md` on 2026-06-23 with §7 numbering preserved. That file is the single source.
# Route, resource, and report invariants

Dispatch depth applies to portable route ownership: quick is dispatch depth 1,
standard+ has a dispatch-depth-1 owner and at most dispatch-depth-2 nodes, and a
native or inline fallback keeps that logical value without becoming a registered
worker. Resource runners are detached processes with no dispatch depth, while
runtime-native subagents and Claude agent-team teammate sessions retain their own
runtime semantics. Review workers write isolated verdicts and map workers write
isolated shards; one owner performs canonical merges. Completion is route-hash,
node, exact attempt, execution-surface, and evidence bound, so a stale dispatch
row or a same-evidence retry with different attempt axes is not completion.

Lab full-run entry requires a current hash-bound smoke attestation. Reports with media use `capabilities/report-manifest.schema.json` as the shared report manifest: each audio sample has 1:1 waveform/spectrogram/playback media, hashes and visual evidence are bound, and the house audio parameters are 48 kHz with the full 0–24 kHz band. Its optional `bundle` block declares representation `format` separately from semantic `roles` (`canonical`/`summary`/`interactive`/`navigation`), one shared `title`, and one `primary_representation_id`: summary statistics bind to the primary plus every canonical/summary representation, media links bind to interactive ones, and equivalence is declared only through `bundle.equivalence_groups` with a shared title and declared ordered section identity — never inferred from filename, extension, or coexistence. A manifest without `bundle` is classified `legacy/unspecified` and keeps the version-1 both-outputs rule unchanged.
