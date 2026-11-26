"""Transaction ownership survives channel death and partial native writes."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import os
import socket
import sys
import unittest

sys.path.insert(0,str(Path(__file__).with_name('session-shell')))
from input_owner import InputOwner


class InputOwnerTest(unittest.TestCase):
    def setUp(self):
        self.client,self.channel=socket.socketpair()
        self.addCleanup(self.client.close);self.addCleanup(self.channel.close)
        self.fd=os.memfd_create('owned-input-test',os.MFD_CLOEXEC)
        self.addCleanup(os.close,self.fd)
        self.pending=bytearray();self.output=bytearray()
        self.writer=InputOwner(self.channel,self.fd,self.pending)

    def request(self,op,**fields):
        self.client.sendall((json.dumps({'id':1,'op':op,**fields})+'\n').encode())
        self.writer.receive(0)

    def drain(self,count=1):
        while self.pending:
            part=self.pending[:count];self.output.extend(part);del self.pending[:len(part)]
            self.writer.written(len(part))

    def test_host_death_before_notification_keeps_enter_and_shared_human_bytes_once(self):
        self.request('begin',ref='owned-peer',text='peer body')
        # Some paste bytes reached the native terminal before the display died.
        part=self.pending[:3];self.output.extend(part);del self.pending[:3];self.writer.written(3)
        held='human 한글🙂'.encode();os.write(self.fd,held)
        # No notification was sent: the owner still owns the committed bytes.
        self.client.close();self.writer.receive(.01)
        self.writer.advance(.049)
        self.assertNotIn(b'\r',self.pending)
        self.writer.advance(.050);self.drain(2)
        self.assertEqual(self.output,b'\x1b[200~peer body\x1b[201~\r'+held)
        self.writer.advance(1);self.drain()
        self.assertEqual(self.output.count(held),1)
        self.assertIsNone(self.writer.ref)
        self.assertEqual(os.fstat(self.fd).st_size,0)

    def test_release_waits_for_partial_writes_before_reusing_the_bounded_buffer(self):
        for index in range(3):
            ref=str(index);text='peer '+ref
            self.request('begin',ref=ref,text=text)
            os.write(self.fd,b'held');self.request('held')
            self.writer.advance(.050)
            self.request('release',ref=ref)
            self.assertEqual(os.fstat(self.fd).st_size,4)
            self.assertIsNotNone(self.writer.ref)
            self.drain(3)
            self.assertIsNone(self.writer.ref)
            self.assertEqual(os.fstat(self.fd).st_size,0)
            self.assertEqual(os.lseek(self.fd,0,os.SEEK_CUR),0)
        self.assertEqual(self.output,b''.join(b'\x1b[200~peer '+str(i).encode()+b'\x1b[201~\rheld' for i in range(3)))

    def test_a_human_commit_before_begin_is_observed_stays_behind_the_paste(self):
        os.write(self.fd,b'early human')
        self.writer.advance(0)
        self.assertEqual(self.pending,b'')
        self.request('begin',ref='peer',text='body')
        self.writer.advance(.050)
        self.request('release',ref='peer')
        self.drain()
        self.assertEqual(self.output,b'\x1b[200~body\x1b[201~\rearly human')

    def test_native_callback_cancels_only_an_enter_that_has_not_been_written(self):
        for enter_queued in (False,True):
            with self.subTest(enter_queued=enter_queued):
                self.output.clear()
                self.request('begin',ref='peer',text='body')
                os.write(self.fd,b'human');self.request('held')
                if enter_queued:self.writer.advance(.050)
                self.request('release',ref='peer',cancel=True)
                self.drain()
                self.assertEqual(self.output,b'\x1b[200~body\x1b[201~human')


if __name__=='__main__':unittest.main()
