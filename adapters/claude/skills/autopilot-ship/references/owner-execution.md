# autopilot-ship

Prepare application deployment and release setup. Accumulate the current shipping contract in `<artifact-root>/spec/ship.md` under the three-tier output convention (`<agent-home>/core/CONVENTIONS.md#5-skill-output-convention--t1t2t3`).

## Purpose

Keep product definition, implementation, and shipping concerns separate:

| Capability | Timing | Responsibility | Typical user decisions |
|---|---|---|---|
| `autopilot-spec` | Initial definition | Decide what to build and establish the blueprint | Stack, data, API, and product boundaries |
| `autopilot-code` | Iterative implementation | Make the product work | Usually implementation outcomes |
| `autopilot-ship` | First release and later operational changes | Decide where and how to deploy | Hosting, DNS, environment, and migration choices |

```text
autopilot-spec --mode app
  → optional autopilot-design
  → repeated autopilot-code
  → autopilot-ship for initial setup
  ↻ autopilot-ship for environment, domain, or migration updates
```

This capability prepares files, records decisions, and explains commands. The user executes production deployment, DNS, billing, secret entry, and production database migration unless they explicitly place those systems and actions in scope under an applicable approval contract.

## Invocation

Route release setup, environment-key changes, domain connection, and production migration guidance here. An explicit `autopilot-ship` invocation supplies the routing choice directly.

- Derive verification rigor from `--intensity`; there is no separate `--qa` axis. Use the canonical mapping (`<agent-home>/core/CONVENTIONS.md#11-verification-rigor-tiers`).
- Prefer standard rigor for hosting or CI/CD configuration unless scope and risk justify another tier.
- Infer initial setup versus a focused update from the request and existing ship record.

## Context Detection

Inspect `<artifact-root>/spec/pipeline_state.yaml` and `spec/ship.md`:

| State | Action |
|---|---|
| `ship.md` absent | Prepare the initial hosting, CI/CD, environment-key, and optional domain setup |
| `ship.md` present | Classify the request as an environment, domain, migration, or other focused update and touch only that area |

## Language Rule

Follow an explicit artifact or audience language when provided. Otherwise, write `ship.md`, prompts, and user-facing guidance in the conversation language according to `<agent-home>/roles/response-policy.md`. Preserve commands, environment-key names, paths, DNS record values, and provider identifiers.

## Procedure

### Step 1: Inspect the Current State

Perform these checks read-only:

- Validate framework and database information in `spec/pipeline_state.yaml`.
- Run `git remote -v` to inspect remote configuration.
- Look for `vercel.json`, `.github/workflows/`, and `.env.example`.
- Run `git status` to inspect working-tree state.

### Step 1.5: Recommend Pre-Deployment Gates

Before a first production deployment, recommend the applicable checks unless the user explicitly skips them:

- **Security**: when changes touch authentication, secrets, external input, or database migration, dispatch the `qa/security-review` unit against the change surface. Report new high-confidence findings scored at least 8 and recommend holding deployment while critical findings remain.
- **Runtime behavior**: dispatch the `qa/test` unit for Level 5b runtime observation against the deployable surface and capture evidence. Recommend holding deployment when it fails.

These gates advise; they do not execute production deployment. Record the evidence and any skipped gate in `ship.md`.

### Step 2: Select the Shipping Path

If the request remains materially ambiguous, ask the user to choose. Otherwise infer one path:

| Intent | Path |
|---|---|
| Initial deployment setup or a named hosting target | Full initial setup |
| Environment-variable or secret-key change | Environment update only |
| Domain or DNS connection | Domain update only |
| Production migration | Migration guidance only, including destructive-risk and rollback notes |

### Step 3: Prepare the Initial Setup

1. **Select hosting with the user.** Base the recommendation on the actual stack, runtime needs, operational constraints, and current provider documentation. Common candidates include Vercel, Cloudflare Pages, Fly.io, Railway, and EAS Build; do not treat this list as a timeless compatibility table.
2. **Create or update `.env.example`.** Include keys only, never secret values. Tell the user where the real values must be entered.
3. **Prepare CI/CD when needed.** Create `.github/workflows/deploy.yml` only after the chosen deployment model requires an explicit workflow. Do not duplicate a provider-managed Git integration.
4. **Describe optional domain setup.** Give the required DNS records; the user changes the registrar or DNS dashboard.
5. **Provide the deployment commands without executing them.** Preserve provider-specific commands such as:

   ```text
   vercel login
   vercel link
   vercel deploy --prod
   ```

6. **Write `spec/ship.md` using this schema:**

   ```markdown
   ---
   changelog:
     - date: <YYYY-MM-DD>
       type: initial
       notes: "<hosting, CI/CD, environment keys, and domain summary>"
   ---

   # Ship Record — <name>

   - Hosting: <provider>
   - Database host: <provider>
   - Domain: <domain, if any>
   - Environment keys: VAR_1 / VAR_2 / ...
   - Deploy command: <command>
   - CI/CD: <workflow path or provider-managed integration>

   ## Notes
   <Operational constraints, external services, and future checks>

   ## Change History
   - <YYYY-MM-DD>: initial setup
   ```

### Step 4: Apply a Focused Update

Keep `ship.md` as one current-state file. On every update, append to frontmatter `changelog:` and `## Change History`; do not create refinement snapshots. Git history is the source for previous states.

| Path | Work | `ship.md` update |
|---|---|---|
| Environment | Add keys to `.env.example` and explain dashboard entry | Refresh environment keys; append `{type: env, notes: "VAR_X added"}` |
| Domain | Explain the required DNS records | Refresh Domain; append `{type: domain}` |
| Migration | Explain destructive risk, rollout, and rollback; leave commands such as `prisma migrate deploy` to the user | Record migration evidence in Notes; append `{type: migration}` |

### Step 5: Confirmation Gate

Present the target, selected path, and 3–5 major decisions. Accept Continue, Revise, Back-jump, or Stop in the conversation language. A revision writes `_internal/refine_v{N}.md`; a back-jump reruns the chosen prior step; Stop preserves current state.

## Forbidden Without Explicit Scope

- executing `vercel deploy`, `fly deploy`, or another production deployment command
- entering payment or credit-card information
- changing DNS directly or purchasing a domain
- entering real environment-variable values
- executing a production database migration

## Return Format

Report the `spec/ship.md` path, selected shipping path, gate evidence, and the next user-owned action. Keep a success-only report concise.

Read [references/examples.md](examples.md) for initial-setup, environment-update, and domain-connection examples.

## Task

$ARGUMENTS

## Artifact Producer Lifecycle (W7C)

Owner-executed, same at every intensity (`direct` inline; `quick`/`standard+`
by the dispatch-depth-1 owner). Full contract: `capabilities/autopilot-ship.md`
§Artifact Producer Lifecycle and `producer_lifecycle` in
`capabilities/topologies.json`.

1. After the route is compiled and bound, and before the first durable
   artifact: `python3 <agent-home>/utilities/artifact_producer.py begin
   --artifact-root <root> --route <route file> --capability autopilot-ship
   --intensity <intensity> --env-file <env>`; export the returned
   `AGENT_ARTIFACT_*` variables. `legacy-compat` means the cutover is inactive
   and the legacy `release-config/` layout is still the write target.
2. Write every artifact under `$AGENT_ARTIFACT_OUTPUT_DIR/release-config/...`; never
   write to a legacy top-level bucket while the cutover is active, never write
   under `shared/`.
3. Pass the exported `AGENT_ARTIFACT_*` variables to every stage dispatch
   (the adapters forward them); stage workers call `begin --node <id>` and
   join the same cycle.
4. Close the route, then `artifact_producer.py finalize --artifact-root <root>
   --cycle $AGENT_ARTIFACT_CYCLE_ID`; on `recovery-required`, run
   `artifact_producer.py recover` and retry.
5. Only then, if this capability owns a shared kind, `admit-shared`.
