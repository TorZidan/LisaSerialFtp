// Tests of the web-based server's protocol core, in Node (no browser):
//   node src/web-based-server/tests/test_protocol.mjs
// A fake Lisa client talks to LisaFtpServer over two in-memory byte
// queues.  Text pages (GET T) are checked against the Python server's
// lisa_pages(), SUM against its cmd_sum() numbers, LIST names against its
// name_to_wire() (python3 must be on the PATH).

import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import {
  LisaFtpServer, ProtocolError, crc16, lisaPages, toWire, fromWire, nameToWire,
  CHUNK, LIST_PAGE,
} from "../protocol.js";
import { ByteQueue } from "../serial.js";
import { splitName } from "../sources.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const serverDir = path.resolve(here, "../../server");
let failures = 0;

// run a Python expression with the server module as s; stdin -> input
function python(code, input = Buffer.alloc(0)) {
  return execFileSync("python3", ["-I", "-c",
    "import sys; sys.path.insert(0, sys.argv[1]); import LisaSerialFtpServer as s;" + code,
    serverDir], { input });
}

function check(cond, what) {
  console.log(`${cond ? "ok  " : "FAIL"} ${what}`);
  if (!cond) failures++;
}

// -- the link ---------------------------------------------------------------

// toServer / toClient: what each side has sent and the other not read yet.
// spoil(bytes) may change or drop what the server sends (loss tests).
function makeLink() {
  const toServer = new ByteQueue();
  const toClient = new ByteQueue();
  const link = { spoil: null, toServer, toClient };
  // the client's rate: bytes between sides at different rates arrive
  // garbled (high bytes; the line end survives, so the other side sees a
  // garbled line).  baudLog: the rates the server switched to.
  link.clientBaud = 57600;
  link.baudLog = [];
  link.garble = b => (link.clientBaud === link.serverPort.baud ? b
    : b.map(x => (x === 10 ? 10 : 0x80 | (x ^ 0x5A))));
  link.serverPort = {
    read: (n, t) => toServer.read(n, t),
    write: async bytes => {
      const b = link.spoil ? link.spoil(Uint8Array.from(bytes)) : bytes;
      if (b) toClient.push(link.garble(Uint8Array.from(b)));
    },
    flushInput: () => toServer.flush(),
    baud: 57600,
    setBaud: async rate => { link.serverPort.baud = rate; link.baudLog.push(rate); },
  };
  return link;
}

class FakeLisa {
  constructor(link) { this.link = link; }
  send(text) { this.sendRaw(toWire(text + "\r\n")); }
  sendRaw(bytes) { this.link.toServer.push(this.link.garble(Uint8Array.from(bytes))); }
  async line(t = 3000) {
    const buf = [];
    for (;;) {
      const b = (await this.link.toClient.read(1, t))[0];
      if (b === 10) break;
      if (b !== 13) buf.push(b);
    }
    return fromWire(Uint8Array.from(buf));
  }
  async frame(t = 3000) {
    const head = await this.link.toClient.read(5, t);
    if (head[0] !== 0x54) throw new Error("not a frame: " + head[0]);
    const seq = (head[1] << 8) | head[2];
    const len = (head[3] << 8) | head[4];
    const rest = await this.link.toClient.read(len + 2, t);
    const data = rest.subarray(0, len);
    const crc = (rest[len] << 8) | rest[len + 1];
    const all = new Uint8Array(4 + len);
    all.set(head.subarray(1));
    all.set(data, 4);
    return { seq, data: Uint8Array.from(data), crcOk: crc16(all) === crc };
  }
  sendFrame(seq, data, spoil = false) {
    const f = new Uint8Array(7 + data.length);
    f[0] = 0x54; f[1] = seq >> 8; f[2] = seq & 0xFF;
    f[3] = data.length >> 8; f[4] = data.length & 0xFF;
    f.set(data, 5);
    const crc = crc16(f.subarray(1, 5 + data.length));
    f[5 + data.length] = crc >> 8; f[6 + data.length] = crc & 0xFF;
    if (spoil) f[5] ^= 0xFF;                  // a data byte (or the CRC)
    this.sendRaw(f);
  }

  // the client side of a PUT.  opts:
  //   spoil: Set of chunks whose first try is corrupted (the server NAKs)
  //   loseAck: Set of chunks whose first ACK is "lost": sent again
  //   failAfter: send FAIL instead of this chunk
  //   loseDone: the DONE is "lost": the end-of-file chunk is sent again
  //   verdict: what to answer instead of the right OK
  //   announce: the size named in the PUT line, if not the real one
  async put(name, data, opts = {}) {
    const { spoil = new Set(), loseAck = new Set() } = opts;
    this.send(`PUT ${name} ${opts.announce ?? data.length}`);
    const ok = await this.line();
    if (ok !== "OK") return { reply: ok };
    let seq = 0;
    for (let off = 0; ; off += CHUNK, seq++) {
      const chunk = data.subarray(off, Math.min(off + CHUNK, data.length));
      if (opts.failAfter === seq) {
        this.send("FAIL cancelled by the user");
        const failReply = await this.line();
        await this.sync();
        return { failReply };
      }
      this.sendFrame(seq, chunk, spoil.delete(seq));
      for (;;) {
        const r = await this.line(20000);
        if (r === `N ${seq}`) { this.sendFrame(seq, chunk); continue; }
        if (r !== `A ${seq}`) return { reply: r };
        if (loseAck.delete(seq)) { this.sendFrame(seq, chunk); continue; }   // the server re-ACKs
        break;
      }
      if (!chunk.length) break;
    }
    let done = await this.line();
    if (opts.loseDone) {
      this.sendFrame(seq, new Uint8Array(0));
      const a = await this.line();
      done = await this.line();
      if (a !== `A ${seq}`) return { reply: a };
    }
    const crc = crc16(data).toString(16).padStart(4, "0");
    this.send(opts.verdict ?? (done === `DONE ${data.length} ${crc}` ? done.replace("DONE", "OK") : "BAD"));
    await this.sync();
    return { done };
  }

  // wait until the server has finished the last command (it has served
  // this HELLO, so it is back in its command loop)
  async sync() {
    this.send("HELLO");
    const b = await this.line();
    if (!b.startsWith("Hi from")) throw new Error("sync: " + b);
  }

  // the client side of a LIST: the entry lines, answering NEXT to MORE.
  // stopAt: send this line instead of NEXT at the first MORE
  async list(dir = "", stopAt = null) {
    this.send(dir ? `LIST ${dir}` : "LIST");
    const first = await this.line();
    if (first !== "OK") return { reply: first };
    const lines = [];
    let pages = 1, maxPage = 0, page = 0;
    for (;;) {
      const l = await this.line();
      page += l.length + 2;
      if (l === "MORE") {
        maxPage = Math.max(maxPage, page);
        page = 0;
        if (stopAt) { this.send(stopAt); return { lines, pages, stopped: true }; }
        this.send("NEXT");
        pages++;
        continue;
      }
      if (l === "DONE") break;
      lines.push(l);
    }
    return { lines, pages, maxPage: Math.max(maxPage, page + 2 + 2 /* OK */) };
  }

  // the client side of a GET: returns the bytes received and the verdict
  // nakFirst: NAK the first try of these chunk numbers
  async get(name, text = false, nakFirst = new Set()) {
    this.send(`GET ${name}${text ? " T" : ""}`);
    const ok = await this.line();
    if (!ok.startsWith("OK ")) return { reply: ok };
    const size = Number(ok.slice(3));
    this.send("GO");
    const got = [];
    for (let want = 0; ; ) {
      const f = await this.frame();
      if (!f.crcOk || f.seq !== want || nakFirst.has(want)) {
        nakFirst.delete(want);
        this.send(`N ${want}`);
        continue;
      }
      this.send(`A ${f.seq}`);
      if (!f.data.length) break;
      got.push(...f.data);
      want++;
    }
    const data = Uint8Array.from(got);
    this.send(`DONE ${data.length} ${crc16(data).toString(16).padStart(4, "0")}`);
    return { size, data, verdict: await this.line() };
  }
}

const same = (a, b) => a && b && a.length === b.length && a.every((x, i) => x === b[i]);

const quietLog = { say() {}, debug() {}, progress() {}, progressStart() {} };

function memorySource(files) {
  return {
    label: "memory",
    async readFile(name) {
      const parts = splitName(name);
      const f = files[parts.join("/")];
      if (!f) throw new ProtocolError(11, "no such file on server: " + name);
      return f;
    },
    writable: true,
    async openWrite(name) {
      const key = splitName(name).join("/");
      const parts = [];
      return {
        write: async b => { parts.push(...b); },
        commit: async () => { files[key] = Uint8Array.from(parts); },
        abort: async () => {},
      };
    },
    async deleteFile(name) {
      const key = splitName(name).join("/");
      if (!files[key]) throw new ProtocolError(11, "no such file on server: " + name);
      delete files[key];
    },
    async list(name) {
      const pre = splitName(name, true).join("/");
      const top = pre ? pre + "/" : "";
      if (pre && !Object.keys(files).some(k => k.startsWith(top)))
        throw new ProtocolError(10, "no such directory on server: " + name);
      const seen = new Map();
      for (const [k, v] of Object.entries(files)) {
        if (!k.startsWith(top)) continue;
        const [first, ...rest] = k.slice(top.length).split("/");
        seen.set(first, rest.length ? { name: first, size: 0, isDir: true }
          : { name: first, size: v.length, isDir: false });
      }
      return [...seen.values()];
    },
  };
}

function randomBytes(n, seed) {
  const out = new Uint8Array(n);
  let x = seed;
  for (let i = 0; i < n; i++) {
    x = (x * 1103515245 + 12345) & 0x7fffffff;
    out[i] = x >> 16;
  }
  return out;
}

// -- the tests --------------------------------------------------------------

check(crc16(toWire("123456789")) === 0x29B1, "crc16('123456789') = 29b1 (CRC-16/CCITT-FALSE)");

const textFile = toWire("program x;\r\n  begin\n\n" + "    writeln('hi');\r" +
  "a".repeat(600) + "\n" + "     end.\n".repeat(400) + "no newline at the end");
const files = {
  "empty.bin": new Uint8Array(0),
  "one.bin": randomBytes(CHUNK, 1),
  "big.bin": randomBytes(CHUNK * 5 + 123, 2),
  "docs/café x.txt": textFile,
  "prog.text": textFile,
};

const link = makeLink();
const server = new LisaFtpServer(link.serverPort, memorySource(files), quietLog);
const running = server.run().catch(e => { if (!server.stopped) throw e; });
const lisa = new FakeLisa(link);

const banner = await lisa.line();
check(banner.startsWith("Hi from Lisa Serial FTP Server version LISA-SERIAL-FTP/v1.0")
  && banner.endsWith("chunk_size=1016"), "banner at start: " + banner);

lisa.send("HELLO");
check((await lisa.line()).startsWith("Hi from"), "HELLO -> banner");
lisa.send("DEBUG OFF");
check(await lisa.line() === "OK debug off", "DEBUG OFF -> OK debug off");
lisa.send("XYZZY");
check(await lisa.line() === "ERR 99 unknown command", "an unknown command -> ERR 99");

// BAUD
{
  const sp = link.serverPort;
  lisa.send("BAUD 9600");
  const ok = await lisa.line();               // still at the old rate
  link.clientBaud = 9600;                     // the client switches too
  await new Promise(r => setTimeout(r, 400));
  lisa.send("HELLO");
  const hi = await lisa.line();
  check(ok === "OK 9600" && hi.startsWith("Hi from") && sp.baud === 9600 && server.confirmUntil === null,
    "BAUD 9600 -> OK 9600 at the old rate, then served at 9600, trial over");
  lisa.send("SUM one.bin B");
  check((await lisa.line()).startsWith("OK 1016 "), "commands work at the new rate");

  lisa.send("BAUD 57600");
  const ok2 = await lisa.line();
  link.clientBaud = 57600;
  await new Promise(r => setTimeout(r, 300));
  await lisa.sync();
  check(ok2 === "OK 57600" && sp.baud === 57600, "BAUD 57600 -> back at 57600");

  lisa.send("BAUD 57600");
  check(await lisa.line() === "OK 57600" && sp.baud === 57600 && server.confirmUntil === null,
    "BAUD at the current rate -> OK, no switch");
  lisa.send("BAUD 115200");
  check((await lisa.line()).startsWith("ERR 21 unsupported baud rate 115200"), "BAUD 115200 -> ERR 21");
  lisa.send("BAUD fast");
  check((await lisa.line()).startsWith("ERR 1 usage"), "BAUD fast -> ERR 1 usage");

  // the client did not switch: its HELLO arrives garbled -> back to the old rate
  lisa.send("BAUD 19200");
  await lisa.line();
  await new Promise(r => setTimeout(r, 300));
  const before = link.baudLog.length;
  lisa.send("HELLO");                         // garbled at 19200
  await new Promise(r => setTimeout(r, 300));
  lisa.send("HELLO");                         // the client tries again
  const hi2 = await lisa.line();
  check(hi2.startsWith("Hi from") && sp.baud === 57600 && link.baudLog[before] === 57600,
    "BAUD, then a garbled line on trial -> back to the old rate, served there");

  // silence on trial -> back to the old rate after the trial time
  server.baudTrial = 1500;
  lisa.send("BAUD 38400");
  await lisa.line();
  await new Promise(r => setTimeout(r, 2500));
  lisa.send("HELLO");
  const hi3 = await lisa.line();
  check(hi3.startsWith("Hi from") && sp.baud === 57600, "BAUD, then no client at the new rate -> back after the trial");
  server.baudTrial = 10000;

  // a garbled line at a changed rate (after the trial) -> back to the start rate
  lisa.send("BAUD 4800");
  await lisa.line();
  link.clientBaud = 4800;
  await new Promise(r => setTimeout(r, 300));
  await lisa.sync();
  link.clientBaud = 57600;                    // e.g. the Lisa client was restarted
  lisa.send("HELLO");                         // garbled at 4800
  await new Promise(r => setTimeout(r, 300));
  lisa.send("HELLO");
  check((await lisa.line()).startsWith("Hi from") && sp.baud === 57600,
    "a garbled line at a changed rate -> back to the startup rate");

  // QUIT at a changed rate -> the next client is served at the startup rate
  lisa.send("BAUD 2400");
  await lisa.line();
  link.clientBaud = 2400;
  await new Promise(r => setTimeout(r, 300));
  await lisa.sync();
  lisa.send("QUIT");
  await new Promise(r => setTimeout(r, 300));
  link.clientBaud = 57600;
  lisa.send("HELLO");
  check((await lisa.line()).startsWith("Hi from") && sp.baud === 57600, "QUIT at 2400 -> the next client at 57600");
}

// CAL: the reply comes CAL_MS (2 s) later
{
  const t0 = performance.now();
  lisa.send("CAL");
  const r = await lisa.line(5000);
  const ms = performance.now() - t0;
  check(r === "CAL 2000" && ms >= 1950 && ms < 2600, `CAL -> ${r} after ${Math.round(ms)} ms`);
}

// the bench: MARK, ECHO, FRAMES
{
  lisa.send("MARK -");
  const a = await lisa.line();
  await new Promise(r => setTimeout(r, 300));
  lisa.send("MARK something timed");
  const b = await lisa.line();
  const ms = Number(b.slice(3));
  check(a === "OK 0" && b.startsWith("OK ") && ms >= 280 && ms < 600, `MARK -> ${a}, then ${b}`);
}
{
  lisa.send("ECHO 3");
  const got = [await lisa.line(), await lisa.line(), await lisa.line()];
  await lisa.sync();
  check(got.every(l => l === "A 12345"), "ECHO 3 -> 3 lines 'A 12345', nothing else");
  lisa.send("ECHO 0");
  const e0 = await lisa.line();
  lisa.send("ECHO 101");
  const e1 = await lisa.line();
  check(e0.startsWith("ERR 1 usage") && e1.startsWith("ERR 1 usage"), "ECHO 0 / 101 -> ERR 1 usage");
}
{
  // FRAMES 3: three 1016-byte chunks of (i * 37) & 255, then the end-of-file
  // chunk; the second chunk NAKed once
  lisa.send("FRAMES 3");
  const want = new Uint8Array(CHUNK).map((_, i) => (i * 37) & 0xFF);
  const seen = [];
  let nakked = false;
  for (;;) {
    const f = await lisa.frame();
    if (f.seq === 1 && !nakked) { nakked = true; lisa.send("N 1"); continue; }
    lisa.send(`A ${f.seq}`);
    seen.push([f.seq, f.crcOk, f.data.length, f.data.length ? same(f.data, want) : true]);
    if (!f.data.length) break;
  }
  await lisa.sync();
  check(JSON.stringify(seen) === JSON.stringify([[0, true, CHUNK, true], [1, true, CHUNK, true],
    [2, true, CHUNK, true], [3, true, 0, true]]), "FRAMES 3 (one NAK) -> chunks 0..2 and the end-of-file chunk, nothing else");
  lisa.send("FRAMES 401");
  check((await lisa.line()).startsWith("ERR 1 usage"), "FRAMES 401 -> ERR 1 usage");
}

for (const name of ["empty.bin", "one.bin", "big.bin"]) {
  const r = await lisa.get(name);
  const same = r.data && r.data.length === files[name].length
    && r.data.every((b, i) => b === files[name][i]);
  check(same && r.verdict.startsWith("OK "), `GET ${name}: ${files[name].length} bytes, ${r.verdict}`);
}

// a name with an encoded space and e-acute (as LIST would show it)
{
  const r = await lisa.get("docs/café%20x.txt");
  check(r.verdict && r.verdict.startsWith("OK ") && r.data.length === textFile.length,
    "GET with %20 and a Mac Roman character in the name");
}

// GET T against the Python server's lisa_pages
{
  const r = await lisa.get("prog.text", true);
  const py = execFileSync("python3", ["-I", "-c",
    "import sys; sys.path.insert(0, sys.argv[1]);" +
    "import LisaSerialFtpServer as s;" +
    "sys.stdout.buffer.write(s.lisa_pages(sys.stdin.buffer.read()))", serverDir],
  { input: textFile });
  const js = lisaPages(textFile);
  check(Buffer.compare(Buffer.from(js), py) === 0, `lisaPages = Python lisa_pages (${py.length} bytes)`);
  check(r.verdict && r.verdict.startsWith("OK ") && Buffer.compare(Buffer.from(r.data), py) === 0,
    "GET T sends the Lisa text pages: " + r.verdict);
}

// SUM B and SUM T, against the Python server's numbers
for (const [name, kind] of [["big.bin", "B"], ["prog.text", "T"], ["prog.text", "B"], ["empty.bin", "T"]]) {
  lisa.send(`SUM ${name} ${kind}`);
  const got = await lisa.line();
  const want = python("d = sys.stdin.buffer.read();" +
    (kind === "T" ? "d = s.lisa_text(d);" : "") +
    "print('OK %d %04x %04x' % (len(d), s.crc16(d), sum(d) & 0xFFFF), end='')",
  files[name]).toString();
  check(got === want, `SUM ${name} ${kind}: ${got}`);
}
lisa.send("SUM big.bin");
check((await lisa.line()).startsWith("ERR 1 usage"), "SUM without T|B -> ERR 1 usage");
lisa.send("SUM nothere.bin B");
check((await lisa.line()).startsWith("ERR 11 "), "SUM of a missing file -> ERR 11");

// LIST: names as name_to_wire() gives them, sorted
{
  const names = ["plain.TXT", "caf\u00e9 x.txt", "a+b&c", "\u4e2d.txt", "tab\there", "~tilde-_."];
  const py = JSON.parse(python("import json; print(json.dumps([s.name_to_wire(n) for n in json.loads(sys.stdin.read())]))",
    Buffer.from(JSON.stringify(names))).toString());
  const js = names.map(nameToWire);
  check(JSON.stringify(js) === JSON.stringify(py), "nameToWire = Python name_to_wire: " + js.join(" "));
}
{
  const r = await lisa.list();
  check(JSON.stringify(r.lines) === JSON.stringify([
    "F 5203 big.bin", "D 0 docs", "F 0 empty.bin", "F 1016 one.bin", `F ${textFile.length} prog.text`])
    && r.pages === 1, "LIST: " + r.lines.join(" | "));
}
{
  const r = await lisa.list("docs");
  check(r.lines.length === 1 && r.lines[0] === `F ${textFile.length} caf\u00e9%20x.txt`, "LIST docs: " + r.lines[0]);
}
lisa.send("LIST nothere");
check((await lisa.line()).startsWith("ERR 10 "), "LIST of a missing folder -> ERR 10");
lisa.send("LIST ../x");
check(await lisa.line() === "ERR 2 bad name", "LIST ../x -> ERR 2 bad name");

// a long listing: pages of at most LIST_PAGE bytes, with MORE / NEXT
const many = {};
for (let i = 0; i < 120; i++) many[`dir/file-with-a-longish-name-${String(i).padStart(3, "0")}.txt`] = new Uint8Array(i);
Object.assign(files, many);
{
  const r = await lisa.list("dir");
  const sorted = Object.keys(many).map(k => k.slice(4)).sort();
  const ok = r.lines.length === 120 && r.lines.every((l, i) => l === `F ${i} ${sorted[i]}`);
  check(ok && r.pages > 10 && r.maxPage <= LIST_PAGE,
    `LIST of 120 entries: ${r.pages} pages, the largest ${r.maxPage} bytes (<= ${LIST_PAGE})`);
}
{
  // the client stops the listing with another command (Apple-period, then HELLO)
  const r = await lisa.list("dir", "HELLO");
  check(r.stopped && (await lisa.line()).startsWith("Hi from"),
    "a command instead of NEXT ends the listing and is served");
}

// errors
lisa.send("GET nothere.bin");
check((await lisa.line()).startsWith("ERR 11 "), "GET of a missing file -> ERR 11");
lisa.send("GET ../etc/passwd");
check(await lisa.line() === "ERR 2 bad name", "GET ../x -> ERR 2 bad name");
lisa.send("GET");
check((await lisa.line()).startsWith("ERR 1 "), "GET without a name -> ERR 1");
lisa.send("GET a b c");
check((await lisa.line()).startsWith("ERR 1 usage"), "GET with 3 arguments -> ERR 1 usage");

// loss recovery: NAKs, then a corrupted frame
{
  const r = await lisa.get("big.bin", false, new Set([0, 2, 6]));
  check(r.verdict && r.verdict.startsWith("OK "), "GET with NAKed chunks 0, 2 and the end-of-file chunk");
}
{
  let n = 0;
  link.spoil = b => {
    if (b[0] === 0x54 && ++n === 2) b[10] ^= 0xFF;   // the 2nd frame, a data byte
    return b;
  };
  const r = await lisa.get("big.bin");
  link.spoil = null;
  check(r.verdict && r.verdict.startsWith("OK ") && n > 7, "GET with a corrupted frame (CRC -> NAK -> resent)");
}

// the client gives up in the middle
{
  lisa.send("GET big.bin");
  await lisa.line();
  lisa.send("GO");
  await lisa.frame();
  lisa.send("FAIL cancelled by the user");
  check(await lisa.line() === "OK", "FAIL from the client -> OK");
  lisa.send("HELLO");
  check((await lisa.line()).startsWith("Hi from"), "and the server serves the next command");
}

// a restarted client (HELLO instead of an ACK)
{
  lisa.send("GET big.bin");
  await lisa.line();
  lisa.send("GO");
  await lisa.frame();
  lisa.send("HELLO");
  check((await lisa.line()).startsWith("Hi from"), "HELLO mid-transfer -> a fresh banner");
}

// PUT
{
  const up = randomBytes(CHUNK * 3 + 7, 3);
  const r = await lisa.put("up/new.bin", up);
  check(r.done && same(files["up/new.bin"], up), `PUT of ${up.length} bytes: ${r.done}, stored`);
}
{
  const up = randomBytes(CHUNK * 2, 4);
  const r = await lisa.put("up/new.bin", up, { spoil: new Set([0, 2]), loseAck: new Set([1]) });
  check(r.done && same(files["up/new.bin"], up),
    "PUT over a file, with corrupted chunks (NAK) and a lost ACK (re-ACK)");
}
{
  const up = randomBytes(500, 5);
  const r = await lisa.put("up/lostdone.bin", up, { loseDone: true });
  check(r.done && same(files["up/lostdone.bin"], up),
    "PUT with a lost DONE: the end-of-file chunk again -> ACK and DONE again");
}
{
  const old = files["up/new.bin"];
  const r = await lisa.put("up/new.bin", randomBytes(3000, 6), { failAfter: 1 });
  check(r.failReply === "OK" && files["up/new.bin"] === old, "PUT aborted by the client: OK, the old file kept");
}
{
  const old = files["up/new.bin"];
  const r = await lisa.put("up/new.bin", randomBytes(100, 7), { verdict: "BAD 100 0000" });
  check(r.done && files["up/new.bin"] === old, "PUT with a BAD verdict: the old file kept");
}
{
  const r = await lisa.put("up/short.bin", randomBytes(100, 8), { announce: 200 });
  check(r.done && r.done.startsWith("ERR 5 size mismatch") && !files["up/short.bin"],
    "PUT with fewer bytes than announced: ERR 5, not stored");
}
{
  const r = await lisa.put("empty-up.bin", new Uint8Array(0));
  check(r.done === "DONE 0 ffff" && same(files["empty-up.bin"], new Uint8Array(0)), "PUT of an empty file");
}
lisa.send("PUT x.bin");
check((await lisa.line()).startsWith("ERR 1 usage"), "PUT without a size -> ERR 1 usage");
lisa.send("PUT ../x.bin 5");
check(await lisa.line() === "ERR 2 bad name", "PUT ../x -> ERR 2 bad name");

// DELETE
lisa.send("DELETE empty-up.bin");
check(await lisa.line() === "OK deleted" && !("empty-up.bin" in files), "DELETE a file -> OK deleted");
lisa.send("DELETE empty-up.bin");
check((await lisa.line()).startsWith("ERR 11 "), "DELETE a missing file -> ERR 11");
lisa.send("DELETE");
check((await lisa.line()).startsWith("ERR 1 usage"), "DELETE without a name -> ERR 1 usage");

// a read-only source
{
  const ro = makeLink();
  const src = memorySource({ "a.bin": new Uint8Array(3) });
  src.writable = false;
  const srv = new LisaFtpServer(ro.serverPort, src, quietLog);
  const run2 = srv.run().catch(e => { if (!srv.stopped) throw e; });
  const lisa2 = new FakeLisa(ro);
  await lisa2.line();
  lisa2.send("PUT b.bin 3");
  const p = await lisa2.line();
  lisa2.send("DELETE a.bin");
  const d = await lisa2.line();
  check(p === "ERR 20 server is read-only" && d === p, "read-only source: PUT and DELETE -> ERR 20");
  srv.stop();
  ro.toServer.fail(new Error("stopped"));
  await run2;
}

lisa.send("QUIT");
lisa.send("HELLO");
check((await lisa.line()).startsWith("Hi from"), "after QUIT, a new client is served");

server.stop();
link.toServer.fail(new Error("stopped"));
await running;

console.log(failures ? `${failures} TEST(S) FAILED` : "ALL TESTS PASSED");
process.exit(failures ? 1 : 0);
