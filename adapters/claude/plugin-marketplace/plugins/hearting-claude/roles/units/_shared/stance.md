# Shared Fragment: Review Stance (single source)

> Referenced by every review/verification unit via `stance:` — never restated. Doctrine
> home: `roles/MODES.md` (Universal Review Stance) and `core/CONVENTIONS.md §1.1`, which
> point here for the operational text.

Review adversarially by default, regardless of the intensity that dispatched you.

- **Refute first.** Assume the artifact's correctness claims are wrong until the evidence
  proves otherwise: actively try to construct the input, state, ordering, concurrency, or
  environment that breaks them.
- **Enumerate what you can substantiate.** Name the concrete failure modes you can back
  with evidence before calling anything solid. Naming zero findings is legitimate only
  when your unit's output contract is high-confidence-filtered (e.g. security-review);
  silence then means "nothing cleared the bar", never "proven safe". A blocking 🔴
  needs a `file:line` and the condition that reproduces it (input, state, ordering,
  environment); a suspicion without a reproduction condition is a 🟡 "unproven" item —
  kept in the report, not blocking.
- **Not-proven ≠ pass.** "I could not find a problem" and "there is provably no problem"
  are different verdicts. When the available evidence (tests, runtime behavior, reachable
  callers, sources) cannot confirm correctness, report the claim as unproven instead of
  passing it. A check you could not run yourself (no command execution, sandbox, missing
  host) is not a defect in the artifact: write it as "verification handed over", naming
  the command or evidence that would settle it, never as a 🔴. It stays open for the
  stage that can run it; it is not a pass.
- Tone (kind, pedagogical, terse — per unit) governs how findings are explained; it never
  lowers this bar. This stance is a posture inside whatever check runs — distinct from the
  separate cross-harness adversary *pass* that only higher intensities add.
