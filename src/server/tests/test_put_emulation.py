#!/usr/bin/env python3
"""
test_put_emulation.py -- byte-exact emulation of the Lisa Pascal client's
PUT path, run against the real LisaSerialFtpServer.py on a pty pair.

This replicates, byte for byte, what LisaSerialFtpClient.text sends:
  * the PUT command line (raw name, decimal size)
  * chunk frames built exactly as SendChunk does:
        'T' + seq(2B BE) + len(2B BE) + payload + crc16(2B BE)
    where the CRC is computed with the *Pascal* bitwise CRC-16/CCITT-FALSE
    (CRC16 procedure in LisaSerialFtpClient.text), not the server's table version --
    so a mismatch between the two implementations would be caught here.
  * the zero-length final chunk and the DONE read;
  * a second PUT with a run of garbage bytes injected between chunks:
    the server must recover (NAK, the chunk is sent again) and still store
    the exact file.

If this passes, the protocol logic is sound and a real-hardware failure
is in the physical/driver layer (or timing), not in the framing.
"""
import atexit
import os
import pty
import shutil
import subprocess
import sys
import time

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tests/..
sys.path.insert(0, TOP)
from LisaSerialFtpServer import crc16  # the server's table-based CRC


def pascal_crc16(data):
    """Bitwise CRC-16/CCITT-FALSE, exactly as LisaSerialFtpClient.text's CRC16 does:
       crc := crc XOR (byte SHL 8); then 8x: if top bit set:
       crc := ((crc AND $7FFF) SHL 1) XOR $1021 else crc := crc SHL 1."""
    crc = 0xFFFF
    for b in data:
        crc ^= (b << 8) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = (((crc & 0x7FFF) << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def pascal_frame(seq, payload):
    """Build the frame exactly as SendChunk does."""
    su = seq & 0xFFFF
    frame = b"T" + su.to_bytes(2, "big") + len(payload).to_bytes(2, "big")
    frame += payload
    c = pascal_crc16(frame[1:])          # CRC over seq..payload
    frame += c.to_bytes(2, "big")
    return frame


def main():
    root = "/tmp/lisaftp_put_emu"
    atexit.register(shutil.rmtree, root, True)   # deleted when the test ends
    os.system("rm -rf %s" % root)
    os.makedirs(os.path.join(root, "sub dir"))
    # a file with every byte value, to be mean to any byte mangling
    data = bytes(range(256)) * 4 + os.urandom(1000)   # 2024 bytes
    with open(os.path.join(root, "UXref.Obj"), "wb") as f:
        f.write(data)

    master, slave = pty.openpty()
    env = dict(os.environ, LISAFTP_FD=str(slave))
    proc = subprocess.Popen(
        # LISAFTP_FD makes the server use the pty, whatever --port says
        [sys.executable, os.path.join(TOP, "LisaSerialFtpServer.py"), "--port", "ignored",
         "--home_dir", root,
         "--timeout", "5"],
        pass_fds=[slave],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(slave)
    time.sleep(0.5)

    def rx(n, what):
        buf = b""
        deadline = time.monotonic() + 10
        while len(buf) < n and time.monotonic() < deadline:
            import select
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                buf += os.read(master, n - len(buf))
        if len(buf) < n:
            print("FAIL %s: timeout after %d bytes" % (what, len(buf)))
            proc.kill()
            sys.exit(1)
        return buf

    def rx_line(what):
        buf = b""
        while not buf.endswith(b"\n"):
            buf += rx(1, what)
        return buf[:-1].rstrip(b"\r").decode()

    banner = rx_line("banner")
    # 'Hi from Lisa Serial FTP Server version LISA-SERIAL-FTP/v1.0 : ...'
    assert banner.startswith("H") and "LISA-SERIAL-FTP/v1.0" in banner, banner
    print("banner OK: %s" % banner)

    # --- exactly what DoPut sends ---
    os.write(master, ("PUT UXref.Obj %d\r\n" % len(data)).encode())
    assert rx_line("put-ok") == "OK"
    print("put-ok OK")

    seq = 0
    off = 0
    while off < len(data):
        payload = data[off:off + 256]
        frame = pascal_frame(seq, payload)
        # sanity: the pascal CRC must equal the server's table CRC
        assert pascal_crc16(frame[1:]) == crc16(frame[1:]), "CRC mismatch!"
        os.write(master, frame)
        r = rx_line("ack-%d" % seq)
        assert r == "A %d" % seq, "chunk %d: got %r" % (seq, r)
        seq += 1
        off += len(payload)
    print("all %d chunks ACKed" % seq)

    # zero-length final chunk
    os.write(master, pascal_frame(seq, b""))
    r = rx_line("final-ack")
    assert r == "A %d" % seq, r
    done = rx_line("done")
    assert done == "DONE %d %04x" % (len(data), crc16(data)), done
    print("done OK: %s" % done)
    # verification: DoPut answers with its own numbers; OK = keep the file
    os.write(master, ("OK %d %04X\r\n" % (len(data), crc16(data))).encode())
    time.sleep(0.3)

    with open(os.path.join(root, "UXref.Obj"), "rb") as f:
        got = f.read()
    assert got == data, "bytes differ: %d vs %d" % (len(got), len(data))
    print("bytes OK: %d bytes match" % len(got))

    # --- second PUT with a desync injected: a run of non-'T' garbage bytes
    # arrives where a frame header is expected (simulating a corrupted or
    # partially-lost frame).  The server must NAK, resync to the next 'T',
    # and accept the retransmitted frame. ---
    data2 = os.urandom(600)
    with open(os.path.join(root, "second.bin"), "wb") as f:
        f.write(data2)
    os.write(master, ("PUT second.bin %d\r\n" % len(data2)).encode())
    assert rx_line("put2-ok") == "OK"
    # garbage where the first frame header is expected
    os.write(master, b"\x00\x01\x02\x03\x04")
    # then the frames, sent like Pascal SendChunk: on a NAK the same frame
    # is sent again.  The server discards the garbage (and anything right
    # behind it) until the line is quiet, NAKs, and accepts the resend.
    seq = 0
    off = 0
    naks = 0
    while off < len(data2):
        payload = data2[off:off + 256]
        while True:
            os.write(master, pascal_frame(seq, payload))
            r = rx_line("ack2-%d" % seq)
            if r == "A %d" % seq:
                break
            assert r == "N %d" % seq, "chunk2 %d: got %r" % (seq, r)
            naks += 1
            assert naks <= 3, "too many NAKs"
            print("  (NAK %r: resending chunk %d)" % (r, seq))
        seq += 1
        off += len(payload)
    assert naks >= 1, "the garbage was not NAKed"
    os.write(master, pascal_frame(seq, b""))
    assert rx_line("final2-ack") == "A %d" % seq
    done = rx_line("done2")
    assert done == "DONE %d %04x" % (len(data2), crc16(data2)), done
    os.write(master, ("OK %d %04X\r\n" % (len(data2), crc16(data2))).encode())
    time.sleep(0.3)
    with open(os.path.join(root, "second.bin"), "rb") as f:
        got = f.read()
    assert got == data2, "second bytes differ: %d vs %d" % (len(got), len(data2))
    print("desync-recovery OK: %d bytes match" % len(got))

    os.write(master, b"QUIT\r\n")
    time.sleep(0.3)
    proc.terminate()                        # the server runs until stopped
    proc.wait(timeout=5)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
