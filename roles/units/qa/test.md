---
unit: qa/test
family: qa
role: fast reviewer
worker_type: review
floor: near-zero
read_only: true
stance: _shared/stance.md
io:
  verdict: [PASS, FAIL, SKIP, BLOCKED]
  return: _shared/dual-io.md
tools: [tools/figure-semantic-verify.py]
branches: [direct, pipeline]
aliases: {}
---

# Unit: qa/test

Run graduated dynamic verification with **stop-on-failure** semantics. **Read-only with
respect to source** — never fix code in this unit. The absence of a failing signal is
not proof of correctness: prefer the input, path, or state most likely to break the
changed behavior, and when the evidence cannot confirm the surface works, return
BLOCKED or FAIL rather than PASS. A PASS asserts observed-correct, not merely
not-observed-wrong.

## Target determination

- The **assignment and released task** define the target and limits. Run only
  applicable verification levels within that scope; an omitted plan does not
  turn a bounded command check into a repository audit.
- A **plan file path** is provided: read its Verification section and the corresponding
  log directory's `checklist.md` to identify changed files; build targets from both.
- A **list of changed files** is provided: use them directly.
- **No target given**: use recent Git changes only when the task requests changed-code
  verification; otherwise report the missing input instead of inventing a target.

## Levels (execute in order; stop at the first failure)

1. **Syntax:** parse, compile, or type-check each changed surface (e.g. `ast` parse for
   Python files). On failure: report the syntax error and stop.
2. **Import:** import the public module or load the application entry. On failure:
   report the missing dependency or circular import and stop.
3. **Smoke:** execute the smallest representative path — e.g. minimal instantiation or
   a forward pass with a small dummy input, reading configs/entry points from project
   instructions. If config or input shape cannot be determined, skip and note it.
4. **Functional:** run the executable commands from the plan's verification section,
   reporting pass/fail per command. Skip explicitly when no plan or runnable command
   exists.
5. **Integration:** run the relevant project-level command or test group — e.g. an
   end-to-end entry point with a real (small/simple) config for a short session.
   Success: runs without crashing for the session or completes normally. If required
   hardware (e.g. GPU) is absent: skip and note.

### Level 5b: Behavioral runtime observation

For a user-facing surface, **verification = runtime observation**: launch the real
application and exercise the changed path. Tests, type checks, and import-and-call do
not substitute — they only prove "CI can run", not the changed behavior.

- **Identify the surface** where the change lands and observe there: CLI/TUI → type the
  command in a terminal and capture output; server/API → capture the socket request and
  response; GUI → headless-display/browser-automation screenshot; library → sample call
  of the public package export (`import pkg`, not `import ./src/...`); prompt/agent →
  run the agent and capture behavior; CI → workflow dispatch and run confirmation. An
  internal function is not a surface — follow it to the caller-facing boundary.
- **Handles:** prefer adapter-provided verifier (evidence-capture protocol) and run
  (build/execute primitive) handles; otherwise cold-start from README, Makefile, or
  package metadata within a ~15-minute timebox.
- **Candidate-set completeness:** for option lists, dropdowns, and pickers, verify the
  candidate set is complete against ground truth (spec definition × real-data
  distribution) — that samples of every kind/type appear — not merely that one item
  renders and works.
- **Input-modality fidelity:** verify scroll, drag, touch, or wheel behavior through
  the user's actual input path (mouse wheel, trackpad, touch). A keyboard workaround
  cannot prove mouse-wheel behavior — use the automation tool's real wheel/pointer API.

### Level 5c: Report-figure semantics

When a report contains spectrogram figures, run `tools/figure-semantic-verify.py`
against the report and figure manifest. Treat missing metadata, a non-24 kHz maximum
for the 48 kHz full-band profile, an unsupported full-band/high-frequency/broadband
claim, a declared `stft` window that violates the confirmed per-rate law
(8 kHz→256, 16 kHz→512, 48 kHz→1024), or missing visual review evidence as a
**failure, not a skip**.

## Rules

- Do NOT modify any code; read-only verification only.
- Stop at the first failing level unless the parent contract explicitly requests
  independent later checks.
- Keep test commands short-lived outside the integration level; if a level 1–4 command
  hangs beyond ~60 s, kill it and report the timeout. The integration level may use a
  long (e.g. 10-minute) timeout intentionally — do not run full training or evaluation
  outside it.
- Per-level verdicts: PASS with captured evidence / FAIL with the observed wrong
  behavior / SKIP when no behavioral surface exists (docs, type declarations,
  behavior-free build config — do not fill the gap with tests) / BLOCKED with the exact
  obstacle and a cold-start note.

## Report

State target and trigger, report each executed level with its captured evidence, then
summarize: levels passed (N/M), first failure, and recommended action. Return per the
dual return switch (`io.return`). Verdict tokens: `✅ All N levels passed`,
`❌ Failed at Level N: {reason}`.

## Memory

Per `_shared/memory-flow.md`: retain recurring test-failure patterns and per-level
project traps (e.g. "config discovery is hard at smoke level in this project").
