#!/usr/bin/env python3
"""
test_loss_recovery.py -- the server's loss recovery, in both directions,
against a simulated Lisa whose serial link drops bytes.

  * PUT: the simulated Lisa sender drops 1-3 bytes from some of its chunks;
    the server must NAK them and still store the exact file.
  * GET: bytes the server sends are dropped on the way to the simulated
    Lisa receiver, which NAKs; the file must still arrive exactly.
  * Both directions with no loss (also a size that is an exact multiple
    of the chunk size), and the end-of-transfer check failing on purpose
    (the client says BAD / reports a bad CRC): the transfer must be
    refused cleanly, not hang.

Run:  python3 src/server/tests/test_loss_recovery.py
"""
import atexit
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # tests/..
import LisaSerialFtpServer as srv

VERBOSE = "-v" in sys.argv


class MockPort:
    """In-memory, thread-safe stand-in for SerialPort."""
    def __init__(self):
        self.rx = bytearray()          # Lisa -> server (server reads)
        self.tx = bytearray()          # server -> Lisa (Lisa reads)
        self.lock = threading.Lock()
        self.rx_event = threading.Event()
        self.tx_event = threading.Event()
        self.closed = False
        self.nak_count = 0
        self.dups = 0

    def read(self, n, timeout):
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                if len(self.rx) >= n:
                    data = bytes(self.rx[:n])
                    del self.rx[:n]
                    return data
                if self.closed:
                    raise srv.ClientGone("port closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("read timeout after %d" % 0)
            self.rx_event.wait(min(remaining, 0.02))
            self.rx_event.clear()

    def write(self, data):
        with self.lock:
            self.tx += data
            if data.startswith(b"N "):
                self.nak_count += 1
            if data == b"A\r\n":
                pass
        self.tx_event.set()

    def log(self, msg):
        if VERBOSE:
            print("[srv] " + msg, file=sys.stderr)

    def drain(self, limit=2.0):
        pass                           # writes are instant here

    def flush_input(self):
        with self.lock:
            self.rx.clear()

    def close(self):
        with self.lock:
            self.closed = True
        self.rx_event.set()
        self.tx_event.set()

    # simulated-Lisa side ---------------------------------------------------
    def send(self, data):
        with self.lock:
            self.rx += data
        self.rx_event.set()

    def read_tx(self, n, timeout):
        """Read n bytes from the server->Lisa direction (self.tx)."""
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                if len(self.tx) >= n:
                    data = bytes(self.tx[:n])
                    del self.tx[:n]
                    return data
                if self.closed:
                    raise srv.ClientGone("port closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("read timeout")
            self.tx_event.wait(min(remaining, 0.02))
            self.tx_event.clear()

    def read_frame(self, timeout):
        """Read one framed chunk from the server->Lisa direction.  Returns
        the raw bytes (possibly short on a mid-frame timeout), or None on an
        idle timeout at the frame start."""
        try:
            hdr = self.read_tx(5, timeout)
        except TimeoutError:
            return None
        clen = int.from_bytes(hdr[3:5], "big")
        rest = b""
        try:
            rest = self.read_tx(clen + 2, timeout)
        except TimeoutError:
            pass
        return hdr + rest

    def read_line(self, timeout):
        buf = b""
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                if self.tx:
                    b = self.tx.pop(0)
                elif self.closed:
                    return None
                else:
                    b = None
            if b is not None:
                buf += bytes([b])
                if b == 0x0A:
                    return buf[:-1].rstrip(b"\r").decode()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.tx_event.wait(min(remaining, 0.02))
                self.tx_event.clear()


def make_server(root):
    """A server on a MockPort, with the command line it would have had."""
    args = types.SimpleNamespace(home_dir=root, timeout=srv.T_FRAME,
                                 read_only=False, monkey_wrench=False,
                                 port=None, baud=57600, debug=False)
    return srv.LisaFtpServer(args, port=MockPort())


def lisa_sender(port, data, chunk, drop_fn, max_retry=1000, lie=False):
    """Simulate the Lisa's SendChunk + DoPut verification.  Returns True on
    success.  lie=True sends a BAD verdict (to test that the server then
    deletes its copy)."""
    seq = 0
    total = (len(data) + chunk - 1) // chunk
    retries = 0
    while seq <= total:
        payload = data[seq*chunk:(seq+1)*chunk] if seq < total else b""
        frame = (b"T" + seq.to_bytes(2, "big")
                 + len(payload).to_bytes(2, "big") + payload)
        frame += srv.crc16(frame[1:]).to_bytes(2, "big")
        frame = drop_fn(frame, seq)
        port.send(frame)
        reply = port.read_line(timeout=15)
        # like the Lisa: skip stale replies (about an earlier chunk)
        while True:
            kind, num = srv.parse_reply(reply)
            if kind is None or not srv.seq_behind(num, seq % srv.SEQ_MOD):
                break
            reply = port.read_line(timeout=15)
        if kind == "A" and num == seq % srv.SEQ_MOD:
            seq += 1
            retries = 0
        elif reply is not None and reply.startswith("N"):
            retries += 1
            if retries > max_retry:
                return False
            continue
        elif reply is not None and reply.startswith("FAIL"):
            port.send(b"OK\r\n")
            return False
        else:
            retries += 1
            if retries > max_retry:
                return False
            continue
    # verification: the server's DONE must match what we sent; answer
    # OK/BAD with our numbers (upper-case hex, as the Lisa's IntToHex)
    done = port.read_line(timeout=15)
    mine = "%d %04X" % (len(data), srv.crc16(data))
    good = done is not None and done.upper() == "DONE " + mine and not lie
    port.send((("OK " if good else "BAD ") + mine + "\r\n").encode())
    return good


def run_case(name, data, drop_fn, lie=False):
    root = tempfile.mkdtemp(prefix="lisaftp_test_")
    atexit.register(shutil.rmtree, root, True)   # deleted when the test ends
    s = make_server(root)
    path = os.path.join(root, "out.bin")
    result = {}

    def server_main():
        try:
            s.cmd_put(["out.bin", str(len(data))])
            result["ok"] = True
        except Exception as e:
            result["ok"] = False
            result["err"] = repr(e)

    t = threading.Thread(target=server_main)
    t.start()
    time.sleep(0.2)                       # let the server get to the chunk loop
    ack = s.port.read_line(timeout=5)     # the PUT acknowledgment
    if ack != "OK":
        result["ok"] = False
        result["err"] = "bad PUT ack: %r" % (ack,)
        t.join(timeout=5)
        return False
    ok = lisa_sender(s.port, data, srv.CHUNK, drop_fn, lie=lie)
    t.join(timeout=30)
    if t.is_alive():
        result["ok"] = False
        result["err"] = "server thread hung"
    if lie:
        # BAD verdict: the server must have deleted its copy
        good = not os.path.exists(path) and not ok
        print("%-28s file_deleted=%s %s" % (name, good, result.get("err", "")))
        return result.get("ok") and good and not t.is_alive()
    # compare
    good = False
    got = b""
    if os.path.exists(path):
        with open(path, "rb") as f:
            got = f.read()
        good = got == data
    if not good:
        print("  file len=%d expected=%d" % (len(got), len(data)))
        for i in range(min(len(got), len(data))):
            if got[i] != data[i]:
                print("  first mismatch at byte %d: got %02x want %02x"
                      % (i, got[i], data[i]))
                break
    result["file_ok"] = good
    result["nak"] = s.port.nak_count
    print("%-28s transfer_ok=%s file_ok=%s naks=%s %s"
          % (name, result.get("ok"), good, s.port.nak_count,
             result.get("err", "")))
    return result.get("ok") and good and not t.is_alive()


def lisa_receiver(port, chunk, drop_fn, max_retry=1000, lie=False):
    """Simulate the Lisa's RecvChunk for GET.  Returns the received bytes.
    drop_fn corrupts the *received* frame (a byte lost server->Lisa)."""
    out = bytearray()
    seq = 0
    retries = 0
    # read the banner + "OK <size>" lines
    while True:
        line = port.read_line(timeout=15)
        if line is None:
            return None
        if line.startswith("OK"):
            break
    port.send(b"GO\r\n")              # the local file is open: ready
    while True:
        # read one frame (with byte loss applied to the received bytes)
        frame = port.read_frame(timeout=15)
        if frame is None:
            return None
        frame = drop_fn(frame, seq)
        # parse
        if len(frame) < 5 or frame[0:1] != b"T":
            port.send(b"N %d\r\n" % seq)
            retries += 1
            if retries > max_retry:
                return None
            continue
        cseq = int.from_bytes(frame[1:3], "big")
        clen = int.from_bytes(frame[3:5], "big")
        if len(frame) < 5 + clen + 2:
            # partial frame (a byte lost): NAK
            port.send(b"N %d\r\n" % seq)
            retries += 1
            if retries > max_retry:
                return None
            continue
        payload = frame[5:5+clen]
        rcrc = int.from_bytes(frame[5+clen:5+clen+2], "big")
        calc = srv.crc16(frame[1:5] + payload)
        if cseq < seq:
            # duplicate: re-ACK, discard
            port.send(b"A %d\r\n" % cseq)
            retries += 1
            if retries > max_retry:
                return None
            continue
        if cseq != seq or rcrc != calc:
            port.send(b"N %d\r\n" % seq)
            retries += 1
            if retries > max_retry:
                return None
            continue
        # good chunk
        port.send(b"A %d\r\n" % seq)
        retries = 0
        if clen == 0:
            break
        out += payload
        seq += 1
    # send DONE
    crc = 0xFFFF
    for i in range(0, len(out), chunk):
        crc = srv.crc16(bytes(out[i:i+chunk]), crc)
    if lie:
        crc ^= 1                          # report a wrong CRC on purpose
    mine = "%d %04X" % (len(out), crc)
    port.send(("DONE " + mine + "\r\n").encode())
    # verification: keep the data only on 'OK' with our own numbers
    verdict = port.read_line(timeout=15)
    if verdict is None or verdict.upper() != "OK " + mine:
        return None
    return bytes(out)


def run_get_case(name, data, drop_fn, lie=False):
    root = tempfile.mkdtemp(prefix="lisaftp_get_")
    atexit.register(shutil.rmtree, root, True)   # deleted when the test ends
    src = os.path.join(root, "src.bin")
    with open(src, "wb") as f:
        f.write(data)
    s = make_server(root)
    result = {}

    def server_main():
        try:
            s.cmd_get(["src.bin"])
            result["ok"] = True
        except Exception as e:
            result["ok"] = False
            result["err"] = repr(e)

    t = threading.Thread(target=server_main)
    t.start()
    time.sleep(0.2)
    got = lisa_receiver(s.port, srv.CHUNK, drop_fn, lie=lie)
    t.join(timeout=30)
    if t.is_alive():
        result["ok"] = False
        result["err"] = "server thread hung"
    if lie:
        # wrong DONE: the server must answer BAD, so the client drops it
        good = got is None
        print("%-28s rejected=%s %s" % (name, good, result.get("err", "")))
        return result.get("ok") and good and not t.is_alive()
    good = (got == data)
    if not good and got is not None:
        print("  got len=%d expected=%d" % (len(got), len(data)))
    print("%-28s transfer_ok=%s file_ok=%s %s"
          % (name, result.get("ok"), good, result.get("err", "")))
    return result.get("ok") and good and not t.is_alive()


def main():
    # sequence numbers wrap at 65536: 65535 is just behind 0
    assert srv.seq_behind(65535, 0) and srv.seq_behind(0, 1)
    assert not srv.seq_behind(0, 0) and not srv.seq_behind(1, 0)
    assert not srv.seq_behind(0, 65535 - 32767)
    assert srv.parse_reply("A 65535") == ("A", 65535)
    assert srv.parse_reply("A 65536") == (None, None)
    assert srv.parse_reply("A") == (None, None)
    random.seed(1234)
    data = os.urandom(10000)              # not a multiple of 1024
    data2 = os.urandom(10240)             # exact multiple of 1024

    def no_drop(frame, seq):
        return frame

    def drop_1_in_5(frame, seq):
        # drop one random byte in ~20% of chunks
        if random.random() < 0.20:
            i = random.randrange(len(frame))
            return frame[:i] + frame[i+1:]
        return frame

    def drop_many(frame, seq):
        # drop up to 3 bytes in ~40% of chunks
        if random.random() < 0.40:
            n = random.randint(1, 3)
            idx = sorted(random.sample(range(len(frame)), n))
            out = bytearray(frame)
            for i in reversed(idx):
                del out[i]
            return bytes(out)
        return frame

    ok = True
    ok &= run_case("PUT no loss (10000B)", data, no_drop)
    ok &= run_case("PUT no loss (10240B, mult)", data2, no_drop)
    ok &= run_case("PUT 1 byte / 20% chunks", data, drop_1_in_5)
    ok &= run_case("PUT 1-3 bytes / 40% chunks", data, drop_many)
    ok &= run_get_case("GET no loss (10000B)", data, no_drop)
    ok &= run_get_case("GET 1 byte / 20% chunks", data, drop_1_in_5)
    ok &= run_get_case("GET 1-3 bytes / 40% chunks", data, drop_many)
    ok &= run_case("PUT client says BAD", data, no_drop, lie=True)
    ok &= run_get_case("GET client reports bad CRC", data, no_drop, lie=True)
    print("\nALL PASS" if ok else "\nFAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
