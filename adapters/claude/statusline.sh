#!/usr/bin/env bash
# Custom statusline (의존성 없음, python3 로 stdin JSON 파싱).
# 표시: 디렉토리 │ git 브랜치 │ context │ 모델·effort + 5h/7d 사용량 (세로선 파티션).
# 입력: stdin JSON (Claude Code statusLine schema). 출력: 한 줄.
# 5h/7d 사용량 = stdin 의 rate_limits.{five_hour,seven_day}.used_percentage — /usage 와 동일한 공식 값.
# context     = stdin 의 context_window.used_percentage (공식 값) — 부재 시 current_usage/context_window_size, 최후 fallback 만 id "1m" 추측.
set -euo pipefail
AGENT_HOME="${AGENT_HOME:-${CLAUDE_HOME:-$HOME/.claude}}"
input=$(cat)
printf '%s' "$input" > "$AGENT_HOME/.statusline-last.json" 2>/dev/null || true  # 디버그·필드 탐사용 (최신 입력 1건)

eval "$(printf '%s' "$input" | python3 -c '
import sys, json, shlex
try: d = json.load(sys.stdin)
except Exception: d = {}
cwd = d.get("cwd") or (d.get("workspace") or {}).get("current_dir") or "."
m = d.get("model") or {}
model = m.get("display_name") or m.get("id") or "?"
mid = (m.get("id") or "").lower()
effort = ((d.get("effort") or {}).get("level") or "")  # reasoning effort: low|medium|high|xhigh|max
sid = d.get("session_id") or ""
session_name = d.get("session_name") or ""
tpath = d.get("transcript_path") or ""
rl = d.get("rate_limits") or {}
def rlpct(k):
    v = (rl.get(k) or {}).get("used_percentage")
    return str(int(v)) if isinstance(v, (int, float)) else ""
import time
def rlrem(k):
    rs = (rl.get(k) or {}).get("resets_at")
    if not isinstance(rs, (int, float)): return ""
    s = int(rs - time.time())
    if s <= 0: return ""
    dd, r = divmod(s, 86400); hh, r = divmod(r, 3600); mm = r // 60
    return f"{dd}d{hh}h" if dd else (f"{hh}h{mm:02d}m" if hh else f"{mm}m")
# per-model 사용량 버킷 → "fable:57 opus:12" 꼴. 두 스키마 모두 수용 (2.1.198 번들 확인):
#  1. named 형제 키: seven_day_opus/sonnet · seven_day_overage_included("Fable 5 limit") · oauth_apps
#  2. model_scoped 배열: [{display_name:"Fable", utilization:0..1, resets_at:str}]
ms_parts = []
for k, lbl in (("seven_day_opus", "opus"), ("seven_day_sonnet", "sonnet"),
               ("seven_day_overage_included", "fable"), ("seven_day_oauth_apps", "apps")):
    v = (rl.get(k) or {}).get("used_percentage")
    if isinstance(v, (int, float)):
        ms_parts.append(f"{lbl}:{round(v)}")
for e in (rl.get("model_scoped") or []):
    if not isinstance(e, dict): continue
    u = e.get("utilization")
    if not isinstance(u, (int, float)): continue
    lbl = (e.get("display_name") or "model").split()[0].lower()
    if not any(p.startswith(lbl + ":") for p in ms_parts):
        ms_parts.append(f"{lbl}:{round(u * 100)}")
cwin = d.get("context_window") or {}
cw = cwin.get("current_usage") or {}
ctok = cw.get("input_tokens", 0) + cw.get("cache_creation_input_tokens", 0) + cw.get("cache_read_input_tokens", 0)
# used_percentage/context_window_size 가 stdin 공식 값 — id 의 "1m" 추측은 fallback 만 (Fable 5 1M 을 200k 로 나눠 42% 오표시한 건, 2026-06-11)
up = cwin.get("used_percentage")
win = cwin.get("context_window_size") or (1_000_000 if "1m" in mid else 200_000)
cpct = min(99, round(up)) if isinstance(up, (int, float)) else (min(99, round(100 * ctok / win)) if ctok else -1)
print("S_CWD=" + shlex.quote(cwd))
print("S_MS=" + shlex.quote(" ".join(ms_parts)))
print("S_MODEL=" + shlex.quote(model))
print("S_EFFORT=" + shlex.quote(effort))
print("S_SID=" + shlex.quote(sid))
print("S_SESSION_NAME=" + shlex.quote(session_name if isinstance(session_name, str) else ""))
print("S_TRANSCRIPT=" + shlex.quote(tpath))
print("S_5H=" + shlex.quote(rlpct("five_hour")))
print("S_7D=" + shlex.quote(rlpct("seven_day")))
print("S_5H_RST=" + shlex.quote(rlrem("five_hour")))
print("S_7D_RST=" + shlex.quote(rlrem("seven_day")))
print("S_CTX=" + shlex.quote(str(cpct)))
' 2>/dev/null)"
: "${S_CWD:=$PWD}" "${S_MODEL:=?}" "${S_EFFORT:=}" "${S_SID:=}" "${S_SESSION_NAME:=}" "${S_5H:=}" "${S_7D:=}" "${S_5H_RST:=}" "${S_7D_RST:=}" "${S_CTX:=-1}" "${S_MS:=}" "${S_TRANSCRIPT:=}"

# F-87 shared session handle projection; unavailable helpers are deliberately silent.
S_SESSION_DISPLAY=""
session_helper="$AGENT_HOME/tools/fleet/session_handle.py"
[ -f "$session_helper" ] || session_helper="$(dirname "$AGENT_HOME")/tools/fleet/session_handle.py"
if [ -n "$S_SID" ] && [ -f "$session_helper" ]; then
  S_SESSION_DISPLAY=$(python3 - "$session_helper" "$S_SID" "$S_SESSION_NAME" <<'PY' 2>/dev/null || true
import importlib.util, sys
try:
    spec = importlib.util.spec_from_file_location("fleet_session_handle", sys.argv[1])
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    print(mod.session_display_name("claude", sys.argv[2], sys.argv[3], budget=48))
except Exception:
    pass
PY
  )
fi

# §5 per-session statusline tap (agent-fleet-dashboard) — 멀티세션 대시보드(tools/fleet)가
# 세션별 telemetry 를 읽게 stdin JSON 을 세션별 파일로도 dump. 단일 .statusline-last.json 은
# last-writer-wins(모든 세션 덮어씀)이라 세션 구분 불가 → 세션별 파일이 필요. 우리 소유 파일 write
# 라 zero-injection 원칙 위배 아님(§0.5). 기존 렌더·단일파일 무영향(순수 추가).
if [ -n "$S_SID" ]; then
  sldir="$AGENT_HOME/.statusline"
  mkdir -p "$sldir" 2>/dev/null || true
  # 소유 claude pid + /proc starttime 을 tap 에 additive 주입 (F-25 tier-2 식별 증거) —
  # sessions/<pid>.json registry 가 살아있는 세션에서 소실되면(2026-07-20 실측) fleet 이
  # pid→sid 를 복구할 유일한 소스가 이 tap 이다. proc_start 는 PID 재사용 오귀속 방지 짝
  # (F-26). 조상에서 claude 를 못 찾으면 필드 생략(F-3 정직 결손). /proc/<p>/stat 은 comm
  # 괄호 필드 뒤로 잘라 파싱한다(comm 에 공백 포함 가능 — field 위치 어긋남 함정).
  tap_pid=""; tap_start=""
  p=$PPID
  for _ in 1 2 3 4 5; do
    [ -n "$p" ] && [ "$p" != 1 ] && [ -r "/proc/$p/comm" ] || break
    if [ "$(cat "/proc/$p/comm" 2>/dev/null)" = "claude" ]; then
      st=$(cat "/proc/$p/stat" 2>/dev/null) || break
      st=${st##*) }                       # comm 뒤 (state 부터); ppid=2번째, starttime=20번째
      # shellcheck disable=SC2086
      set -- $st
      if [ "$#" -ge 20 ]; then tap_pid=$p; tap_start=${20}; fi
      break
    fi
    st=$(cat "/proc/$p/stat" 2>/dev/null) || break
    st=${st##*) }
    # shellcheck disable=SC2086
    set -- $st
    [ "$#" -ge 2 ] || break
    p=$2
  done
  tap_json=$input
  case "$tap_pid$tap_start" in
    ''|*[!0-9]*) ;;                       # 미확보·비정상 값 → 원문 그대로 (주입 생략)
    *) case "$input" in
         '{"'*) tap_json='{"pid":'"$tap_pid"',"proc_start":"'"$tap_start"'",'"${input#\{}" ;;
       esac ;;
  esac
  # atomic write (temp + rename) so a concurrent reader (tools/fleet dashboard) never sees a
  # half-written file → JSON parse fail → telemetry ('$cost' etc.) flickers to '—' for a tick.
  if printf '%s' "$tap_json" > "$sldir/.$S_SID.json.tmp" 2>/dev/null; then
    mv -f "$sldir/.$S_SID.json.tmp" "$sldir/$S_SID.json" 2>/dev/null || true
  fi
  # stale 청소: mtime > 1일 세션 파일 제거(디렉토리 폭증 방지 — 종료된 세션 잔존물). find 없으면 skip.
  find "$sldir" -maxdepth 1 \( -name '*.json' -o -name '.*.json.tmp' \) -mtime +1 -delete 2>/dev/null || true
fi

# §4.7 F-17/F-21 공용 fleet 제목 refresher 트리거 — Claude statusline debounce surface.
# 조건: working main은 120s, NOW 실패는 30/60/120s backoff 뒤 재시도한다.
# 재귀가드: refresher 의 claude -p 는 statusline 미실행 + FLEET_TITLE_REFRESH env 이중.
# 전역 동시성/rolling-start budget은 refresh_title.py가 모든 진입점에 공통 적용한다.
# kill switch는 shell에서도 먼저 확인해 비활성 상태에 Python조차 spawn하지 않는다.
# 반드시 즉시 반환 — 판정은 stat 2회, 실행은 detached setsid.
title_root="${FLEET_TITLE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-fleet/titles}"
if [ -n "$S_SID" ] && [ -n "${S_TRANSCRIPT:-}" ] && [ "${FLEET_TITLE_REFRESH:-}" != "1" ] \
   && [ "${FLEET_TITLE_DISABLE:-}" != "1" ] && [ ! -e "$title_root/.refresh-disabled" ] \
   && [ -f "$S_TRANSCRIPT" ] && command -v python3 >/dev/null 2>&1; then
  refresher="$AGENT_HOME/tools/fleet/refresh_title.py"
  [ -f "$refresher" ] || refresher="$(dirname "$AGENT_HOME")/tools/fleet/refresh_title.py"
  title_dir="$title_root/claude"
  sc="$title_dir/$S_SID.json"
  now=$(date +%s)
  scts=0; [ -f "$sc" ] && scts=$(stat -c %Y "$sc" 2>/dev/null || echo 0)
  trm=$(stat -c %Y "$S_TRANSCRIPT" 2>/dev/null || echo 0)
  sf=0
  if [ -f "$sc" ]; then
    sf=$(sed -n 's/.*"summary_failures": *\([0-9][0-9]*\).*/\1/p' "$sc" 2>/dev/null | head -n 1)
    case "$sf" in ''|*[!0-9]*) sf=0 ;; esac
  fi
  retry=120
  case "$sf" in 1) retry=30 ;; 2) retry=60 ;; 3|4|5|6|7|8|9) retry=120 ;; esac
  if [ "$scts" -eq 0 ] || [ $((now - scts)) -gt "$retry" ]; then
    # 실패 상태는 cursor가 보존됐으므로 transcript mtime과 무관하게 같은 delta를 재시도한다.
    if [ "$scts" -eq 0 ] || [ "$trm" -gt "$scts" ] || [ "$sf" -gt 0 ]; then
      lockdir="$title_dir/.lock-$S_SID"
      if [ -f "$refresher" ] && mkdir -p "$title_dir" 2>/dev/null \
         && mkdir "$lockdir" 2>/dev/null; then
        # 세션당 동시 1개. child 가 trap 으로 lock 해제. 즉시 반환(&).
        # 서브셸 자체도 stdout/stderr 를 /dev/null 로 돌려야 한다 — 안 그러면 서브셸이
        # statusline 의 stdout pipe fd 를 그대로 물고 있어(내부 setsid 커맨드만 리다이렉트해도
        # 서브셸 프로세스 자신은 fork 시점에 상속한 fd 를 계속 들고 있음), 호출자가 stdout 을
        # 파이프로 읽을 때(Claude Code 의 실제 statusLine 소비 방식) EOF 가 refresher 종료까지
        # 지연돼 "즉시 반환" 불변식이 깨진다.
        ( trap 'rmdir "$lockdir" 2>/dev/null || true' EXIT
          priority_arg=
          quota_arg=
          if [ "$scts" -eq 0 ] || [ "$sf" -gt 0 ]; then
            priority_arg=--priority
          fi
          if [ "$scts" -eq 0 ]; then
            quota_arg='--quota-class initial'
          fi
          FLEET_TITLE_REFRESH=1 setsid python3 "$refresher" \
            --harness claude --sid "$S_SID" --transcript "$S_TRANSCRIPT" $priority_arg $quota_arg >/dev/null 2>&1 </dev/null
        ) >/dev/null 2>&1 &
      fi
    fi
  fi
fi

dir=$(basename "$S_CWD")

# git 브랜치 + 위험 상태 플래그 (⚠️ merge/rebase 진행 중 · 💀 머지 완료된 죽은 브랜치)
branch=""; gflag=""
if command -v git >/dev/null 2>&1; then
  branch=$(git -C "$S_CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  if [ -n "$branch" ]; then
    gd=$(git -C "$S_CWD" rev-parse --git-dir 2>/dev/null || true)
    if [ -n "$gd" ]; then
      case "$gd" in /*) ;; *) gd="$S_CWD/$gd" ;; esac
      if [ -f "$gd/MERGE_HEAD" ]; then gflag="⚠️merge"
      elif [ -d "$gd/rebase-merge" ] || [ -d "$gd/rebase-apply" ]; then gflag="⚠️rebase"
      else
        def=$(git -C "$S_CWD" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@' || true); def=${def:-main}
        if [ "$branch" != "$def" ] && [ "$branch" != "HEAD" ] && git -C "$S_CWD" rev-parse -q --verify "origin/$def" >/dev/null 2>&1; then
          [ "$(git -C "$S_CWD" rev-list --count "origin/$def..HEAD" 2>/dev/null)" = "0" ] && gflag="💀merged"
        fi
      fi
    fi
  fi
fi

# ANSI 색 + 퍼센트 색 헬퍼 (녹 <50 / 황 <80 / 적 ≥80)
DIM=$'\033[2m'; CYAN=$'\033[36m'; YEL=$'\033[33m'; GRN=$'\033[32m'; RED=$'\033[31m'; MAG=$'\033[35m'; RST=$'\033[0m'
pcol(){ if [ "${1:-0}" -ge 80 ] 2>/dev/null; then printf '%s' "$RED"; elif [ "${1:-0}" -ge 50 ] 2>/dev/null; then printf '%s' "$YEL"; else printf '%s' "$GRN"; fi; }

# --- 세그먼트 배열 → 세로선(│) 파티션으로 join ---
segs_arr=()
segs_arr+=("📁 ${CYAN}${dir}${RST}")
 [ -n "$S_SESSION_DISPLAY" ] && segs_arr+=("${DIM}${S_SESSION_DISPLAY}${RST}")
if [ -n "$branch" ]; then
  bseg="${DIM}⎇${RST}${YEL}${branch}${RST}"
  [ -n "$gflag" ] && bseg="${bseg} ${RED}${gflag}${RST}"
  # 병렬 작업장 카운터 — 이 repo 에 연결된 추가 worktree 수 (§5.10 디스패치·잔존 감지)
  wt=$(git -C "$S_CWD" worktree list --porcelain 2>/dev/null | grep -c '^worktree ' || true)
  [ "${wt:-1}" -gt 1 ] 2>/dev/null && bseg="${bseg} ${YEL}🚧 $((wt-1))${RST}"
  segs_arr+=("$bseg")
else segs_arr+=("${DIM}⎇ no-git${RST}"); fi

# 도는 headless 파이프·루프 상세 ("N shells" 배지의 중간 단계 — 무엇이·얼마나·뭘 하는지)
jobs_lbl=$(COLUMNS=100000 ps -eo pid=,etime=,args= 2>/dev/null | python3 -c '
# COLUMNS 고정 필수 — CC 가 statusline env 에 터미널 폭을 넣고, ps 는 파이프여도 COLUMNS 로 args 를 잘라 /autopilot- 매칭이 전멸한다 (2026-06-11 실측)
import sys, re, os
CWD = sys.argv[1] if len(sys.argv) > 1 else ""
def related(jcwd):
    # 프로젝트 파이프는 같은 디렉토리 트리의 세션에만 표시 (전사 루프는 무조건)
    if not CWD or not jcwd: return True
    if jcwd == CWD or jcwd.startswith(CWD + "/") or CWD.startswith(jcwd + "/"): return True
    # 형제 worktree(<repo>-wt/<slug>, §5.10 디스패치)는 같은 repo 로 취급 — 세션이 repo 안일 때 worktree job 누락 방지 (2026-06-11)
    if jcwd.startswith(CWD + "-wt/") or CWD.startswith(jcwd + "-wt/"): return True
    pj, pc = os.path.dirname(jcwd), os.path.dirname(CWD)
    return pj == pc and pj.endswith("-wt")
C = {"draft":"35","apply":"35","refine":"33","code":"32","spec":"36","research":"34","lab":"96","design":"95","ship":"32","note":"37","oncall":"37","study":"37","drill":"37"}
def paint(key, s): return f"\033[{C.get(key,'33')}m{s}\033[0m"
def mins(et):
    et = et.strip(); d = 0
    if "-" in et: d, et = et.split("-", 1)
    parts = [int(x) for x in et.split(":")]
    while len(parts) < 3: parts.insert(0, 0)
    tot = int(d) * 1440 + parts[0] * 60 + parts[1]
    return f"{tot//60}h{tot%60:02d}m" if tot >= 60 else f"{tot}m"
def has_entries(p):
    try: return any(True for _ in os.scandir(p))
    except Exception: return False
def live_stage(jcwd, slug, fb):
    # worktree slug → plans/*_<slug>/ 산출물로 실제 파이프 단계 유도 (argv 라벨이 정적 → 실시간 반영). fb = argv 추정 단계.
    if not jcwd or not slug: return fb
    ar = ".agent_reports" if os.path.isdir(jcwd + "/.agent_reports") else ".claude_reports"
    base = jcwd + "/" + ar + "/plans"
    try: cand = sorted(d for d in os.listdir(base) if d.endswith("_" + slug))
    except Exception: cand = []
    if not cand:
        # slug 불일치 폴백 — 헤드리스 autopilot-code 가 plan 폴더를 _task 기반_ slug 로 만들면
        # (예: worktree=detail-perf / plan=…_detail-modal-perf) 정확 매칭이 실패해 stage 가 안 뜬다.
        # 워크트리 slug 와 하이픈 토큰 겹침이 최대인 plan 폴더로 폴백(겹침 0 이면 argv 추정 유지).
        stoks = set(t for t in slug.split("-") if t)
        try: dirs = [d for d in os.listdir(base) if not d.startswith(".") and os.path.isdir(base + "/" + d)]
        except Exception: dirs = []
        best, bestn, bestm = None, 0, -1.0
        for d in dirs:
            if os.path.exists(base + "/" + d + "/pipeline_summary.md"): continue   # 완료(done) 폴더는 폴백 후보 제외 — 새 worktree slug 가 옛 done 폴더와 generic 토큰 1개만 겹쳐 "done"으로 오인되는 것 방지(정확 매칭 경로는 보존)
            dslug = d.split("_", 1)[-1] if "_" in d else d
            n = len(stoks & set(t for t in dslug.split("-") if t))
            try: m = os.path.getmtime(base + "/" + d)
            except Exception: m = 0.0
            if n > bestn or (n == bestn and n > 0 and m > bestm):
                best, bestn, bestm = d, n, m
        if not best or bestn == 0: return fb   # plan 폴더 전 or 무관 → argv 추정 유지
        cand = [best]
    pd = base + "/" + cand[-1]
    if os.path.exists(pd + "/pipeline_summary.md"): return "done"
    if has_entries(pd + "/test_logs"): return "test"
    if has_entries(pd + "/dev_logs"): return "exec"
    try:   # dev_logs 쓰기 전 early-execute: checklist 체크 진행 = exec
        with open(pd + "/plan/checklist.md") as _f:
            if "[x]" in _f.read().lower(): return "exec"
    except Exception: pass
    try:   # 워크트리 소스 dirty = execute 중 (디자인팀 직접편집 등 dev_logs 미경유 흐름)
        import subprocess
        if subprocess.run(["git", "-C", jcwd, "status", "--porcelain", "--untracked-files=no"],
                          capture_output=True, text=True, timeout=2).stdout.strip():
            return "exec"
    except Exception: pass
    if os.path.exists(pd + "/plan/plan.md"): return "plan"
    return "plan"
seen = {}
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line: continue
    fields = line.lstrip().split(None, 2)
    if len(fields) < 3: continue
    pid, etime, args = fields
    ms = re.findall(r"/autopilot-([a-z-]+)", args)
    if ms and "claude" in args:
        try: jcwd = os.readlink(f"/proc/{pid}/cwd")
        except Exception: jcwd = ""
        if not related(jcwd): continue
        if os.path.basename(args.split(None, 1)[0]) in ("zsh", "bash", "sh", "dash"): continue  # 런처 셸 wrapper 제외 — 실 claude 프로세스만
        key = ms[-1]
        # 유효값만 매칭 + 첫 매치(실 플래그는 quoted 프롬프트 텍스트보다 앞) — \w 가 한글까지 잡아 프롬프트 안 "--qa 없으면" 을 qa 로 오인하던 버그 방지
        modes = re.findall(r"--mode ([a-z][a-z-]*)", args); md = modes[0] if modes else None
        qas = re.findall(r"--qa (quick|light|standard|thorough|adversarial)\b", args); qv = qas[0] if qas else None
        # 제목 = worktree/디렉토리 슬러그 우선 (프롬프트 어절 추출은 한국어 프롬프트에서 노이즈 — 2026-06-11)
        slug = os.path.basename(jcwd.rstrip("/")) if jcwd else ""
        desc = slug if (slug and slug != os.path.basename(CWD.rstrip("/"))) else ""
        stage = live_stage(jcwd, slug, key)
        QA = {"quick":"qck","light":"lgt","standard":"std","thorough":"thr","adversarial":"adv"}
        QAC = {"qck":"2","lgt":"32","std":"33","thr":"35","adv":"31"}
        D = "\033[2m"; R = "\033[0m"
        parts = []
        if md: parts.append(f"\033[36m{md}{R}")
        if qv:
            q = QA.get(qv, qv)
            parts.append(f"\033[{QAC.get(q,'33')}m{q}{R}")
        opts = f"{D}\u00b7{R}".join(parts)
        label = f"{key}▸{stage}" if stage != key else key
        head = paint(key, label) + (f"{D}({R}{opts}{D}){R}" if opts else "")
        lbl = head + f" {D}\u23f3{mins(etime)}{R}" + (f" {desc}" if desc else "")
        dkey = f"{key}:{slug}"  # \uc2ac\ub7ec\uadf8 \ub2e8\uc704 \u2014 \uac19\uc740 \ud30c\uc774\ud504 N\uac1c \ubcd1\ub82c \ubd84\uc0ac\uac00 \ud55c \ud56d\ubaa9\uc73c\ub85c \ubb49\uac1c\uc9c0\uc9c0 \uc54a\uac8c (2026-06-11)
    else:
        l = re.search(r"loops/(oncall|note|study|drill|runtime-watch)", args)
        if not l: continue
        key = l.group(1); lbl = paint(key, key) + f" \033[2m\u23f3{mins(etime)}\033[0m"
        dkey = key
    seen.setdefault(dkey, lbl)
out = list(seen.values())[:3]
if len(seen) > 3: out.append(f"+{len(seen)-3}")
print(" \033[1;37m/\033[0m ".join(out))
' "$S_CWD" 2>/dev/null || true)

# context gauge — mid-line ━/─ (fleet design): filled in the level color, empty track dim
if [ "${S_CTX}" -ge 0 ] 2>/dev/null; then
  cc=$(pcol "$S_CTX")
  segs=10; filled=$(( (S_CTX * segs + 50) / 100 )); [ "$filled" -gt "$segs" ] && filled=$segs
  fbar=""; i=0
  while [ "$i" -lt "$filled" ]; do fbar="${fbar}━"; i=$((i+1)); done
  ebar=""; while [ "$i" -lt "$segs" ]; do ebar="${ebar}─"; i=$((i+1)); done
  segs_arr+=("${DIM}context${RST} ${cc}${fbar}${RST}${DIM}${ebar}${RST} ${cc}${S_CTX}%${RST}")
fi

# 모델별 색 (fleet 동일 팔레트): opus=cyan · sonnet=blue · haiku=green · fable=magenta · gpt=yellow
BLU=$'\033[34m'
mcol(){ case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in
  *opus*) printf '%s' "$CYAN" ;; *sonnet*) printf '%s' "$BLU" ;; *haiku*) printf '%s' "$GRN" ;;
  *fable*) printf '%s' "$MAG" ;; *gpt*) printf '%s' "$YEL" ;; *) printf '%s' "$RST" ;; esac; }

# 모델·effort │ 5h/7d 사용량+리셋 잔여시간 (stdin rate_limits = /usage 공식 값, 분모 추측 없음)
# effort = reasoning effort(low→max) — 모델 옆 강도별 색(dim→적)으로 시각 구분
eff_disp=""
if [ -n "$S_EFFORT" ]; then
  case "$S_EFFORT" in
    low)    ecol="$DIM"; etxt="low" ;;
    medium) ecol="$GRN"; etxt="med" ;;
    high)   ecol="$YEL"; etxt="high" ;;
    xhigh)  ecol="$MAG"; etxt="xhigh" ;;
    max)    ecol="$RED"; etxt="max" ;;
    *)      ecol="$DIM"; etxt="$S_EFFORT" ;;
  esac
  eff_disp=" ${ecol}${etxt}${RST}"
fi
# model in its model-family color (fleet 동일 — per-model 구분), dotless effort
segs_arr+=("$(mcol "$S_MODEL")${S_MODEL}${RST}${eff_disp}")
u=""
[ -n "$S_5H" ] && { u="${u} ${DIM}5h${RST} $(pcol "$S_5H")${S_5H}%${RST}"; [ -n "$S_5H_RST" ] && u="${u}${DIM}(↻ ${S_5H_RST})${RST}"; }
[ -n "$S_7D" ] && { u="${u} ${DIM}7d${RST} $(pcol "$S_7D")${S_7D}%${RST}"; [ -n "$S_7D_RST" ] && u="${u}${DIM}(↻ ${S_7D_RST})${RST}"; }
# per-model 버킷 (예: fable-only 주간 한도) — 라벨은 그 모델의 색, 값은 레벨색
for ms in $S_MS; do
  lbl="${ms%%:*}"; pv="${ms##*:}"
  [ -n "$lbl" ] && [ "$pv" -ge 0 ] 2>/dev/null && u="${u} $(mcol "$lbl")${lbl}${RST} $(pcol "$pv")${pv}%${RST}"
done
[ -n "$u" ] && segs_arr+=("${u# }")

# join with 세로선 파티션
out=""; sep=" ${DIM}│${RST} "
for s in "${segs_arr[@]}"; do [ -z "$out" ] && out="$s" || out="${out}${sep}${s}"; done
[ -n "${jobs_lbl:-}" ] && out="${out}
${GRN}>_${RST}${DIM} running:${RST} ${jobs_lbl}"
printf '%s' "$out" > "$AGENT_HOME/.statusline-last-out.txt" 2>/dev/null || true  # 디버그 — 실제 렌더 시점 출력 사본
printf '%s\n' "$out"
exit 0  # 마지막 && list 가 비어있는 jobs_lbl 로 exit 1 → statusline 미표시 (2026-06-11 점검에서 발견)
