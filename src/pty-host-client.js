// PTY host client. webpty connects to a separately-running pty-host daemon
// over a named pipe / Unix socket, so PTYs (claude, codex, pwsh, …) survive
// webpty restarts. If the daemon isn't running we detach-spawn it.

import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { EventEmitter } from 'node:events';

const PIPE_NAME = process.env.WEBPTY_PTY_HOST_PIPE
  || (process.platform === 'win32'
    ? '\\\\.\\pipe\\webpty-pty-host'
    : path.join(os.tmpdir(), 'webpty-pty-host.sock'));

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const HOST_SCRIPT = path.join(__dirname, '..', 'bin', 'pty-host.mjs');

export class PtyHostClient extends EventEmitter {
  constructor() {
    super();
    this.socket = null;
    this.buf = '';
    this.reqId = 0;
    this.pending = new Map(); // reqId → {resolve, reject}
    this.serverVersion = null;
  }

  async connect() {
    if (this.socket) return;
    if (!this._connectPromise) {
      this._connectPromise = (async () => {
        await ensureHostRunning();
        await this.#openSocket();
      })().finally(() => { this._connectPromise = null; });
    }
    await this._connectPromise;
  }

  async #openSocket() {
    await new Promise((resolve, reject) => {
      const s = net.connect(PIPE_NAME);
      const onErr = (err) => { s.removeAllListeners(); reject(err); };
      s.once('error', onErr);
      s.once('connect', () => {
        s.removeListener('error', onErr);
        this.socket = s;
        s.on('data', (chunk) => this.#onData(chunk));
        s.on('close', () => {
          this.socket = null;
          this.emit('disconnect');
        });
        s.on('error', (err) => this.emit('socket-error', err));
        resolve();
      });
    });
  }

  #onData(chunk) {
    this.buf += chunk.toString('utf8');
    let nl;
    while ((nl = this.buf.indexOf('\n')) >= 0) {
      const line = this.buf.slice(0, nl);
      this.buf = this.buf.slice(nl + 1);
      if (!line.trim()) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { continue; }
      this.#onMessage(msg);
    }
  }

  #onMessage(msg) {
    // Reply to a request? Resolve the matching promise first.
    if (msg.reqId && this.pending.has(msg.reqId)) {
      const { resolve, reject } = this.pending.get(msg.reqId);
      this.pending.delete(msg.reqId);
      if (msg.ev === 'error') reject(new Error(msg.message || 'host error'));
      else resolve(msg);
      // Don't return — 'attached' replies also include replay bytes we may
      // want to surface as a synthetic 'output' so listeners see them.
      if (msg.ev === 'attached' && msg.replay) {
        const buf = Buffer.from(msg.replay, 'base64');
        if (buf.length) this.emit('output', msg.id, buf);
      }
      return;
    }
    // Spontaneous events.
    if (msg.ev === 'output') {
      this.emit('output', msg.id, Buffer.from(msg.data, 'base64'));
    } else if (msg.ev === 'exit') {
      this.emit('exit', msg.id, msg.code, msg.signal);
    } else if (msg.ev === 'hello') {
      this.serverVersion = msg.version;
      this.emit('hello', msg.version, msg.pid);
    }
  }

  async #request(op, payload = {}, timeoutMs = 5000) {
    if (!this.socket) await this.connect();
    const sock = this.socket;
    if (!sock) throw new Error('not connected');
    const reqId = ++this.reqId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (!this.pending.has(reqId)) return;
        this.pending.delete(reqId);
        reject(new Error(`host ${op} timed out`));
      }, timeoutMs);
      this.pending.set(reqId, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); }
      });
      try {
        sock.write(JSON.stringify({ op, reqId, ...payload }) + '\n');
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(reqId);
        reject(err);
      }
    });
  }

  #send(op, payload = {}) {
    if (!this.socket) return false;
    try {
      this.socket.write(JSON.stringify({ op, ...payload }) + '\n');
      return true;
    } catch { return false; }
  }

  list() { return this.#request('list'); }
  start(opts) { return this.#request('start', opts); }
  attach(id) { return this.#request('attach', { id }); }
  detach(id) { return this.#send('detach', { id }); }
  kill(id) { return this.#request('kill', { id }); }
  forget(id) { return this.#request('forget', { id }); }
  input(id, data) {
    const b64 = Buffer.isBuffer(data)
      ? data.toString('base64')
      : Buffer.from(String(data), 'utf8').toString('base64');
    return this.#send('input', { id, data: b64 });
  }
  resize(id, cols, rows) { return this.#send('resize', { id, cols, rows }); }
}

// --- helpers ---------------------------------------------------------------
async function tryConnect(timeoutMs = 600) {
  return new Promise((resolve, reject) => {
    const s = net.connect(PIPE_NAME);
    const timer = setTimeout(() => {
      s.destroy();
      reject(new Error('connect timeout'));
    }, timeoutMs);
    s.once('connect', () => {
      clearTimeout(timer);
      s.end();
      resolve();
    });
    s.once('error', (err) => {
      clearTimeout(timer);
      reject(err);
    });
  });
}

async function ensureHostRunning() {
  try { await tryConnect(); return; } catch {}
  // Not up — spawn detached, then poll.
  spawnHost();
  for (let i = 0; i < 40; i++) {
    await sleep(100);
    try { await tryConnect(300); return; } catch {}
  }
  throw new Error(`pty-host did not come up at ${PIPE_NAME}`);
}

function spawnHost() {
  const child = spawn(process.execPath, [HOST_SCRIPT], {
    detached: true,
    stdio: 'ignore',
    windowsHide: true
  });
  child.unref();
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

export const PTY_HOST_PIPE_NAME = PIPE_NAME;
