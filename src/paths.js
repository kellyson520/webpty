import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

// Windows paths are case-insensitive; POSIX paths are case-sensitive. Normalize
// case only on win32 so `/root/Projects` and `/root/projects` stay distinct on
// Linux/macOS (the old unconditional toLowerCase() merged them).
export function caseFold(p) {
  return process.platform === 'win32' ? p.toLowerCase() : p;
}

export function normalizeFsPath(input) {
  return caseFold(path.resolve(String(input || '')).replace(/[\\/]+$/, ''));
}

export function isPathUnderRoots(candidate, roots) {
  const resolved = normalizeFsPath(candidate);
  return roots.some((root) => {
    const base = normalizeFsPath(root);
    return resolved === base || resolved.startsWith(`${base}${path.sep}`);
  });
}

export function publicDir() {
  return path.resolve(here, '..', 'public');
}

export function packageRoot() {
  return path.resolve(here, '..');
}
