// Lisa Serial FTP -- the web page: wires the UI to the protocol core.

import { LisaFtpServer } from "./protocol.js";
import { WebSerialPort } from "./serial.js";
import { FolderSource, WebSource } from "./sources.js";

const $ = id => document.getElementById(id);
const logEl = $("log");
const progressEl = $("progress");

let folderHandle = null;
let server = null;
let port = null;

if (!("serial" in navigator)) {
  $("unsupported").style.display = "block";
  $("start").disabled = true;
}
if (!("showDirectoryPicker" in window)) {
  $("pickFolder").disabled = true;
  $("folderName").textContent = "not available in this browser";
}

// -- the log ----------------------------------------------------------------

const MAX_LOG = 200000;          // characters kept in the log
let marks = 0;                   // progress marks on the current log line
let bytesDone = 0, bytesTotal = 0;

function append(text) {
  const atEnd = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 4;
  logEl.textContent += text;
  if (logEl.textContent.length > MAX_LOG)
    logEl.textContent = logEl.textContent.slice(-MAX_LOG / 2);
  if (atEnd) logEl.scrollTop = logEl.scrollHeight;
}

function endMarks() {
  if (!marks) return;
  const pct = bytesTotal <= 0 ? 100 : Math.min(100, Math.floor(bytesDone * 100 / bytesTotal));
  append(` (${pct}% complete)\n`);
  marks = 0;
}

const log = {
  say(text) { endMarks(); append(text + "\n"); },
  debug(text) { endMarks(); append("  [debug] " + text + "\n"); },
  progressStart(total) {
    bytesDone = 0;
    bytesTotal = total;
    progressEl.hidden = false;
    progressEl.max = Math.max(total, 1);
    progressEl.value = 0;
  },
  // '.' = a chunk got through, 'x' = a failed try (as the Python server)
  progress(ok, n) {
    bytesDone += n;
    progressEl.value = bytesDone;
    if (server && server.debugOn) return;
    append(ok ? "." : "x");
    if (++marks >= 50) endMarks();
  },
};

function setStatus(text, cls = "") {
  $("status").textContent = text;
  $("status").className = cls;
}

// -- the controls -------------------------------------------------------------

$("pickFolder").addEventListener("click", async () => {
  try {
    // with uploads allowed, the browser asks for write access here (it
    // must be asked during a click; Start's click is for the port picker)
    folderHandle = await window.showDirectoryPicker({
      mode: $("allowWrite").checked ? "readwrite" : "read" });
    $("folderName").textContent = folderHandle.name;
    document.querySelector('input[name=src][value=folder]').checked = true;
  } catch (e) {
    if (e.name !== "AbortError") log.say("cannot open the folder: " + e.message);
  }
});

async function makeSource() {
  const kind = document.querySelector("input[name=src]:checked").value;
  if (kind === "folder") {
    if (!folderHandle) throw new Error("choose a folder first");
    let writable = false;
    if ($("allowWrite").checked) {
      writable = (await folderHandle.queryPermission({ mode: "readwrite" })) === "granted";
      if (!writable)
        log.say("no write access to the folder: put and delete are refused."
          + " Choose the folder again to allow them.");
    }
    return new FolderSource(folderHandle, writable);
  }
  const url = $("baseUrl").value.trim();
  if (!/^https:\/\//i.test(url)) throw new Error("the address must start with https://");
  return new WebSource(url);
}

$("start").addEventListener("click", async () => {
  let source;
  try {
    source = await makeSource();
  } catch (e) {
    log.say(e.message);
    return;
  }
  let sp;
  try {
    sp = await navigator.serial.requestPort();
  } catch (e) {
    if (e.name !== "NotFoundError") log.say("no serial port: " + e.message);
    return;                        // NotFoundError: the user cancelled the picker
  }
  const startBaud = Number($("baud").value);
  port = new WebSerialPort(sp, startBaud);
  try {
    await port.open();
  } catch (e) {
    log.say("cannot open the serial port: " + e.message
      + " (is another program, e.g. LisaSerialFtpServer.py, using it?)");
    port = null;
    return;
  }
  server = new LisaFtpServer(port, source, log);
  server.debugDefault = server.debugOn = $("debug").checked;
  const ro = source.writable ? "" : ", read only";
  // BAUD from the Lisa (and the switch back after a failed one, or QUIT):
  // show the rate the port is at now
  server.onBaud = rate => {
    $("baud").value = String(rate);
    setStatus(`running at ${rate} baud${ro}`, "ok");
  };
  $("start").disabled = true;
  $("stop").disabled = false;
  setStatus(`running at ${port.baud} baud${ro}`, "ok");
  $("baud").disabled = true;        // the Lisa's 'baud' command changes it now
  try {
    await server.run();
  } catch (e) {
    if (!server.stopped) {
      log.say("stopped: " + e.message);
      setStatus("stopped: " + e.message, "bad");
    }
  } finally {
    await port.close();
    port = null;
    server = null;
    $("start").disabled = !("serial" in navigator);
    $("stop").disabled = true;
    $("baud").disabled = false;
    $("baud").value = String(startBaud);
    progressEl.hidden = true;
  }
});

$("stop").addEventListener("click", async () => {
  if (!server) return;
  server.stop();
  setStatus("stopped");
  log.say("server stopped");
  await port?.close();   // ends the pending read, so run() returns
});

$("debug").addEventListener("change", () => {
  if (server) server.debugDefault = server.debugOn = $("debug").checked;
});
