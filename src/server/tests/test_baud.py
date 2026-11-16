#!/usr/bin/env python3
"""
test_baud.py -- BAUD (the client's 'baud' command): the server switches its
port to the new rate, keeps it only if the client then talks at it, and
goes back otherwise.

The server runs on a pty.  A pty has no real line speed, but it keeps the
one set on it, so the test reads the server's rate from its end of the pty.

  1. BAUD 19200: 'OK 19200', then the port is at 19200; a HELLO at it is
     answered (the rate is kept).
  2. A garbled line during the trial (bytes sent at another rate): back to
     the rate before at once.
  3. Nothing during the trial: back to the rate before after BAUD_TRIAL.
  4. A garbled line later, at a rate that is not the startup rate: back to
     the startup rate (where a restarted client starts).
  5. QUIT: back to the startup rate for the next client.
  6. Bad requests: an unsupported rate (ERR 21), not a number (ERR 1); the
     same rate again is simply OK.

Run:  python3 tests/test_baud.py
"""

import atexit
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tests/..
SERVER = os.path.join(TOP, "LisaSerialFtpServer.py")
sys.path.insert(0, TOP)

failures = []


def speed(baud):
    """termios' code for a rate, as the server sets it on its port."""
    return getattr(termios, "B%d" % baud)


def check(name, cond, info=""):
    print("%-30s %s %s" % (name, "PASS" if cond else "FAIL", info))
    if not cond:
        failures.append(name)


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
        return line.rstrip(b"\r").decode("ascii", "replace")

    def send(self, data):
        os.write(self.fd, data)

    def ask(self, line):
        self.send(line.encode("ascii") + b"\r\n")
        return self.read_line()


def main():
    root = tempfile.mkdtemp(prefix="lisaftp_baud_")
    atexit.register(shutil.rmtree, root, True)   # deleted when the test ends
    master, slave = pty.openpty()
    env = dict(os.environ, LISAFTP_FD=str(slave))
    proc = subprocess.Popen(
        [sys.executable, SERVER, "--port", "x", "--home_dir", root, "--baud", "57600"],
        pass_fds=[slave], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # the slave stays open here too: its line speed is the server's rate
    link = Link(master)

    def rate():
        return termios.tcgetattr(slave)[5]

    try:
        banner = link.read_line()
        check("banner", banner is not None and banner.startswith("H"), repr(banner)[:40])
        check("start-57600", rate() == speed(57600))

        # 1. switch, then talk at the new rate
        r = link.ask("BAUD 19200")
        time.sleep(0.5)
        check("baud-ok", r == "OK 19200", repr(r))
        check("baud-switched", rate() == speed(19200))
        r = link.ask("HELLO")
        check("baud-hello-answered", r is not None and r.startswith("H"), repr(r)[:40])
        time.sleep(1.5)
        check("baud-kept", rate() == speed(19200))

        # 2. a garbled line during the trial: back at once
        r = link.ask("BAUD 9600")
        time.sleep(0.5)
        check("trial-switched", r == "OK 9600" and rate() == speed(9600), repr(r))
        link.send(b"\xff\x13\x80\xfe")         # no line end: a partial, garbled line
        time.sleep(2.0)
        check("garbled-back", rate() == speed(19200))
        r = link.ask("HELLO")
        check("garbled-then-hello", r is not None and r.startswith("H"), repr(r)[:40])

        # 3. nothing during the trial: back after BAUD_TRIAL (10 s)
        r = link.ask("BAUD 4800")
        time.sleep(0.5)
        check("silent-switched", r == "OK 4800" and rate() == speed(4800), repr(r))
        time.sleep(11.0)
        check("silent-back", rate() == speed(19200))
        r = link.ask("HELLO")
        check("silent-then-hello", r is not None and r.startswith("H"), repr(r)[:40])

        # 4. a garbled line later, not at the startup rate: back to it
        link.send(b"\x80\x81\x82\r\n")
        time.sleep(0.5)
        check("garbled-to-start", rate() == speed(57600))

        # 5. QUIT: back to the startup rate
        r = link.ask("BAUD 38400")
        time.sleep(0.5)
        r2 = link.ask("HELLO")
        check("quit-switched", r == "OK 38400" and rate() == speed(38400)
              and r2 is not None and r2.startswith("H"), repr((r, r2))[:60])
        link.send(b"QUIT\r\n")
        time.sleep(0.5)
        check("quit-back", rate() == speed(57600))
        r = link.ask("HELLO")
        check("quit-then-hello", r is not None and r.startswith("H"), repr(r)[:40])

        # 6. bad requests
        r = link.ask("BAUD 115200")
        check("unsupported", r is not None and r.startswith("ERR 21 "), repr(r))
        r = link.ask("BAUD fast")
        check("usage", r is not None and r.startswith("ERR 1 "), repr(r))
        r = link.ask("BAUD 57600")
        time.sleep(0.5)
        check("same-rate", r == "OK 57600" and rate() == speed(57600), repr(r))
        r = link.ask("HELLO")
        check("same-rate-hello", r is not None and r.startswith("H"), repr(r)[:40])
        link.send(b"QUIT\r\n")
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
