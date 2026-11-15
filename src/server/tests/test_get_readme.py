#!/usr/bin/env python3
"""
test_get_readme.py -- Python simulation of the GET path of LisaSerialFtpClient.text.

Mirrors the Pascal client code in LisaSerialFtpClient.text (the table-driven CRC16,
RecvChunk, DoGet text mode, retry/NAK logic, the DONE -> OK/BAD
verification and the FAIL handshake) and runs it against a fresh
LisaSerialFtpServer.py instance on a pty pair, GETting the real README.md
from this directory.

Usage:
  python3 test_get_readme.py               clean link: expect success
  python3 test_get_readme.py --drop-0xff   simulate the lisaem PseudoTTY
                                           bug (0xFF bytes dropped on
                                           receive): every chunk whose
                                           frame holds a 0xFF byte loses
                                           it, so the GET must fail (or
                                           succeed if no frame holds one).
"""

import os
import pty
import select
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)              # the project folder (tests/..)
SERVER = os.path.join(TOP, "LisaSerialFtpServer.py")
REMOTE = "README.md"
sys.path.insert(0, TOP)
from LisaSerialFtpServer import lisa_pages   # noqa: E402

MAXRETRY = 10
MAXLINE = 255
TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# CRC16 -- port of the Pascal CrcInit / CRC16: the bit-by-bit CRC of each
# byte value is built once into tables (crchi/crclo), and the CRC is kept
# as two bytes hi/lo; the 8-bit XORs use a 4-bit XOR table (nibxor) on the
# nibbles of each byte (nibhi/niblo), as Lisa Pascal has no XOR operator.
# Same signed/unsigned INTEGER handling (C is -32768..32767).
# ---------------------------------------------------------------------------

def xor16(a, b):
    """Pascal Xor16: bitwise XOR of two 0..65535 values (bit by bit)."""
    r = 0
    bit = 1
    for _ in range(16):
        if (a & 1) != (b & 1):
            r += bit
        a >>= 1
        b >>= 1
        bit <<= 1
    return r


def _crc_init():
    """Pascal CrcInit."""
    nibhi = [i // 16 for i in range(256)]
    niblo = [i % 16 for i in range(256)]
    nibxor = [[0] * 16 for _ in range(16)]
    for a in range(16):
        for b in range(16):
            r, bit, x, y = 0, 1, a, b
            for _ in range(4):
                if (x % 2) != (y % 2):
                    r += bit
                x //= 2
                y //= 2
                bit *= 2
            nibxor[a][b] = r
    crchi = [0] * 256
    crclo = [0] * 256
    for i in range(256):
        c = i * 256
        for _ in range(8):
            if c >= 32768:
                c = xor16((c % 32768) * 2, 4129)   # (C SHL 1) XOR $1021
            else:
                c = c * 2
        crchi[i] = c // 256
        crclo[i] = c % 256
    return nibhi, niblo, nibxor, crchi, crclo


NIBHI, NIBLO, NIBXOR, CRCHI, CRCLO = _crc_init()


def crc16(data, c):
    """Pascal CRC16(DATA, L, C): returns the updated signed 16-bit CRC."""
    u = c + 65536 if c < 0 else c
    hi = u // 256
    lo = u % 256
    for v in data:                        # v = DATA^ as 0..255
        x = NIBXOR[NIBHI[hi]][NIBHI[v]] * 16 + NIBXOR[NIBLO[hi]][NIBLO[v]]
        t = CRCHI[x]
        hi = NIBXOR[NIBHI[lo]][NIBHI[t]] * 16 + NIBXOR[NIBLO[lo]][NIBLO[t]]
        lo = CRCLO[x]
    u = hi * 256 + lo
    return u - 65536 if u >= 32768 else u


# ---------------------------------------------------------------------------
# The Lisa side of the link
# ---------------------------------------------------------------------------

class LisaSim:
    """Emulates the Lisa RS-232 side, mirroring LisaSerialFtpClient.text's GET path."""

    def __init__(self, fd, drop_0xff=False):
        self.fd = fd
        self.drop_0xff = drop_0xff
        self.linkdead = False
        self.dropped = 0
        self.last_payload = b""
        self.first_mismatch = None        # (rseq, rlen, ccrc, cu, seqmatch)

    # -- serial primitives (GetChar / GetBlock / SendLine / ReadLine) ------

    def get_char(self, timeout=TIMEOUT):
        """Pascal GetChar: one byte, blocking.  With drop_0xff, 0xFF bytes
        are silently lost (the lisaem PseudoTTY signed-char bug)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no byte from server")
            r, _, _ = select.select([self.fd], [], [], min(remaining, 0.2))
            if r:
                b = os.read(self.fd, 1)
                if not b:
                    raise EOFError("pty closed")
                v = b[0]
                if self.drop_0xff and v == 0xFF:
                    self.dropped += 1
                    continue             # byte lost on the wire
                return v

    def get_block(self, n, timeout=TIMEOUT):
        """Pascal GetBlock: exactly n bytes (READ_DATA waits for full count)."""
        out = bytearray()
        for _ in range(n):
            out.append(self.get_char(timeout))
        return bytes(out)

    def send_line(self, s):
        os.write(self.fd, s.encode("ascii") + b"\r\n")

    def read_line(self, timeout=TIMEOUT):
        """Pascal ReadLine: CR/LF stripped, at most MAXLINE chars kept."""
        n = 0
        s = []
        while True:
            c = self.get_char(timeout)
            if c != 10 and c != 13 and n < MAXLINE:
                n += 1
                s.append(chr(c))
            if c == 10:
                break
        return "".join(s)


def recv_chunk(sim, seq):
    """Mirror of Pascal RecvChunk.  Returns (ok, rlen, seq): like the
    Pascal VAR parameter, seq is incremented on success."""
    hdr = sim.get_block(5)
    if hdr[0] != 84:                      # 84 = 'T'
        print("DEBUG RecvChunk: hdr[1]=%d (not T)" % hdr[0])
        r = ""
        while True:
            ch = sim.get_char()
            if ch not in (10, 13) and len(r) < MAXLINE:
                r += chr(ch)
            if ch == 10:
                break
        if r and r[0] == "F":
            sim.send_line("OK")
            sim.linkdead = True
        else:
            sim.send_line("N %d" % seq)
        return False, 0, seq
    rseq = hdr[1] * 256 + hdr[2]
    rlen = hdr[3] * 256 + hdr[4]
    data = sim.get_block(rlen) if rlen > 0 else b""
    ccrc = sim.get_char() * 256 + sim.get_char()
    # chained CRC: header (seq+len) first, then the payload
    c = 0xFFFF
    c = crc16(hdr[1:5], c)
    if rlen > 0:
        c = crc16(data, c)
    cu = c + 65536 if c < 0 else c
    print("DEBUG RecvChunk: rseq=%d rlen=%d ccrc=%d cu=%d S=%d crcmatch=%s seqmatch=%s"
          % (rseq, rlen, ccrc, cu, seq,
             "TRUE" if cu == ccrc else "FALSE",
             "TRUE" if rseq == seq else "FALSE"))
    if sim.first_mismatch is None and not (rseq == seq and cu == ccrc):
        sim.first_mismatch = (rseq, rlen, ccrc, cu, rseq == seq)
    if rseq == seq and cu == ccrc:
        sim.send_line("A %d" % seq)
        sim.last_payload = data
        return True, rlen, seq + 1
    sim.send_line("N %d" % seq)
    return False, 0, seq


def page_lines(pages):
    """The lines of Lisa text pages (what the client stores after its header
    page): each page up to its first CR-NUL, DLE + CHR(32 + n) at the start
    of a line = n spaces."""
    lines = []
    for k in range(0, len(pages), 1024):
        pg = pages[k:k + 1024]
        p, line, at = 0, bytearray(), True
        while p < len(pg):
            v = pg[p]
            p += 1
            if v == 13:
                lines.append(bytes(line))
                line, at = bytearray(), True
                if p < len(pg) and pg[p] == 0:
                    break
            elif v == 16 and at and p < len(pg) and pg[p] - 32 > 0:
                line += b" " * (pg[p] - 32)
                p += 1
                at = False
            else:
                line.append(v)
                at = False
    return lines


def do_get(sim, remote, binary=False):
    """Mirror of Pascal DoGet: a text get asks for the finished Lisa text
    pages (GET <name> T) and stores them as they come, like a binary get.
    Returns (ok, total, size, lines): lines decoded from the pages."""
    sim.send_line("GET " + remote + ("" if binary else " T"))
    l = sim.read_line()
    if not l or l[0] != "O":
        print(l)
        return False, 0, 0, []
    size = 0                                # StrToInt (positional)
    for ch in l[3:]:
        if ch.isdigit():
            size = size * 10 + int(ch)
    sim.send_line("GO")                     # the local file is open: ready
    seq = 0
    crc = 0xFFFF
    total = 0
    received = bytearray()
    sim.linkdead = False
    ok = True
    while True:
        ok = False
        ln = 0
        retry = 1
        while retry <= MAXRETRY and not ok and not sim.linkdead:
            ok, ln, seq = recv_chunk(sim, seq)
            print("DEBUG DoGet: ok=%s seq=%d len=%d retry=%d"
                  % ("TRUE" if ok else "FALSE", seq, ln, retry))
            retry += 1
        if ok and ln > 0:
            data = sim.last_payload
            received += data              # stored as it comes (obuf, OutFlush)
            crc = crc16(data, crc)
            total += ln
        if not ok or ln == 0:
            break
    lines = [] if binary else page_lines(bytes(received))
    if ok:
        cu = crc + 65536 if crc < 0 else crc
        sim.send_line("DONE %d %04X" % (total, cu))
        # Pascal Matches(l, 'OK', total, crc): keep the file only on
        # 'OK <our bytes> <our crc>' (hex in any case)
        verdict = sim.read_line()
        if verdict.upper() != "OK %d %04X" % (total, cu):
            print("VERIFY FAILED: received %d bytes, CRC %04X; the server says: %s"
                  % (total, cu, verdict))
            ok = False
        else:
            print("received %d pages of Lisa text, verified (CRC %04X)"
                  % (total // 1024, cu))
    else:
        # AbortTransfer
        if not sim.linkdead:
            sim.send_line("FAIL get aborted")
            sim.read_line()                 # the peer's FAIL
            sim.send_line("OK")
        print("transfer failed")
    return ok, total, size, lines


def connect(sim):
    """Mirror of Pascal Connect (HELLO + banner)."""
    sim.send_line("HELLO")
    for _ in range(10):
        l = sim.read_line()
        print("server line: [%s]" % l)
        if l and l[0] == "H":            # the banner: 'Hi from ... LISA-SERIAL-FTP/v1.0 ...'
            return True
    return False


# ---------------------------------------------------------------------------

def main():
    drop = "--drop-0xff" in sys.argv[1:]
    filedata = open(os.path.join(TOP, REMOTE), "rb").read()
    print("== test_get_readme.py: simulating LisaSerialFtpClient.text 'get %s' ==" % REMOTE)
    print("   file size: %d bytes, link: %s\n"
          % (len(filedata), "DROPPING 0xFF BYTES (lisaem bug)" if drop
             else "clean"))

    master, slave = pty.openpty()
    env = dict(os.environ, LISAFTP_FD=str(slave))
    # --timeout 2: in drop mode the first retry needs the server's
    # retransmit-after-timeout, exactly like the real link (15 s there).
    server_args = [sys.executable, SERVER, "--port", "ignored",
                   "--baud", "9600", "--root", TOP,
                   "--timeout", "2", "--debug"]
    proc = subprocess.Popen(server_args, pass_fds=[slave], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    os.close(slave)
    time.sleep(0.5)

    sim = LisaSim(master, drop_0xff=drop)
    failures = []

    def check(name, cond, extra=""):
        print("%-38s %s %s" % (name, "PASS" if cond else "FAIL", extra))
        if not cond:
            failures.append(name)

    # initial banner, then HELLO
    for _ in range(50):
        line = sim.read_line()
        if line.startswith("H") and "LISA-SERIAL-FTP/v1.0" in line:
            break
    else:
        print("no banner from server")
        return 1
    check("connect", connect(sim))

    t0 = time.monotonic()
    ok, total, size, lines = do_get(sim, REMOTE, binary=False)
    dt = time.monotonic() - t0

    sim.send_line("QUIT")
    time.sleep(0.3)
    proc.terminate()                        # the server runs until stopped
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    out = proc.stdout.read().decode(errors="replace")
    for line in out.splitlines():
        if "WARNING" in line or "Traceback" in line:
            print("   server log: " + line)
            failures.append("server-log")

    print()
    if drop:
        # README.md is text, so a 0xFF can only be in a frame's CRC bytes.
        # Expect: if any byte was dropped, the frame fails its CRC check
        # (with the right sequence number) on every retransmit and the
        # GET fails; with no 0xFF in any frame it simply succeeds.
        fm = sim.first_mismatch
        if sim.dropped:
            check("repro-crc-mismatch", fm is not None and fm[4],
                  "first mismatch: rseq=%s rlen=%s ccrc=%s cu=%s seqmatch=%s"
                  % (fm[0], fm[1], fm[2], fm[3], fm[4]) if fm else "no mismatch")
            check("repro-fails", not ok, "transfer unexpectedly succeeded")
        else:
            check("no-0xff-so-ok", ok, "no 0xFF on the wire, yet the GET failed")
        print("\n   (0xFF bytes dropped on the wire: %d)" % sim.dropped)
        print("   elapsed: %.1f s" % dt)
    else:
        check("get-ok", ok)
        pages = lisa_pages(filedata)
        check("get-size", size == len(pages) and size % 1024 == 0,
              "server said %d (%d pages)" % (size, size // 1024))
        check("get-total", total == size, "received %d" % total)
        # text round-trip: the pages hold README.md's lines exactly
        expected = filedata.split(b"\n")
        if filedata.endswith(b"\n"):
            expected.pop()
        check("text-roundtrip", lines == expected,
              "%d lines" % len(lines))
        print("   elapsed: %.1f s" % dt)

    print()
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
