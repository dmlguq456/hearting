### Step 5: Pipeline summary

Use the `pipeline_summary` format from CONVENTIONS. Include:

- Pipeline name: `autopilot-research`.
- Mode and topic.
- `Steps completed` with status and output path.
- Counts for discovered sources, analyzed cards, and generated reports.
- Sources used.
- Failures or skips.
- QA result and unresolved items.

Write a concise final response in the user's communication language. Report the artifact directory, the briefing path, card and report counts, QA status, and any material limitation.

### Step 6: Progressive briefing

Read `{artifact_dir}/00_briefing.md` and present:

1. Level 0 one-line summary.
2. Level 1 key findings.
3. The report index from Level 3.
4. A short invitation to ask follow-up questions, phrased naturally in the user's communication language.

Do not hard-code a Korean or English closing sentence. Match the language and tone of the current user conversation.

After `pipeline_summary` is durable, evaluate the route-sealed optional artifact-sink extension from `WORKFLOW.md §0.2`. When available, offer the canonical research artifact through the app-neutral receipt contract; on unavailable, record `skipped/extension-unavailable` without invalidating the report.
