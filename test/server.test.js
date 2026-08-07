// Integration tests for src/server.js — HTTP API behavior.
// Spawns the real server on a random port with an isolated data dir,
// then exercises the REST endpoints.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const TEST_DATA = fs.mkdtempSync(path.join(os.tmpdir(), 'webpty-srv-test-'));
const PROJ_ROOT = path.join(TEST_DATA, 'projects');
fs.mkdirSync(PROJ_ROOT, { recursive: true });
fs.mkdirSync(path.join(PROJ_ROOT, 'alpha'), { recursive: true });
fs.mkdirSync(path.join(PROJ_ROOT, 'beta'), { recursive: true });

const PORT = 47900 + Math.floor(Math.random() * 500);
const BASE = `http://127.0.0.1:${PORT}`;

let server;
let child;

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
  // Wait for the server to come up.
  for (let i = 0; i < 50; i++) {
    await new Promise((r) => setTimeout(r, 100));
    try {
      const res = await fetch(`${BASE}/api/config`);
      if (res.ok) break;
    } catch {}
  }
  const res = await fetch(`${BASE}/api/config`);
  assert.ok(res.ok, `server did not come up: ${err.slice(0, 300)}`);
});

after(async () => {
  if (child && !child.killed) child.kill('SIGTERM');
  await new Promise((r) => setTimeout(r, 300));
  fs.rmSync(TEST_DATA, { recursive: true, force: true });
});

test('GET /api/config returns tools and gate status', async () => {
  const res = await fetch(`${BASE}/api/config`);
  assert.equal(res.status, 200);
  const j = await res.json();
  assert.ok(j.tools.codex && j.tools.reasonix);
  assert.equal(j.gate, 'none'); // no auth configured
  assert.equal(typeof j.port, 'number');
});

test('GET /api/projects lists project subfolders', async () => {
  const res = await fetch(`${BASE}/api/projects`);
  assert.equal(res.status, 200);
  const j = await res.json();
  const names = j.map((p) => p.name);
  assert.ok(names.includes('alpha'));
  assert.ok(names.includes('beta'));
});

test('POST /api/projects rejects empty path', async () => {
  const res = await fetch(`${BASE}/api/projects`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path: '  ' })
  });
  assert.equal(res.status, 400);
});

test('POST /api/projects rejects non-existent path', async () => {
  const res = await fetch(`${BASE}/api/projects`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path: path.join(TEST_DATA, 'missing-dir') })
  });
  assert.equal(res.status, 400);
});

test('POST /api/projects/create creates a folder with git init', async () => {
  const res = await fetch(`${BASE}/api/projects/create`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ name: 'newproj', gitInit: true })
  });
  assert.equal(res.status, 201);
  const j = await res.json();
  assert.equal(j.name, 'newproj');
  assert.ok(fs.existsSync(path.join(PROJ_ROOT, 'newproj', '.git')));
});

test('POST /api/projects/create rejects traversal outside roots', async () => {
  const res = await fetch(`${BASE}/api/projects/create`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ name: '../evil' })
  });
  assert.equal(res.status, 400);
  const j = await res.json();
  assert.match(j.error, /outside/i);
  assert.ok(!fs.existsSync(path.join(path.dirname(PROJ_ROOT), 'evil')));
});

test('POST /api/projects/create rejects absolute path outside roots', async () => {
  const res = await fetch(`${BASE}/api/projects/create`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path: '/etc/hackdir' })
  });
  assert.equal(res.status, 400);
});

test('POST /api/sessions rejects unknown tool', async () => {
  const res = await fetch(`${BASE}/api/sessions`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ cwd: path.join(PROJ_ROOT, 'alpha'), tool: 'no-such-tool' })
  });
  assert.equal(res.status, 400);
  const j = await res.json();
  assert.match(j.error, /Unknown tool/);
});

test('POST /api/sessions rejects path outside roots', async () => {
  const res = await fetch(`${BASE}/api/sessions`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ cwd: '/etc', tool: 'bash' })
  });
  assert.equal(res.status, 400);
  const j = await res.json();
  assert.match(j.error, /outside/i);
});

test('POST /api/sessions rejects missing cwd', async () => {
  const res = await fetch(`${BASE}/api/sessions`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ tool: 'bash' })
  });
  assert.equal(res.status, 400);
});

test('POST /api/sessions creates a session (no start)', async () => {
  const res = await fetch(`${BASE}/api/sessions`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ cwd: path.join(PROJ_ROOT, 'alpha'), tool: 'bash', name: 'alpha-shell' })
  });
  assert.equal(res.status, 201);
  const j = await res.json();
  assert.equal(j.tool, 'bash');
  assert.equal(j.state, 'stopped');
  assert.equal(j.engine, 'pty');
});

test('GET /api/sessions lists sessions', async () => {
  const res = await fetch(`${BASE}/api/sessions`);
  assert.equal(res.status, 200);
  const j = await res.json();
  assert.ok(Array.isArray(j));
  assert.ok(j.some((s) => s.tool === 'bash'));
});

test('PUT /api/config/roots sets roots', async () => {
  const res = await fetch(`${BASE}/api/config/roots`, {
    method: 'PUT', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ roots: [PROJ_ROOT] })
  });
  assert.equal(res.status, 200);
  const j = await res.json();
  assert.deepEqual(j.roots.map((p) => path.resolve(p)), [PROJ_ROOT]);
});

test('GET /api/fs/list lists root dirs', async () => {
  const res = await fetch(`${BASE}/api/fs/list`);
  assert.equal(res.status, 200);
  const j = await res.json();
  assert.ok(Array.isArray(j));
  // '/' or Home entries present on POSIX
  assert.ok(j.length >= 1);
});

test('GET /api/fs/list rejects bad path', async () => {
  const res = await fetch(`${BASE}/api/fs/list?path=${encodeURIComponent('/definitely/not/here')}`);
  assert.equal(res.status, 400);
});
