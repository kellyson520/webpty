// Unit tests for src/ring-buffer.js — the shared byte ring buffer.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RingBuffer } from '../src/ring-buffer.js';

test('empty buffer snapshot is empty', () => {
  const rb = new RingBuffer(8);
  assert.equal(rb.snapshot().length, 0);
});

test('push below capacity: order preserved', () => {
  const rb = new RingBuffer(8);
  rb.push(Buffer.from('ab'));
  rb.push(Buffer.from('cd'));
  rb.push(Buffer.from('ef'));
  assert.equal(rb.snapshot().toString(), 'abcdef');
});

test('push exactly capacity', () => {
  const rb = new RingBuffer(6);
  rb.push(Buffer.from('abcdef'));
  assert.equal(rb.snapshot().toString(), 'abcdef');
});

test('push over capacity: keeps newest bytes', () => {
  const rb = new RingBuffer(8);
  rb.push(Buffer.from('abcdefgh')); // fills
  rb.push(Buffer.from('ij'));       // over → drop 'ab'
  assert.equal(rb.snapshot().toString(), 'cdefghij');
});

test('multi-chunk wrap-around keeps newest', () => {
  const rb = new RingBuffer(8);
  rb.push(Buffer.from('12345678'));
  rb.push(Buffer.from('90ab'));
  rb.push(Buffer.from('cdef'));
  // 12 bytes pushed, keep last 8
  assert.equal(rb.snapshot().toString(), '567890abcdef'.slice(-8));
});

test('chunk larger than capacity keeps its tail', () => {
  const rb = new RingBuffer(4);
  rb.push(Buffer.from('abcdefghij'));
  assert.equal(rb.snapshot().toString(), 'ghij');
});

test('push empty chunk is a no-op', () => {
  const rb = new RingBuffer(4);
  rb.push(Buffer.from(''));
  assert.equal(rb.snapshot().length, 0);
});

test('repeated wrap keeps exact last-capacity bytes', () => {
  const rb = new RingBuffer(5);
  for (const c of 'a b c d e f g h i j k'.split(' ')) rb.push(Buffer.from(c));
  // 11 single-char pushes → keep last 5: 'g h i j k' → 'ghijk'
  assert.equal(rb.snapshot().toString(), 'ghijk');
});

test('snapshot returns a copy (mutating source chunk does not affect it)', () => {
  const rb = new RingBuffer(8);
  const chunk = Buffer.from('hello');
  rb.push(chunk);
  chunk[0] = 'X'.charCodeAt(0);
  assert.equal(rb.snapshot().toString(), 'hello');
});

test('large capacity single push', () => {
  const rb = new RingBuffer(1024);
  const data = Buffer.from('x'.repeat(500));
  rb.push(data);
  assert.equal(rb.snapshot().length, 500);
});
