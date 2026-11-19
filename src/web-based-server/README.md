# Lisa Serial FTP Server in a web page

The [`LisaSerialFtpServer.py`](../server/LisaSerialFtpServer.py) server as a web page: it runs in the browser, with nothing to install.
It talks to the unchanged Lisa client ([`LisaSerialFtpClient.text`](../client/LisaSerialFtpClient.text)) through the
**Web Serial API**, and serves files from one of two places:

- **a folder on this computer**, picked with the File System Access API. With **Allow put and delete**
  ticked (the default), the browser asks for write access when you choose the folder, and the Lisa can
  `put` and `delete` there too.

- **files on the web**, under a base address, read with `fetch()`. The default is this project on GitHub
  (`https://raw.githubusercontent.com/TorZidan/LisaSerialFtp/main/`), so on the Lisa
  `get src/client/LisaSerialFtpClient.text` fetches the client source. The site must allow browser
  downloads (CORS); `raw.githubusercontent.com` does, most sites do not.
  Web sites cannot list a folder, so `list` (LIST) works in one of two ways:
  - for a `raw.githubusercontent.com` address, through the GitHub contents API. GitHub allows 60 such
    requests an hour from one address without logging in; after that, `list` gets an error for a while
    (`get` keeps working).
  - for any other address, from an `index.json` file in each folder that should be listable, which
    whoever publishes the files writes:
    ```json
    [{"name": "hello.txt", "size": 6}, {"name": "docs", "dir": true}]
    ```
    A folder without one gets `ERR 10`.

  Files on the web are read only: `put` and `delete` get `ERR 20 server is read-only`.

You can change the files to serve while the server runs: pick another folder, switch between
the folder and the web, edit the address (press Enter), or tick or untick uploads. The log says
"now serving ...", and the Lisa gets the new files from its next command on. A transfer that has
already started finishes with the old files.

## Commands

| Command | State |
|---|---|
| `HELLO` | the banner, as the Python server's (`home_dir=` shows the folder name or the base address) |
| `DEBUG ON/OFF` | yes (the client sends it right after `HELLO`) |
| `GET <name>` / `GET <name> T` | yes: binary and Lisa text pages, CRC check, NAK / resend, lost ACKs, `FAIL`, a restarted client |
| `SUM <name> T\|B` | yes (for `get -c`): size, CRC and byte sum, of the file as Lisa text for `T` |
| `LIST [dir]` | yes (for `list`): sorted, names encoded as the Python server does (`%20` for a space), in pages of at most 384 bytes with `MORE` / `NEXT`. Folders show size 0: browsers do not give a folder's size |
| `PUT <name> <size>` | yes, into a folder (see below): CRC check, NAK, re-ACK of a resent chunk, a lost `DONE`, `FAIL`, a size mismatch, a restarted client |
| `DELETE <name>` | yes, in a folder: one file, never a folder |
| `CAL` | yes (for `timeout-factor auto`): answered 2 s after it came |
| `MARK` / `ECHO` / `FRAMES` | yes (for `bench`): the ms between marks, lines `A 12345`, made-up chunks |
| `QUIT` | yes |
| `BAUD <rate>` | yes: `OK <rate>` at the old rate, then the switch. The new rate is on trial: if no valid command comes at it within 10 s, or a garbled line comes, the page goes back to the old rate, as the Python server does. A garbled line at a changed rate, and `QUIT`, go back to the rate the page was started at |

So every command of the client works. Anything else gets `ERR 99 unknown command`, as from the Python
server.

Web Serial cannot change the rate of an open port, so for `BAUD` the page closes the port and opens it
again at the new rate. The page's baud rate field follows: start the page at the
rate the Lisa client is at, and let the client's `baud` command change both.

After a `get` (and the bench's `FRAMES`) the log shows where the time of a chunk went, like the Python
server: the frame being handed to the port, the Lisa until its ACK, and this page. One difference: the
Python server waits until the frame has left the PC, and Web Serial cannot. So "the frame handed to the
port" is shorter than the Python server's "the frame leaves the PC", and "the Lisa until its ACK" is
longer by the same amount; their sum is comparable.

An upload replaces a file only once it is verified, like the Python server's `<name>.part` file. Here the
browser does it: `createWritable()` writes into a hidden swap file, `<name>.crswap` in Chrome, and only
when the client's OK comes is the file replaced by it. Until then an older file of that name is
untouched. A failed upload removes the swap file, and a new file it had created. A `.crswap` file
can only be left behind if the browser itself stops in the middle of an upload.

## Running it

It needs **Chrome or Microsoft Edge** on a computer, and the page must come from `https://` or from
`http://localhost`. Browsers do not allow `file://` pages to load these scripts. To try it locally:

```
cd src/web-based-server
python3 -m http.server 8000
```

Then open <http://localhost:8000/>, choose where the files come from, set the baud rate (the same as on the
Lisa), and click **Choose port and start**. The browser asks which serial port to use. Stop
`LisaSerialFtpServer.py` first if it is running: only one program can have the port open.

On GitHub Pages the folder works as it is (it is all static files).

If the USB serial adapter is unplugged, the server says so in the log and waits. When you plug
it in again it reopens the port at the same baud rate, with no need to click Start, as the Python
server does. A transfer under way when the adapter was pulled fails; the Lisa can retry it. Chrome
finds the adapter again by its USB vendor and product id. For an adapter without a USB serial
number, Chrome may not let the page use it again without asking; if it is not found, click
**Stop**, then **Choose port and start**.

Keep the tab visible during a transfer: browsers slow down timers in hidden tabs, which can break the
protocol's timeouts.

Web Serial opens real serial ports only (USB adapters, and built-in ports on some systems), not the
pseudo-terminal that LisaEm uses. To test with LisaEm, use the Python server.

## Files

| File | What it is |
|---|---|
| [`index.html`](index.html) | the page |
| [`app.js`](app.js) | the page's code: connects the controls and the log to the server |
| [`protocol.js`](protocol.js) | the protocol: a port of the Python server's command code. No browser APIs, so it runs in Node too |
| [`serial.js`](serial.js) | the Web Serial port, with the `read(n, timeout)` the protocol code expects |
| [`sources.js`](sources.js) | where the files come from: a local folder (also written to, for `put` / `delete`), or a base URL (and how each is listed) |
| [`tests/test_protocol.mjs`](tests/test_protocol.mjs) | tests in Node, with a fake Lisa client: `node src/web-based-server/tests/test_protocol.mjs` |
| [`tests/test_serial.mjs`](tests/test_serial.mjs) | tests of `serial.js` with a mock Web Serial port (reading, and the close-and-reopen of `BAUD`): `node src/web-based-server/tests/test_serial.mjs` |

`protocol.js` follows the Python server's names and rules (`send_chunk`, `cmd_get`, `lisa_pages`, ...), so
the Python docstrings and [TECHNICAL.md](../../TECHNICAL.md) describe it too. The test checks its text pages
byte for byte against the Python `lisa_pages()`, its `SUM` numbers against the Python ones, and its `LIST`
names against `name_to_wire()`.
