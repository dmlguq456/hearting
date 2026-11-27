"""11-26: the shell must not mix a person's input with other bytes, or lose it.

Three faults, each closed here:
1. A paste's end mark and a mouse report in one read: the report went to the native whole (`panel-mouse.cjs` passed the
   read through as the paste's). Bytes after the end mark are read as usual.
2. `ESC a` (Alt+letter) left a key front that never ended, so the next Ctrl+] did not open the popup.
3. A shell stopped by SIGTSTP and continued left the outer tty cooked; `pty_owner.py` sets it raw again on SIGCONT.

The Node checks run host.cjs's own functions on synthetic bytes. The tty check runs the real owner in a private PTY
(its own session; the signals go to the pid the test started and no other), with a stand-in display host.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import fcntl
import pty
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
NODE = shutil.which("node")


def node(script):
    result = subprocess.run([NODE, "-e", script, str(SHELL)], capture_output=True, text=True, timeout=20)
    return result


@unittest.skipUnless(NODE, "needs node")
class PasteEndAndMouseTest(unittest.TestCase):
    def test_bytes_after_a_paste_end_in_the_same_read_are_read_as_usual(self):
        script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),decode=require(process.argv[1]+'/panel-mouse.cjs');
// The host's own wiring: which states pass a read through whole, which only until a paste ends.
const host=fs.readFileSync(process.argv[1]+'/host.cjs','utf8');
const wiring=/suppressed:(\(\)=>[^\n]*?),\n(?:\s*pasting:(\(\)=>[^\n]*?),\n)?/.exec(host);assert(wiring,'the host wires the mouse reader');
if(!wiring[2])wiring[2]='()=>false';  // (before: one `suppressed` for every state, a paste included)
const state={paste:false,block:{pasting:false},settingsPaste:false,relay:false,fleetView:undefined};
const make=()=>{let out='',events=[];
  const d=decode({bytes:b=>{out+=b.toString('latin1');
      if(out.endsWith('\x1b[200~'))state.paste=true;if(out.endsWith('\x1b[201~'))state.paste=state.block.pasting=state.settingsPaste=false;},
    mouse:(...e)=>events.push(e.slice(0,4)),
    suppressed:new Function('state','with(state){return ('+wiring[1]+')}')(state),
    pasting:new Function('state','with(state){return ('+wiring[2]+')}')(state)});
  return {d,get out(){return out;},events};};
const REPORT='\x1b[<35;95;1M',END='\x1b[201~';
// 1. end mark and report in one read: the report is the panel's, the paste is whole.
{state.paste=false;const m=make();m.d.feed(Buffer.from('\x1b[200~'));assert.equal(state.paste,true);
 m.d.feed(Buffer.from('text'+END+REPORT+'tail'));
 assert.equal(m.out,'\x1b[200~text'+END+'tail');assert.deepEqual(m.events,[[35,95,1,false]]);assert.equal(state.paste,false);}
// 2. a mouse-looking text inside the paste stays literal; only what follows the end mark is read.
{state.paste=false;const m=make();m.d.feed(Buffer.from('\x1b[200~'));
 m.d.feed(Buffer.from('a'+REPORT+'b'+END+REPORT));
 assert.equal(m.out,'\x1b[200~a'+REPORT+'b'+END);assert.equal(m.events.length,1);}
// 3. the end mark split across reads, the report in the read that finishes it.
for(let cut=1;cut<END.length;cut++){state.paste=false;const m=make();m.d.feed(Buffer.from('\x1b[200~'));
  m.d.feed(Buffer.from('x'+END.slice(0,cut)));m.d.feed(Buffer.from(END.slice(cut)+REPORT+'y'));
  assert.equal(m.out,'\x1b[200~x'+END+'y','cut '+cut);assert.equal(m.events.length,1,'cut '+cut);}
// 4. a paste that begins in the read itself, with a report after its end.
{state.paste=false;const m=make();m.d.feed(Buffer.from('\x1b[200~p'+END+REPORT));
 assert.equal(m.out,'\x1b[200~p'+END);assert.equal(m.events.length,1);}
// 5. relay and Fleet keep the whole read, as before, paste end or not.
for(const key of ['relay','fleetView']){state.paste=false;state[key]=key==='relay'?true:{};
  const m=make();m.d.feed(Buffer.from('a'+END+REPORT));assert.equal(m.out,'a'+END+REPORT,key);assert.equal(m.events.length,0,key);state[key]=key==='relay'?false:undefined;}
// 6. the block editor's and the settings editor's pastes end the same way.
for(const key of ['block','settingsPaste']){state.paste=false;if(key==='block')state.block.pasting=true;else state.settingsPaste=true;
  const m=make();m.d.feed(Buffer.from('z'+END+REPORT));
  assert.equal(m.out,'z'+END,key);assert.equal(m.events.length,1,key);}
"""
        result = node(script)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(NODE, "needs node")
class AltLetterThenCtrlBracketTest(unittest.TestCase):
    def test_escape_and_one_letter_is_a_finished_key_and_the_next_ctrl_bracket_opens_the_popup(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),a=require('node:assert/strict');
const text=fs.readFileSync(process.argv[1]+'/host.cjs','utf8');
const piece=name=>{const begin=text.search(new RegExp('^(?:async )?function '+name+'\\(','m'));return text.slice(begin,text.indexOf('\n}',begin)+2);};
const code=text.match(/^const QUIET_KEY=.*;$/m)[0]+'\n'+piece('observeInput')+'\n'+piece('inputBytes');
function make(){
  const written=[],opened=[];
  const s={Buffer,String,epoch:0,nativeEpoch:0,lastEnter:-1,typed:0,draftChars:0,empty:true,keyTail:'',paste:false,relay:false,
    currentDelivery:null,screenRevision:0,keyRevision:-1,keyAt:0,performance:{now:()=>0},settings:undefined,table:null,
    fleetView:undefined,heldBytes:0,activated:true,ended:false,succeeding:false,
    block:{focused:false,question:null,answeredEscape:()=>false},popupShown:()=>false,sections:()=>['FLEET'],
    openPopup:name=>opened.push(name),queueDisplay(){},rebuildDisplay(){},child:{write:b=>written.push(Buffer.from(b).toString('latin1'))}};
  vm.createContext(s);vm.runInContext(code+'\nthis.input=inputBytes;this.observe=observeInput;',s);
  return {s,written,opened};
}
const alt=key=>{const m=make();m.s.input(Buffer.from(key,'latin1'));return m;};
// Alt+letter alone is a finished key: nothing waits for a rest.
for(const key of ['\x1ba','\x1bb','\x1bz','\x1bA','\x1b1','\x1b\r','\x1b ','\x1b\x7f','\x1b.']){
  const m=alt(key);a.equal(m.s.keyTail,'',JSON.stringify(key)+' ends');a.deepEqual(m.written,[key]);}
// The keys that do open a longer one keep waiting.
for(const key of ['\x1b[','\x1bO','\x1b]','\x1bP','\x1b_','\x1b']){a.equal(alt(key).s.keyTail,key,JSON.stringify(key)+' waits');}
// A key split across reads: ESC then the letter.
{const m=make();m.s.input(Buffer.from('\x1b'));m.s.input(Buffer.from('a'));a.equal(m.s.keyTail,'');}
// The report: Alt+a, then Ctrl+] opens the popup (in one read and in two).
for(const reads of [['\x1ba\x1d'],['\x1ba','\x1d'],['\x1b','a','\x1d'],['x','\x1bb','\x1d']]){
  const m=make();for(const r of reads)m.s.input(Buffer.from(r,'latin1'));
  a.deepEqual(m.opened,['FLEET'],JSON.stringify(reads)+' opens the popup');
}
// Unchanged: a paste's Ctrl+] is literal, a CSI key before it is a finished key, a lone Esc before it still opens.
{const m=make();m.s.input(Buffer.from('\x1b[200~a\x1d'));a.deepEqual(m.opened,[]);a.equal(m.written.join(''),'\x1b[200~a\x1d');}
{const m=make();m.s.input(Buffer.from('\x1b[A\x1d'));a.deepEqual(m.opened,['FLEET']);}
{const m=make();m.s.input(Buffer.from('\x1b\x1d'));a.deepEqual(m.opened,['FLEET']);}
// A typed Alt+letter is typing for readiness (not a quiet key).
{const m=alt('\x1ba');a.equal(m.s.typed,m.s.nativeEpoch);}
"""
        result = node(script)
        self.assertEqual(result.returncode, 0, result.stderr)


DRIVER = r"""
import os, sys, time
sys.path[:0] = [sys.argv[1]]
import pty_owner
os.environ.setdefault('PATH', '/usr/bin:/bin')
code = pty_owner.run('claude', ['true'], ['true'], sys.executable, sys.argv[2],
                     {'HEARTING_SHELL_SOCKET': 'unused', 'HEARTING_SHELL_TOKEN': 'unused'}, sys.argv[3])
sys.exit(code or 0)
"""
# The stand-in display host: it never paints, so the owner keeps it as the display and never relays.
FAKE_HOST = "import time\ntime.sleep(60)\n"
# A stand-in job-control shell: the private PTY's session leader. The owner runs in a group of its own that is the
# tty's foreground (a stop signal is dropped in an orphaned group, so the leader must stay as the group's parent).
SHELL_DRIVER = r"""
import os, signal, sys, time
child = os.fork()
if child == 0:
    os.setpgid(0, 0)
    while os.tcgetpgrp(0) != os.getpgrp():time.sleep(0.02)
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
os.setpgid(child, child)
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
os.tcsetpgrp(0, child)
open(sys.argv[-1] + '/owner-pid', 'w').write(str(child))
while True:
    try:
        pid, status = os.waitpid(child, os.WUNTRACED)
    except ChildProcessError:
        break
    if os.WIFSTOPPED(status):continue
    break
"""


class OuterTtyAfterStopTest(unittest.TestCase):
    def raw(self, fd):
        lflag = termios.tcgetattr(fd)[3]
        return not lflag & (termios.ICANON | termios.ECHO)

    def wait_for(self, fd, want, limit=8):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if self.raw(fd) == want:
                return True
            time.sleep(0.05)
        return False

    def stopped(self, pid, limit=8):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0] == "T":
                return True
            time.sleep(0.05)
        return False

    def test_a_stopped_and_continued_owner_sets_the_outer_tty_raw_again(self):
        with tempfile.TemporaryDirectory(prefix="hearting-stop-", dir="/var/tmp") as tmp:
            tmp = Path(tmp)
            (tmp / "driver.py").write_text(DRIVER)
            (tmp / "host.py").write_text(FAKE_HOST)
            (tmp / "shell.py").write_text(SHELL_DRIVER)
            leader, master = pty.fork()
            if leader == 0:
                try:
                    os.execv(sys.executable, [sys.executable, str(tmp / "shell.py"), str(tmp / "driver.py"),
                                              str(SHELL), str(tmp / "host.py"), str(tmp)])
                finally:
                    os._exit(127)
            pid = None
            try:
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                self.assertTrue(self.wait_for(master, True), "the owner sets the outer tty raw")
                pid = int((tmp / "owner-pid").read_text())
                cooked = termios.tcgetattr(master)
                cooked[3] |= termios.ICANON | termios.ECHO
                for turn in range(2):
                    # What a job-control shell leaves after the stop: its own (cooked) modes.
                    termios.tcsetattr(master, termios.TCSANOW, cooked)
                    os.kill(pid, signal.SIGTSTP)
                    self.assertTrue(self.stopped(pid), "the owner stops")
                    self.assertFalse(self.raw(master))
                    os.kill(pid, signal.SIGCONT)
                    self.assertTrue(self.wait_for(master, True), f"the tty is raw again after SIGCONT (turn {turn})")
            finally:
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGCONT)
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                os.close(master)
                try:
                    os.kill(leader, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(leader, 0)
                except ChildProcessError:
                    pass


if __name__ == "__main__":
    unittest.main()
