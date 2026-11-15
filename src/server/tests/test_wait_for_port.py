#!/usr/bin/env python3
"""
test_wait_for_port.py -- the server waits for a serial port that is not
there yet (LisaEm, which creates its virtual port, not running), instead
of stopping with "No such file or directory".

  1. Start the server with --port pointing at a path that does not exist.
     It must keep running and say it is waiting.
  2. Create the port: a pty, with a symlink at that path to its slave end
     (as an emulator's virtual port).  The server must open it and send
     its banner.
  3. A path that exists but is not a serial device (a plain file) is still
     an error: the server stops at once.

Run:  python3 tests/test_wait_for_port.py
"""

import os
import pty
import select
import subprocess
import sys
import tempfile
import time

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tests/..
SERVER = os.path.join(TOP, "LisaSerialFtpServer.py")

failures = []


def check(name, cond, info=""):
    print("%-28s %s %s" % (name, "PASS" if cond else "FAIL", info))
    if not cond:
        failures.append(name)


def read_until(fd, needle, timeout):
    """Read from fd until needle is seen or timeout; returns the bytes."""
    buf = b""
    deadline = time.monotonic() + timeout
    while needle not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        r, _, _ = select.select([fd], [], [], left)
        if not r:
            break
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def main():
    tmp = tempfile.mkdtemp(prefix="lisaftp_wait_")
    port = os.path.join(tmp, "b")
    env = dict(os.environ)
    env.pop("LISAFTP_FD", None)

    # 1. the port is not there: the server waits
    proc = subprocess.Popen(
        [sys.executable, SERVER, "--port", port, "--root", tmp],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    out = read_until(proc.stdout.fileno(), b"waiting for it", 5)
    check("waits-message", b"is not there" in out and b"waiting for it" in out,
          repr(out[:120]))
    time.sleep(1.5)
    check("waits-still-running", proc.poll() is None)

    # 2. the port appears (pty slave behind a symlink): banner arrives
    master, slave = pty.openpty()
    os.symlink(os.ttyname(slave), port)
    banner = read_until(master, b"\n", 10)
    check("found-banner", banner.startswith(b"Hi from") and b"LISA-SERIAL-FTP/v1." in banner,
          repr(banner[:60]))
    out = read_until(proc.stdout.fileno(), b"found it", 3)
    check("found-message", b"found it" in out, repr(out[:80]))
    proc.terminate()
    proc.wait(timeout=5)
    os.close(master)
    os.close(slave)
    os.remove(port)

    # 3. a plain file is not a serial port: still an error at once
    with open(port, "w") as f:
        f.write("x")
    proc = subprocess.Popen(
        [sys.executable, SERVER, "--port", port, "--root", tmp],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        out, _ = proc.communicate(timeout=10)
        check("plain-file-error", proc.returncode not in (0, None)
              and b"cannot" in out, repr(out[-100:]))
    except subprocess.TimeoutExpired:
        proc.kill()
        check("plain-file-error", False, "the server kept running")

    print()
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
