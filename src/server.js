import express from 'express';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { WebSocketServer } from 'ws';
import { loadConfig, saveConfig, configPath, projectsRoot, effectivePort } from './config.js';
import { isPathUnderRoots, caseFold, publicDir, packageRoot } from './paths.js';
import { SessionManager } from './session-manager.js';
import { authorizePeer } from './auth.js';

const config = loadConfig();
const pkgRoot = packageRoot();
const app = express();
const server = http.createServer(app);
const sessions = new SessionManager(config, () => saveConfig(config));
sessions.setMaxListeners(0); // one output/agentEvent/change listener per open WS
const wss = new WebSocketServer({ noServer: true });

app.use(express.json({ limit: '256kb' }));
app.use((req, res, next) => {
  // Long-cache immutable vendor assets (xterm etc.); everything else in the
  // SPA (the HTML shell) stays no-store so updates are picked up instantly.
  if (!req.path.startsWith('/api/') && !req.path.startsWith('/ws/') && !req.path.startsWith('/vendor/')) {
    res.setHeader('Cache-Control', 'no-store');
  }
  next();
});

// Tailscale identity gate. Localhost always passes. Non-localhost passes only
// when `config.allowedLogins` includes the peer's tailnet LoginName. With an
// empty whitelist the gate is disabled (legacy behavior + boot warning).
app.use(async (req, res, next) => {
  try {
    const auth = await authorizePeer(req, config.allowedLogins, config.authToken);
    if (!auth.ok) {
      res.status(403);
      if (req.path.startsWith('/api/')) {
        return res.json({ error: 'forbidden', reason: auth.reason });
      }
      return res.type('html').send(
        `<!doctype html><meta charset="utf-8"><title>webpty</title>` +
        `<body style="font-family:system-ui;background:#0f0f0f;color:#ededed;padding:48px;max-width:600px;margin:0 auto">` +
        `<h2 style="color:#3fbf7f">webpty — access denied</h2>` +
        `<p>Peer ${auth.peer?.ip || '?'} ${auth.peer?.login ? `(${auth.peer.login}) ` : ''}is not authorized.</p>` +
        `<p style="color:#888;font-size:13px">Reason: ${auth.reason}</p>` +
        `</body>`
      );
    }
    next();
  } catch (err) {
    next(err);
  }
});

app.use(express.static(publicDir()));
app.use('/vendor/xterm', express.static(path.join(pkgRoot, 'node_modules', '@xterm', 'xterm'), { maxAge: '7d', immutable: true }));
app.use('/vendor/xterm-fit', express.static(path.join(pkgRoot, 'node_modules', '@xterm', 'addon-fit'), { maxAge: '7d', immutable: true }));
app.use('/vendor/xterm-web-links', express.static(path.join(pkgRoot, 'node_modules', '@xterm', 'addon-web-links'), { maxAge: '7d', immutable: true }));
app.use('/vendor/xterm-canvas', express.static(path.join(pkgRoot, 'node_modules', '@xterm', 'addon-canvas'), { maxAge: '7d', immutable: true }));
app.use('/vendor/xterm-unicode11', express.static(path.join(pkgRoot, 'node_modules', '@xterm', 'addon-unicode11'), { maxAge: '7d', immutable: true }));

function asyncRoute(fn) {
  return (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
}

function effectiveRoots() {
  return [...config.roots, ...(config.extraFolders || [])];
}

function validateSessionInput(body) {
  const rawCwd = String(body.cwd || '');
  if (!rawCwd) throw Object.assign(new Error('cwd required'), { status: 400 });
  const cwd = path.resolve(rawCwd);
  const tool = String(body.tool || '');
  if (!config.tools[tool]) throw Object.assign(new Error('Unknown tool'), { status: 400 });
  if (!isPathUnderRoots(cwd, effectiveRoots())) throw Object.assign(new Error('Path is outside registered roots'), { status: 400 });
  return {
    name: String(body.name || path.basename(cwd)).trim() || path.basename(cwd),
    cwd,
    tool,
    args: String(body.args || ''),
    autostart: Boolean(body.autostart)
  };
}

function encodeClaudeProject(p) {
  return path.resolve(p).replace(/[:\\/_]/g, '-');
}

function claudeHistoryMtime(cwd) {
  const dir = path.join(os.homedir(), '.claude', 'projects', encodeClaudeProject(cwd));
  let max = 0;
  try {
    for (const f of fs.readdirSync(dir)) {
      if (!f.endsWith('.jsonl')) continue;
      try {
        const m = fs.statSync(path.join(dir, f)).mtimeMs;
        if (m > max) max = m;
      } catch {}
    }
  } catch {}
  return max;
}

function listProjects() {
  const seen = new Set();
  const out = [];
  const push = (full) => {
    const key = caseFold(full);
    if (seen.has(key)) return;
    let mtime = 0;
    try { mtime = fs.statSync(full).mtimeMs; } catch { return; }
    seen.add(key);
    out.push({
      name: path.basename(full) || full,
      path: full,
      mtime,
      claudeMtime: claudeHistoryMtime(full)
    });
  };
  try {
    for (const entry of fs.readdirSync(projectsRoot, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      push(path.join(projectsRoot, entry.name));
    }
  } catch {}
  for (const p of (config.extraFolders || [])) push(path.resolve(p));
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

// Filesystem browser for the "Add Folder" picker. With no path → returns
// platform roots (Windows drives + home dir, or `/` + home dir). With a path
// → returns its direct subdirectories (hidden/system dirs that fail accessSync
// are silently dropped).
function listDirEntries(rawPath) {
  if (!rawPath) {
    const roots = [];
    if (process.platform === 'win32') {
      for (let i = 65; i <= 90; i++) {
        const letter = String.fromCharCode(i);
        const drive = `${letter}:\\`;
        try { fs.accessSync(drive); roots.push({ name: `${letter}:`, path: drive }); } catch {}
      }
    } else {
      roots.push({ name: '/', path: '/' });
    }
    const home = os.homedir();
    if (home) roots.push({ name: `Home (${path.basename(home)})`, path: home });
    return roots;
  }
  const resolved = path.resolve(rawPath);
  const entries = fs.readdirSync(resolved, { withFileTypes: true });
  return entries
    .filter((e) => e.isDirectory())
    .map((e) => ({ name: e.name, path: path.join(resolved, e.name) }))
    .filter((e) => { try { fs.accessSync(e.path); return true; } catch { return false; } })
    .sort((a, b) => a.name.localeCompare(b.name));
}

app.get('/api/config', (req, res) => {
  // Only expose enabled tools (null/false entries are user-disabled).
  const enabledTools = {};
  for (const [k, v] of Object.entries(config.tools)) {
    if (v && typeof v === 'object') enabledTools[k] = v;
  }
  res.json({
    roots: config.roots,
    projectsRoot,
    tools: enabledTools,
    configPath,
    bindHost: config.bindHost,
    port: effectivePort(config.port),
    // Gate status is exposed so the UI can show a token prompt; the token
    // itself is never sent to the browser.
    gate: config.authToken ? 'token' : (Array.isArray(config.allowedLogins) && config.allowedLogins.length ? 'tailscale' : 'none')
  });
});

app.get('/api/projects', (req, res) => res.json(listProjects()));

app.post('/api/projects', (req, res) => {
  const rawPath = String(req.body?.path || '').trim();
  if (!rawPath) return res.status(400).json({ error: 'path required' });
  const p = path.resolve(rawPath);
  let stat;
  try { stat = fs.statSync(p); } catch { return res.status(400).json({ error: 'Path does not exist' }); }
  if (!stat.isDirectory()) return res.status(400).json({ error: 'Not a directory' });
  if (!Array.isArray(config.extraFolders)) config.extraFolders = [];
  const exists = config.extraFolders.some((x) => caseFold(path.resolve(x)) === caseFold(p));
  const isAutoDiscovered = caseFold(path.dirname(p)) === caseFold(path.resolve(projectsRoot));
  if (!exists && !isAutoDiscovered) {
    config.extraFolders.push(p);
    saveConfig(config);
  }
  res.json(listProjects());
});

// Create a brand-new project folder under projectsRoot (or an absolute path
// inside a registered root). The folder is created (and git-initialized on
// request) so the user never has to touch the filesystem to start working.
app.post('/api/projects/create', (req, res) => {
  const raw = String(req.body?.name || '').trim();
  const targetRaw = String(req.body?.path || '').trim();
  if (!raw && !targetRaw) return res.status(400).json({ error: 'name required' });
  // Project name → child of projectsRoot; explicit path → must sit under a
  // registered root (same rule as session creation).
  const target = targetRaw
    ? path.resolve(targetRaw)
    : path.join(path.resolve(projectsRoot), raw);
  if (!isPathUnderRoots(target, effectiveRoots())) {
    return res.status(400).json({ error: 'Path is outside registered roots' });
  }
  if (fs.existsSync(target)) return res.status(400).json({ error: 'Already exists' });
  try {
    fs.mkdirSync(target, { recursive: true });
    if (req.body.gitInit === true) {
      try {
        execFileSync('git', ['init', '-b', 'main'], { cwd: target, stdio: 'ignore' });
      } catch (err) {
        // Don't leave a half-made project behind if git init fails.
        fs.rmSync(target, { recursive: true, force: true });
        return res.status(500).json({ error: `git init failed: ${err.message}` });
      }
    }
  } catch (err) {
    return res.status(500).json({ error: `Create failed: ${err.message}` });
  }
  const stat = fs.statSync(target);
  res.status(201).json({
    name: path.basename(target) || target,
    path: target,
    mtime: stat.mtimeMs,
    claudeMtime: 0
  });
});

app.get('/api/fs/list', (req, res) => {
  try {
    res.json(listDirEntries(String(req.query.path || '')));
  } catch (err) {
    res.status(400).json({ error: err.message || 'Cannot list path' });
  }
});

app.put('/api/config/roots', (req, res) => {
  const roots = Array.isArray(req.body.roots) ? req.body.roots : [];
  config.roots = roots.map((root) => path.resolve(String(root))).filter(Boolean);
  saveConfig(config);
  res.json({ roots: config.roots });
});

app.get('/api/sessions', (req, res) => res.json(sessions.list()));

app.put('/api/sessions/order', (req, res) => {
  const ids = Array.isArray(req.body?.ids) ? req.body.ids : [];
  sessions.reorder(ids);
  res.json({ ok: true });
});

app.post('/api/sessions', asyncRoute(async (req, res) => {
  const session = sessions.create(validateSessionInput(req.body));
  if (req.body.start) await sessions.start(session.id);
  res.status(201).json(sessions.public(session.id));
}));

app.post('/api/sessions/:id/start', asyncRoute(async (req, res) => {
  await sessions.start(req.params.id);
  res.json(sessions.public(req.params.id));
}));
app.post('/api/sessions/:id/stop', asyncRoute(async (req, res) => res.json({ ok: await sessions.stop(req.params.id) })));
app.delete('/api/sessions/:id', asyncRoute(async (req, res) => res.json({ ok: await sessions.remove(req.params.id) })));
app.post('/api/sessions/:id/input', (req, res) => res.json({ ok: sessions.write(req.params.id, req.body.bytes || '') }));

app.use((err, req, res, next) => {
  res.status(err.status || 500).json({ error: err.message || 'Server error' });
});

server.on('upgrade', async (req, socket, head) => {
  let url;
  try {
    url = new URL(req.url, `http://${req.headers.host}`);
  } catch {
    socket.destroy();
    return;
  }
  const match = url.pathname.match(/^\/ws\/sessions\/([^/]+)$/);
  if (!match) {
    socket.destroy();
    return;
  }
  // Validate the session id BEFORE handleUpgrade sends the 101 response —
  // decodeURIComponent can throw on malformed percent-encoding, and once 101
  // is on the wire the client sees an (incorrect) OPEN. Reject early instead.
  let sessionId;
  try {
    sessionId = decodeURIComponent(match[1]);
  } catch {
    socket.write('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n');
    socket.destroy();
    return;
  }
  try {
    const auth = await authorizePeer(req, config.allowedLogins, config.authToken);
    if (!auth.ok) {
      if (!socket.destroyed) {
        socket.write('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n');
        socket.destroy();
      }
      return;
    }
  } catch {
    if (!socket.destroyed) socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    ws.sessionId = sessionId;
    wss.emit('connection', ws, req);
  });
});

wss.on('connection', (ws) => {
  const session = sessions.get(ws.sessionId);
  const isAgent = session?.engine === 'agent';

  const onOutput = (id, chunk) => {
    if (id === ws.sessionId && ws.readyState === ws.OPEN) ws.send(chunk);
  };
  const onAgentEvent = (id, item) => {
    if (id === ws.sessionId && ws.readyState === ws.OPEN) {
      ws.send(JSON.stringify({ type: 'agent', item }));
    }
  };
  const onChange = (s) => {
    if (s.id === ws.sessionId && ws.readyState === ws.OPEN) {
      ws.send(JSON.stringify({ type: 'state', session: s }));
    }
  };

  if (isAgent) {
    // Catch-up: replay the cached transcript, then stream live events.
    ws.send(JSON.stringify({ type: 'snapshot', transcript: sessions.transcript(ws.sessionId) }));
    sessions.on('agentEvent', onAgentEvent);
  } else {
    // PTY: replay recent output so a freshly opened tab (or one that
    // disconnected during webpty restart) re-renders prior context before
    // taking live data.
    const recent = sessions.recentOutput(ws.sessionId);
    if (recent && recent.length && ws.readyState === ws.OPEN) ws.send(recent);
    sessions.on('output', onOutput);
  }
  sessions.on('change', onChange);

  ws.on('message', (message, isBinary) => {
    if (!isBinary) {
      const text = message.toString('utf8');
      if (text.startsWith('{')) {
        try {
          const msg = JSON.parse(text);
          if (msg.type === 'user' && typeof msg.text === 'string') {
            sessions.agentSend(ws.sessionId, msg.text);
            return;
          }
          if (msg.type === 'resize' && Number.isFinite(msg.cols) && Number.isFinite(msg.rows)) {
            sessions.resize(ws.sessionId, msg.cols, msg.rows);
            return;
          }
        } catch {}
      }
    }
    if (!isAgent) sessions.write(ws.sessionId, message);
  });
  ws.on('close', () => {
    sessions.off('output', onOutput);
    sessions.off('agentEvent', onAgentEvent);
    sessions.off('change', onChange);
  });
});

async function boot() {
  try {
    await sessions.init();
  } catch (err) {
    console.error('[webpty] failed to reach pty-host:', err.message);
    console.error('[webpty] continuing in degraded mode — PTY sessions will not start');
  }
  sessions.autostart().catch((err) => console.error('[webpty] autostart error:', err.message));
  server.listen(effectivePort(config.port), config.bindHost, () => {
    console.log(`[webpty] listening on http://${config.bindHost}:${effectivePort(config.port)}`);
    console.log(`[webpty] config: ${configPath}`);
    if (config.authToken) {
      console.log('[webpty] token gate ON — non-localhost clients must present the auth token');
    } else if (Array.isArray(config.allowedLogins) && config.allowedLogins.length) {
      console.log(`[webpty] Tailscale identity gate ON — allowed: ${config.allowedLogins.join(', ')}`);
    } else {
      console.warn('[webpty] WARNING: no authToken and allowedLogins is empty — anyone who can reach this port can access webpty.');
      console.warn('[webpty]          Set config.authToken or add your Tailscale login email(s) to enable a gate.');
    }
  });
}

boot();
