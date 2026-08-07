// Unit tests for src/session-manager.js — SessionManager, RingBuffer, normalizeToolResult.
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';

const TEST_DATA = fs.mkdtempSync(path.join(os.tmpdir(), 'webpty-sm-test-'));
process.env.WEBPTY_DATA_DIR = TEST_DATA;
process.env.WEBPTY_PROJECTS_ROOT = path.join(TEST_DATA, 'projects');

const { SessionManager, normalizeToolResult } = await import('../src/session-manager.js');
const { RingBuffer } = await import('../src/ring-buffer.js');

// A stub PtyHostClient that records calls instead of talking to a real daemon.
class StubHost extends EventEmitter {
  constructor() { super(); this.calls = []; this.sessions = new Map(); }
  async connect() { this.calls.push('connect'); }
  async list() { this.calls.push('list'); return { sessions: [...this.sessions.values()] }; }
  async start(opts) {
    this.calls.push(['start', opts.id]);
    const view = { id: opts.id, pid: 4242, alive: true, startedAt: Date.now(), exitCode: null, exitSignal: null };
    this.sessions.set(opts.id, view);
    return { pid: 4242 };
  }
  async attach(id) { this.calls.push(['attach', id]); }
  async detach() {}
  async kill(id) { this.calls.push(['kill', id]); }
  async forget(id) { this.calls.push(['forget', id]); this.sessions.delete(id); }
  input(id, data) { this.calls.push(['input', id, data]); return true; }
  resize(id, cols, rows) { this.calls.push(['resize', id, cols, rows]); return true; }
}

function makeConfig(sessions = []) {
  return {
    tools: {
      bash: { command: 'bash', defaultArgs: '', nameFlag: null },
      'claude-chat': { command: 'claude', defaultArgs: '', engine: 'agent', permissionMode: 'bypassPermissions' }
    },
    sessions
  };
}

let sm;
let host;

beforeEach(() => {
  fs.rmSync(TEST_DATA, { recursive: true, force: true });
  fs.mkdirSync(TEST_DATA, { recursive: true });
  const cfg = makeConfig();
  sm = new SessionManager(cfg, () => {});
  host = new StubHost();
  sm.host = host;
});

test('create makes a session and persists it', () => {
  const s = sm.create({ name: 't1', cwd: '/tmp', tool: 'bash' });
  assert.ok(s.id);
  assert.equal(s.state, 'stopped');
  assert.equal(sm.get(s.id), s);
  assert.equal(sm.list().length, 1);
});

test('create defaults name to cwd basename', () => {
  const s = sm.create({ name: '', cwd: '/tmp/someproj', tool: 'bash' });
  assert.equal(s.name, 'someproj');
});

test('list returns public shape', () => {
  sm.create({ name: 't', cwd: '/tmp', tool: 'bash' });
  const pub = sm.list()[0];
  assert.equal(typeof pub.id, 'string');
  assert.equal(pub.tool, 'bash');
  assert.equal(pub.state, 'stopped');
  assert.equal(pub.engine, 'pty');
});

test('remove unknown id returns false', async () => {
  assert.equal(await sm.remove('nope'), false);
});

test('remove deletes a session', async () => {
  const s = sm.create({ name: 't', cwd: '/tmp', tool: 'bash' });
  assert.equal(await sm.remove(s.id), true);
  assert.equal(sm.get(s.id), undefined);
});

test('reorder keeps valid ids and appends unknown ones', () => {
  const a = sm.create({ name: 'a', cwd: '/tmp/a', tool: 'bash' });
  const b = sm.create({ name: 'b', cwd: '/tmp/b', tool: 'bash' });
  sm.reorder([b.id, 'ghost', a.id]);
  assert.deepEqual([...sm.sessions.keys()], [b.id, a.id]);
});

test('start pty session asks host to start', async () => {
  const s = sm.create({ name: 't', cwd: '/tmp', tool: 'bash' });
  await sm.init();
  const started = await sm.start(s.id);
  assert.equal(started.state, 'running');
  assert.ok(host.calls.some((c) => Array.isArray(c) && c[0] === 'start' && c[1] === s.id));
});

test('start unknown tool throws', async () => {
  const cfg = makeConfig();
  const s = sm.create({ name: 't', cwd: '/tmp', tool: 'bash' });
  s.tool = 'does-not-exist';
  await assert.rejects(() => sm.start(s.id), /Unknown tool/);
});

test('start twice on running session is a no-op', async () => {
  const s = sm.create({ name: 't', cwd: '/tmp', tool: 'bash' });
  await sm.init();
  await sm.start(s.id);
  const callsBefore = host.calls.filter((c) => Array.isArray(c) && c[0] === 'start').length;
  const again = await sm.start(s.id);
  const callsAfter = host.calls.filter((c) => Array.isArray(c) && c[0] === 'start').length;
  assert.equal(again.state, 'running');
  assert.equal(callsAfter, callsBefore);
});

test('stop pty session marks stopped and tries graceful exit then kill', async () => {
  const s = sm.create({ name: 't', cwd: '/tmp', tool: 'bash' });
  await sm.init();
  await sm.start(s.id);
  const ok = await sm.stop(s.id);
  assert.equal(ok, true);
  assert.equal(s.state, 'stopped');
});

test('stop unknown id returns false', async () => {
  assert.equal(await sm.stop('nope'), false);
});

test('write only works on running pty sessions', async () => {
  const s = sm.create({ name: 't', cwd: '/tmp', tool: 'bash' });
  assert.equal(sm.write(s.id, 'ls'), false); // not running
  await sm.init();
  await sm.start(s.id);
  assert.equal(sm.write(s.id, 'ls'), true);
  assert.ok(host.calls.some((c) => Array.isArray(c) && c[0] === 'input' && c[1] === s.id));
});

test('resize records cols/rows and forwards to host for pty', async () => {
  const s = sm.create({ name: 't', cwd: '/tmp', tool: 'bash' });
  await sm.init();
  await sm.start(s.id);
  assert.equal(sm.resize(s.id, 100, 40), true);
  assert.equal(s.cols, 100);
  assert.equal(s.rows, 40);
  assert.ok(host.calls.some((c) => Array.isArray(c) && c[0] === 'resize' && c[1] === s.id && c[2] === 100 && c[3] === 40));
});

test('host output updates recent buffer and emits output', async () => {
  const s = sm.create({ name: 't', cwd: '/tmp', tool: 'bash' });
  let got = null;
  sm.on('output', (id, chunk) => { if (id === s.id) got = chunk; });
  await sm.init(); // registers host output listener
  host.emit('output', s.id, Buffer.from('hello'));
  assert.equal(got.toString(), 'hello');
  assert.equal(sm.recentOutput(s.id).toString(), 'hello');
});

test('host exit marks session stopped', async () => {
  const s = sm.create({ name: 't', cwd: '/tmp', tool: 'bash' });
  await sm.init();
  await sm.start(s.id);
  host.emit('exit', s.id, 0, null);
  assert.equal(s.state, 'stopped');
  assert.equal(s.exitCode, 0);
});

test('agentSend only works on agent sessions', async () => {
  const cfg = makeConfig();
  const s = sm.create({ name: 't', cwd: '/tmp', tool: 'claude-chat' });
  // Force engine so agentSend path is exercised without a real process.
  s.engine = 'agent';
  s.state = 'stopped';
  assert.equal(sm.agentSend(s.id, 'hi'), false); // not running
});

test('RingBuffer: basic push/snapshot', () => {
  const rb = new RingBuffer(8);
  rb.push(Buffer.from('abc'));
  rb.push(Buffer.from('def'));
  assert.equal(rb.snapshot().toString(), 'abcdef');
});

test('RingBuffer: wraps around, keeps newest', () => {
  const rb = new RingBuffer(8);
  rb.push(Buffer.from('abcdefgh'));
  rb.push(Buffer.from('ij')); // 10 bytes into 8 → keeps 'cdefghij'
  assert.equal(rb.snapshot().toString(), 'cdefghij');
});

test('RingBuffer: chunk larger than capacity keeps tail', () => {
  const rb = new RingBuffer(4);
  rb.push(Buffer.from('abcdef'));
  assert.equal(rb.snapshot().toString(), 'cdef');
});

test('RingBuffer: empty snapshot is empty buffer', () => {
  const rb = new RingBuffer(4);
  assert.equal(rb.snapshot().length, 0);
});

test('RingBuffer: exact multiple fill', () => {
  const rb = new RingBuffer(6);
  rb.push(Buffer.from('abc'));
  rb.push(Buffer.from('def'));
  assert.equal(rb.snapshot().toString(), 'abcdef');
});

test('normalizeToolResult: string passes through, truncated over cap', () => {
  assert.equal(normalizeToolResult('hi'), 'hi');
  const big = 'x'.repeat(9000);
  const out = normalizeToolResult(big);
  assert.ok(out.length < 9000);
  assert.ok(out.includes('truncated'));
});

test('normalizeToolResult: array of text blocks', () => {
  const content = [{ type: 'text', text: 'a' }, 'b', { type: 'text', text: 'c' }];
  assert.equal(normalizeToolResult(content), 'abc');
});

test('normalizeToolResult: image blocks become placeholder', () => {
  assert.equal(normalizeToolResult([{ type: 'image' }]), '[image]');
});

test('normalizeToolResult: null/undefined/empty', () => {
  assert.equal(normalizeToolResult(null), '');
  assert.equal(normalizeToolResult(undefined), '');
  assert.equal(normalizeToolResult(''), '');
});
