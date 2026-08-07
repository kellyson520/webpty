"""Unit tests for src/auth.py — token extraction, IP normalization, gating."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from auth import (  # noqa: E402
    authorize_peer, extract_token, is_localhost, normalize_peer_ip, token_matches,
)


class ExtractTokenTest(unittest.TestCase):
    def test_bearer_header(self):
        self.assertEqual(extract_token({"Authorization": "Bearer abc123"}, "/"), "abc123")

    def test_lowercase_bearer(self):
        self.assertEqual(extract_token({"authorization": "bearer xyz"}, "/"), "xyz")

    def test_header_extra_spaces(self):
        self.assertEqual(extract_token({"Authorization": "Bearer   spaced"}, "/"), "spaced")

    def test_query_param(self):
        self.assertEqual(extract_token({}, "/api/config?token=q1"), "q1")

    def test_empty_query_token(self):
        self.assertEqual(extract_token({}, "/api/config?token="), "")

    def test_cookie(self):
        self.assertEqual(extract_token({"cookie": "webpty_token=c2; other=1"}, "/"), "c2")

    def test_malformed_cookie_encoding(self):
        self.assertEqual(extract_token({"cookie": "webpty_token=%zz"}, "/"), "")

    def test_no_token(self):
        self.assertEqual(extract_token({}, "/"), "")


class TokenMatchesTest(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(token_matches("sekret", {"Authorization": "Bearer sekret"}, "/"))

    def test_mismatch(self):
        self.assertFalse(token_matches("sekret", {"Authorization": "Bearer nope"}, "/"))

    def test_no_configured_token(self):
        self.assertFalse(token_matches("", {"Authorization": "Bearer sekret"}, "/"))


class IpTest(unittest.TestCase):
    def test_ipv4_mapped(self):
        self.assertEqual(normalize_peer_ip("::ffff:127.0.0.1"), "127.0.0.1")

    def test_plain_ipv4(self):
        self.assertEqual(normalize_peer_ip("100.64.1.2"), "100.64.1.2")

    def test_null_empty(self):
        self.assertEqual(normalize_peer_ip(None), "")
        self.assertEqual(normalize_peer_ip(""), "")

    def test_is_localhost(self):
        self.assertTrue(is_localhost("127.0.0.1"))
        self.assertTrue(is_localhost("::1"))
        self.assertTrue(is_localhost("::ffff:127.0.0.1"))
        self.assertTrue(is_localhost("127.0.0.2"))

    def test_not_localhost(self):
        self.assertFalse(is_localhost("100.64.1.2"))
        self.assertFalse(is_localhost("192.168.1.5"))


class AuthorizePeerTest(unittest.IsolatedAsyncioTestCase):
    async def test_localhost_always_allowed(self):
        r = await authorize_peer("127.0.0.1", {}, "/", [], "")
        self.assertTrue(r["ok"])
        self.assertEqual(r["reason"], "localhost")

    async def test_token_accept(self):
        r = await authorize_peer("100.64.1.2", {"Authorization": "Bearer sekret"}, "/", [], "sekret")
        self.assertTrue(r["ok"])
        self.assertEqual(r["reason"], "token")

    async def test_token_reject_wrong(self):
        r = await authorize_peer("100.64.1.2", {"Authorization": "Bearer wrong"}, "/", [], "sekret")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "bad-token")

    async def test_token_reject_missing(self):
        r = await authorize_peer("100.64.1.2", {}, "/", [], "sekret")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "bad-token")

    async def test_gate_disabled(self):
        r = await authorize_peer("100.64.1.2", {}, "/", [], "")
        self.assertTrue(r["ok"])
        self.assertEqual(r["reason"], "gate-disabled")

    async def test_allowed_logins_without_tailscale(self):
        r = await authorize_peer("100.64.1.2", {}, "/", ["you@example.com"], "")
        self.assertFalse(r["ok"])
        self.assertIn(r["reason"], ("not-a-tailnet-peer", "login-not-allowed"))

    async def test_token_beats_tailscale(self):
        r = await authorize_peer("100.64.1.2", {"Authorization": "Bearer sekret"}, "/",
                                 ["someone@example.com"], "sekret")
        self.assertTrue(r["ok"])
        self.assertEqual(r["reason"], "token")


if __name__ == "__main__":
    unittest.main()
