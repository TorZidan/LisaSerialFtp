#!/usr/bin/env python3
"""
test_list_paging.py -- LIST is sent in pages (LISA-SERIAL-FTP/v1.0), so a real Lisa,
whose serial driver keeps at most 512 unread bytes and drops the rest,
can keep up with a long listing at any speed.

The server runs on a pty with a folder of 60 files (a listing of about
1,700 bytes, like the one that overflowed a real Lisa).  The test plays a
slow Lisa: after each MORE it waits before answering NEXT.

  1. Paging: each page (MORE included) is at most 384 bytes, nothing more
     arrives while the "Lisa" holds back its NEXT, and the whole listing
     arrives (60 entries, then DONE).
  2. A lost NEXT: the first MORE is not answered; the server sends MORE
     again after its timeout, and the listing then completes.
  3. The client stops the listing (it restarted: HELLO instead of NEXT):
     the server answers that HELLO with its banner, sends no more of the
     listing, and a new LIST works.
  4. A late NEXT after DONE is ignored: the next command's reply is its
     own (OK), not "ERR 99 unknown command".
  5. A short listing (one page) has no MORE at all.

Run:  python3 tests/test_list_paging.py
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

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tests/..
SERVER = os.path.join(TOP, "LisaSerialFtpServer.py")
NFILES = 60
PAGE = 384

failures = []


def check(name, cond, info=""):
    print("%-30s %s %s" % (name, "PASS" if cond else "FAIL", info))
    if not cond:
        failures.append(name)


class Link:
    """The Lisa end of the pty, reading whole lines."""

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

    def quiet_for(self, secs):
        """True if nothing at all arrives within secs."""
        if self.buf:
            return False
        r, _, _ = select.select([self.fd], [], [], secs)
        return not r

    def send(self, line):
        os.write(self.fd, line.encode("ascii") + b"\r\n")


def start(root, extra=()):
    master, slave = pty.openpty()
    env = dict(os.environ, LISAFTP_FD=str(slave))
    proc = subprocess.Popen(
        [sys.executable, SERVER, "--port", "x", "--home_dir", root] + list(extra),
        pass_fds=[slave], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(slave)
    link = Link(master)
    banner = link.read_line()
    assert banner and "LISA-SERIAL-FTP/v1.0" in banner, banner
    return proc, link


def stop(proc, link):
    link.send("QUIT")
    time.sleep(0.3)
    proc.terminate()
    proc.wait(timeout=5)
    os.close(link.fd)


def read_listing(link, hold=0.0, skip_first_more=False):
    """Read a LIST reply, answering MORE with NEXT after `hold` seconds.
    Returns (lines without MORE, page sizes in bytes, quiet_ok, mores)."""
    lines, pages, quiet_ok, mores = [], [], True, 0
    size = 0
    while True:
        l = link.read_line(15)
        if l is None:
            lines.append(None)
            break
        size += len(l) + 2
        if l == "MORE":
            mores += 1
            pages.append(size)
            size = 0
            if skip_first_more and mores == 1:
                continue                     # "lost" NEXT: answer nothing
            if hold and not link.quiet_for(hold):
                quiet_ok = False             # the server did not wait
            link.send("NEXT")
            continue
        lines.append(l)
        if l == "DONE":
            pages.append(size)
            break
    return lines, pages, quiet_ok, mores


def main():
    root = tempfile.mkdtemp(prefix="lisaftp_list_")
    atexit.register(shutil.rmtree, root, True)   # deleted when the test ends
    for i in range(NFILES):
        with open(os.path.join(root, "LisaListPagingFile%02d.text" % i), "w") as f:
            f.write("x" * (i * 37))
    small = tempfile.mkdtemp(prefix="lisaftp_small_")
    atexit.register(shutil.rmtree, small, True)   # deleted when the test ends
    open(os.path.join(small, "one.text"), "w").close()

    # 1. paging, with a slow Lisa
    proc, link = start(root)
    link.send("LIST")
    lines, pages, quiet_ok, mores = read_listing(link, hold=1.0)
    entries = [l for l in lines if l and l.startswith("F ")]
    check("paged-complete", lines[0] == "OK" and lines[-1] == "DONE"
          and len(entries) == NFILES, "%d entries" % len(entries))
    check("paged-pages-fit", pages and max(pages) <= PAGE,
          "pages %s" % pages)
    check("paged-several-pages", mores >= 4, "%d MORE lines" % mores)
    check("paged-waits-for-next", quiet_ok)

    # 4. a late NEXT after DONE is ignored
    link.send("NEXT")
    link.send("LIST")
    first = link.read_line()
    check("late-next-ignored", first == "OK", repr(first))
    read_listing(link)
    stop(proc, link)

    # 2. a lost NEXT: MORE is sent again after the timeout
    proc, link = start(root, ["--timeout", "1"])
    link.send("LIST")
    lines, pages, quiet_ok, mores = read_listing(link, skip_first_more=True)
    entries = [l for l in lines if l and l.startswith("F ")]
    check("lost-next-recovered", lines[-1] == "DONE" and len(entries) == NFILES,
          "%d entries" % len(entries))
    check("lost-next-more-resent", mores >= 2 and pages[1] == len("MORE\r\n"),
          "pages %s" % pages[:3])
    stop(proc, link)

    # 3. the client stops the listing (HELLO instead of NEXT)
    proc, link = start(root)
    link.send("LIST")
    while True:
        l = link.read_line()
        if l in ("MORE", None):
            break
    link.send("HELLO")
    reply = link.read_line()
    check("stopped-banner", reply is not None and reply.startswith("H")
          and "LISA-SERIAL-FTP/v1.0" in reply, repr(reply)[:60])
    check("stopped-no-more-listing", link.quiet_for(1.5))
    link.send("LIST")
    lines, _, _, _ = read_listing(link)
    check("stopped-list-again", lines[0] == "OK" and lines[-1] == "DONE")
    stop(proc, link)

    # 5. a short listing: one page, no MORE
    proc, link = start(small)
    link.send("LIST")
    lines, pages, _, mores = read_listing(link)
    check("short-no-more", mores == 0 and lines == ["OK", "F 0 one.text", "DONE"],
          repr(lines))
    stop(proc, link)

    print()
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
