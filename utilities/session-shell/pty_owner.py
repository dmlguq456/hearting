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
from input_owner import InputOwner

RELAY_RESET=b'\x1b[0m\x1b[?25h\x1b[2J\x1b[H'
RESET=b'\x1b[0m\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?25h\x1b[?1049l'
LIMIT=65536


class BeforeStartError(OSError):
    """No native process exists; the caller may still execute the original CLI."""


def birth(pid):
    if type(pid) is not int or pid<=1:raise ValueError('unsafe child pid')
    with open(f'/proc/{pid}/stat') as handle:
        return handle.read().rsplit(') ',1)[1].split()[19]


def resize(master,rows,cols):
    try:fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',rows,cols,0,0))
    except OSError:pass  # A display-size failure does not close the I/O channel.


def run(harness,original,wrapped,node,host_path,environment,directory):
    """The display borrows the master; this owner relays only after it exits."""
    pipes=[]
    control=display_control=None;held_fd=None
    try:
        saved=termios.tcgetattr(0);blocking={fd:os.get_blocking(fd) for fd in (0,1)}
        size=os.get_terminal_size(1)
        for _ in range(4):pipes.extend(os.pipe())
        control,display_control=socket.socketpair()
        held_fd=os.memfd_create('hearting-held-input',os.MFD_CLOEXEC)
        native,master=pty.fork()
    except OSError as exc:
        for fd in pipes:os.close(fd)
        for channel in (control,display_control):
            if channel is not None:channel.close()
        if held_fd is not None:os.close(held_fd)
        raise BeforeStartError(str(exc)) from exc
    go_r,go_w,ready_r,ready_w,status_r,status_w,wake_r,wake_w=pipes
    owned=set(pipes)
    if native==0:
        control.close();display_control.close();os.close(held_fd)
        for fd in pipes:
            if fd!=go_r:os.close(fd)
        try:
            mode=os.read(go_r,1);os.close(go_r)
            if not mode:os._exit(125)
            for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP,signal.SIGPIPE):signal.signal(sig,signal.SIG_DFL)
            env=dict(os.environ,PWD=os.getcwd())
            if mode==b'S':
                env.update(HEARTING_SHELL_SOCKET=environment['HEARTING_SHELL_SOCKET'],
                           HEARTING_SHELL_TOKEN=environment['HEARTING_SHELL_TOKEN'])
            command=wrapped if mode==b'S' else original
            os.execvpe(command[0],command,env)
        except BaseException as exc:
            os.write(2,(str(exc)+'\r\n').encode(errors='replace'));os._exit(127)
    os.close(go_r);owned.remove(go_r)
    resize(master,max(1,size.lines-1),size.columns)
    owner_start=birth(os.getpid())
    env=dict(environment,HEARTING_SHELL_START_FD=str(ready_w),HEARTING_SHELL_PTY_FD=str(master),
             HEARTING_SHELL_NATIVE_PID=str(native),HEARTING_SHELL_PTY_EXIT_FD=str(status_r),
             HEARTING_SHELL_INPUT_FD=str(display_control.fileno()),HEARTING_SHELL_HELD_FD=str(held_fd),
             HEARTING_SHELL_BROKER_PID=str(os.getpid()),HEARTING_SHELL_BROKER_START=owner_start)
    previous={}
    os.set_blocking(wake_r,False);os.set_blocking(wake_w,False);os.set_blocking(ready_r,False)
    def wake(_signal,_frame):
        try:os.write(wake_w,b'1')
        except OSError:pass
    for sig in (signal.SIGCHLD,signal.SIGWINCH,signal.SIGINT):
        previous[sig]=signal.signal(sig,signal.SIG_IGN if sig==signal.SIGINT else wake)
    host=None;host_fd=None;native_code=None;deadline=None;started=False;relay=False;ready=b''
    incoming=bytearray();outgoing=bytearray();input_eof=False;master_eof=False;term_sent=False
    writer=InputOwner(control,held_fd,incoming)
    selector=selectors.DefaultSelector();selector.register(wake_r,selectors.EVENT_READ)
    selector.register(ready_r,selectors.EVENT_READ)
    def close(fd):
        try:selector.unregister(fd)
        except (KeyError,ValueError):pass
        if fd in owned:
            owned.remove(fd)
            try:os.close(fd)
            except OSError:pass
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
    def terminate_display():
        nonlocal term_sent
        if host is not None and host.poll() is None and not term_sent:
            if host.pid<=1:raise ValueError('unsafe display pid')
            try:
                if birth(host.pid)!=host_start:return
                signal.pidfd_send_signal(host_fd,signal.SIGTERM);term_sent=True
            except (FileNotFoundError,ProcessLookupError):pass
    try:
        # The display must not reset the outer TTY when it dies.
        tty.setraw(0,when=termios.TCSANOW)
        try:
            os.set_blocking(master,False)
            host=subprocess.Popen([node,str(host_path),harness,*wrapped],env=env,
                                  pass_fds=(master,ready_w,status_r,display_control.fileno(),held_fd))
            host_start=birth(host.pid);host_fd=os.pidfd_open(host.pid)
        except OSError:plain()
        finally:close(ready_w);close(status_r);display_control.close()
        while True:
            writer.advance(time.monotonic())
            if native_code is None:
                pid,status=os.waitpid(native,os.WNOHANG)
                if pid:
                    code=os.waitstatus_to_exitcode(status);native_code=code if code>=0 else 128-code
                    deadline=time.monotonic()+2
                    try:os.write(status_w,(json.dumps({'exitCode':native_code})+'\n').encode())
                    except BrokenPipeError:pass
                    close(status_w)
            host_done=host is None or host.poll() is not None
            if host_done and native_code is None:plain()
            if native_code is not None:
                if host_done and (not relay or master_eof) and not outgoing:return native_code
                if time.monotonic()>=deadline:
                    terminate_display()
                    if host_done and not outgoing:return native_code
            if relay:
                interest(0,selectors.EVENT_READ if not input_eof and writer.ref is None and len(incoming)<LIMIT else 0)
                interest(1,selectors.EVENT_WRITE if outgoing else 0)
            interest(master,(selectors.EVENT_READ if relay and not master_eof and len(outgoing)<LIMIT else 0)
                     |(selectors.EVENT_WRITE if incoming else 0))
            interest(control.fileno(),(selectors.EVENT_READ|(selectors.EVENT_WRITE if writer.responses else 0))
                     if writer.connected else 0)
            timeout=max(0,deadline-time.monotonic()) if deadline is not None and not term_sent else None
            if writer.deadline is not None:
                due=max(0,writer.deadline-time.monotonic());timeout=due if timeout is None else min(timeout,due)
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
                elif fd==ready_r:
                    chunk=os.read(fd,32)
                    if chunk:ready+=chunk
                    if b'\n' in ready or not chunk:
                        close(ready_r)
                        if ready.startswith(b'STARTED'):begin(b'S')
                        else:plain()
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
        if native_code is not None:
            terminate_display()
            if host is not None:
                try:host.wait(timeout=2)
                except subprocess.TimeoutExpired:pass  # Never escalate to SIGKILL.
        selector.close()
        control.close();display_control.close();os.close(held_fd)
        for fd in (*owned,master):
            try:os.close(fd)
            except OSError:pass
        if host_fd is not None:os.close(host_fd)
        for sig,handler in previous.items():signal.signal(sig,handler)
        termios.tcsetattr(0,termios.TCSANOW,saved)
        for fd,mode in blocking.items():os.set_blocking(fd,mode)
        try:os.write(1,RESET)
        except OSError:pass
