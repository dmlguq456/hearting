import argparse
import importlib.util
import json
import os
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("first_journey", Path(__file__).with_name("first-journey.py"))
fj = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fj)


class JourneyTests(unittest.TestCase):
    def test_clean_environment_is_allowlisted(self):
        env = fj.clean_env(Path("/tmp/world"), [Path("/bin")], {"CODEX_HOME": "/tmp/world/.codex"})
        self.assertEqual(set(env), {"HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                                    "XDG_CACHE_HOME", "TMPDIR", "PATH", "LANG", "TERM", "USER",
                                    "DISABLE_AUTOUPDATER", "CODEX_HOME"})
        self.assertNotIn("HERDR_SOCKET", env)
        self.assertNotIn("CLAUDECODE", env)

    def test_safe_root_rejects_home_and_nas(self):
        with self.assertRaises(ValueError):
            fj.safe_root(str(Path.home()), "root", [Path.home(), Path("/home/nas")])
        # Isolated test runners may put HOME under /tmp, so use a private tree.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve(); fake_home = base / "home"; fake_home.mkdir()
            forbidden = [fake_home, Path("/home/nas")]
            for unsafe in (fake_home / "world", base):  # inside, or an ancestor of, a protected path
                with self.assertRaises(ValueError):
                    fj.safe_root(str(unsafe), "local-root", forbidden)
            local = base / "local"
            if fj.platform.system() == "Linux" and Path.home().resolve() not in local.parents:
                self.assertEqual(fj.safe_root(str(local), "local-root", forbidden), local)

    def test_auth_allowlists_are_native_credential_files(self):
        self.assertEqual(fj.AUTH_NAMES["claude"], [".credentials.json"])
        self.assertEqual(fj.AUTH_NAMES["codex"], ["auth.json"])
        self.assertEqual(fj.AUTH_NAMES["opencode"], ["auth.json"])
        self.assertEqual(fj.auth_file(Path("/tmp/auth"), "opencode"), Path("/tmp/auth/opencode/auth.json"))
        self.assertEqual(fj.auth_file(Path("/tmp/auth"), "opencode", "v2"),
                         Path("/tmp/auth/opencode/credentials-v2.json"))

    def test_confirmation_card_gets_one_answer_then_completion(self):
        card = "[실행 확인]\n작업: fix\n이유: typo\n경로: direct\n범위: greet\n완료: test\n\n→ 진행 / 수정: <틀린 부분> / 중단"
        outputs = [json.dumps({"session_id": "s1", "type": "assistant", "message": {"role": "assistant", "content": card}}),
                   json.dumps({"session_id": "s1", "type": "assistant", "message": {"role": "assistant", "content": "Done."}})]
        calls = []
        judged = iter([False, True])
        old = fj.run
        old_sandbox = fj.sandbox
        try:
            fj.run = lambda argv, **k: calls.append(argv) or {"exit": 0, "seconds": 0.1, "output": outputs.pop(0)}
            fj.sandbox = lambda argv, *a, **k: argv
            result = fj.headless_turn("claude", "claude", "task", Path("/tmp/world/repos/demo"), Path("/tmp/world"), {}, 30, [],
                                      judge=lambda: next(judged))
        finally: fj.run = old; fj.sandbox = old_sandbox
        self.assertEqual(result["status"], "candidate-complete")
        self.assertEqual(result["canned_answers"], 1)
        self.assertEqual(calls[1][-3:], ["--resume", "s1", "진행"])

    def test_card_detection_matches_canonical_cards_only(self):
        korean = ("[실행 확인]\n\n작업: 오타 수정\n이유: 출력 오타\n경로: autopilot-code · direct — 작은 수정\n"
                  "범위: greet.py\n완료: 집중 시험 통과\n\n→ 진행 / 수정: <틀린 부분> / 중단")
        english = ("**[Run confirmation]**\n\n**Task:** Fix the typo\n**Why:** wrong output\n**Route:** autopilot-code · direct\n"
                   "**Scope:** greet.py\n**Done:** focused test passes\n\n→ Proceed / Revise: <what is wrong> / Stop")
        self.assertEqual(fj.card_answer(korean), "진행")
        self.assertEqual(fj.card_answer(english), "Proceed")
        self.assertIsNone(fj.card_answer("Should I proceed with the fix?"))
        self.assertIsNone(fj.card_answer("Task: fix\nScope: greet\n→ Proceed / Stop"))

    def test_codex_turns_resume_the_exact_thread(self):
        first = fj.turn_argv("codex", "codex", Path("/tmp/w/repos/codex"), None, "task")
        later = fj.turn_argv("codex", "codex", Path("/tmp/w/repos/codex"), "t-1", "Proceed")
        self.assertEqual(first, ["codex", "exec", "--json", "--cd", "/tmp/w/repos/codex", "task"])
        self.assertEqual(later, ["codex", "exec", "resume", "--json", "t-1", "Proceed"])
        self.assertNotIn("--sandbox", first + later)

    def test_unexpected_question_stops_without_answer(self):
        old = fj.run
        old_sandbox = fj.sandbox
        try:
            fj.run = lambda *a, **k: {"exit": 0, "seconds": 0, "output": json.dumps({"session_id":"s", "type":"assistant", "message":{"role":"assistant", "content":"Which file?"}})}
            fj.sandbox = lambda argv, *a, **k: argv
            result = fj.headless_turn("claude", "claude", "task", Path("/tmp/world/repos/demo"), Path("/tmp/world"), {}, 30, [])
        finally: fj.run = old; fj.sandbox = old_sandbox
        self.assertEqual(result["status"], "unexpected-question")
        self.assertEqual(result["canned_answers"], 0)

    def test_confirmation_answers_are_capped_at_three(self):
        card = "[Run confirmation]\nTask: fix\nWhy: typo\nRoute: direct\nScope: greet\nDone: test\n→ Proceed / Revise: <x> / Stop"
        old = fj.run
        old_sandbox = fj.sandbox
        try:
            fj.run = lambda *a, **k: {"exit": 0, "seconds": 0, "output": json.dumps({"session_id":"s", "type":"assistant", "message":{"role":"assistant", "content":card}})}
            fj.sandbox = lambda argv, *a, **k: argv
            result = fj.headless_turn("claude", "claude", "task", Path("/tmp/world/repos/demo"), Path("/tmp/world"), {}, 30, [])
        finally: fj.run = old; fj.sandbox = old_sandbox
        self.assertEqual(result["canned_answers"], 3)
        self.assertEqual(result["status"], "confirmation-cap")

    def test_unknown_telemetry_is_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = fj.telemetry(Path(tmp), "opencode")
        self.assertIsNone(result["total"])
        self.assertNotEqual(result["total"], 0)

    def test_codex_telemetry_uses_last_cumulative_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "home/.codex/rollout-1.jsonl"; path.parent.mkdir(parents=True)
            path.write_text("\n".join(json.dumps({"type":"event_msg", "payload":{"type":"token_count", "info":{"total_token_usage":{"input_tokens":n,"output_tokens":0,"total_tokens":n}}}}) for n in (10,20)))
            result = fj.telemetry(Path(tmp), "codex")
        self.assertEqual(result["total"], 20)

    def test_codex_telemetry_sums_sessions_not_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, counts in (("rollout-a.jsonl", (10, 20)), ("rollout-b.jsonl", (7,))):
                path = Path(tmp) / "home/.codex/sessions" / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(json.dumps({"type":"event_msg", "payload":{"type":"token_count", "info":{"total_token_usage":{"input_tokens":n,"output_tokens":0,"total_tokens":n}}}}) for n in counts))
            result = fj.telemetry(Path(tmp), "codex")
        self.assertEqual((result["total"], result["sessions"]), (27, 2))

    def test_opencode_telemetry_reads_session_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "home/.local/share/opencode/opencode.db"; db.parent.mkdir(parents=True)
            con = fj.sqlite3.connect(db)
            con.execute("create table session (id text, tokens_input integer, tokens_output integer, tokens_reasoning integer)")
            con.execute("insert into session values ('s', 100, 20, 5)"); con.commit(); con.close()
            result = fj.telemetry(Path(tmp), "opencode")
        self.assertEqual((result["status"], result["total"], result["sessions"]), ("measured", 125, 1))

    def test_opencode_telemetry_prefers_session_v2_and_sums_five_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "home/.local/share/opencode/opencode.db"; db.parent.mkdir(parents=True)
            con = fj.sqlite3.connect(db)
            con.execute("create table session (id text, tokens_input integer, tokens_output integer, tokens_reasoning integer)")
            con.execute("insert into session values ('old', 900, 900, 900)")
            con.execute("create table session_v2 (id text, tokens_input integer, tokens_output integer, tokens_reasoning integer, tokens_cache_read integer, tokens_cache_write integer)")
            con.executemany("insert into session_v2 values (?, ?, ?, ?, ?, ?)", [
                ("s1", 100, 20, 5, 7, 3), ("s2", 10, 2, 1, 4, 2)])
            con.commit(); con.close()
            result = fj.telemetry(Path(tmp), "opencode")
        self.assertEqual((result["status"], result["total"], result["sessions"]), ("measured", 154, 2))
        self.assertEqual((result["input"], result["output"], result["reasoning"],
                          result["cache_read"], result["cache_write"]), (110, 22, 6, 11, 5))
        self.assertEqual(result["total_formula"], "input+output+reasoning+cache_read+cache_write (session_v2 table)")

    def test_opencode_telemetry_rejects_incomplete_session_v2_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "home/.local/share/opencode/opencode.db"; db.parent.mkdir(parents=True)
            con = fj.sqlite3.connect(db)
            con.execute("create table session (id text, tokens_input integer, tokens_output integer, tokens_reasoning integer)")
            con.execute("create table session_v2 (id text, tokens_input integer)")
            con.commit(); con.close()
            result = fj.telemetry(Path(tmp), "opencode")
        self.assertEqual((result["status"], result["total"], result["reason"]),
                         ("unknown", None, "unsupported-opencode-schema"))

    def test_opencode_telemetry_reports_mixed_database_generations_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home/.local/share/opencode"; home.mkdir(parents=True)
            old_db = home / "old/opencode.db"; old_db.parent.mkdir()
            con = fj.sqlite3.connect(old_db)
            con.execute("create table session (id text, tokens_input integer, tokens_output integer, tokens_reasoning integer)")
            con.execute("insert into session values ('old', 900, 900, 900)"); con.commit(); con.close()
            new_db = home / "new/opencode.db"; new_db.parent.mkdir()
            con = fj.sqlite3.connect(new_db)
            con.execute("create table session_v2 (id text, tokens_input integer, tokens_output integer, tokens_reasoning integer, tokens_cache_read integer, tokens_cache_write integer)")
            con.execute("insert into session_v2 values ('new', 1, 2, 3, 4, 5)"); con.commit(); con.close()
            result = fj.telemetry(Path(tmp), "opencode")
        self.assertEqual((result["status"], result["total"], result["reason"]),
                         ("unknown", None, "mixed-opencode-generations"))

    def _run_fake_v2_journey(self, config=None, *, interrupt=False, contend=False):
        if fj.platform.system() != "Linux" or not fj.shutil.which("bwrap"):
            self.skipTest("end-to-end sandbox requires Linux bwrap")
        config = config or {}
        root = Path(tempfile.mkdtemp(prefix="journey-v2-unit-")); os.chmod(root, 0o700)
        auth = root / "auth"; auth.mkdir(mode=0o700)
        store = auth / "opencode/credentials-v2.json"; store.parent.mkdir(mode=0o700)
        original = json.dumps([{"id":"fixture", "integrationID":"provider", "label":"fake key",
                               "active":True, "value":{"type":"key", "key":"fixture-only"}}]).encode()
        store.write_bytes(original); os.chmod(store, 0o600); inode_before=store.stat().st_ino
        fake = root / "opencode"
        fake.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys, time
args=sys.argv[1:]; home=pathlib.Path(os.environ['HOME']); world=home.parent
cfg=json.loads((world/'fake-config.json').read_text()); data=pathlib.Path(os.environ['XDG_DATA_HOME'])/'opencode'; data.mkdir(parents=True,exist_ok=True)
trace=home/'trace.jsonl'
def log(event, **kw):
 with trace.open('a') as f: f.write(json.dumps({'event':event,'argv':args,'time':time.time(),**kw})+'\\n')
if args==['--version']:
 log('version'); print('opencode v2.0.32'); raise SystemExit(0)
if args[:2]==['auth','import']:
 p=pathlib.Path(args[-1]); log('import',temp=str(p),exists=p.exists(),mode=oct(p.stat().st_mode&0o777))
 if cfg.get('import_exit'): raise SystemExit(cfg['import_exit'])
 (data/'state.json').write_bytes(p.read_bytes()); (data/'import-path').write_text(str(p)); raise SystemExit(0)
if args[:2]==['auth','export']:
 p=pathlib.Path((data/'import-path').read_text()); payload=(data/'state.json').read_bytes()
 if cfg.get('fractional_expiry') and cfg.get('task_done'):
  payload=json.dumps([{'id':'fixture-oauth','integrationID':'provider','label':'fake oauth','active':True,'value':{'type':'oauth','methodID':'fixture-method','access':'fixture-access','refresh':'fixture-refresh','expires':1.5}}]).encode()
 elif cfg.get('incomplete') and cfg.get('task_done'): payload=b'[]'
 log('export',temp_exists=p.exists(),size=len(payload)); sys.stdout.buffer.write(payload); raise SystemExit(0)
if args[:1]==['run']:
 if '--help' in args: log('run-help'); print('Usage --standalone --format --session'); raise SystemExit(0)
 first='--session' not in args; log('run',first=first,standalone='--standalone' in args)
 (home/'.runtime/routes').mkdir(parents=True,exist_ok=True); (home/'.runtime/routes/f.json').write_text('{}')
 if first and cfg.get('rotate'):
  state=json.loads((data/'state.json').read_text()); state[0]['value']['key']+='-rotated'; state[0]['label']+=' rotated'; (data/'state.json').write_text(json.dumps(state))
 if first and (cfg.get('incomplete') or cfg.get('fractional_expiry')): cfg['task_done']=True; (world/'fake-config.json').write_text(json.dumps(cfg))
 if first and cfg.get('interrupt'):
  (home/'pid').write_text(str(os.getpid())); time.sleep(30)
 if cfg.get('run_sleep'): time.sleep(cfg['run_sleep'])
 text='[Run confirmation]\\nTask: fix\\nWhy: typo\\nRoute: direct\\nScope: greet\\nDone: test\\n→ Proceed / Revise: <x> / Stop' if first else 'Done.'
 print(json.dumps({'type':'text','sessionID':'session-1','part':{'text':text}})); raise SystemExit(0)
raise SystemExit(9)
"""); fake.chmod(0o700)
        world_ref = {}; judge_n = [0]; contender = {}
        prior = {"which":fj.shutil.which, "run":fj.run, "judge":fj.judge_repo,
                 "profiles":fj.PROFILES["all"], "tasks":fj.TASK_HARNESSES["all"]}
        original_mkdtemp = fj.tempfile.mkdtemp
        def which(name): return str(fake) if name == "opencode" else (None if name in ("claude", "codex") else prior["which"](name))
        def run(argv, **kwargs):
            tail = argv[argv.index("--") + 1:] if "--" in argv else list(argv)
            if tail and tail[0] == "sh" and any(str(x).endswith("install.sh") for x in tail):
                return {"exit":0,"seconds":0,"output":"fixture install"}
            if tail and tail[0] in ("harness","hearting"):
                return {"exit":0,"seconds":0,"output":json.dumps({"freshness":"fresh"}) if "status" in tail else ""}
            if len(tail)>=3 and tail[:2]==["sh","-c"] and "command -v" in tail[2]:
                name=tail[2].split()[-1]
                return {"exit":0 if name in ("claude","codex","opencode") else 1,"seconds":0,"output":"/usr/bin/fake"}
            return prior["run"](argv, **kwargs)
        def mkdtemp(*args, **kwargs):
            path=Path(original_mkdtemp(*args, **kwargs))
            if str(kwargs.get("prefix", "")).startswith("first-journey-"):
                world_ref["path"]=path; (path/"fake-config.json").write_text(json.dumps(config))
            return str(path)
        def judge(*_a, **_k):
            judge_n[0]+=1; passed=judge_n[0]>=2
            return {"code_fixed":passed,"focused_test_passed":passed,"candidate":"fixture" if passed else None,
                    "candidates":[],"worktrees":1,"local_refs":[],"reason":None,"seconds":0}
        fj.PROFILES["all"]=("opencode",); fj.TASK_HARNESSES["all"]=["opencode"]
        fj.shutil.which=which; fj.run=run; fj.judge_repo=judge; fj.tempfile.mkdtemp=mkdtemp
        previous_usr1 = signal.getsignal(signal.SIGUSR1)
        timer = None; contender_thread = None
        try:
            if interrupt:
                signal.signal(signal.SIGUSR1, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
                def interrupt_task():
                    deadline=time.monotonic()+20
                    while time.monotonic()<deadline:
                        path=world_ref.get("path")
                        if path and (path/'home/pid').exists():
                            time.sleep(.2); os.kill(os.getpid(),signal.SIGUSR1); return
                        time.sleep(.01)
                timer=threading.Thread(target=interrupt_task,daemon=True); timer.start()
            if contend:
                def try_lock():
                    deadline=time.monotonic()+20
                    while time.monotonic()<deadline:
                        path=world_ref.get("path"); trace=path/'home/trace.jsonl' if path else None
                        if trace and trace.exists() and '"event": "run"' in trace.read_text():
                            lock=(auth/'.lock').open('a'); start=time.monotonic()
                            fj.fcntl.flock(lock, fj.fcntl.LOCK_EX); contender['acquired']=time.time(); lock.close(); return
                        time.sleep(.01)
                contender_thread=threading.Thread(target=try_lock,daemon=True); contender_thread.start()
            if interrupt:
                try: record=fj.journey_profile("all", argparse.Namespace(source="checkout",ref="HEAD",model_free=False,auth_root=str(auth),local_root=str(root),keep_world="always",step_timeout=60,task_timeout=60,prompt="task",tag="latest"), Path(__file__).resolve().parents[2])
                except KeyboardInterrupt: record={"interrupted":True}
            else:
                record=fj.journey_profile("all", argparse.Namespace(source="checkout",ref="HEAD",model_free=False,auth_root=str(auth),local_root=str(root),keep_world="always",step_timeout=60,task_timeout=60,prompt="task",tag="latest"), Path(__file__).resolve().parents[2])
        finally:
            if timer: timer.join(timeout=5)
            if contender_thread: contender_thread.join(timeout=25)
            signal.signal(signal.SIGUSR1, previous_usr1)
            fj.shutil.which=prior["which"]; fj.run=prior["run"]; fj.judge_repo=prior["judge"]
            fj.tempfile.mkdtemp=original_mkdtemp; fj.PROFILES["all"]=prior["profiles"]; fj.TASK_HARNESSES["all"]=prior["tasks"]
        world=world_ref.get("path"); events=[]
        if world and (world/'home/trace.jsonl').exists(): events=[json.loads(x) for x in (world/'home/trace.jsonl').read_text().splitlines()]
        child_reaped=None
        if world:
            marker=str(world).encode(); survivors=[]
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit() or int(entry.name)==os.getpid(): continue
                try:
                    if marker in (entry/"cmdline").read_bytes() or marker in (entry/"environ").read_bytes():
                        survivors.append(entry.name)
                except OSError: pass
            child_reaped=not survivors
        lock_fd=(auth/".lock").open("a")
        try:
            fj.fcntl.flock(lock_fd,fj.fcntl.LOCK_EX|fj.fcntl.LOCK_NB); lock_released=True
        except BlockingIOError: lock_released=False
        finally: lock_fd.close()
        result={"record":record,"events":events,"world":world,"before":original,"after":store.read_bytes(),
                "inode_before":inode_before,"inode_after":store.stat().st_ino,"contender":contender,
                "child_reaped":child_reaped,"lock_released":lock_released,"auth":auth}
        shutil.rmtree(root,ignore_errors=True)
        return result

    def test_fake_v2_journey_profile_orders_import_task_and_locked_final_export(self):
        result=self._run_fake_v2_journey({"rotate":True})
        events=result["events"]; names=[e["event"] for e in events]
        self.assertEqual(names,["version","run-help","import","export","run","run","export"])
        self.assertEqual(events[2]["mode"],"0o600"); self.assertTrue(events[2]["exists"])
        self.assertFalse(events[3]["temp_exists"])
        self.assertEqual([e["first"] for e in events if e["event"]=="run"],[True,False])
        self.assertTrue(all(e["standalone"] for e in events if e["event"]=="run"))
        self.assertIn(b"fixture-only-rotated",result["after"])
        self.assertNotEqual(result["inode_before"],result["inode_after"])

    def test_fake_v2_journey_import_failure_preserves_store_and_skips_task(self):
        result=self._run_fake_v2_journey({"import_exit":7})
        self.assertEqual([e["event"] for e in result["events"]],["version","run-help","import"])
        self.assertEqual(result["after"],result["before"])

    def test_fake_v2_journey_unchanged_and_incomplete_exports_preserve_store(self):
        for config in ({}, {"incomplete":True}, {"fractional_expiry":True}):
            with self.subTest(config=config):
                result=self._run_fake_v2_journey(config)
                self.assertEqual(result["after"],result["before"])
                self.assertEqual(result["inode_before"],result["inode_after"])
                self.assertEqual(len([e for e in result["events"] if e["event"]=="export"]),2)
                if config.get("fractional_expiry"):
                    model=result["record"]["harnesses"][0]["model"]
                    self.assertEqual(model["auth_persist_warning"],"auth-export-incomplete")

    def test_fake_v2_journey_interrupt_persists_once_and_reaps_child(self):
        result=self._run_fake_v2_journey({"rotate":True,"interrupt":True},interrupt=True)
        self.assertTrue(result["record"].get("interrupted"))
        self.assertEqual(len([e for e in result["events"] if e["event"]=="export"]),2)
        self.assertIn(b"fixture-only-rotated",result["after"])
        self.assertTrue(result["lock_released"])
        self.assertTrue(result["child_reaped"])

    def test_fake_v2_journey_lock_contention_waits_through_final_export(self):
        result=self._run_fake_v2_journey({"rotate":True,"run_sleep":2},contend=True)
        # The flock contender can acquire only after journey_profile released the store lock.
        exports=[e for e in result["events"] if e["event"]=="export"]
        self.assertEqual(len(exports),2)
        self.assertGreater(result["contender"].get("acquired",0),exports[-1]["time"])

    def test_v2_completeness_requires_transferable_native_credentials(self):
        key = {"id": "k", "integrationID": "provider", "label": "Key", "active": True,
               "value": {"type": "key", "key": "fixture-only"}, "future": {"keep": True}}
        oauth = {"id": "o", "integrationID": "provider", "label": "OAuth", "active": False,
                 "value": {"type": "oauth", "methodID": "method", "access": "a", "refresh": "r", "expires": 0}}
        external = {"id": "e", "integrationID": "provider", "label": "External", "active": True,
                    "value": {"type": "external", "methodID": "method"}}
        oauth_float_zero = {**oauth, "value": {**oauth["value"], "expires": 0.0}}
        def encoded(rows): return json.dumps(rows, allow_nan=True).encode()
        for rows in ([key], [oauth], [oauth_float_zero], [key, external]):
            self.assertTrue(fj.credential_complete("opencode", encoded(rows), "v2"))
        rejected = ([], [external], [{**key, "value": {"type": "key", "key": ""}}],
                    [{**oauth, "value": {**oauth["value"], "access": ""}}],
                    [{**oauth, "value": {**oauth["value"], "expires": True}}],
                    [{**oauth, "value": {**oauth["value"], "expires": 1.5}}],
                    [{**key, "id": ""}], [key, key], [{**key, "value": {"type": "unknown", "key": "x"}}])
        for rows in rejected:
            with self.subTest(rows=rows):
                self.assertFalse(fj.credential_complete("opencode", encoded(rows), "v2"))
        self.assertFalse(fj.credential_complete("opencode", b"[{", "v2"))
        self.assertFalse(fj.credential_complete("opencode", encoded([{
            **oauth, "value": {**oauth["value"], "expires": float("nan")}}]), "v2"))

    def test_version_classifier_never_calls_runtime_probe_for_unknown(self):
        self.assertIsNone(fj.opencode_generation("", 0))
        self.assertIsNone(fj.opencode_generation("OpenCode", 0))
        self.assertIsNone(fj.opencode_generation("opencode v2.0.32", 1))
        self.assertEqual(fj.opencode_generation("opencode v2.0.32", 0), "v2")
        self.assertEqual(fj.opencode_generation("opencode 1.2.3", 0), "v1")

    def test_v2_transport_adds_standalone_and_unknown_refuses_argv(self):
        cwd = Path("/tmp/world/repo")
        self.assertEqual(fj.turn_argv("opencode", "opencode", cwd, None, "task", "v1"),
                         ["opencode", "run", "--format", "json", "task"])
        self.assertEqual(fj.turn_argv("opencode", "opencode", cwd, "s", "next", "v2"),
                         ["opencode", "run", "--standalone", "--format", "json", "--session", "s", "next"])
        with self.assertRaisesRegex(ValueError, "opencode-version-unknown"):
            fj.turn_argv("opencode", "opencode", cwd, None, "task")
        result = fj.transport_result("opencode", 0, "--session --format", "v2")
        self.assertFalse(result["supported"])
        self.assertIn("--standalone", result["missing_flags"])

    def test_private_capture_is_bounded_and_keeps_stderr_secret_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "emit.py"
            script.write_text("import sys; sys.stderr.write('fixture-secret'); sys.stdout.write('x'*20000)")
            result = fj.private_auth_command([os.sys.executable, str(script)], env=dict(os.environ))
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["stdout"]), 20000)
            self.assertNotIn("stderr", result)
            oversized = fj.private_auth_command([os.sys.executable, "-c", "import sys; sys.stdout.write('x'*1000)"],
                                                env=dict(os.environ), stdout_limit=999)
            self.assertEqual((oversized["ok"], oversized["reason"], oversized["stdout"]),
                             (False, "oversized", b""))
            stderr_oversized = fj.private_auth_command(
                [os.sys.executable, "-c", "import sys; sys.stderr.write('x'*1000)"],
                env=dict(os.environ), stderr_limit=999)
            self.assertEqual((stderr_oversized["ok"], stderr_oversized["reason"]), (False, "oversized"))
            timed_out = fj.private_auth_command([os.sys.executable, "-c", "import time; time.sleep(30)"],
                                                env=dict(os.environ), timeout=0.1)
            self.assertEqual((timed_out["ok"], timed_out["reason"]), (False, "timeout"))

    def test_fake_v2_cli_import_export_round_trip_uses_no_auth_json(self):
        payload = [{"id": "fixture", "integrationID": "provider", "label": "fake key", "active": True,
                    "value": {"type": "key", "key": "fixture-only-not-a-real-key"}}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); world = root / "world"
            for name in ("tmp", "data/opencode"):
                (world / name).mkdir(parents=True, exist_ok=True)
            store = root / "credentials-v2.json"; store.write_text(json.dumps(payload))
            fake = root / "fake_opencode.py"
            fake.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
cmd = sys.argv[1:]
state = pathlib.Path(os.environ['XDG_DATA_HOME']) / 'opencode' / 'state.json'
if cmd[:2] == ['auth', 'import']:
    if pathlib.Path(cmd[-1]).stat().st_mode & 0o777 != 0o600: raise SystemExit(8)
    state.write_bytes(pathlib.Path(cmd[-1]).read_bytes())
elif cmd[:2] == ['auth', 'export']:
    sys.stdout.buffer.write(state.read_bytes())
else:
    raise SystemExit(9)
""")
            fake.chmod(0o700)
            env = {"XDG_DATA_HOME": str(world / "data"), "HOME": str(world / "home"), "PATH": os.environ["PATH"]}
            old_sandbox = fj.sandbox
            fj.sandbox = lambda argv, *a, **k: argv
            try:
                ok, reason = fj.import_v2_credentials(store, world, str(fake), env, [])
            finally:
                fj.sandbox = old_sandbox
            self.assertTrue(ok, reason)
            self.assertEqual(json.loads((world / "data/opencode/state.json").read_bytes()), payload)
            self.assertFalse((world / "data/opencode/auth.json").exists())
            self.assertEqual(list((world / "tmp").iterdir()), [])

    def test_fake_v1_v2_login_cli_keeps_native_v1_and_exports_v2(self):
        for generation, version in (("v1", "opencode 1.2.3"), ("v2", "opencode v2.0.32")):
            with self.subTest(generation=generation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); fake = root / "opencode"; trace = root / "calls.jsonl"
                fake.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
trace = pathlib.Path(os.environ['FAKE_TRACE'])
with trace.open('a') as f: f.write(json.dumps(args)+'\\n')
data = pathlib.Path(os.environ['XDG_DATA_HOME']) / 'opencode'
data.mkdir(parents=True, exist_ok=True)
if args == ['--version']:
    print(os.environ['FAKE_VERSION'])
elif args[:2] == ['auth', 'login']:
    if ('--standalone' in args) != (os.environ['FAKE_VERSION'].startswith('opencode v2')): raise SystemExit(3)
    filename = 'state-v2.json' if '--standalone' in args else 'auth.json'
    data.joinpath(filename).write_text(json.dumps([{'id':'fixture','integrationID':'provider','label':'key','active':True,'value':{'type':'key','key':'fixture-only'}}] if filename.startswith('state') else {'api':{'type':'api','key':'fixture-only'}}))
elif args[:2] == ['auth', 'export']:
    if '--standalone' not in args: raise SystemExit(4)
    sys.stdout.write(data.joinpath('state-v2.json').read_text())
else: raise SystemExit(5)
""")
                fake.chmod(0o700)
                original = {"argv": os.sys.argv, "which": fj.shutil.which,
                            "sandbox": fj.sandbox, "clean_env": fj.clean_env}
                fj.shutil.which = lambda name: str(fake) if name == "opencode" else original["which"](name)
                fj.sandbox = lambda argv, *a, **k: [str(fake), *argv[1:]]
                def configured_env(*args, **kwargs):
                    env = original["clean_env"](*args, **kwargs)
                    env.update({"FAKE_TRACE": str(trace), "FAKE_VERSION": version})
                    return env
                fj.clean_env = configured_env
                os.sys.argv = ["first-journey.py", "login", "--harness", "opencode",
                               "--auth-root", str(root / "auth")]
                try:
                    self.assertEqual(fj.main(), 0)
                finally:
                    os.sys.argv = original["argv"]
                    fj.shutil.which = original["which"]
                    fj.sandbox = original["sandbox"]
                    fj.clean_env = original["clean_env"]
                calls = [json.loads(line) for line in trace.read_text().splitlines()]
                self.assertEqual(calls.count(["--version"]), 1)
                login_call = next(args for args in calls if args[:2] == ["auth", "login"])
                self.assertEqual("--standalone" in login_call, generation == "v2")
                if generation == "v2":
                    self.assertEqual(calls.count(["auth", "export", "--standalone"]), 1)
                    path = root / "auth/opencode/credentials-v2.json"
                    self.assertFalse((root / "auth/opencode/auth.json").exists())
                else:
                    self.assertNotIn(["auth", "export", "--standalone"], calls)
                    path = root / "auth/opencode/auth.json"
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_v2_final_export_rotation_replaces_store_atomically(self):
        def payload(key):
            return json.dumps([{"id": "fixture", "integrationID": "provider", "label": "fake key",
                                "active": True, "value": {"type": "key", "key": key}}]).encode()
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "opencode/credentials-v2.json"
            original = payload("before")
            rotated = payload("after")
            store.parent.mkdir(mode=0o700); store.write_bytes(original)
            prior = fj._ACTIVE_AUTH; private = fj.private_auth_command
            fj._ACTIVE_AUTH = {"harness": "opencode", "generation": "v2", "store": store,
                               "target": None, "original": original, "env": {},
                               "export_argv": ["fake-opencode", "auth", "export", "--standalone"]}
            fj.private_auth_command = lambda *a, **k: {"ok": True, "reason": None, "exit": 0,
                                                        "seconds": 0, "stdout": rotated}
            try:
                self.assertTrue(fj.persist_active_auth())
            finally:
                fj.private_auth_command = private; fj._ACTIVE_AUTH = prior
            self.assertEqual(store.read_bytes(), rotated)
            self.assertEqual(store.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.parent.stat().st_mode & 0o777, 0o700)

    def test_incomplete_v2_final_export_warns_and_preserves_store(self):
        original = json.dumps([{"id": "fixture", "integrationID": "provider", "label": "key", "active": True,
                                "value": {"type": "key", "key": "fixture"}}]).encode()
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "credentials-v2.json"; store.write_bytes(original)
            prior = fj._ACTIVE_AUTH; private = fj.private_auth_command
            fj._ACTIVE_AUTH = {"harness": "opencode", "generation": "v2", "store": store,
                               "target": None, "original": original, "env": {},
                               "export_argv": ["fake-opencode", "auth", "export", "--standalone"]}
            fj.private_auth_command = lambda *a, **k: {"ok": True, "reason": None, "exit": 0,
                                                        "seconds": 0, "stdout": b"[]"}
            try:
                self.assertFalse(fj.persist_active_auth())
                self.assertEqual(fj._ACTIVE_AUTH["last_error"], "auth-export-incomplete")
            finally:
                fj.private_auth_command = private; fj._ACTIVE_AUTH = prior
            self.assertEqual(store.read_bytes(), original)

    def test_unknown_opencode_generation_never_runs_task(self):
        calls = []
        old = fj.run
        fj.run = lambda *a, **k: calls.append(a) or {"exit": 0, "seconds": 0, "output": ""}
        try:
            result = fj.headless_turn("opencode", "opencode", "task", Path("/tmp/repo"),
                                      Path("/tmp/world"), {}, 30, [], opencode_generation=None)
        finally:
            fj.run = old
        self.assertEqual(result["status"], "opencode-version-unknown")
        self.assertEqual(calls, [])

    def test_claude_usage_deduplicates_message_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"home/.claude/projects/p/a.jsonl"; path.parent.mkdir(parents=True)
            row={"type":"assistant","sessionId":"s","message":{"id":"m","usage":{"input_tokens":5,"output_tokens":2}}}
            path.write_text(json.dumps(row)+"\n"+json.dumps(row))
            result=fj.telemetry(Path(tmp),"claude")
        self.assertEqual(result["input"],5)

    def test_model_run_count_does_not_repeat_claude_in_all_profile(self):
        self.assertEqual(fj.TASK_HARNESSES["claude-only"], ["claude"])
        self.assertEqual(fj.TASK_HARNESSES["all"], ["codex", "opencode"])

    def test_report_has_json_and_markdown_with_unknown_account_usage(self):
        record = {"profile": "all", "source": "published", "isolation": {"level": "env-home-only", "d3": "not-proven"},
                  "elapsed_seconds": 1.25, "steps": [{"name": "install", "exit": 0, "seconds": 1}],
                  "harnesses": [{"name": "claude", "model": {"status": "skipped(model-free)", "turns": 0,
                                                                      "canned_answers": 0, "session_tokens": "unknown"}}],
                  "failures": [], "account_usage": {"before": "unknown", "after": "unknown"}}
        with tempfile.TemporaryDirectory() as tmp:
            fj.write_report(record, Path(tmp))
            value = json.loads((Path(tmp) / "journey.json").read_text())
            markdown = (Path(tmp) / "journey.md").read_text()
        self.assertEqual(value["account_usage"]["before"], "unknown")
        self.assertIn("session tokens unknown", markdown)
        self.assertIn("attribution uncertain", markdown)
        self.assertIn("Headless uncovered", markdown)
        self.assertIn("Isolation checks", markdown)

    def test_success_judgment_accepts_non_main_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)
            fixture=Path(__file__).parent/"fixtures/first-journey"
            shutil.copy(fixture/"greet.py",repo/"greet.py"); shutil.copy(fixture/"focused_test.py.txt",repo/"test_greet.py")
            subprocess.run(["git","init","-q"],cwd=repo,check=True)
            subprocess.run(["git","config","user.name","Test"],cwd=repo,check=True)
            subprocess.run(["git","config","user.email","test@example.invalid"],cwd=repo,check=True)
            subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","commit","-qm","baseline"],cwd=repo,check=True)
            subprocess.run(["git","switch","-qc","feature"],cwd=repo,check=True)
            (repo/"greet.py").write_text('def greet(): return "Hello"\nif __name__ == "__main__": print(greet())\n')
            result=fj.judge_repo(repo,(fixture/"focused_test.py.txt").read_bytes())
        self.assertTrue(result["code_fixed"])
        self.assertIn("refs/heads/feature",result["local_refs"])

    def test_success_judgment_rejects_edited_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)
            (repo/"greet.py").write_text('def greet(): return "Hello"\nif __name__ == "__main__": print(greet())\n')
            (repo/"test_greet.py").write_text('import unittest\nfrom greet import greet\nclass T(unittest.TestCase):\n def test_it(self): self.assertEqual(greet(), "wrong")\n')
            subprocess.run(["git","init","-q"],cwd=repo,check=True)
            subprocess.run(["git","config","user.name","Test"],cwd=repo,check=True)
            subprocess.run(["git","config","user.email","test@example.invalid"],cwd=repo,check=True)
            subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","commit","-qm","edited test"],cwd=repo,check=True)
            result=fj.judge_repo(repo,b"original baseline test bytes")
        self.assertFalse(result["code_fixed"])
        self.assertTrue(any(row["reason"] == "baseline-test-edited" for row in result["candidates"]))

    def test_baseline_fixture_expects_hello_but_greet_is_typo(self):
        fixture = Path(__file__).parent / "fixtures/first-journey"
        source = (fixture / "greet.py").read_text()
        test = (fixture / "focused_test.py.txt").read_text()
        self.assertIn('return "Helo"', source)
        self.assertIn('self.assertEqual(greet(), "Hello")', test)

    def test_sandbox_requires_bwrap_on_linux(self):
        if fj.platform.system() != "Linux":
            self.skipTest("Linux namespace check")
        old = fj.shutil.which
        try:
            fj.shutil.which = lambda command: None if command == "bwrap" else old(command)
            with self.assertRaisesRegex(RuntimeError, "isolation-unavailable"):
                fj.sandbox(["true"], Path("/tmp"), {})
        finally:
            fj.shutil.which = old

    def test_sandbox_keeps_world_path_and_hides_home_and_nas(self):
        if fj.platform.system() != "Linux" or not fj.shutil.which("bwrap"):
            self.skipTest("Linux bwrap check")
        world = Path("/tmp/first-journey-unit")
        joined = " ".join(fj.sandbox(["true"], world, {"HOME": str(world / "home")}))
        self.assertIn(f"--bind {world} {world}", joined)
        self.assertIn("--tmpfs /home/nas", joined)
        self.assertIn("--tmpfs /home ", joined)
        self.assertNotIn("/journey ", joined)
        self.assertIn(f"--setenv HOME {world}/home", joined)

    def test_sandbox_read_only_binds_resolver_target_to_etc_resolv_conf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "resolver-target"; target.write_text("nameserver 192.0.2.1\n")
            resolver = root / "resolv.conf"; resolver.symlink_to(target)
            old_system = fj.platform.system
            old_which = fj.shutil.which
            try:
                fj.platform.system = lambda: "Linux"
                fj.shutil.which = lambda command: "/usr/bin/bwrap" if command == "bwrap" else old_which(command)
                args = fj.sandbox(["true"], root / "world", {}, system_files={"resolv.conf": resolver})
            finally:
                fj.platform.system = old_system
                fj.shutil.which = old_which
        pairs = [args[i + 1:i + 3] for i, arg in enumerate(args[:-2]) if arg == "--ro-bind"]
        self.assertIn([str(target), "/etc/resolv.conf"], pairs)

    def test_run_kills_and_reaps_child_after_keyboard_interrupt(self):
        if fj.platform.system() != "Linux": self.skipTest("process groups are exercised on Linux")
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"
            script = "import os,time; open(%r,'w').write(str(os.getpid())); time.sleep(60)" % str(pidfile)
            previous = signal.getsignal(signal.SIGUSR1)
            signal.signal(signal.SIGUSR1, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            def interrupt_when_started():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not pidfile.exists(): time.sleep(0.01)
                os.kill(os.getpid(), signal.SIGUSR1)
            timer = threading.Timer(0.05, interrupt_when_started)
            timer.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    fj.run(["python3", "-c", script], env=dict(os.environ), timeout=30)
            finally:
                timer.cancel(); signal.signal(signal.SIGUSR1, previous)
            child_pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError): os.kill(child_pid, 0)

    def test_invalid_auth_json_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store.json"; store.write_text('{"token":"original"}')
            target = Path(tmp) / "world-auth.json"; target.write_text('{"token":"')
            previous = fj._ACTIVE_AUTH
            try:
                fj._ACTIVE_AUTH = {"harness": "claude", "generation": "native", "store": store,
                                   "target": target, "original": store.read_bytes()}
                fj.persist_active_auth()
            finally:
                fj._ACTIVE_AUTH = previous
            self.assertEqual(store.read_text(), '{"token":"original"}')

    def test_incomplete_credentials_never_replace_store_and_complete_fixtures_do(self):
        rejected = {
            "claude": (b"{}", b'{"claudeAiOauth":{"accessToken":"fake-access"}}'),
            "codex": (b"{}", b'{"tokens":{"access_token":"","refresh_token":"fake-refresh"}}'),
            "opencode": (
                b"{}", b'{"account":{"type":"api"}}',
                b'{"account":{"type":"api","key":""}}',
                b'{"account":{"type":"api","key":"fixture-key","metadata":{"field":1}}}',
                b'{"account":{"type":"oauth","access":"fixture-access","refresh":"fixture-refresh"}}',
                b'{"account":{"type":"oauth","access":"fixture-access","expires":0}}',
                b'{"account":{"type":"oauth","access":"","refresh":"fixture-refresh","expires":0}}',
                b'{"account":{"type":"oauth","access":"fixture-access","refresh":"fixture-refresh","expires":"0"}}',
                b'{"account":{"type":"oauth","access":"fixture-access","refresh":"fixture-refresh","expires":0,"accountId":1}}',
                b'{"account":{"type":"oauth","access":"fixture-access","refresh":"fixture-refresh","expires":true}}',
                b'{"account":{"type":"oauth","access":"fixture-access","refresh":"fixture-refresh","expires":-1}}',
                b'{"account":{"type":"oauth","access":"fixture-access","refresh":"fixture-refresh","expires":0.5}}',
                b'{"account":{"type":"oauth","access":"fixture-access","refresh":"fixture-refresh","expires":NaN}}',
                b'{"account":{"type":"oauth","access":"fixture-access","refresh":"fixture-refresh","expires":Infinity}}',
                b'{"account":{"type":"wellknown","key":"fixture-key"}}',
                b'{"account":{"type":"wellknown","key":"","token":"fixture-token"}}',
                b'{"account":{"type":"wellknown","key":"fixture-key","token":""}}',
                b'{"account":{"type":"unknown"}}', b'{"account":{"type":{}}}',
                b'{"good":{"type":"api","key":"fixture-key"},"partial":{"type":"api"}}',
            ),
        }
        complete = {
            "claude": b'{"claudeAiOauth":{"accessToken":"fake-access","refreshToken":"fake-refresh"}}',
            "codex": b'{"tokens":{"access_token":"fake-access","refresh_token":"fake-refresh"}}',
            "opencode": (b'{"api":{"type":"api","key":"fixture-key","metadata":{"field":"value"}},'
                         b'"oauth":{"type":"oauth","access":"fixture-access","refresh":"fixture-refresh",'
                         b'"expires":0.0,"accountId":"fixture-account","enterpriseUrl":"https://example.invalid"},'
                         b'"wellknown":{"type":"wellknown","key":"fixture-key","token":"fixture-token"}}'),
        }
        for harness, candidates in rejected.items():
            for candidate in candidates:
                with self.subTest(harness=harness, candidate=candidate), tempfile.TemporaryDirectory() as tmp:
                    store = Path(tmp) / "store.json"
                    original = b'{"keep":"original-bytes"}'
                    store.write_bytes(original)
                    target = Path(tmp) / "world-auth.json"
                    target.write_bytes(candidate)
                    previous = fj._ACTIVE_AUTH
                    try:
                        fj._ACTIVE_AUTH = {"harness": harness, "generation": "v1" if harness == "opencode" else "native",
                                           "store": store, "target": target, "original": original}
                        self.assertFalse(fj.credential_complete(harness, candidate))
                        fj.persist_active_auth()
                    finally:
                        fj._ACTIVE_AUTH = previous
                    self.assertEqual(store.read_bytes(), original)

        for harness, candidate in complete.items():
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp) / "store.json"
                original = b'{"keep":"original-bytes"}'
                store.write_bytes(original)
                target = Path(tmp) / "world-auth.json"
                target.write_bytes(candidate)
                previous = fj._ACTIVE_AUTH
                try:
                    fj._ACTIVE_AUTH = {"harness": harness, "generation": "v1" if harness == "opencode" else "native",
                                       "store": store, "target": target, "original": original}
                    self.assertTrue(fj.credential_complete(harness, candidate))
                    fj.persist_active_auth()
                finally:
                    fj._ACTIVE_AUTH = previous
                self.assertEqual(store.read_bytes(), candidate)

    def test_unsupported_transport_does_not_execute_task_argv(self):
        calls = []
        previous = fj.run
        previous_sandbox = fj.sandbox
        try:
            fj.run = lambda argv, **kwargs: calls.append(argv) or {
                "exit": 0, "seconds": 0, "output": "Usage: opencode run [prompt]"}
            fj.sandbox = lambda argv, *a, **k: argv
            transport = fj.probe_transport("opencode", "opencode", Path("/tmp/world"), {}, [], 5, "v1")
            outcome = fj.headless_turn("opencode", "opencode", "Fix the task", Path("/tmp/repo"),
                                       Path("/tmp/world"), {}, 5, [], transport_supported=transport["supported"],
                                       opencode_generation="v1")
        finally:
            fj.run = previous; fj.sandbox = previous_sandbox
        self.assertFalse(transport["supported"])
        self.assertEqual(outcome["status"], "unsupported-headless-transport")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-2:], ["run", "--help"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
