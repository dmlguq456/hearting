"""Own only an admitted paste/Enter/held-key transaction, outside the display."""
import json
import os

HELD_LIMIT = 131072  # A paused 64KiB read plus at most one read already in flight.
COMMAND_LIMIT = 131072
ENTER_DELAY = .050


class InputOwner:
    def __init__(self, channel, held_fd, pending):
        self.channel, self.held_fd, self.pending = channel, held_fd, pending
        channel.setblocking(False)
        self.connected = True
        self.received = bytearray()
        self.responses = bytearray()
        self.ref = None
        self.deadline = None
        self.entered = False
        self.enter_left = None
        self.release_id = None
        self.held = bytearray()
        self.position = 0

    def reply(self, value):
        if self.connected:
            self.responses.extend((json.dumps(value, separators=(',', ':'))+'\n').encode())

    def capture(self):
        # A key can be committed just before its begin command is received.
        # Keep it in shared memory until the accepted paste owns its ordering.
        if self.ref is None and self.connected:
            return
        # pread never changes the display's append position. A killed display
        # leaves these anonymous shared-memory bytes available to this owner.
        size = os.fstat(self.held_fd).st_size
        while self.position < size:
            data = os.pread(self.held_fd, min(HELD_LIMIT, size-self.position), self.position)
            if not data:
                break
            self.position += len(data)
            (self.held if self.ref is not None and not self.entered else self.pending).extend(data)

    def disconnected(self):
        self.connected = False
        self.responses.clear()
        self.capture()

    def receive(self, now):
        try:
            data = self.channel.recv(65536)
        except BlockingIOError:
            return
        if not data:
            self.disconnected()
            return
        self.received.extend(data)
        if len(self.received) > COMMAND_LIMIT:
            self.channel.shutdown(2)
            self.disconnected()
            return
        while b'\n' in self.received:
            raw, _, rest = self.received.partition(b'\n')
            self.received[:] = rest
            request = json.loads(raw)
            op = request['op']
            if op == 'begin':
                if self.ref is not None:
                    raise ValueError('input transaction already active')
                text = request['text'].encode()
                if len(text) > 32768:
                    raise ValueError('input transaction too large')
                self.ref = request['ref']
                self.entered = False
                self.deadline = now + ENTER_DELAY
                self.pending.extend(b'\x1b[200~'+text+b'\x1b[201~')
                self.reply({'id': request['id'], 'ok': True})
            elif op == 'release':
                if request['ref'] != self.ref:
                    raise ValueError('input transaction changed')
                self.capture()
                if request.get('cancel'):
                    # A native submission/selection callback keeps the former
                    # cancellation boundary, including an Enter not yet written.
                    if self.enter_left is not None:
                        del self.pending[self.enter_left-1]
                    self.enter_left = None
                    self.entered = True
                    self.deadline = None
                    self.pending.extend(self.held)
                    self.held.clear()
                self.release_id = request['id']
            elif op != 'held':
                raise ValueError('unknown input operation')
            self.capture()

    def send(self):
        if self.responses:
            try:
                count = self.channel.send(self.responses)
                del self.responses[:count]
            except BlockingIOError:
                pass
            except BrokenPipeError:
                self.disconnected()

    def advance(self, now):
        self.capture()
        if self.ref is not None and self.deadline is not None and now >= self.deadline:
            self.deadline = None
            self.entered = True
            self.pending.extend(b'\r')
            self.enter_left = len(self.pending)
            self.pending.extend(self.held)
            self.held.clear()
        self.settle()

    def written(self, count):
        if self.enter_left is not None:
            self.enter_left -= count
            if self.enter_left <= 0:
                self.enter_left = None
                self.reply({'event': 'entered', 'ref': self.ref})
        self.settle()

    def settle(self):
        if (self.ref is not None and self.entered and self.enter_left is None
                and not self.pending and (self.release_id is not None or not self.connected)):
            reply = self.release_id
            # The display pauses stdin before release. Reset only after every
            # journaled byte is written, before acknowledging the direct path.
            os.ftruncate(self.held_fd, 0)
            os.lseek(self.held_fd, 0, os.SEEK_SET)
            self.position = 0
            self.ref = self.deadline = self.release_id = None
            if reply is not None:
                self.reply({'id': reply, 'ok': True})
