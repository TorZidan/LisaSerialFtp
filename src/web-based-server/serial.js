// Lisa Serial FTP -- the serial port of the web-based server.
//
// ByteQueue turns a stream of incoming chunks into the blocking-style
// read(n, timeoutMs) the protocol core (protocol.js) expects, like
// PosixSerialPort.read() in the Python server.  WebSerialPort feeds one
// from a Web Serial port (Chrome / Edge only, https only).

import { TimeoutError } from "./protocol.js";

export class ByteQueue {
  constructor() {
    this.chunks = [];       // Uint8Arrays not yet read
    this.head = 0;          // bytes of chunks[0] already read
    this.size = 0;
    this.waiter = null;     // { n, resolve, reject, timer }
    this.error = null;      // set when the port is gone
  }

  push(bytes) {
    if (!bytes.length) return;
    this.chunks.push(bytes);
    this.size += bytes.length;
    this._wake();
  }

  fail(err) {
    this.error = err;
    if (this.waiter) {
      const w = this.waiter;
      this.waiter = null;
      clearTimeout(w.timer);
      w.reject(err);
    }
  }

  flush() {
    this.chunks = [];
    this.head = 0;
    this.size = 0;
  }

  _take(n) {
    const out = new Uint8Array(n);
    let got = 0;
    while (got < n) {
      const c = this.chunks[0];
      const k = Math.min(n - got, c.length - this.head);
      out.set(c.subarray(this.head, this.head + k), got);
      got += k;
      this.head += k;
      if (this.head === c.length) { this.chunks.shift(); this.head = 0; }
    }
    this.size -= n;
    return out;
  }

  _wake() {
    const w = this.waiter;
    if (w && this.size >= w.n) {
      this.waiter = null;
      clearTimeout(w.timer);
      w.resolve(this._take(w.n));
    }
  }

  // n bytes, or TimeoutError if they do not all come within timeoutMs.
  // The timeout covers the whole read, as the Python server's does.
  // The bytes that did come stay queued.
  read(n, timeoutMs) {
    if (this.error) return Promise.reject(this.error);
    if (this.size >= n) return Promise.resolve(this._take(n));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiter = null;
        reject(new TimeoutError());
      }, timeoutMs);
      this.waiter = { n, resolve, reject, timer };
    });
  }
}

// The Lisa's settings (README, "Serial line settings"): 8 data bits, no
// parity, 1 stop bit, no flow control.
export class WebSerialPort {
  constructor(port, baud) {
    this.port = port;
    this.baud = baud;
    this.queue = new ByteQueue();
    this.reader = null;
    this.writer = null;
    this.closing = false;
    this.onGone = null;     // called once if the port goes away
  }

  async open() {
    await this.port.open({
      baudRate: this.baud, dataBits: 8, parity: "none", stopBits: 1,
      flowControl: "none", bufferSize: 64 * 1024,
    });
    this.writer = this.port.writable.getWriter();
    this._readLoop();
  }

  async _readLoop() {
    try {
      while (this.port.readable && !this.closing) {
        this.reader = this.port.readable.getReader();
        try {
          for (;;) {
            const { value, done } = await this.reader.read();
            if (done) break;
            if (value) this.queue.push(value);
          }
        } catch (e) {
          // a framing / parity / break error ends this reader; the
          // stream can be read again (Web Serial spec)
          if (this.closing) break;
        } finally {
          this.reader.releaseLock();
          this.reader = null;
        }
      }
    } finally {
      const err = new Error("the serial port is closed or was unplugged");
      this.queue.fail(err);
      if (!this.closing && this.onGone) this.onGone(err);
    }
  }

  read(n, timeoutMs) { return this.queue.read(n, timeoutMs); }

  write(bytes) { return this.writer.write(bytes); }

  flushInput() { this.queue.flush(); }

  async close() {
    this.closing = true;
    this.queue.fail(new Error("stopped"));
    try { if (this.reader) await this.reader.cancel(); } catch {}
    try { if (this.writer) { this.writer.releaseLock(); } } catch {}
    try { await this.port.close(); } catch {}
  }
}
