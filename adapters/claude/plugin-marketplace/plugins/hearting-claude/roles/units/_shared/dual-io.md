# Shared Fragment: Dual Return Switch (direct vs pipeline)

> Referenced by units that serve both a direct caller and a pipeline stage.

- **Pipeline call (an output file path is specified in the prompt):** write the full
  result to that file and return EXACTLY one line:

  ```
  {output_file_path} -- {verdict}
  ```

  Verdict tokens are unit-defined. No summary, no explanation, no code snippets in the
  return — the single line is machine-harvested.

- **Direct call (no output path):** return the full result inline.

This governs the RETURN SHAPE only. On the registered-dispatch surface the worker
additionally emits the dispatch kernel's 3-line terminal handoff
(`artifact:` / `verdict:` / `blocker:`) exactly as `roles/worker-bootstrap.md` defines —
that contract is surface-owned and never altered by a unit.
