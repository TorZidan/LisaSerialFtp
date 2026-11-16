#!/usr/bin/env python3
"""
test_pyserial_port.py -- the server's Windows serial port code
(PySerialPort, through pyserial), run on Linux / macOS: LISAFTP_PYSERIAL=1
makes the server use it there, on a pty opened by its name like a COM
port.  A short session: banner, HELLO, GET (whole file, verified), PUT,
BAUD (the switch and its HELLO), QUIT.  Skipped if pyserial is not
installed (it is needed only on Windows).

Run:  python3 tests/test_pyserial_port.py
"""

import atexit
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/server
SERVER = os.path.join(TOP, "LisaSerialFtpServer.py")
sys.path.insert(0, TOP)
from LisaSerialFtpServer import crc16   # noqa: E402

failures = []


def check(name, cond, info=""):
    print("%-30s %s %s" % (name, "PASS" if cond else "FAIL", info))
    if not cond:
        failures.append(name)


class Link:
    def __init__(self, fd):
        self.fd = fd
        self.buf = b""

    def fill(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return False
        self.buf += os.read(self.fd, 4096)
        return True

    def read_line(self, timeout=10):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            if not self.fill(max(0, deadline - time.monotonic())):
                return None
        line, _, self.buf = self.buf.partition(b"\n")
        return line.rstrip(b"\r").decode("ascii", "replace")

    def read_bytes(self, n, timeout=10):
        deadline = time.monotonic() + timeout
        while len(self.buf) < n:
            if not self.fill(max(0, deadline - time.monotonic())):
                break
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, line):
        os.write(self.fd, line.encode("ascii") + b"\r\n")

    def ask(self, line):
        self.send(line)
        return self.read_line()


def frame(seq, data):
    body = seq.to_bytes(2, "big") + len(data).to_bytes(2, "big") + data
    return b"T" + body + crc16(body).to_bytes(2, "big")


def main():
    try:
        import serial  # noqa: F401
    except ImportError:
        print("pyserial is not installed: skipped (it is needed only on Windows)")
        return 0
    root = tempfile.mkdtemp(prefix="lisaftp_pyser_")
    atexit.register(shutil.rmtree, root, True)   # deleted when the test ends
    data = os.urandom(2500)
    with open(os.path.join(root, "data.bin"), "wb") as f:
        f.write(data)
    master, slave = pty.openpty()
    name = os.ttyname(slave)              # opened by its name, like COM3
    env = dict(os.environ, LISAFTP_PYSERIAL="1")
    env.pop("LISAFTP_FD", None)
    proc = subprocess.Popen(
        [sys.executable, SERVER, "--port", name, "--home_dir", root],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    link = Link(master)
    try:
        banner = link.read_line()
        check("banner", banner is not None and banner.startswith("Hi from"), repr(banner)[:40])
        r = link.ask("HELLO")
        check("hello", r is not None and r.startswith("Hi from"))
        # GET: every chunk, ACKed, then DONE / OK
        r = link.ask("GET data.bin")
        check("get-ok", r == "OK %d" % len(data), repr(r))
        link.send("GO")
        got = b""
        seq = 0
        while True:
            hdr = link.read_bytes(5)
            n = int.from_bytes(hdr[3:5], "big")
            body = link.read_bytes(n + 2)
            link.send("A %d" % seq)
            if n == 0:
                break
            got += body[:-2]
            seq += 1
        link.send("DONE %d %04x" % (len(got), crc16(got)))
        r = link.read_line()
        check("get-verified", got == data and r is not None and r.startswith("OK "), repr(r))
        # PUT: two chunks and the end-of-file chunk
        up = os.urandom(1500)
        r = link.ask("PUT up.bin %d" % len(up))
        acks = []
        if r == "OK":
            for i, part in enumerate((up[:1016], up[1016:], b"")):
                os.write(master, frame(i, part))
                acks.append(link.read_line())
        done = link.read_line()
        if done and done.startswith("DONE"):
            link.send("OK %d %04x" % (len(up), crc16(up)))
        time.sleep(0.5)
        with open(os.path.join(root, "up.bin"), "rb") as f:
            stored = f.read()
        check("put", acks == ["A 0", "A 1", "A 2"] and stored == up, repr(acks))
        # BAUD: the switch, then a HELLO at the new rate
        r = link.ask("BAUD 19200")
        time.sleep(0.5)
        r2 = link.ask("HELLO")
        check("baud", r == "OK 19200" and r2 is not None and r2.startswith("Hi from"),
              repr((r, r2))[:60])
        link.send("QUIT")
        time.sleep(0.5)
        r = link.ask("HELLO")
        check("quit-then-hello", r is not None and r.startswith("Hi from"))
        link.send("QUIT")
        time.sleep(0.3)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        os.close(master)
        os.close(slave)

    print()
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
