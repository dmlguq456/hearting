#!/usr/bin/env bash
# Session card delivery on the three harness bridges (Claude hook, Codex
# SessionStart/UserPromptSubmit hooks, OpenCode plugin). The card path must not
# depend on the memory candidate probe: short/empty prompts, no memory tool and
# zero candidates still deliver it.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
TMP_BASE=${TMPDIR:-/tmp}
TMP_BASE=${TMP_BASE%/}
if ! mkdir -p "$TMP_BASE" 2>/dev/null || [ ! -w "$TMP_BASE" ]; then
  TMP_BASE=/tmp
fi
TMP=$(mktemp -d "$TMP_BASE/session-card-test.XXXXXX")
SHIM_TMP=$(mktemp -d "$ROOT/.session-card-shim.XXXXXX")
unset FIXTURE_MARKER
FIXTURE_MARKER=
cleanup_fixture() {
  local fixture_status=$? cleanup_status=0
  trap - EXIT INT TERM
  if [ -n "$FIXTURE_MARKER" ]; then
    if python3 - "$ROOT" "$FIXTURE_MARKER" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "tools"))
import fixture_processes
marker = sys.argv[2]
fixture_processes.reap(marker, timeout=10)
left = fixture_processes.marked_processes(marker)
if left:
    raise SystemExit("fixture runner survived cleanup: %s" % sorted(left))
PY
    then
      :
    else
      cleanup_status=$?
      echo "fixture runner cleanup failed" >&2
    fi
  fi
  rm -rf "$TMP" "$SHIM_TMP" || cleanup_status=1
  if [ "$fixture_status" -eq 0 ] && [ "$cleanup_status" -ne 0 ]; then
    exit "$cleanup_status"
  fi
  exit "$fixture_status"
}
trap cleanup_fixture EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
FAILS=0
ok() { printf 'ok - %s\n' "$1"; }
bad() { printf 'not ok - %s\n' "$1"; FAILS=$((FAILS + 1)); }

# Hermetic environment: real state, memory store, remote setting and pane never
# reach a case; worker markers and herdr identity are removed.
unset AGENT_SESSION_ROLE AGENT_DISPATCH_CHILD AGENT_DISPATCH_DEPTH AGENT_DISPATCH_STAGE_AUTHORITY \
  AGENT_DISPATCH_SUBSESSION_ID OPENCODE_DISPATCH_SLUG \
  FLEET_TITLE_REFRESH MEM_DISTILL HERDR_PANE_ID HERDR_ENV HERDR_SOCKET_PATH \
  CLAUDE_CODE_SESSION_ID CODEX_THREAD_ID CODEX_SESSION_ID OPENCODE_SESSION_ID \
  CODEX_SESSION_MEMORY_INJECT
export HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/home/.config" XDG_DATA_HOME="$TMP/home/.local/share" \
  XDG_STATE_HOME="$TMP/home/.local/state" XDG_CACHE_HOME="$TMP/home/.cache" \
  MEM_STORE="$TMP/mem" TMPDIR="$TMP/tmp" AGENT_HOME="$ROOT"
mkdir -p "$HOME" "$XDG_CONFIG_HOME/hearting" "$TMPDIR"
printf '{"enabled": false}\n' > "$XDG_CONFIG_HOME/hearting/memory-sync.json"
case "$XDG_STATE_HOME" in "$TMP_BASE"/session-card-test.*/*) ;; *) echo "isolation failed"; exit 2 ;; esac

start_fixture_runner() {
  local marker_file=${SESSION_CARD_MARKER_FILE:-}
  FIXTURE_MARKER="$TMP/runner-marker-$$"
  if ! python3 - "$ROOT" "$FIXTURE_MARKER" "$marker_file" <<'PY'
import hashlib, os, sys, time
from pathlib import Path
root, marker, marker_file = map(str, sys.argv[1:4])
sys.path.insert(0, str(Path(root) / "utilities"))
sys.path.insert(0, str(Path(root) / "tools"))
import peer_obligations
from harness_capabilities import parent_completion
import fixture_processes

state_root = peer_obligations.peer_state_root()
state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
jobs = state_root / "jobs.log"
jobs.touch(exist_ok=True)
os.environ["AGENT_DISPATCH_JOBS"] = str(jobs)
carrier = parent_completion("codex")["carrier"]
store = peer_obligations.ObligationStore(state_root)
duty_id = "fixture-runner-" + hashlib.sha256(marker.encode()).hexdigest()[:16]
store.create(duty_id, "registered-batch",
    {"jobs": str(jobs), "attempt_id": "att-fixture-runner", "session_id": "fixture",
     "harness": "codex", "server": "default"}, {"carrier": carrier})
os.environ["SESSION_CARD_FIXTURE_MARKER"] = marker
started = peer_obligations.ensure_runner(state_root, reconciliation_required=True)
os.environ.pop("SESSION_CARD_FIXTURE_MARKER", None)
if not started:
    raise SystemExit("fixture runner did not start")
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    runners = []
    for pid in fixture_processes.marked_processes(marker):
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if b"__obligation-runner" in argv and b"peer-steward.py" in argv:
            runners.append(pid)
    if runners:
        break
    time.sleep(0.02)
else:
    raise SystemExit("marked fixture runner did not become visible")
if marker_file:
    Path(marker_file).write_text(marker, encoding="utf-8")
PY
  then
    return 1
  fi
  return 0
}

if [ "${SESSION_CARD_RUNNER_PROBE:-0}" = 1 ]; then
  if ! start_fixture_runner; then exit 72; fi
  case "${SESSION_CARD_PROBE_MODE:-failure}" in
    signal) kill -TERM "$$"; sleep 1; exit 143 ;;
    *) exit 71 ;;
  esac
fi

probe_runner_cleanup() { # exercise EXIT after failure and TERM interruption
  local mode=$1 expected=$2 marker_file="$TMP/runner-probe-$1.marker" pid status marker
  SESSION_CARD_RUNNER_PROBE=1 SESSION_CARD_PROBE_MODE="$mode" \
    SESSION_CARD_MARKER_FILE="$marker_file" bash "$0" \
    >"$TMP/runner-probe-$mode.log" 2>&1 &
  pid=$!
  wait "$pid"
  status=$?
  if [ "$status" -ne "$expected" ] || [ ! -s "$marker_file" ]; then
    bad "session-card runner $mode probe did not preserve exit/cleanup"
    return
  fi
  marker=$(cat "$marker_file")
  if python3 - "$ROOT" "$marker" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "tools"))
import fixture_processes
raise SystemExit(bool(fixture_processes.marked_processes(sys.argv[2])))
PY
  then
    ok "session-card runner cleanup after $mode"
  else
    bad "session-card runner remained after $mode"
  fi
}

probe_runner_cleanup failure 71
probe_runner_cleanup signal 143
if start_fixture_runner; then
  ok "session-card fixture starts a registered shared runner"
else
  bad "session-card fixture runner start"
fi

TIDY="$ROOT/utilities/session_tidy.py"
REAL_PYTHON=$(command -v python3)
cat > "$SHIM_TMP/python3" <<'SH'
#!/bin/sh
case "${1:-}" in
  */worker-state-hook.py)
    if [ "${2:-}" = compact-after ]; then
      # The compact payload arrives on stdin; record it ledger-only so no detached tidy starts.
      exec "$REAL_PYTHON" -c '
import json, subprocess, sys
payload = json.load(sys.stdin)
sid = payload.get("sessionID") or payload.get("session_id") or ""
cwd = payload.get("cwd") or ""
if sid:
    subprocess.run([sys.executable, sys.argv[1], "hook", "--harness", "opencode",
                    "--event", "compact", "--session-id", sid, "--cwd", cwd],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
' "$TIDY"
    fi
    ;;
esac
exec "$REAL_PYTHON" "$@"
SH
chmod 700 "$SHIM_TMP/python3"
export REAL_PYTHON TIDY PATH="$SHIM_TMP:$PATH"
CLAUDE_HOOK="$ROOT/hooks/session-card-inject.sh"
CODEX_START="$ROOT/adapters/codex/hooks/sessionstart-lifecycle.py"
CODEX_PROMPT="$ROOT/adapters/codex/hooks/userprompt-lifecycle.py"
PLUGIN="$ROOT/adapters/opencode/plugins/hearting-guards.js"

proj() { mkdir -p "$TMP/proj/$1"; printf '%s' "$TMP/proj/$1"; }
write_card() { # harness sid project mark
  python3 "$TIDY" card --harness "$1" --session-id "$2" --cwd "$3" --text "$4" >/dev/null
}
claude_hook() { # event sid project [source] [prompt]
  local event=$1 sid=$2 cwd=$3 source=${4:-} prompt=${5-hello}
  python3 - "$event" "$sid" "$cwd" "$source" "$prompt" <<'PY' | "$CLAUDE_HOOK"
import json, sys
event, sid, cwd, source, prompt = sys.argv[1:6]
payload = {"hook_event_name": event, "session_id": sid, "cwd": cwd}
if source:
    payload["source"] = source
if event == "UserPromptSubmit":
    payload["prompt"] = prompt
print(json.dumps(payload))
PY
}
context_of() { # file -> additionalContext (empty when no JSON)
  python3 - "$1" <<'PY'
import json, sys
text = open(sys.argv[1], encoding="utf-8").read().strip()
if text:
    print(json.loads(text.splitlines()[0])["hookSpecificOutput"]["additionalContext"])
PY
}
has() { grep -Fq -- "$2" "$1"; }

# ---- Claude ------------------------------------------------------------------
P=$(proj claude-basic)
write_card claude author-A "$P" "CARD-MARK-claude01"
claude_hook SessionStart claude-B "$P" startup > "$TMP/c1.out"
context_of "$TMP/c1.out" > "$TMP/c1.ctx"
if has "$TMP/c1.ctx" CARD-MARK-claude01 && grep -Fq '"hookEventName": "SessionStart"' "$TMP/c1.out"; then
  ok "claude: a new session's start receives the card once"
else bad "claude: new session start should receive the card"; fi
claude_hook UserPromptSubmit claude-B "$P" > "$TMP/c2.out"
[ ! -s "$TMP/c2.out" ] && ok "claude: the same session's next prompt gets no second copy" \
  || bad "claude: card must be consumed once"

P=$(proj claude-author)
write_card claude author-A "$P" "CARD-MARK-claude02"
claude_hook UserPromptSubmit author-A "$P" > "$TMP/c3.out"
[ ! -s "$TMP/c3.out" ] && ok "claude: the authoring session's ordinary prompt gets nothing" \
  || bad "claude: author's ordinary prompt must not consume the card"
claude_hook SessionStart author-A "$P" compact > "$TMP/c4.out"
context_of "$TMP/c4.out" | grep -Fq CARD-MARK-claude02 \
  && ok "claude: the authoring session receives the card once after compact" \
  || bad "claude: author should receive the card after compact"
claude_hook UserPromptSubmit author-A "$P" > "$TMP/c4b.out"
[ ! -s "$TMP/c4b.out" ] && ok "claude: no repeat after the post-compact delivery" \
  || bad "claude: post-compact delivery must be once"

P=$(proj claude-nocard)
claude_hook SessionStart claude-N "$P" startup > "$TMP/c5.out"
claude_hook UserPromptSubmit claude-N "$P" > "$TMP/c5b.out"
[ ! -s "$TMP/c5.out" ] && [ ! -s "$TMP/c5b.out" ] && ok "claude: no card means no output" \
  || bad "claude: no card must be silent"

P=$(proj claude-worker)
write_card claude author-A "$P" "CARD-MARK-worker"
python3 - "$P" > "$TMP/w.payload" <<'PY'
import json, sys
print(json.dumps({"hook_event_name": "SessionStart", "session_id": "claude-W", "cwd": sys.argv[1]}))
PY
AGENT_SESSION_ROLE=worker "$CLAUDE_HOOK" < "$TMP/w.payload" > "$TMP/w1.out"
AGENT_DISPATCH_DEPTH=2 "$CLAUDE_HOOK" < "$TMP/w.payload" > "$TMP/w2.out"
claude_hook SessionStart claude-M "$P" startup > "$TMP/w3.out"
if [ ! -s "$TMP/w1.out" ] && [ ! -s "$TMP/w2.out" ] && context_of "$TMP/w3.out" | grep -Fq CARD-MARK-worker; then
  ok "claude: a worker receives nothing and does not consume the card"
else bad "claude: worker must not receive or consume the card"; fi

# Short/empty prompt, no memory tool, zero candidates: the card still arrives.
P=$(proj claude-empty)
write_card claude author-A "$P" "CARD-MARK-claude03"
rm -rf "$MEM_STORE"
claude_hook UserPromptSubmit claude-E "$P" "" "" > "$TMP/c6.out"
if context_of "$TMP/c6.out" | grep -Fq CARD-MARK-claude03 \
  && grep -Fq '"hookEventName": "UserPromptSubmit"' "$TMP/c6.out"; then
  ok "claude: an empty prompt with no memory store still delivers the card"
else bad "claude: empty prompt must deliver the card"; fi
P=$(proj claude-nomem)
write_card claude author-A "$P" "CARD-MARK-claude04"
if MEM_PY=/nonexistent/mem.py claude_hook UserPromptSubmit claude-F "$P" "" "ok" > "$TMP/c7.out" \
  && context_of "$TMP/c7.out" | grep -Fq CARD-MARK-claude04; then
  ok "claude: a missing memory tool does not block the card"
else bad "claude: card must not depend on the memory tool"; fi

# Missing helper file or broken payload: silent, exit 0.
printf 'not json' | "$CLAUDE_HOOK" > "$TMP/c8.out" && [ ! -s "$TMP/c8.out" ] \
  && ok "claude: a broken payload is silent" || bad "claude: broken payload must be silent"
NOTOOL="$TMP/notool"
mkdir -p "$NOTOOL/hooks" "$NOTOOL/utilities"
cp "$CLAUDE_HOOK" "$NOTOOL/hooks/"
cp "$ROOT/utilities/agent-home.sh" "$NOTOOL/utilities/"
if claude_hook SessionStart claude-X "$P" startup | AGENT_HOME="$NOTOOL" "$NOTOOL/hooks/session-card-inject.sh" > "$TMP/c9.out" \
  && [ ! -s "$TMP/c9.out" ]; then
  ok "claude: a missing session_tidy.py is silent"
else bad "claude: missing helper must be silent"; fi

# Registration: its own command entry on the two existing events, none on the
# memory recall hook.
python3 - "$ROOT" <<'PY' && ok "claude: settings register the card hook as its own entry on SessionStart and UserPromptSubmit" || bad "claude: settings registration"
import json, sys
root = sys.argv[1]
hooks = json.load(open(root + "/adapters/claude/settings.json", encoding="utf-8"))["hooks"]
for event in ("SessionStart", "UserPromptSubmit"):
    groups = [g for g in hooks[event]
              if any("session-card-inject.sh" in h.get("command", "") for h in g["hooks"])]
    assert len(groups) == 1 and len(groups[0]["hooks"]) == 1, event
assert "session-card-inject" not in open(root + "/hooks/mem-recall-inject.sh", encoding="utf-8").read()
PY

# ---- Codex -------------------------------------------------------------------
codex_run() { # script event sid project [source] [prompt]  (extra env via caller)
  local script=$1 event=$2 sid=$3 cwd=$4 source=${5:-} prompt=${6-hello}
  python3 - "$event" "$sid" "$cwd" "$source" "$prompt" <<'PY' | python3 "$script" 2>/dev/null
import json, sys
event, sid, cwd, source, prompt = sys.argv[1:6]
payload = {"hook_event_name": event, "session_id": sid, "cwd": cwd}
if source:
    payload["source"] = source
if event == "UserPromptSubmit":
    payload["prompt"] = prompt
print(json.dumps(payload))
PY
}

P=$(proj codex-start)
write_card codex author-A "$P" "CARD-MARK-codex01"
codex_run "$CODEX_START" SessionStart codex-B "$P" startup > "$TMP/x1.out"   # CODEX_SESSION_MEMORY_INJECT unset
if context_of "$TMP/x1.out" | grep -Fq CARD-MARK-codex01; then
  ok "codex SessionStart: the card arrives with CODEX_SESSION_MEMORY_INJECT off"
else bad "codex SessionStart: card must not need the memory opt-in"; fi
codex_run "$CODEX_START" SessionStart codex-B "$P" resume > "$TMP/x1b.out"
context_of "$TMP/x1b.out" | grep -Fq CARD-MARK-codex01 \
  && bad "codex SessionStart: consumed card must not repeat" \
  || ok "codex SessionStart: a consumed card does not repeat"

P=$(proj codex-start-on)
write_card codex author-A "$P" "CARD-MARK-codex02"
if CODEX_SESSION_MEMORY_INJECT=1 codex_run "$CODEX_START" SessionStart codex-C "$P" startup > "$TMP/x2.out" \
  && context_of "$TMP/x2.out" | grep -Fq CARD-MARK-codex02; then
  ok "codex SessionStart: the card arrives with the memory opt-in on too"
else bad "codex SessionStart: card with opt-in on"; fi

P=$(proj codex-prompt)
write_card codex author-A "$P" "CARD-MARK-codex03"
codex_run "$CODEX_PROMPT" UserPromptSubmit codex-D "$P" "" "" > "$TMP/x3.out"    # empty prompt, no candidates
if context_of "$TMP/x3.out" | grep -Fq CARD-MARK-codex03 \
  && grep -Fq '"hookEventName": "UserPromptSubmit"' "$TMP/x3.out"; then
  ok "codex UserPromptSubmit: an empty prompt still delivers the card"
else bad "codex UserPromptSubmit: empty prompt must deliver the card"; fi
codex_run "$CODEX_PROMPT" UserPromptSubmit codex-D "$P" "" "next question" > "$TMP/x3b.out"
context_of "$TMP/x3b.out" | grep -Fq CARD-MARK-codex03 \
  && bad "codex UserPromptSubmit: card must be once" \
  || ok "codex UserPromptSubmit: the next prompt gets no second copy"

P=$(proj codex-prompt-author)
write_card codex author-A "$P" "CARD-MARK-codex04"
codex_run "$CODEX_PROMPT" UserPromptSubmit author-A "$P" "" "ok" > "$TMP/x4.out"
context_of "$TMP/x4.out" | grep -Fq CARD-MARK-codex04 \
  && bad "codex UserPromptSubmit: author's ordinary prompt must not consume" \
  || ok "codex UserPromptSubmit: the authoring session's ordinary prompt gets nothing"

P=$(proj codex-worker)
write_card codex author-A "$P" "CARD-MARK-codex05"
AGENT_SESSION_ROLE=worker codex_run "$CODEX_START" SessionStart codex-W "$P" startup > "$TMP/x5.out"
AGENT_SESSION_ROLE=worker codex_run "$CODEX_PROMPT" UserPromptSubmit codex-W "$P" "" "hi" > "$TMP/x5b.out"
codex_run "$CODEX_PROMPT" UserPromptSubmit codex-M "$P" "" "hi" > "$TMP/x5c.out"
if [ ! -s "$TMP/x5.out" ] && [ ! -s "$TMP/x5b.out" ] && context_of "$TMP/x5c.out" | grep -Fq CARD-MARK-codex05; then
  ok "codex: a worker receives nothing and does not consume the card"
else bad "codex: worker must not receive or consume the card"; fi

# ---- OpenCode plugin ---------------------------------------------------------
# chat.message consumes once per user turn; every model call of that turn
# (system.transform) re-emits the kept text; the next turn gets nothing. One card
# generation is handed over once for the whole seat: a third session gets nothing,
# a new card is handed over once more.
P=$(proj opencode)
write_card opencode author-A "$P" "CARD-MARK-open01"
write_card opencode op-A "$P" "CARD-MARK-open02"   # newest card; authored by op-A
if node --input-type=module > "$TMP/o1.out" 2> "$TMP/o1.err" <<EOF
import { AgentHarnessGuards } from "$PLUGIN"
import { execFileSync } from "node:child_process"
const plugin = await AgentHarnessGuards({ directory: "$P", worktree: "$P", client: { session: {
  get: async (request) => ({ data: { id: request.path.id, parentID: null } }),
  messages: async () => ({ data: [] }),
} } })
const call = async (sid, mark) => {
  const output = { system: [] }
  await plugin["experimental.chat.system.transform"]({ sessionID: sid, model: {} }, output)
  return output.system.join("\n").includes(mark)
}
const turn = (sid, id, text) => plugin["chat.message"]({ sessionID: sid, messageID: id }, { parts: [{ type: "text", text }] })
const say = (label, value) => console.log(label + "=" + value)
// Another session, short prompt: delivered, and re-emitted for the same turn.
await turn("op-B", "m1", "ok")
say("b1", await call("op-B", "CARD-MARK-open02"))
say("b1-again", await call("op-B", "CARD-MARK-open02"))
await turn("op-B", "m2", "second")
say("b2", await call("op-B", "CARD-MARK-open02"))
// A third session of the same seat does not get the card again.
await turn("op-C", "c1", "hello")
say("c1", await call("op-C", "CARD-MARK-open02"))
// A new card: the authoring session gets nothing on ordinary turns, once after compact.
execFileSync("python3", ["$TIDY", "card", "--harness", "opencode", "--session-id", "op-A", "--cwd", "$P", "--text", "CARD-MARK-open05"])
await turn("op-A", "a1", "ordinary")
say("a1", await call("op-A", "CARD-MARK-open05"))
try { await plugin.event({ event: { type: "session.compacted", properties: { sessionID: "op-A" } } }) } catch (e) {}
await turn("op-A", "a2", "after compact")
say("a2", await call("op-A", "CARD-MARK-open05"))
say("a2-again", await call("op-A", "CARD-MARK-open05"))
await turn("op-A", "a3", "later")
say("a3", await call("op-A", "CARD-MARK-open05"))
await turn("op-C", "c2", "again")
say("c2", await call("op-C", "CARD-MARK-open05"))
EOF
then
  for expect in b1=true b1-again=true b2=false c1=false a1=false a2=true a2-again=true a3=false c2=false; do
    if grep -Fxq "$expect" "$TMP/o1.out"; then ok "opencode: $expect"; else bad "opencode: expected $expect [got: $(tr '\n' ' ' < "$TMP/o1.out")]"; fi
  done
elif grep -Fq 'spawnSync python3 EPERM' "$TMP/o1.err"; then
  OPENCODE_NODE_CHILD_BLOCKED=1
  ok "opencode: Node child-process check skipped (sandbox returned EPERM)"
else bad "opencode plugin card scenario failed [err=$(head -c 400 "$TMP/o1.err")]"; fi

P=$(proj opencode-empty)
write_card opencode author-A "$P" "CARD-MARK-open03"
if [ "${OPENCODE_NODE_CHILD_BLOCKED:-0}" = 1 ]; then
  ok "opencode: empty-prompt Node check skipped (sandbox returned EPERM)"
elif node --input-type=module > "$TMP/o2.out" 2> "$TMP/o2.err" <<EOF
import { AgentHarnessGuards } from "$PLUGIN"
const plugin = await AgentHarnessGuards({ directory: "$P", worktree: "$P" })
await plugin["chat.message"]({ sessionID: "op-E", messageID: "e1" }, { parts: [] })
const output = { system: [] }
await plugin["experimental.chat.system.transform"]({ sessionID: "op-E", model: {} }, output)
console.log("empty=" + output.system.join("\n").includes("CARD-MARK-open03"))
EOF
then
  grep -Fxq empty=true "$TMP/o2.out" && ok "opencode: an empty prompt with zero candidates still delivers the card" \
    || bad "opencode: empty prompt must deliver the card"
elif grep -Fq 'spawnSync python3 EPERM' "$TMP/o2.err"; then
  OPENCODE_NODE_CHILD_BLOCKED=1
  ok "opencode: empty-prompt Node check skipped (sandbox returned EPERM)"
else bad "opencode empty-prompt scenario failed [err=$(head -c 400 "$TMP/o2.err")]"; fi

P=$(proj opencode-worker)
write_card opencode author-A "$P" "CARD-MARK-open04"
if AGENT_SESSION_ROLE=worker node --input-type=module > "$TMP/o3.out" 2> "$TMP/o3.err" <<EOF
import { AgentHarnessGuards } from "$PLUGIN"
const plugin = await AgentHarnessGuards({ directory: "$P", worktree: "$P" })
await plugin["chat.message"]({ sessionID: "op-W", messageID: "w1" }, { parts: [{ type: "text", text: "hi" }] })
const output = { system: [] }
await plugin["experimental.chat.system.transform"]({ sessionID: "op-W", model: {} }, output)
console.log("worker=" + output.system.join("\n").includes("CARD-MARK-open04"))
EOF
then
  grep -Fxq worker=false "$TMP/o3.out" && ok "opencode: a worker gets no card" || bad "opencode: worker must get no card"
else bad "opencode worker scenario failed [err=$(head -c 400 "$TMP/o3.err")]"; fi
if [ ! -e "$XDG_STATE_HOME/hearting/session-tidy/sessions" ] || ! grep -Rqs '"sid": "op-W"' "$XDG_STATE_HOME/hearting/session-tidy/sessions"; then
  ok "opencode: a worker leaves no ledger row"
else bad "opencode: worker must leave no ledger row"; fi

if [ "$FAILS" -ne 0 ]; then
  printf 'FAIL: %s case(s)\n' "$FAILS"
  exit 1
fi
printf 'PASS: session card bridges\n'
