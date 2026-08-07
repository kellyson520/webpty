import asyncio, unittest, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from ws import Outbox

class FakeWS:
    def __init__(self): self.frames = []
    def _send_frame(self, opcode, payload): self.frames.append((opcode, payload))

class OutboxTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_and_drain(self):
        ws = FakeWS()
        ob = Outbox(ws, maxlen=1024)
        ob.start()
        ob.send(b"hello", binary=True)
        await asyncio.sleep(0.05)
        ob.stop()
        self.assertTrue(any(p == b"hello" for _, p in ws.frames))

    async def test_drop_oldest_on_overflow(self):
        ws = FakeWS()
        ob = Outbox(ws, maxlen=3)
        ob.start()
        for i in range(10):
            ob.send(b"x" * 100, binary=True)
        await asyncio.sleep(0.05)
        ob.stop()
        self.assertGreaterEqual(ob.dropped, 5)

    async def test_dropped_count_tracks(self):
        ws = FakeWS()
        ob = Outbox(ws, maxlen=2)
        ob.start()
        ob.send(b"a", binary=True)
        ob.send(b"b", binary=True)
        ob.send(b"c", binary=True)
        await asyncio.sleep(0.05)
        ob.stop()
        self.assertGreaterEqual(ob.dropped, 1)
