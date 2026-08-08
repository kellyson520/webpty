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




class FrameParseTest(unittest.IsolatedAsyncioTestCase):
    """WS 帧解析：分片到达不误断连（审计高危项：远程网络断连根因）。"""

    def _ws(self, buf: bytes):
        import asyncio as _a
        from ws import WebSocket
        ws = WebSocket(_a.StreamReader(), None)
        ws._recv_buf = bytearray(buf)
        return ws

    async def test_incomplete_payload_returns_sentinel(self):
        from ws import _INCOMPLETE
        ws = self._ws(b"\x81\xfe\x10\x00" + b"ab")  # 16B payload 只给 2B
        self.assertIs(ws._parse_frame(), _INCOMPLETE)

    async def test_incomplete_extended_length_returns_sentinel(self):
        from ws import _INCOMPLETE
        ws = self._ws(b"\x81\x7f")  # 64 位长度头只给 2B
        self.assertIs(ws._parse_frame(), _INCOMPLETE)

    async def test_incomplete_mask_key_returns_sentinel(self):
        from ws import _INCOMPLETE
        ws = self._ws(b"\x81\x80\x11\x22")  # masked，mask key 只给 2B
        self.assertIs(ws._parse_frame(), _INCOMPLETE)

    async def test_recv_handles_split_payload(self):
        # TCP 分片：先到帧头+mask，payload 后到 → recv 不关闭连接，返回完整帧
        import asyncio as _a
        from ws import WebSocket
        reader = _a.StreamReader()
        reader.feed_data(b"\x81\x85\x11\x22\x33\x44")  # 头+mask，len=5 但无 payload
        ws = WebSocket(reader, None)
        reader.feed_data(bytes(b ^ [0x11, 0x22, 0x33, 0x44][i % 4] for i, b in enumerate(b"hello")))
        frame = await ws.recv(timeout_s=1)
        self.assertIsNotNone(frame)
        op, payload = frame
        self.assertEqual(op, 0x1)
        self.assertEqual(payload, b"hello")

    async def test_pings_then_text_no_recursion(self):
        import asyncio as _a
        from ws import WebSocket
        reader = _a.StreamReader()
        reader.feed_data(b"\x89\x00\x89\x00\x81\x01x")  # ping+ping+text
        ws = WebSocket(reader, None)
        frame = await ws.recv(timeout_s=1)
        self.assertIsNotNone(frame)
        self.assertEqual(frame[0], 0x1)
        self.assertEqual(frame[1], b"x")


class OutboxResyncTest(unittest.IsolatedAsyncioTestCase):
    """丢帧时触发 on_resync（TUI 增量状态重建）。"""

    async def test_drop_triggers_resync_callback(self):
        import asyncio as _a
        from ws import Outbox
        ws = FakeWS()
        resynced = []
        ob = Outbox(ws, maxlen=4, on_resync=lambda: resynced.append(True))
        ob.start()
        # 模拟慢消费者：drain 被阻塞，队列堆满触发丢帧
        ob.ws.drain = lambda: asyncio.sleep(0.05)
        for i in range(8):
            ob.send(b"x" * 100, binary=True)
        # 等队列排空（drain 循环在 empty 时检查 resync）
        for _ in range(100):
            if ob._queue.empty() and resynced:
                break
            await _a.sleep(0.02)
        ob.stop()
        self.assertTrue(ob.dropped > 0, "应有丢帧")
        self.assertTrue(resynced, "丢帧应触发 on_resync")


class FrameLimitTest(unittest.IsolatedAsyncioTestCase):
    """超长帧声明必须被拒绝（内存 DoS 防护）。"""

    async def test_huge_extended_length_rejected(self):
        from ws import WebSocket, WebSocketError
        import asyncio as _a
        ws = WebSocket(_a.StreamReader(), None)
        # 64-bit 长度头声明 2^40（远超 16MB 上限）
        ws._recv_buf = bytearray(b"\x81\x7f" + (2 ** 40).to_bytes(8, "big"))
        with self.assertRaises(WebSocketError):
            ws._parse_frame()
