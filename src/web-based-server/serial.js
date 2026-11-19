// Lisa Serial FTP -- the serial port of the web-based server.
//
// ByteQueue turns a stream of incoming chunks into the blocking-style
// read(n, timeoutMs) the protocol core (protocol.js) expects, like
// PosixSerialPort.read() in the Python server.  WebSerialPort feeds one
// from a Web Serial port (Chrome / Edge only, https only).

import { TimeoutError, PortGone } from "./protocol.js";

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
    this.reopening = false; // setBaud: the read loop ends, the queue stays
    this.loop = null;       // the read loop's promise
    this.onGone = null;     // called once if the port goes away
  }

  async open() {
    await this.port.open({
      baudRate: this.baud, dataBits: 8, parity: "none", stopBits: 1,
      flowControl: "none", bufferSize: 64 * 1024,
    });
    this.writer = this.port.writable.getWriter();
    this.loop = this._readLoop();
  }

  // BAUD: Web Serial cannot change the rate of an open port, so close it
  // and open it again at the new rate.  The queue (and a read waiting on
  // it) carries on: to the protocol code it is the same port.
  async setBaud(rate) {
    this.reopening = true;
    try {
      try { if (this.reader) await this.reader.cancel(); } catch {}
      await this.loop;                 // the read loop has ended
      try { this.writer.releaseLock(); } catch {}
      await this.port.close();
    } finally {
      this.reopening = false;          // (before open: its read loop checks it)
    }
    this.baud = rate;
    try {
      await this.open();
    } catch (e) {
      const err = new PortGone(`cannot open the serial port again at ${rate} baud: ${e.message}`);
      this.queue.fail(err);
      if (this.onGone) this.onGone(err);
      throw err;
    }
  }

  async _readLoop() {
    try {
      while (this.port.readable && !this.closing && !this.reopening) {
        this.reader = this.port.readable.getReader();
        try {
          for (;;) {
            const { value, done } = await this.reader.read();
            if (done) break;
            if (value) this.queue.push(value);
          }
        } catch (e) {
          // a framing / parity / break error ends this reader; the
          // stream can be read again (Web Serial spec).  Bytes at a wrong
          // rate cause these, so after BAUD they are normal.
          if (this.closing || this.reopening) break;
        } finally {
          this.reader.releaseLock();
          this.reader = null;
        }
      }
    } finally {
      if (this.reopening) return;      // setBaud opens it again
      const err = new PortGone("the serial port is closed or was unplugged");
      this.queue.fail(err);
      if (!this.closing && this.onGone) this.onGone(err);
    }
  }

  read(n, timeoutMs) { return this.queue.read(n, timeoutMs); }

  async write(bytes) {
    try {
      await this.writer.write(bytes);
    } catch (e) {
      if (this.closing) throw e;
      throw new PortGone(`cannot write to the serial port: ${e.message}`);
    }
  }

  // The port went away (PortGone): wait for it to come back, checking once
  // every poll ms, and open it again, at the rate it was at -- as
  // wait_back() in the Python server.  A USB adapter plugged in again is a
  // new SerialPort for the browser: it is the one among the ports this page
  // may use (navigator.serial.getPorts()) with the same USB vendor and
  // product id.  (Chrome keeps the permission for an adapter with a USB
  // serial number; for one without, it may ask again: then Stop and
  // Start.)  Throws if close() is called meanwhile (Stop).
  async waitBack(poll = 1000) {
    let info = {};
    try { info = this.port.getInfo(); } catch {}
    this.reopening = true;             // the read loop ends without failing the queue
    try {
      try { if (this.reader) await this.reader.cancel(); } catch {}
      try { await this.loop; } catch {}
      try { this.writer.releaseLock(); } catch {}
      try { await this.port.close(); } catch {}
    } finally {
      this.reopening = false;
    }
    while (!this.closing) {
      await new Promise(r => setTimeout(r, poll));
      if (this.closing) break;
      let ports = [];
      try { ports = await navigator.serial.getPorts(); } catch {}
      const same = p => {
        if (p === this.port) return true;
        if (info.usbVendorId === undefined) return false;
        let i = {};
        try { i = p.getInfo(); } catch {}
        return i.usbVendorId === info.usbVendorId && i.usbProductId === info.usbProductId;
      };
      // the old port object first, then the others like it
      const tries = ports.filter(same).sort((a, b) => (b === this.port) - (a === this.port));
      for (const p of tries) {
        if (p.connected === false) continue;
        try {
          this.queue.error = null;
          this.queue.flush();
          const old = this.port;
          this.port = p;
          try {
            await this.open();
          } catch (e) {
            this.port = old;
            throw e;
          }
          if (this.closing) {          // Stop while it was opening
            try { this.writer.releaseLock(); } catch {}
            try { await this.port.close(); } catch {}
            break;
          }
          return;
        } catch {
          // not there yet, or in use by another program
        }
      }
    }
    throw new Error("stopped");
  }

  flushInput() { this.queue.flush(); }

  async close() {
    this.closing = true;
    this.queue.fail(new Error("stopped"));
    try { if (this.reader) await this.reader.cancel(); } catch {}
    try { if (this.writer) { this.writer.releaseLock(); } } catch {}
    try { await this.port.close(); } catch {}
  }
}
