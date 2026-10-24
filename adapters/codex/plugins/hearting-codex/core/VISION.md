# VISION.md — North Star

> A one-page long-range vision memo, not a formal PRD. Promote it through `autopilot-spec` after the direction is agreed.
> Evidence: `research/hermes-agent/` under the artifact root, which benchmarks Hermes Agent, plus the `07_security.md` checklist.
> Written 2026-06-15.

## One Sentence

Graduate today's in-session work pipeline—Skills, agents, hooks, and loops—into an autonomous agent that turns on after installation: first as runtime plugins, then as an installer. The product is not limited to Codex, Claude Code, or any single runtime. Preserve the governance invariant that autonomous mechanisms prepare proposals and drafts while consequential decisions remain explicitly governed.

## Benchmark Motivation

The Hermes Agent benchmark suggests that Hermes and this harness provide complementary halves of a self-improving agent.

- **What this harness already has and must preserve:** hard ordering gates, artifact-guard hooks, adversarial N-vote verification, drill and study meta loops, artifact version governance, and specialized role separation. These provide reliability and verifiability.
- **What it lacked and should learn from:** cross-session recall, self-improvement drafts, time-based lifecycle management, persistent execution, and packaging. These improve automation, accessibility, and distribution.

The north star is to borrow Hermes's automation speed while placing it before this harness's decision gates. Adopt automatic drafting without silently adopting automatic application.

## Non-Negotiable Design Principles

1. **Automate mechanisms; govern decisions.** Autonomous actions may prepare proposals and drafts. Irreversible operations such as application, deletion, or deployment must follow the relevant user, safety, and operational gates.
2. **Governance first.** Strengthen ordering gates, ownership rules, version tracking, and adversarial verification as autonomy grows.
3. **Local and dependency-minimal.** Avoid direct dependence on external services by default; borrow concepts without surrendering local control of data.
4. **Keep meta-testing first-class.** The plugin or installed product must preserve the layers that test its own instructions through drills and compare itself with external developments through study.

## Security Premise

An autonomous agent has a broad attack surface. Start from verified incidents such as OpenClaw's one-click RCE and Hermes's own acknowledgement that in-process defenses are not containment.

- Plugin and installer work must treat the **OWASP ASI01–10 and LLM Top 10 checklist** in `07_security.md` as required input.
- Principal threats include arbitrary shell execution, prompt injection, Skill/plugin supply-chain compromise, credential exposure, and exposed gateways.
- Do not ship autonomous execution without real containment through sandboxing or isolation. In-process controls such as approvals, redaction, and scans are defense in depth, not substitutes for containment.

## Phased Roadmap

| Phase | Objective | Delivery form | Gate |
|---|---|---|---|
| **P0 — Strengthen the harness** | Import the useful Hermes gaps: recall, self-improvement drafts, lifecycle, and multi-pass behavior | Instructions, loops, and Skills inside `<agent-home>` | Drill regression passes |
| **P1 — Package and pluginize** | Turn distributed parts into installable units with standard plugin manifests, declared dependencies, and configuration scaffolds | Runtime plugins, beginning with the adapters that support marketplace distribution | Security checklist plus reproducible installation |
| **P2 — Installer** | Let non-experts boot the full harness by installing it, with an isolated execution environment and safe defaults | Standalone installer | Verified containment plus external security review |

## Non-Goals

- Self-training model weights in the style of Atropos; this harness consumes models rather than trains them.
- Ungoverned self-editing or automatic deployment.
- A persistent multichannel gateway today; it does not fit the current paper, experiment, and code workflows and can be reconsidered after P1.
- Direct integration with an external memory backend such as Honcho; borrow the concept only.

## Next Step

After P0 is stable and the direction is agreed, use `autopilot-spec` to write the P1 plugin PRD from this vision, the research, and `07_security.md`. Until then, this document remains the north-star anchor.
