#!/usr/bin/env bash
# dispatch-headless.sd15.test.sh — SD-15 (OPERATIONS §5.10 ⑨) limit-사망 즉시 감지 회귀 (opencode).
#   Homomorphic with the claude/codex SD-15 tests. --start 가 preflight projection 게이트를
#   거치므로 이식된 SD-15 헬퍼를 import 로 직접 구동해 검증한다:
#   (1) scan_death 패턴/reset (2) clean-exit limit-death → row done,note=dead-<reason> 마감 + 캐시
#   (3) clean 조기 exit(비-limit)는 row open 유지 (4) ADAPTATION: hang-on-limit(#8203)은 watch 를
#   벗어나 row open 유지(→ liveness 담당) (5) liveness log_shows_limit 이 그 hang 로그를 DEAD 로 잡음.
set -uo pipefail
# Hermetic fixture: a managed caller may export its real immutable registry.
# This test builds an isolated registry under $AH and must not inherit that row set.
unset AGENT_DISPATCH_JOBS
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WRAP="$SCRIPT_DIR/dispatch-headless.py"
LIVE="$SCRIPT_DIR/dispatch-liveness.py"
fails=0
ok()  { printf 'ok   - %s\n' "$1"; }
bad() { printf 'FAIL - %s\n' "$1"; fails=$((fails + 1)); }

# --- Unit: scan_death ---
u=$(python3 - "$WRAP" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("dh", sys.argv[1])
dh = importlib.util.module_from_spec(spec); spec.loader.exec_module(dh)
cases = {
  "Selected model is at capacity": ("capacity", ""),
  "You've hit your session limit · resets 3pm": ("session-limit", "3pm"),
  "Provider Rate Limit exceeded [retrying in 15s attempt #5]": ("usage-limit", ""),
  "API rate limited (429)": ("usage-limit", ""),
  "not logged in": ("auth", ""),
  "permission requested: external_directory (/tmp/opencode/*); auto-rejecting": ("permission-reject", ""),
  "all good, work complete": None,
}
bad = 0
for text, want in cases.items():
    got = dh.scan_death(text)
    if got != want:
        print(f"MISMATCH {text!r}: got {got} want {want}"); bad += 1
prose = "The implementation discusses Selected model is at capacity handling " + ("x" * 180)
if dh.scan_anchored_death(prose) is not None:
    print("MISMATCH capacity report prose was treated as terminal"); bad += 1
short_prose = "Handled Selected model is at capacity errors."
if dh.scan_anchored_death(short_prose) is not None:
    print("MISMATCH short capacity report prose was treated as terminal"); bad += 1
if dh.scan_anchored_death('{"type":"error","message":"Selected model is at capacity"}') != ("capacity", ""):
    print("MISMATCH structured terminal capacity event was missed"); bad += 1
# R10: an ANSI-decorated reject line must not be dropped by the 200-char cut
# just because the escape codes push its raw length over 200 -- the line is
# tuned so raw length > 200 but ANSI-stripped length <= 200.
ansi_line = "\x1b[93m\x1b[1m! \x1b[0mpermission requested: external_directory (" + ("x" * 134) + "/*); auto-rejecting"
if len(ansi_line) <= 200:
    print("MISMATCH fixture line is not actually over 200 raw bytes"); bad += 1
if len(dh._ANSI_RE.sub("", ansi_line)) > 200:
    print("MISMATCH fixture line is not actually <=200 bytes after stripping ANSI"); bad += 1
if dh.scan_anchored_death(ansi_line) != ("permission-reject", ""):
    print(f"MISMATCH ANSI-decorated long reject line was dropped: {dh.scan_anchored_death(ansi_line)!r}"); bad += 1
print("SCAN_OK" if bad == 0 else "SCAN_FAIL")
PY
)
echo "$u" | grep -q SCAN_OK && ok "scan_death patterns (opencode provider-rate-limit/429) + reset" || bad "scan_death: $u"

command -v git >/dev/null || { echo "(git 없음 — skip launch cases)"; exit $fails; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
AH="$tmp/agent_setting"; mkdir -p "$AH/.dispatch/logs"

drive=$(python3 - "$WRAP" "$AH" <<'PY'
import importlib.util, subprocess, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("dh", sys.argv[1])
dh = importlib.util.module_from_spec(spec); spec.loader.exec_module(dh)
AH = Path(sys.argv[2])
jobs = AH / ".dispatch" / "jobs.log"
logs = AH / ".dispatch" / "logs"

def row(slug, wt, attempt=""):
    pipe = "capability=code-plan,mode=dev,qa=standard,intensity=standard,attempt_schema_version=2,dispatch_depth=2,transport=headless,execution_surface=registered-headless,registered_worker=1,fallback_hop=same-harness-headless,harness=opencode,parent=oc,worker_role=code-plan,owner=autopilot-code,model=openai/gpt-5"
    if attempt:
        pipe += f",attempt_id={attempt}"
    with jobs.open("a", encoding="utf-8") as f:
        f.write(f"2026-07-10T00:00:00Z\topen\t/repo\t{wt}\t{slug}\t{pipe}\n")

def launch(slug, body, watch, attempt=""):
    wt = f"/wt/{slug}"
    row(slug, wt, attempt)
    log_path = logs / f"{slug}.opencode.jsonl"
    proc = subprocess.Popen(["sh", "-c", f"( {body} ) >> {log_path} 2>&1"], start_new_session=True)
    death = dh.watch_early_death(proc, log_path, watch)
    if death:
        reason, reset = death
        dh.close_job_row(jobs, slug, wt, reason, reset, attempt or None)
        if reason != "capacity":
            dh.write_reset_cache(AH, "opencode", reason, reset)
        return f"early_death={reason}:{reset}"
    return "early_death=-"

print(launch("limit1", "echo \"You've hit your session limit · resets 3pm\"; exit 1", 6))
print(launch("capacity1", "echo 'Selected model is at capacity'; exit 1", 4, "att-capacity0001"))
print(launch("capacityhang", "echo 'Selected model is at capacity'; sleep 5", 2, "att-capacityhang1"))
print(launch("clean1", "echo ok done; exit 0", 4))
# hang-on-limit (#8203): prints the limit line but does NOT exit within the watch window.
print("hang1=" + launch("hang1", "echo 'API rate limited (429)'; sleep 5", 1))
PY
)

echo "$drive" | grep -q 'early_death=session-limit:3pm' \
  && grep -q $'\tdone\t.*note=dead-session-limit,reset=3pm' "$AH/.dispatch/jobs.log" \
  && ok "clean-exit limit-death → row done,note=dead-session-limit,reset=3pm" \
  || bad "limit-death row not closed. drive=[$drive] jobs=[$(cat "$AH/.dispatch/jobs.log")]"
[ -f "$AH/.dispatch/usage-reset.opencode" ] && ok "reset cache written" || bad "no reset cache"

echo "$drive" | grep -q 'early_death=capacity:' \
  && awk -F'\t' '$2=="done" && $5=="capacity1" && $6 ~ /(^|,)attempt_id=att-capacity0001(,|$)/ && $6 ~ /(^|,)note=dead-capacity(,|$)/ && $6 ~ /(^|,)failure_class=capacity(,|$)/ { found=1 } END { exit !found }' "$AH/.dispatch/jobs.log" \
  && awk -F'\t' '$2=="done" && $5=="capacityhang" && $6 ~ /(^|,)attempt_id=att-capacityhang1(,|$)/ && $6 ~ /(^|,)note=dead-capacity(,|$)/ && $6 ~ /(^|,)failure_class=capacity(,|$)/ { found=1 } END { exit !found }' "$AH/.dispatch/jobs.log" \
  && ok "capacity death closes the exact attempt as dead-capacity" \
  || bad "capacity row not closed exactly. drive=[$drive] jobs=[$(cat "$AH/.dispatch/jobs.log")]"

echo "$drive" | grep -q 'early_death=-' \
  && awk -F'\t' '$5=="clean1"{print $2}' "$AH/.dispatch/jobs.log" | grep -qx open \
  && ok "clean fast exit → row stays open" \
  || bad "clean exit wrongly closed. drive=[$drive]"

# ADAPTATION disclosure: hang-on-limit escapes the launch watch → row stays open.
echo "$drive" | grep -q 'hang1=early_death=-' \
  && awk -F'\t' '$5=="hang1"{print $2}' "$AH/.dispatch/jobs.log" | grep -qx open \
  && ok "hang-on-limit (#8203) escapes launch watch → row open (liveness owns it)" \
  || bad "hang case unexpected. drive=[$drive]"

# Axis 6 (SD-15b): liveness log_shows_limit catches that same hung log as DEAD.
live=$(python3 - "$LIVE" "$AH" <<'PY'
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("lv", sys.argv[1])
lv = importlib.util.module_from_spec(spec); spec.loader.exec_module(lv)
AH = Path(sys.argv[2])
hit = lv.log_shows_limit(AH, "hang1")
miss = lv.log_shows_limit(AH, "clean1")
print("LIVE_OK" if (hit is not None and miss is None) else f"LIVE_FAIL hit={hit} miss={miss}")
PY
)
echo "$live" | grep -q LIVE_OK && ok "liveness log_shows_limit: hung limit log DEAD, clean log alive" || bad "liveness: $live"

echo "— opencode dispatch-headless SD-15 conformance: $([ $fails -eq 0 ] && echo PASS || echo "FAIL ($fails)")"
exit $fails
