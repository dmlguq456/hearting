# Capability: autopilot-ship

This is the portable capability contract for `autopilot-ship`. It defines runtime-neutral meaning and adapter obligations. It is not a Claude Skill file.

## Contract
<!-- GENERATED: harness-manifest.json -->

| Field | Value |
|---|---|
| Identifier | `autopilot-ship` |
| Group | `entry` |
| Supported modes | `default, package` |
| Portable meaning | Prepare deployment/release setup or package existing artifacts. |
| Argument shape | `<task description (optional)> [--mode default\|package] [--intensity direct\|quick\|standard\|strong\|thorough\|adversarial]` |
| Execution topology | `transactional-owner`; registry `capabilities/topologies.json` |
| Entry load phase | `post-approval`; owner contract `capabilities/autopilot-ship.md` |

## Invocation Semantics

Two delivery purposes share this entrypoint. Omitted mode and `--mode default`
retain the existing deployment/release path. `--mode package` collects existing
artifacts into a delivery archive and uses its own minimal recipe below; it
does not enter the deployment procedure.

Default mode is an application deployment-setup entrypoint for projects with an existing `spec/`
and substantially complete functionality. Guide the first ship setup,
environment, domain, and migration deployment; select hosting (Vercel, Fly,
Railway, Cloudflare, or EAS); create CI/CD files, `.env.example`, domain guidance,
and a deployment record. The route card obtains approval for the selected
`deploy` part at start. Within that approved scope, the owner may run deployment
and verification; otherwise leave `deploy` out for a later compose. Keep it distinct from autopilot-spec's initial
spec/skeleton work. It may be rerun for environment changes, added domains, or
production migration deployment.

Package mode reuses the existing delivery template, explicitly named model
versions, requested input/output list, and existing reports. Collect only that
scope, check file presence, relative links, version consistency and archive
contents, then provide the ZIP or requested equivalent. An application spec,
hosting choice, CI/CD setup, deployment, installation, security/release review,
training/evaluation, or report rewrite is not implied by packaging.

Reuse sufficient results first. If sample outputs are missing, the necessary
inference belongs to lab/eval and stays limited to the requested samples;
do not silently expand to earlier datasets, alternative/old models, or full
benchmark runs. A request for received field samples plus one or two simulations
does not authorize raw59, A-weight96, IPC, or historical-model sweeps. A later
approval to finish a 167-item batch is local to that batch, not a new default.
Reusable packaging-program implementation belongs to code; new experiment
analysis/reporting to lab; independent document drafting to draft.

Adapters may expose this capability through native commands, skill files, prompt instructions, or explicit wrappers. The adapter must report unsupported runtime mechanics instead of silently treating another runtime's native file format as portable.

## Artifact Ownership

The existing start choice carries `complete` or `report`. Report ends after
both security and release preparation reviews, including their required merge
evidence, and never starts deploy or post-deploy verification. Complete follows
the shown deployment scope and existing safety restrictions. Older sealed
routes retain their original gate and continuation.

Artifact root: `core/CONVENTIONS.md §5.1`; output placement: `§5`.

## Artifact Producer Lifecycle

W7C write-cutover contract (`utilities/artifact_producer.py`, registry table
`producer_lifecycle` in `capabilities/topologies.json`). The same lifecycle
binds `direct`, `quick`, and `standard+`; only the acting owner differs.

1. The launcher prepares the route's cycle and carries its exact context.
2. **write only inside the open cycle.** Every durable artifact uses the
   selected node's declared outputs under `AGENT_ARTIFACT_OUTPUT_DIR`.
   `artifact_producer.py` owns cycle output paths and shared revisions.
3. The runtime carries the same producer context through stage dispatch.
4. **runtime-owned closure.** A new registered owner returns its final report;
   the shared completion controller owns workflow/route closure and exact-cycle
   sealing after PASS and process cleanup. Interrupted closure retains the
   result and transaction, retries without a model turn, and carries a recovery
   notice. `roles/worker-types/owner.md` defines this shared contract.
5. **shared admission.** This capability's output is cycle-local; it is never admitted to `shared/` (only `spec`, `analysis`, and explicitly promoted `research` are shared kinds).

## Role Requirements

Use portable role names from `roles/README.md` and `core/CONVENTIONS.md`. Concrete model names, subagent frontmatter, and runtime-specific tool lists belong in adapter files.

Pipeline intensity follows `core/CONVENTIONS.md §1`: `direct` has no plan stage or durable plan artifact; `quick` is one registered-headless dispatch-depth-1 one-shot conductor with its inline micro-plan plus plan-check-lite; `standard+` uses the capability's durable work-cycle plan when applicable. This recipe has no separate `plan-check` node (only `autopilot-code` declares one): `quick` checks its micro-plan inline (plan-check-lite), and `standard+` reviews through the recipe's own review stages — `security-review`, `release-review`, and `post-deploy-verify` — rather than after every stage. Verification rigor for those reviews and final verify is derived from intensity; it does not name a model or introduce a separate stage graph. `capabilities/topologies.json` (recipe plus `part_catalog`) is the one stage list; `capability-route.py stages --capability autopilot-ship` prints it.

## Stage Graph and Start Approval

### Package

Package uses the existing owner behavior and `ship-setup` completion contract:
the verified delivery archive is its output. `direct` performs the bounded
work inline; `quick` uses one one-shot owner; `standard+` uses one terminal
`package` owner node. The owner performs the plan/check and file/link/version/
archive verification appropriate to the selected intensity inside that work.
Higher intensity preserves rigor and owner profile without adding deployment
or unrelated review stages. No new approval, completion gate, manifest, or
proof file is required. Existing route/producer completion remains unchanged.

The package recipe has no `security-review`, `release-review`, `deploy`,
`post-deploy-verify`, `eval-run`, or human-gate node. Select it by mode before
recipe compilation; pruning the default deployment graph is not the package
implementation. Outputs use the existing `release-config/` artifact bucket.
Reuse the existing delivery layout and include a brief content/version note
only when needed to make the archive usable. Verify the actual archive entries
and that copied relative links resolve within the delivery; do not change or
re-seal published source reports, experiment records, or previous archives.

### Default deployment/release

The default `standard+` graph is `release-setup → {security-review, release-review} →
deploy → post-deploy-verify`. Readiness reviews continue inline after their
existing dependencies pass. The part catalog declares `start_approval: deploy`
on `autopilot-ship:deploy`; the route card obtains that approval before this
route starts. If deployment was not approved or is outside the selected route,
leave `deploy` out and compose it separately after approval. When included,
`deploy` records the authorized deployment, and `post-deploy-verify` is the
terminal node — the workflow is complete only after verification.

At `adversarial` the `security-review` group realizes a third
`failure-mode-check` leg with `leg_class: auxiliary`. Its arbiter is the
**owner**, not the group's anchor — the anchor runs concurrently with it. After
the group joins, the owner puts `auxiliary_findings_considered` in the merge
record's frontmatter with exactly one entry per realized auxiliary leg (adopted
or rejected, with the reason) and registers it with
`capability-route.py arbitrate --group security-review`. `deploy` is a
`capability-owner` node, so it does not pass a wrapper start-gate: here the
enforcement is the route's terminal-gate observation, which carries a failed
`parallel_group:security-review` row and holds `terminal_gate_proven` false
until the record exists. The route-start approval for `deploy` is separate from this arbitration evidence and does not substitute for it. `core/OPERATIONS.md §5.10` owns the
transaction and its typed refusals.

**Open, and owned by the spec, not by this document.** `deploy` is the one node
in the registry that takes an irreversible external action, and the terminal-gate
row is a POST-HOC observation, not a pre-action gate: an unarbitrated
`security-review` leaves `terminal_gate_proven` false and blocks publication
*after* the deploy has already gone out. Nothing reads the `failure-mode-check`
findings before the irreversible step. Closing that needs a gate shape the
current contract does not have — a `capability-owner` node passes no wrapper
start-gate — so it is a spec decision about gate authority, not something to
invent here. Until it is decided, the owner checks the arbitration record itself
before running `deploy`; that is procedure, and this paragraph says plainly that
it is not enforcement.

Older sealed routes may still contain the historical `deploy-authorization` binding. New default compiles use the start approval mark and inline readiness continuations.

### Deployment by the interactive parent

For an already approved deployment that the owner's runtime cannot execute, the
owner prepares the exact deployment command as JSON: `{"argv":["/absolute/executable",
"/absolute/deploy-script"],"cwd":"/absolute/source","timeout_seconds":300}`.
Arguments naming existing files are hashed automatically; additional source or
configuration files the command reads can be listed in `inputs`. The script
retains the project's normal scope, target checks, rollback and private preimage
handling. Do not put secrets in argv or the request.

`hearting run ship-parent-deploy prepare --attempt-id <owner> --command-file <json>`
returns a request bound to the same route and cycle's existing deploy record.
The owner or its actual interactive parent may prepare it. Preparation also
records whether that parent is on the host; a parent preparation before readiness
reviews supplies executor availability, not a deployment or review PASS. Give
the exact request to the existing independent readiness reviews and finish them
within their existing ceilings. While waiting for parent execution the owner
uses its ordinary BLOCKED handoff. If upgrading an older owner or parent runtime, name the
installed utility's absolute path in its existing correction; do not rewrite
the sealed owner environment.

Owner status and parent admission use the common continuation decision. A live
owner hands work back; an answerable BLOCKED/FAIL owner receives the existing
correction with the deployment result. A wait, cancellation or settlement keeps
that decision's existing next action and cannot execute deployment.

The parent runs the returned `run_command` with `--check`
to inspect readiness without mutation, then the same command without `--check`
to execute the exact request once on the host. Current reviews, arbitration,
stop/approval fences and command inputs are checked before spawn. Only that
route's actual parent or its confirmed same-seat successor can execute it;
dispatch owners and workers cannot. No access request is broadened. A running,
failed or interrupted request is never retried automatically; inspect its
retained execution evidence and the project's rollback procedure.

Preparation records the existing review attempts automatically under the normal
registry lock. The reviews that judge the command — those whose verdict `deploy`
reads, reviews holding the same gate (such as a plan's security or release stage)
and their realized parallel legs — authorize execution only when registered
independently after that exact request. Preparing the same normalized command
and input hashes preserves this binding across an owner replacement; changing
them requires fresh command reviews within the existing ceilings. Another review
before `deploy`, such as a functional check, does not judge the command. When
only argv, cwd, timeout or argv-named files changed and every file listed in
`inputs` keeps its hash, its independent PASS remains valid if it was registered
after the earliest preparation that observed those inputs; the request records
that preparation automatically. A changed or empty `inputs` list, or a review
registered before that preparation, needs a fresh review as before, so list the
files a functional check depends on in `inputs`. If `deploy` reads no review
verdict, every review judges the command.
The original marker's independence record is consumed even when the registered
review row was subsequently closed by an owner. This adds no review override,
manual proof input or approval step.

The utility writes the execution result in the existing `deploy-record.json`
and prints one `correction_command`. The parent runs it to deliver the result
through the existing owner input path, obeying its `parent_next` receipt. The
owner completes `deploy` using that record and resumes `post-deploy-verify`.
Completion checks the bound successful execution receipt; the receipt does not
substitute for post-deploy verification or assert service/DB health. Claude,
Codex and OpenCode use this common utility and the same parent authority.

## Guard Requirements

Adapters must preserve the portable invariants relevant to this capability:

- resolve artifact root through `utilities/artifact-root.sh` or equivalent logic;
- use DB memory paths, not runtime-native memory files.

## Adapter Realization

| Adapter | Realization |
|---|---|
| Claude Code | `adapters/claude/skills/autopilot-ship/SKILL.md` and `skills/autopilot-ship/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-ship/SKILL.md`, while `skills/autopilot-ship/SKILL.md` remains a generated compatibility projection sourced from `capabilities/` and kept for parity/drift checks. |
| Codex | Read this spec and run `adapters/codex/bin/preflight.sh capability-info autopilot-ship`. Use `adapters/codex/skills/autopilot-ship/SKILL.md` as the native Codex Skill projection; do not consume `skills/autopilot-ship/SKILL.md` or Claude command files as native Codex configuration. |
| OpenCode | Read this spec and run `adapters/opencode/bin/preflight.sh capability-info autopilot-ship`. Use `adapters/opencode/skills/autopilot-ship/SKILL.md` and `adapters/opencode/commands/autopilot-ship.md` as native OpenCode projections; do not consume `skills/autopilot-ship/SKILL.md` or Claude command files as native OpenCode configuration. |

## Compatibility Reference

`skills/autopilot-ship/SKILL.md` and `adapters/claude/skills/autopilot-ship/SKILL.md` are byte-identical (enforced by `check-adaptation-boundary.sh`'s `diff -qr`); the only difference is the runtime discovery path — Claude Code discovers `adapters/claude/skills/autopilot-ship/SKILL.md`, while `skills/autopilot-ship/SKILL.md` remains a generated compatibility projection sourced from `capabilities/` and kept for parity/drift checks.
