# autopilot-ship

Select the purpose first: `--mode package` prepares an archive from existing
artifacts using the package procedure below. Omitted mode or `--mode default`
keeps the deployment/release procedure and its `spec/ship.md` record. Package
does not require a hosting/app spec or run the deployment steps.

## Package procedure

Reuse the existing delivery template and the request's named model versions,
input/sample list, expected outputs, and existing reports. Keep this exact
scope: received field samples plus one or two simulations does not include
raw59, A-weight96, IPC, or historical models merely because they exist. A later
approval of a larger batch is specific to that batch, not a reusable default.

Collect the existing files into the requested layout. Reuse finalized results;
only missing requested sample inference goes to lab/eval, without a full
training/evaluation loop or new report. If packaging requires reusable program
development, route that implementation to code. Independent document creation
belongs to draft; interpretation of experiment results stays lab/eval.

Check that the named versions and requested files match, copied relative links
resolve within the delivery, and the ZIP (or requested equivalent) contains the
intended files. Use the existing `release-config/` artifact bucket, preserve
source reports and experiment/route/manifest state, and return the actual
archive path with any missing inputs or known limitations. A content/version
note may reuse the template; do not require a new manifest or proof file.

Select the package recipe at the requested intensity: direct inline, quick
one-shot owner, or standard+ single `package` owner. Apply the existing
plan/check and verification rigor within that work. Stop when the verified
archive is ready; do not continue into the deployment procedure below, invoke
security/release reviews, install, deploy, or expand the sample/model list.

## Default deployment/release procedure

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

The existing start choice carries complete or report for the shown scope. Report ends after both preparation reviews and their required merge evidence; complete may execute deployment and verify only within the approved system and action scope. DNS changes, billing, secret entry, and production database migrations remain outside scope unless the approval names those systems and actions.

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
5. **Run deployment only when the selected route includes the start-approved `deploy` part and the approved scope names the system and action.** Otherwise provide the deployment commands for a later compose. Preserve provider-specific commands such as:

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

### Step 5: Verify and Record

Review the prepared record and its decisions with the user. They may Continue, Revise (write `_internal/refine_v{N}.md`), Back-jump (rerun the chosen prior step), or Stop (preserve current state). This review does not ask for a second approval of the `deploy` part.

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

The launcher supplies the exact cycle context. Each ship stage uses its sealed
node outputs and write scope; deployment targets and external bundle roots keep
their existing authority. The runtime owns route closure and cycle sealing.
