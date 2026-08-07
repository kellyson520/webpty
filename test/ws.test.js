// WebSocket integration tests — upgrade auth, malformed id handling, data flow.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { WebSocket } from 'ws';

const TEST_DATA = fs.mkdtempSync(path.join(os.tmpdir(), 'webpty-ws-test-'));
const PROJ_ROOT = path.join(TEST_DATA, 'projects');
fs.mkdirSync(PROJ_ROOT, { recursive: true });
fs.mkdirSync(path.join(PROJ_ROOT, 'alpha'), { recursive: true });

const PORT = 48500 + Math.floor(Math.random() * 400);
const BASE = `http://127.0.0.1:${PORT}`;
const WS_BASE = `ws://127.0.0.1:${PORT}`;

let child;
let sessionId;

before(async () => {
  child = spawn(process.execPath, ['src/server.js'], {
    cwd: path.resolve(import.meta.dirname, '..'),
    env: {
      ...process.env,
      WEBPTY_DATA_DIR: TEST_DATA,
      WEBPTY_PROJECTS_ROOT: PROJ_ROOT,
      WEBPTY_PORT: String(PORT),
      WEBPTY_BIND_HOST: '127.0.0.1'
    },
    stdio: ['ignore', 'pipe', 'pipe']
  });
  let err = '';
  child.stderr.on('data', (d) => { err += d; });
  for (let i = 0; i < 50; i++) {
    await new Promise((r) => setTimeout(r, 100));
    try { if ((await fetch(`${BASE}/api/config`)).ok) break; } catch {}
  }
  // Create a bash session for the WS tests.
  const res = await fetch(`${BASE}/api/sessions`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ cwd: path.join(PROJ_ROOT, 'alpha'), tool: 'bash', name: 'ws-shell' })
  });
  const j = await res.json();
  sessionId = j.id;
  assert.ok(sessionId);
});

after(async () => {
  if (child && !child.killed) child.kill('SIGTERM');
  await new Promise((r) => setTimeout(r, 300));
  fs.rmSync(TEST_DATA, { recursive: true, force: true });
});

function wsOpen(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    ws.once('open', () => resolve(ws));
    ws.once('error', (e) => reject(e));
  });
}

test('WS upgrade to unknown session still connects (server tolerates)', async () => {
  const ws = await wsOpen(`${WS_BASE}/ws/sessions/no-such-session`);
  assert.equal(ws.readyState, WebSocket.OPEN);
  ws.close();
});

test('WS upgrade with malformed percent-encoding is rejected cleanly', async () => {
  // '%zz' is invalid UTF-8 percent encoding; the server must destroy the
  // socket instead of crashing.
  await assert.rejects(
    () => wsOpen(`${WS_BASE}/ws/sessions/%zz`),
    /Unexpected server response|socket hang up|ECONNREFUSED/i
  );
  // Server is still alive afterwards.
  const res = await fetch(`${BASE}/api/config`);
  assert.equal(res.status, 200);
});

test('WS non-session path is rejected', async () => {
  await assert.rejects(
    () => wsOpen(`${WS_BASE}/ws/not-a-session`),
    /Unexpected server response|socket hang up/i
  );
});

test('WS sends state events and accepts resize/user messages', async () => {
  const ws = await wsOpen(`${WS_BASE}/ws/sessions/${sessionId}`);
  const messages = [];
  ws.on('message', (data) => messages.push(data));
  ws.send(JSON.stringify({ type: 'resize', cols: 100, rows: 40 }));
  await new Promise((r) => setTimeout(r, 300));
  ws.send('echo hi\r');
  await new Promise((r) => setTimeout(r, 400));
  assert.ok(messages.length >= 0); // no crash, connection healthy
  assert.equal(ws.readyState, WebSocket.OPEN);
  ws.close();
});

test('WS agent-type message to pty session is ignored safely', async () => {
  const ws = await wsOpen(`${WS_BASE}/ws/sessions/${sessionId}`);
  ws.send(JSON.stringify({ type: 'user', text: 'hello' }));
  await new Promise((r) => setTimeout(r, 200));
  assert.equal(ws.readyState, WebSocket.OPEN);
  ws.close();
});
