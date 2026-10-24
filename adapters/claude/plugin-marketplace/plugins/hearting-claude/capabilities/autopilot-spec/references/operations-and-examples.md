## Deployment Setup — autopilot-ship

The separate [`autopilot-ship`](../../autopilot-ship.md) skill owns initial shipping setup, environment changes, domains, and migration deployment. This follows separation by the nature of the work ([CONVENTIONS §6.3](../../../core/CONVENTIONS.md)): autopilot-spec covers initial design, requirements, structure, and skeletons; autopilot-ship covers final deployment and later deployment updates.

Invocation paths:

- For deployment setup, environment changes, domains, or migrations, invoke `/autopilot-ship <task>` directly.
- The main agent should also route natural-language requests such as "배포 셋업", "Vercel", "env 변경", or "도메인" to autopilot-ship. These are examples, not fixed trigger phrases.

## Forbidden Without an Explicit Request

- Running real deployment commands such as `vercel deploy` or `fly deploy`; provide instructions only
- Registering payment information or credit cards
- Changing DNS directly
- Purchasing a domain
- Entering real environment-variable values; the user does this in the provider dashboard
- Automatically running a production database migration

## CONFIRM Gate Branches

Render these choices naturally in the user's communication language.

| Response intent | Handling |
|---|---|
| **Proceed** | Continue to the next step or finish |
| **Modify** | Refine the current step as v2, backing up to `_internal/refine_v{N}.md` |
| **Back-jump** | Rerun an earlier step and invalidate downstream work |
| **Stop** | Stop and preserve `pipeline_state.yaml` |

If the response is ambiguous, show the choices again instead of guessing.

## Pipeline State

`spec/pipeline_state.yaml`:

```yaml
project_name: <name>
created: <date>
mode: [library, cli, research]   # or one mode, such as [app]
(app mode only) stack:
  framework: <chosen>
  db: <chosen>
phases:
  spec: done                    # PRD complete
  (app mode only) scaffolding: done
  (app mode only) skeleton: done
  design: pending               # set to done by autopilot-design
  dev: in_progress              # accumulated by autopilot-code
  (app mode only) ship_setup: pending
last_updated: <timestamp>
```

autopilot-code stores work artifacts in the sibling bucket `plans/<date>_<slug>/`, not under `spec/`; this keeps blueprints separate from execution records.

## Return Format

```
<artifact-root>/spec/ -- ✅ spec completed (mode: <list>)
```

Suggest the next step in the user's communication language:

- Spec complete → autopilot-design for visual work, or autopilot-code for implementation and cleanup
- App setup complete → subsequent pushes can deploy automatically

## Memory Considerations

Memory is an agent judgment, not a fixed extraction rule. If a detail is durable and useful across projects, the agent may consider patterns such as frequently combined modes, improved inference evidence, stack/language preferences, or recurring shipping pitfalls. Do not store these mechanically.

## Examples

The Korean request strings below are quoted multilingual examples. They are not fixed routing signals.

### Example 1 — Household-management app (single `app` mode)

```
/autopilot-spec "할 일 + 가계부 가사관리 웹 앱"
→ auto mode infers app
→ PRD: features, scenarios, API Contract, data model, UI flow
→ Stack: Next.js + Prisma + Turso
→ Scaffolding + skeleton
→ spec/가사관리/prd.md
```

### Example 2 — Organize TF-Restormer research code (`research` + `cli`)

```
/autopilot-spec "TF-Restormer 정돈·재현성 준비"
→ auto mode infers research + cli from configs/, argparse, and ipynb signals
→ Common PRD: module structure, dependencies, license
→ PRD [research]: entry points, configs, reproduction commands, expected metrics
→ PRD [cli]: train.py / eval.py commands and options
→ spec/TF-Restormer/prd.md (no code generation; autopilot-code organizes code from this spec)
```

### Example 3 — npm library (`library` + `cli`)

```
/autopilot-spec "audio-utils — Node 라이브러리 + CLI 도구"
→ auto mode infers library + cli
→ PRD [library]: public API (loadAudio / saveAudio / ...), examples, semver
→ PRD [cli]: au-tool commands and options
→ Phase 0: choose reference source → Phase 1: clone a GitHub repository → Phase 2: create src/{io,core,utils}/ export skeleton + CLI entry (skip Phase 1.5; code-only reference)
→ spec/audio-utils/prd.md + scaffolded skeleton under spec/audio-utils/
```

### Example 4 — ASR Conformer baseline (`research` + `cli`, Phase 1.5 checkpoint verification)

```
/autopilot-spec "Conformer ASR baseline — fine-tuning 시작 자리"
→ auto mode infers research + cli from configs, argparse, and metric signals
→ Step 1: collect inputs — cite code_resources under research/asr-conformer/ (espnet, transformers). No similar_models exist in this empty codebase, so prefer an external reference.
   "위 자료들로 진행?" → proceed
→ Step 2: confirm mode + stack (PyTorch + Lightning)
→ Step 3a: confirm PRD [research] entry points, configs, and metrics (WER / CER)
→ Step 3b: confirm Component diagram (data → encoder → decoder → metric)
→ Step 3c: confirm PRD [cli] train / eval / serve commands
→ Step 4 Phase 0: confirm reference source — espnet asr1 recipe + Hugging Face Transformers Conformer checkpoint
→ Step 4 Phase 1: git clone espnet + huggingface-cli download nvidia/conformer-asr-small → /tmp/asr_ref
→ Step 4 Phase 1.5: verify the checkpoint before use
   inference command: use the accumulated Quick verify command from research/asr-conformer/07_resources
   result: ✅ one sample WAV produces valid text → proceed to Phase 2
→ Step 4 Phase 2: `dev/new-lib` unit
   - model/Conformer/ (preferred layers only: MHSA / Conv / FFN)
   - train.py / eval.py / config.yaml skeleton (Lightning)
   - cli.py (Typer subcommands: train / eval / inference)
→ Step 4 Phase 3: confirm the result
→ Step 5: spec complete → next: /autopilot-lab "ASR fine-tuning Common Voice ko"
```
