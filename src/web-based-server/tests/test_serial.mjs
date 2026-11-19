// Tests of serial.js in Node, with a mock of the Web Serial port that
// follows its rules (close() fails while a stream is locked):
//   node src/web-based-server/tests/test_serial.mjs

import { WebSerialPort, ByteQueue } from "../serial.js";
import { TimeoutError, PortGone } from "../protocol.js";

let failures = 0;
function check(cond, what) {
  console.log(`${cond ? "ok  " : "FAIL"} ${what}`);
  if (!cond) failures++;
}

class MockSerialPort {
  constructor(info = {}) {
    this.readable = null; this.writable = null; this.opens = []; this.written = [];
    this.info = info; this.unplugged = false;
  }
  getInfo() { return this.info; }
  async open(opts) {
    if (this.unplugged) throw new Error("device lost");
    if (this.readable) throw new Error("already open");
    this.opens.push(opts.baudRate);
    this.readable = new ReadableStream({ start: c => { this.rx = c; } });
    this.writable = new WritableStream({ write: chunk => { this.written.push(...chunk); } });
  }
  async close() {
    if (this.readable?.locked || this.writable?.locked) throw new Error("a stream is locked");
    this.readable = this.writable = null;
  }
  feed(bytes) { this.rx.enqueue(Uint8Array.from(bytes)); }
  // as Chrome when a USB adapter is pulled out: the read fails, readable
  // becomes null, and this SerialPort can never be opened again
  unplug() {
    this.unplugged = true;
    this.connected = false;
    this.rx.error(new Error("The device has been lost."));
    this.readable = null;
  }
}

const tick = () => new Promise(r => setTimeout(r, 20));

{
  const q = new ByteQueue();
  const p = q.read(3, 500);
  q.push(Uint8Array.of(1, 2));
  q.push(Uint8Array.of(3, 4));
  const got = await p;
  let timedOut = false;
  try { await q.read(2, 50); } catch (e) { timedOut = e instanceof TimeoutError; }
  const rest = await q.read(1, 10);
  check(got.join() === "1,2,3" && timedOut && rest[0] === 4,
    "ByteQueue: a read across chunks; a short read times out and leaves the bytes queued");
}

const mock = new MockSerialPort();
const port = new WebSerialPort(mock, 57600);
let gone = null;
port.onGone = e => { gone = e; };
await port.open();
mock.feed([65, 66]);
const ab = await port.read(2, 500);
await port.write(Uint8Array.of(1, 2, 3));
check(ab.join() === "65,66" && mock.written.join() === "1,2,3", "open at 57600: read and write work");

// a read waiting across the switch is served by bytes that come after it
const pending = port.read(3, 2000);
mock.feed([1]);
await tick();
await port.setBaud(9600);
mock.feed([2, 3]);
const across = await pending;
await port.write(Uint8Array.of(9));
check(mock.opens.join() === "57600,9600" && port.baud === 9600 && across.join() === "1,2,3"
  && mock.written.at(-1) === 9 && gone === null,
  "setBaud(9600): closed and reopened at 9600; a waiting read and writes carry on");

await port.setBaud(57600);
mock.feed([7]);
check((await port.read(1, 500))[0] === 7 && mock.opens.join() === "57600,9600,57600", "and back to 57600");

// the device refuses to open again: the server stops with a clear error
mock.open = async () => { throw new Error("device gone"); };
let err = null;
try { await port.setBaud(4800); } catch (e) { err = e; }
let readErr = null;
try { await port.read(1, 100); } catch (e) { readErr = e; }
check(err && /again at 4800 baud: device gone/.test(err.message) && gone === err && readErr === err,
  "a failed reopen: setBaud throws, reads fail, onGone is called");

await port.close();

// -- unplugged, and plugged in again (waitBack) --------------------------

let granted = [];                         // what navigator.serial.getPorts() gives
Object.defineProperty(globalThis, "navigator", {
  value: { serial: { getPorts: async () => granted } }, configurable: true });
const FTDI = { usbVendorId: 0x0403, usbProductId: 0x6001 };
{
  const old = new MockSerialPort(FTDI);
  const p = new WebSerialPort(old, 9600);
  await p.open();
  const waiting = p.read(1, 2000);
  old.unplug();
  let e1 = null;
  try { await waiting; } catch (e) { e1 = e; }
  check(e1 instanceof PortGone && /unplugged/.test(e1.message),
    "unplugged: a waiting read fails with PortGone");

  // the adapter comes back as a new port object; another adapter is there too
  const back = new MockSerialPort(FTDI);
  const other = new MockSerialPort({ usbVendorId: 0x1a86, usbProductId: 0x7523 });
  granted = [other];                      // not back yet at the first checks
  setTimeout(() => { granted = [other, old, back]; }, 80);
  const t0 = Date.now();
  await p.waitBack(20);
  back.feed([42]);
  const b = await p.read(1, 500);
  await p.write(Uint8Array.of(5));
  check(p.port === back && back.opens.join() === "9600" && other.opens.length === 0
    && b[0] === 42 && back.written.join() === "5" && Date.now() - t0 >= 80,
    "plugged in again: waitBack opens the new port with the same USB ids, at the same rate");

  // a write to a port that is gone fails with PortGone too
  back.writable = null;
  p.writer = { write: async () => { throw new Error("The device has been lost."); }, releaseLock() {} };
  let e2 = null;
  try { await p.write(Uint8Array.of(1)); } catch (e) { e2 = e; }
  check(e2 instanceof PortGone, "a failed write is PortGone");

  // Stop while waiting: waitBack gives up
  granted = [];
  const w = p.waitBack(20);
  setTimeout(() => p.close(), 60);
  let e3 = null;
  try { await w; } catch (e) { e3 = e; }
  check(e3 && !(e3 instanceof PortGone) && /stopped/.test(e3.message), "Stop while waiting: waitBack throws");
}

console.log(failures ? `${failures} TEST(S) FAILED` : "ALL TESTS PASSED");
process.exit(failures ? 1 : 0);
