# Lisa Serial FTP — file transfer between an Apple Lisa and a modern computer over a serial cable

The client: a terminal-like Pascal-based program LisaSerialFtpClient.obj that runs in the Apple Lisa Workshop 3.0 development environment. All user interaction happens here. It supports downloading and uploading both text and binary files (note: the
Lisa OS stores them differently), listing and deleting files on both sides. It
transfers data in retryable chunks; every chunk is verified and, if needed,
retried; the whole file size, CRC16 checksum and byte sum is verified at the
end, and the file is accepted only then.

The server: a non-interactive Python-based LisaSerialFtpServer.py program that runs on your modern computer and listens for commands.

The tool is useful for developing applications for the Apple Lisa (both Lisa Office system and Workshop apps). Here is a typical development setup:
* Write source code (in Pascal, Assembler, Basic, etc) on your PC using your favorite editor.
* Keep the LisaSerialFtpServer.py server running on your PC (it auto-reconnects as needed, there is no need to restart it).
* Download your code onto your Lisa: go on your Lisa (either on a real Lisa or the LisaEm emulator running on the same PC), launch the LisaSerialFtpClient app in the Workshop development environment,
  and run one-or-more "get" commands to download your files.
* Compile, link and test your app on the Lisa.
* A powerful option: use a Workshop Macro like [this one](src/client/LisaSerialFtpClientMake.text)  on your Lisa to download, compile and link your code with just one command!

![The client on a real Lisa: a get of its own source as a text file, then a put of its executable file](assets/photos/lisa-in-action.jpg)


## Installing and running the client app in Lisa Workshop

Clone this repository to a folder on your PC.

### Get the `LisaSerialFtpClient.obj` executable into Workshop.

* Option 1: The DC42 floppy disk image file `LisaSerialFtpClient.dc42` contains the
executable file `LisaSerialFtpClient.obj`. Boot into Lisa Workshop, then insert ("mount") that
disk image, e.g. using a FloppyEmu or ESFloppy floppy disk hardware
emulator on a real Lisa, or in the LisaEm emulator, from the File -> Insert Diskette menu item.
Once in Workshop, you will have to copy it to your ProFile hard
drive, this way: Press `f` to get into the File Manager submenu, then press `c` for
"copy", then enter source filename `-UPPER-LisaSerialFtpClient.obj`, enter,
then the target filename `-LOWER-LisaSerialFtpClient.obj`, and follow the
prompts to complete the copying. Here we assume that you are using a Lisa 2/5
whose boot menu has the floppy disk above the hard disk (hence we use "UPPER"
in the source filename). On a Lisa 2/10 the boot menu looks different: your
floppy disk drive is named "LOWER", and the hard disk is "UPPER" in Workshop.
Press `q` to exit to the main Workshop menu.

* Option 2: Most people use a ProFile hardware emulator on their Lisa-s such as ESProfile (the most popular one) and others; the hard disk image files sit on an micro-SD card.
Plug the SD card in your PC, and use my "copy" tool at https://github.com/TorZidan/LisaFileSystemTool#12-lisafilesystemtoolperfilepy---adding-replacing-deleting-and-getting-files
to copy file [LisaSerialFtpClient.obj](./LisaSerialFtpClient.obj) onto your desired disk image, then move the mocroSD card back into the ProFile hardware emulator.

* Option 3: Just copy the
  [profile-5MB-LOSAndWorkshop3-UseWith88iorom.image](./profile-5MB-LOSAndWorkshop3-UseWith88iorom.image)
  to your SD card and boot it up into Workshop. It has a clean installation of
  Lisa Office System 3.1, Workshop 3.0 and the LisaSerialFtpClient.obj
  executable. As the file name says, it should be used with I/O Rom version 88
  (aka Lisa 2/10). Booting it up using any other I/O Rom version may result in
  a boot error.

Note: once you have `LisaSerialFtpClient.obj` on your ProFile hard drive, you can get newer versions of this tool (if any) by running this tool.

### Ensure Serial Port B is set to "Serial Cable" in the Workshop Preferences
In Workshop, press `s` for Sys-Manager submenu, then `p` to run the Preferences app.
In it: check the "Connect Devices" checkbox and make sure that "Serial B Connector"
is set to "Serial Cable, like so:

![thisishowyoudoitbabe](assets/screenshots/workshop-prefrences-app.png)

### Running the client app in Lisa Workshop

To run the app: in the main Workshop menu, press `r` (for "run"), then type the
program name `LisaSerialFtpClient` (there is no need to type the file
extension), enter. The program will start, it will attempt to connect to the
server, and will give you a `>` command prompt. Type `hello` and `help` to see what's there.

## Running the server on your PC

Requirements: **Python 3.5 or newer**.  On Linux and macOS nothing else is
needed: the server uses only Python's standard library.  On Windows it also
needs **pyserial** (`pip install pyserial`), because Windows has no `termios`.

The server shares one folder with the Lisa, the "home folder" given with
`--home_dir`: the Lisa can list, get, put and delete the files in it and in its
subfolders, and nothing outside it.  The folder must exist (the server does
not create it, so a mistyped path is an error rather than a new, empty share).

Run these commands from the folder where you cloned this repository:

### On Linux

```sh
mkdir -p ~/lisa-share
python3 src/server/LisaSerialFtpServer.py --port /dev/ttyUSB0 --home_dir ~/lisa-share
```

### On macOS

```sh
mkdir -p ~/lisa-share
python3 src/server/LisaSerialFtpServer.py --port /dev/cu.usbserial-1410 --home_dir ~/lisa-share
```

Use the `/dev/cu.*` name of the adapter, not its `/dev/tty.*` twin (opening that
one can wait for a carrier signal that never comes).

### On Windows

```
pip install pyserial
mkdir C:\lisa-share
python src\server\LisaSerialFtpServer.py --port COM3 --home_dir C:\lisa-share
```

The device name (`/dev/ttyUSB0`, `/dev/cu.usbserial-1410`, `COM3`) is that of
your USB serial adapter: see the FAQ
[How to find the device name of my USB serial dongle?](#how-to-find-the-device-name-of-my-usb-serial-dongle)
. If using the LisaEm emulator (instead of a real Apple Lisa computer), use its virtual port instead, e.g. `--port /tmp/lisaem-port-b`. Find detailed instructions
at [Setting up LisaEm for a 1st time](https://github.com/arcanebyte/lisaem/blob/master/LisaEmAsASoftwareDevelopmentEnvironment.md#setting-up-lisaem-for-a-1st-time).

`--port` can also be left out: it defaults to `--port auto`, which finds the
port itself.  If there is just one serial or PseudoTTY port, it uses
that one; if there are more, it lists them and chooses one this way: a symbolic link to a pty
(such as `/tmp/lisaem-port-b` (created by the LisaEm emulator) first, then the pty of a running LisaEm, then a USB adapter, then a
built-in serial port.  If there
is none yet, it waits for one.  Two narrower variants pick in the same way,
but only among some of the ports:

* `--port auto-pty`: discovers only PseudoTTYs ports, like the ones opened by LisaEm;
* `--port auto-serial`: discovers only the USB adapters and built-in serial ports, use with a
  a real Lisa.

For example, `--port auto` may show:

```
--port auto: ports found on this computer:
  PseudoTTY:  /tmp/b  (-> /dev/pts/0)
  PseudoTTY:  /dev/pts/0  (lisaem, pid 838896)
  USB serial: /dev/ttyUSB0  (USB-Serial Controller D)
--port auto: using /tmp/b (give --port to use another one)
```

When the server starts, it prints a line such as

```
LISA-SERIAL-FTP/v1.0 listening on /dev/ttyUSB0 at 57600 baud, home_dir=/home/me/lisa-share, debug output off
```

and then waits for the Lisa client.  Each command from the Lisa is printed as
it is served.

### Useful options

| Option | What it does |
|---|---|
| `--port DEVICE` | the serial port; default `auto`, also `auto-pty` and `auto-serial` (see above) |
| `--baud N` | the starting baud rate (default 57600); the client's `baud` command can change it later |
| `--debug` | print every protocol line and chunk (the client's `debug on/off` switches it too) |
| `--read-only` | refuse PUT and DELETE: the Lisa can only list and get |
| `--timeout S` | seconds to wait for a command or abort reply (default 15) |

`python3 src/server/LisaSerialFtpServer.py --help` lists them all.

### Stopping the server

The server runs until you stop it with Ctrl-C.

If the port is not there when it starts (LisaEm emulator not running yet, adapter not
plugged in), the server says so once and waits for it.  It also lists the
ports it can find, to help you pick the right `--port` name, and lists them
again when they change (e.g. when you plug the adapter in):

```
serial port /tmp/lisaem-port-b is not there (No such file or directory); waiting for it.  Ctrl-C to stop.
ports found on this computer:
  USB serial: /dev/ttyUSB0  (USB-Serial Controller D)
  PseudoTTY:  /dev/pts/0  (lisaem, pid 838896)
  PseudoTTY:  /tmp/b  (-> /dev/pts/0)
```

USB serial adapters are found on Linux, macOS and Windows; built-in serial
ports (`/dev/ttyS0`, ...) on Linux.  PseudoTTYs are the
pty of each running LisaEm (Linux only) and any link to a pty in `/tmp` or
`/dev`, whatever `--port` says.

If the port goes away
while it runs (the emulator quits, the adapter is unplugged), it says so once,
checks every second, and reopens the port by its path when it is back (a
virtual port created again, e.g. a `socat` link, is picked up too).  It then sends
its banner and carries on.

## Operating the client, commands and a sample session 

Below is a sample client session.

```
LisaSerialFtpClient v1.0 (build 2026-11-18 16:52)
file transfer over RS232B at 57600 baud
commands: list, !list, get, put, delete, !delete, baud, hello, timeout-factor,
          debug, repeat, help, quit

> help
commands:
  list [dir]            list files on the server
  !list [name]          list all filenames starting with [name] on the
                        default volume
  get [-c] [-b|-t] <remote> [local]  download: as text (CRLF/LF -> CR)
                        if the local or remote name ends in .TEXT,
                        else byte-exact
      -c: skip it if the local copy is the same as the server's
      -b: get as binary (byte-exact) / -t: save as a text file
  put [-c] [-b|-t] <local> [remote]  upload: a .TEXT file as text (lines
                        end in LF), any other file byte-exact
      -c: skip it if the server's copy is the same as the local one
      -b: send as binary (byte-exact) / -t: send as a text file
  delete <remote>       delete a file on the server (asks Y/N); also del
  !delete <local>       delete a file on the Lisa (asks Y/N); also !del
  baud [rate]           show or change the baud rate, here and on the server
  hello                 (re-)send HELLO, wait for the server banner
  timeout-factor [<n> | auto]  show/set timeout multiplier (auto = measure)
  debug [on | off]      show or switch debug output (here and on the server):
                        off = progress marks (. per chunk, x per retry),
                        on  = a line for every protocol line and chunk
  repeat / r            run the last command again
  help / h / ?          show this help
  quit / q              exit
Use hotkey Apple + . to cancel a wait or a transfer at any time.

> list
F 1792 hello.txt
F 4096 image.bin
D 60 src

> get src/hello.txt hello.TEXT
get src/hello.txt -> hello.TEXT  (saving it as text file, converting line endings to \r)
... (100% complete)
received 2 pages of Lisa text into hello.TEXT, verified (CRC 3A7C)

> get image.bin
get image.bin -> image.bin  (saving it as-is, byte-exact)
..... (100% complete)
received 4096 of 4096 bytes into image.bin, verified (CRC 1C2D)

> put notes.TEXT src/notes.txt
put notes.TEXT -> src/notes.txt  (sending it as text file, converting line endings to \n)
..... (100% complete)
sent 120 lines (4321 bytes) from notes.TEXT, verified (CRC 8F3A)

> put image.bin
put image.bin -> image.bin  (sending it as-is, byte-exact)
..... (100% complete)
sent 4096 bytes from image.bin, verified (CRC 1C2D)

> r
repeating: put image.bin
put image.bin -> image.bin  (sending it as-is, byte-exact)
..... (100% complete)
sent 4096 bytes from image.bin, verified (CRC 1C2D)

> quit
```

The client app supports the following commands:

* `put` hands each chunk frame (up to 1023 bytes) to the serial driver in
  one `WRITE_DATA` call, from a global buffer that stays put while the
  driver sends it.  Sending it byte by byte, as earlier versions did,
  made uploads about 3 times slower than downloads.

* `repeat` (or `r`) runs the last command line again, arguments and
  all, e.g. to retry a failed transfer.  It is not itself remembered, so
  `r` twice repeats the same command twice, and neither is an unknown
  command (a typo), so `r` after one still runs the last real command.

* `get` picks the mode from the names.  If the local name is given and
  ends in `.TEXT`, or the remote name ends in `.TEXT` (any case), it
  downloads as text and writes a proper Lisa text file: CRLF, LF and
  lone CR line-endings become Lisa text lines.  The server builds the
  text pages (`GET <name> T`, `lisa_pages`) and the client only stores
  them after the header page it makes itself (`OpenText`), so a text get
  is as fast as a binary one (it moves ~3% more bytes: the padding at
  the end of each page).  Otherwise it writes a byte-exact binary copy with no
  changes.  A remote `.TEXT` file must be saved under a `.TEXT` name
  (`get notes.text notes` is refused, with a suggestion).  `get -b`
  always downloads byte-exact, and `get -t` always as text (e.g. `get -t
  readme.md README.TEXT`); a text download still needs a local name
  ending in `.TEXT`.  The line `get <remote> -> <local>` says which mode
  was chosen.  Both modes are verified by the `DONE` byte/CRC check,
  computed over the received bytes (for text, the pages).
* `get -c` and `put -c` (c for checksum, as in `rsync -c`) first check
  whether the transfer is needed:
  the client works out the size, the CRC-16 and the byte sum (MOD 65536)
  of its local copy -- of a `.TEXT` file, the bytes `put` would send --
  and asks the server (`SUM`) for the same three numbers of its copy.
  It says what it found on one line, e.g.
  `-c: checking if file notes.TEXT is the same on both sides
  ... the two copies differ: transferring it`.  If all three match, the
  answer is `unchanged: the local copy is the same -- not fetched` (or
  `the server's copy is the same -- not sent`) and nothing is
  transferred; this counts as a success for `quit`.  If anything
  differs, or one copy is missing (`no local file to compare, so getting
  the file`, `no copy on the server, so sending the file`, ...), the
  file is transferred as usual.  For text files the server applies the Lisa's
  own rules to its copy first, so a file with CRLF or CR line endings,
  no final line ending, or lines over 255 characters still counts as the
  same as its Lisa copy.  (So `put -c` also skips a file whose server
  copy differs from the Lisa's only in its line endings.)  A text file
  can still look different when it is not: a line that the Lisa stores
  with leading-space compression and that comes back longer than 255
  characters, or a DLE byte at the start of a line -- then it is just
  transferred.  The other way round, two different files of the same
  size pass both the CRC and the sum only very rarely (a single changed
  character, or two within about 4 KB, is always caught; for unrelated
  changes both checks together miss far less than 1 in 65,536); when in
  doubt, a plain `get` or `put` always transfers.  The check reads the
  whole local file, which is several times faster than receiving it.
* `put` picks the mode from the local file name.  A name ending in
  `.TEXT` (any case) is a Lisa text file and is sent as text, the reverse
  of `get`: it reads the file's raw pages and decodes
  the Lisa text format itself (skips the 1024-byte header page, ends each
  page at its CR-NUL, expands DLE leading-space codes, CR -> LF) -- the
  same decoding as LisaFileSystemTool.  It does not use the Pascal `text`
  type: its `READLN` treats CHR(8) as a backspace and can stop early.
  Any other file is sent byte-exact, its bytes unchanged.  `put -b`
  always sends byte-exact, even a `.TEXT` file (its raw pages, header
  included: a byte-exact backup), and `put -t` always as text, if the
  file looks like a Lisa text file: whole 1024-byte pages, and its first
  text page lines of text up to a CR-NUL, then only NULs; anything else
  (e.g. a `.OBJ`) is refused.  The flags `-c`, `-b`, `-t` go before the
  names, in any order (`-b` and `-t` not together).  Both modes
  use the same `PUT` protocol command; the line `put <local> -> <remote>`
  says which mode was chosen.
* `delete <remote>` (or `del`) deletes a file on the server, `!delete
  <local>` (or `!del`) one on the Lisa (`KILL_OBJECT`).  Both first ask
  `delete <name> on the server? (Y/N)` (or `on the Lisa?`) and wait for
  Y or N from the keyboard; any other key is ignored.  Neither checks
  first whether the file exists: the server's answer (`ERR 11 no such
  file`) or the Lisa's error number says so.  The server never deletes a
  folder, and nothing at all with `--read-only`.
* `list` and `!list` stop after every 30 lines with `Type SPACE to
  continue, CLEAR to exit.`  SPACE (or Return) shows the next page,
  CLEAR (keypad; Esc in LisaEm) ends the listing.  `list` first reads the
  whole remote listing (up to 8 KB) and then pages it: the server sends
  each page of the listing only when the client asks for it (`NEXT`),
  and waiting for a key in the middle would keep it waiting too long.
  While a long listing comes in, the client shows its progress, a `.` per
  page of up to 384 bytes (`receiving a long listing ...... 7 pages`).
  A listing over 8 KB is still read to the end, but only its first 8 KB
  are shown, followed by `(listing too long: only the first N entries
  are shown)`.
* The client uses Port B only: the port is hard-coded
  (`devname := 'RS232B'`), and Port A is not supported.  `BAUD` is the
  rate it starts at, which the server must match.  At runtime, `baud <rate>` (300…57600)
  changes the rate here and on the server, and checks the new rate: it
  sends `BAUD <rate>`, the server answers `OK` and switches, and so does
  the client, which then sends `HELLO`.  If no banner comes, both go back
  to the old rate: the client by itself, the server when no valid line
  arrives at the new rate within 10 s, or a garbled one does (the
  client's next `HELLO`, sent at the old rate).  Not connected, it
  changes the Lisa's rate only.  The server goes back to its startup
  rate (`--baud`) when the client quits, and when a garbled line arrives
  at another rate (a client started again, at its built-in 57600), so
  the next client always finds it.
* If the link dies mid-transfer, the transfer does not hang: every
  serial read on the Lisa has a timeout (stretched by the
  timeout-factor), a lost or damaged chunk or reply is retried, and
  after 10 failed tries on one chunk the transfer is aborted (FAIL/OK)
  and the program returns to its prompt.  Apple-period also stops any
  wait.  If the server is gone, the client says so and the next server
  command first sends HELLO again.  The exception is the operating
  system itself blocking inside a serial call: a write whose completion
  never comes back (seen once in LisaEm, at the end of a long upload)
  freezes the program, and even Apple-period cannot stop it; then
  restart the Lisa (or the emulator).

## Compiling and linking the client app

This step is needed only if you made changes to the client app and want to recompile it (Find above a pre-compiled client app).

`LisaSerialFtpClient.text` is a plain Lisa Pascal console program (`USES syscall`),
with three small routines in 68000 assembly (`LisaSerialFtpClientAsm.text`).
Assemble that file, compile the program, and link the two objects with
`IOSPASLIB.OBJ`. It uses only stock OS file APIs (`OPEN`, `READ_DATA`,
`WRITE_DATA`, `DEVICE_CONTROL`, `MAKE_FILE`, ... from the `syscall`
unit).

 The Workshop macro / exec file `LisaSerialFtpClientMake.text` does all
of it, after fetching the newest sources (which requires having a compiled `LisaSerialFtpClient.obj` on the
Lisa).  It fetches the source files from the server's `src/client` folder, so the server must
be "sharing" that folder; for example, in the repository folder:

```sh
python3 src/server/LisaSerialFtpServer.py --port /dev/ttyUSB0 --home_dir src/client
```

How to run the `LisaSerialFtpClientMake.text` macro: in the main workshop menu
press `r` ("run"), type `<LisaSerialFtpClientMake` and enter to run it. Note the
`<` in front: it tells Workshop to run this as a macro. Observe its progress.
At the end, upon success, it will just exit to the Workshop main menu.

## Overview of the files in this repository

| file | runs on | role |
|---|---|---|
| `src/server/LisaSerialFtpServer.py` | Linux, macOS, Windows | serial **server**; shares a directory |
| `src/client/LisaSerialFtpClient.text` | the Lisa (Lisa Pascal) | console **client**; talks to the built-in RS-232 driver |
| `src/client/LisaSerialFtpClientInclude.text` | the Lisa (Lisa Pascal) | part of the client (the `!list` and `!delete` commands), pulled in by `{$I LisaSerialFtpClientInclude.text}`; keep it on the same volume |
| `src/client/LisaSerialFtpClientAsm.text` | the Lisa (68000 assembly) | part of the client: CRC-16, byte sum, byte copy, the text decoder and page writer, 10-15 times faster than Pascal; built with it (see `LisaSerialFtpClientMake.text`) |
| `src/client/LisaSerialFtpClientMake.text` | the Lisa (Workshop exec file) | fetches the client's sources with the current client (the server must share `src/client`), assembles, compiles and links the new one |
| `src/server/tests/*.py` | Linux/macOS | Python tests of the server, with no hardware needed (each file starts with a description of what it tests); `audit_8char.py` checks the client for Lisa Pascal's 8-character name limit |
| `src/client/tests/LisaSerialFtpTest.text` | the Lisa (Workshop exec file) | round-trip test on a real Lisa / LisaEm: get, put, get again, DIFF (the server must share `src/client`) |

## How the Lisa sees the serial ports

In `LISA_OS/OS/source-fsui1.text.unix.txt` the `Alias` procedure maps the
device names `RS232A` and `RS232B` to `#10#1` / `#10#2` (the 6850 SCC,
channels A and B). A Lisa Pascal program therefore just opens them as
files:

```pascal
OPEN (ecode, 'RS232B', refnum, [dread, dwrite]);
```

The client uses `RS232B` (Port B) only; the name is hard-coded.

It issues multiple `DEVICE_CONTROL` commands to setup the serial port, using the RS-232 codes found in the Lisa OS source code
`LISA_OS/LIBS/LIBOS/libos-syscall.text.unix.txt`:
`dvParity=1, dvOutDTR=2, dvOutXON=3, dvOutDelay=4, dvBaud=5, dvInWait=6,
dvInDTR=7, dvInXON=8, dvTypeahd=9`.

## Serial wiring
The Lisa has two DB-25 serial ports on the back labeled "__a" and "__b" (referred in the code as Port A and Port B). This tool works with Port B only: the port is hard-coded in the client (`devname := 'RS232B'`); Port A is not supported.

I use two cables connected together:
* a serial **Null-modem (crossover) cable** with DB-25 male on the Lisa side and DB9 female on the other side, wired like this: TX↔RX, GND↔GND. That is all the
  protocol needs — flow control is done by the protocol itself (see below), so no DTR/DSR/RTS/CTS wiring is required (you can still use a cable that has these wires).
* a serial-to-USB dongle with DB9 male on one side (plugged into the other cable) and a standard USB on the other side (plugged into any USB port of my PC). 
There are many options on the market, including very cheap ones with questionable quality. I can't recommend a specific one.

The back of the Lisa, with the null-modem cable plugged into Port B (the DB-25 port labeled `__b`):

![The back of the Lisa, with the cable in serial Port B](assets/photos/lisa-back-side.jpg)

The two cables: the serial-to-USB dongle (left) and the null-modem cable (right):

![The serial-to-USB dongle and the null-modem cable](assets/photos/cables.jpg)

The two cables plugged together:

![The dongle and the null-modem cable plugged together](assets/photos/cables-connected.jpg)


## Serial line settings

Both sides set the port the same way; there is nothing to configure
(the server and the client set it up themselves):

| setting | value |
|---|---|
| data bits, parity, stop bits | **8N1**: 8 data bits, no parity, 1 stop bit |
| hardware flow control (RTS/CTS, DTR/DSR) | **none**: only TX, RX and GND are needed |
| software flow control (XON/XOFF) | **none** (see [Why no XON/XOFF](#why-no-xonxoff)) |
| baud rate | **57600** by default, the highest the Lisa's Port B can do (see [Baud rates](#baud-rates)); the client's `baud` command changes it on both sides |

There is no parity because every chunk carries a CRC-16, which catches far
more errors than parity would, and the whole file is checked again at the
end.  Flow control is done by the protocol itself: the sender waits for an
ACK after each chunk, so the Lisa is never sent more than it can hold.

On the Lisa the client sets these with `DEVICE_CONTROL` at startup
(`dvParity`, `dvBaud`, `dvInDTR`, `dvInXON`, `dvOutDTR`, `dvOutXON`, ...);
on the PC the server sets them with `termios` (or pyserial on Windows).

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
1200 baud; `LisaSerialFtpClient.text` reconfigures it with `dvBaud` at startup, and
the `baud <rate>` command changes it again at runtime, here and on the
server (plain `baud` prints the current rate; see `BAUD` below).

## Why no XON/XOFF

XON/XOFF (0x11/0x13) bytes inside binary data are consumed by the
line discipline / driver, which corrupts transfers. Instead:

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

Neither side ever uses XON/XOFF: the server clears IXON/IXOFF on its
port, and the Lisa client sets no handshake.  It could not be used even
for text files: every chunk is a binary frame, whose sequence number,
length and CRC bytes can be 0x11 or 0x13 too.

## Measured transfer speeds

Below are typical speeds at 57600 baud (the default, recommended baud), in bytes per second of file data, for a
file of a few tens of KB copied to or from the ProFile. The Lisa itself is the limit, not the line.

| transfer | real Lisa | LisaEm emulator via Pseudo TTY |
|---|---|---|
| `get`| about 3.1 KB/s | about 54   KB/s |
| `put`| about 2.1 KB/s | about  1.6 KB/s |

Notes:
* A text `get` moves about 3% more bytes than the file's size (the NUL
  padding at the end of each 1 KB page), and the server builds the Lisa
  text pages, so it should come close to a binary `get`.
* A `put` is slower than a `get` because the Lisa sends a frame more
  slowly than it receives one (about 2,600 bytes/s at best).
* In LisaEm the baud rate does not limit the speed: the emulated port
  moves bytes at a pace set by the emulator.
* The server prints the time and speed at the end of each transfer
  (`... verified (crc e317) in 12.3 s (2650 bytes/s)`), so you can
  measure your own setup.

## The LISA-SERIAL-FTP/v1.0 protocol

The server and the client each have a version, `v1.0` (`VERSION` in
`LisaSerialFtpServer.py` and in `LisaSerialFtpClient.text`); the `1` is
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

ASCII command lines, CRLF-terminated, case-insensitive.

Lines are at most **255 characters** (Lisa Pascal's max string length,
`string[255]`): the client silently truncates anything longer, so the
server also truncates any line it sends that would exceed the limit. 
Note: the Lisa editor also does not support lines longer than 255 characters and pops a warning when opening such files: "Line exceeds 255 characters, Truncating line; type SPACE to continue".

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

### Commands

Note: these are the commands that the client and server exchange as part of the LISA-SERIAL-FTP/v1.0 protocol. They are different from the commands being typed into the client app.

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
the client stores, without its header page (`lisa_pages`): the lines of
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
`LisaSerialFtpServer.py`, the same rules as the client's `get`).  The
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
*emulated* time: in LisaEm at a high throttle, a "15 s" timeout is over in a
fraction of a real second.  So the client multiplies all its timeouts by
a timeout-factor.  It starts at 1, right for a real Lisa, and is never
measured by itself (not at startup, not on HELLO): in an emulator, type
`timeout-factor auto`.  The client then sends `CAL` and counts how many
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
  LisaFileSystemTool), and names on the server are UTF-8.  A character
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
* The Lisa file system is case-insensitive, POSIX is not — use exact
  case in remote names.


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
| `CmdSeg` | paging, `list`, `!list` and `!delete` (the whole include file `LisaSerialFtpClientInclude.text`), `help`, `baud`, port setup, the timeout-factor measurement, `hello`, `debug` and the other commands |
| `XferSeg` | the `get` and `put` transfers (`DoGet`, `DoPut`) and the end-of-transfer verification |

A `{$S CmdSeg  }` or `{$S XferSeg }` line in the source starts putting
the following routines into that segment, and `{$S          }` (a blank
name) puts the main program back into the default segment.  The OS loads
a segment the first time one of its routines is called, and calls between
segments need nothing special, so the split changes nothing in how the
program behaves.  The linker places the segments by itself, so `LisaSerialFtpClientMake.text`
needs no changes.

Each segment is now about a third of the program.  When a new routine
makes one of them too big, move a group of routines to another segment
(or start a new one) by adding a `{$S name}` line in front of them;
segment names have at most 8 characters.

The first line the client prints at startup carries a build mark
(`BUILD` in the source), so it is easy to see which build is running on
the Lisa.

## Testing without a Lisa

The tests are in `src/server/tests`; run them from any folder, e.g.
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

`src/server/tests/test_lisaftp.py` spawns the server on a pty pair and emulates the Lisa client: banner,
HELLO, LIST, GET (byte-exact, nested + percent-encoded names), PUT
(byte-exact), the DONE -> OK verification, NAK/retransmit, rejection of
the removed commands (STAT, DEL, MKDIR, PWD), path-traversal rejection,
unknown command, DEBUG ON/OFF, peer restarts (HELLO mid-transfer), QUIT.

`src/server/tests/test_get_readme.py` goes one step further: it re-implements the
Pascal client's GET path (the table-driven CRC16, chunk receive,
text-mode line folding, retry/NAK logic, the DONE -> OK/BAD verification,
FAIL handshake) in Python and GETs the real `README.md` from the
repository folder.  With `--drop-0xff` it simulates the old lisaem PseudoTTY bug
(0xFF bytes dropped on receive): a frame holding a 0xFF (README is text,
so only in a CRC) then always fails its CRC check and the GET must fail.
Whether any frame holds one depends on the README's current contents;
the test says which case it ran.

`src/server/tests/test_put_emulation.py` does the same for the PUT path: it builds the
frame bytes the Pascal `SendChunk` would (checking the CRC with an
independent bit-by-bit CRC-16 against the server's table) and PUTs them
to the server, answering its DONE with OK, and verifies the file is
byte-exact.  It also injects a desync (garbage where a frame header is
expected) and, like `SendChunk`, resends the frame the server NAKs, to
prove the server resynchronises and recovers.

`src/server/tests/test_loss_recovery.py` drives the server's PUT and GET against a
simulated Lisa that drops random bytes from chunks, and checks the
verification (a client that answers BAD, or reports a wrong CRC).

### Testing on the Lisa

`src/client/tests/LisaSerialFtpTest.text` is a Workshop exec file
that tests the client on the Lisa itself.  `LisaSerialFtpClientMake.text`
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

`bench [file]` (not listed in `help`) times the client's own work on the
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

## Design notes / limitations

* The Lisa RS-232 driver (`LISA_OS/OS/source-rs232.text.unix.txt`) is a
  raw byte-stream character device — it has no file concept. All
  "file" semantics live in this protocol.
* File size is not limited by Lisa RAM: both sides stream in 1016-byte
  chunks (the Lisa reads/writes one chunk from/to the local file at a
  time).
* Only one client at a time; the server is single-threaded.
* No authentication — this is a vintage-hobby protocol on a private
  wire. Keep the `--home_dir` directory accordingly, and consider
  `--read-only` if you only want to pull files off the modern machine.
* CRC-16/CCITT-FALSE was chosen because it is easy to implement in both
  Python and 16-bit Lisa Pascal.  Lisa Pascal has no XOR or shift
  operator, so `CRC16N` builds each XOR from 4-bit XOR tables: six table
  lookups and two multiplications per byte, about 225 µs per byte on a
  real Lisa.  The client therefore uses `CRC16A`, the same table-driven
  CRC in 68000 assembly (`LisaSerialFtpClientAsm.text`, with the byte sum
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
  (`lisa_pages`, a copy of `PutLine`; `src/server/tests/test_text_pages.py` checks
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

## FAQ

### Why not use a standard Kermit server instead of this python-based server?

Mainly 2 reasons:

* The kermit server cannot auto-translate \r\n (Windows line-endings) and \n (Linux line-endings) to \r (Lisa line-endings).
This would have to be done on the Lisa client side, which is a CPU-intensive task that would slow down the transfer considerably.
* The Lisa stores text files in a special way (see ????????????):  A Lisa text
  file isn't just lines ending in \r: it has a 1024-byte header page, lines
  packed into 1 KB pages with NUL padding, and leading spaces compressed. A
  Lisa Kermit client would have to build those pages itself, e.g. with Pascal
  WRITELN. This adds about 36 ms per line on a real Lisa. Avoiding it is the
  reason the conversion is done on our server.
* In conclusion: it is possible to implement a Lisa Client app that works with a standard Kermit server, but its transfer speeds will be considerably lower due to the added CPU overhead on the client.


### How to find the device name of my USB serial dongle?

The server needs it for `--port`.  Plug the dongle in first.

**Linux**

```sh
ls -l /dev/serial/by-id/          # each USB serial adapter by name -> its device
ls -l /dev/ttyUSB* /dev/ttyACM*   # the device files themselves
sudo dmesg | grep -i tty          # which device a newly plugged adapter got
```

Most adapters (FTDI, Prolific PL2303, CH340, CP210x) show up as
`/dev/ttyUSB0`, `/dev/ttyUSB1`, ...; some as `/dev/ttyACM0`.  The
number can change when the adapter is plugged in again or another one is
added.  The long name in `/dev/serial/by-id/` stays the same, so it is a
good `--port`:

```sh
python3 src/server/LisaSerialFtpServer.py \
    --port /dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller_D-if00-port0 \
    --home_dir src/client
```

Your user needs to be in the `dialout` group to open the port
(`sudo usermod -aG dialout $USER`, then log in again).  If the name is
right but the Lisa still gets no answer, check that no other program
(an old server, a terminal program) has the port open: two programs
reading one port each get only part of the bytes.
`pgrep -af LisaSerialFtpServer` lists the servers that are running.

A serial port built into the PC (on the motherboard, or a PCI card) is
`/dev/ttyS0` (COM1), `/dev/ttyS1` (COM2), ...  Linux creates `/dev/ttyS0`
to `/dev/ttyS31` whether or not there is hardware behind them; a real one
has a non-zero `type`:

```sh
grep -v '^0$' /sys/class/tty/ttyS*/type   # the real ones, e.g. ".../ttyS0/type:4"
sudo dmesg | grep ttyS                    # e.g. "ttyS0 at I/O 0x3f8 (irq = 4) is a 16550A"
```

It is a true RS-232 port, so it needs only the null-modem cable (no dongle).

**macOS**

```sh
ls /dev/cu.*
```

An adapter shows up as `/dev/cu.usbserial-XXXX` (FTDI, Prolific) or
`/dev/cu.wchusbserialXXXX` (CH340), where `XXXX` is a serial number or
a USB location.  Use the `cu.` device, not the matching `tty.` one: a
`tty.` device waits for the modem's carrier signal before it opens.  If
no new `cu.` device appears when you plug the adapter in, it needs a
driver from its maker (some PL2303 and CH340 adapters do); System
Information (Apple menu > System Information > USB) shows whether macOS
sees the adapter at all.

**Windows**

The adapter gets a COM port name.  Open Device Manager (right-click
Start > Device Manager) and look under **Ports (COM & LPT)**, e.g.
"Prolific USB-to-Serial Comm Port (COM3)", or in PowerShell:

```powershell
[System.IO.Ports.SerialPort]::GetPortNames()
```

Give that name to the server: `--port COM3`.  The number can change
when the adapter is plugged into another USB socket.  If the adapter
shows up with a yellow warning sign, or not under Ports at all, it needs
a driver from its maker (some PL2303 and CH340 adapters do).  Only one
program can have a COM port open: close any terminal program using it
before starting the server.
