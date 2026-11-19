# Lisa Serial FTP: technical details

The protocol, testing, baud rates and design notes of Lisa Serial FTP.  How to
set it up and use it is in the [README](README.md).

## The LISA-SERIAL-FTP/v1.0 protocol

The server and the client each have a version, `v1.0` (`VERSION` in
[`LisaSerialFtpServer.py`](src/server/LisaSerialFtpServer.py) and in [`LisaSerialFtpClient.text`](src/client/LisaSerialFtpClient.text)); the `1` is
the major version.  The server names its own in its banner
(`LISA-SERIAL-FTP/v1.0`).  If its major version differs from the
client's (or the banner names none: an older server), `hello` still
connects, prints the `Connected.` line, and then `WARNING: this client
... may not be compatible with this server ...`: use a server and a
client of the same major version.  Likewise, if the `chunk_size=` in the
banner is not the client's own `CHUNK` (1016), or the banner names
none, `hello` connects but warns that `get` and `put` will fail: the
chunk size is fixed at compile time on both sides, and the largest a
real Lisa can take (a whole frame must fit its 1024-byte input buffer).

(Before v1.0 the protocol was called LISA-FTP/2..5, and the client
refused any other version.  Version 2 numbered the ACK: `A <seq>`
instead of a bare `A`.  Version 3 sends `LIST` in pages, `MORE`/`NEXT`.
Version 4 adds `SUM`, for `get -c` / `put -c`.  Version 5 moves 1016
bytes per chunk instead of 256 (about a third faster: each chunk costs
the Lisa ~40 ms of OS calls, whatever its size).)

ASCII command lines, **CRLF-terminated**, case-insensitive.

Lines are at most **255 characters** (Lisa Pascal's max string length,
`string[255]`): the client silently truncates anything longer, so the
server also truncates any line it sends that would exceed the limit. 
Note: the Lisa editor also does not support lines longer than 255 characters and pops a warning when opening such files: `Line exceeds 255 characters, Truncating line; type SPACE to continue`.

### Connection

1. Server sends a banner on startup:
   `Hi from Lisa Serial FTP Server version LISA-SERIAL-FTP/v1.0 : home_dir=<home_dir> chunk_size=<n>\r\n`
   (the shared folder's path as it is, not percent-encoded: it is only displayed; any
   character outside printable ASCII is shown as `?`)
2. Client sends `HELLO`; server re-sends the banner. (This makes the
   handshake work regardless of which side started first.)

If the server is restarted while the client is idle at the prompt, its
startup banner sits in the client's type-ahead buffer and arrives before
the reply to the next command. The client recognizes the banner (the
only line that starts with `L`) and skips it, so the session
self-heals without a relaunch.

### Recovery from a crashed client

The server never gets stuck after the Lisa side crashes or exits: every
wait is bounded by the chunk timeout, and all failure paths return to the
command loop (worst case ~90 s for a GET, ~30 s for a PUT, immediate if
the server was waiting for a command).  If the Lisa restarts quickly and
its `HELLO` arrives while the server is still in the binary phase, the
server detects it — a `HELLO` where a chunk header or an `A`/`N` reply is
expected — discards a partial `PUT` (its `.part` file), and answers the `HELLO` with a
fresh banner, so the new session continues without relaunching the
server.  A `HELLO` buried inside a chunk payload is not detected and
costs one chunk timeout before recovery.

### Status lines

| line | meaning |
|---|---|
| `OK ...` | success (details per command) |
| `ERR <n> <text>` | error; back to command state |
| `DONE <bytes> <crc16hex>` | transfer finished |
| `FAIL <text>` | transfer aborted; **peer must answer `OK`**, then both sides return to command state |

###  Communication commands

Note: these are the commands that the client and server exchange as part of the LISA-SERIAL-FTP/v1.0 protocol. They are **different from the commands typed into the client app** (see [the README](README.md#operating-the-client-commands-and-a-sample-session)).

| command | effect | response |
|---|---|---|
| `HELLO` | re-send banner | banner |
| `LIST [dir]` | list a directory | `OK`, then one line per entry `<F\|D> <size> <name>`, then `DONE`; in pages of at most 384 bytes, each but the last ending with `MORE`, which the client answers with `NEXT` (see below) |
| `GET <name> [T]` | download; `T`: as Lisa text pages (see below) | `OK <size>`; the client sends `GO` once its file is open (or `FAIL <why>`: the get ends), then chunks, then client sends `DONE <bytes> <crc>`, server answers `OK`/`BAD <bytes> <crc>` |
| `PUT <name> <size>` | upload | `OK`, then client sends chunks, then server sends `DONE <bytes> <crc>`, client answers `OK`/`BAD <bytes> <crc>`; a size other than `<size>`: `ERR 5`, discarded; a name the OS refuses: `ERR 13`, before the `OK` (below) |
| `DELETE <name>` | delete a file (the client's `delete`) | `OK deleted`; `ERR 11` no such file, `ERR 12` a folder (never deleted), `ERR 13` the OS refused, `ERR 20` with `--read-only` |
| `SUM <name> T\|B` | size and checksums of a file, for `get -c` / `put -c` | `OK <size> <crc16> <sum16>` (hex), see below |
| `CAL` | timeout-factor calibration | `CAL 2000`, sent exactly 2 real seconds after the request |
| `DEBUG ON` / `DEBUG OFF` | switch the server's debug output (see below); `HELLO` resets it to the startup setting | `OK debug on` / `OK debug off` |
| `BAUD <rate>` | change the rate (the client's `baud`) | `OK <rate>` at the old rate, then the server switches; it keeps the new rate only if a valid line arrives at it within 10 s (else back to the old rate); `ERR 21` an unsupported rate |
| `QUIT` | close the session (the server goes back to its startup rate) | — |
| `MARK <label>` | hidden, for the client's `bench`: the server is the stopwatch | `OK <ms>`, the milliseconds since the previous `MARK` (0 for the first) |
| `ECHO <n>` | hidden, for `bench`: n short lines to time reading them | n lines `A 12345` (1..100), nothing else |
| `FRAMES <n>` | hidden, for `bench`: chunks to time receiving them | n chunks of 1016 made-up bytes and the empty end-of-file chunk, as `GET` sends them (1..400), no `OK` before or `DONE` after |

`PUT`: the server receives the file into a temporary `<name>.part` in
the same folder, and renames it to `<name>` only after the client's `OK`
verdict.  Until then an older file of that name stays as it is; a failed
`PUT` (a bad verdict, too many errors, a client that restarts or goes
away) removes the `.part` file and leaves the older file untouched.
`LIST` shows a `.part` file like any other file (one is left behind only
if the server itself is stopped in the middle of a `PUT`).

`GET <name> T`: the server sends the text pages of the Lisa `.TEXT` file
(see [LisaOsTextFileSpecification.txt](LisaOsTextFileSpecification.txt)) the client stores, without its header page (`lisa_pages`): the lines of
`lisa_text` (below), each ending in CR, 2..223 leading spaces as DLE +
CHR(32 + n), a line that does not fit in its page (1023 bytes) starting
the next, each page padded with NULs to 1024 bytes.  `<size>`, the
chunks and the `DONE` check are over these pages.

`SUM`: `<size>`, the CRC-16 (the same as the chunks') and the sum of all
bytes MOD 65536, over the file's bytes as they are (`B`) or, for `T`,
over the bytes the file would come back as after a round trip through
the Lisa as a text file: LF, CRLF and a lone CR each end a line, lines
over 255 characters are split after every 255 (the Lisa's limit), and a
last line without a line ending gets an LF (`lisa_text` in
[`LisaSerialFtpServer.py`](src/server/LisaSerialFtpServer.py), the same rules as the client's `get`).  The
client computes the same three numbers on its own copy: for a `.TEXT`
file, on the bytes `put` would send.

`LIST` pages: the page size (384 bytes, `MORE\r\n` included, `OK` and
`DONE` counted too) keeps a page well inside the Lisa's 1024-byte input
buffer.  A short listing is one page: no `MORE` at all.  If no `NEXT`
comes within `--timeout`, the server sends `MORE` again (a lost line),
up to 3 times, then gives the listing up.  Any other line in place of
`NEXT` (the user stopped the listing with Apple-period, or the client
restarted and sent `HELLO`) ends the listing, and the server handles that
line as the next command; a late `NEXT` after the listing is over is
ignored.

`CAL` exists because the Lisa's input timeouts (`DEVICE_CONTROL` 14) count
*emulated* time: in [LisaEm](https://lisaem.sunder.net/) at a high throttle, a "15 s" timeout is over in a
fraction of a real second.  So the client multiplies all its timeouts by
a timeout-factor.  It starts at 1, right for a real Lisa, and is never
measured by itself (not at startup, not on HELLO): **in an emulator, type
`timeout-factor auto`**.  The client then sends `CAL` and counts how many
1-second timeouts expire before the reply; that count / 2 is the
timeout-factor (about 100 at 512 MHz).  It sends HELLO first only if it
is not connected yet.  `timeout-factor` prints the factor, and
`timeout-factor <n>` sets it by hand; measure again after changing the
emulator's throttle.  A missing server is never
fatal on the Lisa side: the client reports it and stays at its prompt, and
Apple-period stops any wait or transfer.

During `get`/`put` the client shows its progress: one `.` per chunk that
got through and one `x` per failed try (a NAK or no reply) that is
retried, 60 marks per line.  Each line ends with how much of the file
is done, e.g. `(23% complete)`, and so does the last, shorter line
(`(100% complete)` when the whole file went through).  The empty chunk
that ends the file counts as a `.` too.

`debug on` / `debug off` on the Lisa switches the debug output on both
sides (the client sends `DEBUG ON`/`DEBUG OFF`; `debug` alone shows the
setting).  Both the client screen and the server console then show:

* **debug off** (the default): each command, the progress marks, and
  the outcome (e.g. `ok: sent 200 bytes ..., verified (crc e317)` or
  `FAILED: ...`).  On the server, a successful get or put also shows its
  time and speed: `... verified (crc e317) in 1.2 s (167 bytes/s)`,
  and a line on where a chunk's time went, averaged over the chunks that
  went through at the first try (min-max in brackets).  For a get: `the
  frame leaves the PC`, `the Lisa until its ACK` (receiving, checking,
  writing, the ACK), `the server`.  For a put: `the Lisa until its next
  frame starts` (the ACK, reading, decoding, the CRC), `the frame
  arrives`, `the server`.  With debug on, each chunk's line shows them.
* **debug on**: no progress marks; instead a line for every protocol line
  sent or received (`sent: GET x`, `received: OK 200`) and for every
  chunk, with what happened to it, e.g.
  `chunk 3: 1016 bytes received, CRC ok -> ACK`,
  `chunk 4: rejected, incomplete: 200 of 1016 bytes (bytes lost) -> NAK`,
  `chunk 4: 1016 bytes sent (try 2) -> accepted (ACK)`.

The server starts with debug output off (`--debug`: on); a HELLO (a new client
session) resets it to that, and the client sends `DEBUG ON` again after
its HELLO when its own debug output is on.

### Binary chunk framing

Both GET and PUT move data in framed chunks:

```
'T' | seq (2 bytes big-endian) | len (2 bytes big-endian) | payload | crc (2 bytes BE)
```

* `crc` = **CRC-16/CCITT-FALSE** (poly `0x1021`, init `0xFFFF`, no
  reflection, no final XOR) over `seq..payload`.
* `seq` counts from 0 and wraps from 65535 to 0, so there is no file
  size limit from the protocol.
* After every chunk the receiver sends `A <seq>` (accept chunk `seq`) or
  `N <expected_seq>` (NAK).  The sender sends the same chunk again on a
  NAK, on no reply within 10 s, and on a garbled reply (a corrupted `A`),
  up to 10 tries, then `FAIL`; a resend is safe, the receiver re-ACKs a
  chunk it already has.  `N <seq+1>` (a NAK for the *next* chunk) counts
  as an ACK: the receiver already has this one, its ACK was lost and its
  own wait for the next chunk ran out first.
* A reply about an *earlier* chunk is stale (for example the re-ACK of a
  duplicate that the sender resent when the first ACK was lost) and is
  skipped: the sender keeps waiting for the reply to the current chunk.
  Because every reply carries its chunk number, the replies can never
  fall one step behind the chunks.
* A chunk with `len = 0` ends the transfer.  Its receiver sends the
  ACK and then `DONE` straight away, so a lost ACK is handled at both
  ends: the sender takes a `DONE` in place of the ACK as the ACK; and a
  receiver waiting for the verdict that gets the end-of-file chunk again
  (both lines were lost, or the sender's wait ran out before they came)
  ACKs it again and resends `DONE`, instead of reading the frame as a
  garbled verdict and deleting a good file.
* The receiver finishes with `DONE <total bytes> <crc16 of all payload>`
  (GET: the Lisa sends it; PUT: the server sends it).
* The sender compares that with what it sent and answers
  `OK <bytes> <crc>` or `BAD <bytes> <crc>`, always with **its own**
  numbers.  The receiver checks those numbers too and keeps the file only
  on `OK` with numbers equal to its own; otherwise it deletes it (GET: the
  Lisa never writes the target; PUT: the server removes its copy).  The
  hex CRC may be in either case.

### Names

* `/` is the path separator, as on the server, under the shared folder
  (`--home_dir`): `get Lisa/Docs/report.txt` → `<home_dir>/Lisa/Docs/report.txt`.
  (The Lisa itself separates path parts with `-`; there `/` is just a
  character in a file name.)  Without a local name, `get` saves the file
  under the last part of the remote name (`report.txt`).
* On the Lisa a `-` separates the parts of a path (`-volume-name`), so
  without a local name `get` refuses a remote file name with a `-`, and
  suggests one with `_` instead: `get notes/my-file.txt` gives
  `error: my-file.txt has a '-', which the Lisa reads as a path
  separator`, `e.g. get notes/my-file.txt my_file.txt`.  With a local
  name given, the name is used as it is.
* A Lisa file name has at most 32 characters: `get` refuses a longer
  local name before it starts (`error: the Lisa file name is 36
  characters long; at most 32 are allowed`) and asks for a shorter one.
* Names are percent-encoded in LIST output and accepted encoded in
  commands (e.g. `sub%20dir` for `sub dir`).
* Lines on the wire use **Mac Roman**, the Lisa's character set (as in
  [LisaFileSystemTool](https://github.com/TorZidan/LisaFileSystemTool)), and names on the server are UTF-8.  A character
  the Lisa has is sent as itself: `café.txt` on the server is listed as
  `café.txt` on the Lisa (`é` = byte 0x8E), and a Lisa name with `é` is
  stored as `café` in UTF-8.  A character the Lisa lacks (e.g. `中`), and
  a byte of a server name that is not UTF-8, are listed percent-encoded
  as UTF-8 bytes (`%E4%B8%AD`, `%FF`); typed that way they name the same
  file.  In a reply, a character the Lisa lacks becomes `?`.  (Whether
  every Mac Roman character looks right on the Lisa's screen is not
  checked yet.)
* The server sets no length limit of its own on names (the OS's applies:
  a name it refuses is an error reply); it rejects any `..` escape.  The
  32-character limit of a Lisa file name is the client's concern (see
  above).
* The Lisa file system is case-insensitive, POSIX is not — **use exact
  case in remote names**.


### Code segments

The Lisa Pascal compiler puts a program's code into *segments*, and a
segment can hold at most 32 KB of code.  A program without segment
directives is a single segment, so once the client grew past 32 KB the
linker stopped with `Code size too big`.

The client is therefore split into three segments with the `{$S name}`
directive, the same way Apple's own programs do it (for example the OS
test shell, `nwshell`):

| Segment | Contents |
|---|---|
| blank (default) | the serial port routines, the CRC, the chunk protocol (`SendChunk`, `RecvChunk`, `ReadFrame`) and the main program: the code every transfer runs all the time |
| `CmdSeg` | paging, `list`, `!list` and `!delete` (the whole include file [`LisaSerialFtpClientInclude.text`](src/client/LisaSerialFtpClientInclude.text)), `help`, `baud`, port setup, the timeout-factor measurement, `hello`, `debug` and the other commands |
| `XferSeg` | the `get` and `put` transfers (`DoGet`, `DoPut`) and the end-of-transfer verification |

A `{$S CmdSeg  }` or `{$S XferSeg }` line in the source starts putting
the following routines into that segment, and `{$S          }` (a blank
name) puts the main program back into the default segment.  The OS loads
a segment the first time one of its routines is called, and calls between
segments need nothing special, so the split changes nothing in how the
program behaves.  The linker places the segments by itself, so [`LisaSerialFtpClientMake.text`](src/client/LisaSerialFtpClientMake.text)
needs no changes.

Each segment is now about a third of the program.  When a new routine
makes one of them too big, move a group of routines to another segment
(or start a new one) by adding a `{$S name}` line in front of them;
segment names have **at most 8 characters**.

The first line the client prints at startup carries a build mark
(`BUILD` in the source), so it is easy to see which build is running on
the Lisa.

## Testing without a Lisa

The tests are in [`src/server/tests`](src/server/tests); run them from any folder, e.g.
from the repository folder:

```sh
python3 src/server/tests/test_lisaftp.py
python3 src/server/tests/test_get_readme.py
python3 src/server/tests/test_put_emulation.py
python3 src/server/tests/test_loss_recovery.py
```

or all of them:

```sh
for t in src/server/tests/test_*.py; do python3 "$t" || echo "FAILED: $t"; done
```

[`src/server/tests/test_lisaftp.py`](src/server/tests/test_lisaftp.py) spawns the server on a pty pair and emulates the Lisa client: banner,
HELLO, LIST, GET (byte-exact, nested + percent-encoded names), PUT
(byte-exact), the DONE -> OK verification, NAK/retransmit, rejection of
the removed commands (STAT, DEL, MKDIR, PWD, DELTESTS), path-traversal rejection,
unknown command, DEBUG ON/OFF, peer restarts (HELLO mid-transfer), QUIT.

[`src/server/tests/test_get_readme.py`](src/server/tests/test_get_readme.py) goes one step further: it re-implements the
Pascal client's GET path (the table-driven CRC16, chunk receive,
text-mode line folding, retry/NAK logic, the DONE -> OK/BAD verification,
FAIL handshake) in Python and GETs the real [`README.md`](README.md) from the
repository folder.  With `--drop-0xff` it simulates the old lisaem PseudoTTY bug
(0xFF bytes dropped on receive): a frame holding a 0xFF (README is text,
so only in a CRC) then always fails its CRC check and the GET must fail.
Whether any frame holds one depends on the README's current contents;
the test says which case it ran.

[`src/server/tests/test_put_emulation.py`](src/server/tests/test_put_emulation.py) does the same for the PUT path: it builds the
frame bytes the Pascal `SendChunk` would (checking the CRC with an
independent bit-by-bit CRC-16 against the server's table) and PUTs them
to the server, answering its DONE with OK, and verifies the file is
byte-exact.  It also injects a desync (garbage where a frame header is
expected) and, like `SendChunk`, resends the frame the server NAKs, to
prove the server resynchronises and recovers.

[`src/server/tests/test_loss_recovery.py`](src/server/tests/test_loss_recovery.py) drives the server's PUT and GET against a
simulated Lisa that drops random bytes from chunks, and checks the
verification (a client that answers BAD, or reports a wrong CRC).

### Testing on the Lisa

[`src/client/tests/LisaSerialFtpTest.text`](src/client/tests/LisaSerialFtpTest.text) is a Workshop exec file
that tests the client on the Lisa itself.  [`LisaSerialFtpClientMake.text`](src/client/LisaSerialFtpClientMake.text)
fetches it onto the Lisa.  It gets the client's sources and puts a file
in `src/client/tests`, so start the server sharing `src/client` for it
(as for `LisaSerialFtpClientMake.text`), e.g. in the repository folder:

```sh
python3 src/server/LisaSerialFtpServer.py --port /dev/ttyUSB0 --home_dir src/client
```

and run it from the Workshop: in the main Workshop menu press `r`
("run"), type `<LisaSerialFtpTest` and enter (the `<` runs it as an exec
file, as for `<LisaSerialFtpClientMake`).  It:

1. deletes `LisaSerialFtpClientTest1.text`, `LisaSerialFtpClientTest2.text`
   and `LisaSerialFtpClientTest3.text` left from an earlier run;
2. gets `LisaSerialFtpClient.text` into `LisaSerialFtpClientTest1.text` and
   checks it exists;
3. puts `LisaSerialFtpClientTest1.text` back as
   `tests/LisaSerialTest-upload1.txt`, gets that into
   `LisaSerialFtpClientTest2.text`, checks it exists, and removes the
   uploaded copy with `delete tests/LisaSerialTest-upload1.txt` (answering its Y/N question).
   On the way it tries `get -c` and `put -c`: of the files it just
   transferred (each must say `unchanged`), with no local copy
   (`LisaSerialFtpClientInclude.text` into `LisaSerialFtpClientTest3.text`:
   transferred) and of a different file over that one
   (`LisaSerialFtpClientAsm.text`: transferred).  The macro cannot see these messages, so
   it prints what to expect and you check them on the screen;
4. runs `DIFF` on `LisaSerialFtpClientTest1.text` and
   `LisaSerialFtpClientTest2.text`: no differences means
   the round trip (text conversion both ways) is exact.

**`bench [file]`** (not listed in `help`) times the client's own work on the
Lisa, with the server as the clock (`MARK`), 10 KB per test, each the
way it is done now and the way it was done before.  It first measures
the cost of a measurement itself (the `MARK` line out, its `OK` back)
and takes it off every time it prints.  Then: sending 20 short lines
(one `WRITE_DATA` per line) and receiving 20 (`ECHO`;
read ahead, or a `READ_DATA` per character), receiving 10 chunks
exactly as `get` does but with no file (`FRAMES`), 10 progress marks on
the screen alone, the CRC and the byte sum (assembly), writing a binary
file (256 bytes or 4 KB per `WRITE_DATA`) and a text file (`WRITELN`),
and, with a file given, reading it (1 KB or 4 KB per `READ_DATA`),
decoding it (for a `.TEXT` file, in assembly) and the whole `-c` check
of it.  (The Pascal timings, and writing a text file the way `get` once
did, with `TXTPUT`, and sending a `WRITE_DATA` per character, are
commented out in the source.)  It prints bytes/s for each; the server
prints the times too.  Give it a small file (e.g.
`bench LisaSerialFtpClientInclude.text`); a file that is not there is
reported before anything is measured, and `bench` stops.

## Baud rates

The Lisa's SCC divisor math (see `CONTROLIT` dcode 5 in
`LISA_OS/OS/source-rs232.text.unix.txt`) is:

* **Port A**: divisor = 125000 / baud (integer)
* **Port B**: divisor = 115200 / baud (integer)

| baud | Port A error | Port B error |
|---|---|---|
| 1200 | 0.8 % | 0 % |
| 2400 | 0.16 % | 0 % |
| 4800 | 0.16 % | 0 % |
| 9600 | 0.16 % | 0 % |
| 19200 | **8.5 % — do not use** | 0 % |
| 38400 | **8.5 % — do not use** | 0 % |
| 57600 | **8.5 % — do not use** | 0 % |
| 115200 | not possible | not possible |

**57600 is the maximum.**  The driver programs the SCC's baud-rate
generator with the time constant `divisor − 2`, which is 0 at 57600 on
Port B.  At 115200 it would be −1: the driver then writes `$FF` and `0`
to WR12/WR13, a time constant of 255 — about 450 baud.  The `baud`
command therefore stops at 57600.  (Faster rates, e.g. the 230.4 kbit/s
SDLC of lisa-share, need the SCC's clocks set up directly, bypassing the
OS serial driver.)  In LisaEm the baud rate does not limit the transfer
speed at all: the emulated port moves bytes at a pace set by the
emulator, whatever the rate.

**Default: 57600**, the maximum.  The client uses Port B only (it is
hard-coded: `devname := 'RS232B'`), where every rate up to 57600 is
exact; the Port A column above is only for reference, as the client
does not support Port A.  The driver initializes at
1200 baud; [`LisaSerialFtpClient.text`](src/client/LisaSerialFtpClient.text) reconfigures it with `dvBaud` at startup, and
the `baud <rate>` command changes it again at runtime, here and on the
server (plain `baud` prints the current rate; see `BAUD` in the
protocol's [commands](#communication-commands) above).

## Why no XON/XOFF

XON/XOFF (0x11/0x13) bytes inside binary data are consumed by the
line discipline / driver, which **corrupts transfers**. Instead:

* the transfer is **ACK-gated**: the sender transmits one chunk frame
  (up to 1023 bytes: 1016 of data) and waits for `A <seq>` before
  sending the next;
* the Lisa client enlarges its type-ahead buffer to 1024 bytes
  (`dvTypeahd`; the most the driver allows, `size_typeah: 34..1024` in
  `source-rs232`), and a whole frame must fit it.  At 57600 baud a real
  Lisa copies the bytes of a frame to a waiting `READ_DATA` more slowly
  than they arrive, so most of the frame piles up in the type-ahead
  buffer, and a byte past 1024 is dropped and fails the read.  So the
  chunk is 1016 bytes (a 1023-byte frame): 1024-byte chunks worked at
  19200 baud but never at 57600.
  The driver's full threshold is switched off too (`DCp(9, 1024, -1,
  32767)`), or it would send XOFF at it.  In a `get` the client holds
  back the ACK of a chunk until it has done any disk write that chunk
  causes (`holdack`, `AckNow`), so the next frame never arrives during
  a disk write.
  Sending the ACK at once and writing while the next frame arrives was
  tried and measured the same (about 3.1 KB/s for a binary get):
  apparently the Lisa OS does no serial transfer while a disk operation
  is in progress, so nothing overlaps.  Holding the ACK is also the safer of
  the two, as a 1023-byte frame leaves only 1 byte free in the buffer;
* a `list` reply, the only reply longer than a chunk, comes in **pages**
  of at most 384 bytes: each page but the last ends with `MORE`, and the
  server sends the next page only when the client answers `NEXT`, i.e.
  once it has read the page.  Before version 3 the server sent the whole
  listing at once; a long one (about 1,600 bytes for 54 files)
  overflowed a real Lisa's 512-byte buffer, which the client empties more
  slowly than even 9600 baud fills it, so the `DONE` line was lost and
  the client reported "no reply from the server".  LisaEm never showed
  this, as it holds back every byte until the Lisa has read the one
  before.

**Neither side ever uses XON/XOFF**: the server clears IXON/IXOFF on its
port, and the Lisa client sets no handshake.  It could not be used even
for text files: every chunk is a binary frame, whose sequence number,
length and CRC bytes can be 0x11 or 0x13 too.

## Design notes / limitations

* The Lisa RS-232 driver (`LISA_OS/OS/source-rs232.text.unix.txt`) is a
  raw byte-stream character device — it has no file concept. All
  "file" semantics live in this protocol.
* File size is not limited by Lisa RAM: both sides stream in 1016-byte
  chunks (the Lisa reads/writes one chunk from/to the local file at a
  time).
* **Only one client at a time**; the server is single-threaded.
* **No authentication** — this is a vintage-hobby protocol on a private
  wire. Keep the `--home_dir` directory accordingly, and consider
  `--read-only` if you only want to pull files off the modern machine.
* CRC-16/CCITT-FALSE was chosen because it is easy to implement in both
  Python and 16-bit Lisa Pascal.  Lisa Pascal has no XOR or shift
  operator, so `CRC16N` builds each XOR from 4-bit XOR tables: six table
  lookups and two multiplications per byte, about 225 µs per byte on a
  real Lisa.  The client therefore uses `CRC16A`, the same table-driven
  CRC in 68000 assembly ([`LisaSerialFtpClientAsm.text`](src/client/LisaSerialFtpClientAsm.text), with the byte sum
  `SUM16A` and the byte copy `MOVEB`).  At startup it checks them against
  known results (`AsmSelfTest`, then the CRC's standard check value) and
  stops if they are wrong.  The Pascal versions are kept in the source,
  commented out.  (A `SET OF
  0..15` laid over an `integer`, where `(a + b) - (a * b)` is the XOR,
  was tried first: it failed its self-test on the Lisa.)
* Where a real Lisa's time goes (`bench`, 10 KB each, before these
  changes): CRC in Pascal 2.3 s, byte sum 0.7 s, a Pascal procedure call
  per byte 0.6 s, writing a binary file 256 bytes per `WRITE_DATA` 2.0 s
  (about 50 ms per call), writing a text file with `WRITELN` 5.8 s (about
  36 ms per line), reading 1 KB per `READ_DATA` about 100 ms per call.
  So the client now does the CRC, the sum, the text decoding (`DECODEP`)
  and the folding of received text into Lisa lines and pages (`TXTPUT`)
  in assembly, reads files 4 KB per `READ_DATA` (`TBUF`),
  writes them 4 KB per `WRITE_DATA` (`WBLOCK`), and built a Lisa text
  file's 1 KB pages itself (`PutLine`, `TxtPage`, `TxtEnd`) instead of a
  `WRITELN` per line.  Since then the server builds the pages
  (`lisa_pages`, a copy of `PutLine`; [`src/server/tests/test_text_pages.py`](src/server/tests/test_text_pages.py) checks
  that the two agree), and text gets run at the speed of binary ones;
  the Lisa's own page writer (and `TXTPUT`) is commented out in the
  source.  The header page
  still comes from the Pascal run-time system: `REWRITE` + `CLOSE` makes a
  text file of just that page (`OpenText`), and the pages go after it.
* Each OS call on the serial port costs about 6 ms on a real Lisa, so
  the client makes as few as it can.  It sends a whole chunk frame
  (`txframe`) and a whole protocol line with its CR LF (`txline`, in
  `SendRaw`) with one `WRITE_DATA` each; both buffers are globals, not on
  the stack, because the driver keeps reading from them while it sends
  (a stack buffer moved meanwhile was the suspected cause of garbage sent
  by an early version).  Before, a line went out a character at a time:
  an ACK cost ~40 ms per chunk.  Reading a chunk takes two `READ_DATA`
  calls: the first byte, then the rest of the frame, whose length the
  client knows from the file size (`OK <size>`), into a read-ahead queue
  (`rxq`), from which the header, data and CRC are then taken.  Reading a
  reply
  line whose length is known -- the ACK `A <n>` that `put` waits for
  after each chunk -- takes two (`ReplyLine`): the first byte, then the
  rest in one call into a small read-ahead queue (`rxq`), from which the
  line is then taken as usual; so a different reply (a NAK, a stale ACK,
  `FAIL`, an ACK with `DONE` right behind it) is still read correctly,
  and every other read takes the queued bytes first.  The same holds for
  a frame that is not the expected one (a repeat of the chunk before, or
  one with bytes lost).
* The server resynchronises after a corrupted or partially-lost frame:
  on a non-`T` byte where a frame header is expected it NAKs, then
  discards bytes until the next `T` (the CRC rejects any false sync),
  so the peer's retransmission is accepted instead of the transfer
  wedging.
