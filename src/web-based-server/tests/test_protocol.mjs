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
  link.serverPort = {
    read: (n, t) => toServer.read(n, t),
    write: async bytes => {
      const b = link.spoil ? link.spoil(Uint8Array.from(bytes)) : bytes;
      if (b) toClient.push(Uint8Array.from(b));
    },
    flushInput: () => toServer.flush(),
  };
  return link;
}

class FakeLisa {
  constructor(link) { this.link = link; }
  send(text) { this.link.toServer.push(toWire(text + "\r\n")); }
  sendRaw(bytes) { this.link.toServer.push(bytes); }
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
lisa.send("DELETE x");
check(await lisa.line() === "ERR 99 unknown command", "DELETE -> ERR 99 (not in the prototype yet)");

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

lisa.send("QUIT");
lisa.send("HELLO");
check((await lisa.line()).startsWith("Hi from"), "after QUIT, a new client is served");

server.stop();
link.toServer.fail(new Error("stopped"));
await running;

console.log(failures ? `${failures} TEST(S) FAILED` : "ALL TESTS PASSED");
process.exit(failures ? 1 : 0);
