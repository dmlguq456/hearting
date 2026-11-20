#!/usr/bin/env python3
"""A lookup unit route (`general/explore`), launched and finished on the three harnesses.

``unit-route-explore``
    The depth-0 session runs ``capability-route.py compose --start --unit general/explore --ephemeral
    --prompt-file <brief>`` with one task text and ends its turn. The route has no owner and is
    read-only: the harness's dispatch wrapper starts the one ``support`` worker on the fixture model,
    the worker answers (file and line beside the claim), ends ``verdict: PASS`` and the completion
    controller closes the route. The answer itself comes back to the resting session in the
    completion notice (``Answer:``). Nothing is left in the project's artifact folder.

    The oracle reads the same things ``unit-route-ephemeral`` does, and in addition that the leg
    is this unit (row ``unit``, worker type ``support``, model profile ``light``), that its first
    instruction carries the unit's text, that the route's field is ``other``, and how long the
    start took (the session's prompt to the worker's first model request, and the worker's last
    answer to the notice). Synthetic data stays under the fixture root.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_behavior_cases_unit_route as unit  # noqa: E402
from harness_behavior_cases_unit_nested import EphemeralScenario  # noqa: E402

QUESTION = "SCENARIO-UNIT-WORK: where does the fixture README say what the fixture is for? Answer with the file and line."


class ExploreScenario(EphemeralScenario):
    name = "unit-route-explore"
    summary = ("A lookup unit route (`compose --start --unit general/explore --ephemeral`): one task text starts one "
               "read-only worker with no owner, and its answer comes back with its completion.")
    unit_name = "general/explore"
    report_head = "Answered: the fixture README line 1 says what it is for"

    def prepare(self, run):
        super().prepare(run)
        (run.home.root / "brief.md").write_text(QUESTION + "\n", encoding="utf-8")

    def drive(self, run):
        ev = run.evidence
        asked = time.time()
        verdict = super().drive(run)
        workers = [r for r in run.main_requests() if "Dispatch metadata:" in r["text"] and unit.WORK in r["text"]]
        if workers:
            first = workers[0]
            ev["start_seconds"] = round(first["at"] - asked, 1)        # the prompt, the compose, the launch
            instruction = first["text"]
            ev["instruction_carries_unit"] = {word: word in instruction for word in
                                              ("general/explore", "answered", "not-found", "No further workers")}
            ev["instruction_has_question"] = QUESTION.split(": ", 1)[1] in instruction
        notice = [r for r in unit.resting_session_requests(run) if "Answer:" in r["text"] and r["last_kind"] != "tool"]
        if workers and notice:
            ev["answer_seconds"] = round(notice[0]["at"] - workers[-1]["at"], 1)   # the worker's last request to the notice
        route = self.route_in_scratch(ev)
        ev["route_field"] = (route.get("field"), route.get("field_source")) if route else None
        ev["route_node"] = ([(n.get("unit"), n.get("worker_type"), n.get("model_profile"), n.get("write_scope"))
                             for n in route.get("nodes", [])] if route else None)
        row = ev.get("row") or {}
        rows = self.unit_rows()
        ev["registry_unit"] = {k: rows[-1].get(k) for k in ("unit", "worker_type", "model_profile", "dispatch_depth",
                                                           "workflow_completion", "status")} if rows else None
        if verdict != "pass":
            return verdict
        ok = (ev["registry_unit"] and ev["registry_unit"]["unit"] == "general/explore"
              and ev["registry_unit"]["worker_type"] == "support" and ev["registry_unit"]["model_profile"] == "light"
              and ev["registry_unit"]["workflow_completion"] == "runtime-v1"
              and ev.get("instruction_has_question") and all(ev["instruction_carries_unit"].values())
              and ev["route_field"] == ("other", "preset")
              and ev["route_node"] and ev["route_node"][0][:3] == ("general/explore", "support", "light")
              and row.get("status") == "done")
        run.note(f"unit={ev['registry_unit'] and ev['registry_unit']['unit']}; field={ev['route_field']}; "
                 f"start={ev.get('start_seconds')}s; answer={ev.get('answer_seconds')}s")
        return "pass" if ok else "fail"

    def route_in_scratch(self, ev):
        """The route file the ephemeral route sealed (in its scratch root, named by the registry row)."""
        scratch = (ev.get("row") or {}).get("artifact_root")
        if not scratch:
            return None
        found = sorted((Path(scratch) / ".runtime" / "routes").glob("rt-*.json"))
        found = [p for p in found if not p.name.endswith(".outcome.json") and ".superseded" not in p.name]
        return json.loads(found[0].read_text(encoding="utf-8")) if found else None


SCENARIOS = [ExploreScenario]
