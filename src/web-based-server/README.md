# Lisa Serial FTP Server in a web page

This is another implementation of the [`LisaSerialFtpServer.py`](../server/LisaSerialFtpServer.py) server, in as a web page: 
it runs in the browser, with nothing to install.
It talks to the same Lisa client ([`LisaSerialFtpClient.text`](../client/LisaSerialFtpClient.text)) through the
[**Chrome browser Web Serial API**](https://developer.chrome.com/docs/capabilities/serial).

## Running it 

Just open https://torzidan.github.io/LisaSerialFtp/src/web-based-server/ in **Chrome or Edge** browser
on your PC.

## How to use it

Make sure the Lisa and your PC are connected with a [serial cable](../../README.md#serial-wiring).

Pick the files to serve: a folder on your PC (the Lisa can also put and delete
files there), or files on the web (read only).
Then click "Choose port and start", choose your USB serial adapter and click "Connect".
Keep the tab visible while it serves: browsers slow down hidden tabs.
It does not work with LisaEm, whose serial port is a PseudoTTY port that the browser cannot open.


On the Lisa side, run the LisaSerialFtpClient.obj app, as described in the 
[README](../../README.md#installing-and-running-the-client-app-in-lisa-workshop)

 It can serve files from one of two places:

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

## Running it from a locally-hosted web page on your PC

This is useful if you want to make code changes to it.

It needs **Chrome or Microsoft Edge** on a computer, and the page must come from `https://` or from
`http://localhost`. Browsers do not allow `file://` pages to load these scripts. 

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
| [`index.html`](index.html) | the web page |
| [`app.js`](app.js) | the web page's code: connects the controls and the log to the server |
| [`protocol.js`](protocol.js) | the protocol: a port of the Python server's command code. No browser APIs, so it runs in Node too |
| [`serial.js`](serial.js) | the Web Serial port, with the `read(n, timeout)` the protocol code expects |
| [`sources.js`](sources.js) | where the files come from: a local folder (also written to, for `put` / `delete`), or a base URL (and how each is listed) |
| [`tests/test_protocol.mjs`](tests/test_protocol.mjs) | tests in Node, with a fake Lisa client: `node src/web-based-server/tests/test_protocol.mjs` |
| [`tests/test_serial.mjs`](tests/test_serial.mjs) | tests of `serial.js` with a mock Web Serial port (reading, and the close-and-reopen of `BAUD`): `node src/web-based-server/tests/test_serial.mjs` |

`protocol.js` follows the Python server's names and rules (`send_chunk`, `cmd_get`, `lisa_pages`, ...), so
the Python docstrings and [TECHNICAL.md](../../TECHNICAL.md) describe it too. The test checks its text pages
byte for byte against the Python `lisa_pages()`, its `SUM` numbers against the Python ones, and its `LIST`
names against `name_to_wire()`.
