// Shared byte ring buffer used by both pty-host (per-session scrollback) and
// the webpty server (per-session recent-output replay). Bytes only — the data
// flowing through PTYs is UTF-8 but we never decode here, we just slice the
// tail and re-emit verbatim so escape sequences stay intact.
export class RingBuffer {
  constructor(capacity) {
    this.capacity = capacity;
    this.buf = Buffer.alloc(capacity);
    this.write = 0;
    this.size = 0;
  }

  push(chunk) {
    const n = chunk.length;
    if (n === 0) return;
    if (n >= this.capacity) {
      // Chunk alone exceeds buffer — keep only the tail.
      chunk.copy(this.buf, 0, n - this.capacity, n);
      this.write = 0;
      this.size = this.capacity;
      return;
    }
    let offset = 0;
    while (offset < n) {
      const space = this.capacity - this.write;
      const toCopy = Math.min(n - offset, space);
      chunk.copy(this.buf, this.write, offset, offset + toCopy);
      this.write = (this.write + toCopy) % this.capacity;
      offset += toCopy;
    }
    this.size = Math.min(this.size + n, this.capacity);
  }

  snapshot() {
    if (this.size === 0) return Buffer.alloc(0);
    const out = Buffer.alloc(this.size);
    const start = (this.write - this.size + this.capacity) % this.capacity;
    if (start + this.size <= this.capacity) {
      this.buf.copy(out, 0, start, start + this.size);
    } else {
      const first = this.capacity - start;
      this.buf.copy(out, 0, start, this.capacity);
      this.buf.copy(out, first, 0, this.size - first);
    }
    return out;
  }
}
