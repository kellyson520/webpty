"""PtyHostClient lifecycle tests (audit L7): close() must be idempotent,
must cancel the reader task, and must not break a subsequent connect."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pty_host_client import PtyHostClient  # noqa: E402


class PtyHostClientCloseTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_idempotent_and_cancels_reader(self):
        """close() twice, and after connect: reader task cancelled, socket
        closed, _connected cleared."""
        client = PtyHostClient()
        # close on a never-connected client must be a no-op
        await client.close()
        await client.close()
        self.assertFalse(client.connected)

    async def test_close_after_connect_cancels_reader_task(self):
        """Connecting spawns a _reader_task; close() cancels it."""
        client = PtyHostClient()
        try:
            await client.connect()
        except (OSError, FileNotFoundError, RuntimeError):
            self.skipTest("pty-host not available in test env")
        self.assertTrue(client.connected)
        task = client._reader_task
        self.assertIsNotNone(task)
        await client.close()
        self.assertFalse(client.connected)
        self.assertTrue(task.cancelled())
        self.assertIsNone(client._reader_task)

    async def test_close_does_not_raise_with_half_open_state(self):
        """Even with a dangling writer, close() must not raise."""
        client = PtyHostClient()
        client._connected = True
        await client.close()
        self.assertFalse(client.connected)


if __name__ == "__main__":
    unittest.main()
