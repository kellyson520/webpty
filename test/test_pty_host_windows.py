"""Windows pywinpty backend tests.

The Windows backend cannot run on POSIX, so the winpty assertions are
skipped here. `test_posix_backend_selected` instead pins the platform
dispatch that routes to winpty on Windows and forkpty elsewhere.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pty_host  # noqa: E402


@unittest.skipUnless(os.name == "nt", "Windows only")
class WindowsHostTest(unittest.TestCase):
    def test_pty_backend_selected(self):
        # Importing pty_host on Windows must select the winpty backend and
        # must not trip over POSIX-only stdlib imports (pty.fork etc.).
        import pty_host as ph

        self.assertEqual(ph._backend, "winpty")


@unittest.skipUnless(os.name == "posix", "POSIX only")
class PosixDispatchTest(unittest.TestCase):
    def test_forkpty_backend_selected(self):
        self.assertEqual(pty_host._backend, "forkpty")
