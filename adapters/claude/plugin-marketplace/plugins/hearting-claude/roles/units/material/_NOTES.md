# material — Authoring Residue & Merge Decisions (for review)

Sources merged per unit: `roles/modes/material/<mode>.md` (EN),
`adapters/claude/agent-modes/material/<mode>.md` (KO),
`adapters/claude/agents/material-team.md` (team router).

## Divergences resolved (verify the resolution)

1. **Spectrogram window sizes — EN and KO contradict.**
   EN `roles/modes/material/figure-gen.md:26` says "512 samples at 48 kHz, 400 at
   16 kHz, and 256 at 8 kHz"; KO `adapters/claude/agent-modes/material/figure-gen.md:30-33`
   says 8 kHz→256, 16 kHz→512, 48 kHz→1024. The KO values predate the EN file (EN
   values first appear in the 2026-07-14 English-migration commit `4e65ef3d`) and are
   internally consistent (window scales with rate); the EN triple looks like a
   migration transcription error. **Resolved to KO (256/512/1024)** in
   `roles/units/material/figure-gen.md`. USER-CONFIRMED 2026-07-22
   (8 kHz→256, 16 kHz→512, 48 kHz→1024); `tools/figure-semantic-verify.py`
   now enforces the law when a manifest figure records an `stft` block.
2. **data-script metric table rows dropped by the EN migration.** KO
   `agent-modes/material/data-script.md:26-35` has three domain rows absent from EN
   `roles/modes/material/data-script.md:25-31` (Bandwidth extension LSD/NISQA;
   Continuous speech separation WER LibriCSS 0S/0L/10/20/30/40; ASR robustness WER
   CHiME-4 dt/et/sim/real). **Union restored** in the unit.
3. **figure-gen manifest audit fields.** KO `agent-modes/material/figure-gen.md:56-58`
   requires recording PNG SHA-256, reviewer/tool, timestamp, and verdict in the
   manifest and re-review on PNG change; EN omits this. **Adopted (stricter)** in the
   unit.
4. **Combined-PPTX wording variance.** pdf-extract/figure-gen sources allow "one
   combined PPTX when needed" produced by the unit; web-image-search KO
   (`agent-modes/material/web-image-search.md:52`) assigns the combined PPTX to the
   caller's batch utility. Kept per-unit as sourced; harmonize if desired.

## Team-file content re-homed elsewhere (not in any unit — needs a WS-B/C home)

5. **Mode-selection trigger table** (`adapters/claude/agents/material-team.md:24-32`):
   routing content. Unit purpose lines carry the semantics; the literal trigger
   phrasing should inform the entry-skill recipe/compose matching for the material
   family.
6. **Scope Boundary out-of-scope routing table** (`material-team.md:44-52`): the
   cross-family redirect list (refactor→dev, new-lib→dev, training→autopilot-code,
   wording→editorial, UI visuals→design maker, ml-debug→qa, data hygiene→qa
   data-curate). Unit-relevant slices are embedded in `figure-gen.md` and
   `data-script.md` (Scope Boundary sections); the full table is router doctrine for
   the entry skill/topologies.
7. **Cross-project profile preload block** (`material-team.md:54-63`): the team ran
   all four `mem profile` reads (01_paper_figure_style, 03_presentation_strategy,
   04_analysis_methodology, 05_domain_expertise) at start of ANY material work.
   Distributed: 01/03/05 → figure-gen; 04/05 → data-script. The three collector units
   (browser-fetch, pdf-extract, web-image-search) now read none — review whether
   pdf-extract should consume 01 (figure-style expectations for crop quality).
8. **"Use one mode per invocation"** (`material-team.md:81`): router-surface rule;
   under the unit model each dispatch node IS one unit, so it holds by construction.
   No unit text needed unless compose-on-demand can bundle.

## Deliberately dropped (confirm)

9. **Model literals** (`material-team.md:5` adapter-configured model; `:67-69`
   adapter-configured tier defaults): role NAMES carried into `role:` frontmatter (fast tool
   worker / deep maker); concrete models resolve via per-adapter models.conf. The
   team file's "deep maker **or reviewer**" for data-script was resolved to `deep
   maker` (the unit's stance is maker; sanity checks are not adversarial review).
10. **Native-team runtime frontmatter** (`material-team.md:4-7` tools list, color,
    `memory: project`) and **invocation examples** (`material-team.md:90-106`,
    `Agent(자료팀, ...)` syntax): native-agent surface config, retired with the team;
    tool needs are now node-owned. Example payloads are redundant with unit bodies.
11. **Merge-history note** (`material-team.md:15`, "merged the former analysis and
    exploration teams on 2026-05-25") and the **Language Rule pointer**
    (`material-team.md:17-20`, response-policy): history and surface-owned policy;
    not unit content.
