import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

function searchPath(command) {
  try {
    const out = execFileSync('where.exe', [command], {
      windowsHide: true,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore']
    });
    const lines = out.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    // Prefer Windows-executable extensions over extensionless shell scripts
    // (npm installs both `codex` and `codex.cmd` — only .cmd is spawnable).
    const exec = lines.find((p) => /\.(exe|cmd|bat|com)$/i.test(p));
    return exec || lines[0] || null;
  } catch {
    return null;
  }
}

export function resolveCommand(command) {
  if (!command) return null;
  if (path.isAbsolute(command) && fs.existsSync(command)) return command;
  return searchPath(command) || command;
}

export function splitArgs(input) {
  const args = [];
  let cur = '';
  let quote = null;
  let escaped = false;
  // Backslash only escapes on Windows (C:\path\style args); on POSIX a lone
  // `\` is a literal path character (e.g. `\.\d` regex or `dir\file`).
  const backslashEscapes = process.platform === 'win32';

  for (const ch of String(input || '')) {
    if (escaped) {
      cur += ch;
      escaped = false;
      continue;
    }
    if (backslashEscapes && ch === '\\') {
      escaped = true;
      continue;
    }
    if (quote) {
      if (ch === quote) quote = null;
      else cur += ch;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      continue;
    }
    if (/\s/.test(ch)) {
      if (cur) {
        args.push(cur);
        cur = '';
      }
      continue;
    }
    cur += ch;
  }
  if (cur) args.push(cur);
  return args;
}
