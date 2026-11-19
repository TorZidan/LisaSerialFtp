// Lisa Serial FTP -- the protocol core of the web-based server.
//
// A JavaScript port of the parts of src/server/LisaSerialFtpServer.py that
// the prototype needs: HELLO, DEBUG (the client sends it right after
// HELLO), QUIT, GET (binary and text), SUM (get -c), LIST (list), PUT,
// DELETE, BAUD, CAL (timeout-factor auto) and the bench's MARK / ECHO /
// FRAMES: all the commands the client sends.  Anything else is answered
// "ERR 99 unknown command".
// Names, constants and behaviour follow the Python server, so its
// docstrings (and TECHNICAL.md) explain this code too; keep the two in
// step.
//
// It knows nothing about the browser: the serial port and the file source
// are passed in (see serial.js and sources.js), so the same code runs in
// Node with a fake port (tests/test_protocol.mjs).
//
//   port:   { baud, read(n, timeoutMs) -> Promise<Uint8Array>  (rejects
//             with TimeoutError if fewer than n bytes came),
//             write(bytes) -> Promise, flushInput(),
//             setBaud(rate) -> Promise  (BAUD: switch the open port) }
//   source: { label, readFile(name) -> Promise<Uint8Array>,
//             list(dir) -> Promise<[{ name, size, isDir }]>  (dir '' is
//             the top),
//             writable  (false: PUT and DELETE get ERR 20),
//             openWrite(name) -> Promise<{ write(bytes), commit(), abort() }>
//               (nothing changes under name until commit()),
//             deleteFile(name) -> Promise;
//             all throw ProtocolError for a bad or missing name) }
//   log:    { say(text), debug(text), progress(ok, nbytes), progressStart(total) }

export const VERSION = "v1.0";
export const PROTOCOL = "LISA-SERIAL-FTP/" + VERSION;
export const SEQ_MOD = 65536;
export const MAX_LINE = 255;
export const MAX_IN_LINE = 1024;
export const CHUNK = 1016;
export const MAX_FILE = 64 * 1024 * 1024;
export const TEXT_PAGE = 1024;
export const MAX_RETRIES = 10;
export const LIST_PAGE = 384;   // LIST: bytes per page, MORE line included
export const LIST_TRIES = 3;    // MORE is resent this often if no NEXT comes

// timeouts, in milliseconds (the Python server's are in seconds)
export const T_FRAME = 15000;
export const T_REPLY = 10000;
export const LINE_GAP = 5000;
export const T_COMMAND = 15000;     // the Python server's --timeout default
export const T_BLOCK = 1000;        // mid-chunk bytes (receiver)
export const T_QUIET = 300;         // discard-until-quiet (receiver)
export const CAL_MS = 2000;         // CAL reply delay (CALSECS in the client)
export const BAUD_TRIAL = 10000;    // BAUD: the client has this long to send a
                                    // command at the new rate

// The rates the Lisa can use too (57600 is the most its driver can set).
export const BAUDS = [300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600];

// every line a client sends: a command, or a protocol reply.  Any other
// line is garbled, or sent at another rate (see BAUD).
const KNOWN = new Set(["HELLO", "LIST", "GET", "PUT", "SUM", "DELETE", "BAUD",
  "DEBUG", "CAL", "QUIT", "MARK", "ECHO", "FRAMES",
  "NEXT", "DONE", "OK", "BAD", "FAIL", "GO", "A", "N"]);

export class TimeoutError extends Error {
  constructor(msg = "timeout") { super(msg); this.name = "TimeoutError"; }
}

export class ProtocolError extends Error {
  constructor(code, text) { super(text); this.code = code; this.text = text; }
}

class PeerRestarted extends Error {}
class ClientQuit extends Error {}

// -------------------------------------------------------------------------
// CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF), as crc16() in the server
// -------------------------------------------------------------------------

const CRC_TABLE = (() => {
  const t = new Uint16Array(256);
  for (let i = 0; i < 256; i++) {
    let crc = i << 8;
    for (let k = 0; k < 8; k++)
      crc = (crc & 0x8000) ? ((crc << 1) ^ 0x1021) : (crc << 1);
    t[i] = crc & 0xFFFF;
  }
  return t;
})();

export function crc16(data, crc = 0xFFFF) {
  for (let i = 0; i < data.length; i++)
    crc = ((crc << 8) & 0xFFFF) ^ CRC_TABLE[((crc >> 8) ^ data[i]) & 0xFF];
  return crc;
}

// -------------------------------------------------------------------------
// Lisa text files (GET T): lisa_text() and lisa_pages() of the server
// -------------------------------------------------------------------------

// The lines of a text file as the Lisa stores them: LF, CRLF and a lone CR
// each end a line; a line is split after every 255 characters; a last line
// without a line ending gets one.  Returns an array of Uint8Arrays.
export function lisaLines(data) {
  const lines = [];
  let line = [];
  let pendCR = false;
  const end = () => { lines.push(Uint8Array.from(line)); line = []; };
  for (const b of data) {
    if (b === 10) { end(); pendCR = false; continue; }
    if (pendCR) end();
    if (b === 13) { pendCR = true; continue; }
    pendCR = false;
    if (line.length >= 255) end();
    line.push(b);
  }
  if (pendCR || line.length) end();
  return lines;
}

// lisa_text(): the bytes a text file comes back as after a round trip
// through the Lisa (each of lisaLines ending in LF).  SUM T checks these.
export function lisaText(data) {
  const lines = lisaLines(data);
  const out = new Uint8Array(lines.reduce((n, l) => n + l.length + 1, 0));
  let k = 0;
  for (const l of lines) { out.set(l, k); k += l.length; out[k++] = 10; }
  return out;
}

// The text pages of the Lisa .TEXT file the client writes for data (without
// the header page, which the client makes): each line ends in CR, 2..223
// leading spaces become DLE + CHR(32 + n), a line that does not fit in the
// page (1023 bytes) starts a new one, pages are padded with NULs to 1024.
export function lisaPages(data) {
  const out = [];
  let page = [];
  const flush = () => {
    while (page.length < TEXT_PAGE) page.push(0);
    out.push(...page);
    page = [];
  };
  for (const line of lisaLines(data)) {
    const n = line.length;
    let sp = 0;
    while (sp < n && line[sp] === 32) sp++;
    sp = Math.min(sp, 223);
    if (sp < 2) sp = 0;
    const need = sp ? 2 + n - sp + 1 : n + 1;
    if (page.length && page.length + need > TEXT_PAGE - 1) flush();
    if (sp) page.push(16, 32 + sp);
    for (let i = sp; i < n; i++) page.push(line[i]);
    page.push(13);
  }
  if (page.length) flush();
  return Uint8Array.from(out);
}

// -------------------------------------------------------------------------
// Lines on the wire: Mac Roman, the Lisa's character set
// -------------------------------------------------------------------------

const MAC_DECODER = new TextDecoder("macintosh");
const MAC_ENCODE = new Map();      // character -> Mac Roman byte (>= 0x80)
{
  const hi = new Uint8Array(128).map((_, i) => 0x80 + i);
  [...MAC_DECODER.decode(hi)].forEach((c, i) => MAC_ENCODE.set(c, 0x80 + i));
}

export function fromWire(bytes) {
  return MAC_DECODER.decode(bytes);
}

// a character the Lisa does not have becomes '?'
export function toWire(text) {
  const out = [];
  for (const c of text) {
    const code = c.codePointAt(0);
    if (code < 0x80) out.push(code);
    else out.push(MAC_ENCODE.get(c) ?? 0x3F);
  }
  return Uint8Array.from(out);
}

// name_to_wire(): a file name as the Lisa sees it (LIST): letters, digits
// and ._-~ as they are; a character the Lisa has (Mac Roman 0x80..0xFF,
// e.g. e-acute) as itself; everything else (spaces, other punctuation,
// characters the Lisa lacks) percent-encoded as UTF-8 bytes, e.g. %20.
// nameFromWire() reverses it, so any name LIST shows can be typed back.
const SAFE_CHARS = /^[A-Za-z0-9._~-]$/;
const UTF8 = new TextEncoder();

export function nameToWire(name) {
  let out = "";
  for (const c of name) {
    if (SAFE_CHARS.test(c) || (c >= "\x80" && c !== "\xa0" && MAC_ENCODE.has(c))) {
      out += c;
      continue;
    }
    for (const b of UTF8.encode(c))
      out += "%" + b.toString(16).toUpperCase().padStart(2, "0");
  }
  return out;
}

// name_from_wire(): %XX escapes are UTF-8 bytes.  (The Python server keeps
// bytes that are not UTF-8 with surrogateescape; a browser cannot name
// such files anyway, so here a bad escape is just a bad name.)
export function nameFromWire(s) {
  try {
    return decodeURIComponent(s);
  } catch {
    throw new ProtocolError(2, "bad name");
  }
}

export function seqBehind(r, s) {
  const d = ((s - r) % SEQ_MOD + SEQ_MOD) % SEQ_MOD;
  return d >= 1 && d <= 32767;
}

export function parseReply(line) {
  if (line) {
    const p = line.split(/\s+/).filter(Boolean);
    if (p.length === 2 && (p[0] === "A" || p[0] === "N") && /^\d+$/.test(p[1])
        && Number(p[1]) < SEQ_MOD)
      return [p[0], Number(p[1])];
  }
  return [null, null];
}

// -------------------------------------------------------------------------
// The server
// -------------------------------------------------------------------------

export class LisaFtpServer {
  constructor(port, source, log) {
    this.port = port;
    this.source = source;
    this.log = log;
    this.debugDefault = false; // debug output at startup; HELLO goes back to it
    this.debugOn = false;
    this.stopped = false;
    this.pending = null;       // a line read while waiting for NEXT, for run()
    this.markAt = null;        // MARK: time of the previous MARK
    this.timing = {};          // per-chunk timing: key -> [n, total, min, max] ms
    this.tReply = null;        // sendChunk: when the last ACK came
    this.startBaud = port.baud; // QUIT, and a garbled line, go back to it
    this.confirmUntil = null;  // BAUD: until when the new rate is on trial
    this.baudOld = null;       // BAUD: the rate to go back to if it fails
    this.onBaud = null;        // called with the new rate after a switch
    this.baudTrial = BAUD_TRIAL;
    this.page = 0;             // LIST: bytes sent since the last MORE
    this.doneLine = null;      // sendChunk: a DONE taken as the last ACK
    this.clientFailed = false; // sendChunk: the client sent FAIL
    this.sendWhy = "";
  }

  dlog(text) { if (this.debugOn) this.log.debug(text); }

  // -- lines ---------------------------------------------------------------

  async sendLine(line) {
    if (line.length > MAX_LINE) line = line.slice(0, MAX_LINE);
    await this.port.write(concatBytes(toWire(line), CRLF));
    this.dlog("sent: " + line);
  }

  // One CRLF-terminated line.  null: no byte within timeoutMs; "": a
  // partial line was dropped, or the line was too long.
  async readLine(timeoutMs, quiet = false) {
    const buf = [];
    for (;;) {
      let b;
      try {
        b = (await this.port.read(1, timeoutMs))[0];
      } catch (e) {
        if (!(e instanceof TimeoutError)) throw e;
        if (buf.length) { this.dlog("dropping partial line"); return ""; }
        return null;
      }
      if (b === 10) {
        if (buf.length && buf[buf.length - 1] === 13) buf.pop();
        if (buf.length > MAX_IN_LINE) { this.dlog("line too long, dropped"); return ""; }
        const line = fromWire(Uint8Array.from(buf));
        if (!quiet) this.dlog("received: " + line);
        return line;
      }
      buf.push(b);
    }
  }

  async sendBanner() {
    const head = `Hi from Lisa Serial FTP Server version ${PROTOCOL} : `;
    const shown = [...this.source.label].map(c => (c >= " " && c <= "~") ? c : "?").join("");
    let home = `home_dir=${shown} `;
    const tail = `chunk_size=${CHUNK}`;
    const room = MAX_LINE - head.length - tail.length;
    if (home.length > room) home = home.slice(0, room);
    await this.sendLine(head + home + tail);
  }

  async abort(text) {
    this.log.say("  FAILED: " + text);
    await this.sendLine("FAIL " + text);
    for (;;) {
      const line = await this.readLine(T_COMMAND);
      if (line === null || line === "" || line === "OK") return;
      if (line === "HELLO") { await this.peerRestarted(); return; }
    }
  }

  async peerRestarted() {
    this.log.say("  FAILED: the client restarted (HELLO) -- transfer abandoned");
    this.debugOn = this.debugDefault;
    this.port.flushInput();
    await this.sendBanner();
  }

  // -- commands ------------------------------------------------------------

  async cmdHello() {
    this.debugOn = this.debugDefault;     // a new session
    await this.sendBanner();
    this.log.say("  ok: banner sent");
  }

  async cmdDebug(args) {
    const a = (args[0] || "").toUpperCase();
    if (a !== "ON" && a !== "OFF") throw new ProtocolError(1, "usage: DEBUG ON|OFF");
    this.debugOn = a === "ON";
    await this.sendLine("OK debug " + a.toLowerCase());
    this.log.say("  ok: debug output " + a.toLowerCase());
  }

  async cmdQuit() {
    this.log.say("  ok: the client quit");
    throw new ClientQuit();
  }

  // SUM name T|B: size, CRC-16 and byte sum of a file, for get -c / put -c
  // (T: of the file as Lisa text, see lisaText)
  async cmdSum(args) {
    if (args.length !== 2 || !["T", "B"].includes(args[1].toUpperCase()))
      throw new ProtocolError(1, "usage: SUM name T|B");
    const name = this.remoteName(args[0]);
    let data = await this.source.readFile(name);
    if (data.length > MAX_FILE) throw new ProtocolError(4, "file too big");
    const text = args[1].toUpperCase() === "T";
    if (text) data = lisaText(data);
    const crc = crc16(data);
    let bsum = 0;
    for (const b of data) bsum = (bsum + b) & 0xFFFF;
    await this.sendLine(`OK ${data.length} ${hex4(crc)} ${hex4(bsum)}`);
    this.log.say(`  ok: ${name}${text ? " (as Lisa text)" : ""}: ${data.length} bytes,`
      + ` crc ${hex4(crc)}, sum ${hex4(bsum)}`);
  }

  // LIST [dir]: 'OK', then 'D|F <size> <name>' per entry, sorted, then
  // 'DONE'; in pages of at most LIST_PAGE bytes, each but the last ending
  // with MORE (the client answers NEXT): the Lisa's serial input has no
  // flow control and a 512-byte buffer (see LIST_PAGE in the Python server).
  async cmdList(args) {
    const dir = args.length ? this.remoteName(args[0]) : "";
    const entries = await this.source.list(dir);
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    this.page = 0;
    if (!(await this.listLine("OK"))) return;
    for (const e of entries) {
      if (!(await this.listLine(`${e.isDir ? "D" : "F"} ${e.size} ${nameToWire(e.name)}`)))
        return;
    }
    if (!(await this.listLine("DONE"))) return;
    const n = entries.length;
    this.log.say(`  ok: ${n} ${n === 1 ? "entry" : "entries"} listed`);
  }

  // sendLine for LIST, in pages: if this line would make the page longer
  // than LIST_PAGE bytes (with room for MORE), end the page with MORE and
  // wait for NEXT.  FALSE if the client did not ask for more.
  async listLine(line) {
    const n = Math.min(line.length, MAX_LINE) + 2;
    if (this.page && this.page + n + "MORE\r\n".length > LIST_PAGE) {
      if (!(await this.more())) return false;
      this.page = 0;
    }
    this.page += n;
    await this.sendLine(line);
    return true;
  }

  // MORE, then wait for NEXT; MORE is resent if no reply comes.  Any other
  // line means the client gave up the listing (Apple-period, or it
  // restarted): it is kept, and run() handles it as the next command.
  async more() {
    for (let i = 0; i < LIST_TRIES; i++) {
      await this.sendLine("MORE");
      const line = await this.readLine(T_COMMAND);
      if (!line) {
        this.dlog(`no NEXT within ${T_COMMAND / 1000} s, sending MORE again`);
        continue;
      }
      if (line.toUpperCase() === "NEXT") return true;
      this.log.say(`  FAILED: the client stopped the listing (${JSON.stringify(line)})`);
      this.pending = line;
      return false;
    }
    this.log.say("  FAILED: no NEXT from the client -- listing abandoned");
    return false;
  }

  // BAUD <rate>: answer 'OK <rate>' at the old rate, let it leave, and
  // switch.  The new rate is then on trial: the client's next command (its
  // HELLO) must arrive at it, as a valid line, within BAUD_TRIAL; silence,
  // or a garbled line (what bytes sent at another rate look like), switches
  // back to the old rate (run).  Web Serial cannot change the rate of an
  // open port, so the switch closes and reopens it (port.setBaud).
  async cmdBaud(args) {
    if (args.length !== 1 || !/^\d+$/.test(args[0])) throw new ProtocolError(1, "usage: BAUD rate");
    const nb = Number(args[0]);
    if (!BAUDS.includes(nb))
      throw new ProtocolError(21, `unsupported baud rate ${nb} (${BAUDS.join(" ")})`);
    const old = this.port.baud;
    await this.sendLine("OK " + nb);
    // the reply must be out of the adapter too before the switch: write()
    // has handed it to the port; then about its own length again at the
    // old rate for the adapter's FIFO, as the Python server waits
    await sleep(100 + 12 * 10000 / old);
    if (nb === old) {
      this.log.say(`  ok: the baud rate stays ${nb}`);
      return;
    }
    await this.setBaud(nb);
    this.baudOld = old;
    this.confirmUntil = performance.now() + this.baudTrial;
    this.log.say(`  ok: now at ${nb} baud (was ${old}); waiting for the client at it`);
  }

  async setBaud(rate) {
    await this.port.setBaud(rate);
    if (this.onBaud) this.onBaud(rate);
  }

  // Back to the rate `to` (a failed BAUD, a garbled line, QUIT).
  async revertBaud(to, why) {
    this.confirmUntil = null;
    if (this.port.baud !== to) {
      const was = this.port.baud;
      await this.setBaud(to);
      this.log.say(`  baud: back to ${to} (from ${was}): ${why}`);
    }
  }

  // CAL: answer exactly CAL_MS of real time after the request came.  The
  // Lisa counts its 1-second input timeouts meanwhile: in LisaEm at a high
  // throttle that is how the client learns how much faster emulated time
  // runs (timeout-factor auto).  A real Lisa counts ~2.
  async cmdCal() {
    await sleep(CAL_MS);
    await this.sendLine(`CAL ${CAL_MS}`);
    this.log.say(`  ok: answered after ${CAL_MS / 1000} s`);
  }

  // MARK <label>: answer 'OK <ms since the previous MARK>' (the client's
  // bench times its own work with it; the Lisa has no fine clock).
  async cmdMark(args) {
    const now = performance.now();
    const ms = this.markAt === null ? 0 : Math.round(now - this.markAt);
    this.markAt = now;
    await this.sendLine("OK " + ms);
    const label = args.join(" ");
    if (label && label !== "-") this.log.say(`  bench: ${label}: ${ms} ms`);
  }

  // ECHO <n>: n lines 'A 12345', back to back (the bench times reading
  // short lines with them).
  async cmdEcho(args) {
    const n = /^\d+$/.test(args[0] || "") ? Number(args[0]) : 0;
    if (n < 1 || n > 100) throw new ProtocolError(1, "usage: ECHO n (1..100)");
    await this.port.write(toWire("A 12345\r\n".repeat(n)));
    this.dlog(`sent ${n} lines 'A 12345'`);
  }

  // FRAMES <n>: n chunks of CHUNK made-up bytes, then the empty end-of-file
  // chunk, sent as a GET sends them; nothing before or after (the bench
  // times receiving chunks with it).
  async cmdFrames(args) {
    const n = /^\d+$/.test(args[0] || "") ? Number(args[0]) : 0;
    if (n < 1 || n > 400) throw new ProtocolError(1, "usage: FRAMES n (1..400)");
    const payload = new Uint8Array(CHUNK).map((_, i) => (i * 37) & 0xFF);
    this.log.progressStart(n * CHUNK);
    this.timingReset();
    for (let seq = 0; seq <= n; seq++) {
      if (!(await this.sendChunk(seq, seq < n ? payload : new Uint8Array(0)))) {
        this.log.say("  FAILED: frames: " + this.sendWhy);
        return;
      }
    }
    this.log.say(`  ok: ${n} chunks sent`);
    this.sayTiming();
  }

  // -- per-chunk timing (after a GET or FRAMES), as the Python server's:
  // "write": the frame handed to the serial port (Web Serial cannot wait
  // until it has left the PC, as the Python server's drain() does, so this
  // is shorter there, and "lisa" longer); "lisa": from then until its ACK
  // is here; "server": this page, before the next frame.  Clean chunks
  // only: not retries, not the end-of-file chunk.

  timingReset() { this.timing = {}; this.tReply = null; }

  timingAdd(key, ms) {
    const t = this.timing[key];
    this.timing[key] = t ? [t[0] + 1, t[1] + ms, Math.min(t[2], ms), Math.max(t[3], ms)]
      : [1, ms, ms, ms];
    return ` ${key} ${Math.round(ms)} ms`;
  }

  sayTiming() {
    const out = [];
    let count = 0;
    for (const [key, label] of [["write", "the frame handed to the port"],
      ["lisa", "the Lisa until its ACK"], ["server", "this page"]]) {
      const t = this.timing[key];
      if (!t) continue;
      count = Math.max(count, t[0]);
      out.push(`${label} ${Math.round(t[1] / t[0])} ms (${Math.round(t[2])}-${Math.round(t[3])})`);
    }
    if (out.length) this.log.say(`  per chunk (${count} chunks): ${out.join(", ")}`);
  }

  // DELETE <name>: delete one file (the client has asked the user first).
  // Never a folder.
  async cmdDelete(args) {
    if (!this.source.writable) throw new ProtocolError(20, "server is read-only");
    if (args.length !== 1) throw new ProtocolError(1, "usage: DELETE name");
    const name = this.remoteName(args[0]);
    await this.source.deleteFile(name);
    await this.sendLine("OK deleted");
    this.log.say("  ok: deleted " + name);
  }

  // PUT <name> <size>: receive the file.  It goes into the source's
  // openWrite() (in the browser, createWritable()'s hidden swap file), and
  // replaces <name> only once the client has verified it: until then an
  // older file of that name is untouched, and a failed upload leaves
  // nothing behind -- as the Python server's '<name>.part'.  Opened before
  // the OK, so a name the browser refuses is an error reply.
  async cmdPut(args) {
    if (!this.source.writable) throw new ProtocolError(20, "server is read-only");
    if (args.length < 2 || !/^\d+$/.test(args[1]))
      throw new ProtocolError(1, "usage: PUT name size");
    const name = this.remoteName(args[0]);
    const size = Number(args[1]);
    if (size > MAX_FILE) throw new ProtocolError(4, "file too big");
    const out = await this.source.openWrite(name);
    let kept = false;
    try {
      kept = await this.putInto(out, name, size);
    } finally {
      if (!kept) {
        try { await out.abort(); } catch {}
        this.dlog("partial upload discarded");
      }
    }
  }

  // PUT, once out is open: receive the chunks into it, check them with the
  // client, and on success commit it.  TRUE if the file was kept.
  async putInto(out, name, size) {
    this.log.progressStart(size);
    const t0 = performance.now();
    await this.sendLine("OK");
    let seq = 0, crc = 0xFFFF, total = 0, fails = 0, giveup = false;
    try {
      for (;;) {
        const got = await this.recvChunk(seq);
        if (got.kind === "fail") {
          this.log.say("  FAILED: the client aborted: " + got.line);
          await this.port.write(toWire("OK\r\n"));
          return false;
        }
        if (got.kind === "dup") {
          this.dlog(`chunk ${got.seq}: received again (our ACK was lost) -> ACK again`);
          await this.ack(got.seq);
        } else if (got.kind === "bad") {
          this.dlog(`chunk ${seq}: ${got.why}`);
          this.log.progress(false, 0);
          if (got.discard) await this.discardUntilQuiet();
        }
        if (got.kind !== "ok") {
          if (++fails >= MAX_RETRIES) { giveup = true; break; }
          if (got.kind === "bad") await this.nak(seq);   // the client sends it again
          continue;
        }
        const payload = got.payload;
        await this.ack(seq);
        this.dlog(payload.length
          ? `chunk ${seq}: ${payload.length} bytes received, CRC ok -> ACK`
          : `chunk ${seq}: end-of-file chunk received -> ACK`);
        this.log.progress(true, payload.length);
        fails = 0;
        if (!payload.length) break;
        await out.write(payload);
        crc = crc16(payload, crc);
        total += payload.length;
        seq++;
      }
    } catch (e) {
      if (e instanceof PeerRestarted) { await this.peerRestarted(); return false; }
      throw e;
    }
    if (giveup) { await this.abort("too many chunk errors"); return false; }
    let done = `DONE ${total} ${hex4(crc)}`;
    if (total !== size) {
      // the CRC only covers what arrived: a short file must not pass
      this.log.say(`  FAILED: the client announced ${size} bytes, ${total} arrived`);
      done = `ERR 5 size mismatch: announced ${size}, received ${total}`;
    }
    await this.sendLine(done);
    // the client answers OK/BAD with ITS numbers: keep the file only on OK
    // with the numbers we received
    const verdict = await this.readVerdict(seq, done);
    const p = (verdict || "").split(/\s+/).filter(Boolean);
    const good = p.length === 3 && p[0] === "OK" && /^\d+$/.test(p[1])
      && Number(p[1]) === total && p[2].toLowerCase() === hex4(crc);
    if (good && total === size) {
      try {
        await out.commit();
      } catch (e) {
        this.log.say(`  FAILED: cannot save ${name}: ${e.message}`);
        return false;
      }
      const secs = (performance.now() - t0) / 1000;
      this.log.say(`  ok: received ${total} bytes into ${name}, verified (crc ${hex4(crc)})`
        + (secs > 0 ? `, ${Math.round(total / secs)} bytes/s` : ""));
      return true;
    }
    if (good) this.log.say("  upload discarded (size mismatch)");
    else {
      this.log.say(`  FAILED: verify: the client said ${JSON.stringify(verdict)}, we received`
        + ` ${total} ${hex4(crc)}; upload discarded`);
      if (verdict === "HELLO") await this.peerRestarted();
    }
    return false;
  }

  // Timeout for n bytes of a frame that has started: T_BLOCK plus twice
  // their time on the wire, or at the Lisa's own sending speed (~2700
  // bytes/s) if that is slower.
  tBlock(n) {
    const rate = Math.min((this.port.baud || 57600) / 10, 2700);
    return T_BLOCK + 2000 * n / rate;
  }

  async readOr(n, timeoutMs) {
    try {
      return await this.port.read(n, timeoutMs);
    } catch (e) {
      if (e instanceof TimeoutError) return null;
      throw e;
    }
  }

  // PUT: read one framed chunk, the one numbered seq, and check it:
  //   { kind: "ok", payload }        chunk seq, CRC good (an empty payload
  //                                  is the end-of-file chunk)
  //   { kind: "dup", seq }           an earlier chunk again
  //   { kind: "bad", why, discard }  no good chunk (discard: bytes of a bad
  //                                  frame may still be arriving)
  //   { kind: "fail", line }         the client's FAIL line
  // Throws PeerRestarted on a HELLO where a chunk should start.
  async recvChunk(seq) {
    const first = await this.readOr(1, T_FRAME);
    if (!first) return { kind: "bad", why: `nothing arrived within ${T_FRAME / 1000} s -> NAK`, discard: false };
    const b = first[0];
    const outOfStep = { kind: "bad", discard: true,
      why: `byte 0x${b.toString(16).padStart(2, "0")} where a chunk should start (out of step) -> discard, NAK` };
    if (b === 0x48) {                                  // 'H': HELLO?
      const rest = await this.readOr(4, T_BLOCK);
      if (rest && fromWire(rest) === "ELLO") throw new PeerRestarted();
      return outOfStep;
    }
    if (b === 0x46) {                                  // 'F': FAIL?
      const line = "F" + await this.readRestOfLine();
      return line.startsWith("FAIL") ? { kind: "fail", line } : outOfStep;
    }
    if (b !== 0x54) return outOfStep;                  // not 'T'
    const head = await this.readOr(4, T_BLOCK);
    if (!head) return { kind: "bad", why: "incomplete (bytes lost) -> discard, NAK", discard: true };
    const gotSeq = (head[0] << 8) | head[1];
    const gotLen = (head[2] << 8) | head[3];
    if (gotLen > CHUNK)
      return { kind: "bad", why: `impossible length ${gotLen} (out of step) -> discard, NAK`, discard: true };
    const payload = gotLen ? await this.readOr(gotLen, this.tBlock(gotLen)) : new Uint8Array(0);
    const crcBytes = payload && await this.readOr(2, T_BLOCK);
    if (!crcBytes) return { kind: "bad", why: "incomplete (bytes lost) -> discard, NAK", discard: true };
    const all = new Uint8Array(4 + gotLen);
    all.set(head);
    all.set(payload, 4);
    const gotCrc = (crcBytes[0] << 8) | crcBytes[1];
    const calc = crc16(all);
    const wireSeq = seq % SEQ_MOD;
    if (seqBehind(gotSeq, wireSeq)) return { kind: "dup", seq: gotSeq };
    let why;
    if (gotSeq !== wireSeq) why = `chunk ${gotSeq} arrived, expected ${seq}`;
    else if (gotCrc !== calc) why = `bad CRC (received ${hex4(gotCrc)}, computed ${hex4(calc)})`;
    else return { kind: "ok", payload };
    return { kind: "bad", why: `rejected, ${why} -> NAK`, discard: true };
  }

  // The rest of a CRLF line whose first byte was read: '' on a timeout.
  async readRestOfLine() {
    const buf = [];
    while (buf.length < MAX_IN_LINE) {
      const b = await this.readOr(1, T_BLOCK);
      if (!b || b[0] === 10) break;
      if (b[0] !== 13) buf.push(b[0]);
    }
    return fromWire(Uint8Array.from(buf));
  }

  // After a bad chunk: drop the bytes still arriving until the line is
  // quiet (the client waits for our reply, so they are the bad frame's).
  async discardUntilQuiet() {
    let n = 0;
    while (n < 2 * (CHUNK + 7) && await this.readOr(1, T_QUIET)) n++;
    if (n) this.dlog(`discarded ${n} stray byte(s)`);
  }

  async ack(seq) { await this.port.write(toWire(`A ${seq % SEQ_MOD}\r\n`)); }
  async nak(seq) { await this.port.write(toWire(`N ${seq % SEQ_MOD}\r\n`)); }

  // PUT: the client's OK/BAD after our DONE.  If it sends the end-of-file
  // chunk (eofseq) again instead -- our ACK and DONE were lost -- ACK it
  // and resend DONE rather than reading the frame as a garbled verdict.
  async readVerdict(eofseq, done) {
    for (let i = 0; i < MAX_RETRIES; i++) {
      const first = await this.readOr(1, T_COMMAND);
      if (!first) return null;
      if (first[0] !== 0x54) {
        const line = (fromWire(first) + await this.readRestOfLine()).replace(/\r$/, "");
        this.dlog("received: " + line);
        return line;
      }
      const rest = await this.readOr(6, T_BLOCK);      // seq, length 0, CRC
      if (rest && ((rest[0] << 8) | rest[1]) === eofseq % SEQ_MOD && rest[2] === 0 && rest[3] === 0
          && ((rest[4] << 8) | rest[5]) === crc16(rest.subarray(0, 4))) {
        this.dlog(`chunk ${eofseq}: end-of-file chunk received again -> ACK and DONE again`);
        await this.ack(eofseq);
        await this.sendLine(done);
      } else {
        this.dlog("unexpected bytes instead of the verdict -> discarded");
        await this.discardUntilQuiet();
      }
    }
    return null;
  }

  // a name from the wire, decoded and checked (the source checks '..')
  remoteName(s) {
    const name = nameFromWire(s.trim());
    if (!name) throw new ProtocolError(1, "empty name");
    return name;
  }

  async cmdGet(args) {
    if (!args.length) throw new ProtocolError(1, "missing filename in GET command");
    if (args.length > 2 || (args.length === 2 && args[1].toUpperCase() !== "T"))
      throw new ProtocolError(1, "usage: GET name [T]");
    const name = this.remoteName(args[0]);
    let data = await this.source.readFile(name);
    if (data.length > MAX_FILE) throw new ProtocolError(4, "file too big");
    const text = args.length === 2;
    if (text) data = lisaPages(data);
    const size = data.length;
    this.log.progressStart(size);
    this.timingReset();
    const t0 = performance.now();
    await this.sendLine("OK " + size);

    // the client's GO (or 'N 0': our OK's GO was lost; FAIL: it could not
    // create its file) -- see cmd_get in the Python server
    const go = await this.readLine(Math.max(T_COMMAND, T_FRAME + 5000), true);
    if (go === "HELLO") { await this.peerRestarted(); return; }
    if (go !== null && go.startsWith("FAIL")) {
      this.log.say("  FAILED: the client could not start the get: " + go);
      return;
    }
    if (go !== "GO" && go !== "N 0") {
      await this.abort(`no GO from the client (got ${JSON.stringify(go)})`);
      return;
    }
    this.dlog("received: " + go);

    let line;
    try {
      for (let seq = 0, off = 0; ; seq++, off += CHUNK) {
        const payload = data.subarray(off, Math.min(off + CHUNK, size));
        if (!(await this.sendChunk(seq, payload))) {
          if (!this.clientFailed) await this.abort(this.sendWhy);
          return;
        }
        if (!payload.length) break;       // the end-of-file chunk went through
      }
      line = this.doneLine ?? await this.readLine(T_COMMAND);
    } catch (e) {
      if (e instanceof PeerRestarted) { await this.peerRestarted(); return; }
      throw e;
    }
    if (line === "HELLO") { await this.peerRestarted(); return; }
    if (line === null || !line.startsWith("DONE")) {
      await this.abort("no DONE from client");
      return;
    }
    const mycrc = hex4(crc16(data));
    const p = line.split(/\s+/).filter(Boolean);
    const good = p.length === 3 && /^\d+$/.test(p[1]) && Number(p[1]) === size
      && p[2].toLowerCase() === mycrc;
    await this.sendLine(`${good ? "OK" : "BAD"} ${size} ${mycrc}`);
    if (good) {
      const secs = (performance.now() - t0) / 1000;
      const what = text ? ` (${size / TEXT_PAGE} Lisa text pages)` : "";
      this.log.say(`  ok: sent ${size} bytes of ${name}${what}, verified (crc ${mycrc})`
        + (secs > 0 ? ` in ${secs.toFixed(1)} s (${Math.round(size / secs)} bytes/s)` : ""));
      this.sayTiming();
    } else {
      this.log.say(`  FAILED: verify: the client got ${JSON.stringify(line)}, we sent ${size} ${mycrc}`);
    }
  }

  // One framed chunk ('T', seq, length, data, CRC), then wait for A / N.
  // TRUE when the client has it.  The same rules as send_chunk() in the
  // Python server: stale replies are skipped, 'N <seq+1>' and (for the
  // end-of-file chunk) DONE count as an ACK, anything else is a failed try.
  async sendChunk(seq, payload) {
    this.clientFailed = false;
    this.doneLine = null;
    this.sendWhy = `no ACK from the client after ${MAX_RETRIES} tries`;
    const wireSeq = seq % SEQ_MOD;
    const nextSeq = (seq + 1) % SEQ_MOD;
    const frame = new Uint8Array(5 + payload.length + 2);
    frame[0] = 0x54;                                   // 'T'
    frame[1] = wireSeq >> 8; frame[2] = wireSeq & 0xFF;
    frame[3] = payload.length >> 8; frame[4] = payload.length & 0xFF;
    frame.set(payload, 5);
    const crc = crc16(frame.subarray(1, 5 + payload.length));
    frame[5 + payload.length] = crc >> 8;
    frame[6 + payload.length] = crc & 0xFF;
    const what = payload.length ? `chunk ${seq}: ${payload.length} bytes sent`
      : `chunk ${seq}: end-of-file chunk sent`;

    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
      const t0 = performance.now();
      const prev = this.tReply;
      await this.port.write(frame);
      const t1 = performance.now();          // handed to the port
      const tries = attempt > 1 ? ` (try ${attempt})` : "";
      let line, kind, num, nstale = 0;
      for (;;) {
        line = await this.readLine(T_REPLY, true);
        [kind, num] = parseReply(line);
        if (kind === null || !seqBehind(num, wireSeq) || nstale >= 4) break;
        nstale++;
        this.dlog(`chunk ${seq}: stale reply ${JSON.stringify(line)} skipped`);
      }
      if (kind !== null && seqBehind(num, wireSeq)) kind = null;
      if (kind === "A" && num === wireSeq) {
        const t2 = performance.now();
        this.tReply = t2;
        let tms = "";
        if (attempt === 1 && payload.length)          // clean chunks only
          tms = this.timingAdd("write", t1 - t0) + this.timingAdd("lisa", t2 - t1)
            + (prev !== null ? this.timingAdd("server", t0 - prev) : "");
        this.dlog(`${what}${tries} -> accepted (ACK)${tms}`);
        this.log.progress(true, payload.length);
        return true;
      }
      if (!payload.length && line && line.startsWith("DONE")) {
        this.dlog(`${what}${tries} -> DONE instead of the ACK -> accepted`);
        this.log.progress(true, 0);
        this.doneLine = line;
        return true;
      }
      if (line === "HELLO") throw new PeerRestarted();
      if (line && line.startsWith("FAIL")) {
        this.dlog(`${what}${tries} -> the client gave up: ${line}`);
        await this.port.write(toWire("OK\r\n"));
        this.log.say("  FAILED: the client aborted: " + line);
        this.clientFailed = true;
        return false;
      }
      const nak = kind === "N" ? num : null;
      if (nak === nextSeq) {
        this.dlog(`${what}${tries} -> NAK for the next chunk (its ACK was lost) -> accepted`);
        this.log.progress(true, payload.length);
        return true;
      }
      if (nak === wireSeq) this.dlog(`${what}${tries} -> rejected by the client (NAK), resending`);
      else if (!line) this.dlog(`${what}${tries} -> no reply within ${T_REPLY / 1000} s, resending`);
      else this.dlog(`${what}${tries} -> unexpected reply ${JSON.stringify(line)}, resending`);
      this.log.progress(false, 0);
    }
    this.dlog(`chunk ${seq}: no ACK after ${MAX_RETRIES} tries`);
    return false;
  }

  // -- main loop -----------------------------------------------------------

  async handleLine(line) {
    const parts = line.split(/\s+/).filter(Boolean);
    const cmd = parts.length ? parts[0].toUpperCase() : "";
    if (cmd === "NEXT") {
      // a late NEXT for a listing that is over (its MORE was resent)
      this.dlog("late NEXT ignored");
      return;
    }                  // a late NEXT: nothing to answer
    // bench marks: cmdMark reports them, and their 'bench: ...' lines
    // belong together (no '> MARK', no empty line between them)
    if (cmd !== "MARK") this.log.say("> " + line);
    const serve = {
      HELLO: () => this.cmdHello(),
      DEBUG: a => this.cmdDebug(a),
      QUIT: () => this.cmdQuit(),
      GET: a => this.cmdGet(a),
      SUM: a => this.cmdSum(a),
      PUT: a => this.cmdPut(a),
      DELETE: a => this.cmdDelete(a),
      LIST: a => this.cmdList(a),
      CAL: () => this.cmdCal(),
      MARK: a => this.cmdMark(a),
      ECHO: a => this.cmdEcho(a),
      FRAMES: a => this.cmdFrames(a),
      BAUD: a => this.cmdBaud(a),
    }[cmd];
    try {
      if (serve) await serve(parts.slice(1));
      else {
        await this.sendLine("ERR 99 unknown command");
        this.log.say("  error: unknown command");
      }
    } catch (e) {
      if (e instanceof ProtocolError) {
        await this.sendLine(`ERR ${e.code} ${e.text}`);
        this.log.say("  error: " + e.text);
      } else if (e instanceof PeerRestarted) {
        await this.peerRestarted();
      } else if (e instanceof TimeoutError) {
        await this.abort("timeout");
      } else {
        throw e;
      }
    } finally {
      if (cmd !== "MARK") this.log.say("");   // the commands stand apart
    }
  }

  // The next command line, or null.  As handle_client() in the Python
  // server, it also runs the BAUD trial: a new rate on trial needs bytes at
  // least every second and a whole valid line before the deadline, else it
  // goes back; at a changed rate, a garbled line goes back to the starting
  // rate, where the client may be.
  async nextLine() {
    let line = this.pending;           // a command that ended a listing early
    this.pending = null;
    if (line === null && this.confirmUntil !== null) {
      const left = this.confirmUntil - performance.now();
      line = await this.readLine(Math.max(50, Math.min(1000, left)), true);
      if (line === null && performance.now() < this.confirmUntil) return null;
      if (!line) {
        await this.revertBaud(this.baudOld, line === null
          ? `no client at ${this.port.baud} baud`
          : "a garbled line: the client is at another rate");
        return null;
      }
    } else if (line === null) {
      line = await this.readLine(LINE_GAP, true);
    }
    if (line === null || line === "") {
      if (line === "" && this.port.baud !== this.startBaud)
        await this.revertBaud(this.startBaud, "a garbled line");
      return null;                     // idle, or a partial line
    }
    const parts = line.split(/\s+/).filter(Boolean);
    const cmd = parts.length ? parts[0].toUpperCase() : "";
    if (!KNOWN.has(cmd) || /[\x00-\x1f\x7f]/.test(line)) {
      // bytes sent at another rate: back to the old rate (on trial) or to
      // the startup rate; at the startup rate, it is answered ERR 99
      if (this.confirmUntil !== null) {
        await this.revertBaud(this.baudOld, "a garbled line: the client is at another rate");
        return null;
      }
      if (this.port.baud !== this.startBaud) {
        await this.revertBaud(this.startBaud, "a garbled line");
        return null;
      }
    } else if (this.confirmUntil !== null) {
      this.confirmUntil = null;
      this.log.say(`  baud: the client is there at ${this.port.baud} baud`);
    }
    return line;
  }

  // Serve until stop() is called or the port fails.
  async run() {
    this.log.say(`${PROTOCOL} listening at ${this.port.baud} baud, home_dir=${this.source.label}`);
    await this.sendBanner();
    while (!this.stopped) {
      const line = await this.nextLine();
      if (line === null) continue;
      try {
        await this.handleLine(line);
      } catch (e) {
        if (!(e instanceof ClientQuit)) throw e;
        // the next client starts at the startup rate
        await this.revertBaud(this.startBaud, "the client quit");
        this.log.say("waiting for a new client");
      }
    }
  }

  stop() { this.stopped = true; }
}

const CRLF = Uint8Array.of(13, 10);

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function concatBytes(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
}

function hex4(n) { return n.toString(16).padStart(4, "0"); }
