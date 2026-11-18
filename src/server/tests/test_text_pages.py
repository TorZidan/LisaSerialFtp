#!/usr/bin/env python3
"""
test_text_pages.py -- the client's own Lisa text pages (get) and its text
decoder (put, and the -c check) are each other's reverse, and together
give exactly the bytes the server's lisa_text works out (SUM T).

Line-for-line Python copies of LisaSerialFtpClient.text:
  * doget:    DoGet's text loop (LF, CRLF, lone CR; lines split at 255),
              with PutLine / TxtPage / OpenText building the 1 KB pages
              (leading spaces as DLE + CHR(32 + n), at least one NUL at
              the end of each page);
  * textpass: TextPass reading 4 KB at a time (header page skipped, a
              page ends at its first CR-NUL, DLE expanded, CR -> LF);
  * decodep:  the same per page in DECODEP (LisaSerialFtpClientAsm.text),
              which stops when its output buffer is nearly full and is
              called again (tried with its real 2 KB buffer and a small
              one, so that resuming happens often).
The writer holds 4 pages (WBLOCK); before the ACK of each 1016-byte chunk,
TxtEarly writes the full pages once there are 2 or 3 of them.
  * txtput:   TXTPUT (assembly), which does DoGet's text loop and
              PutLine in one, fed 1016-byte chunks: it must write the very
              same file as the Pascal path (and the client's self-test
              case is checked).
Both paths must never write to disk while a chunk is being filed (after
its ACK, when the next frame is arriving): only TxtEarly may write.
For 4000 random texts (long lines, up to 300 leading spaces, bytes over
127, every kind of line ending) textpass(doget(t)) must equal
lisa_text(t).  If this fails after a change to one side, get -c / put -c
would stop finding unchanged files.

The client's page writer (PutLine / TxtPage / TxtEnd, TxtChunk and
TXTPUT) is now commented out in the client: the server builds the pages
(lisa_pages, which mirrors it).  doget and txtput are kept here as the
reference that lisa_pages is checked against.

Run:  python3 tests/test_text_pages.py
"""
import os
import random
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaSerialFtpServer import lisa_text, lisa_pages
TPAGE, TBUF = 1024, 4096

WBLOCK = 4096
OBSIZE = 4096                       # obuf, and TXTPUT's limit
from LisaSerialFtpServer import CHUNK   # 1016

class Writer:                       # PutLine / TxtPage / TxtEnd / OpenText
    def __init__(self):
        self.file = bytearray(TPAGE)  # header page (zeros here)
        self.obuf = bytearray(OBSIZE); self.opos = 0; self.pbase = 0
        self.writes = 0; self.late = 0  # late: writes while filing a chunk
        self.filing = False
    def flush(self):                  # OutFlush
        if self.opos > 0:
            self.file += self.obuf[:self.opos]; self.writes += 1
            if self.filing: self.late += 1
        self.opos = 0
    def early(self):                  # TxtEarly, before the ACK
        if self.pbase > TPAGE:
            n = self.opos - self.pbase
            self.opos = self.pbase; self.flush()
            self.obuf[0:n] = self.obuf[self.pbase:self.pbase + n]
            self.opos = n; self.pbase = 0
    def page(self):                   # TxtPage: end the current page
        if self.opos > self.pbase:
            for k in range(self.opos, self.pbase + TPAGE): self.obuf[k] = 0
            self.opos = self.pbase + TPAGE
            if self.opos == OBSIZE: self.flush()
            self.pbase = self.opos
    def end(self):                    # TxtEnd
        self.page(); self.flush(); self.pbase = 0
    def putline(self, S, N):
        sp = 0
        while sp < N and S[sp] == 32: sp += 1
        sp = min(sp, 223)
        if sp < 2: sp = 0
        need = (2 + (N - sp) + 1) if sp else N + 1
        if self.opos - self.pbase + need > TPAGE - 1: self.page()
        if sp:
            self.obuf[self.opos] = 16; self.obuf[self.opos+1] = 32 + sp; self.opos += 2
        self.obuf[self.opos:self.opos + N - sp] = S[sp:N]; self.opos += N - sp
        self.obuf[self.opos] = 13; self.opos += 1

def doget(data):                    # DoGet's text loop (Pascal), 1 KB chunks
    w = Writer(); line = bytearray(); pend = False
    for i, v in enumerate(data):
        if i % CHUNK == 0:
            w.filing = False; w.early(); w.filing = True   # TxtEarly, ACK
        if v == 10:
            w.putline(line, len(line)); line = bytearray(); pend = False
        else:
            if pend:
                w.putline(line, len(line)); line = bytearray()
            if v == 13: pend = True
            else:
                pend = False
                if len(line) >= 255:
                    w.putline(line, 255); line = bytearray()
                line.append(v)
    w.filing = False
    if pend or line: w.putline(line, len(line))
    w.end()
    doget.writes = w.writes; doget.late = w.late
    return bytes(w.file)

def textpass(f):                    # TextPass, 4 KB reads
    out = bytearray(); first = True; pos = 0
    while True:
        blk = f[pos:pos+TBUF]; pos += len(blk); n = len(blk)
        pg = 0
        while pg < n:
            pe = min(pg + TPAGE, n)
            if first: first = False
            else:
                p = pg; atstart = True
                while p < pe:
                    v = blk[p]; p += 1
                    if v == 13:
                        out.append(10); atstart = True
                        if p < pe and blk[p] == 0: p = pe
                    elif v == 0:
                        if all(b == 0 for b in blk[p:pe]): p = pe
                        else: out.append(0)
                        atstart = False
                    else:
                        cnt = 0
                        if v == 16 and atstart and p < pe: cnt = blk[p] - 32
                        if cnt > 0: p += 1; out += b" " * cnt
                        else: out.append(v)
                        atstart = False
            pg += TPAGE
        if n == 0: break
    return bytes(out)

def txtput(src, n, st, line, ob, chars):
    """TXTPUT (LisaSerialFtpClientAsm.text), step for step.  st: [next
    input byte, chars in line, CR pending, line ready, bytes in ob, page
    start, result]; line: bytearray(256); ob: bytearray(OBSIZE); chars:
    [count].  Returns with st[6] = 0 (input used) or 1 (ob full)."""
    ip, ln, ob_pos, pb = st[0], st[1], st[4], st[5]
    st[6] = 0
    while True:
        if st[3]:                                  # TMAIN: place the line
            sp = 0
            while sp < ln and line[sp] == 32: sp += 1
            sp = min(sp, 223)
            if sp < 2: sp = 0
            need = ln - sp + 1 + (2 if sp else 0)
            if ob_pos - pb + need > 1023:
                while ob_pos < pb + 1024: ob[ob_pos] = 0; ob_pos += 1
                pb = ob_pos
                if ob_pos >= OBSIZE:
                    st[6] = 1; break
            if sp:
                ob[ob_pos] = 16; ob[ob_pos + 1] = 32 + sp; ob_pos += 2
            ob[ob_pos:ob_pos + ln - sp] = line[sp:ln]; ob_pos += ln - sp
            ob[ob_pos] = 13; ob_pos += 1
            chars[0] += ln; ln = 0; st[3] = 0
        if ip >= n: break                          # TLOOP
        v = src[ip]; ip += 1
        if v == 10:
            st[2] = 0; st[3] = 1; continue
        if st[2]:
            ip -= 1; st[2] = 0; st[3] = 1; continue
        if v == 13:
            st[2] = 1; continue
        if ln >= 255:
            ip -= 1; st[3] = 1; continue
        line[ln] = v; ln += 1
    st[0], st[1], st[4], st[5] = ip, ln, ob_pos, pb

def doget_asm(data):                # DoGet with TxtEarly + TxtChunk / TXTPUT, 1 KB chunks
    f = bytearray(TPAGE); st = [0] * 8; line = bytearray(256)
    ob = bytearray(OBSIZE); chars = [0]; opos = pbase = 0
    late = [0]
    def chunk(buf, n, filing=True):
        nonlocal opos, pbase
        st[0] = 0; st[4] = opos; st[5] = pbase
        while True:
            txtput(buf, n, st, line, ob, chars)
            opos, pbase = st[4], st[5]
            if st[6] == 1:
                if filing: late[0] += 1
                f.extend(ob[:opos]); opos = pbase = 0; st[4] = st[5] = 0
            if st[6] == 0: break
    for k in range(0, len(data), CHUNK):
        part = data[k:k + CHUNK]
        if pbase > TPAGE:                          # TxtEarly, then the ACK
            n = opos - pbase
            f.extend(ob[:pbase]); ob[0:n] = ob[pbase:pbase + n]
            opos = n; pbase = 0
        chunk(part, len(part))
    doget_asm.late = late[0]
    if st[2] or st[1] > 0:
        st[2] = 0; st[3] = 1; chunk(b"", 0, False)
    if opos > pbase:                              # TxtEnd
        for k in range(opos, pbase + TPAGE): ob[k] = 0
        opos = pbase + TPAGE
    f.extend(ob[:opos])
    return bytes(f), chars[0]

def decodep(src, n, p, at, dst, dmax):
    """DECODEP (LisaSerialFtpClientAsm.text): from page byte p into dst;
    stops at the page end or when len(dst) > dmax - 224.  Returns p, at, CRs."""
    crs = 0
    while p < n and len(dst) <= dmax - 224:
        v = src[p]; p += 1
        if v == 13:
            dst.append(10); crs += 1; at = 1
            if p < n and src[p] == 0: p = n
        elif v == 0:
            if all(b == 0 for b in src[p:n]): p = n
            else: dst.append(0); at = 0
        elif v == 16 and at and p < n and src[p] - 32 > 0:
            dst += b" " * (src[p] - 32); p += 1; at = 0
        else:
            dst.append(v); at = 0
    return p, at, crs

def textpass_asm(f, dmax):          # TextPass with DECODEP, 4 KB reads
    out = bytearray(); first = True; pos = 0
    while True:
        blk = f[pos:pos+TBUF]; pos += len(blk); n = len(blk); pg = 0
        while pg < n:
            pe = min(pg + TPAGE, n)
            if first: first = False
            else:
                p, at = 0, 1
                while True:
                    dst = bytearray()
                    p, at, _ = decodep(blk[pg:pe], pe - pg, p, at, dst, dmax)
                    out += dst
                    if p >= pe - pg: break
            pg += TPAGE
        if n == 0: break
    return bytes(out)

rng = random.Random(7); bad = 0; bad_asm = 0; bad_put = 0; late = 0; bad_srv = 0
for t in range(4000):
    parts = []
    for _ in range(rng.randint(0, 60)):
        r = rng.random()
        n = rng.randint(250, 700) if r < 0.08 else rng.randint(0, 80)
        lead = rng.choice([0, 0, 1, 2, 5, 40, 230, 300]) if rng.random() < 0.4 else 0
        body = bytes(rng.choice(b"ab c;(){}\x80\xff") for _ in range(n))
        parts.append(b" " * lead + body)
        parts.append(rng.choice([b"\n", b"\r\n", b"\r", b""]))
    data = b"".join(parts)
    f = doget(data)
    want = lisa_text(data)
    # the server's pages (GET name T) are the client's file without its header
    if lisa_pages(data) != f[TPAGE:]:
        bad_srv += 1
        if bad_srv < 3: print("SERVER PAGES MISMATCH", t, len(data))
    if textpass(f) != want:
        bad += 1
        if bad < 3: print("MISMATCH", t, len(data))
    # DECODEP with the real buffer, and with a small one (resumes often)
    if textpass_asm(f, 2048) != want or textpass_asm(f, 300) != want:
        bad_asm += 1
        if bad_asm < 3: print("DECODEP MISMATCH", t, len(data))
    # TXTPUT (fed 1 KB chunks) must write the very same file
    if doget_asm(data)[0] != f:
        bad_put += 1
        if bad_put < 3: print("TXTPUT MISMATCH", t, len(data))
    # and neither may write to disk while filing a chunk (after its ACK)
    if doget.late + doget_asm.late:
        late += 1
        if late < 3: print("A WRITE AFTER THE ACK", t, len(data))
# the worst case for TxtEarly: lines that waste the most of each page
# (250-258 bytes, which pad a page early) and long lines that are split
for t in range(3000):
    parts = []
    for _ in range(rng.randint(1, 40)):
        n = rng.choice([0, 1, 2, 200, 250, 253, 254, 255, 256, 257, 300, 510, 766])
        parts.append(bytes(rng.choice(b"ab") for _ in range(n)))
        parts.append(rng.choice([b"\n", b"\r\n", b"\r"]))
    data = b"".join(parts)
    f = doget(data)
    if lisa_pages(data) != f[TPAGE:]:
        bad_srv += 1
    if doget_asm(data)[0] != f:
        bad_put += 1
    if doget.late + doget_asm.late:
        late += 1
        if late < 3: print("A WRITE AFTER THE ACK (long lines)", t, len(data))
f = doget(b"x" * 63 + b"\n")
ok = (len(f) == 2 * TPAGE and bad == 0 and bad_asm == 0 and bad_put == 0 and late == 0
      and bad_srv == 0 and lisa_pages(b"") == b"")
WANT_WRITES = 5                     # 10 pages after the header: TxtEarly writes 2-3 at a time
f2 = doget((b"x" * 126 + b"\n") * 80)   # bench's file: 80 lines of 126 chars, 8 per page
ok = ok and len(f2) == 11 * TPAGE and doget.writes == WANT_WRITES
print("text pages: %d mismatches (Pascal decode), %d (DECODEP), %d (TXTPUT), %d"
      " (server lisa_pages), %d with a write after an ACK, in 7000 texts;"
      " bench file %d bytes in %d writes"
      % (bad, bad_asm, bad_put, bad_srv, late, len(f2), doget.writes))
# the client's self-test case for TXTPUT
st = [0] * 8; line = bytearray(256); ob = bytearray(OBSIZE); ch = [0]
src = bytes([97, 98, 13, 10, 32, 32, 32, 120, 13, 121])
txtput(src, 10, st, line, ob, ch)
step1 = (st[0], st[1], st[4], st[6], ch[0]) == (10, 1, 7, 0, 6)
st[0] = 0; st[3] = 1
txtput(b"", 0, st, line, ob, ch)
step2 = (st[4], ch[0], st[3]) == (9, 7, 0) and ob[:9] == bytes([97, 98, 13, 16, 35, 120, 13, 121, 13])
print("TXTPUT self-test case:", "as the client expects" if step1 and step2 else "WRONG")
ok = ok and step1 and step2

# put -t: the client's LooksText.  Every text file the page writer makes
# (as get does) passes; files that are not text fail, among them a real
# .OBJ, which is a whole number of 1024-byte pages too.
def looks_text(f):
    if len(f) < TPAGE or len(f) % TPAGE:
        return False
    if len(f) == TPAGE:
        return True
    pg = f[TPAGE:2 * TPAGE]
    endp = 0
    j = 0
    while endp == 0 and j < TPAGE - 1:
        if pg[j] == 0:
            break
        if pg[j] == 13 and pg[j + 1] == 0:
            endp = j + 1
        else:
            j += 1
    return endp > 0 and all(b == 0 for b in pg[endp + 1:])
rng = random.Random(77)
lt_bad = sum(1 for _ in range(500)
             if not looks_text(doget(bytes(rng.choice(b"ab c\n") for _ in range(rng.randint(0, 5000))))))
lt_ok = lt_bad == 0 and looks_text(bytes(TPAGE)) and looks_text(f2)
not_text = [os.urandom(3 * TPAGE), bytes(2 * TPAGE), b"x" * 1500]
for name in ("pascal.obj", "uxref.obj", "test3.obj"):
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
    if os.path.exists(p):
        with open(p, "rb") as fh:
            not_text.append(fh.read())
lt_ok = lt_ok and not any(looks_text(f) for f in not_text)
print("LooksText: %d of 500 written text files rejected; %d non-text files, %s"
      % (lt_bad, len(not_text), "all rejected" if lt_ok else "SOME ACCEPTED"))
ok = ok and lt_ok
print("ALL TESTS PASSED" if ok else "FAILED")
sys.exit(0 if ok else 1)
