# Unit-Def Schema (canonical authoring contract)

> A **unit** is the single declaration of one dispatchable behavior atom — the SoT that
> replaces the `roles/modes/*` / `adapters/*/agent-modes/*` persona double-copy. Units run as
> route nodes — a stage at dispatch depth 2, or the one leg of a unit route at depth 1; a sealed
> fallback may run a node on a runtime-native subagent. `family:` is a grouping label only.

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

## Frontmatter (YAML; machine contract, parsed at build/route-compile time — the dispatch hot
## path reads the BODY plus the one flat `bootstrap:` block with the same stdlib regex technique,
## no YAML dependency)

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
field: code                   # OPTIONAL default field (plan|design|code|lab|research|docs|admin|other):
                              # written only where the field is clear (dev/* -> code, qa/ml-debug -> lab);
                              # common or ambiguous units leave it out and `compose --unit` then needs --field.
                              # The one home of this pairing (the `unit` route reads it).
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
bootstrap:                    # OPTIONAL runtime bootstrap material; absent = nothing attached
  memory: [report-format]     #   memory topics (topic table in utilities/worker_bootstrap.py)
  exemplar: deliverable       #   prior completed deliverable of this node's gate
  gates: [research-report]    #   optional node limit; absent = every node of this unit
---
```

## Body

The irreducible domain persona at the declared floor. English canonical (localization is
a projection concern). Reference `_shared/*` fragments — do not restate them. Merge, per
unit, the three legacy sources: `roles/modes/<f>/<m>.md` (EN), the
`adapters/claude/agent-modes/<f>/<m>.md` divergent copy (KO), and any load-bearing blocks
from the retired team agent file. Nothing load-bearing drops silently → `_NOTES.md`.

The `stance:` text is carried by the runtime into the worker prompt, just before the body.

**Two-layer form (pilot: `qa/code-review`, `qa/test`, `dev/backend`, `editorial/report`).**
A unit body may be written as two sections, both always carried with the worker text:

```
## 지킬 것   (every line is a must: ownership boundaries, domain prohibitions, tool contract,
              spec/precondition checks, the output shape and verdict tokens, round rules;
              review units add one line pointing at the carried stance text)
## 참고      (defaults the worker may override: role and tone, procedure, criteria lists,
              examples, memory; its head states the precedence: this turn's instruction >
              project conventions > the person's profile > these defaults)
```

A line the unit cannot do without belongs in 지킬 것; write-scope does not stop a worker from
writing inside its worktree, so a prohibition lives there too. Lines moved out of the worker
text are recorded in `<family>/_NOTES.md`, never deleted. No checker enforces the headings;
units not yet moved keep the single-body form.

## Floor guidance (from the investigation gradient)

- `near-zero`: material/*, qa/test, design/verifier, research/fact-check — kernel +
  worker-type + stance + I/O carry almost everything; keep THICK tool fragments.
- `low`: dev/*, editorial/*, qa/data-curate.
- `moderate`: qa/code-review, qa/plan-review, qa/security-review (confidence bar = 8;
  silence means "no HIGH/MEDIUM found", never "proven safe").
- `high`/`highest`: qa/ml-debug, research/{research-survey,claim-verify,plan-review},
  design/{critic,maker} — the domain core is the value; do not thin it.

## Guard contract

`check-unit-config.py` (fail-closed) validates the `bootstrap` sub-keys and vocabulary and forbids: model literals in units; persona/stance
text restated outside the catalog or `_shared/`; a unit declaring a concrete write_scope.
`capability_topology.py` validates every topology node's `unit` ref: unit exists ∧
`node.kind` compatible with `worker_type` ∧ role consistency.
