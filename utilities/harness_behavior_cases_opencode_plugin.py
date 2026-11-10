#!/usr/bin/env python3
"""Skill-load behaviour scenarios for ``harness_behavior_scenarios.py``.

``skill-stage-reminder``
    The TUI runs as a dispatch-depth-1 conductor at ``standard`` intensity
    (``AGENT_DISPATCH_DEPTH=1``, ``AGENT_DISPATCH_INTENSITY=standard``) and the
    model loads the ``code-execute`` skill in-session.  Pass: the SD-11
    stage-dispatch reminder (``hooks/stage-dispatch-reminder.sh``) reaches the
    model with that skill load (Claude ``PreToolUse[Skill]``; OpenCode decides in
    ``tool.execute.before`` on ``skill`` and delivers with its result).  Codex has
    no skill tool; its router realization belongs to ``preflight.sh route``.

``skill-grounding``
    The person starts ``autopilot-code`` by name (``/autopilot-code``).  Pass: the
    capability-grounding marker Fleet reads
    (``$XDG_STATE_HOME/agent-fleet/capability-grounding/<sid>``) names
    ``autopilot-code``.  Codex has no skill tool and grounds at its router, so
    its model runs ``preflight.sh route autopilot-code``.

Synthetic data: only the fixture HOME's environment; no registry rows.
"""
from __future__ import annotations

from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402

REMINDER = "stage-dispatch: this session is a dispatch-depth-1 conductor"


def skill_call(protocol: str, names, skill: str, args: str = ""):
    """The harness's own skill-load tool call, or None when it has none."""
    if protocol == "anthropic" and "Skill" in names:
        return ("Skill", {"skill": skill, **({"args": args} if args else {})})
    if protocol == "chat" and "skill" in names:
        # OpenCode V2 names the skill `id` (https://opencode.ai/docs/skills/#loading).
        return ("skill", {"id": skill})
    return None


class SkillStageReminderScenario(Scenario):
    name = "skill-stage-reminder"
    summary = "A depth-1 conductor loading code-execute in-session sees the SD-11 reminder."

    def prepare(self, run):
        run.home.env["AGENT_DISPATCH_DEPTH"] = "1"
        run.home.env["AGENT_DISPATCH_INTENSITY"] = "standard"
        # Stage skills are withheld from every native skill list
        # (`harness_manifest.main_hidden_skill_names`), so the standard projection gives
        # the model no `code-execute` to load on any harness. Expose the projected skill
        # file in the native skill directory, as a profile that keeps it would, so the
        # reminder carrier itself is what is measured.
        source = Path(run.home.env["AGENT_HOME"]) / "adapters"
        targets = {
            "claude": (source / "claude" / "skills" / "code-execute",
                       Path(run.home.env.get("CLAUDE_CONFIG_DIR")
                            or Path(run.home.env["HOME"]) / ".claude") / "skills"),
            "opencode": (source / "opencode" / "skills" / "code-execute",
                         Path(run.home.env["XDG_CONFIG_HOME"]) / "opencode" / "skills"),
        }
        if run.harness in targets:
            skill, directory = targets[run.harness]
            destination = directory / "code-execute"
            if not destination.exists():
                destination.mkdir(parents=True)
                (destination / "SKILL.md").write_text(
                    (skill / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")
            run.evidence["exposed_skill"] = str(destination)

    def script(self, run):
        def answer(record, earlier):
            if record["last_kind"] != "tool" and "SCENARIO-SKILL-STAGE" in record["last_text"] \
                    and not any(r["reply"].get("tool") for r in earlier):
                call = skill_call(record["protocol"], record.get("tools") or [], "code-execute")
                if call:
                    return Reply(tool=call)
                return Reply("no skill tool")
            return Reply("skill observed")
        return answer

    def drive(self, run):
        run.prompt("SCENARIO-SKILL-STAGE go")
        run.wait_idle(6, 120)
        requests = run.main_requests()
        loaded = any(r["reply"].get("tool") for r in requests)
        run.evidence["skill_loaded"] = loaded
        if not loaded:
            run.note("the harness has no skill-load tool")
            return "gap"
        seen = any(REMINDER in r["text"] for r in requests[1:])
        run.evidence["reminder_seen"] = seen
        if not seen:
            run.note("stage-dispatch reminder never reached the model")
            return "fail"
        return "pass"


class SkillGroundingScenario(Scenario):
    name = "skill-grounding"
    summary = "Inline autopilot-code work leaves the capability-grounding marker Fleet reads."

    def script(self, run):
        def answer(record, earlier):
            if record["last_kind"] != "tool" and "SCENARIO-SKILL-GROUND" in record["last_text"] \
                    and not any(r["reply"].get("tool") or r["reply"]["shell"] for r in earlier):
                if record["protocol"] != "responses":
                    return Reply("grounding observed")
                # Codex: no skill tool; its entry router records the grounding (F-43).
                router = shlex.quote(str(Path(run.home.env["AGENT_HOME"]) / "adapters"
                                         / "codex" / "bin" / "preflight.sh"))
                return Reply(shell=f'sh {router} route autopilot-code "$PWD" '
                                   f'"${{CODEX_THREAD_ID:-codex}}" dev quick >/dev/null')
            return Reply("grounding observed")
        return answer

    def drive(self, run):
        # The person starts an entry capability by name (Claude and OpenCode `/name`);
        # Codex has no skill tool and its model runs the entry router.
        text = "SCENARIO-SKILL-GROUND --mode dev --intensity quick"
        run.prompt(text if run.harness == "codex" else "/autopilot-code " + text)
        run.wait_idle(6, 120)
        directory = Path(run.home.env["XDG_STATE_HOME"]) / "agent-fleet" / "capability-grounding"

        def markers():
            found = {}
            for path in sorted(directory.glob("*")) if directory.is_dir() else []:
                if path.is_file() and not path.name.startswith("."):
                    found[path.name] = path.read_text(encoding="utf-8", errors="replace")
            return found
        wait_for(lambda: any("capability=autopilot-code" in body
                             for body in markers().values()), 10)
        found = markers()
        run.evidence["grounding_markers"] = found
        if not any("capability=autopilot-code" in body for body in found.values()):
            run.note("no capability-grounding marker for autopilot-code")
            return "fail"
        return "pass"


SCENARIOS = (SkillStageReminderScenario, SkillGroundingScenario)
