# editorial — authoring residue (review required; nothing here may drop silently)

Sources merged: `roles/modes/editorial/{translate,polish,review}.md` (EN),
`adapters/claude/agent-modes/editorial/{translate,polish,review}.md` (KO),
`adapters/claude/agents/editorial-team.md` (team file). Items below found no home in
the units/_voice or were resolved with a judgment call.

1. **Model literals dropped (rule-mandated).**
   `adapters/claude/agents/editorial-team.md:5` (adapter-configured model) and
   `:117-121` (adapter-configured tier defaults). Units carry role NAMES only
   (`deep editor`, `fast reviewer`). Verify the per-adapter models.conf maps these
   roles equivalently before team-file deletion (WS C).

2. **Native-surface config dropped.**
   `editorial-team.md:4` (`tools: Read, Write, Edit, Grep, Glob`), `:6` (`color`),
   `:7` (`memory: project`), `:8-10` (metadata modes/blurb). Surface-owned runtime
   config; node/dispatch config must grant equivalent tool access when these units run
   as dispatch-depth-2 workers (WS B/C).

3. **Session-start reads not placed.**
   `editorial-team.md:103-105`: read the runtime adapter bootstrap (CLAUDE.md) and
   `README.md` at session start. This is dispatch-kernel/worker-bootstrap territory,
   not unit persona; confirm the kernel overlay covers orientation reads. (The
   `mem profile` reading list from `:106` WAS placed → `_voice.md` Knowledge sources.)

4. **Memory-upsert hazard is surface-specific.**
   `editorial-team.md:114`: recommended channel `mem profile-append
   02_paper_writing_style`; never pass a partial profile body to raw
   `mem add ... --source user-profile:02_paper_writing_style` because the source-keyed
   upsert would REPLACE the full profile body. `_voice.md` keeps only the portable form
   (one candidate in summary; caller records via authorized memory flow /
   `_shared/memory-flow.md`). RESOLVED 2026-07-22: the concrete hazard was promoted to
   `core/MEMORY.md §7.6` ("Source-keyed upsert hazard") — that section is now the
   durable home; this item remains as provenance only.

5. **KO/EN divergence — polish gate rigor condition (resolved to EN).**
   KO `agent-modes/editorial/polish.md:15` enumerates literal flags (`--qa quick/light`
   skip; `standard/thorough/adversarial` invoke); EN `roles/modes/editorial/polish.md:14`
   says "the selected graph's derived rigor is standard or higher". Resolved to the EN
   derived-rigor form (portable, most recent); the flag-literal enumeration dropped.
   Entry-skill recipes must encode this gate at compose time (WS B).

6. **Catch-net ownership pointer generalized.**
   KO `agent-modes/editorial/polish.md:36` cites `skills/draft-strategy/SKILL.md`
   sections ("Paragraph Cohesion Pre-Check", "Natural-integration rule for paper-body
   mutations", "Tone Auto-Detection") as the author-stage owner of cohesion/tone.
   `_voice.md` keeps the ownership statement abstractly ("drafting/strategy stage");
   the concrete skill-path/section references were dropped for path stability. Review
   whether an explicit cross-ref should return once draft-strategy's post-migration
   home is fixed.

7. **KO/EN wording — review report destination (resolved).**
   KO `agent-modes/editorial/review.md:12` "보고서는 … 메모리에만 남긴다" vs EN
   `roles/modes/editorial/review.md:13` "return it in-memory as requested". Interpreted
   as "return inline instead of writing a file" (no memory write implied); the unit says
   "return it inline when the caller asks".

8. **Verdict tokens are NEW.**
   No legacy source defined machine verdicts. `DONE/BLOCKED` (translate, polish) and
   `CLEAN/FINDINGS/BLOCKED` (review) were introduced to satisfy the schema's
   `io.verdict`. WS B must wire these exact tokens into recipe/validator expectations.

9. **Team-file routing triggers now entry-skill duty.**
   `editorial-team.md:3` description carried routing triggers ("final passes on
   autopilot-draft, autopilot-research, code-report, audit reports, draft-strategy,
   user-facing plan mirrors"). Surfaces are preserved as scope guidance in `_voice.md`;
   the ROUTING behavior (when a pipeline appends an editorial node) must be realized by
   entry recipes (WS B) — units themselves never route.
