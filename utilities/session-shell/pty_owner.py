"""Retain the original PTY across display-worker failures; no normal-path I/O hop."""
from __future__ import annotations
import errno
import fcntl
import json
import os
import pty
import selectors
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty
from types import SimpleNamespace
from input_owner import InputOwner

RELAY_RESET=b'\x1b[0m\x1b[?25h\x1b[2J\x1b[H'
RESET=b'\x1b[0m\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?25h\x1b[?1049l'
LIMIT=65536
# Generation hand-over (plan C3/C4): a deadline per step, then the old display resumes.
PREPARE_LIMIT,FREEZE_LIMIT,COMMIT_LIMIT,SWITCH_LIMIT,TERM_LIMIT,MAX_FAILURES=10,3,5,5,2,3


class BeforeStartError(OSError):
    """No native process exists; the caller may still execute the original CLI."""


def birth(pid):
    if type(pid) is not int or pid<=1:raise ValueError('unsafe child pid')
    with open(f'/proc/{pid}/stat') as handle:
        return handle.read().rsplit(') ',1)[1].split()[19]


def resize(master,rows,cols):
    try:fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',rows,cols,0,0))
    except OSError:pass  # A display-size failure does not close the I/O channel.


def release_of(path):
    """The installed bundle behind a host path; None while an install is mid-switch."""
    try:return os.path.realpath(path,strict=True)
    except OSError:return None


def channels():
    """One display generation's private channels: ready/exit pipes and the input and succession sockets."""
    ready=os.pipe();status=os.pipe()
    try:control=socket.socketpair();succession=socket.socketpair()
    except OSError:
        for fd in (*ready,*status):os.close(fd)
        raise
    return SimpleNamespace(ready_r=ready[0],ready_w=ready[1],status_r=status[0],status_w=status[1],
                           control=control[0],display_control=control[1],succ=succession[0],succ_host=succession[1])


def run(harness,original,wrapped,node,host_path,environment,directory):
    """The display borrows the master; this owner relays only after it exits.

    A display generation can be replaced by a newer one while the native keeps
    its PID and this master (plan §3.0 C3): the successor is spawned, the old
    display freezes and writes its screen out, the successor restores it and
    takes the control socket, then the old one retires. Any failed or late step
    puts the old display back.
    """
    pipes=[]
    first=None;held_fd=None
    try:
        saved=termios.tcgetattr(0);blocking={fd:os.get_blocking(fd) for fd in (0,1)}
        size=os.get_terminal_size(1)
        for _ in range(2):pipes.extend(os.pipe())
        first=channels()
        held_fd=os.memfd_create('hearting-held-input',os.MFD_CLOEXEC)
        native,master=pty.fork()
    except OSError as exc:
        for fd in pipes:os.close(fd)
        if first is not None:
            for fd in (first.ready_r,first.ready_w,first.status_r,first.status_w):os.close(fd)
            for channel in (first.control,first.display_control,first.succ,first.succ_host):channel.close()
        if held_fd is not None:os.close(held_fd)
        raise BeforeStartError(str(exc)) from exc
    go_r,go_w,wake_r,wake_w=pipes
    owned={*pipes,first.ready_r,first.ready_w,first.status_r,first.status_w}
    if native==0:
        for channel in (first.control,first.display_control,first.succ,first.succ_host):channel.close()
        os.close(held_fd)
        for fd in (*pipes,first.ready_r,first.ready_w,first.status_r,first.status_w):
            if fd!=go_r:os.close(fd)
        try:
            mode=os.read(go_r,1);os.close(go_r)
            if not mode:os._exit(125)
            for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP,signal.SIGPIPE):signal.signal(sig,signal.SIG_DFL)
            env=dict(os.environ,PWD=os.getcwd())
            if mode==b'S':
                env.update(HEARTING_SHELL_SOCKET=environment['HEARTING_SHELL_SOCKET'],
                           HEARTING_SHELL_TOKEN=environment['HEARTING_SHELL_TOKEN'])
                # Only the started shell's native child gets the panel-side
                # native settings; a fallback start keeps the user's own.
                if 'OPENCODE_CLI_CONFIG_CONTENT' in environment:
                    env['OPENCODE_CLI_CONFIG_CONTENT']=environment['OPENCODE_CLI_CONFIG_CONTENT']
            command=wrapped if mode==b'S' else original
            os.execvpe(command[0],command,env)
        except BaseException as exc:
            os.write(2,(str(exc)+'\r\n').encode(errors='replace'));os._exit(127)
    os.close(go_r);owned.remove(go_r)
    resize(master,max(1,size.lines-1),size.columns)
    owner_start=birth(os.getpid())
    base=dict(environment,HEARTING_SHELL_PTY_FD=str(master),
              HEARTING_SHELL_NATIVE_PID=str(native),HEARTING_SHELL_HELD_FD=str(held_fd),
              HEARTING_SHELL_BROKER_PID=str(os.getpid()),HEARTING_SHELL_BROKER_START=owner_start,
              HEARTING_SHELL_HOST_PATH=str(host_path),HEARTING_SHELL_SUCCESSION='1')
    previous={}
    os.set_blocking(wake_r,False);os.set_blocking(wake_w,False);os.set_blocking(first.ready_r,False)
    def wake(_signal,_frame):
        try:os.write(wake_w,b'1')
        except OSError:pass
    for sig in (signal.SIGCHLD,signal.SIGWINCH,signal.SIGINT):
        previous[sig]=signal.signal(sig,signal.SIG_IGN if sig==signal.SIGINT else wake)
    cur=None;pending=None;retiring=[];state=None;failures={}
    native_code=None;deadline=None;started=False;relay=False
    incoming=bytearray();outgoing=bytearray();input_eof=False;master_eof=False
    selector=selectors.DefaultSelector();selector.register(wake_r,selectors.EVENT_READ)
    def close(fd):
        try:selector.unregister(fd)
        except (KeyError,ValueError):pass
        if fd in owned:
            owned.remove(fd)
            try:os.close(fd)
            except OSError:pass
    def unwatch(sock):
        try:selector.unregister(sock.fileno())
        except (KeyError,ValueError,OSError):pass
    def interest(fd,events):
        try:
            if events:selector.modify(fd,events)
            else:selector.unregister(fd)
        except KeyError:
            if events:selector.register(fd,events)
    def begin(mode):
        nonlocal started
        if not started:
            os.write(go_w,mode);close(go_w);started=True
    def plain():
        nonlocal relay
        if relay:return
        begin(b'R');relay=True;tty.setraw(0,when=termios.TCSANOW)
        for fd in (0,1,master):os.set_blocking(fd,False)
        outgoing.extend(RELAY_RESET)
        size=os.get_terminal_size(1);resize(master,max(1,size.lines),max(1,size.columns))
    def terminate_display(gen):
        if gen is not None and gen.host is not None and gen.host.poll() is None and not gen.term_sent:
            if gen.host.pid<=1:raise ValueError('unsafe display pid')
            try:
                if birth(gen.host.pid)!=gen.start:return
                signal.pidfd_send_signal(gen.fd,signal.SIGTERM);gen.term_sent=True
            except (FileNotFoundError,ProcessLookupError):pass
    def spawn_display(number,successor,c):
        """Start one display generation; its channels are closed on the parent side as it takes them over."""
        env=dict(base,HEARTING_SHELL_START_FD=str(c.ready_w),HEARTING_SHELL_PTY_EXIT_FD=str(c.status_r),
                 HEARTING_SHELL_INPUT_FD=str(c.display_control.fileno()),
                 HEARTING_SHELL_SUCCESSION_FD=str(c.succ_host.fileno()),HEARTING_SHELL_GENERATION=str(number))
        if successor:env['HEARTING_SHELL_SUCCESSOR']='1'
        gen=SimpleNamespace(number=number,host=None,start=None,fd=None,ready_r=c.ready_r,ready=b'',status_w=c.status_w,
                            writer=InputOwner(c.control,held_fd,incoming),succ=c.succ,buffer=bytearray(),term_sent=False,
                            successor=successor,release=release_of(host_path),term_deadline=None)
        gen.succ.setblocking(False)
        try:
            os.set_blocking(c.ready_r,False)
            os.set_blocking(master,False)
            gen.host=subprocess.Popen([node,str(host_path),harness,*wrapped],env=env,
                                      pass_fds=(master,c.ready_w,c.status_r,c.display_control.fileno(),held_fd,c.succ_host.fileno()))
            gen.start=birth(gen.host.pid);gen.fd=os.pidfd_open(gen.host.pid)
        except OSError:
            if gen.host is not None and gen.host.poll() is None:
                try:gen.host.terminate()
                except OSError:pass
            gen.error=True
        finally:
            close(c.ready_w);close(c.status_r);c.display_control.close();c.succ_host.close()
        selector.register(c.ready_r,selectors.EVENT_READ);selector.register(c.succ.fileno(),selectors.EVENT_READ)
        return gen
    def send(gen,message):
        try:gen.succ.send((json.dumps(message,separators=(',',':'))+'\n').encode());return True
        except OSError:return False
    def retire_generation(gen,term=True):
        """Drop a display this owner no longer routes to; it exits on its own, or by SIGTERM after a bound."""
        unwatch(gen.succ);gen.succ.close()
        unwatch(gen.writer.channel);gen.writer.channel.close()
        if gen.ready_r is not None:close(gen.ready_r);gen.ready_r=None
        if gen.status_w is not None:close(gen.status_w);gen.status_w=None
        if gen.host is not None and gen.host.poll() is None:
            gen.term_deadline=time.monotonic()+(0 if term else TERM_LIMIT)
            retiring.append(gen)
        elif gen.fd is not None:
            try:os.close(gen.fd)
            except OSError:pass
            gen.fd=None
    def abort(failed=True):
        """Put the old display back: undo the swap if it happened, wake it, drop the successor."""
        nonlocal state,cur,pending
        if state is None:return
        done,state=state,None
        old,new=done['old'],done['new']
        if cur is new:cur=old
        # Always told: a frozen host wakes; one that only asked learns the attempt is over (and whether to retry at once).
        send(old,{'op':'resume','failed':bool(failed)})
        if pending is new:pending=None
        retire_generation(new)
        if failed and done['release']:failures[done['release']]=failures.get(done['release'],0)+1
    def decline(gen,reason):send(gen,{'op':'declined','reason':reason})
    def begin_succession(gen):
        nonlocal state,pending
        target=release_of(host_path)
        if state is not None:return decline(gen,'busy')
        if relay or native_code is not None or gen is not cur or gen.host.poll() is not None:return decline(gen,'not-running')
        if target is None or target==gen.release:return decline(gen,'no-new-release')
        if failures.get(target,0)>=MAX_FAILURES:return decline(gen,'repeated-failure')
        try:c=channels()
        except OSError:return decline(gen,'resources')
        owned.update((c.ready_r,c.ready_w,c.status_r,c.status_w))
        new=spawn_display(gen.number+1,True,c)
        new.release=target
        if getattr(new,'error',False):
            retire_generation(new);return decline(gen,'spawn')
        pending=new
        state={'phase':'spawning','old':gen,'new':new,'release':target,'deadline':time.monotonic()+PREPARE_LIMIT}
    def swap_to(new):
        """The new display becomes the one this owner serves; its input channel takes over from the old one."""
        nonlocal cur
        old=cur
        if old.writer.ref is not None or old.writer.held or old.writer.responses or os.fstat(held_fd).st_size!=0:return False
        interest(old.writer.channel.fileno(),0)
        cur=new;return True
    def on_message(gen,message):
        nonlocal state,pending
        if message.get('op')=='request':
            return begin_succession(gen)
        if state is None:return
        phase=state['phase']
        if message.get('op')=='eof':
            if gen is state['old'] or gen is state['new']:abort()
            return
        if gen is state['old'] and phase=='freezing' and 'ok' in message:
            if message.get('ok') is True and isinstance(message.get('seed'),str):
                state.update(phase='committing',deadline=time.monotonic()+COMMIT_LIMIT)
                if not send(state['new'],{'op':'commit','seed':message['seed']}):abort()
            else:abort(failed=message.get('reason')!='unsafe')
        elif gen is state['new'] and phase=='committing' and 'ok' in message:
            if message.get('ok') is not True:return abort()
            if not swap_to(state['new']):return abort(failed=False)
            state.update(phase='switching',deadline=time.monotonic()+SWITCH_LIMIT)
            if not send(state['new'],{'op':'switch'}):abort()
        elif gen is state['new'] and phase=='switching' and message.get('settled') is True:
            done,state=state,None
            old,new=done['old'],done['new']
            pending=None;failures.pop(done['release'],None)
            send(old,{'op':'retire'});send(new,{'op':'run'})
            retire_generation(old,term=False)
    def on_ready(gen,chunk):
        nonlocal state
        if chunk:gen.ready+=chunk
        if b'\n' not in gen.ready and chunk:return
        close(gen.ready_r);gen.ready_r=None
        if not gen.successor:
            if gen.ready.startswith(b'STARTED'):begin(b'S')
            else:plain()
        elif state is not None and gen is state['new'] and state['phase']=='spawning' and gen.ready.startswith(b'PREPARED'):
            state.update(phase='freezing',deadline=time.monotonic()+FREEZE_LIMIT)
            if not send(state['old'],{'op':'freeze'}):abort()
        elif state is not None and gen is state['new']:abort()
    def drain(gen):
        try:data=gen.succ.recv(LIMIT)
        except BlockingIOError:return []
        except OSError:data=b''
        if not data:
            unwatch(gen.succ);return [{'op':'eof'}]
        gen.buffer.extend(data);messages=[]
        while b'\n' in gen.buffer:
            line,_,rest=bytes(gen.buffer).partition(b'\n');gen.buffer[:]=rest
            try:value=json.loads(line)
            except ValueError:value=None
            messages.append(value if isinstance(value,dict) else {'op':'invalid'})
        if len(gen.buffer)>LIMIT:gen.buffer.clear()
        return messages
    try:
        # The display must not reset the outer TTY when it dies.
        tty.setraw(0,when=termios.TCSANOW)
        cur=spawn_display(0,False,first)
        if getattr(cur,'error',False):plain()
        while True:
            now=time.monotonic()
            cur.writer.advance(now)
            if native_code is None:
                pid,status=os.waitpid(native,os.WNOHANG)
                if pid:
                    code=os.waitstatus_to_exitcode(status);native_code=code if code>=0 else 128-code
                    deadline=time.monotonic()+2
                    # A display swap does not outlive the native: the old display handles the exit.
                    abort(failed=False)
                    if cur.status_w is not None:
                        try:os.write(cur.status_w,(json.dumps({'exitCode':native_code})+'\n').encode())
                        except OSError:pass
                    close(cur.status_w);cur.status_w=None
            if state is not None and now>=state['deadline']:abort()
            for gen in list(retiring):
                if gen.host.poll() is not None:
                    retiring.remove(gen)
                    if gen.fd is not None:
                        try:os.close(gen.fd)
                        except OSError:pass
                        gen.fd=None
                elif gen.term_deadline is not None and now>=gen.term_deadline and not gen.term_sent:
                    terminate_display(gen);gen.term_deadline=None
            host_done=cur.host is None or cur.host.poll() is not None
            if host_done and native_code is None:
                if state is not None and state['phase']=='switching' and cur is state['new'] and state['old'].host.poll() is None:
                    abort();host_done=cur.host.poll() is not None
                else:
                    abort(failed=False)
                if host_done:plain()
            if native_code is not None:
                if host_done and (not relay or master_eof) and not outgoing:return native_code
                if time.monotonic()>=deadline:
                    terminate_display(cur)
                    if host_done and not outgoing:return native_code
            if relay:
                interest(0,selectors.EVENT_READ if not input_eof and cur.writer.ref is None and len(incoming)<LIMIT else 0)
                interest(1,selectors.EVENT_WRITE if outgoing else 0)
            interest(master,(selectors.EVENT_READ if relay and not master_eof and len(outgoing)<LIMIT else 0)
                     |(selectors.EVENT_WRITE if incoming else 0))
            writer=cur.writer;control=writer.channel
            interest(control.fileno(),(selectors.EVENT_READ|(selectors.EVENT_WRITE if writer.responses else 0))
                     if writer.connected else 0)
            timeout=max(0,deadline-time.monotonic()) if deadline is not None and not cur.term_sent else None
            for due in ([writer.deadline] if writer.deadline is not None else [])+([state['deadline']] if state is not None else [])\
                    +[gen.term_deadline for gen in retiring if gen.term_deadline is not None]:
                due=max(0,due-time.monotonic());timeout=due if timeout is None else min(timeout,due)
            if retiring:timeout=.05 if timeout is None else min(timeout,.05)
            # Process a queued native selection/submission cancellation before
            # writing an Enter that became writable in the same observation.
            for key,events in sorted(selector.select(timeout),key=lambda row:row[0].fd!=control.fileno()):
                fd=key.fd
                if fd==control.fileno():
                    try:
                        if events&selectors.EVENT_READ:writer.receive(time.monotonic())
                        if events&selectors.EVENT_WRITE:writer.send()
                    except (OSError,ValueError,KeyError,TypeError):
                        try:control.shutdown(socket.SHUT_RDWR)
                        except OSError:pass
                        writer.disconnected()
                elif fd==wake_r:
                    try:os.read(fd,4096)
                    except BlockingIOError:pass
                    if relay:
                        size=os.get_terminal_size(1);resize(master,max(1,size.lines),max(1,size.columns))
                elif fd in [gen.ready_r for gen in (cur,pending) if gen is not None and gen.ready_r is not None]:
                    gen=cur if fd==cur.ready_r else pending
                    try:chunk=os.read(fd,32)
                    except BlockingIOError:continue
                    on_ready(gen,chunk)
                elif any(fd==gen.succ.fileno() for gen in (cur,pending,state and state['old']) if gen is not None):
                    gen=next(gen for gen in (cur,pending,state and state['old']) if gen is not None and fd==gen.succ.fileno())
                    for message in drain(gen):on_message(gen,message)
                elif fd==0:
                    try:data=os.read(0,LIMIT-len(incoming))
                    except BlockingIOError:continue
                    if data:incoming.extend(data)
                    else:input_eof=True
                elif fd==1:
                    try:written=os.write(1,outgoing)
                    except BlockingIOError:continue
                    del outgoing[:written]
                elif fd==master:
                    if events&selectors.EVENT_READ:
                        try:data=os.read(master,LIMIT-len(outgoing))
                        except BlockingIOError:continue
                        except OSError as exc:
                            if exc.errno!=errno.EIO:raise
                            data=b''
                        if data:outgoing.extend(data)
                        else:master_eof=True
                    if events&selectors.EVENT_WRITE and incoming:
                        try:written=os.write(master,incoming)
                        except BlockingIOError:continue
                        del incoming[:written]
                        writer.written(written)
    finally:
        abort(failed=False) if state is not None else None
        if native_code is not None:
            terminate_display(cur)
        for gen in [*retiring]:terminate_display(gen)
        for gen in [cur,*retiring]:
            if gen is not None and gen.host is not None and (native_code is not None or gen is not cur):
                try:gen.host.wait(timeout=2)
                except subprocess.TimeoutExpired:pass  # Never escalate to SIGKILL.
        selector.close()
        for gen in [cur,pending,*retiring]:
            if gen is None:continue
            for channel in (gen.writer.channel,gen.succ):
                try:channel.close()
                except OSError:pass
            if gen.fd is not None:
                try:os.close(gen.fd)
                except OSError:pass
        os.close(held_fd)
        for fd in (*owned,master):
            try:os.close(fd)
            except OSError:pass
        for sig,handler in previous.items():signal.signal(sig,handler)
        termios.tcsetattr(0,termios.TCSANOW,saved)
        for fd,mode in blocking.items():os.set_blocking(fd,mode)
        try:os.write(1,RESET)
        except OSError:pass
