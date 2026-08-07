// Unit tests for src/config.js — load/persist/merge, env overrides.
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const TEST_DATA = fs.mkdtempSync(path.join(os.tmpdir(), 'webpty-cfg-test-'));
process.env.WEBPTY_DATA_DIR = TEST_DATA;
// Isolate projectsRoot so defaults are predictable regardless of cwd.
process.env.WEBPTY_PROJECTS_ROOT = path.join(TEST_DATA, 'projects');

// Import AFTER env is set (module reads env at import time).
const { loadConfig, saveConfig, configPath, projectsRoot, effectivePort, dataDir, logsDir } =
  await import('../src/config.js');

beforeEach(() => {
  // Fresh data dir per test.
  fs.rmSync(TEST_DATA, { recursive: true, force: true });
  fs.mkdirSync(TEST_DATA, { recursive: true });
  fs.mkdirSync(projectsRoot, { recursive: true });
  delete process.env.WEBPTY_PORT;
  delete process.env.WEBPTY_PROJECTS_ROOT;
  delete process.env.WEBPTY_DATA_DIR;
});

afterEach(() => {
  fs.rmSync(TEST_DATA, { recursive: true, force: true });
});

test('configPath points inside the env-overridden data dir', () => {
  assert.equal(configPath, path.join(TEST_DATA, 'config.json'));
  assert.equal(dataDir, TEST_DATA);
  assert.equal(logsDir, path.join(TEST_DATA, 'logs'));
});

test('loadConfig creates default config on first run', () => {
  const cfg = loadConfig();
  assert.ok(fs.existsSync(configPath));
  assert.ok(Array.isArray(cfg.roots) && cfg.roots.length >= 1);
  assert.equal(cfg.bindHost, '0.0.0.0');
  assert.ok(cfg.tools.claude && cfg.tools.codex && cfg.tools.reasonix);
  assert.ok(Array.isArray(cfg.sessions));
});

test('loadConfig preserves user-added custom tools', () => {
  loadConfig();
  const raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  raw.tools['my-agent'] = { command: 'myagent', defaultArgs: '--custom' };
  saveConfig(raw);
  const cfg = loadConfig();
  assert.equal(cfg.tools['my-agent'].command, 'myagent');
  assert.equal(cfg.tools['my-agent'].defaultArgs, '--custom');
  // And it survives a second load (persisted, not clobbered).
  const cfg2 = loadConfig();
  assert.equal(cfg2.tools['my-agent'].command, 'myagent');
});

test('loadConfig lets user override built-in tool fields', () => {
  loadConfig();
  const raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  raw.tools.codex.defaultArgs = '--full-auto';
  saveConfig(raw);
  const cfg = loadConfig();
  assert.equal(cfg.tools.codex.defaultArgs, '--full-auto');
});

test('loadConfig disables a tool with null and keeps it disabled', () => {
  loadConfig();
  const raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  raw.tools.gemini = null;
  saveConfig(raw);
  const cfg = loadConfig();
  assert.equal(cfg.tools.gemini, null);
  // Marker persisted — a second load stays disabled (not resurrected).
  const cfg2 = loadConfig();
  assert.equal(cfg2.tools.gemini, null);
});

test('loadConfig disables a tool with false', () => {
  loadConfig();
  const raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  raw.tools.aider = false;
  saveConfig(raw);
  const cfg = loadConfig();
  assert.equal(cfg.tools.aider, false);
});

test('loadConfig recovers from corrupt config.json', () => {
  fs.writeFileSync(configPath, '{broken json');
  const cfg = loadConfig();
  assert.ok(Array.isArray(cfg.roots));
  assert.ok(cfg.tools.codex);
  // Backup of the broken file exists.
  const broken = fs.readdirSync(TEST_DATA).filter((f) => f.includes('.broken-'));
  assert.ok(broken.length >= 1);
});

test('loadConfig recovers from config.json that is literal null', () => {
  fs.writeFileSync(configPath, 'null');
  const cfg = loadConfig();
  assert.ok(Array.isArray(cfg.roots));
  assert.ok(cfg.tools.codex);
  const broken = fs.readdirSync(TEST_DATA).filter((f) => f.includes('.broken-'));
  assert.ok(broken.length >= 1);
});

test('loadConfig recovers from config.json that is a string', () => {
  fs.writeFileSync(configPath, '"just a string"');
  const cfg = loadConfig();
  assert.ok(Array.isArray(cfg.roots));
  assert.ok(cfg.tools.codex);
  const broken = fs.readdirSync(TEST_DATA).filter((f) => f.includes('.broken-'));
  assert.ok(broken.length >= 1);
});

test('loadConfig recovers from config.json that is an array', () => {
  fs.writeFileSync(configPath, '[1, 2, 3]');
  const cfg = loadConfig();
  assert.ok(Array.isArray(cfg.roots));
  assert.ok(cfg.tools.codex);
  const broken = fs.readdirSync(TEST_DATA).filter((f) => f.includes('.broken-'));
  assert.ok(broken.length >= 1);
});

test('loadConfig keeps explicit empty roots (deny all)', () => {
  loadConfig();
  const raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  raw.roots = [];
  saveConfig(raw);
  const cfg = loadConfig();
  assert.deepEqual(cfg.roots, []);
});

test('loadConfig keeps user roots instead of defaulting', () => {
  loadConfig();
  const raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  raw.roots = ['/srv/a', '/srv/b'];
  saveConfig(raw);
  const cfg = loadConfig();
  assert.deepEqual(cfg.roots.map((p) => path.resolve(p)), ['/srv/a', '/srv/b']);
});

test('loadConfig preserves allowedLogins and authToken', () => {
  loadConfig();
  const raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  raw.allowedLogins = ['User@Example.com'];
  raw.authToken = 'tok';
  saveConfig(raw);
  const cfg = loadConfig();
  assert.deepEqual(cfg.allowedLogins, ['user@example.com']);
  assert.equal(cfg.authToken, 'tok');
});

test('effectivePort: uses config port', () => {
  assert.equal(effectivePort(4791), 4791);
});

test('effectivePort: rejects invalid config ports', () => {
  assert.equal(effectivePort('abc'), 4789);
  assert.equal(effectivePort(0), 4789);
  assert.equal(effectivePort(-1), 4789);
  assert.equal(effectivePort(70000), 4789);
  assert.equal(effectivePort(undefined), 4789);
});

test('effectivePort: WEBPTY_PORT env wins over config', () => {
  process.env.WEBPTY_PORT = '8888';
  assert.equal(effectivePort(4791), 8888);
  delete process.env.WEBPTY_PORT;
});

test('effectivePort: invalid env falls back to config', () => {
  process.env.WEBPTY_PORT = 'not-a-port';
  assert.equal(effectivePort(4791), 4791);
  delete process.env.WEBPTY_PORT;
});
