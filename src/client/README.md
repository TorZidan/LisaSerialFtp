# The LisaSerialFtp clients

This folder holds the two Apple Lisa clients of [LisaSerialFtp](../../README.md). Both speak the same `LISA-SERIAL-FTP/v1.0` chunked CRC-16 protocol over **Serial Port B (`RS232B`)**, so both work with **`LisaSerialFtpServer.py`** and the **Chrome/Edge Web Serial server** at up to **57600 baud**:

| Program | Runs in | Main source |
|---|---|---|
| `LisaSerialFtpClient` | the Lisa Workshop: a console program (see the [main README](../../README.md)) | `LisaSerialFtpClient.text` |
| `LosSerialFtpClient` | the Lisa Office System (LOS 3.1): a desktop tool, **Serial FTP** | `LosSerialFtpClient.text` |

Most of their code is the same and is in shared files, included in both programs with the Lisa Pascal file-inclusion directive (`{$I LisaSerialFtpXfer.text}`): the serial port routines, the CRC, the chunk protocol, the transfer engine (`get`, `put`, the `-c` check) and the server commands (`list`, `hello`, `baud`, `timeout-factor`, `debug`, `delete`). Each program adds its own user interface: console output, paging, help and the local-file commands, and for the Office System client its window, menus and Filer handling. Both link the same 68000 routines.

---

## `LosSerialFtpClient`: Serial FTP for the Apple Lisa Office System

`LosSerialFtpClient` is a native **Apple Lisa Office System (LOS 3.1)** desktop application implementing the LisaSerialFtp file-transfer client using QuickDraw, the LOS Window Manager, Alert/Menu Manager, and Filer protocol.

- **Native Lisa Office System Tool**:
  - Launches from its desktop icon (**Serial FTP**), participates in the full LOS Filer lifecycle (*Set Aside*, *Set Aside Everything*, *Save & Put Away*, `fcSuspend`, `fcTerminate`, `SYS_TERMINATE` / `ImDying`), and cleanly releases `RS232B` when set aside or closed so other LOS tools can use the serial port.
- **QuickDraw User Interface**:
  - **Top Info Banner**: QuickDraw status badge (`CONNECTED` / `READY` / `OFFLINE` rounded pill), active baud rate, current Lisa working volume, and timeout multiplier.
  - **Scrollable Monospaced Console Pane**: 240-line scrollback buffer (kept in a private OS data segment with automatic heap fallback), smooth QuickDraw `ScrollRect` scrolling, selectable fixed-width fonts (`20 Pitch`, `15 Pitch`, `12 Pitch`), interactive `> ` command prompt with blinking XOR caret and `Up`/`Down` history recall, `WMLsb` vertical scroll bar, and `WMLgrow` window resize box.
  - **Bottom Status & Graphical Progress Bar**: Displays live transfer status on the left and a proportional QuickDraw graphical progress bar (`0%..100%`) on the right during downloads and uploads.
  - **Pull-Down Menus & Dialog Band**: Every common operation is available from the 5 LOS menus (`File`, `Transfer`, `Local`, `Baud`, `Options`) with `DialogFolder` prompts under the menu bar, or by typing commands directly at the `> ` prompt.
- **Full `LisaSerialFtp` Protocol & Local Disk Management**:
  - `get` and `put` with automatic `.TEXT` <-> UTF-8/LF conversion (68000 assembly `DECODEP`), binary (`-b`), text (`-t`), and skip-if-unchanged (`-c` comparing size, CRC-16, and 16-bit byte sum).
  - Atomic downloads into a temporary file (`LosSerialFtpClientTemp.TEXT` / `.BIN`) renamed to the target only after end-to-end byte count + CRC-16 verification.
  - Multi-page server `list` and local volume `!list` (showing free disk space, file sizes, block counts, dates, and attributes), `delete` and `!delete` (protecting `{...}` LOS desktop/tool files), `!volume` selector across mounted Lisa disks (`-#12-`, `-#2#1-`, `-UPPER-`, `-LOWER-`, etc.), dynamic `baud` switching (`300..57600`), and `timeout-factor auto` calibration for fast LisaEm throttling.
  - Responsive **Apple-.** cancellation and live window-update / Filer event servicing mid-transfer.

---

## Files

Shared by both programs (keep them on the same Lisa volume as the program you build):

| File | Purpose |
|---|---|
| `LisaSerialFtpDecl.text` | the protocol constants, the transfer types, the engine's global variables, the 68000 routines (`EXTERNAL`) |
| `LisaSerialFtpUtil.text` | string, number and date helpers |
| `LisaSerialFtpSerial.text` | the serial port: `DEVICE_CONTROL`, progress marks, block and line input/output |
| `LisaSerialFtpProto.text` | the CRC-16 and the chunk protocol (`SendChunk`, `RecvChunk`, `ReadFrame`, ...) |
| `LisaSerialFtpXfer.text` | the transfer engine: `get`, `put`, the `-c` check, `.TEXT` decoding, temp files |
| `LisaSerialFtpCmds.text` | the server commands: `list`, `hello`, `baud`, `timeout-factor`, `debug`, `delete` |
| `LisaSerialFtpClientAsm.text` | the 68000 assembly routines (`CRC16A`, `SUM16A`, `MOVEB`, `DECODEP`) |

Each shared file's header says what the including program must declare before it (the console output `ConWr`/`ConWrLn`, the Office System's `BeginIO`/`EndIO`, `SetupPort`, ...).

The Workshop client:

| File | Purpose |
|---|---|
| `LisaSerialFtpClient.text` | the main program: console output, paging, help, port setup, `bench` |
| `LisaSerialFtpClientInclude.text` | the local-file commands `!list`, `!delete`, `!volume` |
| `LisaSerialFtpClientMake.text` | Lisa Workshop `$EXEC` file: fetches the sources from the server, assembles, compiles and links `LisaSerialFtpClient.OBJ` |
| `tests/` | Workshop exec files that test the client on the Lisa |

The Office System client:

| File | Purpose |
|---|---|
| `LosSerialFtpClient.text` | the main program: the window, menus, dialog band, Filer lifecycle, local-file commands |
| `LosSerialFtpClientAsm.text` | includes `LisaSerialFtpClientAsm.text` (the same 68000 routines), assembled to `LosSerialFtpClientAsm.OBJ` |
| `LosSerialFtpClientAlerts.text` | Phrase-file source (alerts #1, #2 and the 5 pull-down menus) for the Workshop `Alert` tool |
| `LosSerialFtpClientMake.text` | Lisa Workshop `$EXEC` build & `InstallTool` script (installs tool `197001`, `"Serial FTP"`) |
| `icon_48x32.png` | 48 x 32 monochrome desktop icon source |
| `ICON.BIN` | Compiled LOS desktop icon (`{T197001}ICON`) |
| `{T197001}PHRASE` | Compiled phrase file (menus and alerts), built by `make.sh` |

And `make.sh`, which builds both on a PC.

---

## Building

### Option A: Cross-Compiling on a PC (`make.sh`)

With [`LisaPascalCrossCompiler`](https://github.com/TorZidan/LisaPascalCrossCompiler) next to the repository (or pointed to via `LISA_PASCAL`):

```sh
cd src/client
./make.sh                # both programs, put onto the disk images in ../..
./make.sh clean          # delete build/
```

Before compiling each client it checks it for Lisa Pascal's 8-character identifier rule, with the files it includes and against the units it uses (`pascal/audit_8char.py` of `LisaPascalCrossCompiler`; `src/server/tests/audit_8char.py` runs the same check for both clients). For the Workshop client it assembles `LisaSerialFtpClientAsm.text`, compiles `LisaSerialFtpClient.text`, links `build/LisaSerialFtpClient.OBJ` against `IOSPASLIB`, and puts it as `LisaSerialFtpClient.obj` on the floppy image `../../LisaSerialFtpClient.dc42` (`DC42_IMAGE`), where users get the program from (see the [main README](../../README.md)), and on the ProFile image `../../profile-5MB-LOSAndWorkshop3-UseWith88iorom.image` (`PROFILE_IMAGE`). For the Office System client it builds `ICON.BIN`, assembles `LosSerialFtpClientAsm.text`, compiles `LosSerialFtpClient.text`, links `build/LosSerialFtpClient.OBJ` against `IOSPASLIB`, `SYS1LIB`, and `SYS2LIB`, compiles the phrase file, and installs tool `197001` onto the disk image (`DC42_IMAGE`). It uses the tools of `LisaPascalCrossCompiler`: `pascal/lpc.py`, `assembler/`, `linker/`, `alert/`, `icon-tool/png2lisaicon.py` (for `ICON.BIN`), and `file-system-tools/` and `installtool/` to install the tool onto the disk image.

### Option B: Building & Installing on the Lisa

The Workshop client: see "Compiling and linking the client app" in the [main README](../../README.md) (`<LisaSerialFtpClientMake` fetches all the files it needs from the server).

The Office System client (`LosSerialFtpClientMake.text`):

1. Have the Workshop client `LisaSerialFtpClient.OBJ` and `LosSerialFtpClientMake.text` on your Workshop / LOS disk, and start `LisaSerialFtpServer.py --home_dir src/client` (the server must share `src/client`). The first step of `LosSerialFtpClientMake.text` runs the Workshop client to fetch all the files the build needs: `LosSerialFtpClient.text`, the six shared `LisaSerialFtp*.text` files, `LosSerialFtpClientAsm.text`, `LisaSerialFtpClientAsm.text`, `LosSerialFtpClientAlerts.text`, and `ICON.BIN`.
2. In Lisa Workshop, press **`R`** (*Run*) and enter:
   ```
   <LosSerialFtpClientMake
   ```
   This assembles, compiles, and links `LosSerialFtpClient.OBJ`, copies `{T197001}Obj` and `{T197001}ICON`, compiles `{T197001}PHRASE` with `Alert`, and registers tool **`197001` ("Serial FTP Client")** with `InstallTool`.
3. Boot into the **Lisa Office System**: **Serial FTP** appears as a tool icon on your hard disk. Ensure **Preferences -> Connect Devices -> Serial B Connector** is set to **Serial Cable**, start `LisaSerialFtpServer.py` on your PC, and open **Serial FTP**.
