"""Native screen observations are a display cache, never session/input authority."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import subprocess,unittest
ROOT=Path(__file__).resolve().parents[1]
class NativeDisplayTest(unittest.TestCase):
 def test_first_cached_value_does_not_wait_for_the_projection(self):
  source=ROOT/'utilities/session-shell/native-display.cjs'
  script="""const assert=require('node:assert/strict'),d=require(process.argv[1]);
const sid='00000000-0000-4000-8000-000000000052';
const n=d.observe('codex',['body','',sid+' · Context 0% used','? shortcuts'],sid);
assert.equal(n.percent,0);assert.equal(d.firstLine(n)[0].text,'context 0%');
assert.equal(d.observe('codex',['Context 99% used','body','',''],sid),null);
assert.equal(d.observe('codex',['','',sid+' · Context 0% used',''], 'other'),null);
"""
  p=subprocess.run(['node','-e',script,str(source)],text=True,capture_output=True)
  self.assertEqual(p.returncode,0,p.stderr)
 def test_opencode_uses_the_visible_sidebar_percentage(self):
  source=ROOT/'utilities/session-shell/native-display.cjs'
  script="""const assert=require('node:assert/strict'),d=require(process.argv[1]);
const lines=['assistant mentions 99% used',' '.repeat(80)+'Context',' '.repeat(80)+'20,010 tokens',' '.repeat(80)+'10% used'];
assert.equal(d.observe('opencode',lines,'exact').percent,10);
assert.equal(d.observe('opencode',['Context','20,010 tokens','10% used'],'exact'),null);
assert.equal(d.observe('opencode',[' '.repeat(80)+'Context',' '.repeat(80)+'20,010 tokens',' '.repeat(80)+'101% used'],'exact'),null);
"""
  p=subprocess.run(['node','-e',script,str(source)],text=True,capture_output=True)
  self.assertEqual(p.returncode,0,p.stderr)
 def test_a_source_that_was_not_read_keeps_the_panel_value_of_the_same_live_session(self):
  # Fleet's native-reader rule on the panel's own read: an unread tap keeps
  # the last value and the next projection reads again; another SID or PID,
  # an absent tap and an ended process never borrow it.
  script=r"""
import os,sys,json,errno,subprocess,tempfile
from pathlib import Path
from unittest import mock
W=sys.argv[1];R=Path(sys.argv[2])
os.environ.clear();os.environ.update(PATH='/usr/bin:/bin',HOME=str(R/'home'),XDG_CONFIG_HOME=str(R/'config'),
  XDG_STATE_HOME=str(R/'state'),XDG_CACHE_HOME=str(R/'cache'),AGENT_HOME=W,TMPDIR=str(R),
  CLAUDE_CONFIG_DIR=str(R/'home'/'.claude'),CODEX_HOME=str(R/'home'/'.codex'))
sys.path[:0]=[W,W+'/tools',W+'/utilities']
import session_shell
from fleet import native_display
from fleet.collectors import claude
from fleet.collectors.procscan import read_proc_start
kids=[subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']) for _ in range(2)]
try:
  sid='11111111-2222-3333-4444-555555555555';pid=kids[0].pid;start=read_proc_start(pid)
  tap=Path(claude._home())/'.statusline'/(sid+'.json');tap.parent.mkdir(parents=True)
  def write(pct):
    tmp=tap.with_suffix('.tmp');tmp.write_text(json.dumps({'session_id':sid,'model':{'display_name':'Fixture Model'},
      'context_window':{'used_percentage':pct}}));tmp.chmod(0o600);os.replace(tmp,tap)
  def seen(s=sid,p=pid,st=start):
    r=session_shell.panel_projection('claude',s,native_pid=p,native_start=st,columns=100,rows=30)
    return r['native_display']['model'],r['context']
  real=os.read
  def fail(fd,n):
    if n==65537:raise OSError(errno.EIO,'unread')
    return real(fd,n)
  def swap(fd,n):
    raw=real(fd,n)
    if n==65537:write(52)
    return raw
  write(47);assert seen()==('Fixture Model',47)
  with mock.patch.object(native_display.os,'read',side_effect=fail):assert seen()==('Fixture Model',47),seen()
  assert seen()==('Fixture Model',47)
  with mock.patch.object(native_display.os,'read',side_effect=swap):assert seen()==('Fixture Model',47)
  assert seen()==('Fixture Model',52)
  with mock.patch.object(native_display.os,'read',side_effect=fail):
    assert seen(s='99999999-2222-3333-4444-555555555555')==(None,None)
    assert seen(p=kids[1].pid,st=read_proc_start(kids[1].pid))==(None,None)
  assert seen()==('Fixture Model',52)
  tap.unlink();assert seen()==(None,None)
  with mock.patch.object(native_display.os,'read',side_effect=fail):assert seen()==(None,None)
  write(61);assert seen()==('Fixture Model',61)
  kids[0].kill();kids[0].wait();assert seen()==(None,None)
finally:
  for kid in kids:kid.kill();kid.wait()
"""
  import tempfile
  with tempfile.TemporaryDirectory() as root:
   p=subprocess.run(['python3','-I','-c',script,str(ROOT),root],text=True,capture_output=True,timeout=120)
  self.assertEqual(p.returncode,0,p.stderr)
if __name__=='__main__':unittest.main()
