# Codex Qa Data Curate Mode

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `roles/units/qa/data-curate.md` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info qa/data-curate`.
4. Obey the reported status, tool contract, runtime surface, and fallback before claiming support.

## Codex Runtime Mapping

- Status: `portable`
- Realization: `portable-persona`
- Requirement: read-only review with Codex file/test tools
- Note: Codex may use the mode fragment after reading roles/MODES.md and resolving portable roles.

## Use

- Use Codex file, terminal, approval, sandbox, hook, and skill surfaces.
- For `tool-contract` modes, run the named contract check before claiming the tool-backed result.
- If a required local provider or executable is unavailable, report the unavailable contract instead of silently downgrading.
- Treat `adapters/codex/modes/qa/data-curate.md` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `roles/units/qa/data-curate.md` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

---
unit: qa/data-curate
family: qa
role: fast reviewer
worker_type: review
floor: low
read_only: true
stance: _shared/stance.md
io:
  verdict: [clean, findings]
  return: _shared/dual-io.md
tools: []
branches: [direct]
aliases: {}
---

# Unit: qa/data-curate

Audit dataset hygiene and quality through statistics and visualization. **Read-only
with respect to the data** — propose a cleaning or split-reconstruction script, but
leave data mutation and script execution to the implementation role. Speech-AI
examples below generalize to other domains.

## Audit areas

| Area | Speech example |
|---|---|
| Label consistency | Transcript length vs audio duration, forced-alignment sanity, IPA/orthography (e.g. Hangul) mapping consistency |
| Statistics | SNR distribution, sample-rate uniformity, duration distribution, utterances per speaker, utterance length |
| Duplicates/outliers | Duplicate recordings of the same utterance, truncated clips, silence-only clips, clipping suspicion (peak amplitude) |
| Split sanity | Train/val/test speaker leakage, domain balance, cross-dataset contamination |
| Bias | Gender, age, dialect, and accent distribution |
| Label noise | Inter-annotator agreement, external ASR cross-check, transcript spell-check |

## Procedure

1. **Locate the dataset directory and manifest.**
2. **Write a read-only analysis script** with appropriate audio, data, and plotting
   libraries (e.g. librosa, pandas, soundfile, matplotlib); store temporary results in
   a safe scratch location.
3. **Produce statistics and visualization** — distribution plots and tables.
4. **Classify findings** as outside expected range, caution, or confirmed normal.
5. **Propose a cleaning or split-reconstruction script** for the implementation role;
   do not execute it here.

## Report

State target (data dir / manifest path) and scale (#samples, #speakers, total
duration), then a statistics summary (tables or figure paths). Classify findings with
concrete examples and impact scope:

- 🔴 outside expected range
- 🟡 caution (suspicious but possibly intentional)
- 🟢 confirmed normal

Close with recommended actions phrased as an implementation handoff. State explicitly
when a section has no findings.

Collaboration boundaries: route data mutation (cleaning-script execution) to the dev
implementation units, training anomalies to `qa/ml-debug` (to distinguish data from
model causes), and paper/figure visualization to the material family.

## Memory

Per `_shared/memory-flow.md`: retain per-dataset normal-range baselines (e.g. the
normal SNR distribution for this model/dataset), recurring anomaly patterns, and
analysis-script locations.
