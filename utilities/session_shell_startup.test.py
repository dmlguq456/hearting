"""Startup ordering and same-child fallback, using private real PTYs and no model."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import fcntl
import importlib.util
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('startup_fixture', ROOT/'utilities/session_shell_pty.test.py')
fixture = importlib.util.module_from_spec(spec); sys.modules[spec.name] = fixture; spec.loader.exec_module(fixture)


class SessionShellStartupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionShellPTYTest.setUpClass.__func__(cls)

    def run_start(self, mode='normal', bad_assets=False, fast_exit=False):
        directory = self.root/self._testMethodName; directory.mkdir(mode=0o700)
        pidfile = directory/'native.json'; output = directory/'input.json'
        ready = directory/'vt-ready'; gate = directory/'vt-gate'; os.mkfifo(gate, 0o600)
        log = directory/'nodes.jsonl'; native = directory/'native.py'
        native.write_text("import json,os,sys,tty,termios\nfrom pathlib import Path\n"
            "root=Path(os.environ['STARTUP_CASE_DIR']);tty.setraw(0,when=termios.TCSANOW)\n"
            "with (root/'native.json').open('a') as f:f.write(json.dumps({'pid':os.getpid(),'argv':sys.argv[1:]})+'\\n')\n"
            "os.write(1,b'NATIVE_READY\\r\\n')\n"
            "if os.environ.get('STARTUP_FAST_EXIT'):\n os.write(1,b'NATIVE_FAST_EXIT\\r\\n');os._exit(7)\n"
            "data=b''\n"
            "while b'\\r' not in data:\n data+=os.read(0,65536)\n"
            "(root/'input.json').write_text(json.dumps({'pid':os.getpid(),'bytes':data.hex()}))\n"
            "os.write(1,b'NATIVE_DONE\\r\\n')\n")
        binary = directory/'bin'; binary.mkdir(); node = binary/'node'
        node.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
            +'with open('+repr(str(log))+",'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')\n"
            +'os.execv('+repr(self.node)+',['+repr(self.node)+',*sys.argv[1:]])\n'); node.chmod(0o700)
        preload = directory/'gate.cjs'
        preload.write_text("const fs=require('node:fs'),M=require('node:module'),load=M._load;\n"
            "if(process.argv.some(a=>a.endsWith('/session-shell/host.cjs')))M._load=function(name,...args){\n"
            " if(name==='@xterm/headless'&&process.env.STARTUP_MODE!=='normal'){\n"
            "  fs.writeFileSync(process.env.STARTUP_READY,JSON.stringify({native:Number(process.env.HEARTING_SHELL_NATIVE_PID)}));\n"
            "  const fd=fs.openSync(process.env.STARTUP_GATE,fs.constants.O_RDWR);fs.readSync(fd,Buffer.alloc(1),0,1,null);fs.closeSync(fd);\n"
            "  if(process.env.STARTUP_MODE==='fail')throw Error('owned VT preparation failure');\n"
            " }return load.call(this,name,...args);};\n")
        env = dict(self.env, STARTUP_CASE_DIR=str(directory), STARTUP_READY=str(ready),
                   STARTUP_GATE=str(gate), STARTUP_MODE=mode, NODE_OPTIONS='--require '+str(preload),
                   PATH=str(binary)+os.pathsep+self.env['PATH'], STARTUP_FAST_EXIT='1' if fast_exit else '')
        manifest = Path(self.env['NODE_PATH']).parent/'ready.json'; before = manifest.read_bytes()
        if bad_assets: manifest.write_text('{"key":"wrong-ABI-assets"}')
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
        child = None; raw = bytearray(); began = None; fork_pid = None; input_bytes = 'first 한글 second\r'.encode()
        def wait_file(path):
            until = time.monotonic()+5
            while not path.exists() and time.monotonic()<until:
                if child.poll() is not None: break
                time.sleep(.005)
            return path.exists()
        def release():
            fd = os.open(gate, os.O_RDWR|os.O_NONBLOCK)
            try: os.write(fd,b'1')
            finally: os.close(fd)
        try:
            child = subprocess.Popen([sys.executable,str(ROOT/'utilities/session-shell/entry.py'),
                'claude',sys.executable,str(native),'a b','--native-flag'],env=env,cwd=self.project,
                stdin=slave,stdout=slave,stderr=slave,start_new_session=True,
                preexec_fn=lambda: fcntl.ioctl(0,termios.TIOCSCTTY,0))
            os.close(slave); slave = None
            if mode!='normal':
                self.assertTrue(wait_file(ready),'VT preparation did not reach its barrier')
                fork_pid = json.loads(ready.read_text())['native']
                began = wait_file(pidfile)
                if fast_exit and began:
                    native_pid = json.loads(pidfile.read_text().splitlines()[0])['pid']
                    try:
                        fd = os.pidfd_open(native_pid)
                    except ProcessLookupError:
                        pass
                    else:
                        try: self.assertTrue(select.select([fd],[],[],5)[0],'native did not exit')
                        finally: os.close(fd)
                if not fast_exit: os.write(master,input_bytes[:-1])
                release()
                self.assertTrue(wait_file(pidfile),"native did not reach raw input")
                if not fast_exit: os.write(master,input_bytes[-1:])
            else:
                self.assertTrue(wait_file(pidfile),'native did not start')
                if not fast_exit: os.write(master,input_bytes)
            until = time.monotonic()+8
            while time.monotonic()<until:
                if select.select([master],[],[],.1)[0]:
                    try: data=os.read(master,65536)
                    except OSError: break
                    if not data: break
                    raw.extend(data)
                if child.poll() is not None: break
            child.wait(timeout=5)
            rows = [json.loads(line) for line in pidfile.read_text().splitlines()]
            self.assertEqual(child.returncode,7 if fast_exit else 0,raw.decode(errors='replace'))
            self.assertEqual(len(rows),1,'native was started more than once')
            if fork_pid is not None:self.assertEqual(rows[0]['pid'],fork_pid)
            self.assertEqual(rows[0]['argv'],['a b','--native-flag'])
            if fast_exit:
                self.assertIn(b'NATIVE_FAST_EXIT',raw,'exited native output was lost during display failure')
            else:
                result = json.loads(output.read_text())
                self.assertEqual(result,{'pid':rows[0]['pid'],'bytes':input_bytes.hex()})
            return began,[json.loads(line) for line in log.read_text().splitlines()]
        finally:
            release()
            if child is not None and child.poll() is None:
                try: os.write(master,b'cleanup\r');child.wait(timeout=3)
                except (OSError,subprocess.TimeoutExpired):
                    from fixture_processes import reap
                    reap(str(directory));child.wait(timeout=3)
            os.close(master)
            if slave is not None:os.close(slave)
            manifest.write_bytes(before)

    def test_no_auxiliary_node_probe_on_the_start_path(self):
        _, launches = self.run_start()
        self.assertEqual(len(launches),1,launches)

    def test_native_release_keeps_the_existing_vt_preparation_boundary(self):
        began,_ = self.run_start('gate')
        self.assertFalse(began,'native execution escaped the existing startup boundary')

    def test_vt_preparation_failure_preserves_the_same_native_and_early_keys(self):
        began,_ = self.run_start('fail')
        self.assertFalse(began,'failed preparation started a different wrapped invocation')

    def test_fast_native_exit_during_vt_failure_keeps_its_output(self):
        self.run_start('fail', fast_exit=True)

    def test_wrong_asset_manifest_runs_the_original_command_once(self):
        self.run_start(bad_assets=True)


if __name__=='__main__': unittest.main()
