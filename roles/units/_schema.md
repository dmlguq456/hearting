# Unit-Def Schema (canonical authoring contract)

> A **unit** is the single declaration of one dispatchable behavior atom — the SoT that
> replaces both the `roles/modes/*` / `adapters/*/agent-modes/*` persona double-copy and
> the runtime team agents. Units are dispatched as dispatch-depth-2 nodes; they are never native
> team subagents. `family:` is a grouping label only. Spec:
> `.agent_reports/plans/2026-07-21_bootstrap-unit-architecture/architecture-spec-v3.md`.

## File layout

```
roles/units/
  _schema.md            # this contract
  _shared/              # single-source fragments referenced (never restated) by units
    stance.md           # refute-by-default review stance
    triage-output.md    # 🔴🟡🟢 severity output skeleton
    memory-flow.md      # authorized-memory closing wrapper
    dual-io.md          # direct-call vs pipeline return switch
  <family>/<unit>.md    # one unit per file; family ∈ {qa, research, dev, design,
                        #   material, editorial, plan}
  <family>/_NOTES.md    # authoring residue: content mined from legacy sources that
                        #   found no home — must be reviewed, never silently dropped
```

## Frontmatter (YAML; machine contract, parsed at build/route-compile time only — the
## dispatch hot path reads the BODY as a plain .md and stays stdlib-only)

```yaml
---
unit: qa/code-review          # id = <family>/<name>; unique across the catalog
family: qa                    # grouping LABEL (the former "team" name-space); never a runtime agent
role: fast reviewer           # PORTABLE role name only — concrete model resolves via the
                              # per-adapter models.conf; a model literal here is a guard violation
worker_type: review           # owner | stage | review | support | frame → dispatch lifecycle overlay
                              # Changing this vocabulary means walking every location listed in
                              # this unit's fence (see the W1 dev log under
                              # .agent_reports/campaigns/2026-09-10_frame-bootstrap-layer/) in one
                              # shot — a partial landing accepts a tuple in one place and refuses
                              # it in another, which is worse than not starting.
floor: moderate               # near-zero | low | moderate | high | highest — persona-minimization
                              # floor; graded per UNIT (never per family, never per (role,kind))
read_only: true               # the unit's NATURE; the concrete write_scope stays NODE-owned
stance: _shared/stance.md     # ref, or `none` for non-review units
io:
  verdict: [PASS, FAIL, BLOCKED]   # verdict SEMANTICS; each surface renders its own syntax
  return: _shared/dual-io.md       # return-shape ref (or a unit-specific description)
tools: []                     # tool-contract refs — relocated domain LAW (e.g.
                              #   tools/figure-semantic-verify.py); never minimized away
branches: []                  # surviving execution branches (e.g. [direct, pipeline]);
                              # structural pruning requires usage evidence
aliases: {}                   # per-surface name aliases; empty in the end state
---
```

## Body

The irreducible domain persona at the declared floor. English canonical (localization is
a projection concern). Reference `_shared/*` fragments — do not restate them. Merge, per
unit, the three legacy sources: `roles/modes/<f>/<m>.md` (EN), the
`adapters/claude/agent-modes/<f>/<m>.md` divergent copy (KO), and any load-bearing blocks
from the retired team agent file. Nothing load-bearing drops silently → `_NOTES.md`.

## Floor guidance (from the investigation gradient)

- `near-zero`: material/*, qa/test, design/verifier, research/fact-check — kernel +
  worker-type + stance + I/O carry almost everything; keep THICK tool fragments.
- `low`: dev/*, editorial/*, qa/data-curate.
- `moderate`: qa/code-review, qa/plan-review, qa/security-review (confidence bar = 8;
  silence means "no HIGH/MEDIUM found", never "proven safe").
- `high`/`highest`: qa/ml-debug, research/{research-survey,claim-verify,plan-review},
  design/{critic,maker} — the domain core is the value; do not thin it.

## Guard contract

`check-unit-config.py` (fail-closed) forbids: model literals in units; persona/stance
text restated outside the catalog or `_shared/`; a unit declaring a concrete write_scope.
`capability_topology.py` validates every topology node's `unit` ref: unit exists ∧
`node.kind` compatible with `worker_type` ∧ role consistency.
