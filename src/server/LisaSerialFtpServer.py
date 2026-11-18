#!/usr/bin/env python3
"""
LisaSerialFtpServer.py -- serial file server for the Apple Lisa RS-232 driver.

Runs on a modern computer at the other end of a serial cable (a USB serial
adapter, a built-in serial port, or LisaEm's PseudoTTY) and serves one
folder (--home_dir) to LisaSerialFtpClient on the Lisa, which uses the
Lisa's Port B (RS232B) only.  It speaks the LISA-SERIAL-FTP/v1.0 protocol;
README.md has the full specification.

Needs Python 3.5 or newer.  Linux and macOS: the standard library only
(termios).  Windows: also pyserial for the serial port (pip install pyserial).

Usage:
    python3 LisaSerialFtpServer.py --port /dev/ttyUSB0 --home_dir ~/lisa-share

Without --port (or with --port auto) the server finds the port itself: the
only one there is, else a link to a pty first, then LisaEm's own pty, then
a USB serial adapter, then a built-in serial port (auto_port).  --port
auto-pty chooses among the PseudoTTYs only (LisaEm), --port auto-serial
among the USB adapters and built-in ports only (a real Lisa).

Commands (lines of at most 255 characters, Lisa Pascal's string[255],
ending in CRLF; the command word in any case):
  HELLO              the banner: "Hi from ... LISA-SERIAL-FTP/v1.0 ...
                     chunk_size=1016"
  LIST [dir]         "OK", one line per entry, "DONE"; in pages of at most
                     LIST_PAGE bytes, each but the last ending with MORE,
                     answered NEXT by the client (see LIST_PAGE)
  GET name [T]       "OK <size>", then (after the client's GO) the file in
                     chunks; T: as the pages of a Lisa .TEXT file (lisa_pages)
  PUT name size      "OK", then the client sends the file in chunks
  SUM name T|B       "OK <size> <crc16> <sum16>" of the file as it is (B) or
                     as the Lisa would send it back after a text get (T), for
                     the client's get -c / put -c
  DELETE name        "OK deleted"; ERR 11 no such file, 12 a folder (never
                     deleted), 13 the OS refused
  BAUD rate          "OK <rate>" at the old rate, then switch; the new rate
                     is kept only if a valid line arrives at it within
                     BAUD_TRIAL seconds, else back to the old one
  DEBUG ON|OFF       the server's detailed console output
  CAL                "CAL <ms>" after exactly CAL_SECONDS: the client's
                     timeout-factor auto
  QUIT               the client is leaving; back to the startup --baud
  hidden: MARK, ECHO, FRAMES (the client's 'bench' command)
PUT and DELETE are refused with --read-only.  Errors are "ERR <n> <text>".

Chunks (GET and PUT): 'T' + seq (2 bytes) + length (2) + data + CRC-16 (2),
big-endian, the CRC (CCITT-FALSE) over seq..data; at most CHUNK bytes of
data, so a frame fits the Lisa's 1024-byte input buffer.  The receiver
answers "A <seq>" (ACK) or "N <seq>" (NAK, send it again); a chunk with no
data ends the file.  Stop-and-wait: the sender waits for the reply to each
chunk, so the Lisa is never sent more than it can hold (no XON/XOFF, no
hardware handshake).  The receiver then sends "DONE <bytes> <crc>" for the
whole file, and the sender answers "OK ..." or "BAD ..." with its own
numbers; the file is kept only if they agree.  A PUT is received into
'<name>.part' and renamed to <name> only then, so an
older file of that name stays as it is until the new one is verified, and
a failed PUT leaves nothing behind.

Loss recovery: a lost byte leaves the receiver short and its read times
out; it throws away the rest of the frame and NAKs.  A lost reply makes the
sender time out and send the chunk again, which the receiver ACKs again.
Replies carry the chunk number, so a stale one is skipped.  After
MAX_RETRIES failed tries on one chunk either side gives up ("FAIL <why>",
answered "OK").  A HELLO in the middle of a transfer means the client
restarted: the transfer is abandoned (a partial PUT discarded) and the
new session gets a banner.

Names: '/' separates folders, always under --home_dir.  The wire is Mac
Roman; characters the Lisa lacks are percent-encoded (see name_to_wire /
name_from_wire).
"""

import argparse
import errno
import io
import os
import random
import select
import sys
import time
import urllib.parse

# Linux / macOS: the serial port through termios (the standard library).
# Windows has no termios: there the port goes through pyserial (pip
# install pyserial), the only case that needs a package (PySerialPort).
try:
    import fcntl
    import termios
    import tty
    POSIX = True
except ImportError:
    POSIX = False


# --------------------------------------------------------------------------
# Protocol constants
# --------------------------------------------------------------------------

VERSION = "v1.0"            # the server's version; the 1 is the MAJOR
                            # version: the client warns when its own major
                            # version differs (it may not be compatible)
PROTOCOL = "LISA-SERIAL-FTP/" + VERSION   # named in the banner.  Before v1.0:
                            # LISA-FTP/2..5 (2: numbered ACKs "A <seq>";
                            # 3: LIST in pages; 4: SUM; 5: 1016-byte chunks)

SEQ_MOD = 65536             # chunk sequence numbers are 16 bits and wrap


def seq_behind(r, s):
    """True if sequence number r is EARLIER than s (1..32767 behind,
    across the wrap): a duplicate chunk, or a stale reply."""
    return 1 <= (s - r) % SEQ_MOD <= 32767


def parse_reply(line):
    """'A <n>' / 'N <n>' -> ('A' or 'N', n); anything else -> (None, None)."""
    if line:
        parts = line.split()
        if (len(parts) == 2 and parts[0] in ("A", "N") and parts[1].isdigit()
                and int(parts[1]) < SEQ_MOD):
            return parts[0], int(parts[1])
    return None, None
MAX_LINE = 255          # Lisa Pascal's max string length (string[255])

# LIST paging.  A listing is the only reply longer than a chunk, and the
# Lisa's serial input has no flow control: its driver keeps what arrives in
# a 512-byte type-ahead buffer and drops the rest.  A real Lisa empties
# that buffer more slowly than even 9600 baud fills it, so a long listing
# sent in one go overflowed it.  (LisaEm never showed this: it holds every
# byte back until the Lisa has read the one before.)  So the listing goes
# out in pages of at most LIST_PAGE bytes, each but the last ending with a
# MORE line; the client answers NEXT once it has read the page, and only
# then is the next page sent -- stop-and-wait, like the file chunks.
LIST_PAGE = 384         # bytes per page, MORE line included; well below 512
LIST_TRIES = 3          # MORE is resent this often if no NEXT comes back

# Loss recovery (must match LisaSerialFtpClient.text).  The transfer is stop-and-wait,
# so while the sender waits for a reply the line is quiet: a byte lost in a
# chunk leaves the receiver short of bytes and its read times out.  The
# receiver then discards the partial block, waits for the line to go quiet,
# and NAKs; the sender retransmits the same chunk.  A lost reply makes the
# sender time out (T_REPLY) and retransmit anyway; the receiver recognises
# the repeated sequence number and re-ACKs it.  After MAX_RETRIES failed
# tries on one chunk either side gives up and aborts.
T_FRAME  = 15.0         # first byte of a chunk (receiver); > T_REPLY so a
                        # retransmit of a fully-lost chunk is caught
T_BLOCK  = 1.0          # mid-chunk bytes (receiver)
T_QUIET  = 0.3          # discard-until-quiet (receiver)
T_REPLY  = 10.0         # wait for the A/N reply (sender)
LINE_GAP = 5.0          # command line: the longest gap between two of its
                        # bytes.  The Lisa sends a line in one go, so bytes
                        # left without a line end for this long (noise, e.g.
                        # when a cable is plugged in) are dropped, and do not
                        # spoil the next command.  With no bytes at all the
                        # server just keeps waiting, for ever.
MAX_RETRIES = 10        # failed tries on one chunk before aborting.  Fixed:
                        # equals MAXRETRY in LisaSerialFtpClient.text (each side counts
                        # its own failures; the first to reach it aborts)
CHUNK = 1016            # bytes of file data per chunk.  Fixed: must equal
                        # CHUNK in LisaSerialFtpClient.text -- the Lisa rejects longer
                        # chunks (get) and we would reject its 1016-byte
                        # ones if ours were smaller (put).  A whole frame
                        # (1023 bytes) fits the Lisa's 1024-byte type-ahead
                        # buffer: at 57600 baud a real Lisa empties it more
                        # slowly than a frame fills it, and 1024-byte chunks
                        # (1031) worked at 19200 but never at 57600.
MAX_FILE = 64 * 1024 * 1024    # the largest file PUT accepts, and the largest
                        # GET T / SUM T read whole into memory
MAX_IN_LINE = 1024      # a longer line from the client is garbage: dropped
FRAME_HEAD = 5          # bytes before a chunk's data: 'T', seq (2), length (2)
DUMP_BYTES = False      # True: --debug also prints EVERY byte received, one per
                        # line, as <RX x> (printable ASCII as is, else \xNN).
                        # Very verbose -- a chunk is 1023 lines -- but it
                        # shows exactly what arrived, below the protocol
WRENCH_ODDS = 0.20      # --monkey_wrench: the chance a chunk gets a byte flipped
CAL_SECONDS = 2.0       # CAL reply delay, real seconds (must match CALSECS
                        # in LisaSerialFtpClient.text)

# The rates the Lisa can use too: 57600 is the most its serial driver can
# set (at 115200 its baud-rate generator time constant would be -1; see
# README, "Baud rate").
BAUDS = (300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600)

# --------------------------------------------------------------------------
# CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect, no final xor)
# --------------------------------------------------------------------------

def _make_table():
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
        table.append(crc & 0xFFFF)
    return table

_CRC_TABLE = _make_table()


def crc16(data, crc=0xFFFF):
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


# --------------------------------------------------------------------------
# Lisa text files (GET T, SUM T)
# --------------------------------------------------------------------------

def lisa_text(data):
    """The bytes a text file comes back as after a round trip through the
    Lisa: 'get' stores it as Lisa text lines (LisaSerialFtpClient.text,
    DoGet), and 'put' sends those lines back, each ending in LF (TextPass).
    Mirrors DoGet exactly: LF, CRLF and a lone CR each end a line; a line
    longer than 255 characters is split after every 255 (the Lisa's
    limit); a last line without a line ending gets one.  Used by SUM T, so
    that get -c / put -c see a file and its Lisa copy as the same even
    when the server's copy has CRLF or CR line endings."""
    out = bytearray()
    line = bytearray()
    pend_cr = False
    for b in data:
        if b == 10:                     # LF: the end of a line (or of a CRLF)
            out += line
            out.append(10)
            line.clear()
            pend_cr = False
            continue
        if pend_cr:                     # the CR before was a lone line ending
            out += line
            out.append(10)
            line.clear()
        if b == 13:
            pend_cr = True
            continue
        pend_cr = False
        if len(line) >= 255:            # split at the Lisa's line length
            out += line
            out.append(10)
            line.clear()
        line.append(b)
    if pend_cr or line:                 # the last line, without its ending
        out += line
        out.append(10)
    return bytes(out)


TEXT_PAGE = 1024        # a Lisa .TEXT file is 1024-byte pages (the first a header)


def lisa_pages(data):
    """A text GET (GET <name> T): the text pages of the Lisa .TEXT file the
    client writes for data, without its header page (the client makes
    that itself).  Built here so that the Lisa only stores what it gets,
    as fast as a binary get.  Mirrors the client's PutLine / TxtPage
    exactly (tests/test_text_pages.py checks it against them): the lines
    are those of lisa_text (LF, CRLF and a lone CR end a line; split at
    255 characters); each is stored with a CR, and 2..223 leading spaces
    as DLE + CHR(32 + n); a line that does not fit in the page (1023
    bytes, at least one NUL at the end) starts a new one; each page is
    padded with NULs to 1024 bytes.  No text: no pages."""
    out = bytearray()
    page = bytearray()
    for line in lisa_text(data).split(b"\n")[:-1]:
        n = len(line)
        sp = 0
        while sp < n and line[sp] == 32:
            sp += 1
        sp = min(sp, 223)
        if sp < 2:
            sp = 0
        need = (2 + n - sp + 1) if sp else n + 1
        if page and len(page) + need > TEXT_PAGE - 1:
            out += page + bytes(TEXT_PAGE - len(page))
            page = bytearray()
        if sp:
            page += bytes((16, 32 + sp))
        page += line[sp:]
        page.append(13)
    if page:
        out += page + bytes(TEXT_PAGE - len(page))
    return bytes(out)


# --------------------------------------------------------------------------
# Names on the wire
# --------------------------------------------------------------------------

# Lines on the wire are Mac Roman, the Lisa's character set (as in
# LisaFileSystemTool): a byte 0x8E is 'e-acute' on the Lisa and here.
WIRE = "mac_roman"

# the characters a name keeps as they are on the wire (see name_to_wire)
SAFE_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-~"
)


def name_to_wire(name):
    """A server file name as the Lisa sees it (LIST): letters, digits and
    ._-~ as they are; a character the Lisa has (Mac Roman 0x80..0xFF, e.g.
    e-acute) as itself, so the Lisa shows it; everything else (spaces,
    other ASCII punctuation, characters the Lisa lacks, bytes that are not
    UTF-8) percent-encoded as UTF-8 bytes, e.g. %20 or %E4%B8%AD.
    name_from_wire() reverses it, so any name LIST shows can be typed
    back."""
    out = []
    for c in name:
        if c in SAFE_CHARS:
            out.append(c)
            continue
        if c >= "\x80" and c != "\xa0" and not "\udc80" <= c <= "\udcff":
            try:
                c.encode(WIRE)
                out.append(c)
                continue
            except UnicodeEncodeError:
                pass
        out.extend("%{:02X}".format(b) for b in c.encode("utf-8", "surrogateescape"))
    return "".join(out)


def name_from_wire(s):
    """A name from the wire (already Mac Roman -> text): %XX escapes are
    UTF-8 bytes; bytes that are not UTF-8 stay as they are on disk
    (surrogateescape, as os.listdir gives them)."""
    return urllib.parse.unquote(s, errors="surrogateescape")


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class ProtocolError(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code
        self.text = text


class ClientGone(Exception):
    pass


class PeerRestarted(Exception):
    """The peer sent HELLO in the middle of a transfer: it crashed (or
    exited) and a new session is starting.  The in-flight transfer is
    abandoned and the new HELLO is answered with a fresh banner."""
    pass


# --------------------------------------------------------------------------
# Port discovery: --port auto, and when the --port device is not there, the
# list of the serial ports and PseudoTTYs that ARE there, to help pick one.
# --------------------------------------------------------------------------

def note(msg):
    """A message about the port, on the console with the server's own (stderr)."""
    print(msg, file=sys.stderr, flush=True)


USB_GLOBS = ("/dev/ttyUSB*", "/dev/ttyACM*",              # Linux
             "/dev/cu.usbserial*", "/dev/cu.usbmodem*",   # macOS
             "/dev/cu.PL2303*", "/dev/cu.SLAB*", "/dev/cu.wchusbserial*")


def _real_builtin(dev):
    """True if /dev/ttyS<n> (Linux) is a real built-in serial port: Linux
    creates ttyS0-ttyS31 whether or not there is hardware behind them,
    and /sys/class/tty/ttyS<n>/type is 0 (unknown) when there is none."""
    try:
        with open("/sys/class/tty/%s/type" % os.path.basename(dev)) as f:
            return f.read().strip() not in ("", "0")
    except OSError:
        return False


def _builtin_ports():
    """[(device, description)] of the real built-in serial ports (Linux:
    /dev/ttyS<n>, e.g. a motherboard's COM1 is /dev/ttyS0)."""
    import glob
    return sorted((dev, "built-in") for dev in glob.glob("/dev/ttyS*")
                  if _real_builtin(dev))


def _usb_ports():
    """[(device, description)] of the USB serial adapters: from pyserial if
    it is installed (the only way on Windows), else from /dev."""
    try:
        from serial.tools import list_ports
        found = []
        for p in list_ports.comports():
            # Linux lists every /dev/ttyS* too: the built-in ports are
            # found by _builtin_ports
            if p.device.startswith("/dev/ttyS"):
                continue
            desc = p.description if p.description not in (None, "", "n/a") else ""
            found.append((p.device, desc))
        return sorted(found)
    except ImportError:
        pass
    import glob
    names = {}                      # Linux: /dev/serial/by-id names the adapter
    for link in glob.glob("/dev/serial/by-id/*"):
        names[os.path.realpath(link)] = os.path.basename(link)
    found = set()
    for pattern in USB_GLOBS:
        for dev in glob.glob(pattern):
            found.add((dev, names.get(dev, "")))
    return sorted(found)


PTY_LINK_FOLDERS = ("/tmp", "/dev")   # where links to a pty are looked for
if os.environ.get("LISAFTP_LINK_DIRS"):   # tests only: other folders
    PTY_LINK_FOLDERS = tuple(os.environ["LISAFTP_LINK_DIRS"].split(os.pathsep))


def _is_pty(path):
    return path.startswith("/dev/pts/") or path.startswith("/dev/ttys")


def _lisaem_ptys():
    """[(device, description)] of the pty of each running LisaEm (Linux
    only): LisaEm holds the master end, /dev/ptmx, and
    /proc/<pid>/fdinfo/<fd> gives the pty's number (tty-index)."""
    found = set()
    for pid in (os.listdir("/proc") if os.path.isdir("/proc") else ()):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % pid) as f:
                comm = f.read().strip()
            if "lisaem" not in comm.lower():
                continue
            for fd in os.listdir("/proc/%s/fd" % pid):
                if os.readlink("/proc/%s/fd/%s" % (pid, fd)) != "/dev/ptmx":
                    continue
                with open("/proc/%s/fdinfo/%s" % (pid, fd)) as f:
                    for line in f:
                        if line.startswith("tty-index:"):
                            found.add(("/dev/pts/" + line.split()[1],
                                       "%s, pid %s" % (comm, pid)))
        except OSError:
            continue                # gone meanwhile, or not ours to read
    return sorted(found)


def _pty_links():
    """[(link, description)] of the symbolic links to a pty in
    PTY_LINK_FOLDERS (as made for LisaEm, e.g. /tmp/lisaem-port-b ->
    /dev/pts/3), whatever --port says.  Not /dev/stdin and the like, nor
    any link to the terminal the server itself runs in."""
    own = set()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            own.add(os.ttyname(stream.fileno()))
        except (OSError, AttributeError, ValueError):
            pass
    found = set()
    for d in PTY_LINK_FOLDERS:
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for e in entries:
            link = os.path.join(d, e)
            if e in ("stdin", "stdout", "stderr") or not os.path.islink(link):
                continue
            target = os.path.realpath(link)
            if _is_pty(target) and os.path.exists(target) and target not in own:
                found.add((link, "-> " + target))
    return sorted(found)


# --port auto picks the first port of the best kind there is: a link to a
# pty first (made on purpose for the emulator, e.g. /tmp/lisaem-port-b),
# then the pty of a running LisaEm, then a USB serial adapter, then a
# built-in serial port.
LINK, LISAEM, USB, BUILTIN = 0, 1, 2, 3


def port_entries():
    """[(kind, device, description)] of the ports there are now, best
    kind first (see LINK, LISAEM, USB, BUILTIN)."""
    found = []
    for kind, finder in ((LINK, _pty_links), (LISAEM, _lisaem_ptys),
                         (USB, _usb_ports), (BUILTIN, _builtin_ports)):
        found.extend((kind, dev, desc) for dev, desc in finder())
    return sorted(found)


def port_line(entry):
    kind, dev, desc = entry
    label = {USB: "USB serial:", BUILTIN: "serial:    "}.get(kind, "PseudoTTY: ")
    return "  %s %s%s" % (label, dev, "  (%s)" % desc if desc else "")


def find_ports():
    """The ports there are now, as printable lines (also used to see
    whether that changed)."""
    return [port_line(e) for e in port_entries()]


# --port auto, auto-pty, auto-serial: the kinds of port each may pick,
# and what to call them when there is none
AUTO_MODES = {
    "auto": ((LINK, LISAEM, USB, BUILTIN), "serial port or PseudoTTY"),
    "auto-pty": ((LINK, LISAEM), "PseudoTTY"),
    "auto-serial": ((USB, BUILTIN), "serial port"),
}


def auto_port(mode="auto", poll=1.0):
    """--port auto / auto-pty / auto-serial: the port to use, among the
    kinds MODE allows (auto: all; auto-pty: the PseudoTTYs, for LisaEm;
    auto-serial: the USB adapters and built-in ports, for a real Lisa).
    The only one if there is just one; else the first of the best kind (a
    link to a pty before LisaEm's own pty, before a USB adapter, before a
    built-in port).  None there yet: wait for one."""
    kinds, what = AUTO_MODES[mode]
    said = False
    while True:
        try:
            entries = [e for e in port_entries() if e[0] in kinds]
        except Exception as e:      # discovery failed: say so, keep trying
            entries = []
            if not said:
                note("--port %s: could not list the ports: %s" % (mode, e))
        if entries:
            chosen = entries[0]
            if len(entries) > 1:
                note("--port %s: ports found on this computer:" % mode)
                for e in entries:
                    note(port_line(e))
            note("--port %s: using %s (give --port to use another one)"
                 % (mode, chosen[1]))
            return chosen[1]
        if not said:
            note("--port %s: no %s found; waiting for one.  Ctrl-C to stop."
                 % (mode, what))
            said = True
        time.sleep(poll)


def report_ports(before):
    """Print the ports found, if they differ from BEFORE (None: the first
    time).  Returns them, to pass back in as BEFORE next time."""
    try:
        now = find_ports()
    except Exception as e:          # discovery is only a help: never fatal
        now = ["  (could not list the ports: %s)" % e]
    if now != before:
        if before is None:
            note("ports found on this computer:" if now else
                 "no serial ports or PseudoTTYs found on this computer")
        else:
            note("the ports changed; now:" if now else
                 "the ports changed; now there are none")
        for line in now:
            note(line)
    return now


# --------------------------------------------------------------------------
# Serial ports: termios on Linux / macOS, pyserial on Windows
# --------------------------------------------------------------------------

class _SerialPort:
    """What the two serial port classes share: waiting for a port that is
    not there yet.  A subclass has _open(), OPEN_ERRORS (what _open raises)
    and _not_there(e) (the reason if E means the port is not there, else
    None); a real error is raised as _fatal(e)."""

    def _fatal(self, e):
        return e

    def _open_wait(self, poll=1.0):
        """Open the port; if it is not there yet (e.g. LisaEm, which
        creates its virtual port, is not running, or the adapter is not
        plugged in), say so ONCE, list the ports that are there, and check
        again every POLL seconds until it appears (listing the ports again
        if they change).  Any other error (no permission, not a serial
        device, ...) is raised."""
        said = False
        ports = None                # the ports last listed
        while True:
            try:
                self._open()
            except self.OPEN_ERRORS as e:
                why = self._not_there(e)
                if why is None:
                    raise self._fatal(e)
                if not said:
                    note("serial port %s is not there%s; waiting for it.  "
                         "Ctrl-C to stop." % (self.path, " (%s)" % why if why else ""))
                    said = True
                ports = report_ports(ports)
                time.sleep(poll)
                continue
            if said:
                note("serial port %s: found it" % self.path)
            return


class PosixSerialPort(_SerialPort):
    """The serial port on Linux / macOS (termios, non-blocking reads with
    select).  PySerialPort is the same for Windows."""

    def __init__(self, port, baud, debug=False):
        self.port = port
        self.path = port            # reopened by this path (see wait_back)
        self.baud = baud
        self.debug = debug          # --debug (used only by the byte dump in read:
                                    # see DUMP_BYTES)
        self.pending = b""          # bytes read by wait_back's probe, not yet consumed
        # Allow tests to hand us an already-open fd (pty pair).
        env_fd = os.environ.get("LISAFTP_FD")
        self.fixed_fd = bool(env_fd)    # an fd we cannot reopen
        if env_fd:
            self.fd = int(env_fd)
            self.port = "<fd %s>" % env_fd
            self._setup()
        else:
            self._open_wait()

    # errno values that mean "the device is not there (yet)": the path
    # does not exist (or is a dangling link -- a virtual port whose
    # emulator is not running), or no device is behind it
    NOT_THERE = (errno.ENOENT, errno.ENXIO, errno.ENODEV)
    OPEN_ERRORS = (OSError,)

    def _not_there(self, e):
        """For _open_wait: the reason if OSError E means the port is not
        there (yet), else None (a real error)."""
        return e.strerror if e.errno in self.NOT_THERE else None

    def _open(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self._setup()

    def _setup(self):
        baud = self.baud
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        # Always 8N1, as the Lisa client sets its port (no parity option:
        # the per-chunk CRC-16 catches far more than parity would).
        attrs[2] &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
        attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
        bconst = getattr(termios, "B%d" % baud)
        attrs[2] |= bconst          # cflag baud
        attrs[4] = bconst           # ispeed
        attrs[5] = bconst           # ospeed
        # No XON/XOFF, ever: the kernel would swallow every 0x11/0x13 byte,
        # and those occur in file data and in the binary frame header/CRC
        # of every chunk.  Flow control is the protocol's (ACK per chunk).
        attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.IXANY)
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def set_baud(self, baud):
        """Switch the port to another rate (BAUD, and back)."""
        self.baud = baud
        self._setup()

    def _gone_now(self):
        """Probe the port without waiting: True if the other end is gone
        (end-of-file or an error), False if it is there.  A byte that
        arrives during the probe is kept in self.pending for read()."""
        try:
            r, _, _ = select.select([self.fd], [], [], 0)
            if not r:
                return False              # nothing to read: quiet, but there
            b = os.read(self.fd, 1)
        except BlockingIOError:
            return False
        except OSError:
            return True
        if not b:
            return True                   # end-of-file: still gone
        self.pending += b
        return False

    def wait_back(self, poll=1.0):
        """The other end of the port went away (end-of-file or an error:
        e.g. the emulator that owned a virtual port quit, or a USB adapter
        was unplugged).  Wait quietly for it to come back, checking once
        every POLL seconds; the device is reopened by its path, as a
        virtual port may have been deleted and created again.  Returns
        when the port is usable again."""
        while True:
            time.sleep(poll)
            if not self.fixed_fd:
                self.close()
                try:
                    self._open()
                except (OSError, termios.error):
                    continue              # not there (yet)
            if not self._gone_now():
                return

    def read(self, n, timeout):
        """Read exactly n bytes or raise TimeoutError / ClientGone."""
        buf = self.pending[:n]
        self.pending = self.pending[n:]
        deadline = time.monotonic() + timeout
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("read timeout after %d bytes" % len(buf))
            r, _, _ = select.select([self.fd], [], [], min(remaining, 0.2))
            if r:
                try:
                    chunk = os.read(self.fd, n - len(buf))
                except BlockingIOError:
                    continue
                except OSError as e:
                    raise ClientGone("port error: %s" % e.strerror)
                if not chunk:
                    raise ClientGone("port closed")
                if self.debug and DUMP_BYTES:
                    for b in chunk:
                        ch = chr(b) if 32 <= b < 127 else "\\x%02x" % b
                        print("  <RX %s>" % ch, file=sys.stderr)
                buf += chunk
        return buf

    def write(self, data):
        """Write all of data, waiting for the non-blocking port to drain.
        Raises TimeoutError if the output buffer stays full for 60 s
        (e.g. a long LIST while the peer is gone)."""
        deadline = time.monotonic() + 60.0
        while data:
            try:
                n = os.write(self.fd, data)
                data = data[n:]
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("write timeout: port not draining")
                select.select([], [self.fd], [], min(remaining, 1.0))
            except OSError as e:
                raise ClientGone("port error: %s" % e.strerror)

    def drain(self, limit=2.0):
        """Wait until everything written has left the PC, for the
        per-chunk timing -- at most LIMIT seconds.  Not tcdrain: that
        waits with no limit, and a pty whose other end stops reading (a
        stalled emulator, a test) would hang the server.  Instead the
        bytes still queued for output (TIOCOUTQ) are polled every ms."""
        deadline = time.monotonic() + limit
        buf = bytearray(4)
        while True:
            try:
                fcntl.ioctl(self.fd, termios.TIOCOUTQ, buf, True)
            except (OSError, AttributeError):
                return                  # not a tty, or no TIOCOUTQ: no wait
            if int.from_bytes(buf, sys.byteorder) == 0:
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.001)

    def flush_input(self):
        """Discard received-but-unread bytes (e.g. the CRLF tail of a
        HELLO line that arrived in the middle of a binary phase)."""
        self.pending = b""
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except termios.error:
            pass

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class PySerialPort(_SerialPort):
    """The serial port on Windows, through pyserial (pip install pyserial):
    the same methods as PosixSerialPort, so the server does not see the
    difference.  --port is a COM port name, e.g. COM3."""

    def __init__(self, port, baud, debug=False):
        try:
            import serial
        except ImportError:
            sys.exit("On Windows the server needs pyserial for the serial "
                     "port: pip install pyserial")
        self.serial = serial
        self.OPEN_ERRORS = (serial.SerialException,)
        self.port = port
        self.path = port
        self.baud = baud
        self.debug = debug
        self.pending = b""
        self.ser = None
        self._open_wait()

    def _open(self):
        # 8N1, no XON/XOFF and no hardware handshake, as on Linux / macOS;
        # timeouts are set per read and write
        self.ser = self.serial.Serial(
            port=self.path, baudrate=self.baud, bytesize=8, parity="N",
            stopbits=1, xonxoff=False, rtscts=False, dsrdtr=False,
            timeout=0, write_timeout=60.0)

    def _not_there(self, e):
        """For _open_wait: "" if the SerialException means the port does
        not exist (yet), e.g. the adapter is not plugged in; else None."""
        # pyserial: on Linux / macOS the OS errno; on Windows a message
        # like "could not open port 'COM3': FileNotFoundError(2, 'The
        # system cannot find the file specified.', ...)"
        if getattr(e, "errno", None) in PosixSerialPort.NOT_THERE:
            return ""
        text = str(e)
        if ("FileNotFoundError" in text or "No such file" in text
                or "cannot find the file" in text):
            return ""
        return None

    def _fatal(self, e):
        return OSError(str(e))      # main reports it as "cannot open ..."

    def set_baud(self, baud):
        """Switch the port to another rate (BAUD, and back)."""
        self.baud = baud
        self.ser.baudrate = baud

    def wait_back(self, poll=1.0):
        """The port went away (e.g. the adapter was unplugged): reopen it
        by its name once it is back."""
        while True:
            time.sleep(poll)
            self.close()
            try:
                self._open()
                return
            except self.serial.SerialException:
                continue              # not there (yet)

    def read(self, n, timeout):
        """Read exactly n bytes or raise TimeoutError / ClientGone."""
        buf = self.pending[:n]
        self.pending = self.pending[n:]
        deadline = time.monotonic() + timeout
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("read timeout after %d bytes" % len(buf))
            try:
                # 0.2 s slices: the timeout is set only when it changes
                # (each change is an OS call on Windows)
                t = 0.2 if remaining >= 0.2 else remaining
                if self.ser.timeout != t:
                    self.ser.timeout = t
                buf += self.ser.read(n - len(buf))
            except self.serial.SerialException as e:
                raise ClientGone("port error: %s" % e)
        return buf

    def write(self, data):
        """Write all of data; TimeoutError if it does not go out within
        60 s (e.g. a long LIST while the peer is gone)."""
        try:
            self.ser.write(data)
        except self.serial.SerialTimeoutException:
            raise TimeoutError("write timeout: port not draining")
        except self.serial.SerialException as e:
            raise ClientGone("port error: %s" % e)

    def drain(self, limit=2.0):
        """Wait until everything written has left the PC (the bytes still
        queued for output, polled every ms), at most LIMIT seconds."""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            try:
                if not self.ser.out_waiting:
                    return
            except self.serial.SerialException:
                return
            time.sleep(0.001)

    def flush_input(self):
        """Discard received-but-unread bytes."""
        self.pending = b""
        try:
            self.ser.reset_input_buffer()
        except self.serial.SerialException:
            pass

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except self.serial.SerialException:
                pass


# LISAFTP_PYSERIAL=1 uses pyserial on Linux / macOS too: the tests run the
# Windows code that way (tests/test_pyserial_port.py)
SerialPort = (PosixSerialPort if POSIX and not os.environ.get("LISAFTP_PYSERIAL")
              else PySerialPort)
PORT_SETUP_ERRORS = (termios.error,) if POSIX else ()   # see main


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class Console:
    """What the server prints on its console (stderr): messages, the
    progress bar of a GET/PUT, its speed and the per-chunk timing."""

    MARKS_PER_LINE = 60         # progress marks per console line (as the Lisa)

    def __init__(self, debug):
        self.debug_on = debug       # detailed output (DEBUG ON / --debug)
        self.debug_default = debug  # the startup setting; HELLO restores it
        self._marks_on_line = 0     # progress marks on the current console line
        self._bytes_done = 0        # progress: bytes of the file transferred so far
        self._bytes_total = 0       # progress: the file size (for the percentage)
        self._t_start = 0.0         # progress: when the transfer started
        self._timing = {}           # per-chunk timing: key -> (n, total, min, max)

    def say(self, msg):
        """Always shown: commands and their outcome."""
        self._endline()
        print(msg, file=sys.stderr, flush=True)

    def dlog(self, msg):
        """Shown only when debug output is on."""
        if self.debug_on:
            self.say("    " + msg)

    # -- progress bar ------------------------------------------------------

    def progress_start(self, total):
        self._marks_on_line = 0
        self._bytes_done = 0
        self._bytes_total = total
        self._t_start = time.monotonic()

    def mark(self, ok, n=0):
        """Progress bar (debug off only): '.' = a chunk of n file bytes got
        through, 'x' = a failed try that is retried.  Each line of marks
        ends with the percentage of the file done (see _endline)."""
        self._bytes_done += n
        if self.debug_on:
            return
        sys.stderr.write("." if ok else "x")
        sys.stderr.flush()
        self._marks_on_line += 1
        if self._marks_on_line >= self.MARKS_PER_LINE:
            self._endline()

    def _endline(self):
        """End a line of progress marks with ' (23% complete)'."""
        if self._marks_on_line:
            pct = 100 if self._bytes_total <= 0 else \
                min(100, self._bytes_done * 100 // self._bytes_total)
            sys.stderr.write(" (%d%% complete)\n" % pct)
            sys.stderr.flush()
            self._marks_on_line = 0

    def rate(self, nbytes):
        """' in 12.3 s (456 bytes/s)': time and speed since progress_start,
        for the end of a successful GET/PUT."""
        secs = time.monotonic() - self._t_start
        bps = nbytes / secs if secs > 0 else 0.0
        return " in %.1f s (%.0f bytes/s)" % (secs, bps)

    # -- per-chunk timing (shown after a successful GET/PUT) -------------
    # Where the time of a chunk goes, measured here, as the Lisa has no
    # fine clock: GET -- the frame leaving the PC ("wire"), the Lisa from
    # then until its ACK is here ("lisa": receive, check, write, ACK), the
    # server before the next frame ("server").  PUT -- the Lisa from our
    # ACK leaving until its next frame starts ("lisa": the ACK, reading,
    # decoding, CRC, the start of sending), the frame arriving ("frame"),
    # the server checking it and sending the ACK ("server").  Only clean
    # chunks count: not retries, not the empty end-of-file chunk.

    def timing_reset(self):
        self._timing = {}

    def timing_add(self, key, secs):
        n, tot, lo, hi = self._timing.get(key, (0, 0.0, secs, secs))
        self._timing[key] = (n + 1, tot + secs, min(lo, secs), max(hi, secs))

    def timing_ms(self, key, secs):
        """timing_add, and ' key N ms' for the debug line."""
        self.timing_add(key, secs)
        return " %s %.0f ms" % (key, secs * 1000)

    def timing_line(self, parts):
        """'  per chunk (N): label A ms (min-max), ...' or None."""
        out = []
        count = 0
        for key, label in parts:
            if key in self._timing:
                n, tot, lo, hi = self._timing[key]
                count = max(count, n)
                out.append("%s %.0f ms (%.0f-%.0f)"
                           % (label, tot / n * 1000, lo * 1000, hi * 1000))
        if not out:
            return None
        return "  per chunk (%d chunks): %s" % (count, ", ".join(out))


class LisaFtpServer(Console):
    def __init__(self, args, port=None):
        """args: the command line (see main).  port: an open port to use
        instead of opening args.port (the tests pass a stand-in)."""
        Console.__init__(self, args.debug)
        self.args = args
        self.home_dir = os.path.realpath(args.home_dir)
        self.timeout = args.timeout
        self.readonly = args.read_only
        self.monkey_wrench = args.monkey_wrench
        self.port = port or SerialPort(args.port, args.baud, args.debug)
        self.start_baud = args.baud     # QUIT, and a garbled line, go back to it
        self._confirm_until = None      # BAUD: until when the new rate is on trial
        self._baud_old = None           # BAUD: the rate to go back to if it fails
        self._pending = None            # a line read while waiting for NEXT,
                                        # for the command loop
        self._page = 0                  # LIST: bytes sent since the last MORE
        self._mark = None               # MARK: time of the previous MARK
        self._t_reply = None            # send_chunk: when the last ACK came
        self._client_failed = False     # send_chunk: the client sent FAIL
                                        # (answered OK)
        self._done_line = None          # send_chunk: the client's DONE, taken
                                        # as the ACK of the end-of-file chunk
                                        # (its A was lost)
        self._send_why = ""             # send_chunk: why it failed (for our
                                        # FAIL line)

    # -- low level ---------------------------------------------------------

    def t_block(self, n):
        """Timeout for n bytes of a frame that has started: T_BLOCK plus
        twice their time on the wire (10 bits each), or at the Lisa's own
        sending speed (~2700 bytes/s) if that is slower."""
        baud = self.args.baud
        rate = min(baud / 10.0, 2700.0)
        return T_BLOCK + 2.0 * n / rate

    def send_line(self, line):
        if len(line) > MAX_LINE:
            # The Lisa client holds lines in string[255] and would drop
            # the excess chars silently; truncate here instead.  The tail
            # is always the expendable part (names, error text, home_dir path).
            self.dlog("line truncated to %d chars: %r"
                          % (MAX_LINE, line))
            line = line[:MAX_LINE]
        # a character the Lisa does not have becomes '?' (never an error:
        # a reply must not stop the server)
        self.port.write(line.encode(WIRE, "replace") + b"\r\n")
        self.dlog("sent: " + line)

    def read_line(self, timeout, quiet=False):
        """Read one CRLF-terminated line.  Returns None if no byte came
        within TIMEOUT, '' if a partial line was dropped (no byte for TIMEOUT
        after it) or the line was too long.
        quiet: do not log it (the caller reports what it meant)."""
        buf = b""
        while True:
            try:
                b = self.port.read(1, timeout)
            except TimeoutError:
                if buf:
                    # partial line: drop it, stay in sync
                    self.dlog("dropping partial line: %r" % buf)
                    return ""
                return None          # no byte within TIMEOUT
            buf += b
            if b == b"\n":
                line = buf[:-1].rstrip(b"\r").decode(WIRE)
                if len(line) > MAX_IN_LINE:
                    self.dlog("line too long, dropped")
                    return ""
                if not quiet:
                    self.dlog("received: " + line)
                return line

    def send_banner(self):
        # The home_dir path is the only part of the banner that can exceed
        # MAX_LINE, so truncate it there (the client only displays the
        # banner), keeping the chunk= field intact.  It is sent as it is,
        # not percent-encoded, so it is readable on the Lisa (nothing
        # parses it); only characters outside printable ASCII, which the
        # line cannot carry, are shown as '?'.
        head = "Hi from Lisa Serial FTP Server version %s : " % PROTOCOL
        shown = "".join(c if " " <= c <= "~" else "?" for c in self.home_dir)
        home = "home_dir=%s " % shown
        tail = "chunk_size=%d" % CHUNK
        room = MAX_LINE - len(head) - len(tail)
        if len(home) > room:
            home = home[:room]
        self.send_line(head + home + tail)

    def abort(self, text):
        """Tell the peer the transfer is dead; it must answer OK.
        The peer may answer with its own FAIL first (both sides gave up
        at the same time), so read lines until its OK (or a HELLO from a
        restarted peer)."""
        self.say("  FAILED: " + text)
        try:
            self.send_line("FAIL " + text)
        except Exception:
            return
        while True:
            try:
                line = self.read_line(self.timeout)
            except Exception:
                return
            if line is None or line == "":
                return          # idle timeout / partial line: give up
            if line == "OK":
                return
            if line == "HELLO":
                # the peer crashed and came back while we waited for its OK:
                # the FAIL went to the old session, so greet the new one
                self.peer_restarted()
                return

    def peer_restarted(self):
        """The peer sent HELLO mid-transfer: it crashed and restarted.
        Abandon the transfer, discard the rest of that HELLO line, and
        answer with a fresh banner so the new session can carry on."""
        self.say("  FAILED: the client restarted (HELLO) -- transfer abandoned")
        self.debug_on = self.debug_default
        self.port.flush_input()
        self.send_banner()

    PART = ".part"      # PUT: '<name>.part' holds the upload until it is verified

    @classmethod
    def part_path(cls, p):
        """The temporary file a PUT to p writes into: '<name>.part' in the
        same folder (so the final rename stays on one file system)."""
        return p + cls.PART

    def drop_partial(self, tmp):
        """Remove the temporary file of a PUT that did not complete."""
        try:
            os.remove(tmp)
            self.dlog("removed partial upload: %s" % tmp)
        except OSError:
            pass

    # -- loss recovery helpers (must match LisaSerialFtpClient.text) -------------------

    def _nak(self, seq):
        """NAK the chunk we are still waiting for; the peer retransmits it."""
        self.port.write(b"N %d\r\n" % (seq % SEQ_MOD))

    def _wait_sent(self):
        """Wait until what we wrote has left the PC (for the timing, and
        before a baud change)."""
        self.port.drain()

    def _ack(self, seq):
        """ACK chunk seq: 'A <seq>' (numbered, so the peer can tell it
        from a stale re-ACK)."""
        self.port.write(b"A %d\r\n" % (seq % SEQ_MOD))

    def _discard_until_quiet(self):
        """Discard received-but-unread bytes until a read times out (the
        line is quiet).  Used after a bad/partial chunk to drop its stray
        tail before we NAK; the peer is quiet (stop-and-wait) so this only
        ever discards the bad frame's leftover bytes.  A cap keeps it from
        running away on a noisy line."""
        n = 0
        cap = 2 * (CHUNK + 7)
        while n < cap:
            try:
                self.port.read(1, T_QUIET)
                n += 1
            except (TimeoutError, ClientGone):
                break
        if n:
            self.dlog("discarded %d stray byte(s)" % n)

    def _read_rest_of_line(self):
        """Read the remainder of a CRLF line (the first byte was already
        consumed by the caller).  Returns the text without the CRLF; '' on
        timeout.  Used to inspect a 'FAIL ...' line the peer sent where a
        chunk header was expected."""
        buf = b""
        while len(buf) < MAX_IN_LINE:
            try:
                b = self.port.read(1, T_BLOCK)
            except (TimeoutError, ClientGone):
                break
            if b == b"\n":
                break
            if b != b"\r":
                buf += b
        return buf.decode(WIRE)

    # -- path safety -------------------------------------------------------

    def safe_path(self, name):
        name = name_from_wire(name.strip())
        if not name:
            raise ProtocolError(1, "empty name")
        # '/' is the separator; the name is under home_dir
        rel = name.lstrip("/")
        if ".." in rel.split("/"):
            raise ProtocolError(2, "bad name")
        # no length limit of our own: the OS's applies (a name it refuses
        # is an error reply); the Lisa's 32 characters are the client's
        # concern, for the names it stores
        p = os.path.realpath(os.path.join(self.home_dir, rel))
        if p != self.home_dir and not p.startswith(self.home_dir + os.sep):
            raise ProtocolError(2, "name outside the shared folder (--home_dir)")
        return p

    # -- commands ----------------------------------------------------------

    def cmd_hello(self, args):
        self.debug_on = self.debug_default     # a new session
        self.send_banner()
        self.say("  ok: banner sent")

    BAUD_TRIAL = 10.0   # BAUD: seconds the client has to send a command at the new rate

    def cmd_baud(self, args):
        """BAUD <rate>: answer 'OK <rate>' at the old rate, wait until it has
        left the PC, and switch.  The new rate is then on trial: the
        client's next command (its HELLO) must arrive at it, as a valid
        line, within BAUD_TRIAL seconds; silence, or a garbled line (what
        bytes sent at another rate look like), switches back to the old
        rate (handle_client)."""
        if len(args) != 1 or not args[0].isdigit():
            raise ProtocolError(1, "usage: BAUD rate")
        nb = int(args[0])
        if nb not in BAUDS:
            raise ProtocolError(21, "unsupported baud rate %d (%s)"
                                % (nb, " ".join(str(b) for b in sorted(BAUDS))))
        old = self.port.baud
        self.send_line("OK %d" % nb)
        # the reply must be out of the adapter too before the switch: the
        # kernel's queue first, then about its own length again at the old
        # rate for the adapter's FIFO
        self._wait_sent()
        time.sleep(0.1 + 12 * 10.0 / old)
        if nb == old:
            self.say("  ok: the baud rate stays %d" % nb)
            return
        self.port.set_baud(nb)
        self._baud_old = old
        self._confirm_until = time.monotonic() + self.BAUD_TRIAL
        self.say("  ok: now at %d baud (was %d); waiting for the client at it"
                 % (nb, old))

    def revert_baud(self, to, why):
        """Back to the rate TO (a failed BAUD, a garbled line, QUIT)."""
        self._confirm_until = None
        if self.port.baud != to:
            was = self.port.baud
            self.port.set_baud(to)
            self.say("  baud: back to %d (from %d): %s" % (to, was, why))

    def cmd_delete(self, args):
        """DELETE <name>: delete one file (the client's 'delete'; it has
        asked the user first).  Not checked beforehand: os.remove says
        whether the file is there.  Never a folder (that is refused)."""
        if self.readonly:
            raise ProtocolError(20, "server is read-only")
        if len(args) != 1:
            raise ProtocolError(1, "usage: DELETE name")
        p = self.safe_path(args[0])
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                raise IsADirectoryError(p)
            os.remove(p)
        except FileNotFoundError:
            raise ProtocolError(11, "no such file on server: %s" % p)
        except IsADirectoryError:
            raise ProtocolError(12, "is a folder, not deleted: %s" % p)
        except OSError as e:
            raise ProtocolError(13, "cannot delete %s: %s" % (p, e.strerror))
        self.send_line("OK deleted")
        self.say("  ok: deleted %s" % p)

    def cmd_debug(self, args):
        a = args[0].upper() if args else ""
        if a not in ("ON", "OFF"):
            raise ProtocolError(1, "usage: DEBUG ON|OFF")
        self.debug_on = (a == "ON")
        self.send_line("OK debug %s" % a.lower())
        self.say("  ok: debug output %s" % a.lower())

    def cmd_cal(self, args):
        """Speed calibration.  Answer exactly CAL_SECONDS of real time
        after the request arrived.  The Lisa counts how many of its
        1-second input timeouts expire in the meantime: in LisaEm at a
        high throttle, emulated time runs much faster than real time, and
        that count is how the client learns by how much to stretch its
        timeouts.  On a real Lisa it counts ~CAL_SECONDS."""
        time.sleep(CAL_SECONDS)
        self.send_line("CAL %d" % int(CAL_SECONDS * 1000))
        self.say("  ok: answered after %g s" % CAL_SECONDS)

    def cmd_list(self, args):
        d = self.safe_path(args[0]) if args else self.home_dir
        if not os.path.isdir(d):
            raise ProtocolError(10, "no such directory on server: %s" % d)
        self._page = 0
        self.list_line("OK")
        n = 0
        for entry in sorted(os.listdir(d)):
            full = os.path.join(d, entry)
            try:
                st = os.stat(full)
            except OSError:
                continue
            kind = "D" if os.path.isdir(full) else "F"
            # long names are truncated to MAX_LINE by send_line
            line = "%s %d %s" % (kind, st.st_size, name_to_wire(entry))
            if not self.list_line(line):
                return
            n += 1
        if not self.list_line("DONE"):
            return
        self.say("  ok: %d %s listed" % (n, "entry" if n == 1 else "entries"))

    def list_line(self, line):
        """send_line for LIST, in pages (see LIST_PAGE): if this line would
        make the page longer than LIST_PAGE bytes (with room for the MORE
        line), end the page with MORE and wait for the client's NEXT.
        False if the client did not ask for more: the listing is over."""
        n = min(len(line), MAX_LINE) + 2
        if self._page and self._page + n + len("MORE\r\n") > LIST_PAGE:
            if not self._more():
                return False
            self._page = 0
        self._page += n
        self.send_line(line)
        return True

    def _more(self):
        """End a page with MORE and wait for NEXT; MORE is resent if no
        reply comes (a lost line).  Any other line means the client gave up
        the listing (Apple-period, or it restarted): that line is kept, and
        the command loop handles it as the next command."""
        for _ in range(LIST_TRIES):
            self.send_line("MORE")
            line = self.read_line(self.timeout)
            if not line:               # nothing, or a broken line
                self.dlog("no NEXT within %.0f s, sending MORE again" % self.timeout)
                continue
            if line.upper() == "NEXT":
                return True
            self.say("  FAILED: the client stopped the listing (%r)" % line)
            self._pending = line
            return False
        self.say("  FAILED: no NEXT from the client -- listing abandoned")
        return False

    def cmd_sum(self, args):
        """SUM name T|B: size, CRC-16 and byte sum of a file, for get -c /
        put -c (see lisa_text for T)."""
        if len(args) != 2 or args[1].upper() not in ("T", "B"):
            raise ProtocolError(1, "usage: SUM name T|B")
        p = self.safe_path(args[0])
        if not os.path.isfile(p):
            raise ProtocolError(11, "no such file on server: %s" % p)
        if os.path.getsize(p) > MAX_FILE:
            raise ProtocolError(4, "file too big")
        with open(p, "rb") as f:
            data = f.read()
        if args[1].upper() == "T":
            data = lisa_text(data)
        crc = crc16(data)
        bsum = sum(data) & 0xFFFF
        self.send_line("OK %d %04x %04x" % (len(data), crc, bsum))
        self.say("  ok: %s%s: %d bytes, crc %04x, sum %04x"
                 % (name_from_wire(args[0]),
                    " (as Lisa text)" if args[1].upper() == "T" else "",
                    len(data), crc, bsum))

    def cmd_mark(self, args):
        """MARK <label>: answer with the ms since the previous MARK (the
        client's bench command times its own work with it)."""
        now = time.monotonic()
        ms = 0 if self._mark is None else int(round((now - self._mark) * 1000))
        self._mark = now
        self.send_line("OK %d" % ms)
        label = " ".join(args)
        if label and label != "-":
            self.say("  bench: %s: %d ms" % (label, ms))

    def cmd_echo(self, args):
        """ECHO <n>: n lines 'A 12345', back to back (the client's bench
        times reading short lines with them)."""
        try:
            n = int(args[0]) if args else 0
        except ValueError:
            n = 0
        if not 1 <= n <= 100:
            raise ProtocolError(1, "usage: ECHO n (1..100)")
        self.port.write(b"A 12345\r\n" * n)
        self.dlog("sent %d lines 'A 12345'" % n)

    def cmd_frames(self, args):
        """FRAMES <n>: n chunks of CHUNK made-up bytes, then the empty
        end-of-file chunk, sent as a GET sends them; nothing before or
        after (the client's bench times receiving chunks with it)."""
        try:
            n = int(args[0]) if args else 0
        except ValueError:
            n = 0
        if not 1 <= n <= 400:
            raise ProtocolError(1, "usage: FRAMES n (1..400)")
        payload = bytes((i * 37) & 0xFF for i in range(CHUNK))
        self.progress_start(n * CHUNK)
        self.timing_reset()
        self._t_reply = None
        for seq in range(n + 1):
            if not self.send_chunk(seq, payload if seq < n else b""):
                self.say("  FAILED: frames: %s" % self._send_why)
                return
        self.say("  ok: %d chunks sent" % n)
        tl = self.timing_line([("wire", "the frame leaves the PC"),
                               ("lisa", "the Lisa until its ACK"),
                               ("server", "the server")])
        if tl:
            self.say(tl)

    def cmd_get(self, args):
        """GET <name> [T]: send the file as it is, or (T) as the text pages
        of the Lisa .TEXT file the client writes (lisa_pages): the client
        then stores the bytes as they come, after its header page."""
        if not args:
            raise ProtocolError(1, "missing filename in GET command")
        if len(args) > 2 or (len(args) == 2 and args[1].upper() != "T"):
            raise ProtocolError(1, "usage: GET name [T]")
        p = self.safe_path(args[0])
        if not os.path.isfile(p):
            raise ProtocolError(11, "no such file on server: %s" % p)
        pages = None
        if len(args) == 2:
            if os.path.getsize(p) > MAX_FILE:
                raise ProtocolError(4, "file too big")
            with open(p, "rb") as f:
                pages = lisa_pages(f.read())
            size = len(pages)
        else:
            size = os.path.getsize(p)
        self.progress_start(size)
        self.timing_reset()
        self._t_reply = None
        self.send_line("OK %d" % size)
        # Wait for the client's GO: it sends it once its local file is
        # open.  Sent straight after OK, chunk 0 arrived while a real Lisa
        # was still reading the OK line and creating the file, and the
        # rest of the OK line plus a 1023-byte frame overflowed its
        # 1024-byte type-ahead buffer: chunk 0 always failed once.  'N 0'
        # counts as GO too (our GO was lost; the client NAKs chunk 0 after
        # T_FRAME), and FAIL means the client could not create its file.
        try:
            go = self.read_line(max(self.timeout, T_FRAME + 5.0), quiet=True)
        except TimeoutError:
            go = None
        if go == "HELLO":
            self.peer_restarted()
            return
        if go is not None and go.startswith("FAIL"):
            self.say("  FAILED: the client could not start the get: %s" % go)
            return
        if go not in ("GO", "N 0"):
            self.abort("no GO from the client (got %r)" % go)
            return
        self.dlog("received: %s" % go)
        seq = 0
        try:
            with (io.BytesIO(pages) if pages is not None else open(p, "rb")) as f:
                while True:
                    payload = f.read(CHUNK)
                    if not self.send_chunk(seq, payload):
                        if self._client_failed:
                            return        # the client gave up (we said OK)
                        self.abort(self._send_why)
                        return
                    if not payload:
                        break             # the end-of-file chunk went through
                    seq += 1
            # receiver (Lisa) finishes with DONE <bytes> <crc> (already
            # read if it arrived in place of the end-of-file chunk's ACK)
            line = self._done_line
            if line is None:
                line = self.read_line(self.timeout)
        except PeerRestarted:
            self.peer_restarted()
            return
        if line == "HELLO":
            self.peer_restarted()
            return
        if line is None or not line.startswith("DONE"):
            self.abort("no DONE from client")
            return
        # Verify: the client (receiver) reported what it got; compare it
        # with what was sent and answer with the verdict and OUR numbers,
        # so the client can check them too before it keeps the file.
        mycrc = "%04x" % (crc16(pages) if pages is not None else self.file_crc(p))
        parts = line.split()
        good = False
        if len(parts) == 3:
            try:
                good = (int(parts[1]) == size and parts[2].lower() == mycrc)
            except ValueError:
                good = False
        self.send_line("%s %d %s" % ("OK" if good else "BAD", size, mycrc))
        if good:
            what = (" (%d Lisa text pages)" % (size // TEXT_PAGE)
                    if pages is not None else "")
            self.say("  ok: sent %d bytes of %s%s, verified (crc %s)%s"
                     % (size, name_from_wire(args[0]), what, mycrc,
                        self.rate(size)))
            tl = self.timing_line([("wire", "the frame leaves the PC"),
                                   ("lisa", "the Lisa until its ACK"),
                                   ("server", "the server")])
            if tl:
                self.say(tl)
        else:
            self.say("  FAILED: verify: the client got %r, we sent %d %s"
                     % (line, size, mycrc))

    def file_crc(self, p):
        crc = 0xFFFF
        with open(p, "rb") as f:
            while True:
                b = f.read(4096)
                if not b:
                    break
                crc = crc16(b, crc)
        return crc

    def _wrench(self, frame, seq, what):
        """Monkey wrench (test mode): with probability WRENCH_ODDS, flip one
        random byte after the FRAME_HEAD -- the payload or the CRC, never the
        header -- to a random value.  The header is left intact on purpose:
        corrupting it would desync the byte stream, whereas corrupting the
        payload or the CRC is what exercises the CRC check + NAK + retransmit
        path this option exists to test.  frame must be a mutable bytearray.
        Reports each flipped byte on the console."""
        if not self.monkey_wrench or random.random() >= WRENCH_ODDS:
            return
        i = FRAME_HEAD + random.randrange(len(frame) - FRAME_HEAD)
        old = frame[i]
        new = random.randrange(256)
        if new == old:
            new = (new + 1) & 0xFF     # guarantee an actual change
        frame[i] = new
        self.say("[monkey-wrench] %s chunk seq=%d: byte %d 0x%02x -> 0x%02x"
                 % (what, seq, i, old, new))

    def send_chunk(self, seq, payload):
        """Send one framed chunk, wait for A / N.  Returns True when the
        client has the chunk.  Raises PeerRestarted if the client sends
        HELLO (it crashed and restarted).

        Every reply other than an ACK counts as one failed try, and the
        same chunk is sent again, up to max_retries tries: a NAK, no reply
        within T_REPLY, and also a garbled reply (a byte of the 'A' line
        was corrupted on the wire).  A resend is always safe: the client
        re-ACKs a chunk it already has.

        Replies are numbered: 'A <seq>' accepts.  A reply about an
        EARLIER chunk is stale (e.g. the re-ACK of a duplicate we sent)
        and is skipped -- we keep waiting -- so replies cannot fall out
        of step with the chunks.

        'N <seq+1>' means the client already has this chunk (it asks for
        the next one): its ACK was lost, and its wait for the next chunk
        ran out before our T_REPLY did (in a fast emulator its timeouts
        are only as good as its timeout-factor).  That is an ACK.

        On False, self._send_why says why (for the FAIL line), and
        self._client_failed is True if the client itself gave up (it sent
        FAIL, which has been answered with OK: no FAIL from us then)."""
        self._client_failed = False
        self._done_line = None
        self._send_why = "no ACK from the client after %d tries" % MAX_RETRIES
        wire_seq = seq % SEQ_MOD
        next_seq = (seq + 1) % SEQ_MOD
        base = (b"T" + wire_seq.to_bytes(2, "big")
                + len(payload).to_bytes(2, "big") + payload)
        base += crc16(base[1:]).to_bytes(2, "big")
        what = ("chunk %d: %d bytes sent" % (seq, len(payload))
                if payload else "chunk %d: end-of-file chunk sent" % seq)
        for attempt in range(1, MAX_RETRIES + 1):
            frame = bytearray(base)
            self._wrench(frame, seq, "send")
            t0 = time.monotonic()
            prev = self._t_reply
            self.port.write(bytes(frame))
            self._wait_sent()
            t1 = time.monotonic()          # the frame has left the PC
            tries = " (try %d)" % attempt if attempt > 1 else ""
            # Wait for the A/N reply with T_REPLY: if it (or the whole
            # chunk) was lost, no reply arrives and we retransmit the same
            # chunk.
            nstale = 0
            while True:
                try:
                    line = self.read_line(T_REPLY, quiet=True)
                except TimeoutError:
                    line = None
                kind, num = parse_reply(line)
                if (kind is None or not seq_behind(num, wire_seq)
                        or nstale >= 4):
                    break
                nstale += 1
                self.dlog("chunk %d: stale reply %r (about an earlier chunk)"
                          " skipped" % (seq, line))
            if kind is not None and seq_behind(num, wire_seq):
                kind = None           # only stale replies: resend
            if kind == "A" and num == wire_seq:
                t2 = time.monotonic()
                self._t_reply = t2
                tms = ""
                if attempt == 1 and payload:   # clean chunks only
                    tms = (self.timing_ms("wire", t1 - t0)
                           + self.timing_ms("lisa", t2 - t1)
                           + (self.timing_ms("server", t0 - prev)
                              if prev else ""))
                self.dlog("%s%s -> accepted (ACK)%s" % (what, tries, tms))
                self.mark(True, len(payload))
                return True
            if not payload and line is not None and line.startswith("DONE"):
                # The end-of-file chunk: the client sends its ACK and its
                # DONE back to back.  The ACK was lost but the DONE came:
                # the client has the chunk.  Keep the DONE for cmd_get.
                self.dlog("%s%s -> DONE instead of the ACK: the client has it"
                          " (its ACK was lost) -> accepted" % (what, tries))
                self.mark(True, 0)
                self._done_line = line
                return True
            if line == "HELLO":
                raise PeerRestarted("HELLO instead of A/N")
            if line is not None and line.startswith("FAIL"):
                # the client gave up (too many bad tries, or Apple-period)
                self.dlog("%s%s -> the client gave up: %s" % (what, tries, line))
                self.port.write(b"OK\r\n")
                self.say("  FAILED: the client aborted: %s" % line)
                self._client_failed = True
                return False
            nak = num if kind == "N" else None
            if nak == next_seq:
                self.dlog("%s%s -> NAK for the next chunk: the client already "
                          "has this one (its ACK was lost) -> accepted"
                          % (what, tries))
                self.mark(True, len(payload))
                return True
            if nak == wire_seq:
                self.dlog("%s%s -> rejected by the client (NAK), resending"
                          % (what, tries))
            elif line is None or line == "":
                self.dlog("%s%s -> no reply within %.0f s, resending"
                          % (what, tries, T_REPLY))
            else:
                # a garbled ACK/NAK, or a NAK for a chunk we are not at
                self.dlog("%s%s -> unexpected reply %r, resending"
                          % (what, tries, line))
            self.mark(False)
        self.dlog("chunk %d: no ACK after %d tries" % (seq, MAX_RETRIES))
        return False

    def cmd_put(self, args):
        if self.readonly:
            raise ProtocolError(20, "server is read-only")
        if len(args) < 2 or not args[1].isdigit():
            raise ProtocolError(1, "usage: PUT name size")
        p = self.safe_path(args[0])
        size = int(args[1])
        if size > MAX_FILE:
            raise ProtocolError(4, "file too big")
        d = os.path.dirname(p)
        if not os.path.isdir(d):
            raise ProtocolError(10, "no such directory")
        if os.path.isdir(p):
            raise ProtocolError(13, "cannot create %s: it is a folder" % p)
        # The upload goes into a temporary file, '<name>.part', renamed to
        # the name only once the client has verified it: until then an
        # older file of that name is untouched, and a failed upload leaves
        # nothing behind.  It is created before the OK, so a name the OS
        # refuses (a character it does not allow) is an error reply, not a
        # failure in the middle of the transfer.
        tmp = self.part_path(p)
        try:
            f = open(tmp, "wb")
        except OSError as e:
            raise ProtocolError(13, "cannot create %s: %s" % (p, e.strerror))
        kept = False
        try:
            kept = self._put_into(f, tmp, p, size, args[0])
        finally:
            f.close()
            if not kept:
                self.drop_partial(tmp)

    def _put_into(self, f, tmp, p, size, name):
        """PUT, after the temporary file tmp (open as f) is made: receive
        the chunks into it, check them with the client, and on success
        rename it to p.  Returns True if the file was kept (renamed);
        otherwise the caller removes tmp."""
        self.progress_start(size)
        self.timing_reset()
        self.send_line("OK")
        seq = 0
        crc = 0xFFFF
        total = 0
        fails = 0                       # failed tries on the current chunk
        giveup = False
        t_acked = None                  # our last ACK left the PC (timing)
        try:
            with f:
                while True:
                    got = self._recv_chunk(seq)
                    kind = got[0]
                    if kind == "fail":
                        # the peer aborted: acknowledge its FAIL, drop the
                        # partial file
                        self.say("  FAILED: the client aborted: %s" % got[1])
                        self.port.write(b"OK\r\n")
                        return False
                    if kind == "dup":
                        # already accepted (our A was lost, so the peer
                        # retransmitted it): ACK it again and wait for the
                        # real next chunk
                        self.dlog("chunk %d: received again (our ACK was lost)"
                                  " -> ACK again" % got[1])
                        self._ack(got[1])
                    elif kind == "bad":
                        _, why, discard = got
                        self.dlog("chunk %d: %s" % (seq, why))
                        self.mark(False)
                        if discard:
                            self._discard_until_quiet()
                    if kind != "ok":
                        fails += 1
                        if fails >= MAX_RETRIES:
                            giveup = True
                            break
                        if kind == "bad":
                            self._nak(seq)     # the peer sends it again
                        continue
                    # a good chunk: ACK it
                    _, payload, t_first, t_done = got
                    got_len = len(payload)
                    self._ack(seq)
                    self._wait_sent()
                    t_ack = time.monotonic()
                    tms = ""
                    if got_len and fails == 0:     # clean chunks only
                        tms = ((self.timing_ms("lisa", t_first - t_acked)
                                if t_acked else "")
                               + self.timing_ms("frame", t_done - t_first)
                               + self.timing_ms("server", t_ack - t_done))
                    t_acked = t_ack
                    if got_len:
                        self.dlog("chunk %d: %d bytes received, CRC ok -> ACK%s"
                                  % (seq, got_len, tms))
                    else:
                        self.dlog("chunk %d: end-of-file chunk received -> ACK" % seq)
                    self.mark(True, got_len)
                    fails = 0
                    if got_len == 0:
                        break
                    f.write(payload)
                    crc = crc16(payload, crc)
                    total += got_len
                    seq += 1
        except PeerRestarted:
            self.peer_restarted()
            return False
        if giveup:
            self.abort("too many chunk errors")
            return False
        done = "DONE %d %04x" % (total, crc)
        if total != size:
            # a short (or long) file must not pass as verified: the CRC only
            # covers what arrived.  The client's check of DONE fails on this
            # reply, it answers BAD, and the file is deleted below.
            self.say("  FAILED: the client announced %d bytes, %d arrived" % (size, total))
            done = "ERR 5 size mismatch: announced %d, received %d" % (size, total)
        self.send_line(done)
        # Verify: the client (sender) answers OK/BAD with ITS numbers.  Keep
        # the file only on OK with numbers equal to what we received.
        verdict = self._read_verdict(seq, done)
        parts = (verdict or "").split()
        good = False
        if len(parts) == 3 and parts[0] == "OK":
            try:
                good = (int(parts[1]) == total and parts[2].lower() == "%04x" % crc)
            except ValueError:
                good = False
        if good and total == size:
            f.close()                   # (Windows cannot rename an open file)
            try:
                self._keep_mode(p, tmp)
                os.replace(tmp, p)
            except OSError as e:
                self.say("  FAILED: cannot rename %s to %s: %s"
                         % (tmp, p, e.strerror))
                return False
            self.say("  ok: received %d bytes into %s, verified (crc %04x)%s"
                     % (total, name_from_wire(name), crc,
                        self.rate(total)))
            tl = self.timing_line([("lisa", "the Lisa until its next frame"
                                            " starts"),
                                   ("frame", "the frame arrives"),
                                   ("server", "the server")])
            if tl:
                self.say(tl)
            return True
        if good:
            self.say("  upload discarded (size mismatch)")
        else:
            self.say("  FAILED: verify: the client said %r, we received %d %04x;"
                     " upload discarded" % (verdict, total, crc))
            if verdict == "HELLO":
                self.peer_restarted()
        return False

    @staticmethod
    def _keep_mode(p, tmp):
        """A PUT over an existing file keeps that file's permissions."""
        try:
            os.chmod(tmp, os.stat(p).st_mode & 0o7777)
        except OSError:
            pass                        # no older file (or no permission)

    def _recv_chunk(self, seq):
        """PUT: read one framed chunk, the one numbered SEQ, and check it.
        Returns what arrived:
          ("ok", payload, t_first, t_done)  chunk SEQ, CRC good (payload b""
                                            is the end-of-file chunk); the
                                            times are for the timing line
          ("dup", n)                        chunk n, an earlier one again
          ("bad", why, discard)             no good chunk: WHY for the debug
                                            line; DISCARD if bytes of a bad
                                            frame may still be arriving
          ("fail", line)                    the peer's FAIL line (it aborted)
        Raises PeerRestarted on a HELLO where a chunk should start."""
        # 1. First byte of a chunk, long timeout: if the whole chunk was
        #    lost, the peer retransmits it after T_REPLY, so wait longer
        #    than that to catch it.
        try:
            b = self.port.read(1, T_FRAME)
        except TimeoutError:
            return ("bad", "nothing arrived within %.0f s -> NAK" % T_FRAME, False)
        t_first = time.monotonic()      # a frame starts (timing)
        out_of_step = ("bad", "byte %r where a chunk should start (out of step)"
                       " -> discard, NAK" % b, True)
        if b == b"H":
            # possibly HELLO (the peer restarted)
            try:
                rest = self.port.read(4, T_BLOCK)
            except TimeoutError:
                rest = b""
            if b"H" + rest == b"HELLO":
                raise PeerRestarted("HELLO in place of chunk header")
            return out_of_step          # a corrupted header byte
        if b == b"F":
            line = "F" + self._read_rest_of_line()
            return ("fail", line) if line.startswith("FAIL") else out_of_step
        if b != b"T":
            return out_of_step          # a corrupted or partial frame
        # 2. Rest of the header, payload, CRC with short timeouts: a byte
        #    lost mid-chunk makes a read short.
        try:
            rest = self.port.read(4, T_BLOCK)
            got_seq = int.from_bytes(rest[0:2], "big")
            got_len = int.from_bytes(rest[2:4], "big")
            if got_len > CHUNK:
                # implausible length: a false sync
                return ("bad", "impossible length %d (out of step) -> discard, NAK"
                        % got_len, True)
            payload = (self.port.read(got_len, self.t_block(got_len))
                       if got_len else b"")
            crc_bytes = self.port.read(2, T_BLOCK)
            t_done = time.monotonic()   # the frame is in (timing)
        except TimeoutError:
            return ("bad", "incomplete (bytes lost) -> discard, NAK", True)
        # 3. Reassemble the whole received frame so the monkey wrench (if
        #    enabled) can flip a payload/CRC byte before the CRC check.
        rx = bytearray(b"T" + rest + payload + crc_bytes)
        self._wrench(rx, got_seq, "recv")
        got_crc = int.from_bytes(rx[-2:], "big")
        calc = crc16(bytes(rx[1:-2]))
        wire_seq = seq % SEQ_MOD
        if seq_behind(got_seq, wire_seq):
            return ("dup", got_seq)
        if got_seq != wire_seq:
            why = "chunk %d arrived, expected %d" % (got_seq, seq)
        elif got_crc != calc:
            why = "bad CRC (received %04x, computed %04x)" % (got_crc, calc)
        else:
            return ("ok", payload, t_first, t_done)
        # discard any stray tail (a byte lost in the header can leave the
        # frame short even when the reads succeeded); quiet if it was whole
        return ("bad", "rejected, %s -> NAK" % why, True)

    def _read_verdict(self, eofseq, done):
        """PUT: read the client's OK/BAD verdict after our DONE.  If the
        client sends the end-of-file chunk (number eofseq) again instead
        -- our ACK and DONE were lost, or its wait for them ran out before
        they arrived -- ACK it again and resend DONE, rather than reading
        the frame as a garbled verdict (which would delete a good file).
        Returns the verdict line, or None."""
        for _ in range(MAX_RETRIES):
            try:
                b = self.port.read(1, self.timeout)
            except TimeoutError:
                return None
            if b != b"T":
                line = (b + self._read_rest_of_line().encode(WIRE))
                line = line.rstrip(b"\r\n").decode(WIRE)
                self.dlog("received: " + line)
                return line
            try:
                rest = self.port.read(6, T_BLOCK)   # seq, len, crc of an empty chunk
            except TimeoutError:
                rest = b""
            if (len(rest) == 6
                    and int.from_bytes(rest[0:2], "big") == eofseq % SEQ_MOD
                    and rest[2:4] == b"\x00\x00"
                    and int.from_bytes(rest[4:6], "big") == crc16(rest[0:4])):
                self.dlog("chunk %d: end-of-file chunk received again (our ACK"
                          " or DONE was lost) -> ACK and DONE again" % eofseq)
                self._ack(eofseq)
                self.send_line(done)
            else:
                self.dlog("unexpected bytes instead of the verdict -> discarded")
                self._discard_until_quiet()
        return None

    # -- main loop ---------------------------------------------------------

    def cmd_quit(self, args):
        self.say("  ok: the client quit")
        raise ClientGone("quit")

    # the commands a client can send, and the method that serves each
    COMMANDS = {
        "HELLO": cmd_hello, "LIST": cmd_list, "GET": cmd_get, "PUT": cmd_put,
        "SUM": cmd_sum, "DELETE": cmd_delete, "BAUD": cmd_baud,
        "DEBUG": cmd_debug, "CAL": cmd_cal, "QUIT": cmd_quit,
        # hidden: the client's 'bench' command
        "MARK": cmd_mark, "ECHO": cmd_echo, "FRAMES": cmd_frames,
    }
    # every line a client sends: a command, or a protocol reply (which is
    # answered ERR 99 where no command is expected).  Any other line is
    # garbled, or sent at another rate (see BAUD).
    KNOWN = set(COMMANDS) | {"NEXT", "DONE", "OK", "BAD", "FAIL", "GO", "A", "N"}

    def handle_client(self):
        while True:
            line = self._pending       # a command that ended a listing early
            self._pending = None
            if line is None and self._confirm_until is not None:
                # a new rate on trial (BAUD): bytes at least every second,
                # a whole valid line, before the deadline -- else go back
                left = self._confirm_until - time.monotonic()
                line = self.read_line(max(0.05, min(1.0, left)), quiet=True)
                if line is None and time.monotonic() < self._confirm_until:
                    continue          # nothing yet: keep waiting
                if not line:
                    self.revert_baud(self._baud_old, "no client at %d baud"
                                     % self.port.baud if line is None else
                                     "a garbled line: the client is at another rate")
                    continue
            elif line is None:
                line = self.read_line(LINE_GAP, quiet=True)
            if line is None or line == "":
                if line == "" and self.port.baud != self.start_baud:
                    self.revert_baud(self.start_baud, "a garbled line")
                continue              # idle or partial line: keep waiting
            parts = line.split()
            cmd = parts[0].upper() if parts else ""
            if cmd not in self.KNOWN or any(c < " " or c == "\x7f" for c in line):
                # bytes sent at another rate: back to the old rate (on
                # trial) or to the startup rate, where the client may be
                if self._confirm_until is not None:
                    self.revert_baud(self._baud_old, "a garbled line: the "
                                     "client is at another rate")
                    continue
                if self.port.baud != self.start_baud:
                    self.revert_baud(self.start_baud, "a garbled line")
                    continue
            elif self._confirm_until is not None:
                self._confirm_until = None
                self.say("  baud: the client is there at %d baud" % self.port.baud)
            if cmd == "NEXT":
                # a late NEXT for a listing that is over (its MORE was
                # resent): nothing to answer
                self.dlog("late NEXT ignored")
                continue
            if cmd != "MARK":           # bench marks: cmd_mark reports them
                self.say("> " + line)
            try:
                serve = self.COMMANDS.get(cmd)
                if serve:
                    serve(self, parts[1:])
                else:
                    self.send_line("ERR 99 unknown command")
                    self.say("  error: unknown command")
            except ProtocolError as e:
                self.send_line("ERR %d %s" % (e.code, e.text))
                self.say("  error: %s" % e.text)
            except PeerRestarted:
                self.peer_restarted()
            except TimeoutError:
                self.abort("timeout")
            finally:
                # an empty line after each command's output, so the commands
                # stand apart; not after the bench's MARKs, whose lines
                # ('bench: ...') belong together, like their '> MARK' lines
                if cmd != "MARK":
                    self.say("")

    def run(self):
        self.say("%s listening on %s at %d baud, home_dir=%s, debug output %s"
                 % (PROTOCOL, self.args.port, self.args.baud, self.home_dir,
                    "on" if self.debug_on else "off"))
        try:
            self.send_banner()
            while True:
                try:
                    self.handle_client()
                except ClientGone as e:
                    if str(e) == "quit":
                        # the port is fine: the next client's HELLO is
                        # read as usual -- at the startup rate, where a
                        # new client starts too
                        self.revert_baud(self.start_baud, "the client quit")
                        self.say("waiting for a new client")
                        continue
                    # The other end of the port is gone (end-of-file or an
                    # error).  Reading again would fail at once, forever:
                    # say so ONCE, then wait quietly for it to come back.
                    self.say("serial port: the other end is gone (%s); "
                             "waiting for it to come back" % e)
                    self.port.wait_back()
                    self.say("serial port: the other end is back; "
                             "waiting for a client")
                    self.send_banner()
        except KeyboardInterrupt:
            pass
        finally:
            self.port.close()


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="LISA-SERIAL-FTP/v1.0 serial file server for the Apple Lisa RS-232 driver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  LisaEm, Serial B set to PseudoTTY named /tmp/lisaem-port-b:
    python3 LisaSerialFtpServer.py --port /tmp/lisaem-port-b --home_dir ~/lisa-share

  a real Lisa on a USB-serial adapter, with debug output:
    python3 LisaSerialFtpServer.py --port /dev/ttyUSB0 --baud 57600 \\
        --home_dir ~/lisa-share --debug

  the same on Windows (needs: pip install pyserial):
    python LisaSerialFtpServer.py --port COM3 --home_dir C:\\lisa-share

The baud rate must match the Lisa's (57600 by default); the client's 'baud'
command then changes it on both sides (BAUD), and QUIT goes back to it.""")
    ap.add_argument("--port", default="auto", metavar="DEVICE",
                    help="the serial device connected to the Lisa, e.g. "
                         "/dev/ttyUSB0 (Windows: COM3), or LisaEm's PseudoTTY "
                         "link such as /tmp/lisaem-port-b.  Default: auto = "
                         "find it: the only port there is, else a link to a "
                         "pty first, then LisaEm's pty, then a USB adapter, "
                         "then a built-in serial port (/dev/ttyS0).  "
                         "auto-pty: the same among the PseudoTTYs only (for "
                         "LisaEm); auto-serial: among the USB adapters and "
                         "built-in ports only (for a real Lisa)")
    ap.add_argument("--baud", type=int, default=57600, choices=sorted(BAUDS),
                    help="baud rate (default 57600, the Lisa's "
                         "maximum on Port B, the only port the client uses)")
    ap.add_argument("--home_dir", default=".",
                    help="the folder shared with the Lisa: its files and "
                         "subfolders are what the Lisa sees (default: the "
                         "current folder)")
    ap.add_argument("--timeout", type=float, default=15.0,
                    help="timeout in seconds for command/abort replies "
                         "(default 15); the per-chunk loss-recovery "
                         "timeouts are fixed (see T_FRAME/T_BLOCK/T_REPLY)")
    ap.add_argument("--read-only", action="store_true",
                    help="refuse PUT and DELETE")
    ap.add_argument("--monkey_wrench", action="store_true",
                    help="test mode: with 20%% probability, flip one random "
                         "byte of each data chunk (payload or CRC, never the "
                         "header) in both directions, to exercise the "
                         "CRC/NAK/retransmit path; prints a line to stderr "
                         "per flipped byte")
    ap.add_argument("--debug", action="store_true",
                    help="start with debug output on (a line per protocol "
                         "line and per chunk, to stderr); the client's "
                         "'debug on|off' switches it, HELLO restores this "
                         "setting"),
    args = ap.parse_args()
    if not os.path.isdir(args.home_dir):
        ap.error("--home_dir: no such folder: %s" % args.home_dir)
    try:
        if args.port in AUTO_MODES and not os.environ.get("LISAFTP_FD"):
            args.port = auto_port(args.port)
        LisaFtpServer(args).run()
    except KeyboardInterrupt:
        pass                        # Ctrl-C while waiting for the port
    except PORT_SETUP_ERRORS as e:
        sys.exit("cannot set up %s: %s" % (args.port, e))
    except OSError as e:
        sys.exit("cannot open %s: %s" % (args.port, e))


if __name__ == "__main__":
    main()
