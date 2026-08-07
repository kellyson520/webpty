// Identity gate for webpty.
//
// 1. If `config.authToken` is set, every request that is not localhost must
//    present it (Authorization: Bearer <token>, ?token=<token>, or the
//    `webpty_token` cookie). This is the lightweight self-hosted gate — no
//    Tailscale required.
// 2. Otherwise we fall back to the Tailscale-identity gate: shell out to
//    `tailscale whois --json` to map a peer IP back to its tailnet login;
//    requests are allowed when (a) the peer is localhost, or (b) the login is
//    in `config.allowedLogins`. With an empty `allowedLogins` the gate is
//    disabled (legacy behavior) — a startup warning nudges the operator.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import crypto from 'node:crypto';

const execFileP = promisify(execFile);
const CACHE_TTL_MS = 60_000;
const NEG_CACHE_TTL_MS = 10_000; // shorter for "not a tailnet peer" / errors
const whoisCache = new Map(); // ip -> { value, expiresAt }

let tailscaleMissingLogged = false;

// Timing-safe token compare.
function safeEqual(a, b) {
  const ba = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  if (ba.length !== bb.length) return false;
  return crypto.timingSafeEqual(ba, bb);
}

export function extractToken(req) {
  const header = req.headers?.authorization;
  if (typeof header === 'string' && /^Bearer\s+/i.test(header)) return header.slice(7).trim();
  const q = new URL(req.url, 'http://x').searchParams.get('token');
  if (q) return q;
  const cookie = req.headers?.cookie;
  if (typeof cookie === 'string') {
    const m = /(?:^|;)\s*webpty_token=([^;]+)/i.exec(cookie);
    if (m) {
      try {
        return decodeURIComponent(m[1]);
      } catch {
        // Malformed percent-encoding in the cookie — treat as no token
        // (a clean 403 beats a 500 from URIError).
      }
    }
  }
  return '';
}

export function tokenMatches(token, req) {
  if (!token) return false;
  return safeEqual(token, extractToken(req));
}

export function normalizePeerIp(addr) {
  if (!addr) return '';
  // IPv4-mapped IPv6 (`::ffff:127.0.0.1`) → plain v4
  const m = /^::ffff:(\d+\.\d+\.\d+\.\d+)$/i.exec(addr);
  return m ? m[1] : addr;
}

export function isLocalhost(ip) {
  const n = normalizePeerIp(ip);
  return n === '127.0.0.1' || n === '::1' || n.startsWith('127.');
}

export async function tailscaleWhoIs(ip) {
  const cached = whoisCache.get(ip);
  if (cached && cached.expiresAt > Date.now()) return cached.value;
  let value = null;
  try {
    const { stdout } = await execFileP('tailscale', ['whois', '--json', ip], { timeout: 2000 });
    const info = JSON.parse(stdout);
    const loginName = info?.UserProfile?.LoginName || null;
    const displayName = info?.UserProfile?.DisplayName || null;
    const nodeName = info?.Node?.Name?.replace(/\.$/, '') || null;
    if (loginName) value = { loginName, displayName, nodeName };
  } catch (err) {
    if (err.code === 'ENOENT' && !tailscaleMissingLogged) {
      tailscaleMissingLogged = true;
      console.error('[webpty] `tailscale` CLI not found on PATH — identity-based auth disabled');
    }
  }
  whoisCache.set(ip, { value, expiresAt: Date.now() + (value ? CACHE_TTL_MS : NEG_CACHE_TTL_MS) });
  return value;
}

// Returns { ok, reason, peer } where peer is { ip, login, displayName, nodeName }.
// `ok: false` is a deliberate deny (identity/token didn't match); the caller
// should respond 403.
export async function authorizePeer(req, allowedLogins, authToken = '') {
  const ip = normalizePeerIp(req.socket?.remoteAddress);
  if (isLocalhost(ip)) {
    return { ok: true, reason: 'localhost', peer: { ip, login: 'localhost' } };
  }
  // Token gate (when configured) takes precedence over Tailscale identity.
  if (authToken) {
    if (tokenMatches(authToken, req)) {
      return { ok: true, reason: 'token', peer: { ip } };
    }
    return { ok: false, reason: 'bad-token', peer: { ip } };
  }
  if (!Array.isArray(allowedLogins) || allowedLogins.length === 0) {
    // Gate disabled — let everything through (logged once at boot).
    return { ok: true, reason: 'gate-disabled', peer: { ip } };
  }
  const info = await tailscaleWhoIs(ip);
  if (!info) {
    return { ok: false, reason: 'not-a-tailnet-peer', peer: { ip } };
  }
  const allowed = allowedLogins.map((s) => String(s).toLowerCase());
  if (!allowed.includes(info.loginName.toLowerCase())) {
    return { ok: false, reason: 'login-not-allowed', peer: { ip, login: info.loginName, displayName: info.displayName, nodeName: info.nodeName } };
  }
  return { ok: true, reason: 'allowed', peer: { ip, login: info.loginName, displayName: info.displayName, nodeName: info.nodeName } };
}
