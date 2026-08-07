// Unit tests for src/paths.js — path normalization and root containment.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { caseFold, normalizeFsPath, isPathUnderRoots } from '../src/paths.js';

test('caseFold: identity on POSIX, lowercase on win32', () => {
  if (process.platform === 'win32') {
    assert.equal(caseFold('/Root/Web'), '/root/web');
  } else {
    assert.equal(caseFold('/Root/Web'), '/Root/Web');
  }
});

test('normalizeFsPath: resolves relative to cwd', () => {
  const cwd = process.cwd();
  assert.equal(normalizeFsPath('.'), cwd);
  assert.equal(normalizeFsPath('./x'), path.join(cwd, 'x'));
});

test('normalizeFsPath: strips trailing slashes', () => {
  const root = normalizeFsPath('/tmp/abc');
  assert.equal(normalizeFsPath('/tmp/abc///'), root);
  assert.equal(normalizeFsPath('/tmp/abc/'), root);
});

test('normalizeFsPath: root stays root', () => {
  assert.equal(normalizeFsPath('/'), path.parse('/').root);
});

test('isPathUnderRoots: direct child under root', () => {
  const root = path.resolve('/tmp', 'wp-root-a');
  assert.ok(isPathUnderRoots(path.join(root, 'proj'), [root]));
});

test('isPathUnderRoots: root itself is under root', () => {
  const root = path.resolve('/tmp', 'wp-root-b');
  assert.ok(isPathUnderRoots(root, [root]));
});

test('isPathUnderRoots: sibling with shared prefix is NOT under root', () => {
  const root = path.resolve('/tmp', 'wp-root-c');
  const sibling = path.resolve('/tmp', 'wp-root-c2');
  assert.ok(!isPathUnderRoots(sibling, [root]));
});

test('isPathUnderRoots: parent of root is NOT under root', () => {
  const root = path.resolve('/tmp', 'wp-root-d', 'sub');
  const parent = path.resolve('/tmp', 'wp-root-d');
  assert.ok(!isPathUnderRoots(parent, [root]));
});

test('isPathUnderRoots: deep nesting under root', () => {
  const root = path.resolve('/tmp', 'wp-root-e');
  assert.ok(isPathUnderRoots(path.join(root, 'a', 'b', 'c'), [root]));
});

test('isPathUnderRoots: filesystem root is under itself', () => {
  const fsRoot = path.parse('/').root;
  assert.ok(isPathUnderRoots(fsRoot, [fsRoot]));
});

test('isPathUnderRoots: absolute root contains everything', () => {
  const fsRoot = path.parse('/').root;
  assert.ok(isPathUnderRoots('/etc', [fsRoot]));
  assert.ok(isPathUnderRoots('/usr/local/bin', [fsRoot]));
});

test('isPathUnderRoots: empty roots deny everything', () => {
  assert.ok(!isPathUnderRoots('/tmp/x', []));
});

test('isPathUnderRoots: any-of semantics with multiple roots', () => {
  const r1 = path.resolve('/tmp', 'wp-root-f1');
  const r2 = path.resolve('/tmp', 'wp-root-f2');
  assert.ok(isPathUnderRoots(path.join(r2, 'p'), [r1, r2]));
  assert.ok(!isPathUnderRoots('/etc/passwd', [r1, r2]));
});
