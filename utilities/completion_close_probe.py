#!/usr/bin/env python3
"""Opt-in probe of the user-facing first close after a completed owner, before vs after.

The probe keeps the two halves of a close comparison apart:

* ``prompts`` renders, for each of two Hearting trees and in a subprocess that
  imports only that tree's ``utilities/``, the completion summary of four
  synthetic final reports (``work_intent.completion_summary``, Korean catalog)
  and the carrier each harness's main session receives: the Claude wake
  (``hooks/dispatch-owner-rewake.py`` ``classified_receipt``), the Codex queue
  body (``codex_queue_delivery.native_completion_body``) and the OpenCode turn
  (``dispatch_session_sweep.delivery_context``), the last two around
  ``dispatch_completion_join.completion_followup_text``.  Only the delivery
  state the existing rewake test stubs is stubbed; the summary is sealed in a
  real terminal slot and read back by each tree's own ``sealed_summary``.  It
  then writes one prompt per harness x event x side: that tree's bootstrap,
  its ``core/WORKFLOW.md`` §0.5, the request, the carrier and a fixed task.
* ``run`` (optional) pipes each prompt to an operator's model command.  Replies
  may equally be saved by hand as ``<harness>/<event>.<side>.reply.txt``.
* ``measure`` reads every reply present: length, whether the summary head is
  reproduced, and each event's fixture facts.  An absent reply is listed as
  ``unmeasured``; nothing here is a pass or a gate.

Nothing else reads or requires this probe, and nothing calls a model unless
``run`` is given a command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

EVENTS = ("success", "failure", "partial", "decision", "recommended", "usage")
HARNESSES = ("claude", "codex", "opencode")
SIDES = ("before", "after")
BOOTSTRAPS = {"claude": "adapters/claude/CLAUDE.md", "codex": "adapters/codex/AGENTS.md",
              "opencode": "adapters/opencode/AGENTS.md"}
SESSION_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}
# Files whose text decides a prompt; their digests go into the manifest.
SOURCES = ("utilities/work_intent.py", "hooks/dispatch-owner-rewake.py",
           "utilities/dispatch_completion_join.py", "utilities/codex_queue_delivery.py",
           "utilities/dispatch_session_sweep.py", "core/WORKFLOW.md", *BOOTSTRAPS.values())
TASK = ("Write exactly the reply you would now show the person, in Korean, following your "
        "instructions. Output only that reply text, nothing before or after it.")
# Classification markers only (which first-close directive a carrier holds); the probe
# renders every directive from the tree under test and never from these words.
OLD_DIRECTIVE_MARK = "as written"
CURRENT_DIRECTIVE_MARK = "do not reproduce it for the person"
RECORD_PLACEHOLDER = "<기록>"
PROBE_PLACEHOLDER = "<probe>"

# Common ways a reply asks the person for something (absent from a reply that proceeds).
ASKING_PHRASES = ("할까요", "드릴까요", "하실까요", "괜찮을까요", "정해 주", "정해주", "골라 주", "골라주",
                  "선택해", "결정이 하나 필요", "결정이 필요", "결정 필요", "원하시면", "원하신다면",
                  "알려 주세요", "알려주세요", "말씀해 주", "말씀해주")

# The four synthetic final reports and requests of evidence/real-model-probe (leg 0).
# A fact holds when any one of its phrases appears in the reply.
FIXTURES = {
    "success": {
        "request": "설정 화면 저장 버튼 중복 저장 버그를 고쳐줘",
        "report": ("# 최종 보고\n\n## 바뀐 것\n"
                   "- 설정 화면의 저장 버튼이 두 번 눌려도 한 번만 저장되도록 고쳤다\n"
                   "- 저장 실패 시 이전 값을 되돌리는 처리를 넣었다\n- 관련 시험 두 개를 추가했다\n\n"
                   "## 검증\n- 단위 시험 42개 통과\n- 저장 중복 재현 시험 통과\n- 생성물 검사 통과\n\n"
                   "## 미검증\n- 없음\n\n## 후속\n- 없음\n"),
        "facts": {},
    },
    "failure": {
        "request": "내보내기 파일 이름 규칙을 날짜-제목 순서로 바꿔줘",
        "report": ("# 최종 보고\n\n## 바뀐 것\n- 내보내기 기능의 파일 이름 규칙을 바꿨다\n\n"
                   "## 검증\n- 단위 시험 40개 중 3개 실패: 한글 파일 이름에서 인코딩 오류(UnicodeEncodeError)\n"
                   "- 생성물 검사 통과\n\n## 남은 것\n"
                   "- 실패: 한글 파일 이름 내보내기가 깨진다. 원인(파일 시스템 인코딩 처리)은 아직 고치지 못했다\n"
                   "- 이 변경은 아직 쓰면 안 된다\n- 인코딩 처리 수정 후 재시험 필요\n"),
        "facts": {"failure_stated": ("실패", "깨지", "깨진", "깨져", "오류"),
                  "not_usable": ("쓰면 안", "쓸 수 없", "사용하면 안", "사용할 수 없", "쓰지 말", "쓰지 마",
                                 "사용하지 말", "사용하지 마")},
        "facts_absent": {"asks_followup": ASKING_PHRASES},
    },
    "partial": {
        "request": "검색 결과에 최신순·관련도순 정렬을 추가해줘",
        "report": ("# 최종 보고\n\n## 바뀐 것\n- 검색 결과 정렬 옵션 두 개를 추가했다\n\n"
                   "## 검증\n- 단위 시험 통과\n- 정렬 순서 시험 통과\n\n## 남은 것\n"
                   "- 실제 서버 부하 시험은 실행하지 않았다(시험 서버 접속 불가)\n"
                   "- 대용량 결과(10만 건 이상)에서의 속도는 확인하지 못했다\n"
                   "- 시험 서버가 열리면 부하 시험 재실행\n"),
        "facts": {"unrun_check": ("부하 시험", "부하 테스트", "부하시험")},
    },
    "decision": {
        "request": "알림이 너무 자주 와서 묶어서 받게 해줘",
        "report": ("# 최종 보고\n\n## 바뀐 것\n- 새 알림 묶음 기능을 만들었다. 지금은 이 저장소에서만 켜져 있다\n\n"
                   "## 검증\n- 단위 시험 통과\n- 격리 환경 실측 통과\n\n## 남은 것\n"
                   "- 다른 프로젝트에도 기본으로 켤지는 사용자가 정해야 한다: 켜면 모든 프로젝트의 알림이 "
                   "하루 세 번 묶음으로 바뀌고, 끄면 지금처럼 바로바로 온다\n- 사용자 결정 후 기본값 반영\n"),
        "facts": {"question": ("?", "할까요", "정해 주", "정해주", "골라 주", "골라주", "선택해"),
                  "on_effect": ("하루 세 번", "하루 3번", "하루 3회"),
                  "off_effect": ("바로바로", "바로 ", "즉시", "지금처럼"),
                  "recommendation": ("추천", "권장"),
                  "if_no_answer": ("답이 없으면", "답하지 않으", "정해 주실 때까지", "기다리", "멈춰")},
    },
    "recommended": {
        "request": "저장소 안에서 로그 보관 기간 기본값을 골라줘",
        "report": ("# 최종 보고\n\n## 바뀐 것\n- 로그 보관 기간을 정리했다\n\n"
                   "## 검증\n- 관련 시험 통과\n\n## 남은 것\n"
                   "- 로그 보관 기간: 기존 설정 30일 유지(권장: 요청 범위 안이고 되돌릴 수 있음) "
                   "또는 90일로 늘리기(저장 공간 3배)\n"),
        "facts": {"chosen_stated": ("30일",), "reason_stated": ("되돌릴", "범위", "권장")},
        "facts_absent": {"asks": ASKING_PHRASES},
    },
    "usage": {
        "request": "이미 승인된 다음 단계를 이어서 진행해줘",
        "report": ("# 최종 보고\n\n## 바뀐 것\n- 요청한 작업을 완료했다\n\n"
                   "## 검증\n- 관련 시험 통과\n\n## 남은 것\n"
                   "- 이미 승인된 다음 단계: 같은 처리를 목록 화면에도 적용하고 시험한다(이 세션이 이어서 진행)\n"),
        # The token count reaches the person's prompt only through the summary's real cost line.
        "cost": {"elapsed_seconds": 5400, "tokens": 2400000, "sessions": 5, "unknown": 0, "partial": 0},
        "facts": {"usage_stated": ("2,400,000", "240만", "2.4M", "2400000"),
                  "continues": ("이어서", "진행", "시작")},
        "facts_absent": {"waits": ("기다리", "할까요", "괜찮을까요", "멈춰", "미루")},
    },
}
COMMON_FACTS = {"record_pointer": ("final_report.md", RECORD_PLACEHOLDER)}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"


def _tree_head(tree: Path) -> str:
    result = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True,
                            text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "-"


def directive(carrier: str) -> str:
    """Which first-close directive a carrier holds."""
    if OLD_DIRECTIVE_MARK in carrier:
        return "old-as-written"
    if CURRENT_DIRECTIVE_MARK in carrier:
        return "current-short"
    return "other"


def workflow_section(text: str) -> str:
    """`### 0.5.` up to the next heading of the same or a higher level."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("### 0.5."))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith(("### ", "## ", "# "))), len(lines))
    return "\n".join(lines[start:end]).rstrip() + "\n"


def prompt_text(harness: str, bootstrap: str, section: str, request: str, carrier: str) -> str:
    name = BOOTSTRAPS[harness].rsplit("/", 1)[-1]
    return (f"You are taking part in a measurement. Play the user-facing main {SESSION_NAMES[harness]} "
            "session of a project that uses the Hearting workflow harness. Apart from having read this "
            "file, do not use any tools; answer from the text below only.\n\n"
            f"=== Your project instruction file ({name}), loaded at session start ===\n{bootstrap.rstrip()}\n\n"
            f"=== core/WORKFLOW.md §0.5, which those instructions reference ===\n{section.rstrip()}\n\n"
            f"=== Conversation so far ===\nPerson (writes Korean): {request}\n"
            "You: started the registered owner for this work and ended your turn.\n\n"
            f"=== The runtime now delivers this message into your session ===\n{carrier.rstrip()}\n"
            f"=== Task ===\n{TASK}\n")


# ---------------------------------------------------------------------------
# Rendering inside one tree (subprocess: only that tree's utilities are importable)
# ---------------------------------------------------------------------------

def _render_tree(tree: Path, work: Path) -> dict:
    import importlib.util
    from types import SimpleNamespace
    from unittest import mock

    sys.path.insert(0, str(tree / "utilities"))
    import work_intent
    import dispatch_terminal_commit
    import dispatch_completion_join as join
    import codex_queue_delivery
    import dispatch_session_sweep as sweep

    hook_path = tree / "hooks" / "dispatch-owner-rewake.py"
    spec = importlib.util.spec_from_file_location("probe_owner_rewake", hook_path)
    hook = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = hook
    spec.loader.exec_module(hook)
    modules = {m.__name__: str(Path(m.__file__).resolve())
               for m in (work_intent, dispatch_terminal_commit, join, codex_queue_delivery, sweep, hook)}
    stray = [path for path in modules.values() if not Path(path).is_relative_to(tree.resolve())]
    if stray:
        raise SystemExit(f"modules imported outside {tree}: {stray}")

    t = work_intent.ui_translator()
    events = {}
    for event in EVENTS:
        fixture = FIXTURES[event]
        base = work / event
        base.mkdir(parents=True)
        attempt = f"att-probe-{event}"
        route = {"capability": "autopilot-code", "route_id": f"rt-probe{event[:4]}",
                 "artifact_root": str(base), "work_request": {"text": fixture["request"]}}
        route_file = base / "route.json"
        route_file.write_text(json.dumps(route, ensure_ascii=False), encoding="utf-8")
        primary = base / "final_report.md"
        primary.write_text(fixture["report"], encoding="utf-8")
        summary = work_intent.completion_summary(route, str(primary), t=t, cost=fixture.get("cost"))
        summary = summary.replace(str(base), RECORD_PLACEHOLDER)
        slot = dispatch_terminal_commit.terminal_slot(base, route["route_id"], attempt)
        slot.mkdir(parents=True)
        (slot / work_intent.SUMMARY_NAME).write_text(summary, encoding="utf-8")
        jobs = base / "jobs.log"
        meta = {"attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "attempt_id": attempt, "parent_attempt_id": "att-probe-parent", "note": "",
                "worker_type": "owner", "workflow_completion": "runtime-v1",
                "owner_route_file": str(route_file)}
        jobs.write_text("2026-11-11T00:00:00Z\tdone\t/repo\t/wt\ttask\t"
                        + ",".join(f"{k}={v}" for k, v in meta.items()) + "\n", encoding="utf-8")
        child = {"attempt_id": attempt, "delivery_classification": "success", "readiness": "ready",
                 "reason": "registry-closed", "required_action": "advance-completed", "status": "done"}
        receipt = {"children": [child], "delivery_classification": "success", "state": "ready",
                   "job_registry": str(jobs)}
        followup = join.completion_followup_text(
            receipt, jobs=str(jobs), surface=str(tree / "adapters" / "codex" / "bin" / "preflight.sh"))
        codex = codex_queue_delivery.native_completion_body("probe", [], receipt, followup)
        opencode = sweep.delivery_context([(base.resolve(), [
            {"delivery_id": f"delivery-probe-{event}", "route_id": route["route_id"], "receipt": receipt}])])
        delivery = SimpleNamespace(status="done", row_revision="1", row_digest="d", owned_children=0,
                                   quiescent=True, advanced=True)
        with mock.patch.object(hook, "current_delivery_state", return_value=delivery), \
                mock.patch.object(hook, "current_attempt_row",
                                  return_value=SimpleNamespace(metadata={"owner_route_file": str(route_file)})), \
                mock.patch.object(hook, "delivery_required_action", return_value="advance-completed"), \
                mock.patch.object(hook, "delivery_classification", return_value="success"), \
                mock.patch.object(hook, "_completion_evidence_current", return_value=True):
            state, claude = hook.classified_receipt(hook.Launch(attempt_id=attempt, jobs=jobs, session_id="probe"),
                                                    "ready", "terminal", tree)
        if state != "success":
            raise SystemExit(f"{tree}: Claude wake for {event} classified {state}")
        carriers = {"claude": claude, "codex": codex, "opencode": opencode}
        carriers = {name: text.replace(str(base.resolve()), PROBE_PLACEHOLDER).replace(str(base), PROBE_PLACEHOLDER)
                    for name, text in carriers.items()}
        events[event] = {"summary": summary, "carriers": carriers}
    return {"modules": modules, "heads": list(work_intent.summary_heads()), "events": events}


def render(tree: Path) -> dict:
    """Run `_render_tree` for one tree in a fresh isolated interpreter."""
    with tempfile.TemporaryDirectory(prefix="completion-close-probe-") as scratch:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "AGENT_"))}
        env.update(FLEET_NOW_LANG="ko", TMPDIR=scratch)
        work = Path(scratch) / "work"
        result = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "_render",
                                 "--tree", str(tree), "--work", str(work)],
                                capture_output=True, text=True, env=env, cwd=scratch, timeout=300, check=False)
    if result.returncode != 0:
        raise SystemExit(f"rendering {tree} failed (exit {result.returncode}):\n{result.stderr[-4000:]}")
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_prompts(before: Path, after: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"probe": "completion-close", "trees": {}, "heads": [], "prompts": []}
    for side, tree in (("before", before), ("after", after)):
        tree = tree.resolve()
        rendered = render(tree)
        section = workflow_section((tree / "core" / "WORKFLOW.md").read_text(encoding="utf-8"))
        manifest["trees"][side] = {"path": str(tree), "head": _tree_head(tree), "modules": rendered["modules"],
                                   "sources": {name: _sha256(tree / name) for name in SOURCES}}
        manifest["heads"] = sorted(set(manifest["heads"]) | set(rendered["heads"]))
        for harness in HARNESSES:
            bootstrap = (tree / BOOTSTRAPS[harness]).read_text(encoding="utf-8")
            (out / harness).mkdir(exist_ok=True)
            for event in EVENTS:
                data = rendered["events"][event]
                carrier = data["carriers"][harness]
                text = prompt_text(harness, bootstrap, section, FIXTURES[event]["request"], carrier)
                path = out / harness / f"{event}.{side}.prompt.txt"
                path.write_text(text, encoding="utf-8")
                manifest["prompts"].append({
                    "harness": harness, "event": event, "side": side, "prompt": str(path.relative_to(out)),
                    "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(), "prompt_chars": len(text),
                    "carrier_directive": directive(carrier), "carrier_chars": len(carrier),
                    "summary_chars": len(data["summary"].strip()),
                    "summary_in_carrier": data["summary"].strip() in carrier})
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def reply_path(out: Path, harness: str, event: str, side: str) -> Path:
    return out / harness / f"{event}.{side}.reply.txt"


def cmd_run(out: Path, harness: str, command: str, timeout: float) -> list[dict]:
    """Pipe each prompt of one harness to `command`; keep stdout of a clean exit as the reply."""
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    runs = []
    for entry in manifest["prompts"]:
        if entry["harness"] != harness:
            continue
        event, side = entry["event"], entry["side"]
        prompt = (out / entry["prompt"]).read_text(encoding="utf-8")
        record = {"event": event, "side": side, "command": command, "prompt_sha256": entry["prompt_sha256"]}
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="completion-close-run-") as cwd:
            env = {**os.environ, "PROBE_HARNESS": harness, "PROBE_EVENT": event, "PROBE_SIDE": side,
                   "PROBE_PROMPT": str((out / entry["prompt"]).resolve())}
            try:
                result = subprocess.run(command, shell=True, input=prompt, capture_output=True, text=True,
                                        cwd=cwd, env=env, timeout=timeout, check=False)
                record.update(exit=result.returncode, stderr_tail=result.stderr[-2000:])
                if result.returncode == 0 and result.stdout.strip():
                    reply_path(out, harness, event, side).write_text(result.stdout, encoding="utf-8")
                    record["reply"] = str(reply_path(out, harness, event, side).relative_to(out))
            except subprocess.TimeoutExpired:
                record.update(exit=None, timed_out=True)
        record["seconds"] = round(time.monotonic() - started, 1)
        (out / harness / f"{event}.{side}.run.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        runs.append(record)
    return runs


def measure_reply(text: str, event: str, heads) -> dict:
    body = text.strip()
    fixture = FIXTURES[event]
    facts = {name: any(phrase in body for phrase in phrases)
             for name, phrases in {**fixture.get("facts", {}), **COMMON_FACTS}.items()}
    facts_absent = {name: not any(phrase in body for phrase in phrases)
                    for name, phrases in fixture.get("facts_absent", {}).items()}
    return {"chars": len(body), "lines": len(body.splitlines()),
            "head_reproduced": any(head in body for head in heads),
            "facts": facts, "facts_absent": facts_absent}


def cmd_measure(out: Path) -> dict:
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    rows = []
    for entry in manifest["prompts"]:
        path = reply_path(out, entry["harness"], entry["event"], entry["side"])
        row = {key: entry[key] for key in ("harness", "event", "side", "carrier_directive", "summary_chars")}
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if text.strip():
            row.update(status="measured", reply=str(path.relative_to(out)),
                       **measure_reply(text, entry["event"], manifest["heads"]))
        else:
            row.update(status="unmeasured", reply=None)
        rows.append(row)
    results = {"probe": "completion-close", "trees": {side: {"path": tree["path"], "head": tree["head"]}
                                                      for side, tree in manifest["trees"].items()},
               "rows": rows, "unmeasured": [f"{r['harness']}/{r['event']}.{r['side']}" for r in rows
                                            if r["status"] == "unmeasured"]}
    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "results.md").write_text(results_markdown(results), encoding="utf-8")
    return results


def _cell(row, key):
    if row is None or row["status"] != "measured":
        return "unmeasured"
    value = row[key]
    return "yes" if value is True else "no" if value is False else str(value)


def _facts_cell(row):
    if row is None or row["status"] != "measured":
        return "unmeasured"
    facts = [f"{name}={'yes' if held else 'NO'}" for name, held in row["facts"].items()]
    facts.extend(f"no:{name}={'yes' if held else 'NO'}"
                 for name, held in row.get("facts_absent", {}).items())
    return " ".join(facts)


def results_markdown(results: dict) -> str:
    rows = {(r["harness"], r["event"], r["side"]): r for r in results["rows"]}
    lines = ["# Completion close probe — results", "",
             f"before: `{results['trees'].get('before', {}).get('path', '-')}` "
             f"({results['trees'].get('before', {}).get('head', '-')})",
             f"after: `{results['trees'].get('after', {}).get('path', '-')}` "
             f"({results['trees'].get('after', {}).get('head', '-')})", "",
             "Chars are Unicode characters of the stripped reply; lines include blank lines. "
             "A reply that is absent is `unmeasured`, never a pass.", "",
             "| harness | event | directive before→after | chars before | chars after | change | lines before→after "
             "| summary head before/after | facts before | facts after |",
             "|---|---|---|---:|---:|---:|---|---|---|---|"]
    for harness in HARNESSES:
        for event in EVENTS:
            b, a = rows.get((harness, event, "before")), rows.get((harness, event, "after"))
            change = "-"
            if b and a and b["status"] == a["status"] == "measured" and b["chars"]:
                change = f"{round(100 * (a['chars'] - b['chars']) / b['chars'])}%"
            lines.append(f"| {harness} | {event} | {(b or {}).get('carrier_directive', '-')}→"
                         f"{(a or {}).get('carrier_directive', '-')} | {_cell(b, 'chars')} | {_cell(a, 'chars')} | "
                         f"{change} | {_cell(b, 'lines')}→{_cell(a, 'lines')} | "
                         f"{_cell(b, 'head_reproduced')}/{_cell(a, 'head_reproduced')} | "
                         f"{_facts_cell(b)} | {_facts_cell(a)} |")
    lines += ["", f"Unmeasured ({len(results['unmeasured'])}): "
              + (", ".join(results["unmeasured"]) if results["unmeasured"] else "none"), ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prompts = sub.add_parser("prompts", help="render before/after prompts from two trees")
    prompts.add_argument("--before", type=Path, required=True)
    prompts.add_argument("--after", type=Path, required=True)
    prompts.add_argument("--out", type=Path, required=True)
    run = sub.add_parser("run", help="pipe one harness's prompts to a model command (stdin -> stdout)")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--harness", choices=HARNESSES, required=True)
    run.add_argument("--model-command", required=True)
    run.add_argument("--timeout", type=float, default=900.0)
    measure = sub.add_parser("measure", help="measure the replies present under --out")
    measure.add_argument("--out", type=Path, required=True)
    internal = sub.add_parser("_render")
    internal.add_argument("--tree", type=Path, required=True)
    internal.add_argument("--work", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "_render":
        json.dump(_render_tree(args.tree.resolve(), args.work), sys.stdout, ensure_ascii=False)
        return 0
    if args.command == "prompts":
        manifest = cmd_prompts(args.before, args.after, args.out)
        print(f"{len(manifest['prompts'])} prompts under {args.out}")
        return 0
    if args.command == "run":
        runs = cmd_run(args.out, args.harness, args.model_command, args.timeout)
        print(f"{sum('reply' in r for r in runs)}/{len(runs)} replies saved for {args.harness}")
        return 0
    results = cmd_measure(args.out)
    print(f"{len(results['rows']) - len(results['unmeasured'])} measured, "
          f"{len(results['unmeasured'])} unmeasured; see {args.out / 'results.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
