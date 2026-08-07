#!/usr/bin/env node
// PTY host — detached daemon that owns node-pty processes on behalf of webpty.
// Survives webpty restarts so claude / codex / pwsh sessions don't die when
// the HTTP server is bounced. Listens on a named pipe (Windows) or Unix
// socket. Line-delimited JSON protocol; PTY output is base64 in the payload.

import net from 'node:net';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import * as nodePty from 'node-pty';
import { RingBuffer } from '../src/ring-buffer.js';

const PIPE_NAME = process.env.WEBPTY_PTY_HOST_PIPE
  || (process.platform === 'win32'
    ? '\\\\.\\pipe\\webpty-pty-host'
    : path.join(os.tmpdir(), 'webpty-pty-host.sock'));

const BUFFER_CAP = 256 * 1024; // per-session scrollback for replay on reattach
const HOST_VERSION = 1;
const IDLE_NO_CLIENT_KILL_MS = Number(process.env.WEBPTY_HOST_IDLE_KILL_MS || 0);

// --- Sessions ---------------------------------------------------------------
// id → { proc, buffer, clients:Set, cols, rows, pid, alive, exitCode, exitSignal }
const sessions = new Map();

function send(socket, msg) {
  if (!socket.writable) return;
  try { socket.write(JSON.stringify(msg) + '\n'); } catch {}
}

function broadcast(session, line) {
  for (const c of session.clients) {
    if (c.writable) {
      try { c.write(line); } catch {}
    }
  }
}

function handleStart(socket, msg) {
  if (sessions.has(msg.id)) {
    send(socket, { ev: 'error', reqId: msg.reqId, id: msg.id, message: 'already started' });
    return;
  }
  let proc;
  try {
    proc = nodePty.spawn(msg.command, msg.args || [], {
      name: 'xterm-256color',
      cols: msg.cols || 120,
      rows: msg.rows || 30,
      cwd: msg.cwd,
      env: { ...process.env, ...(msg.env || {}) },
      useConpty: true
    });
  } catch (err) {
    send(socket, { ev: 'error', reqId: msg.reqId, id: msg.id, message: err.message });
    return;
  }
  const session = {
    id: msg.id,
    proc,
    buffer: new RingBuffer(BUFFER_CAP),
    clients: new Set(),
    cols: msg.cols || 120,
    rows: msg.rows || 30,
    pid: proc.pid,
    alive: true,
    exitCode: null,
    exitSignal: null,
    command: msg.command,
    args: msg.args || [],
    cwd: msg.cwd,
    startedAt: Date.now()
  };
  sessions.set(msg.id, session);

  proc.onData((data) => {
    const chunk = Buffer.from(data, 'utf8');
    session.buffer.push(chunk);
    const line = JSON.stringify({ ev: 'output', id: session.id, data: chunk.toString('base64') }) + '\n';
    broadcast(session, line);
  });

  proc.onExit(({ exitCode, signal }) => {
    session.alive = false;
    session.exitCode = exitCode ?? null;
    session.exitSignal = signal ?? null;
    const line = JSON.stringify({
      ev: 'exit', id: session.id, code: session.exitCode, signal: session.exitSignal
    }) + '\n';
    broadcast(session, line);
  });

  send(socket, { ev: 'started', reqId: msg.reqId, id: session.id, pid: proc.pid });
}

function handleAttach(socket, msg) {
  const session = sessions.get(msg.id);
  if (!session) {
    send(socket, { ev: 'error', reqId: msg.reqId, id: msg.id, message: 'not found' });
    return;
  }
  session.clients.add(socket);
  socket._sessions ||= new Set();
  socket._sessions.add(msg.id);
  const snapshot = session.buffer.snapshot();
  send(socket, {
    ev: 'attached',
    reqId: msg.reqId,
    id: msg.id,
    cols: session.cols,
    rows: session.rows,
    pid: session.pid,
    alive: session.alive,
    exitCode: session.exitCode,
    exitSignal: session.exitSignal,
    replay: snapshot.toString('base64')
  });
}

function handleDetach(socket, msg) {
  const session = sessions.get(msg.id);
  if (!session) return;
  session.clients.delete(socket);
  socket._sessions?.delete(msg.id);
}

function handleInput(msg) {
  const session = sessions.get(msg.id);
  if (!session || !session.alive) return;
  try { session.proc.write(Buffer.from(msg.data, 'base64').toString('utf8')); } catch {}
}

function handleResize(msg) {
  const session = sessions.get(msg.id);
  if (!session) return;
  session.cols = msg.cols;
  session.rows = msg.rows;
  if (session.alive) {
    try { session.proc.resize(msg.cols, msg.rows); } catch {}
  }
}

function handleKill(socket, msg) {
  const session = sessions.get(msg.id);
  if (session) {
    try { session.proc.kill(); } catch {}
  }
  send(socket, { ev: 'killed', reqId: msg.reqId, id: msg.id });
}

function handleForget(socket, msg) {
  // Remove a dead session from the host's map so its id can be reused.
  const session = sessions.get(msg.id);
  if (session && session.alive) {
    try { session.proc.kill(); } catch {}
  }
  sessions.delete(msg.id);
  send(socket, { ev: 'forgotten', reqId: msg.reqId, id: msg.id });
}

function handleList(socket, msg) {
  const list = [...sessions.values()].map((s) => ({
    id: s.id, pid: s.pid, cols: s.cols, rows: s.rows,
    alive: s.alive, exitCode: s.exitCode, exitSignal: s.exitSignal,
    command: s.command, args: s.args, cwd: s.cwd, startedAt: s.startedAt
  }));
  send(socket, { ev: 'list', reqId: msg.reqId, sessions: list });
}

function onLine(socket, line) {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  switch (msg.op) {
    case 'list': return handleList(socket, msg);
    case 'start': return handleStart(socket, msg);
    case 'attach': return handleAttach(socket, msg);
    case 'detach': return handleDetach(socket, msg);
    case 'input': return handleInput(msg);
    case 'resize': return handleResize(msg);
    case 'kill': return handleKill(socket, msg);
    case 'forget': return handleForget(socket, msg);
  }
}

function onConnection(socket) {
  send(socket, { ev: 'hello', version: HOST_VERSION, pid: process.pid });
  let buf = '';
  socket.on('data', (chunk) => {
    buf += chunk.toString('utf8');
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (line.trim()) onLine(socket, line);
    }
  });
  const cleanup = () => {
    for (const id of socket._sessions || []) {
      const session = sessions.get(id);
      if (session) session.clients.delete(socket);
    }
  };
  socket.on('close', cleanup);
  socket.on('error', cleanup);
}

// --- Server lifecycle -------------------------------------------------------
// Unix socket path needs cleanup if a previous run left it behind. Windows
// named pipes are kernel objects and don't require this.
if (process.platform !== 'win32') {
  try { fs.unlinkSync(PIPE_NAME); } catch {}
}

const server = net.createServer(onConnection);

server.on('error', (err) => {
  console.error(`[pty-host] server error:`, err.message);
  process.exit(1);
});

server.listen(PIPE_NAME, () => {
  console.log(`[pty-host] listening on ${PIPE_NAME}, pid=${process.pid}`);
});

// Optional self-kill when no clients have been attached for a while AND no
// sessions are running. Off by default — pty-host normally outlives webpty.
if (IDLE_NO_CLIENT_KILL_MS > 0) {
  setInterval(() => {
    if (sessions.size > 0) return;
    // No sessions and no clients → safe to exit (webpty will respawn us).
    let anyClient = false;
    server.getConnections((err, count) => {
      if (!err && count === 0) {
        console.log('[pty-host] idle, exiting');
        process.exit(0);
      }
    });
  }, IDLE_NO_CLIENT_KILL_MS);
}

let shuttingDown = false;
function shutdown(reason) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[pty-host] shutting down (${reason})`);
  for (const s of sessions.values()) {
    try { s.proc.kill(); } catch {}
  }
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 1500).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGHUP', () => shutdown('SIGHUP'));
