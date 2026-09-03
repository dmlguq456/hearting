# Autopilot Design Principles

> The architectural constitution under `<agent-home>/core/`: the single source for how the autopilot family separates and collaborates.
>
> Sibling sources:
> - `CONVENTIONS.md` owns QA levels, portable model roles, artifact folders, and hard invariants.
> - Main-agent response behavior has two layers. `roles/response-policy.md` owns the runtime-neutral minimum, including audience language, response discipline, pause/autonomy, and follow-through. Each runtime adapter bootstrap adds only runtime mechanics and non-locale-specific voice details.
>
> This document contains the structural and behavioral skeleton. Definitions, policies, and operational wording remain in the sibling sources.

---

## §0. Purpose — Model-Agnostic Skeleton

The fundamental purpose of `<agent-home>` is a work substrate that does not depend on a particular model and continues to work after switching LLMs. A Skill, agent, or harness surface is made from prompts, local tools, encoded judgment guidance, and scaffolds that survive replacement of the model.

- **Value invariant:** even when one vendor currently offers a better built-in capability, do not remove the portable on-premises realization for that reason. Vendor advantage disappears on a model switch; a portable capability survives.
- **Evaluation criterion:** do not measure a capability only against the current runtime. Ask whether GPT, Gemini, a local model, or another future engine could drive the process adequately. Zero daily usage is not sufficient reason to remove it; some components are model-switching insurance and substrate.
- **Internalization:** reverse-engineer well-designed vendor pipelines into portable capabilities rather than depending on their invocation. Examples include deep research into claim verification, native security review into the QA security role, and public design rules into `_design_rules.md`. Preserve source work under `<nas-archive>/claude-meta-spec/`.

When a structural decision conflicts with §0, this section wins.

---

## §0.5. Deterministic First

Mechanize what is genuinely deterministic through hooks, scripts, gates, and DB constraints so agents can spend judgment on meaning and creativity. This 2026-06-15 principle generalizes the deterministic orchestrator state machine across the harness.

- Agent judgments can be inconsistent, fallible, and token-expensive. Mechanical enforcement is cheap, exact, reproducible, and model-independent.
- For a new feature or policy, first ask whether code can enforce or automate it. Use an instruction only for the semantic or creative judgment that cannot be reduced without losing meaning.
- Examples include routing state machines, artifact/git/lock guards, schema and scope checks, pending protection, turn-counter nudges, and machine-checkable QA criteria.
- The anti-pattern is delegating a mechanical invariant to “the agent will decide each time.” The opposite anti-pattern is encoding semantic relevance as token rules merely because code can match text.

Mechanization is not a license to expand the harness. Resolve each issue at the
smallest appropriate layer; before adding an instruction, gate, command,
schema, registry, or other global device, simplify or extend an existing
surface. Add a new device only after repeated evidence and an explicit check
that existing mechanisms cannot solve the problem. When a runtime lacks the
needed mechanic, mark it unsupported and give a checked fallback instead of
simulating parity. Keep each semantic rule in one owning document; adapters map
runtime mechanics and point back rather than duplicating it.

This section follows only §0 in priority. True semantic judgment is the explicit exception; §0.7 verifies that boundary.

---

## §0.7. Semantic-to-Rule Boundary Verification

This verification layer was added after the 2026-06-22 worklog-board incident. §0.5 asks at design time whether a behavior can be mechanized. §0.7 asks during authoring, completion, and regression whether mechanization accidentally reduced a semantic requirement into brittle rules.

Deterministic code owns branch boundaries, routing, gates, and hard invariants. Contextual appropriateness and semantic matching belong to the model. Deciding whether a requirement is semantic and whether an implementation captures it is itself semantic, so the verdict remains model-owned. Determinism owns when and where that review runs: spec Step 3d, automatic audit scope, and drills.

The incident-shaped anti-pattern is a spec that explicitly requires semantic judgment but an implementation that replaces it with token matching, regex, or fixed rules and then fails silently.

Memory follows D-40: the acting agent decides storing, retrieval, promotion, merge, and pruning. Deterministic memory code enforces integrity, scope, pending protection, lifecycle mechanics, bounded telemetry, and recovery; it does not infer relevance from prompt tokens, fixed phrases, content categories, or confidence thresholds.

Three-step review:

1. Locate semantic requirements in the spec, including contextual or appropriateness judgments.
2. Inspect whether the implementation substitutes only fixed tokens, regex, or rules for that meaning.
3. Resolve a conflict by narrowing the spec into an honestly deterministic requirement, moving the implementation back to an LLM judgment, or adding an explicit semantic fallback after the deterministic branch.

Run this review while authoring at `autopilot-spec` Step 3d, after completion in the audit plans aspect, and during the corresponding drill. These three points do not yet cover the exact development moment when code first introduces the mismatch; audit remains the backstop. `CONVENTIONS §3 item 7` holds the one-line operational invariant.

---

## §0.8. Loop Engineering First Principle — Core First, Adapters Derived

Establish the shared contract in core, then derive adapters. A runtime-specific improvement must first be expressed or confirmed in the model-neutral `core/` contract, then mapped into each adapter surface.

- Adapters map runtimes; core owns meaning. Adapter-first edits cause behavior to diverge across Claude, Codex, and OpenCode and erase intent during the next port.
- Claude, Codex, and OpenCode are sibling realizations. No adapter may serve as another adapter's semantic source or completion proxy; an unverified sibling keeps the shared change partial.
- Read the relevant core document, update the contract or confirm that it already covers the behavior, then edit bootstrap, hook, or projection files.
- `core-read-marker` and `core-first-guard` mechanically require a session read marker before `adapters/**` edits. They verify the read, while this principle and drills verify that core actually led the change.
- The motivating incident was a 2026-07-03 adapter recall edit made before the core contract and then reverted.

---

## §0.9. Proposal-Gated Improvement — Evidence Is Not Activation

Harness learning and harness mutation are different phases. A loop may capture
an incident, build a fixture, and draft a candidate, but the active contract
changes only through the ordinary spec/code/release path.

- Adoption applies to a portable invariant; runtime realizations remain bound
  to exact runtime, plugin, documentation, and active-provider fingerprints.
- A fingerprint change invalidates automatic reuse of the realization and
  requires revalidation, native-supersession, retirement, or human resolution.
- Generated projections, installed plugin caches, and runtime-owned config are
  never self-edit targets.
- The approval and ownership guards are a trust root: a candidate may propose a
  change to them, but cannot weaken and use the new guard in the same cycle.

This principle narrows D-25 for harness policy: reversibility alone does not
authorize unattended instruction, adapter, plugin, or setting changes.

---

## §0.6. Prefer Positive Instructions

Describe the desired behavior rather than accumulating “do not do X” patches. Naming an unwanted behavior can prime it, and repeated hotfix prohibitions create noise while hiding the original cause.

- If an unwanted behavior came from an existing misleading mention or ambiguity, remove that mention or rewrite it positively instead of adding another ban.
- Add a new instruction only when the cause is external to the existing contract.
- Negative instructions remain appropriate for safety, irreversibility, security, and hard gates where a positive formulation cannot safely block the action.
- Apply this review to every instruction edit, including direct hotfixes and meta-Skill changes. Reviewers check negative overuse, priming risk, and symptom-only patches.

---

## 1. Three-Tier Role Separation

| Tier | Role | Examples | Anti-pattern |
|---|---|---|---|
| **Orchestrator** | Deterministic state machine owning routing, gates, and verdicts rather than content | Main bodies of `autopilot-code`, `autopilot-draft`, `autopilot-research`, and `autopilot-refine` | Reading, summarizing, or judging delegated content |
| **Skill** | Expert capability defining its work and selected assurance gate | `code-plan`, `code-execute`, `code-test`, `draft-strategy`, `draft-refine` | Hiding the entire pipeline graph or repeated QA loop inside one Skill |
| **Agent** | Tool-enabled persona that performs work inside a Skill | Role Catalog in `roles/README.md` | Returning verbose bodies to the orchestrator instead of artifacts and verdicts |

This role separation is distinct from the T1/T2/T3 artifact visibility tiers in §4.

Primary entry capabilities add a load-phase boundary to that separation. A
compact pre-approval router exposes only manifest-owned routing metadata and a
single owner edge. After route approval, the selected capability owner loads
the portable owner contract and materializes the stage graph; an assigned
stage worker loads only its stage contract. Parent-invoked and model-support
Skills are not primary-router candidates.

```text
Orchestrator → Agent: file paths plus a one-line task directive
Agent → Orchestrator: file path plus a one-line verdict token
Agent ↔ Agent: file system; the next agent reads the prior artifact
```

The no-read rule applies only to the orchestrator. Skills and their agents read files freely. A single expert flow may combine orchestration and execution when splitting it would add more cost than capability separation—for example, investigate → plan → diff preview → apply → report in one refinement stage. A sub-Skill may also make a natural extension such as final-report updating `analysis_project/code/`. Split only when two or more genuinely distinct expert capabilities are present.

---

## 2. Default Autopilot Behavior

Autopilot members run their pipeline to completion after the primary entry
intent is aligned. `WORKFLOW §0.4` owns the one-time route confirmation before
execution; it is distinct from an internal pipeline pause.

- Enable `--user-refine`, `--confirm`, or an equivalent pause only when the user explicitly asks for it; defaults remain false.
- Select expensive `thorough` or `adversarial` graphs from explicit signals or high-risk routing. Verification rigor derives from intensity; it is not a separate graph or `--qa` selector.
- On failure, fail loudly, record unresolved work in `pipeline_summary.md`, and allow the next call to resume with `--from <stage>`.

Once the user confirms the proposed route—or has already explicitly approved
the same route and scope—adding cautionary or stage-by-stage confirmations
delays work and creates follow-up friction. The portable response policy and
each capability's pause-option contract enforce this principle.

---

## 3. Implicit Input Discovery

Discover inputs automatically from persistent artifacts in the project context:

- `$AGENT_ARTIFACT_OUTPUT_DIR/analysis_project/{code,paper,doc}/*` from `analyze-project`;
- `$AGENT_ARTIFACT_OUTPUT_DIR/research/<topic>/*` from `autopilot-research`;
- external raw inputs first persisted through `analyze-project --mode code|paper|doc`, then found through contextual matching.

The assumption that one session works in one cwd reduces cognitive cost and increases artifact reuse. Cross-project work changes into the other repository and uses a separate session.

---

## 4. Artifact Convention — T1/T2/T3

`CONVENTIONS §5` is the detail source for visibility inside an artifact directory.

| Tier | Location | Examples |
|---|---|---|
| **T1 primary** | Root | `pipeline_summary.md`, `draft/`, `plan/` |
| **T2 secondary** | Named subdirectory | `strategy/`, `analysis/`, `dev_logs/`, `test_logs/`, `cards/` |
| **T3 tertiary** | `_internal/` | Review logs, version snapshots, raw scan metadata |

Follow-up changes to one artifact also have two scales:

| Scale | Handling | Tracking |
|---|---|---|
| **Minor**, default | Direct edit | Minor log entry in `pipeline_summary.md` |
| **Major**: user-declared, structurally at least 200 lines, or immediately before external review | `autopilot-refine` ceremony | Snapshot, integrated history, and QA |

After five accumulated minor changes, surface an `/audit` alert. Audit compares both against the last major version and against universal principles.

---

## 5. Quality Gates

Quality gates live inside the selected graph. Intensity chooses the graph; derived verification rigor scales plan checks, selected independent passes, and final verification. A high rigor tier does not secretly open stages, repeat loops, or dispatch-depth-2 workers.

- QA assurance levels—quick, light, standard, thorough, adversarial—are check budgets defined in `CONVENTIONS §1`, not graph selectors.
- Stage-local gates stay small and answer only whether the next stage may proceed.
- Every review that runs is adversarial in stance regardless of intensity: it tries to falsify the artifact, enumerates the concrete failure modes it can substantiate, and treats inadequate evidence as not proven. This stance is universal and near-free; it is distinct from a separate adversary pass, which only higher tiers add (`CONVENTIONS §1.1`).
- Independent QA runs only where intensity and risk call for it. Registry-declared 2–4-way groups combine cross-harness placement with asymmetric execution profiles and perspectives, then join evidence before continuation. Width two is the normal cheap-breadth starting point; selected high-value anchors may add a third deep contrarian, light implementation-risk, or deep failure-mode leg. Direct and quick stay single-session. When one harness is available, fail loudly for an explicit cross-harness request or record typed degradation for an auto-selected group.
- Fact and source checking applies to selected document, research, refinement, and note claims. Code uses code, tests, and runtime behavior as ground truth. `--no-fact-check` remains limited to capabilities that expose it.
- In paper mode, integrate reviewer feedback naturally. Do not paste an entire table or enumeration into prose. If one or two inline sentences cannot express it, drop it or move it to an appendix. The four-step Paragraph Cohesion Pre-Check evaluates substantive duplication, paragraph axis, cross-section redundancy, and the EDIT/REPLACE/INSERT/DROP action before a mechanical insert.

Keeping assurance separate from stage selection prevents small work from becoming full ceremony while still allowing cross-harness review outside a monolithic Skill.

---

## 6. Output Surface

User-facing Markdown owns both factual quality and readable rhythm. The editorial role performs the final pass.

- **Audience-language first:** a document or artifact intended for the user defaults to the language the user currently uses. Explicit target language, publication venue, external audience, or existing artifact language overrides that default. Public repository documentation follows the repository's chosen documentation language.
- Editorial modes are translation into a target language, language-independent polish, and review-only.
- Avoid unnecessary language mixing when a natural expression exists in the target language. Preserve domain terms, proper names, and established loanwords.
- This applies to every user-facing Markdown artifact, including final reports, audits, refinement results, and pipeline summaries.

Main-agent replies are a separate two-layer contract. `roles/response-policy.md` owns the portable minimum; runtime adapters add routing, tools, and session lifecycle mechanics without imposing a fixed locale or redefining portable clauses. Worktree safety belongs to `OPERATIONS §5.10`, not response style.

---

## 7. Memory Layers

### Memory application (D-40)

Project memory distinguishes explicit user-directed notes from agent-learned records.

| Layer | Location | Owner | Purpose |
|---|---|---|---|
| Explicit user note | DB working tier through `/post-it` and `mem note` or `mem add`; the former five categories remain as type taxonomy | User-directed `/post-it` | Conventions, resources, open threads, decisions, and next-session hints the user wants retained |
| Agent learning | DB working or durable tier populated by an external distiller from session deltas | Detached distiller triggered by turn count or SessionEnd | Reusable procedures, corrections, conventions, and lessons selected contextually by the agent |

- Main, distiller, or curator owns semantic decisions. Scripts expose candidates and mechanical safety rather than keyword rules or automatic prompt classification.
- Direct writes to built-in file memory under `<agent-home>/projects/*/memory/` are hard-blocked. `mem sync` only absorbs stray writes from other sessions or harnesses; `mem` is the unified write path.
- The session-end deep curator owns deletion, pruning, consolidation, merge, and graduation through no-tools action JSON plus guarded script execution. The N-turn distiller is add-only. Main performs no housekeeping, and a 21-day working TTL is a deterministic backstop.
- Session injection uses `mem inject --hook`. `core/MEMORY.md §7` and adapter bootstraps own details.

The separation prevents automatic learning noise from burying information the user deliberately chose to retain.

---

## 8. Performance Preservation

Efficiency is not corner cutting. Reduce duplicated orchestrator reasoning, not verification depth.

- Preserve QA rounds selected by the graph, rich role prompts, and required verification.
- Remove repeated orchestrator reading and summarization and the accumulation of large result bodies in context.
- Protect dispatch-depth-0 context first: before route confirmation, main uses compact
  entry metadata and the routing map rather than loading full Skill bodies or
  references. The user receives a completed five-field proposal, which changes
  the task from route generation to error recognition.
- Pass results through files and return only the portable three-line worker
  envelope: artifact path, `PASS|FAIL|BLOCKED`, and a one-line blocker. Changed
  files, commands, logs, findings, and reasoning stay in the artifact; worker
  output is a machine handoff, not a user-facing report.
- At `standard+`, the dispatch-depth-1 owner reads the selected entry contract and
  extends file-only handoff to dispatch-depth-2 stages. Each plan, execute, test, and
  report worker reads only its stage contract and writes a complete artifact
  for the next stage. Main retains route, state, artifact paths, and verdicts;
  the owner remains a thin conductor. If a file cannot carry required context,
  improve the artifact schema instead of passing conversation history.
- Waiting and harvesting are part of the deterministic flow only when the parent runtime owns either a checked session supervisor or an explicitly entered single-ingress gateway. Registered standard+ conductors yield while their supervisor joins the exact child batch outside the model. A managed interactive Codex session stays conversational while its gateway serializes manual input and one bounded typed completion receipt; its control-only sidecar owns neither the upstream connection nor approvals. Parent runtime chooses the wake adapter independently of child runtime. Hooks must not simulate wake by blocking Stop, parking all tools, or creating a synthetic user turn. When atomic idle claim, durable idempotency, or approval routing cannot be proved, `dispatch-wait`/later-turn harvest is a disclosed finite fallback and ambiguous sends fail closed. `OPERATIONS §5.10` owns the runtime details.
- Reduce fixed input before squeezing output: keep always-loaded bootstraps as routers, expose Skill detail progressively, prevent duplicate discovery, and keep ordinary hooks silent. `ADAPTATION §6.1` owns the measurable budgets.
- Worker pruning follows the same rule: one minimal kernel, one worker-type
  fragment, and only the assigned capability/stage contract. Runtime-owned
  project-instruction inheritance is reported separately from harness-controlled
  prompt isolation and is never called physical masking without a verified switch.
- Treat footprint reduction as a static result until paired real-session measurement proves token or cost savings. Never trade intensity, model role, required context, tools, tests, or safety for a smaller counter.

---

## 9. Design Ownership — Design Leads, Code Applies

Visual decisions begin in design rather than emerging ad hoc in code. Design acts as the visual blueprint, and implementation applies it.

- **Tokens are one design-owned contract imported by code.** Color, typography, spacing, radius, and shadow live in exactly one file that the real app imports, such as `app/globals.css` with `@theme` or `styles/tokens.css`. `autopilot-design` decides and edits that file; `autopilot-code` consumes it. Do not keep a token copy under `designs/`, which should contain references, mockups, rationale, and specimens only.
- Components consume tokens rather than redefining them through scattered inline hex or pixel values. A token change routes back to design.
- Built apps remain design-first: render the real running application through the adapter visual harness, make the visual decision, update the token contract, and then apply code.
- Direction, tokens, a new screen layout, or structure are substantial and route through design-first. A tiny adjustment to one element may remain a direct code tweak.

This prevents token drift, fossilized design copies, and loss of visual decision history. The rule came from the 2026-06-08 worklog-board divergence between a stale 7 KB design token copy and the live 50 KB application stylesheet.

## 10. Skill-Design Tenets

The root virtue is **predictability**: reproduce the same process, gates, and procedure in the same situation rather than forcing identical output. Refactoring preserves this behavior skeleton.

Four design levers:

1. **Invocation:** a resident description with no reachability gain wastes
   context, but optimize invocation only after preserving the call graph. Set
   `disable-model-invocation: true` only for workflows that must begin through
   explicit user invocation. Sub-Skills called by parents or preloaded into
   subagents remain model-invoked because the flag blocks programmatic use too.
   Entrypoint routers remain model-invoked: each has a concrete English “Use
   when” trigger and “Not for” boundary, main proposes one of them from compact
   metadata, and the user confirms intent before the Skill owns execution.
   Parent-invoked and model-support Skills remain outside the top-level primary
   candidate set. Consider orthogonal `user-invocable: false` only to hide menu
   exposure.
2. **Information hierarchy:** use three-rung progressive disclosure. Keep only the router, contract, and mapping table in `SKILL.md`; move examples, delegate prompts, templates, and execution detail to one-depth `references/*.md` loaded on demand.
3. **Steering:** start with one leading-concept line and end with checkable completion. Negative safety gates remain valid steering when they protect destructive or irreversible boundaries.
4. **Pruning:** collapse cross-Skill duplication into one source plus pointers. Repeated prose drifts and sediments; pointers retain filename, timing, and obligation.

Quantitative limits for line count, reference depth, and frontmatter live only in `CONVENTIONS §5.6a`.
Cross-adapter completion and active-context budgets live only in `ADAPTATION §2.0` and `§6.1`.

## Appendix — Decision History

| Section | Incident or decision |
|---|---|
| §1 | Early autopilot orchestration mixed file reading with state control; it was separated into a state machine |
| §2 | `782ccf6` made refinement auto-apply by default; `2058325` made user pause opt-in only after 2026-05-21 feedback |
| §3 | `444616a` added cwd-default analysis, `d8f42cd` removed `--format-ref`, and `215fc23` cleaned legacy behavior |
| §4 | Early output conventions moved into `CONVENTIONS §5`; `56708c4` added minor/major tracking and dual-perspective audit |
| §5 | `bf8d565` rejected pasting a rebuttal table into paper prose after the ICML camera-ready incident; the four-step cohesion check followed |
| §6 | `3f5a48c` created the translation role; `cfb0e12` renamed and expanded it into editorial ownership |
| §7 | `60f141a` created the notes flow, now `/post-it`, separating explicit retention from agent learning |
| §8 | A 2026-07-06 single-owner design was reversed on 2026-07-10 into stage dispatch with file-only dispatch-depth-2 handoff |
| §10 | The 2026-07-13 Skill-design refactor placed Pocock's four levers plus predictability here and scan-ready quantitative rules in `CONVENTIONS §5.6a` |
