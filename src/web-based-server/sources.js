// Lisa Serial FTP -- where the web-based server's files come from.
//
// A source turns a remote name ('docs/a.txt', already decoded from the
// wire) into the file's bytes (readFile: GET, SUM) and a folder name into
// its entries (list: LIST; '' is the top).  A writable one also takes
// uploads (openWrite: PUT) and deletes files (deleteFile: DELETE); see
// the source interface in protocol.js.  Two kinds:
//   FolderSource -- a folder on this computer, picked with the File System
//                   Access API (showDirectoryPicker; Chrome / Edge)
//                   Writable if the user allowed it.
//   WebSource    -- files on the web (read only) under a base URL, read with fetch().
//                   The web server must allow it (CORS); GitHub's
//                   raw.githubusercontent.com does.  Web servers cannot
//                   list a folder, so LIST uses the GitHub API for a
//                   raw.githubusercontent.com address, and an index.json
//                   file in the folder for any other address.

import { ProtocolError, MAX_FILE } from "./protocol.js";

// 'a/b/c.txt' -> ['a', 'b', 'c.txt'], as safe_path() in the Python server:
// '/' is the separator, a leading '/' is ignored, '..' is refused.
// allowEmpty: '' (the top folder, for LIST) gives [].
export function splitName(name, allowEmpty = false) {
  const parts = name.replace(/^\/+/, "").split("/");
  if (parts.includes("..")) throw new ProtocolError(2, "bad name");
  const kept = parts.filter(p => p !== "" && p !== ".");
  if (!kept.length && !allowEmpty) throw new ProtocolError(1, "empty name");
  return kept;
}

function noDir(name) {
  return new ProtocolError(10, "no such directory on server: " + (name || "/"));
}

export class FolderSource {
  // writable: PUT and DELETE allowed (the page has readwrite permission)
  constructor(dirHandle, writable = false) {
    this.dir = dirHandle;
    this.label = dirHandle.name;
    this.writable = writable;
  }

  // the folder that holds name, and name's last part
  async _parent(name) {
    const parts = splitName(name);
    let dir = this.dir;
    try {
      for (const p of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(p);
    } catch (e) {
      if (e.name === "TypeError") throw new ProtocolError(2, "bad name");
      throw new ProtocolError(10, "no such directory");
    }
    return [dir, parts[parts.length - 1]];
  }

  async _isFolder(dir, last) {
    try { await dir.getDirectoryHandle(last); return true; } catch { return false; }
  }

  // PUT: createWritable() writes into a hidden swap file next to the file
  // (Chrome: '<name>.crswap'), and only close() replaces the file with it;
  // abort() throws it away.  getFileHandle(create) makes an empty file at
  // once if there was none, so abort() removes that too: a failed upload
  // leaves nothing behind.
  async openWrite(name) {
    const [dir, last] = await this._parent(name);
    if (await this._isFolder(dir, last))
      throw new ProtocolError(13, `cannot create ${name}: it is a folder`);
    let existed = true;
    try { await dir.getFileHandle(last); } catch { existed = false; }
    let w;
    try {
      const fh = await dir.getFileHandle(last, { create: true });
      w = await fh.createWritable({ keepExistingData: false });
    } catch (e) {
      if (!existed) { try { await dir.removeEntry(last); } catch {} }
      if (e.name === "TypeError") throw new ProtocolError(2, "bad name");
      if (e.name === "NotAllowedError")
        throw new ProtocolError(13, `cannot create ${name}: no permission to write to the folder`);
      throw new ProtocolError(13, `cannot create ${name}: ${e.message}`);
    }
    return {
      write: bytes => w.write(bytes),
      commit: () => w.close(),
      abort: async () => {
        try { await w.abort(); } catch {}
        if (!existed) { try { await dir.removeEntry(last); } catch {} }
      },
    };
  }

  // DELETE: one file, never a folder
  async deleteFile(name) {
    const [dir, last] = await this._parent(name);
    if (await this._isFolder(dir, last))
      throw new ProtocolError(12, "is a folder, not deleted: " + name);
    try {
      await dir.getFileHandle(last);
    } catch (e) {
      if (e.name === "TypeError") throw new ProtocolError(2, "bad name");
      throw new ProtocolError(11, "no such file on server: " + name);
    }
    try {
      await dir.removeEntry(last);
    } catch (e) {
      throw new ProtocolError(13, `cannot delete ${name}: ${e.message}`);
    }
  }

  async readFile(name) {
    const parts = splitName(name);
    let dir = this.dir;
    try {
      for (const p of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(p);
      const fh = await dir.getFileHandle(parts[parts.length - 1]);
      const file = await fh.getFile();
      if (file.size > MAX_FILE) throw new ProtocolError(4, "file too big");
      return new Uint8Array(await file.arrayBuffer());
    } catch (e) {
      if (e instanceof ProtocolError) throw e;
      if (e.name === "NotFoundError" || e.name === "TypeMismatchError")
        throw new ProtocolError(11, "no such file on server: " + name);
      if (e.name === "TypeError")          // a name the browser refuses
        throw new ProtocolError(2, "bad name");
      throw new ProtocolError(5, `cannot read ${name}: ${e.message}`);
    }
  }

  // The browser does not give a folder's size: folders are listed as 0
  // (the Python server shows what the OS says, e.g. 4096).
  async list(name) {
    let dir = this.dir;
    try {
      for (const p of splitName(name, true)) dir = await dir.getDirectoryHandle(p);
    } catch (e) {
      if (e instanceof ProtocolError) throw e;
      if (e.name === "TypeError") throw new ProtocolError(2, "bad name");
      throw noDir(name);
    }
    const out = [];
    for await (const h of dir.values()) {
      if (h.kind === "directory") {
        out.push({ name: h.name, size: 0, isDir: true });
        continue;
      }
      try {
        out.push({ name: h.name, size: (await h.getFile()).size, isDir: false });
      } catch {
        // unreadable (e.g. gone since): left out, as the Python server
        // leaves out what os.stat cannot read
      }
    }
    return out;
  }
}

// https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path...>
const RAW_GITHUB = /^https:\/\/raw\.githubusercontent\.com\/([^/]+)\/([^/]+)\/([^/]+)\/(.*)$/i;

export class WebSource {
  constructor(baseUrl) {
    this.base = baseUrl.endsWith("/") ? baseUrl : baseUrl + "/";
    this.label = this.base;
    this.writable = false;
    const m = RAW_GITHUB.exec(this.base);
    this.github = m ? { owner: m[1], repo: m[2], ref: m[3], prefix: m[4] } : null;
  }

  async _fetch(url) {
    try {
      return await fetch(url, { cache: "no-store" });
    } catch (e) {
      // the browser does not tell a CORS refusal from a network error
      throw new ProtocolError(5, "cannot fetch " + url + " (network error, or the site refuses it)");
    }
  }

  async readFile(name) {
    const url = this.base + splitName(name).map(encodeURIComponent).join("/");
    const resp = await this._fetch(url);
    if (resp.status === 404)
      throw new ProtocolError(11, "no such file on server: " + name);
    if (!resp.ok)
      throw new ProtocolError(5, `cannot fetch ${url}: HTTP ${resp.status}`);
    const len = Number(resp.headers.get("Content-Length") || 0);
    if (len > MAX_FILE) throw new ProtocolError(4, "file too big");
    return new Uint8Array(await resp.arrayBuffer());
  }

  async list(name) {
    const parts = splitName(name, true);
    return this.github ? this._listGithub(parts, name) : this._listIndex(parts, name);
  }

  // The GitHub contents API: allows browsers (CORS), and 60 requests an
  // hour from one address without logging in.
  async _listGithub(parts, name) {
    const g = this.github;
    const path = (g.prefix + parts.join("/")).replace(/\/+$/, "");
    const url = `https://api.github.com/repos/${g.owner}/${g.repo}/contents/`
      + path.split("/").filter(Boolean).map(encodeURIComponent).join("/")
      + `?ref=${encodeURIComponent(g.ref)}`;
    const resp = await this._fetch(url);
    if (resp.status === 404) throw noDir(name);
    if (resp.status === 403 || resp.status === 429)
      throw new ProtocolError(5, "GitHub allows 60 listings an hour without login: try again later");
    if (!resp.ok) throw new ProtocolError(5, `cannot list ${name || "/"}: GitHub API HTTP ${resp.status}`);
    const items = await resp.json();
    if (!Array.isArray(items)) throw noDir(name);      // a file, not a folder
    return items
      .filter(it => it.type !== "submodule")
      .map(it => ({ name: it.name, size: it.type === "dir" ? 0 : it.size || 0, isDir: it.type === "dir" }));
  }

  // Any other site: an index.json in the folder, written by whoever
  // publishes the files:  [{"name": "a.txt", "size": 123},
  //                        {"name": "docs", "dir": true}, ...]
  async _listIndex(parts, name) {
    const url = this.base + parts.map(p => encodeURIComponent(p) + "/").join("") + "index.json";
    const resp = await this._fetch(url);
    if (resp.status === 404)
      throw new ProtocolError(10, `cannot list ${name || "/"}: no index.json there (web sites cannot list folders)`);
    if (!resp.ok) throw new ProtocolError(5, `cannot fetch ${url}: HTTP ${resp.status}`);
    let items;
    try {
      items = await resp.json();
    } catch {
      throw new ProtocolError(5, `cannot list ${name || "/"}: index.json is not valid JSON`);
    }
    if (!Array.isArray(items))
      throw new ProtocolError(5, `cannot list ${name || "/"}: index.json is not a list`);
    return items
      .filter(it => it && typeof it.name === "string" && it.name && !it.name.includes("/"))
      .map(it => ({ name: it.name, size: Number(it.size) || 0, isDir: !!it.dir }));
  }
}
