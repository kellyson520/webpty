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
  let p = path.resolve(String(input || ''));
  // Strip trailing separators but never reduce to empty — the filesystem
  // root must stay itself ('/'). (Old code regex-stripped '/', turning it
  // into '', which broke root containment checks.)
  while (p.length > 1 && (p.endsWith('/') || p.endsWith('\\'))) {
    p = p.slice(0, -1);
  }
  return caseFold(p);
}

export function isPathUnderRoots(candidate, roots) {
  const resolved = normalizeFsPath(candidate);
  return roots.some((root) => {
    const base = normalizeFsPath(root);
    if (resolved === base) return true;
    // The filesystem root ('/') contains everything; the usual
    // `base + sep` prefix check would produce '//' and never match.
    if (base === path.parse(base).root && base.length === 1) return true;
    return resolved.startsWith(`${base}${path.sep}`);
  });
}

export function publicDir() {
  return path.resolve(here, '..', 'public');
}

export function packageRoot() {
  return path.resolve(here, '..');
}
