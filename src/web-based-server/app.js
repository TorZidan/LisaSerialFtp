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
    return;
  }
  await sourceChanged();
});

// Uploads allowed after the folder was chosen read only: ask for write
// access now (the browser asks only during a click, as this one).
$("allowWrite").addEventListener("change", async () => {
  if ($("allowWrite").checked && folderHandle) {
    try {
      if ((await folderHandle.queryPermission({ mode: "readwrite" })) !== "granted")
        await folderHandle.requestPermission({ mode: "readwrite" });
    } catch (e) {
      log.say("cannot get write access to the folder: " + e.message);
    }
  }
  await sourceChanged();
});
for (const r of document.querySelectorAll("input[name=src]"))
  r.addEventListener("change", sourceChanged);
$("baseUrl").addEventListener("change", sourceChanged);   // Enter, or leaving the field

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

function showRunning(rate) {
  setStatus(`running at ${rate} baud${server.source.writable ? "" : ", read only"}`, "ok");
}

// A change in "1. Files to serve" while the server runs: serve the new
// files from the Lisa's next command on.  A transfer under way finishes
// with the old ones (it has its file open already).
async function sourceChanged() {
  if (!server) return;               // not running: Start reads the fields
  let source;
  try {
    source = await makeSource();
  } catch (e) {
    log.say(`${e.message}: still serving ${server.source.label}`);
    return;
  }
  if (!server) return;               // stopped meanwhile
  server.source = source;
  log.say(`now serving ${source.label}${source.writable ? "" : " (read only)"}`
    + "; the Lisa sees it from its next command on");
  showRunning(port.baud);
}

// The browser does not give a page the port's name (/dev/ttyUSB0, COM3):
// only the USB adapter's vendor and product ids, e.g. "USB 0403:6001 (FTDI)",
// which lsusb (Linux) and the Device Manager (Windows) show too.
const USB_VENDORS = { 0x0403: "FTDI", 0x067b: "Prolific", 0x10c4: "Silicon Labs",
  0x1a86: "WCH (CH340)", 0x2341: "Arduino", 0x0557: "ATEN" };

function portName(sp) {
  let info = {};
  try { info = sp.getInfo(); } catch {}
  if (info.usbVendorId === undefined) return "the serial port";
  const hex = n => n.toString(16).padStart(4, "0");
  const maker = USB_VENDORS[info.usbVendorId];
  return `the serial port USB ${hex(info.usbVendorId)}:${hex(info.usbProductId ?? 0)}`
    + (maker ? ` (${maker})` : "");
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
    // Chrome gives no reason ("Failed to open serial port."): the usual
    // one is that something else has the port open
    log.say(`cannot open ${portName(sp)}. Is it in use by another browser tab`
      + " running this page, or by LisaSerialFtpServer.py, or another program?"
      + " Stop that and click Start again. (The browser said: " + e.message + ")");
    port = null;
    return;
  }
  server = new LisaFtpServer(port, source, log);
  server.debugDefault = server.debugOn = $("debug").checked;
  // BAUD from the Lisa (and the switch back after a failed one, or QUIT):
  // show the rate the port is at now
  server.onBaud = rate => {
    $("baud").value = String(rate);
    showRunning(rate);
  };
  // the adapter unplugged, and plugged in again
  server.onPort = back => {
    if (back) showRunning(port.baud);
    else setStatus("the serial port is gone: plug the adapter in again", "bad");
  };
  $("start").disabled = true;
  $("stop").disabled = false;
  showRunning(port.baud);
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
