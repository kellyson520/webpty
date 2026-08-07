// Unit tests for src/tooling.js — resolveCommand / splitArgs.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { resolveCommand, splitArgs } from '../src/tooling.js';

test('splitArgs: splits on whitespace', () => {
  assert.deepEqual(splitArgs('a b c'), ['a', 'b', 'c']);
});

test('splitArgs: collapses runs of whitespace', () => {
  assert.deepEqual(splitArgs('a    b\t c'), ['a', 'b', 'c']);
});

test('splitArgs: preserves empty quoted argument', () => {
  assert.deepEqual(splitArgs('a "" b'), ['a', '', 'b']);
});

test('splitArgs: trailing empty quoted argument', () => {
  assert.deepEqual(splitArgs('--name ""'), ['--name', '']);
});

test('splitArgs: lone empty quotes produce one empty arg', () => {
  assert.deepEqual(splitArgs('""'), ['']);
});

test('splitArgs: quotes group whitespace', () => {
  assert.deepEqual(splitArgs('--name "my project"'), ['--name', 'my project']);
});

test('splitArgs: single quotes group whitespace', () => {
  assert.deepEqual(splitArgs("--name 'my project'"), ['--name', 'my project']);
});

test('splitArgs: empty input gives no args', () => {
  assert.deepEqual(splitArgs(''), []);
  assert.deepEqual(splitArgs(null), []);
  assert.deepEqual(splitArgs(undefined), []);
});

test('splitArgs: backslash is literal on POSIX', { skip: process.platform === 'win32' }, () => {
  // On POSIX a lone backslash is a path char (e.g. dir\file), not an escape.
  assert.deepEqual(splitArgs('a\\b c'), ['a\\b', 'c']);
});

test('splitArgs: backslash escapes on Windows', { skip: process.platform !== 'win32' }, () => {
  assert.deepEqual(splitArgs('a\\ b'), ['a b']);
});

test('splitArgs: backslash inside quotes stays literal on Windows', { skip: process.platform !== 'win32' }, () => {
  // Windows command lines pass quoted paths like "C:\temp\new" through
  // verbatim — backslashes must not escape the following char inside quotes.
  assert.deepEqual(splitArgs('--config "C:\\temp\\new"'), ['--config', 'C:\\temp\\new']);
  assert.deepEqual(splitArgs('"C:\\Program Files\\app.exe"'), ['C:\\Program Files\\app.exe']);
});

test('splitArgs: mixed quoting', () => {
  assert.deepEqual(splitArgs('x "y z" w'), ['x', 'y z', 'w']);
  assert.deepEqual(splitArgs('x "y"z'), ['x', 'yz']);
});

test('resolveCommand: absolute existing path is returned as-is', () => {
  const p = path.join(os.tmpdir(), 'webpty-tooling-abs-test');
  fs.writeFileSync(p, '#!/bin/sh\n');
  try {
    assert.equal(resolveCommand(p), p);
  } finally {
    fs.rmSync(p, { force: true });
  }
});

test('resolveCommand: empty command gives null', () => {
  assert.equal(resolveCommand(''), null);
  assert.equal(resolveCommand(null), null);
});

test('resolveCommand: bare name falls back to itself when not on PATH', () => {
  // 'webpty-definitely-not-a-real-binary-xyz' should not resolve; the fallback
  // is the original command string (spawn will fail at runtime, which is the
  // caller's job to surface).
  const cmd = 'webpty-definitely-not-a-real-binary-xyz';
  const r = resolveCommand(cmd);
  assert.ok(r === cmd || r === null || r.endsWith(cmd));
});
