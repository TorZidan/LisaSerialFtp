#!/usr/bin/env python3
"""
test_sum.py -- the SUM command (LISA-SERIAL-FTP/v1.0), behind the client's get -c
and put -c: "SUM name T|B" answers "OK <size> <crc16> <sum16>".

  1. lisa_text (the server's model of a text file after a round trip
     through the Lisa) against a line-by-line copy of what the client
     does: DoGet stores the text as Lisa lines, TextPass sends each line
     back with LF.  Random texts with LF, CRLF and lone CR endings, empty
     lines, long lines (over 255 chars) and no final line ending: the two
     must give exactly the same bytes.
  2. lisa_text is stable: a file that came from a put (already in the
     Lisa's form) is left as it is, so put -c after put finds it the same.
  3. The server over a pty: SUM B (bytes as they are), SUM T on a CRLF
     file (equals the LF version), a missing file (ERR 11), bad usage
     (ERR 1); and MARK (the client's hidden bench command), which
     answers with the ms since the previous MARK, ECHO, which sends n
     short lines back, and FRAMES, which sends n chunks as a GET does;
     and a PUT that ends short of its announced size (ERR 5, not kept).

Run:  python3 tests/test_sum.py
"""

import os
import pty
import random
import select
import subprocess
import sys
import tempfile
import time

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tests/..
SERVER = os.path.join(TOP, "LisaSerialFtpServer.py")
sys.path.insert(0, TOP)
from LisaSerialFtpServer import lisa_text, crc16, CHUNK as srv_chunk   # noqa: E402

failures = []


def check(name, cond, info=""):
    print("%-30s %s %s" % (name, "PASS" if cond else "FAIL", info))
    if not cond:
        failures.append(name)


def client_get_lines(data):
    """DoGet, text mode, byte by byte: the Lisa text lines it writes."""
    lines = []
    linebuf = bytearray()
    pend_cr = False
    for b in data:
        if b == 10:
            lines.append(bytes(linebuf))
            linebuf.clear()
            pend_cr = False
        else:
            if pend_cr:
                lines.append(bytes(linebuf))
                linebuf.clear()
            if b == 13:
                pend_cr = True
            else:
                pend_cr = False
                if len(linebuf) >= 255:
                    lines.append(bytes(linebuf))      # linelen := 1 below
                    linebuf = bytearray()
                linebuf.append(b)
    if pend_cr:
        lines.append(bytes(linebuf))
    elif linebuf:
        lines.append(bytes(linebuf))
    return lines


def client_put_bytes(lines):
    """TextPass: each stored line comes back followed by LF."""
    return b"".join(l + b"\n" for l in lines)


def random_text(rng):
    parts = []
    for _ in range(rng.randint(0, 12)):
        kind = rng.random()
        if kind < 0.1:
            n = rng.randint(250, 600)                 # a long line
        else:
            n = rng.randint(0, 20)
        parts.append(bytes(rng.choice(b"ab c") for _ in range(n)))
        parts.append(rng.choice([b"\n", b"\r\n", b"\r", b"\n\n", b"\r\r", b""]))
    return b"".join(parts)


def sum16(data):
    return sum(data) & 0xFFFF


class Link:
    def __init__(self, fd):
        self.fd = fd
        self.buf = b""

    def read_line(self, timeout=10):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                return None
            self.buf += os.read(self.fd, 4096)
        line, _, self.buf = self.buf.partition(b"\n")
        return line.rstrip(b"\r").decode("ascii")

    def read_bytes(self, n, timeout=10):
        deadline = time.monotonic() + timeout
        while len(self.buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                break
            self.buf += os.read(self.fd, 4096)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def ask(self, line):
        os.write(self.fd, line.encode("ascii") + b"\r\n")
        return self.read_line()


def main():
    # 1. lisa_text equals the client's get + put
    rng = random.Random(1234)
    bad = None
    for _ in range(3000):
        data = random_text(rng)
        if lisa_text(data) != client_put_bytes(client_get_lines(data)):
            bad = data
            break
    check("model-matches-client", bad is None, repr(bad)[:80] if bad else "3000 texts")
    for name, data, want in [
            ("lf", b"a\nb\n", b"a\nb\n"),
            ("crlf", b"a\r\nb\r\n", b"a\nb\n"),
            ("lone-cr", b"a\rb\r", b"a\nb\n"),
            ("no-final-eol", b"a\nb", b"a\nb\n"),
            ("empty", b"", b""),
            ("only-cr", b"\r", b"\n"),
            ("255-chars", b"x" * 255 + b"\n", b"x" * 255 + b"\n"),
            ("256-chars", b"x" * 256 + b"\n", b"x" * 255 + b"\nx\n")]:
        got = lisa_text(data)
        check("case-" + name, got == want and got == client_put_bytes(client_get_lines(data)),
              repr(got)[:60])

    # 2. stable on its own output
    rng = random.Random(99)
    stable = all(lisa_text(lisa_text(d)) == lisa_text(d)
                 for d in (random_text(rng) for _ in range(1000)))
    check("model-stable", stable)

    # 3. the server
    root = tempfile.mkdtemp(prefix="lisaftp_sum_")
    lf = b"program x;\nbegin\n  writeln('hi');\nend.\n"
    with open(os.path.join(root, "unix.text"), "wb") as f:
        f.write(lf)
    with open(os.path.join(root, "dos.text"), "wb") as f:
        f.write(lf.replace(b"\n", b"\r\n"))
    binary = bytes(range(256)) * 3
    with open(os.path.join(root, "data.bin"), "wb") as f:
        f.write(binary)

    master, slave = pty.openpty()
    env = dict(os.environ, LISAFTP_FD=str(slave))
    proc = subprocess.Popen(
        [sys.executable, SERVER, "--port", "x", "--root", root],
        pass_fds=[slave], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(slave)
    link = Link(master)
    try:
        banner = link.read_line()
        check("banner-version", banner is not None and "LISA-SERIAL-FTP/v1.0" in banner, repr(banner)[:60])
        want_b = "OK %d %04x %04x" % (len(binary), crc16(binary), sum16(binary))
        check("sum-binary", link.ask("SUM data.bin B") == want_b, want_b)
        want_t = "OK %d %04x %04x" % (len(lf), crc16(lf), sum16(lf))
        check("sum-text-lf", link.ask("SUM unix.text T") == want_t, want_t)
        check("sum-text-crlf-same", link.ask("SUM dos.text T") == want_t)
        r = link.ask("SUM dos.text B")
        check("sum-crlf-as-binary-differs", r is not None and r != want_t, repr(r))
        r = link.ask("SUM nothere.text T")
        check("sum-missing", r is not None and r.startswith("ERR 11"), repr(r))
        r = link.ask("SUM unix.text")
        check("sum-usage", r is not None and r.startswith("ERR 1 "), repr(r))
        # MARK (the client's bench command): ms since the previous MARK
        link.ask("MARK -")
        time.sleep(0.3)
        r = link.ask("MARK test")
        ms = int(r.split()[1]) if r and r.startswith("OK ") else -1
        check("mark-elapsed", 250 <= ms <= 1000, repr(r))
        # ECHO (bench): n lines 'A 12345' and nothing else
        os.write(master, b"ECHO 3\r\n")
        lines = [link.read_line() for _ in range(3)]
        check("echo-lines", lines == ["A 12345"] * 3, repr(lines))
        r = link.ask("ECHO 0")
        check("echo-usage", r is not None and r.startswith("ERR 1 "), repr(r))
        # FRAMES (bench): n chunks + the empty end-of-file chunk, ACKed
        os.write(master, b"FRAMES 2\r\n")
        good = True
        for seq in range(3):
            fr = link.read_bytes(5)
            n = int.from_bytes(fr[3:5], "big") if len(fr) == 5 else -1
            body = link.read_bytes(n + 2) if n >= 0 else b""
            good = good and fr[:1] == b"T" and int.from_bytes(fr[1:3], "big") == seq \
                and n == (srv_chunk if seq < 2 else 0) \
                and int.from_bytes(body[-2:], "big") == crc16(fr[1:5] + body[:-2])
            os.write(master, b"A %d\r\n" % seq)
        check("frames", good)
        r = link.ask("FRAMES 0")
        check("frames-usage", r is not None and r.startswith("ERR 1 "), repr(r))
        # PUT that ends short of the announced size: refused, file not kept
        def frame(seq, data):
            body = seq.to_bytes(2, "big") + len(data).to_bytes(2, "big") + data
            return b"T" + body + crc16(body).to_bytes(2, "big")
        r = link.ask("PUT short.bin 512")
        acks = []
        if r == "OK":
            os.write(master, frame(0, bytes(256)))
            acks.append(link.read_line())
            os.write(master, frame(1, b""))
            acks.append(link.read_line())
        done = link.read_line()
        os.write(master, b"BAD 256 0000\r\n")
        time.sleep(0.5)
        check("put-short-refused", r == "OK" and done is not None and done.startswith("ERR 5 ")
              and not os.path.exists(os.path.join(root, "short.bin")), repr((acks, done)))
        os.write(master, b"QUIT\r\n")
        time.sleep(0.3)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        os.close(master)

    print()
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
