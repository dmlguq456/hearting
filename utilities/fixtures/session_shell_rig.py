"""A private session-shell window for delivery tests: a real host in a private PTY.

The host (this checkout's, or an older release's from git) runs a synthetic native with a
private HOME; no model, user pane or account is used. The native acts as its harness does:
lifecycle events over the shell socket, its composer painted as the readiness reader expects,
the real Claude UserPromptSubmit hooks (or OpenCode's plugin receive helper) on every prompt it
takes, and local commands (``/model``) that open and close a menu with no prompt event (OpenCode's
``/models`` paints the real dialog of ``fixtures/opencode-2.0.32-dialogs``). ``ARI_NATIVE_MODE`` makes
it slow to call back after an Enter (``slow``) or slow to paint a key (``lag``). It reads its keys as a
terminal app does: cursor keys, focus and mouse reports and the terminal's own replies are taken without a
paint, and Up on an empty composer brings back the last prompt. ``ARI_NATIVE_MOUSE`` makes it turn on mouse
reporting, and a ``bgshell`` prompt ends its turn as Claude does while a background shell still runs
(session record ``shell``). The obligation runner's own pass is called in-process, so a suite owns every
process it starts.
"""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
OLD_RELEASE = "v4.29.3"
SID = "00000000-0000-4000-8000-0000000a1116"

FAKE_NATIVE = r"""
import json,os,re,subprocess,sys,time,tty
harness,sid,root,log=sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]
def start_of(pid):
    raw=open('/proc/%d/stat'%pid).read();return raw.rsplit(') ',1)[1].split()[19]
def note(**row):
    with open(log,'a') as f:f.write(json.dumps(row)+'\n')
def record(status,waiting=None):
    if harness!='claude':return
    path=os.path.join(os.environ['CLAUDE_CONFIG_DIR'],'sessions',str(os.getpid())+'.json')
    os.makedirs(os.path.dirname(path),exist_ok=True)
    with open(path,'w') as f:f.write(json.dumps({'pid':os.getpid(),'procStart':start_of(os.getpid()),'sessionId':sid,
        'status':status,**({'waitingFor':waiting} if waiting else {}),'statusUpdatedAt':int(time.time()*1000)}))
def event(kind):
    import socket
    record('busy' if kind=='prompt' else 'idle')
    request={'op':'event','event':kind,'harness':harness,'sid':sid,'pid':os.getpid(),'start':start_of(os.getpid()),
             'token':os.environ['HEARTING_SHELL_TOKEN'],'seq':time.monotonic_ns()//1000}
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(5);channel.connect(os.environ['HEARTING_SHELL_SOCKET'])
        channel.sendall((json.dumps(request)+'\n').encode());channel.recv(8192)
last='READY'
# `slow`: a person's Enter clears the composer at once and its turn calls back only once the test
# opens ARI_NATIVE_GATE; `lag`: after the warm-up turn, a typed key's own paint comes ARI_NATIVE_LAG
# seconds after a repaint of the old screen; `slow-up`: Up's recalled prompt is painted
# ARI_NATIVE_LAG seconds after the key and nothing is painted before it.
mode=os.environ.get('ARI_NATIVE_MODE','')
def dialog_screen():
    # OpenCode's model dialog as the real native draws it (fixtures/opencode-2.0.32-dialogs): the
    # rows that fit from the bottom, so its composer stays the last box, each cut to this width.
    size=os.get_terminal_size(0)
    path=os.path.join(root,'utilities','fixtures','opencode-2.0.32-dialogs',os.environ.get('ARI_DIALOG_SCREEN','models-120x50.txt'))
    rows=open(path,encoding='utf-8').read().rstrip('\n').split('\n')[-size.lines:]
    return '\r\n'.join(row[:size.columns-1] for row in rows)
sized=None
def fill():
    # `ARI_NATIVE_FILL=dense`: every row above the composer filled to the width, as a busy transcript
    # fills a wide window: width rules (three bytes a cell), Hangul (two cells, three bytes a letter)
    # and dim text; `heavy`: rows of accented letters, too much for one request. The native's own
    # size is noted once per size (`size`: rows, columns).
    global sized
    size=os.get_terminal_size(0)
    if sized!=size:sized=size;note(size=[size.lines,size.columns])
    fill=os.environ.get('ARI_NATIVE_FILL')
    if fill=='heavy':
        # Every cell a letter with twelve combining accents: a screen past what one request carries.
        return ''.join(('e'+'\u0301'*12)*size.columns+'\r\n' for n in range(size.lines-5))
    if fill!='dense':return ''
    rows=['\u2500'*size.columns if n%3==0 else '\ud55c\uae00'*(size.columns//4) if n%3==1
          else '\x1b[2m'+('dim transcript '*size.columns)[:size.columns]+'\x1b[22m' for n in range(size.lines-5)]
    return ''.join(row+'\r\n' for row in rows)
def paint(draft=''):
    if menu and harness=='opencode':
        print('\x1b[2J\x1b[H'+dialog_screen(),end='',flush=True);return
    if menu:
        # A local command's menu (`/model`): a form, never a composer.
        box='Select model\r\n  1. Default\r\n  2. Synthetic\r\nEnter to select \u00b7 Esc to cancel\r\n'
    elif harness=='claude':
        # `ARI_NATIVE_SUGGEST`: Claude's dim suggestion in an empty composer (not a draft).
        hint=os.environ.get('ARI_NATIVE_SUGGEST','') if not draft else ''
        box='\u276f '+draft+('\x1b[2m'+hint+'\x1b[22m' if hint else '')+'\r\n'+'\u2500'*18+'\r\n'
    elif harness=='codex':box='\u203a '+draft+'\r\n\r\n'
    else:box='\u2503  '+draft+'\r\n\u2503  Build  synthetic-model\r\n'
    print('\x1b[2J\x1b[H'+fill()+last+'\r\n'+box,end='',flush=True)
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
    if 'peer-from:' in line or 'runtime-from:' in line or re.search(r'(?m)^— (?:\[[0-9a-f?]{1,2}\]|claude|codex|opencode)( .*)?$',line) or line.lstrip().startswith('↳ '):
        run([os.path.join(root,'utilities','peer-message.py'),'receive','--from-project',os.path.basename(os.getcwd()),
             '--to-harness','opencode','--to-session-id',sid],line)
    return ''
menu=False
def close_menu(word):
    # The menu closes and the composer is empty again; a local command starts no turn, so no
    # prompt or stop callback is sent (Claude's UserPromptSubmit does not run for it either).
    global menu,last
    menu=False;last=word;record('idle');note(local=word);paint()
tty.setraw(0)
# `ARI_NATIVE_UNSELECTED`: as OpenCode on its home route, no session is selected until the first
# prompt's start hook (sent with the prompt, after an `ARI_NATIVE_GATE` a `slow` prompt waits on).
unselected=os.environ.get('ARI_NATIVE_UNSELECTED')=='1'
if not unselected:event('start')
print('\x1b[?2004h'+('\x1b[?1000h\x1b[?1006h' if os.environ.get('ARI_NATIVE_MOUSE') else ''),end='',flush=True);paint()
buffer='';draft='';pasting=False;prompts=0;warmed=False;history=[]
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
        if buffer=='\x1b':
            # A lone Esc (its own key, not the start of a sequence) dismisses an open menu.
            buffer=''
            if menu:close_menu('MENU_DISMISSED')
            continue
        if menu:
            char,buffer=buffer[0],buffer[1:]
            if char=='\r':close_menu('MODEL_CHOSEN')
            continue
        if buffer.startswith('\x1b') and len(buffer)<6 and '\x1b[200~'.startswith(buffer):break
        # The terminal's own replies (OSC, DCS strings) are read whole, never as typed text.
        reply=re.match(r'\x1b[\]P_][^\x07\x1b]*(?:\x07|\x1b\\)',buffer)
        if reply:buffer=buffer[reply.end():];continue
        if re.match(r'\x1b[\]P_][^\x07]*$',buffer) and len(buffer)<512:break
        key=re.match(r'\x1b\[[0-9;?<>=$]*[A-Za-z~]',buffer)
        if key:
            # Up on an empty composer brings back the last prompt (a draft, painted).
            if key.group()=='\x1b[A' and not draft and history:
                if mode=='slow-up':time.sleep(float(os.environ.get('ARI_NATIVE_LAG','4')))
                draft=history[-1];paint(draft)
            # Any other cursor key, a focus or mouse report or a terminal reply changes nothing on
            # screen: taken, never painted.
            buffer=buffer[key.end():];continue
        char,buffer=buffer[0],buffer[1:]
        if char=='\x7f':draft=draft[:-1];paint(draft);continue
        if char!='\r':
            if mode=='lag' and warmed:
                paint(draft);time.sleep(float(os.environ.get('ARI_NATIVE_LAG','1.5')))
            draft+=char if char>=' ' or char=='\n' else '';paint(draft);continue
        line,draft=draft,''
        history.append(line)
        if line=='warm':
            event('prompt');last='DONE_warm';paint();event('stop');warmed=True;continue
        if mode=='slow' and line.startswith('slow'):
            note(taken=line);paint()
            while not os.path.exists(os.environ['ARI_NATIVE_GATE']):time.sleep(0.05)
        if line.startswith('/') and harness!='opencode' or line.startswith('/models'):
            menu=True;record('waiting','dialog open');note(local=line);paint();continue
        prompts+=1
        context=take(line)
        if unselected:unselected=False;event('start')
        event('prompt')
        note(number=prompts,prompt=line,context=context)
        last='DONE_PROMPT_%d'%prompts;paint();event('stop')
        # The turn ended with a background shell still running: Claude writes `shell`, not `idle`.
        if line=='bgshell':record('shell')
"""


# The outer terminal's screen as a person sees it: the window's bytes through the shell's own
# headless xterm (its painter writes only changed cells, so the raw bytes do not hold whole words).
SCREEN = r"""
const fs=require('fs'),{Terminal}=require('@xterm/headless');
const term=new Terminal({cols:Number(process.argv[2]),rows:Number(process.argv[3]),allowProposedApi:true});
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

    def __init__(self, test, entry, name, harness="claude", env=None, size=(30, 130)):
        self.test, self.entry, self.name, self.harness = test, Path(entry), name, harness
        self.rows, self.cols = size
        self.env = dict(os.environ, **(env or {}))
        self.directory = test.case / ("window-" + name)
        self.directory.mkdir(mode=0o700)
        self.log = self.directory / "native.log"
        self.raw = bytearray()
        self.lock = threading.Lock()
        self.process = None
        self.master = None
        self.binding = None

    def start(self, idle=True, selected=True):
        """Start the window and its warm-up turn; ``idle``: until it reads idle with an empty composer.

        ``selected=False`` (a native started with ``ARI_NATIVE_UNSELECTED``): stop at the seat's
        unselected attachment (``sid="-"``), before any key or turn."""
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
        command = [sys.executable, str(self.entry), self.harness, sys.executable, str(self.test.root / "native.py"),
                   self.harness, SID, str(ROOT), str(self.log)]
        self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=self.env,
                                        cwd=self.test.root / "project", start_new_session=True, close_fds=True)
        os.close(slave)
        self.master = master
        threading.Thread(target=self._drain, daemon=True).start()
        self.test.addCleanup(self.stop)
        self.wait_for(b"READY", "the native never painted")
        if not selected:
            self.binding = self.wait(lambda: next((row for row in self.session_shell.bindings(
                harness=self.harness) if row["sid"] == "-"), None), "the shell never published its attachment")
            return self
        self.binding = self.wait(lambda: self.session_shell.exact_binding(self.harness, SID)[0],
                                 "the shell never published its attachment")
        self.type("warm\r")
        self.wait_for(b"DONE_warm", "the warm-up turn never ran")
        if idle:
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
        done = subprocess.run([shutil.which("node"), "-e", SCREEN, str(dump), str(self.cols), str(self.rows)],
                              capture_output=True, text=True,
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

    def notes(self):
        try:
            return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            return []

    def prompts(self):
        """The prompts the native took as turns (its hooks ran for each)."""
        return [row for row in self.notes() if "prompt" in row]

    def local_commands(self):
        """What the native did with local commands (a menu opened, chosen or dismissed)."""
        return [row["local"] for row in self.notes() if "local" in row]

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
