// Tests of serial.js in Node, with a mock of the Web Serial port that
// follows its rules (close() fails while a stream is locked):
//   node src/web-based-server/tests/test_serial.mjs

import { WebSerialPort, ByteQueue } from "../serial.js";
import { TimeoutError } from "../protocol.js";

let failures = 0;
function check(cond, what) {
  console.log(`${cond ? "ok  " : "FAIL"} ${what}`);
  if (!cond) failures++;
}

class MockSerialPort {
  constructor() { this.readable = null; this.writable = null; this.opens = []; this.written = []; }
  async open(opts) {
    if (this.readable) throw new Error("already open");
    this.opens.push(opts.baudRate);
    this.readable = new ReadableStream({ start: c => { this.rx = c; } });
    this.writable = new WritableStream({ write: chunk => { this.written.push(...chunk); } });
  }
  async close() {
    if (this.readable.locked || this.writable.locked) throw new Error("a stream is locked");
    this.readable = this.writable = null;
  }
  feed(bytes) { this.rx.enqueue(Uint8Array.from(bytes)); }
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
console.log(failures ? `${failures} TEST(S) FAILED` : "ALL TESTS PASSED");
process.exit(failures ? 1 : 0);
