// Unit tests for src/auth.js — token extraction, IP normalization, gating.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  extractToken, tokenMatches, normalizePeerIp, isLocalhost, authorizePeer
} from '../src/auth.js';

function req({ headers = {}, url = '/api/config', ip = '100.64.1.2' } = {}) {
  return { headers, url, socket: { remoteAddress: ip } };
}

test('extractToken: Bearer header', () => {
  assert.equal(extractToken(req({ headers: { authorization: 'Bearer abc123' } })), 'abc123');
});

test('extractToken: lowercase bearer header', () => {
  assert.equal(extractToken(req({ headers: { authorization: 'bearer xyz' } })), 'xyz');
});

test('extractToken: header with extra spaces', () => {
  assert.equal(extractToken(req({ headers: { authorization: 'Bearer   spaced' } })), 'spaced');
});

test('extractToken: query param', () => {
  assert.equal(extractToken(req({ url: '/api/config?token=q1' })), 'q1');
});

test('extractToken: empty query token is empty string', () => {
  assert.equal(extractToken(req({ url: '/api/config?token=' })), '');
});

test('extractToken: cookie', () => {
  assert.equal(extractToken(req({ headers: { cookie: 'webpty_token=c2; other=1' } })), 'c2');
});

test('extractToken: malformed percent-encoding in cookie is ignored', () => {
  assert.equal(extractToken(req({ headers: { cookie: 'webpty_token=%zz' } })), '');
});

test('extractToken: no token anywhere', () => {
  assert.equal(extractToken(req()), '');
});

test('tokenMatches: exact match', () => {
  const r = req({ headers: { authorization: 'Bearer sekret' } });
  assert.ok(tokenMatches('sekret', r));
});

test('tokenMatches: mismatch', () => {
  const r = req({ headers: { authorization: 'Bearer nope' } });
  assert.ok(!tokenMatches('sekret', r));
});

test('tokenMatches: no configured token means never match', () => {
  const r = req({ headers: { authorization: 'Bearer sekret' } });
  assert.ok(!tokenMatches('', r));
});

test('normalizePeerIp: IPv4-mapped IPv6 becomes IPv4', () => {
  assert.equal(normalizePeerIp('::ffff:127.0.0.1'), '127.0.0.1');
});

test('normalizePeerIp: plain IPv4 unchanged', () => {
  assert.equal(normalizePeerIp('100.64.1.2'), '100.64.1.2');
});

test('normalizePeerIp: null/empty', () => {
  assert.equal(normalizePeerIp(null), '');
  assert.equal(normalizePeerIp(''), '');
});

test('isLocalhost: loopback addresses', () => {
  assert.ok(isLocalhost('127.0.0.1'));
  assert.ok(isLocalhost('::1'));
  assert.ok(isLocalhost('::ffff:127.0.0.1'));
  assert.ok(isLocalhost('127.0.0.2'));
});

test('isLocalhost: non-loopback', () => {
  assert.ok(!isLocalhost('100.64.1.2'));
  assert.ok(!isLocalhost('192.168.1.5'));
});

test('authorizePeer: localhost always allowed', async () => {
  const r = req({ ip: '127.0.0.1' });
  const a = await authorizePeer(r, [], '');
  assert.deepEqual(a, { ok: true, reason: 'localhost', peer: { ip: '127.0.0.1', login: 'localhost' } });
});

test('authorizePeer: token gate accepts correct token', async () => {
  const r = req({ headers: { authorization: 'Bearer sekret' } });
  const a = await authorizePeer(r, [], 'sekret');
  assert.equal(a.ok, true);
  assert.equal(a.reason, 'token');
});

test('authorizePeer: token gate rejects wrong token', async () => {
  const r = req({ headers: { authorization: 'Bearer wrong' } });
  const a = await authorizePeer(r, [], 'sekret');
  assert.equal(a.ok, false);
  assert.equal(a.reason, 'bad-token');
});

test('authorizePeer: token gate rejects missing token', async () => {
  const a = await authorizePeer(req(), [], 'sekret');
  assert.equal(a.ok, false);
  assert.equal(a.reason, 'bad-token');
});

test('authorizePeer: no token + no allowedLogins = gate disabled (allow)', async () => {
  const a = await authorizePeer(req(), [], '');
  assert.equal(a.ok, true);
  assert.equal(a.reason, 'gate-disabled');
});

test('authorizePeer: allowedLogins without tailscale binary = deny', async () => {
  // tailscale almost certainly isn't installed in the test env; the peer IP is
  // a CGNAT address so whois returns nothing → deny.
  const a = await authorizePeer(req(), ['you@example.com'], '');
  assert.equal(a.ok, false);
  assert.ok(['not-a-tailnet-peer', 'login-not-allowed'].includes(a.reason));
});

test('authorizePeer: token beats tailscale gate when both present', async () => {
  const r = req({ headers: { authorization: 'Bearer sekret' } });
  const a = await authorizePeer(r, ['someone@example.com'], 'sekret');
  assert.equal(a.ok, true);
  assert.equal(a.reason, 'token');
});
