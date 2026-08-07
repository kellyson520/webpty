import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import { logsDir, safeName } from './config.js';
import { resolveCommand, splitArgs } from './tooling.js';
import { RingBuffer } from './ring-buffer.js';
import { PtyHostClient } from './pty-host-client.js';

const AGENT_MAX_ITEMS = 4000;
const TOOL_RESULT_MAX = 8000;
// A pty session counts as "busy" (working) while it keeps repainting — every
// TUI agent animates a spinner, so output is near-continuous while a turn runs.
// Once output goes quiet for this long, it's idle/waiting for input.
const BUSY_IDLE_MS = 1200;
// Per-session recent-output ring used to replay history to a freshly opened
// browser tab. Smaller than the host's own buffer; this only needs to cover
// "what happened since the page was reloaded".
const RECENT_BUF_CAP = 128 * 1024;

export function normalizeToolResult(content) {
  let text;
  if (typeof content === 'string') text = content;
  else if (Array.isArray(content)) {
    text = content.map((b) => {
      if (typeof b === 'string') return b;
      if (b?.type === 'text') return b.text || '';
      if (b?.type === 'image') return '[image]';
      return '';
    }).join('');
  } else text = '';
  if (text.length > TOOL_RESULT_MAX) {
    text = text.slice(0, TOOL_RESULT_MAX) + `\n… (${text.length - TOOL_RESULT_MAX} more chars truncated)`;
  }
  return text;
}

const RESUME_FLAGS = new Set(['-c', '--continue', '-r', '--resume']);

function encodeClaudeProject(p) {
  return path.resolve(p).replace(/[:\\/_]/g, '-');
}

function hasPriorConversation(cwd) {
  const dir = path.join(os.homedir(), '.claude', 'projects', encodeClaudeProject(cwd));
  try {
    return fs.readdirSync(dir).some((f) => f.endsWith('.jsonl'));
  } catch { return false; }
}

const DEFAULT_COLS = 120;
const DEFAULT_ROWS = 30;


export class SessionManager extends EventEmitter {
  constructor(config, save) {
    super();
    this.config = config;
    this.save = save;
    this.sessions = new Map();
    this.host = new PtyHostClient();
    this.hostReady = false;
    this.hostSessions = new Map(); // id → host's view (from list)
    for (const stored of config.sessions) {
      const session = this.#inflate(stored);
      this.sessions.set(session.id, session);
    }
  }

  // Connect to the PTY host daemon and learn which PTYs it already owns.
  // Must be awaited before autostart() / start() are called.
  async init() {
    this.host.on('output', (id, chunk) => this.#onHostOutput(id, chunk));
    this.host.on('exit', (id, code, signal) => this.#onHostExit(id, code, signal));
    this.host.on('disconnect', () => this.#onHostDisconnect());
    await this.host.connect();
    const { sessions: hostList } = await this.host.list();
    this.hostSessions = new Map(hostList.map((s) => [s.id, s]));
    this.hostReady = true;
  }

  list() {
    return [...this.sessions.values()].map((s) => this.#publicSession(s));
  }

  public(id) {
    const session = this.sessions.get(id);
    return session ? this.#publicSession(session) : null;
  }

  get(id) {
    return this.sessions.get(id);
  }

  // Recent stdout/stderr for a session — used by server.js to replay history
  // to a freshly opened browser tab. Returns a Buffer (possibly empty).
  recentOutput(id) {
    const session = this.sessions.get(id);
    if (!session?.recentBuf) return null;
    return session.recentBuf.snapshot();
  }

  create({ name, cwd, tool, args = '', autostart = false }) {
    const id = randomUUID();
    const session = this.#inflate({ id, name: name || path.basename(cwd), cwd, tool, args, autostart });
    this.sessions.set(id, session);
    this.#persist();
    this.emit('change', this.#publicSession(session));
    return session;
  }

  async remove(id) {
    const session = this.sessions.get(id);
    if (!session) return false;
    // User intent: this session is gone. Drop it from webpty unconditionally
    // and let host cleanup run in the background — if the host is wedged,
    // refusing to await it keeps the UI responsive.
    if (session.engine === 'agent') {
      if (session.proc) { try { session.proc.kill(); } catch {} }
      session.proc = null;
    } else {
      this.host.forget(id).catch(() => {});
      this.hostSessions.delete(id);
    }
    clearTimeout(session._busyTimer);
    this.sessions.delete(id);
    this.#persist();
    this.emit('remove', id);
    return true;
  }

  reorder(ids) {
    if (!Array.isArray(ids)) return false;
    const current = this.sessions;
    const next = new Map();
    const seen = new Set();
    for (const raw of ids) {
      const id = String(raw);
      if (current.has(id) && !seen.has(id)) {
        next.set(id, current.get(id));
        seen.add(id);
      }
    }
    for (const [id, session] of current) {
      if (!seen.has(id)) next.set(id, session);
    }
    this.sessions = next;
    this.#persist();
    return true;
  }

  // start() now branches on engine. PTY sessions are delegated to the
  // detached pty-host (which keeps them alive across webpty restarts); agent
  // sessions still spawn child_process.spawn inline since claude --print's
  // stream-json protocol needs in-process buffering and doesn't benefit from
  // outliving webpty.
  async start(id) {
    const session = this.sessions.get(id);
    if (!session) return null;

    const tool = this.config.tools[session.tool];
    if (!tool) throw new Error(`Unknown tool: ${session.tool}`);

    if (tool.engine === 'agent') return this.#startAgent(session, tool);
    return this.#startPty(session, tool);
  }

  async #startPty(session, tool) {
    if (session.state === 'running') return session;
    session.engine = 'pty';

    const command = resolveCommand(tool.command);
    const userArgs = splitArgs(session.args);
    const argv = [...splitArgs(tool.defaultArgs), ...userArgs];
    const userResume = userArgs.some((a) => RESUME_FLAGS.has(a));
    if (session.tool === 'claude' && !userResume && hasPriorConversation(session.cwd)) {
      argv.unshift('-c');
    }
    if (tool.nameFlag && session.name && !userArgs.includes(tool.nameFlag)) {
      argv.unshift(tool.nameFlag, session.name);
    }

    session.logPath = path.join(logsDir, `${safeName(session.name)}-${session.id.slice(0, 8)}.log`);
    fs.mkdirSync(path.dirname(session.logPath), { recursive: true });
    fs.appendFileSync(session.logPath,
      `\r\n===== webpty start ${new Date().toISOString()} =====\r\n`);

    const startOpts = {
      id: session.id,
      command,
      args: argv,
      cwd: session.cwd,
      cols: session.cols || DEFAULT_COLS,
      rows: session.rows || DEFAULT_ROWS
    };
    const fail = (err) => {
      const message = `[webpty] failed to spawn ${command}: ${err.message}\r\n`;
      fs.appendFileSync(session.logPath, message);
      session.state = 'stopped';
      session.exitCode = -1;
      this.emit('output', session.id, Buffer.from(message));
      this.emit('change', this.#publicSession(session));
      throw err;
    };

    let started;
    try {
      started = await this.host.start(startOpts);
    } catch (err) {
      // The host may already own this id from a prior webpty run that
      // hostSessions never learned about (e.g., started after init()). Probe
      // it live: alive → reattach, dead → forget and retry.
      if (err.message !== 'already started') return fail(err);
      let view = null;
      try {
        const { sessions: list } = await this.host.list();
        view = list.find((x) => x.id === session.id) || null;
        this.hostSessions = new Map(list.map((s) => [s.id, s]));
      } catch {}
      if (view?.alive) {
        await this.#reattach(session, view);
        return session;
      }
      try { await this.host.forget(session.id); } catch {}
      this.hostSessions.delete(session.id);
      try {
        started = await this.host.start(startOpts);
      } catch (err2) {
        return fail(err2);
      }
    }

    session.pid = started.pid;
    session.mode = 'pty-host';
    session.startedAt = Date.now();
    session.state = 'running';
    session.exitCode = null;
    session.signal = null;

    // Subscribe to output from this session via the host. The host emits
    // both the replay snapshot (empty here, we just started) and any live
    // output until we detach.
    try { await this.host.attach(session.id); } catch (err) {
      console.error(`[webpty] attach failed for ${session.id}:`, err.message);
    }

    this.emit('change', this.#publicSession(session));
    return session;
  }

  // Reattach to a PTY that the host already owns (started by a previous
  // webpty run). Replays the host's buffer to current listeners.
  async #reattach(session, hostView) {
    if (session.engine !== 'pty') return false;
    session.pid = hostView.pid;
    session.mode = 'pty-host';
    session.startedAt = hostView.startedAt || Date.now();
    session.state = hostView.alive ? 'running' : 'stopped';
    session.exitCode = hostView.exitCode ?? null;
    session.signal = hostView.exitSignal ?? null;
    if (!session.logPath) {
      session.logPath = path.join(logsDir, `${safeName(session.name)}-${session.id.slice(0, 8)}.log`);
      fs.mkdirSync(path.dirname(session.logPath), { recursive: true });
    }
    fs.appendFileSync(session.logPath,
      `\r\n===== webpty reattach ${new Date().toISOString()} =====\r\n`);
    try { await this.host.attach(session.id); } catch (err) {
      console.error(`[webpty] reattach failed for ${session.id}:`, err.message);
      return false;
    }
    this.emit('change', this.#publicSession(session));
    return true;
  }

  // --- Agent engine: claude headless stream-json (CLI → HTML chat) ---------
  #startAgent(session, tool) {
    if (session.state === 'running') return session;
    const command = resolveCommand(tool.command);
    const permMode = tool.permissionMode || 'bypassPermissions';
    const argv = [
      '-p',
      '--input-format', 'stream-json',
      '--output-format', 'stream-json',
      '--verbose',
      '--permission-mode', permMode
    ];
    const resuming = Boolean(session.agentSessionId);
    if (resuming) argv.push('--resume', session.agentSessionId);
    const extra = splitArgs(session.args);
    if (extra.length) argv.push(...extra);

    session.logPath = path.join(logsDir, `${safeName(session.name)}-${session.id.slice(0, 8)}.log`);
    fs.mkdirSync(path.dirname(session.logPath), { recursive: true });
    fs.appendFileSync(session.logPath,
      `\r\n===== webpty agent start ${new Date().toISOString()} =====\r\n`);

    const useShell = process.platform === 'win32' && /\.(cmd|bat)$/i.test(command);

    let proc;
    try {
      proc = spawn(command, argv, {
        cwd: session.cwd,
        env: process.env,
        windowsHide: true,
        shell: useShell,
        stdio: ['pipe', 'pipe', 'pipe']
      });
    } catch (err) {
      session.state = 'stopped';
      session.exitCode = -1;
      session.proc = null;
      this.#pushAgent(session, { t: 'error', message: `failed to spawn ${command}: ${err.message}` });
      this.emit('change', this.#publicSession(session));
      throw err;
    }

    session.proc = proc;
    session.pid = proc.pid;
    session.mode = 'agent';
    session.engine = 'agent';
    session.startedAt = Date.now();
    session.state = 'running';
    session.exitCode = null;
    session.signal = null;
    session.turnActive = false;

    let gotInit = false;
    let buf = '';
    proc.stdout.setEncoding('utf8');
    proc.stdout.on('data', (chunk) => {
      fs.appendFile(session.logPath, chunk, () => {});
      buf += chunk;
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (line && this.#handleAgentLine(session, line)) gotInit = true;
      }
    });
    proc.stderr.setEncoding('utf8');
    proc.stderr.on('data', (chunk) => {
      fs.appendFile(session.logPath, `[stderr] ${chunk}`, () => {});
    });
    proc.on('error', (err) => {
      this.#pushAgent(session, { t: 'error', message: `process error: ${err.message}` });
    });
    proc.on('exit', (code, signal) => {
      session.state = 'stopped';
      session.exitCode = code ?? null;
      session.signal = signal ?? null;
      session.proc = null;
      session.pid = null;
      session.turnActive = false;
      fs.appendFileSync(session.logPath,
        `\r\n[webpty] agent exited code=${code} signal=${signal}\r\n`);
      if (resuming && !gotInit) {
        session.agentSessionId = null;
        this.#persist();
        this.#pushAgent(session, { t: 'error', message: 'previous conversation could not be resumed — start a new message to begin fresh' });
      } else {
        this.#pushAgent(session, { t: 'exit', code });
      }
      this.emit('change', this.#publicSession(session));
    });

    this.emit('change', this.#publicSession(session));
    return session;
  }

  #handleAgentLine(session, line) {
    let evt;
    try { evt = JSON.parse(line); } catch { return false; }
    if (!evt || typeof evt !== 'object') return false;

    switch (evt.type) {
      case 'system':
        if (evt.subtype === 'init') {
          if (evt.session_id && evt.session_id !== session.agentSessionId) {
            session.agentSessionId = evt.session_id;
            this.#persist();
          }
          this.#pushAgent(session, {
            t: 'system', model: evt.model, cwd: evt.cwd,
            permissionMode: evt.permissionMode, sessionId: evt.session_id
          });
          return true;
        }
        return false;
      case 'assistant': {
        const blocks = evt.message?.content || [];
        const mid = evt.message?.id || null;
        for (const block of blocks) {
          if (block.type === 'text') this.#pushAgent(session, { t: 'text', id: mid, text: block.text || '' });
          else if (block.type === 'thinking') this.#pushAgent(session, { t: 'thinking', id: mid, text: block.thinking || '' });
          else if (block.type === 'tool_use') this.#pushAgent(session, { t: 'tool_use', id: mid, toolId: block.id, name: block.name, input: block.input });
        }
        return false;
      }
      case 'user': {
        const blocks = evt.message?.content || [];
        for (const block of blocks) {
          if (block.type === 'tool_result') {
            this.#pushAgent(session, {
              t: 'tool_result', toolId: block.tool_use_id,
              content: normalizeToolResult(block.content), isError: Boolean(block.is_error)
            });
          }
        }
        return false;
      }
      case 'result':
        session.turnActive = false;
        if (evt.session_id) session.agentSessionId = evt.session_id;
        this.#pushAgent(session, {
          t: 'result', isError: Boolean(evt.is_error),
          costUsd: evt.total_cost_usd, durationMs: evt.duration_ms,
          numTurns: evt.num_turns,
          text: evt.subtype === 'success' ? null : (evt.result || evt.subtype || 'error')
        });
        this.emit('change', this.#publicSession(session));
        return false;
      default:
        return false;
    }
  }

  #pushAgent(session, item) {
    if (!session.transcript) session.transcript = [];
    if (item.t !== 'user') session.lastOutputAt = Date.now();
    session.transcript.push(item);
    if (session.transcript.length > AGENT_MAX_ITEMS) {
      session.transcript.splice(0, session.transcript.length - AGENT_MAX_ITEMS);
    }
    this.emit('agentEvent', session.id, item);
  }

  agentSend(id, text) {
    const session = this.sessions.get(id);
    if (!session || session.engine !== 'agent') return false;
    const message = Buffer.isBuffer(text) ? text.toString('utf8') : String(text);
    if (!message.trim()) return false;
    if (!session.proc || session.state !== 'running') return false;
    try {
      this.#pushAgent(session, { t: 'user', text: message });
      session.turnActive = true;
      session.proc.stdin.write(JSON.stringify({ type: 'user', message: { role: 'user', content: message } }) + '\n');
      this.emit('change', this.#publicSession(session));
      return true;
    } catch {
      return false;
    }
  }

  transcript(id) {
    const session = this.sessions.get(id);
    return session?.transcript || [];
  }

  async stop(id) {
    const session = this.sessions.get(id);
    if (!session) return false;
    if (session.engine === 'agent') {
      if (session.proc) {
        try { session.proc.kill(); } catch {}
        session.proc = null;
      }
    } else {
      // Graceful exit first to dodge the Windows ConPTY console-window flash
      // that a forced kill causes. Ctrl-C cancels any pending input; "exit\r"
      // ends a shell prompt; most TUI tools also quit on double Ctrl-C. Fall
      // back to host.kill only if nothing exits on its own.
      let exitedNaturally = false;
      if (session.state === 'running') {
        const waiter = this.#waitHostExit(id, 600);
        try { this.host.input(id, '\x03\x03exit\r'); } catch {}
        exitedNaturally = await waiter;
      }
      if (!exitedNaturally) {
        try { await this.host.kill(id); } catch {}
      }
    }
    session.state = 'stopped';
    session.pid = null;
    clearTimeout(session._busyTimer);
    session.busy = false;
    this.emit('change', this.#publicSession(session));
    return true;
  }

  #waitHostExit(id, ms) {
    return new Promise((resolve) => {
      let done = false;
      let timer = null;
      const onExit = (exitedId) => {
        if (exitedId !== id || done) return;
        done = true;
        this.host.off('exit', onExit);
        if (timer) clearTimeout(timer);
        resolve(true);
      };
      timer = setTimeout(() => {
        if (done) return;
        done = true;
        this.host.off('exit', onExit);
        resolve(false);
      }, ms);
      this.host.on('exit', onExit);
    });
  }

  write(id, bytes) {
    const session = this.sessions.get(id);
    if (!session || session.state !== 'running') return false;
    if (session.engine === 'agent') return false;
    const data = Buffer.isBuffer(bytes) ? bytes.toString('utf8') : String(bytes);
    return this.host.input(id, data);
  }

  resize(id, cols, rows) {
    const session = this.sessions.get(id);
    if (!session) return false;
    session.cols = cols;
    session.rows = rows;
    if (session.engine === 'agent' || session.state !== 'running') return true;
    this.host.resize(id, cols, rows);
    return true;
  }

  // Start or reattach every persisted session. For PTY sessions, reattach
  // when the host already owns the id (webpty restart case), otherwise spawn
  // fresh. Agent sessions always re-spawn — claude --print stream-json doesn't
  // survive across webpty restarts.
  async autostart() {
    for (const session of this.sessions.values()) {
      const tool = this.config.tools[session.tool];
      const engine = tool?.engine || 'pty';
      try {
        if (engine === 'agent') {
          if (session.autostart) this.start(session.id);
          continue;
        }
        // PTY: if the host still owns this id (webpty restarted but host
        // survived), always reattach — keeps the live shell visible regardless
        // of the autostart flag. Otherwise only spawn fresh when the user
        // opted in.
        const hostView = this.hostSessions.get(session.id);
        if (hostView) {
          if (await this.#reattach(session, hostView)) continue;
        }
        if (!session.autostart) continue;
        await this.start(session.id);
      } catch (err) {
        console.error(`autostart ${session.name} failed:`, err.message);
      }
    }
  }

  #onHostOutput(id, chunk) {
    const session = this.sessions.get(id);
    if (!session) return;
    this.#emitOutput(session, chunk);
  }

  #onHostExit(id, code, signal) {
    const session = this.sessions.get(id);
    if (!session) return;
    session.state = 'stopped';
    session.exitCode = code ?? null;
    session.signal = signal ?? null;
    session.pid = null;
    clearTimeout(session._busyTimer);
    session.busy = false;
    if (session.logPath) {
      try {
        fs.appendFileSync(session.logPath,
          `\r\n[webpty] exited code=${code} signal=${signal}\r\n`);
      } catch {}
    }
    this.emit('change', this.#publicSession(session));
  }

  #onHostDisconnect() {
    // Lost connection to pty-host. Mark all PTY sessions as stopped so the UI
    // reflects reality. We don't auto-reconnect — admin should restart webpty
    // (or the host) to get back into a known state.
    this.hostReady = false;
    console.error('[webpty] disconnected from pty-host');
    for (const session of this.sessions.values()) {
      if (session.engine !== 'pty') continue;
      if (session.state !== 'running') continue;
      session.state = 'stopped';
      session.pid = null;
      this.emit('change', this.#publicSession(session));
    }
  }

  #inflate(stored) {
    const engine = this.config.tools?.[stored.tool]?.engine || 'pty';
    return {
      id: stored.id || randomUUID(),
      name: stored.name,
      cwd: stored.cwd,
      tool: stored.tool,
      args: stored.args || '',
      autostart: Boolean(stored.autostart),
      state: 'stopped',
      pid: null,
      proc: null,
      startedAt: null,
      exitCode: null,
      signal: null,
      logPath: stored.logPath || null,
      mode: null,
      cols: DEFAULT_COLS,
      rows: DEFAULT_ROWS,
      engine,
      agentSessionId: stored.agentSessionId || null,
      transcript: [],
      turnActive: false,
      busy: false,
      lastOutputAt: null,
      recentBuf: engine === 'pty' ? new RingBuffer(RECENT_BUF_CAP) : null
    };
  }

  #persist() {
    this.config.sessions = [...this.sessions.values()].map(({ id, name, cwd, tool, args, autostart, logPath, agentSessionId }) => ({
      id, name, cwd, tool, args, autostart, logPath, agentSessionId
    }));
    this.save();
  }

  #publicSession(session) {
    return {
      id: session.id,
      name: session.name,
      cwd: session.cwd,
      tool: session.tool,
      args: session.args,
      autostart: session.autostart,
      state: session.state,
      pid: session.pid,
      startedAt: session.startedAt,
      exitCode: session.exitCode,
      signal: session.signal,
      logPath: session.logPath,
      mode: session.mode,
      engine: session.engine || 'pty',
      turnActive: Boolean(session.turnActive),
      busy: session.engine === 'agent' ? Boolean(session.turnActive) : Boolean(session.busy),
      lastOutputAt: session.lastOutputAt || null
    };
  }

  #emitOutput(session, chunk) {
    session.lastOutputAt = Date.now();
    this.#markBusy(session);
    if (session.recentBuf) session.recentBuf.push(chunk);
    if (session.logPath) fs.appendFile(session.logPath, chunk, () => {});
    this.emit('output', session.id, chunk);
  }

  #markBusy(session) {
    if (!session.busy) {
      session.busy = true;
      this.emit('change', this.#publicSession(session));
    }
    clearTimeout(session._busyTimer);
    session._busyTimer = setTimeout(() => {
      session.busy = false;
      this.emit('change', this.#publicSession(session));
    }, BUSY_IDLE_MS);
  }
}
