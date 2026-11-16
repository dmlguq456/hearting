"""A person's answer from Fleet reaches the idle shell session that asked.

A real session-shell host (this checkout's, and the v4.29.3 host taken from git) runs a
synthetic native in a private PTY with a private HOME; no model, user pane or account is
used. The native acts as Claude does: lifecycle events over the shell socket, a composer
painted as ``❯ …``, and the real UserPromptSubmit hooks run on every prompt it takes.
The obligation runner's own pass (``_reconcile_obligations``) is called here instead of a
detached runner, so the suite owns every process it starts.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
OLD_RELEASE = "v4.29.3"
SID = "00000000-0000-4000-8000-0000000a1116"

FAKE_NATIVE = r"""
import json,os,subprocess,sys,time,tty
harness,sid,root,log=sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]
def start_of(pid):
    raw=open('/proc/%d/stat'%pid).read();return raw.rsplit(') ',1)[1].split()[19]
def note(**row):
    with open(log,'a') as f:f.write(json.dumps(row)+'\n')
def record(status):
    if harness!='claude':return
    path=os.path.join(os.environ['CLAUDE_CONFIG_DIR'],'sessions',str(os.getpid())+'.json')
    os.makedirs(os.path.dirname(path),exist_ok=True)
    with open(path,'w') as f:f.write(json.dumps({'pid':os.getpid(),'procStart':start_of(os.getpid()),'sessionId':sid,
        'status':status,'statusUpdatedAt':int(time.time()*1000)}))
def event(kind):
    import socket
    record('busy' if kind=='prompt' else 'idle')
    request={'op':'event','event':kind,'harness':harness,'sid':sid,'pid':os.getpid(),'start':start_of(os.getpid()),
             'token':os.environ['HEARTING_SHELL_TOKEN'],'seq':time.monotonic_ns()//1000}
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(5);channel.connect(os.environ['HEARTING_SHELL_SOCKET'])
        channel.sendall((json.dumps(request)+'\n').encode());channel.recv(8192)
last='READY'
def paint(draft=''):
    if harness=='claude':box='\u276f '+draft+'\r\n'+'\u2500'*18+'\r\n'
    else:box='\u2503  '+draft+'\r\n\u2503  Build  synthetic-model\r\n'
    print('\x1b[2J\x1b[H'+last+'\r\n'+box,end='',flush=True)
def run(argv,line,payload=None):
    done=subprocess.run([sys.executable,*argv],input=payload if payload is not None else line,
        capture_output=True,text=True,timeout=30,env=dict(os.environ,AGENT_HOME=root))
    return done.stdout
def take(line):
    if harness=='claude':
        # Claude's UserPromptSubmit order: the session sweep, then the peer-message recorder.
        payload=json.dumps({'session_id':sid,'cwd':os.getcwd(),'prompt':line,'hook_event_name':'UserPromptSubmit'})
        return (run([os.path.join(root,'hooks','dispatch-session-sweep.py')],line,payload)
                +run([os.path.join(root,'hooks','peer-message-record.py'),'prompt'],line,payload))
    # The OpenCode plugin's chat.message callback (`spawnPeerNotice`): the same helper and arguments.
    if 'peer-from:' in line or 'runtime-from:' in line:
        run([os.path.join(root,'utilities','peer-message.py'),'receive','--from-project',os.path.basename(os.getcwd()),
             '--to-harness','opencode','--to-session-id',sid],line)
    return ''
tty.setraw(0)
event('start')
print('\x1b[?2004h',end='',flush=True);paint()
buffer='';draft='';pasting=False;prompts=0
while True:
    data=os.read(0,65536)
    if not data or data==b'\x03':break
    buffer+=data.decode(errors='replace')
    while buffer:
        if pasting:
            end=buffer.find('\x1b[201~')
            if end<0:draft+=buffer;buffer='';break
            draft+=buffer[:end];buffer=buffer[end+6:];pasting=False;paint(draft);continue
        if buffer.startswith('\x1b[200~'):pasting=True;buffer=buffer[6:];continue
        if buffer.startswith('\x1b') and len(buffer)<6 and '\x1b[200~'.startswith(buffer):break
        char,buffer=buffer[0],buffer[1:]
        if char=='\x7f':draft=draft[:-1];paint(draft);continue
        if char!='\r':
            draft+=char if char>=' ' or char=='\n' else '';paint(draft);continue
        line,draft=draft,''
        if line=='warm':
            event('prompt');last='DONE_warm';paint();event('stop');continue
        prompts+=1
        context=take(line)
        event('prompt')
        note(number=prompts,prompt=line,context=context)
        last='DONE_PROMPT_%d'%prompts;paint();event('stop')
"""


# The outer terminal's screen as a person sees it: the window's bytes through the shell's own
# headless xterm (its painter writes only changed cells, so the raw bytes do not hold whole words).
SCREEN = r"""
const fs=require('fs'),{Terminal}=require('@xterm/headless');
const term=new Terminal({cols:130,rows:30,allowProposedApi:true});
term.write(fs.readFileSync(process.argv[1]),()=>{
  const b=term.buffer.active,out=[];
  for(let y=0;y<term.rows;y++)out.push(b.getLine(b.viewportY+y)?.translateToString(true)||'');
  process.stdout.write(out.join('\n'));
});
"""


def _private_root():
    # Short and under /tmp: the shell's socket paths must stay below the 104-byte limit.
    return tempfile.TemporaryDirectory(prefix="ari-", dir="/tmp")


def _end_bound_processes(marker):
    """Every process whose environment names this suite's private root: its own descendants only."""
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in (me, 1):
            continue
        try:
            environ = Path("/proc", entry, "environ").read_bytes()
        except OSError:
            continue
        if marker.encode() + b"\0" in environ + b"\0":
            with contextlib.suppress(OSError):
                os.kill(int(entry), signal.SIGKILL)


def _peer_module():
    name = "ari_peer_message"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "utilities/peer-message.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class ShellRig:
    """One private shell window around the synthetic native, driven through its PTY."""

    def __init__(self, test, entry, name, harness="claude", env=None):
        self.test, self.entry, self.name, self.harness = test, Path(entry), name, harness
        self.env = dict(os.environ, **(env or {}))
        self.directory = test.case / ("window-" + name)
        self.directory.mkdir(mode=0o700)
        self.log = self.directory / "native.log"
        self.raw = bytearray()
        self.lock = threading.Lock()
        self.process = None
        self.master = None
        self.binding = None

    def start(self):
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 130, 0, 0))
        command = [sys.executable, str(self.entry), self.harness, sys.executable, str(self.test.root / "native.py"),
                   self.harness, SID, str(ROOT), str(self.log)]
        self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=self.env,
                                        cwd=self.test.root / "project", start_new_session=True, close_fds=True)
        os.close(slave)
        self.master = master
        threading.Thread(target=self._drain, daemon=True).start()
        self.test.addCleanup(self.stop)
        self.wait_for(b"READY", "the native never painted")
        self.binding = self.wait(lambda: self.session_shell.exact_binding(self.harness, SID)[0],
                                 "the shell never published its attachment")
        self.type("warm\r")
        self.wait_for(b"DONE_warm", "the warm-up turn never ran")
        self.wait(self.idle_empty, "the window never became idle with an empty composer")
        return self

    @property
    def session_shell(self):
        import session_shell
        return session_shell

    def _drain(self):
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            with self.lock:
                self.raw.extend(data)
                if len(self.raw) > 4 * 1024 * 1024:
                    del self.raw[:2 * 1024 * 1024]

    def type(self, text):
        os.write(self.master, text.encode())

    def wait(self, check, message, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.1)
        self.test.fail(message)

    def wait_for(self, marker, message, timeout=20.0):
        return self.wait(lambda: marker in bytes(self.raw), message, timeout)

    def screen(self):
        dump = self.directory / "screen.bin"
        with self.lock:
            dump.write_bytes(bytes(self.raw))
        done = subprocess.run([shutil.which("node"), "-e", SCREEN, str(dump)], capture_output=True, text=True,
                              timeout=30, env=dict(os.environ))
        return done.stdout

    def current_binding(self):
        self.binding = self.session_shell.exact_binding(self.harness, SID)[0] or self.binding
        return self.binding

    def status(self):
        return self.session_shell.control(self.current_binding(), "status")

    def idle_empty(self):
        status = self.status()
        return status.get("state") == "idle" and status.get("empty") is True

    def prompts(self):
        try:
            return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            return []

    def stop(self):
        if self.process is None:
            return
        process, self.process = self.process, None
        if process.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGHUP)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        with contextlib.suppress(OSError):
            os.close(self.master)


class AnswerReachesIdleShellTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node") or not shutil.which("npm"):
            raise unittest.SkipTest("the session shell host needs node/npm")
        cls.temporary = _private_root()
        cls.root = Path(cls.temporary.name)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.addClassCleanup(_end_bound_processes, "ARI_SUITE_ROOT=" + str(cls.root))
        env = dict(os.environ)
        env.update({
            "ARI_SUITE_ROOT": str(cls.root),
            "npm_config_cache": str(cls.root / "npm-cache"),
            "npm_config_userconfig": str(cls.root / "npmrc"),
            "npm_config_devdir": str(cls.root / "node-gyp"),
            "TERM": "xterm-256color",
        })
        (cls.root / "npmrc").write_text("")
        (cls.root / "project").mkdir()
        (cls.root / "native.py").write_text(FAKE_NATIVE)
        policy = Path(env.get("XDG_CONFIG_HOME") or Path(env["HOME"]) / ".config") / "hearting/session-shell.json"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text('{"enabled":true}')
        policy.chmod(0o600)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        cls.addClassCleanup(patch.stop)
        for path in (ROOT / "tools/install", ROOT / "tools", ROOT / "utilities"):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        import session_shell_install
        assets = session_shell_install.prepare_assets()
        os.environ["NODE_PATH"] = str(assets / "node_modules")
        cls.base_env = dict(os.environ)
        cls.old_tree = cls._old_tree()

    @classmethod
    def _old_tree(cls):
        """The v4.29.3 shell (entry, owner, host and its backend) exactly as it was released."""
        try:
            archive = subprocess.run(["git", "-C", str(ROOT), "archive", OLD_RELEASE, "utilities", "tools", "hooks"],
                                     capture_output=True, timeout=120, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        target = cls.root / "releases" / OLD_RELEASE
        target.mkdir(parents=True)
        subprocess.run(["tar", "-x", "-C", str(target)], input=archive, check=True, timeout=120)
        return target

    def setUp(self):
        # Each test is its own machine: private state, Claude record and shell registry.
        self.case = Path(tempfile.mkdtemp(prefix="t-", dir=self.root))
        env = dict(self.base_env, XDG_STATE_HOME=str(self.case / "xdg-state"),
                   CLAUDE_CONFIG_DIR=str(self.case / "native-claude"))
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        from dispatch_contract import stable_state_root
        self.state = stable_state_root(os.environ)
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "jobs.log").touch()
        import human_wait_state as waits
        import peer_obligations as obligations
        # No detached runner or projection producer: `courier()` is the runner's own pass.
        for patch in (mock.patch.object(waits, "_request_producer"),
                      mock.patch.object(obligations, "ensure_normal_runner", return_value=False),
                      mock.patch.object(obligations, "request_dispatch_reconciliation", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)

    # ---- the person's side: the asking session's question and the Fleet answer ----
    def ask(self, harness="claude", text="PRIVATE-QUESTION?"):
        import human_wait_state as waits
        import session_tidy as tidy
        seat = tidy.session_seat(tidy.Seat("pane", "question-seat-" + harness, pane="fixture:p1",
                                           harness=harness), harness, SID)
        return waits.register_message(harness, SID, seat, text, sequence=tidy.read_prompt_seq(seat),
                                      cwd=str(self.root / "project"))

    def fleet_answer(self, signal_, text="PRIVATE-ANSWER"):
        """Fleet's submit effect, as `_question_submit` builds it, through Fleet's own runtime call."""
        import human_wait_state as waits
        from fleet.explorer.runtime import ExplorerRuntime
        row = {"id": signal_["id"], "harness": signal_["harness"], "session_id": SID}
        envelope = waits.read_selected_request(row)
        ref = {**envelope, **row, "_fleet_row_ref": dict(row), "question_id": "message-response"}
        result = ExplorerRuntime.execute_question_action(
            ("questions.submit", ref, {"answers": {"message-response": [text]}}))
        self.assertEqual(result.get("state"), "submitted", result)
        return result

    def courier(self, passes=1):
        """The existing obligation runner's pass (notice courier and message duties)."""
        name = "ari_courier"
        runner = sys.modules.get(name)
        if runner is None:
            spec = importlib.util.spec_from_file_location(name, ROOT / "utilities/peer-steward.py")
            runner = importlib.util.module_from_spec(spec)
            sys.modules[name] = runner
            spec.loader.exec_module(runner)
            self.addCleanup(sys.modules.pop, name, None)
        import peer_obligations as obligations
        for _ in range(passes):
            with contextlib.redirect_stdout(io.StringIO()):
                runner._reconcile_obligations(obligations.ObligationStore(self.state))
            time.sleep(0.5)

    def states(self, signal_):
        """The delivery's own records, by their state names."""
        import dispatch_pending_delivery as pending
        import human_wait_state as waits
        import peer_obligations as obligations
        delivery = "delivery-" + hashlib.sha256(("notice\0question-answer:" + signal_["id"]).encode()).hexdigest()
        record = pending.read(self.state, SID, delivery) or {}
        duties = obligations.ObligationStore(self.state).list()
        refs = [(duty.get("observation") or {}).get("transfer_ref") for duty in duties]
        ref = next((value for value in refs if value), None)
        row = (_peer_module()._read_pending(ref) or {}) if ref else {}
        envelope = waits.read_selected_request({"id": signal_["id"], "harness": signal_["harness"], "session_id": SID})
        return {
            "delivery": {key: record.get(key) for key in
                         ("state", "claim_owner", "claim_authority", "attempts", "acked_at_ns")},
            "message": {key: row.get(key) for key in ("state", "receipt")},
            "request_state": envelope.get("state"),
            "duty_reasons": sorted(str((duty.get("observation") or {}).get("reason"))
                                   for duty in duties if (duty.get("intent") or {}).get("kind") == "message"),
        }

    def assert_field_wait(self, states, receipt, reason):
        """The record states seen on the stuck window (11-16): claimed once by the retained courier."""
        self.assertEqual(states["delivery"]["state"], "sent-ambiguous", states)
        self.assertTrue(str(states["delivery"]["claim_owner"]).startswith("retained-rewake:run-notice-"), states)
        self.assertEqual(states["delivery"]["claim_authority"], "deliverer-unproven", states)
        self.assertEqual(states["delivery"]["attempts"], 1, states)
        self.assertIsNone(states["delivery"]["acked_at_ns"], states)
        self.assertEqual(states["message"], {"state": "pending", "receipt": receipt}, states)
        self.assertEqual(states["duty_reasons"], [reason], states)
        self.assertEqual(states["request_state"], "submitted", states)

    def install(self, release):
        """A private install whose ``current`` link names ``release``, as the launcher's runtime home does."""
        home = self.case / "install"
        home.mkdir(exist_ok=True)
        (home / "current").symlink_to(release)
        return home / "current"

    @staticmethod
    def move(link, release):
        fresh = link.with_name(link.name + ".next")
        fresh.symlink_to(release)
        os.replace(fresh, link)

    def upgrade_note(self, rig):
        screen = rig.screen()
        return "New release waiting" in screen or "새 판 대기" in screen


class AnswerReachesIdleSessionTest(AnswerReachesIdleShellTest):
    def _takes_once(self, harness):
        rig = ShellRig(self, ROOT / "utilities/session-shell/entry.py", "current-" + harness, harness).start()
        signal_ = self.ask(harness)
        self.fleet_answer(signal_)
        self.courier()
        rig.wait(lambda: rig.prompts(), "the idle window never received the answer")
        self.courier(passes=3)
        states = self.states(signal_)
        self.assertEqual(len(rig.prompts()), 1, rig.prompts())
        self.assertIn("PRIVATE-ANSWER", rig.prompts()[0]["prompt"])
        self.assertEqual(states["delivery"]["state"], "acked", states)
        self.assertEqual(states["delivery"]["claim_authority"], "deliverer-unproven", states)
        self.assertEqual(states["request_state"], "applied", states)
        self.assertTrue(rig.wait(rig.idle_empty, "the window did not return to idle"))

    def test_current_claude_shell_takes_the_fleet_answer_once_without_a_person(self):
        self._takes_once("claude")

    def test_current_opencode_shell_takes_the_fleet_answer_once_without_a_person(self):
        self._takes_once("opencode")

    def test_a_typed_draft_keeps_the_answer_waiting_until_the_person_submits(self):
        rig = ShellRig(self, ROOT / "utilities/session-shell/entry.py", "draft").start()
        signal_ = self.ask()
        rig.type("half typed")
        rig.wait_for(b"half typed", "the draft never painted")
        self.fleet_answer(signal_)
        self.courier(passes=3)
        states = self.states(signal_)
        self.assertEqual(rig.prompts(), [], "the answer was typed over the person's draft")
        self.assert_field_wait(states, "native-pending", "not-idle-empty")
        # The person's own Enter submits their text; that prompt's hooks hand the answer over.
        rig.type("\r")
        rig.wait(lambda: rig.prompts(), "the person's prompt never ran")
        self.courier(passes=2)
        prompts = rig.prompts()
        self.assertEqual([row["prompt"] for row in prompts], ["half typed"])
        self.assertIn("PRIVATE-ANSWER", prompts[0]["context"])
        self.assertEqual(self.states(signal_)["request_state"], "applied", self.states(signal_))

    def _old_tree_or_skip(self):
        if self.old_tree is None:
            self.skipTest(OLD_RELEASE + " is not in this checkout's history")
        return self.old_tree

    def test_an_old_shell_host_never_takes_the_answer_and_says_where_it_stopped(self):
        rig = ShellRig(self, self._old_tree_or_skip() / "utilities/session-shell/entry.py", "old").start()
        signal_ = self.ask()
        self.fleet_answer(signal_)
        self.courier(passes=4)
        self.assertTrue(rig.idle_empty(), "the old window is not idle and empty")
        self.assertEqual(rig.prompts(), [])
        # Idle and empty, its host still refuses: its backend cannot verify this release's answer
        # notice, so the host's payload step answers `peer-claim-changed` and starts nothing.
        self.assert_field_wait(self.states(signal_), "session-shell-not-started", "peer-claim-changed")
        # One prompt the person types in that window carries the answer to the session.
        rig.type("hello\r")
        rig.wait(lambda: rig.prompts(), "the person's prompt never ran")
        self.courier(passes=2)
        prompts = rig.prompts()
        self.assertEqual([row["prompt"] for row in prompts], ["hello"])
        self.assertIn("PRIVATE-ANSWER", prompts[0]["context"])
        self.assertEqual(self.states(signal_)["request_state"], "applied", self.states(signal_))

    def test_an_old_window_after_an_install_shows_new_release_and_the_answer_says_why_it_waits(self):
        import session_shell
        from fleet.session_block import sent_answer_line
        link = self.install(self._old_tree_or_skip())
        rig = ShellRig(self, link / "utilities/session-shell/entry.py", "installed").start()
        signal_ = self.ask()
        # Something in the composer: the window neither takes the answer nor hands over yet.
        rig.type("still typing")
        rig.wait_for(b"still typing", "the draft never painted")
        self.assertFalse(session_shell.release_behind(rig.binding))
        self.move(link, ROOT)
        # Its host's own rule (the realpath its launch path names now) says it is behind. Its v4.29.3
        # side panel drew no `New release waiting` here; a window whose launch path was pinned to a
        # release (every Herdr window before this change) cannot see the next release at all.
        rig.wait(lambda: session_shell.release_behind(rig.current_binding()), "the move was not seen as a newer release")
        self.assertEqual(session_shell.answer_delivery_wait("claude", SID), "old-shell")
        board = [{"id": signal_["id"], "kind": "message", "answer": "sent", "owner_state": "live",
                  "harness": "claude", "session_id": SID}]
        self.assertEqual(session_shell.mark_answer_waits(board)[0]["answer_wait"], "old-shell")
        self.fleet_answer(signal_)
        self.courier(passes=3)
        self.assertEqual(rig.prompts(), [])
        # The records of the stuck window seen on 11-16, by name.
        self.assert_field_wait(self.states(signal_), "native-pending", "not-idle-empty")
        line = "".join(text for text, _key in sent_answer_line(["PRIVATE-ANSWER"], wait="old-shell"))
        self.assertRegex(line, r"PRIVATE-ANSWER · (awaiting delivery|전달 대기) · (New release waiting|새 판 대기) · ")
        # The person's Enter in that window hands the answer over; then the window takes the new release.
        rig.type("\r")
        rig.wait(lambda: rig.prompts(), "the person's prompt never ran")
        self.courier(passes=2)
        self.assertIn("PRIVATE-ANSWER", rig.prompts()[0]["context"])
        self.assertEqual(self.states(signal_)["request_state"], "applied", self.states(signal_))
        rig.wait(lambda: rig.idle_empty() and session_shell.release_behind(rig.current_binding()) is False,
                 "the window never handed over to the installed release")
        self.assertEqual(session_shell.answer_delivery_wait("claude", SID), "")
        self.assertNotIn("answer_wait", session_shell.mark_answer_waits(board)[0])

    def test_a_herdr_window_keeps_its_installed_launch_path_and_sees_the_next_release(self):
        import session_shell
        link = self.install(ROOT)
        herdr = {"HERDR_ENV": "1", "HERDR_PANE_ID": "private:p1", "HERDR_BIN_PATH": "/nonexistent/herdr",
                 "HERDR_SOCKET_PATH": str(self.case / "absent-herdr.sock")}
        rig = ShellRig(self, link / "utilities/session-shell/entry.py", "herdr", env=herdr).start()
        environ = Path("/proc", str(rig.binding["host_pid"]), "environ").read_bytes().split(b"\0")
        launch = next(entry for entry in environ if entry.startswith(b"HEARTING_SHELL_HOST_PATH="))
        # The installed link as the launcher named it: the release it points at may move.
        self.assertEqual(launch.split(b"=", 1)[1].decode(), str(link / "utilities/session-shell/host.cjs"))
        self.assertIn(b"HERDR_AGENT=claude", Path("/proc", str(rig.binding["native_pid"]), "environ").read_bytes())
        rig.type("held")
        rig.wait_for(b"held", "the draft never painted")
        # The next release, installed while the window runs (the draft holds its hand-over).
        following = self.case / "following-release/utilities/session-shell"
        following.mkdir(parents=True)
        shutil.copy2(ROOT / "utilities/session-shell/host.cjs", following / "host.cjs")
        self.move(link, self.case / "following-release")
        rig.wait(lambda: self.upgrade_note(rig), "a Herdr window never noticed the next release")
        self.assertTrue(session_shell.release_behind(rig.current_binding()))


class SentAnswerLineTest(unittest.TestCase):
    """The same line in Fleet and the panel; in no count; nothing to send again (rule 5)."""

    def setUp(self):
        for path in (ROOT / "tools", ROOT / "utilities"):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))

    def snapshot(self, wait=None):
        item = {"id": "S4:sent", "source": "S4", "kind": "message", "since": 900, "stale": "current",
                "harness": "claude", "session_id": "sid-a", "answer": "sent"}
        if wait:
            item["answer_wait"] = wait
        from fleet.refresh import fleet_answer_wait_view
        return {"sessions": [{"harness": "claude", "session_id": "sid-a", "session_tag": "a1",
                              "cwd": "/synthetic/repo", "liveness": "idle"}],
                "project_of": lambda _cwd: "repo",
                "answer_waits": fleet_answer_wait_view({"items": [item], "total": 0, "older": 0,
                                                        "complete": True, "health": []}),
                "resources": [], "jobs": []}

    def board(self, vm, state=None):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import NavState
        return draw(vm, state or NavState(view="QUESTIONS", title="QUESTIONS"), 220, 50, beat=1).text_rows()

    def test_fleet_row_says_the_answer_awaits_delivery_and_why_and_counts_nothing(self):
        from fleet.config import ui_text
        from fleet.explorer.live import build_view_model
        for wait, why in (("", None), ("old-shell", "session_shell.upgrade_pending"),
                          ("no-shell", "fleet.session_block.wait_no_shell")):
            with self.subTest(wait=wait):
                vm = build_view_model(self.snapshot(wait), now=1000)
                self.assertEqual([(q["sent"], q["answer_wait"]) for q in vm.questions], [(True, wait or None)])
                rows = self.board(vm)
                row = next(line for line in rows if "[a1]" in line)
                expected = ui_text("fleet.session_block.sent").removeprefix("✓").strip() + " · " + ui_text(
                    "fleet.session_block.delivery_waiting")
                self.assertIn(expected, row)
                self.assertIn("✓", row)
                if why:
                    self.assertIn(ui_text(why), row)
                from fleet.explorer.tables import answer_wait_word
                self.assertIn(answer_wait_word() + " 0 ", next(line for line in rows if answer_wait_word() in line))
                self.assertTrue(all(not session.get("ask") for session in vm.sessions))

    def test_fleet_unfolded_answer_shows_what_was_sent_and_offers_no_resend(self):
        from fleet.config import ui_text
        from fleet.explorer.live import build_view_model
        from fleet.explorer.state import NavState
        vm = build_view_model(self.snapshot("old-shell"), now=1000)
        question = vm.questions[0]
        envelope = {"v": 1, "harness": "claude", "session_id": "sid-a", "request_id": "S4:sent",
                    "kind": "question", "state": "submitted", "support": {"supported": True},
                    "detail": {"questions": [{"id": "message-response", "question": "Which one?",
                                              "choices": [], "is_other": True}]},
                    "question_id": "message-response", "sent": {"answers": {"message-response": ["PRIVATE-ANSWER"]}},
                    "native_ref": {"transport": "hearting-message-answer"}}
        state = NavState(view="QUESTIONS", folds=(("qsel:QUESTIONS", question["tag"]),),
                         question_overlay={"ref": dict(question["ref"]), "detail": envelope, "draft": None,
                                           "state": "submitted", "editing": False, "pending": False})
        text = "\n".join(self.board(vm, state))
        self.assertIn("PRIVATE-ANSWER · " + ui_text("fleet.session_block.delivery_waiting"), text)
        self.assertIn(ui_text("fleet.session_block.wait_old_shell"), text)
        self.assertNotIn("submit", text)

    def test_the_panel_receives_the_same_parts_fleet_draws(self):
        from fleet.session_block import question_presentations, sent_answer_line
        item = {"id": "S4:sent", "revision": "r", "answerable": False, "request_state": "submitted",
                "sent": ["PRIVATE-ANSWER"], "questions": [{"id": "message-response", "question": "Which one?"}]}
        question_presentations([item], 80, brief_columns=36)
        self.assertEqual(item["sent_line"], sent_answer_line(["PRIVATE-ANSWER"]))
        self.assertEqual([part[1] for part in item["sent_line"][:3]], ["lvl_g", "dim", "loc_repo"])
        delivered = dict(item, request_state="applied")
        question_presentations([delivered], 80)
        self.assertNotIn("sent_line", delivered)

    def test_secrets_stay_hidden_in_the_line(self):
        from fleet.session_block import sent_answer_line
        line = "".join(text for text, _key in sent_answer_line(
            {"answers": {"q": ["PRIVATE-SECRET"]}}, questions=[{"id": "q", "is_secret": True}]))
        self.assertNotIn("PRIVATE-SECRET", line)
        self.assertIn("•••", line)

    def test_what_each_harness_waits_for_without_a_shell(self):
        import session_shell
        with mock.patch.object(session_shell, "exact_binding", return_value=(None, "attachment-unavailable")):
            self.assertEqual(session_shell.answer_delivery_wait("claude", "sid-a"), "no-shell")
            # OpenCode's plugin takes it when idle; Codex from its own queue.
            self.assertEqual(session_shell.answer_delivery_wait("opencode", "sid-a"), "")
            self.assertEqual(session_shell.answer_delivery_wait("codex", "sid-a"), "")
        with mock.patch.object(session_shell, "exact_binding", return_value=(None, "attachment-ambiguous")):
            self.assertEqual(session_shell.answer_delivery_wait("claude", "sid-a"), "")
        with mock.patch.object(session_shell, "exact_binding", return_value=(None, "attachment-unavailable")):
            board = [{"kind": "message", "answer": "sent", "owner_state": state, "harness": "claude",
                      "session_id": "sid-a"} for state in ("live", "none", "successor")]
            board.append({"kind": "message", "harness": "claude", "session_id": "sid-a"})
            self.assertEqual([item.get("answer_wait") for item in session_shell.mark_answer_waits(board)],
                             ["no-shell", None, None, None])

    def test_each_harness_takes_one_answer_once_through_the_existing_courier(self):
        from fixtures.message_questions import MessageQuestionFixture
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness), MessageQuestionFixture() as fixture:
                signal_, envelope = fixture.ask(harness)
                fixture.submit(envelope, "PRIVATE-ANSWER")
                calls, ref, row = fixture.courier(signal_)
                self.assertEqual(len(calls), 1, calls)
                self.assertIn("PRIVATE-ANSWER", calls[0])
                fixture.consume(signal_, calls[0])
                # Received once: the record is acked and the question applied, so no pass sends it again.
                self.assertEqual(fixture.record(signal_)["state"], "acked")
                import human_wait_state as waits
                self.assertEqual(waits.read_selected_request(
                    {"id": signal_["id"], "harness": harness, "session_id": signal_["session_id"]})["state"], "applied")


if __name__ == "__main__":
    unittest.main()
