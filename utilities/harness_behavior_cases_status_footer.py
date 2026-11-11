#!/usr/bin/env python3
"""Hearting status display behaviour scenario for ``harness_behavior_scenarios.py``.

``status-footer``
    The model ends its turn with a marked question (``[답변 필요] ...``, the S4
    answer wait each harness's turn-end hook records), so the session waits for
    the person with its prompt box on screen.  The shared answer-wait cache
    (``utilities/human_wait_state.py --statusline``, the source of Claude's
    status line, the OpenCode footer, the session-shell panel and Fleet) then
    names this session.  Pass: while it waits, the TUI screen shows Hearting's
    ``◑답 대기 N`` badge with the cache's count.  Under ``--session-shell`` the
    badge belongs to the panel: pass when the panel shows the wait and no native
    footer row repeats the badge.  Gap: the cache names the wait and the screen
    does not.  Codex draws only built-in status items (PG-30, limit).

    OpenCode 2 loads its terminal plugins from ``plugins/<name>/`` (release
    activation renders them there with absolute imports); the source-tree fixture
    install skips that step, so ``prepare`` places the identity entry (which owns
    the ``prompt.footer.status`` slot) and the session-shell report the same way.
    While a native question form is open OpenCode replaces the prompt box and its
    footer with the form (measured on 2.0.32), so a form wait is not this case.

Synthetic data: the fixture HOME only; no registry rows.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
from harness_behavior_cases_b import fixture_python, session_id  # noqa: E402

BADGE = re.compile(r"◑\s*답 대기 (\?|\d+)")
QUESTION = "[답변 필요] SCENARIO-FOOTER Alpha 또는 Beta 중 어느 쪽으로 갈까요?"

STATUS_CODE = r"""
import subprocess
def status(*extra):
    done = subprocess.run([sys.executable, home + "/utilities/human_wait_state.py", "--statusline",
                           args["harness"], args["sid"], *extra],
                          capture_output=True, text=True, timeout=20)
    return done.returncode, done.stdout.split("\n")
code, lines = status()
# A tree before the footer change has no --with-tidy-notice; the counts stay comparable.
notice_code, notice = status("--with-tidy-notice")
print(json.dumps({"exit": code, "counts": lines[0] if lines else "",
                  "route": lines[1] if len(lines) > 1 else "",
                  "tidy_notice": notice[2] if notice_code == 0 and len(notice) > 2 else None}))
"""

# Fleet's own collection pass: the answer waits it shows for this session.
FLEET_CODE = r"""
from fleet.collectors import collect_all
from fleet.refresh import as_snapshot
snapshot = as_snapshot(collect_all(harness_filter={args["harness"]}, usage="cache-only"))
waits = snapshot.answer_waits or {}
mine = [item.get("id") for item in waits.get("items", ())
        if (item.get("owner") or {}).get("session_id") == args["sid"]]
print(json.dumps({"complete": waits.get("complete"), "total": len(waits.get("items", ())),
                  "this_session": len(mine)}, default=str))
"""


def place_opencode_terminal_plugins(run) -> dict:
    """Place the OpenCode 2 terminal plugins as release activation does."""
    home = Path(run.home.env["AGENT_HOME"])
    tui = home / "adapters" / "opencode" / "tui"
    plugins = Path(run.home.env["XDG_CONFIG_HOME"]) / "opencode" / "plugins"
    placed = {}
    identity = home / "adapters" / "opencode" / "plugins" / "hearting-tui-identity" / "tui.ts"
    if identity.is_file():
        target = plugins / "hearting-tui-identity" / "tui.ts"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(identity.read_text(encoding="utf-8").replace(
            '"../../tui/', '"' + str(tui) + "/"), encoding="utf-8")
        placed["identity"] = str(target)
    shell = tui / "hearting-session-shell.js"
    if shell.is_file():
        target = plugins / "hearting-session-shell" / "tui.js"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(shell.read_text(encoding="utf-8"), encoding="utf-8")
        placed["session_shell"] = str(target)
    return placed


class StatusFooterScenario(Scenario):
    name = "status-footer"
    summary = ("While the session waits for an answer, the TUI shows Hearting's answer-wait "
               "badge from the shared cache (panel under the session shell, else the "
               "native status line or footer).")

    def prepare(self, run):
        if run.harness == "opencode":
            run.evidence["terminal_plugins"] = place_opencode_terminal_plugins(run)
        if run.harness == "codex":
            path = Path(run.home.env["CODEX_HOME"]) / "config.toml"
            text = path.read_text(encoding="utf-8")
            if "default_mode_request_user_input" not in text:
                text += "\n[features]\ndefault_mode_request_user_input = true\n"
                path.write_text(text, encoding="utf-8")

    def script(self, run):
        def answer(record, earlier):
            if "SCENARIO-QUESTION" in record["last_text"] and record["last_kind"] != "tool":
                return Reply(text="한 가지만 정해 주세요.\n\n" + QUESTION)
            return Reply(f"answer noted {record['main_index']}")
        return answer

    def _shared(self, run, sid):
        return fixture_python(run, STATUS_CODE, {"sid": sid, "harness": run.harness})

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-QUESTION please ask me")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        sid = session_id(run)
        if not sid:
            run.note("session id not found")
            return "inconclusive"
        ev["question_on_screen"] = bool(wait_for(lambda: "SCENARIO-FOOTER" in run.tui.screen(), 30))
        run.wait_idle(quiet=4, timeout=60)
        # The cache producer refreshes a demanded session within about 30 seconds and
        # the footer reads the cache every 5 seconds.
        shared = wait_for(lambda: (lambda value: value if re.fullmatch(
            r"(\?|\d+) [1-9]\d*", str(value.get("counts", ""))) else None)(
                self._shared(run, sid)), 75) or self._shared(run, sid)
        ev["shared_status"] = shared
        waiting = bool(re.fullmatch(r"(\?|\d+) [1-9]\d*", str(shared.get("counts", ""))))
        badge = wait_for(lambda: BADGE.findall(run.tui.screen()) or None, 30) if waiting else None
        time.sleep(1)
        screen = run.tui.screen()
        ev["screen_waiting"] = screen[-2500:]
        ev["badges_on_screen"] = BADGE.findall(screen)
        ev["badge_rows"] = [line.strip()[:200] for line in screen.splitlines() if BADGE.search(line)]
        if not waiting:
            run.note("the shared cache never named this session's wait")
            return "inconclusive"
        total = str(shared["counts"]).split()[0]
        ev["fleet_answer_waits"] = fixture_python(run, FLEET_CODE, {"sid": sid,
                                                                  "harness": run.harness}, 120)
        if "session_shell" in ev:   # set by Run.execute under --session-shell
            # The panel paints the wait with Fleet's glyph and catalog (`◑ questions 1`).
            ev["panel_wait_rows"] = [line.strip()[:200] for line in screen.splitlines()
                                     if "◑" in line and not BADGE.search(line)]
            run.note(f"shell: badge rows {len(ev['badge_rows'])}, wait rows "
                     f"{len(ev['panel_wait_rows'])}")
            if not ev["panel_wait_rows"]:
                return "gap"
            return "pass" if len(ev["badge_rows"]) <= 1 else "fail"
        if not badge:
            run.note(f"shared cache counts {shared['counts']}, no badge on screen")
            return "gap"
        if any(value != total for value in ev["badges_on_screen"]):
            run.note(f"badge {ev['badges_on_screen']} != shared total {total}")
            return "fail"
        return "pass"


SCENARIOS = (StatusFooterScenario,)
