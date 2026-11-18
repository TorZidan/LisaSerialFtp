#!/usr/bin/env python3
"""
test_lisaftp.py -- loopback test of the whole LisaSerialFtpServer.py
command set, with no hardware: a pty pair, the server on its slave end, and
a Python stand-in for the Lisa client on the master end.

  * banner and HELLO: the protocol version and chunk size, and the client's
    warnings when they differ from its own;
  * LIST, GET (also of a file in a subfolder) and PUT, with the bytes and the
    end-of-transfer check (DONE <bytes> <crc> / OK <bytes> <crc>) verified;
  * NAK and retransmit, duplicate chunks, empty files, the GO / FAIL / "N 0"
    start of a GET, a client that restarts in the middle of a transfer;
  * names: a path outside --home_dir refused, long names, non-ASCII names
    (Mac Roman on the wire, percent-encoding for the rest);
  * GET name T (the server builds the Lisa text pages);
  * stray bytes with no line end dropped after LINE_GAP;
  * DELETE, DELTESTS, DEBUG ON/OFF, unknown and removed commands (STAT,
    MKDIR, DEL, PWD) answered ERR 99, and QUIT (the server waits for the
    next client).

Run:  python3 src/server/tests/test_lisaftp.py
"""
import os
import pty
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)              # the server folder (src/server)
REPO = os.path.dirname(os.path.dirname(TOP))   # the repository
SERVER = os.path.join(TOP, "LisaSerialFtpServer.py")


def is_banner(line):
    """The server's banner: 'Hi from Lisa Serial FTP Server version
    LISA-SERIAL-FTP/v1.0 : home_dir=... chunk_size=...' (the client only checks
    that it starts with 'H')."""
    return line.startswith("H") and "LISA-SERIAL-FTP/v1.0" in line


CLIENT = os.path.join(REPO, "src", "client", "LisaSerialFtpClient.text")


def major_of(s):
    """Mirror of the client's MajorOf: the digits after 'v' that follow
    'LISA-SERIAL-FTP/' ('' if none: an older server)."""
    k = s.find("LISA-SERIAL-FTP/")
    if k < 0:
        return ""
    k += len("LISA-SERIAL-FTP/")
    if k >= len(s) or s[k] not in "vV":
        return ""
    m = ""
    k += 1
    while k < len(s) and s[k].isdigit():
        m += s[k]
        k += 1
    return m


def client_version():
    """VERSION = 'v...' in LisaSerialFtpClient.text."""
    import re
    with open(CLIENT) as f:
        return re.search(r"^\s*VERSION\s*=\s*'([^']*)'", f.read(), re.M).group(1)


def chunk_of(s):
    """Mirror of the client's ChunkOf: the number after 'chunk_size='
    (-1 if none)."""
    k = s.find("chunk_size=")
    if k < 0:
        return -1
    k += len("chunk_size=")
    if k >= len(s) or not s[k].isdigit():
        return -1
    n = 0
    while k < len(s) and s[k].isdigit():
        n = n * 10 + int(s[k])
        k += 1
    return n


def client_chunk():
    """CHUNK = n in LisaSerialFtpClient.text."""
    import re
    with open(CLIENT) as f:
        return int(re.search(r"^\s*CHUNK\s*=\s*(\d+)\s*;", f.read(), re.M).group(1))


def crc16(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
        crc &= 0xFFFF
    return crc


class Lisa:
    """Emulates the Lisa side of LISA-SERIAL-FTP/v1.0 on a pty master fd."""

    def __init__(self, fd, debug=False):
        self.fd = fd
        self.buf = b""
        self.debug = debug

    def read_byte(self, timeout=10.0):
        import select
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
                if self.debug:
                    ch = chr(b[0]) if 32 <= b[0] < 127 else "\\x%02x" % b[0]
                    print("  <RX %s>" % ch, file=sys.stderr)
                return b[0]

    def read_line(self, timeout=10.0):
        line = b""
        while True:
            b = self.read_byte(timeout)
            if b == 0x0A:
                return line.rstrip(b"\r").decode("mac_roman")   # the Lisa's characters
            line += bytes([b])

    def drain(self, pause=0.2):
        """Discard whatever the server has queued for us (e.g. a chunk
        frame that was in flight when the 'peer' restarted)."""
        import select
        time.sleep(pause)
        while True:
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if not r:
                return
            os.read(self.fd, 65536)

    def wait_banner(self):
        for _ in range(50):
            line = self.read_line()
            if is_banner(line):
                return line
        raise AssertionError("no banner")

    def send_line(self, s):
        os.write(self.fd, s.encode("mac_roman") + b"\r\n")
        self.need_go = s.startswith("GET ")   # the client's GO before chunk 0


    def command(self, s, timeout=10.0):
        self.send_line(s)
        return self.read_line(timeout)

    def send_chunk(self, seq, payload, timeout=10.0):
        frame = (b"T" + seq.to_bytes(2, "big")
                 + len(payload).to_bytes(2, "big") + payload)
        frame += crc16(frame[1:]).to_bytes(2, "big")
        os.write(self.fd, frame)
        return self.read_line(timeout)

    def recv_chunk(self, timeout=10.0):
        if getattr(self, "need_go", False):  # its file is open: ready
            self.need_go = False
            os.write(self.fd, b"GO\r\n")
        hdr = b""
        for _ in range(5):
            hdr += bytes([self.read_byte(timeout)])
        assert hdr[0] == ord("T"), "bad chunk header %r" % hdr
        seq = int.from_bytes(hdr[1:3], "big")
        clen = int.from_bytes(hdr[3:5], "big")
        payload = b""
        for _ in range(clen):
            payload += bytes([self.read_byte(timeout)])
        crc = int.from_bytes(bytes([self.read_byte(timeout)])
                             + bytes([self.read_byte(timeout)]), "big")
        return seq, payload, crc


def main():
    debug = "--debug" in sys.argv[1:]
    with tempfile.TemporaryDirectory() as root:
        # files to serve
        filedata = bytes(range(256)) * 7       # 1792 bytes
        with open(os.path.join(root, "hello.txt"), "wb") as f:
            f.write(filedata)
        os.mkdir(os.path.join(root, "sub dir"))
        with open(os.path.join(root, "sub dir", "x.bin"), "wb") as f:
            f.write(b"\x00\x01\x02")

        master, slave = pty.openpty()
        env = dict(os.environ, LISAFTP_FD=str(slave))
        server_args = [sys.executable, SERVER, "--port", "ignored",
                       "--baud", "9600", "--home_dir", root,
                       "--timeout", "5", "--debug"]
        proc = subprocess.Popen(
            server_args,
            pass_fds=[slave],
            env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.close(slave)
        time.sleep(0.5)

        lisa = Lisa(master, debug=debug)
        failures = []

        def check(name, cond, extra=""):
            print("%-38s %s %s" % (name, "PASS" if cond else "FAIL", extra))
            if not cond:
                failures.append(name)

        # 1. banner
        banner = lisa.wait_banner()
        check("banner", is_banner(banner), banner)
        # the client's version check: same major version as the server,
        # so no WARNING; an older or another major version would warn
        mine = major_of("LISA-SERIAL-FTP/" + client_version())
        check("version-same-major", mine == "1" and major_of(banner) == mine,
              "client %r, server %r" % (mine, major_of(banner)))
        # the client's chunk size check: the banner names the client's CHUNK
        check("chunk-size-same", chunk_of(banner) == client_chunk(),
              "server %d, client %d" % (chunk_of(banner), client_chunk()))
        check("chunk-size-other-warns",
              chunk_of("Hi ... home_dir=/x chunk_size=256") == 256
              and chunk_of("Hi ... LISA-FTP/5 : home_dir=/x") == -1)
        check("version-other-major-warns",
              major_of("Hi ... LISA-SERIAL-FTP/v2.0 : x") == "2"
              and major_of("Hi ... LISA-FTP/5 : x") == ""
              and major_of("Hi ... LISA-SERIAL-FTP/v10.3 : x") == "10")

        # 2. HELLO re-sends banner
        lisa.send_line("HELLO")
        check("hello", is_banner(lisa.read_line()))

        # 3. LIST
        lisa.send_line("LIST")
        lines = []
        while True:
            l = lisa.read_line()
            if l == "MORE":                  # end of a page: ask for the next
                lisa.send_line("NEXT")
                continue
            lines.append(l)
            if l == "DONE":
                break
        body = lines[1:-1]
        check("list-ok", lines[0] == "OK")
        check("list-hello.txt", any(l.startswith("F 1792 hello.txt") for l in body),
              str(body))
        check("list-subdir", any(l.startswith("D ") and l.endswith("sub%20dir")
                                 for l in body))

        # 4. GET hello.txt
        lisa.send_line("GET hello.txt")
        r = lisa.read_line()
        check("get-ok", r == "OK 1792", r)
        got = b""
        crc = 0xFFFF
        seq = 0
        while True:
            cseq, payload, ccrc = lisa.recv_chunk()
            ok = (cseq == seq and ccrc == crc16(hdr_bytes(cseq, payload)))
            os.write(master, b"A %d\r\n" % seq if ok else b"N %d\r\n" % seq)
            if not ok:
                check("get-chunk-crc", False, "seq %d" % cseq)
                break
            if len(payload) == 0:
                break
            got += payload
            crc = crc16(payload, crc)
            seq += 1
        lisa.send_line("DONE %d %04x" % (len(got), crc))
        r = lisa.read_line()
        check("get-verified", r == "OK %d %04x" % (len(got), crc), r)
        check("get-bytes", got == filedata)

        # 5. GET nested name with the '/' separator and percent-encoding
        lisa.send_line("GET sub%20dir/x.bin")
        r = lisa.read_line()
        check("get-nested-ok", r == "OK 3", r)
        got = b""
        seq = 0
        while True:
            cseq, payload, ccrc = lisa.recv_chunk()
            os.write(master, b"A %d\r\n" % cseq)
            if len(payload) == 0:
                break
            got += payload
            seq += 1
        lisa.send_line("DONE %d %04x" % (len(got), crc16(got)))
        r = lisa.read_line()
        check("get-nested-verified", r == "OK 3 %04x" % crc16(got), r)
        check("get-nested-bytes", got == b"\x00\x01\x02", repr(got))

        # 6. PUT a new file
        data = os.urandom(1000)
        lisa.send_line("PUT upload.dat 1000")
        check("put-ok", lisa.read_line() == "OK")
        seq = 0
        for off in range(0, len(data), 256):
            payload = data[off:off + 256]
            r = lisa.send_chunk(seq, payload)
            if r != "A %d" % seq:
                check("put-ack", False, r)
                break
            seq += 1
        r = lisa.send_chunk(seq, b"")
        check("put-ack-final", r == "A %d" % seq, r)
        r = lisa.read_line()
        check("put-done", r == "DONE 1000 %04x" % crc16(data), r)
        lisa.send_line("OK 1000 %04x" % crc16(data))   # verdict: keep it
        time.sleep(0.3)
        with open(os.path.join(root, "upload.dat"), "rb") as f:
            check("put-bytes", f.read() == data)

        # 7. NAK handling: send a chunk with a corrupt CRC, expect the
        #    server to NAK it, then retransmit it correctly.
        data2 = os.urandom(300)
        lisa.send_line("PUT nakt.dat 300")
        check("nak-ok", lisa.read_line() == "OK")
        seq = 0
        naks = 0
        while True:
            payload = data2[seq * 256:(seq + 1) * 256]
            if not payload:
                r = lisa.send_chunk(seq, b"")
                check("nak-final-ack", r == "A %d" % seq, r)
                r = lisa.read_line()
                check("nak-done", r == "DONE 300 %04x" % crc16(data2), r)
                lisa.send_line("OK 300 %04x" % crc16(data2))
                time.sleep(0.3)
                break
            if seq == 1 and naks == 0:
                # corrupt the CRC of this chunk
                frame = (b"T" + seq.to_bytes(2, "big")
                         + len(payload).to_bytes(2, "big") + payload)
                frame += (crc16(frame[1:]) ^ 0xFFFF).to_bytes(2, "big")
                os.write(master, frame)
                r = lisa.read_line()
                check("nak-response", r == "N 1", r)
                naks += 1
                continue                    # retransmit same seq
            r = lisa.send_chunk(seq, payload)
            check("nak-ack-seq%d" % seq, r == "A %d" % seq, r)
            seq += 1
        with open(os.path.join(root, "nakt.dat"), "rb") as f:
            check("nak-bytes", f.read() == data2)

        # 8. removed commands are rejected as unknown
        for gone in ("STAT upload.dat", "MKDIR newdir", "DEL nakt.dat", "PWD"):
            lisa.send_line(gone)
            check("gone-%s" % gone.split()[0],
                  lisa.read_line().startswith("ERR 99"))
        check("nakt-still-there", os.path.exists(os.path.join(root, "nakt.dat")))

        # 9. path traversal rejected
        lisa.send_line("GET ..%2FLisaSerialFtpServer.py")
        r = lisa.read_line()
        check("traversal-rejected", r.startswith("ERR"), r)

        # 10. unknown command
        lisa.send_line("FROB")
        check("unknown-cmd", lisa.read_line().startswith("ERR 99"))

        # 11. peer restart mid-PUT: the new session's HELLO arrives where
        #     a chunk header is expected; the server drops the partial
        #     file and answers with a fresh banner
        data3 = os.urandom(1000)
        lisa.send_line("PUT restart.dat 1000")
        check("restart-put-ok", lisa.read_line() == "OK")
        check("restart-put-ack", lisa.send_chunk(0, data3[:256]) == "A 0")
        lisa.send_line("HELLO")                  # "crash + restart"
        check("restart-banner", is_banner(lisa.read_line()))
        check("restart-partial-dropped",
              not os.path.exists(os.path.join(root, "restart.dat")))
        check("restart-put-alive", is_banner(lisa.command("HELLO")))

        # 12. peer restart mid-GET: HELLO arrives where an A/N reply is
        #     expected; the server answers with a fresh banner
        lisa.send_line("GET hello.txt")
        check("restart-get-ok", lisa.read_line() == "OK 1792")
        lisa.recv_chunk()
        lisa.send_line("HELLO")                  # instead of "A"
        check("restart-get-banner", is_banner(lisa.read_line()))
        lisa.drain()                             # in-flight chunk frame
        check("restart-get-alive", is_banner(lisa.command("HELLO")))

        # 13a. a garbled reply to a chunk is retried (the same chunk is sent
        #      again), and 'N <seq+1>' counts as an ACK (the client already
        #      has the chunk: its ACK was lost)
        lisa.send_line("GET hello.txt")
        check("retry-get-ok", lisa.read_line() == "OK 1792")
        s0, p0, _ = lisa.recv_chunk()
        lisa.send_line("XYZZY")                   # a garbled ACK
        s1, p1, _ = lisa.recv_chunk()
        check("retry-garbled-resent", (s1, p1) == (s0, p0), "seq %d" % s1)
        lisa.send_line("N 1")                     # 'I have 0, send 1'
        s2, p2, _ = lisa.recv_chunk()
        check("retry-nak-next-is-ack", s2 == 1, "seq %d" % s2)
        got = p0 + p2
        # a stale reply (about chunk 0) is skipped, not taken as the ACK
        # of chunk 1: the server keeps waiting and takes the real 'A 1'
        lisa.send_line("A 0")
        lisa.send_line("A 1")
        while True:
            sq, pl, _ = lisa.recv_chunk()
            if sq == 1:
                check("retry-stale-skipped", False, "chunk 1 resent")
                break
            lisa.send_line("A %d" % sq)
            if not pl:
                break
            got += pl
        lisa.send_line("DONE %d %04x" % (len(got), crc16(got)))
        r = lisa.read_line()
        check("retry-verified", r == "OK 1792 %04x" % crc16(got), r)
        check("retry-bytes", got == filedata)

        # 13b. the client gives up (FAIL) in the middle of a GET: the
        #      server answers OK, sends no FAIL of its own
        lisa.send_line("GET hello.txt")
        lisa.read_line()                          # OK 1792
        lisa.recv_chunk()
        lisa.send_line("FAIL get aborted")
        check("client-fail-ok", lisa.read_line() == "OK")
        check("client-fail-alive", is_banner(lisa.command("HELLO")))

        # 13b2. GET waits for the client's GO: no chunk before it; and the
        #       client's FAIL instead of GO (it could not create its file)
        #       ends the get without a FAIL from the server
        import select as _sel
        lisa.send_line("GET hello.txt")
        check("go-ok", lisa.read_line() == "OK 1792")
        r, _, _ = _sel.select([lisa.fd], [], [], 1.0)
        check("go-no-chunk-before-go", not r)
        lisa.send_line("FAIL cannot create the local file")
        r, _, _ = _sel.select([lisa.fd], [], [], 1.0)
        check("go-fail-quiet", not r)
        check("go-fail-alive", is_banner(lisa.command("HELLO")))
        lisa.send_line("GET sub%20dir/x.bin")
        lisa.read_line()                          # OK 3
        lisa.send_line("N 0")                     # our GO was lost: NAK of chunk 0
        sq, pl, _ = lisa.recv_chunk()
        check("go-nak0-starts", sq == 0 and pl == b"\x00\x01\x02", repr((sq, pl)))
        lisa.send_line("A 0")
        lisa.recv_chunk()
        lisa.send_line("A 1")
        lisa.send_line("DONE 3 %04x" % crc16(b"\x00\x01\x02"))
        check("go-nak0-verified", (lisa.read_line() or "").startswith("OK 3"))

        # 13c. HELLO during the FAIL -> OK handshake: the server re-banners
        lisa.send_line("GET sub%20dir/x.bin")
        lisa.read_line()                          # OK 3
        lisa.recv_chunk()
        lisa.send_line("A 0")
        lisa.recv_chunk()                         # the end-of-file chunk
        lisa.send_line("A 1")
        lisa.send_line("XYZZY")                   # instead of DONE: server aborts
        check("restart-fail", lisa.read_line().startswith("FAIL"))
        lisa.send_line("HELLO")                   # while server waits for OK
        check("restart-fail-banner",
              is_banner(lisa.read_line()))
        lisa.drain()                             # in-flight chunk frame
        check("restart-fail-alive", is_banner(lisa.command("HELLO")))

        # 13d. PUT: a duplicate chunk (the client's ACK was lost) is
        #      re-ACKed with ITS number, 'A 0', so the client can tell
        #      that reply from the ACK of chunk 1
        data4 = os.urandom(300)
        lisa.send_line("PUT dup.dat 300")
        check("dup-put-ok", lisa.read_line() == "OK")
        check("dup-ack0", lisa.send_chunk(0, data4[:256]) == "A 0")
        check("dup-reack0", lisa.send_chunk(0, data4[:256]) == "A 0")
        check("dup-ack1", lisa.send_chunk(1, data4[256:]) == "A 1")
        check("dup-ack2", lisa.send_chunk(2, b"") == "A 2")
        r = lisa.read_line()
        check("dup-done", r == "DONE 300 %04x" % crc16(data4), r)
        lisa.send_line("OK 300 %04x" % crc16(data4))
        time.sleep(0.3)

        # 13e. GET: the ACK of the end-of-file chunk is lost, the DONE
        #      arrives: the server takes the DONE as the ACK and answers
        #      the verdict (no resend of the end-of-file chunk)
        lisa.send_line("GET sub%20dir/x.bin")
        lisa.read_line()                          # OK 3
        sq, pl, _ = lisa.recv_chunk()
        lisa.send_line("A %d" % sq)
        sq, pl, _ = lisa.recv_chunk()             # the end-of-file chunk
        check("eof-get-empty", pl == b"", repr(pl))
        got = b"\x00\x01\x02"                    # (its 'A 1' is "lost")
        lisa.send_line("DONE 3 %04x" % crc16(got))
        r = lisa.read_line()
        check("eof-get-done-is-ack", r == "OK 3 %04x" % crc16(got), r)

        # 13f. PUT: the client resends the end-of-file chunk after the
        #      server's DONE (the client missed the ACK, or its wait ran
        #      out first): the server ACKs it and sends DONE again, then
        #      takes the verdict and keeps the file
        data5 = os.urandom(100)
        lisa.send_line("PUT eof.dat 100")
        check("eof-put-ok", lisa.read_line() == "OK")
        check("eof-put-ack0", lisa.send_chunk(0, data5) == "A 0")
        check("eof-put-ack1", lisa.send_chunk(1, b"") == "A 1")
        done = "DONE 100 %04x" % crc16(data5)
        check("eof-put-done", lisa.read_line() == done)
        r = lisa.send_chunk(1, b"")               # the end-of-file chunk again
        check("eof-put-reack", r == "A 1", r)
        check("eof-put-redone", lisa.read_line() == done)
        lisa.send_line("OK 100 %04x" % crc16(data5))
        time.sleep(0.3)
        with open(os.path.join(root, "eof.dat"), "rb") as f:
            check("eof-put-kept", f.read() == data5)

        # 13g. DELTESTS (hidden, for the Lisa test macro): no tests folder
        #      -> ERR; then it deletes only <root>/tests/lisatest-* files
        check("deltests-nodir", lisa.command("DELTESTS").startswith("ERR"))
        td = os.path.join(root, "tests")
        os.mkdir(td)
        for name in ("lisatest-up1.txt", "LISATEST-UP2.TXT", "test_keep.py"):
            with open(os.path.join(td, name), "w") as f:
                f.write("x")
        os.mkdir(os.path.join(td, "lisatest-dir"))           # a folder: kept
        with open(os.path.join(root, "lisatest-top.txt"), "w") as f:
            f.write("x")                                      # not in tests/
        r = lisa.command("DELTESTS")
        check("deltests-ok", r == "OK 2 deleted", r)
        check("deltests-kept",
              sorted(os.listdir(td)) == ["lisatest-dir", "test_keep.py"]
              and os.path.exists(os.path.join(root, "lisatest-top.txt")),
              repr(sorted(os.listdir(td))))
        r = lisa.command("DELTESTS")
        check("deltests-none", r == "OK 0 deleted", r)

        # 13h. DELETE (the client's 'delete'): a file goes; a missing file,
        #      a folder, a name outside the root and bad usage are refused
        with open(os.path.join(root, "gone.txt"), "w") as f:
            f.write("x")
        r = lisa.command("DELETE gone.txt")
        check("delete-ok", r == "OK deleted"
              and not os.path.exists(os.path.join(root, "gone.txt")), repr(r))
        r = lisa.command("DELETE gone.txt")
        check("delete-missing", r is not None and r.startswith("ERR 11 "), repr(r))
        r = lisa.command("DELETE sub%20dir")
        check("delete-folder-refused", r is not None and r.startswith("ERR 12 ")
              and os.path.isdir(os.path.join(root, "sub dir")), repr(r))
        r = lisa.command("DELETE ..%2Fx")
        check("delete-escape-refused", r is not None and r.startswith("ERR 2 "), repr(r))
        r = lisa.command("DELETE")
        check("delete-usage", r is not None and r.startswith("ERR 1 "), repr(r))

        # 13h2. no 32-character limit of the server's own: a 40-character
        #       name can be put and got; a name the OS refuses (over 255
        #       bytes) is refused before the OK, with nothing created
        longname = "a-server-file-name-of-40-characters.bin"[:40]
        longname = longname + "x" * (40 - len(longname))
        r = lisa.command("PUT %s 3" % longname)
        acks = []
        if r == "OK":
            acks.append(lisa.send_chunk(0, b"abc"))
            acks.append(lisa.send_chunk(1, b""))
            done = lisa.read_line()
            lisa.send_line("OK 3 %04x" % crc16(b"abc"))
            time.sleep(0.3)
        check("longname-put", r == "OK" and acks == ["A 0", "A 1"]
              and os.path.exists(os.path.join(root, longname)), repr((r, acks)))
        r = lisa.command("GET %s" % longname)
        check("longname-get", r == "OK 3", repr(r))
        if r == "OK 3":
            lisa.recv_chunk()
            lisa.send_line("A 0")
            lisa.recv_chunk()
            lisa.send_line("A 1")
            lisa.send_line("DONE 3 %04x" % crc16(b"abc"))
            lisa.read_line()
        toolong = "y" * 300
        r = lisa.command("PUT %s 3" % toolong)
        check("toolong-refused", r is not None and r.startswith("ERR 13 ")
              and not os.path.exists(os.path.join(root, toolong[:255])), repr(r)[:70])

        # 13h3. non-ASCII names: the wire is Mac Roman, the Lisa's
        #       character set.  LIST shows a character the Lisa has as
        #       itself (e-acute = 0x8E on the wire), one it lacks and a name
        #       that is not UTF-8 percent-encoded (UTF-8 / raw bytes); every
        #       name LIST shows can be typed back; a PUT of a Lisa name with
        #       e-acute makes a UTF-8 name; an error reply naming a
        #       character the Lisa lacks does not stop the server
        nd = os.path.join(root, "names")
        os.mkdir(nd)
        for n in ("caf\u00e9.txt", "\u4e2d.txt"):
            with open(os.path.join(nd, n), "wb") as f:
                f.write(b"abc")
        with open(os.path.join(nd.encode(), b"raw\xff.txt"), "wb") as f:
            f.write(b"abc")
        lisa.send_line("LIST names")
        listed = []
        while True:
            l = lisa.read_line()
            if l == "DONE":
                break
            if l.startswith("F "):
                listed.append(l.split(" ", 2)[2])
        check("nonascii-list", sorted(listed) == sorted(
            ["caf\u00e9.txt", "%E4%B8%AD.txt", "raw%FF.txt"]), repr(listed))
        for n in listed:
            r = lisa.command("SUM names/%s B" % n)
            check("nonascii-typed-back " + n.encode("unicode_escape").decode(),
                  r is not None and r.startswith("OK 3 "), repr(r))
        r = lisa.command("PUT names/na\u00efve.bin 3")
        if r == "OK":
            lisa.send_chunk(0, b"abc")
            lisa.send_chunk(1, b"")
            lisa.read_line()
            lisa.send_line("OK 3 %04x" % crc16(b"abc"))
            time.sleep(0.3)
        check("nonascii-put-utf8", os.path.exists(os.path.join(nd, "na\u00efve.bin")), repr(r))
        r = lisa.command("GET names/%E9%9B%BB.txt")
        check("nonascii-error-reply", r is not None and r.startswith("ERR 11 ")
              and r.endswith("?.txt"), repr(r))
        check("nonascii-server-alive", is_banner(lisa.command("HELLO")))

        # 13i. GET <name> T: the finished Lisa text pages (lisa_pages), whole
        #      1024-byte pages, verified like any GET; a bad mode is refused
        sys.path.insert(0, TOP)
        from LisaSerialFtpServer import lisa_pages
        text = (b"program x;\r\nbegin\r\n    writeln('hi');\r\nend.\r\n"
                + b"y" * 300 + b"\n" + b"  indented\n" * 200)
        with open(os.path.join(root, "notes.txt"), "wb") as f:
            f.write(text)
        want = lisa_pages(text)
        lisa.send_line("GET notes.txt T")
        r = lisa.read_line()
        check("gett-ok", r == "OK %d" % len(want) and len(want) % 1024 == 0, repr(r))
        got = b""
        while True:
            sq, pl, _ = lisa.recv_chunk()
            lisa.send_line("A %d" % sq)
            if not pl:
                break
            got += pl
        lisa.send_line("DONE %d %04x" % (len(got), crc16(got)))
        r = lisa.read_line()
        check("gett-pages", got == want and r == "OK %d %04x" % (len(got), crc16(got)),
              "%d bytes, %r" % (len(got), r))
        check("gett-format", got[:11] == b"program x;\r" and b"\x10\x22indented\r" in got,
              repr(got[:12]))
        r = lisa.command("GET notes.txt X")
        check("gett-usage", r is not None and r.startswith("ERR 1 "), repr(r))

        # 13b. stray bytes with no line end (noise) are dropped after
        # LINE_GAP (5 s) and do not spoil the next command
        os.write(master, b"x\x07y")
        time.sleep(6.0)
        check("stray-bytes-dropped", is_banner(lisa.command("HELLO")))

        # 14. DEBUG ON / OFF (the client's 'debug' command)
        check("debug-on", lisa.command("DEBUG ON") == "OK debug on")
        check("debug-off", lisa.command("debug off") == "OK debug off")
        check("debug-bad", lisa.command("DEBUG maybe").startswith("ERR"))

        # 15. QUIT ends the session, not the server: it waits for the
        #     next client, which is greeted as usual
        lisa.send_line("QUIT")
        time.sleep(0.5)
        check("quit-server-alive", proc.poll() is None)
        check("quit-next-client", is_banner(lisa.command("HELLO")))
        lisa.send_line("QUIT")
        time.sleep(0.3)
        proc.terminate()                    # the server runs until stopped
        proc.wait(timeout=5)

        out = proc.stdout.read().decode(errors="replace")
        for line in out.splitlines():
            if "WARNING" in line or "Traceback" in line:
                print("   server log: " + line)
                failures.append("server-log")

    print()
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


def hdr_bytes(seq, payload):
    return seq.to_bytes(2, "big") + len(payload).to_bytes(2, "big") + payload


if __name__ == "__main__":
    sys.exit(main())
