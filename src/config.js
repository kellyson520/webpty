import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

function defaultDataDir() {
  if (process.platform === 'win32') {
    const appData = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
    return path.join(appData, 'webpty');
  }
  const xdg = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config');
  return path.join(xdg, 'webpty');
}

export const dataDir = process.env.WEBPTY_DATA_DIR || process.env.PTYHUB_DATA_DIR || defaultDataDir();
export const logsDir = path.join(dataDir, 'logs');
export const configPath = path.join(dataDir, 'config.json');
export const projectsRoot =
  process.env.WEBPTY_PROJECTS_ROOT ||
  process.env.PTYHUB_PROJECTS_ROOT ||
  process.env.CSMWEB_PROJECTS_ROOT ||
  path.resolve(here, '..', '..');

// Port can be overridden at boot without touching config.json (useful for
// container/CI deploys). Precedence: env var > config.json > 4789 default.
export function effectivePort(configPort) {
  const env = Number(process.env.WEBPTY_PORT || process.env.PTYHUB_PORT || '');
  if (Number.isInteger(env) && env > 0 && env < 65536) return env;
  const n = Number(configPort);
  return Number.isInteger(n) && n > 0 && n < 65536 ? n : 4789;
}

const defaultConfig = {
  bindHost: '0.0.0.0',
  port: 4789,
  roots: [projectsRoot],
  // User-added folders that live outside `projectsRoot` — surfaced in the
  // drawer alongside the auto-discovered subdirectories and treated as a
  // single-folder root for session-creation validation.
  extraFolders: [],
  // Tailscale login emails permitted to access webpty over the tailnet.
  // Empty array = identity gate disabled (any tailnet peer can access).
  // Localhost connections always bypass the gate.
  allowedLogins: [],
  // Optional shared access token. When set, every non-localhost request must
  // present it (Authorization: Bearer <token>, ?token=<token>, or the
  // `webpty_token` cookie). Takes precedence over the Tailscale gate.
  authToken: '',
  tools: {
    // nameFlag: CLI flag the tool exposes for "session display name" (claude
    // shows it in /resume picker + terminal title via `-n`). codex/agy have no
    // equivalent option, so they get null and we skip the inject at spawn.
    claude: { command: 'claude', defaultArgs: '--remote-control', nameFlag: '-n' },
    'claude-chat': { command: 'claude', defaultArgs: '', engine: 'agent', permissionMode: 'bypassPermissions', label: 'Claude (chat)' },
    codex: { command: 'codex', defaultArgs: '', nameFlag: null },
    // reasonix — Reasonix CLI agent (this project's sibling agent).
    reasonix: { command: 'reasonix', defaultArgs: '', nameFlag: null },
    // Mainstream coding agents, all mapped to the same web UI. Each entry is
    // just a spawn profile — install the CLI and it appears in the drawer.
    opencode: { command: 'opencode', defaultArgs: '', nameFlag: null },
    aider: { command: 'aider', defaultArgs: '', nameFlag: null },
    gemini: { command: 'gemini', defaultArgs: '', nameFlag: null },
    qwen: { command: 'qwen-code', defaultArgs: '', nameFlag: null },
    'cursor-agent': { command: 'cursor-agent', defaultArgs: '', nameFlag: null },
    'copilot': { command: 'copilot', defaultArgs: '', nameFlag: null },
    agy: { command: 'agy', defaultArgs: '', nameFlag: null },
    powershell: { command: 'powershell', defaultArgs: '-NoLogo' },
    bash: { command: 'bash', defaultArgs: '', nameFlag: null }
  },
  sessions: []
};

export function ensureDataDirs() {
  fs.mkdirSync(logsDir, { recursive: true });
}

function migrateLegacyDataDir() {
  if (fs.existsSync(configPath) || process.platform !== 'win32') return;
  const appData = process.env.APPDATA || '';
  // Try most-recent legacy names first, fall through to oldest.
  for (const legacyName of ['ptyhub', 'CSMWeb']) {
    const legacy = path.join(appData, legacyName);
    const legacyConfig = path.join(legacy, 'config.json');
    if (!fs.existsSync(legacyConfig)) continue;
    ensureDataDirs();
    fs.copyFileSync(legacyConfig, configPath);
    const legacyLogs = path.join(legacy, 'logs');
    if (fs.existsSync(legacyLogs)) {
      try { fs.cpSync(legacyLogs, logsDir, { recursive: true, force: false }); } catch {}
    }
    console.log(`[webpty] migrated legacy ${legacyName} config → ${configPath}`);
    return;
  }
}

export function loadConfig() {
  ensureDataDirs();
  migrateLegacyDataDir();
  if (!fs.existsSync(configPath)) {
    saveConfig(defaultConfig);
    return structuredClone(defaultConfig);
  }

  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  } catch (err) {
    // Corrupt config (partial write, hand-edit mistake). Don't crash the
    // server — back the file up and fall back to defaults so webpty still
    // boots and the user can recover the backup.
    try { fs.copyFileSync(configPath, `${configPath}.broken-${Date.now()}`); } catch {}
    console.error(`[webpty] config.json is corrupt (${err.message}) — backed up and starting with defaults`);
    raw = {};
  }
  // JSON.parse can succeed while still being invalid as a config object:
  // `null`, a bare string, or an array all parse fine but are not usable —
  // treat them like the corrupt case instead of crashing on raw.tools below.
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    try { fs.copyFileSync(configPath, `${configPath}.broken-${Date.now()}`); } catch {}
    console.error('[webpty] config.json is not a config object — backed up and starting with defaults');
    raw = {};
  }
  // Merge tools so user configuration always wins AND user-added tools are
  // preserved. Iterate over the union of default keys and raw keys — the old
  // code only walked defaultConfig.tools, silently dropping any custom tool
  // the user added to config.json (and saveConfig() below then deleted it
  // from disk, so a restart permanently locked the tool list).
  //
  // A tool set to `null` or `false` in config.json is *disabled*: it is
  // dropped from the merged list (and kept as a marker below so the
  // disable survives restarts) — letting users remove built-in tools too.
  const rawTools = (raw.tools && typeof raw.tools === 'object') ? raw.tools : {};
  const mergedTools = {};
  for (const key of new Set([...Object.keys(defaultConfig.tools), ...Object.keys(rawTools)])) {
    const userVal = rawTools[key];
    if (userVal === null || userVal === false) continue; // disabled by user
    mergedTools[key] = { ...(defaultConfig.tools[key] || {}), ...((userVal && typeof userVal === 'object') ? userVal : {}) };
  }
  const merged = {
    ...structuredClone(defaultConfig),
    ...raw,
    tools: mergedTools,
    sessions: Array.isArray(raw.sessions) ? raw.sessions : [],
    // Keep user-configured roots when present — even an explicit [] (deny
    // all). Only fall back to the default when the config predates the
    // field. (Old code clobbered this to [projectsRoot] on every load,
    // silently discarding PUT /api/config/roots — and an explicit deny-all
    // would have been reset to allow-everything on restart.)
    roots: Array.isArray(raw.roots)
      ? raw.roots.map((p) => path.resolve(String(p)))
      : [projectsRoot],
    extraFolders: Array.isArray(raw.extraFolders)
      ? raw.extraFolders.filter((p) => typeof p === 'string' && p.length).map((p) => path.resolve(p))
      : [],
    allowedLogins: Array.isArray(raw.allowedLogins)
      ? raw.allowedLogins.filter((s) => typeof s === 'string' && s.length).map((s) => s.toLowerCase())
      : [],
    authToken: typeof raw.authToken === 'string' ? raw.authToken : ''
  };
  // Persist disable markers (null/false) back into tools so a disabled tool
  // stays disabled across restarts instead of being resurrected as default.
  for (const [k, v] of Object.entries(rawTools)) {
    if (v === null || v === false) merged.tools[k] = v;
  }
  // Persist merged form so newly added defaults (e.g., new tools) appear on disk
  saveConfig(merged);
  return merged;
}

export function saveConfig(config) {
  ensureDataDirs();
  fs.writeFileSync(configPath, `${JSON.stringify(config, null, 2)}\n`, 'utf8');
}

export function safeName(value) {
  return String(value || 'session').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 80);
}
