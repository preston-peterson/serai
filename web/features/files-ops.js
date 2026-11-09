// The file drawer's network half: listing, download, ops, the cross-host relay
// and uploads. All DOM lives in files.js; these take a plain `ctx`
// ({host, path, entries}) and report progress through the callback the caller
// hands them. Errors land as toasts here (they're this module's own failures);
// listDir is the one that rethrows, because the drawer renders that message.

import { get, post } from "../core/api.js";
import { confirm, prompt, toast } from "../core/dialog.js";

export function joinPath(base, part) {
  if (part === "..") {
    const trimmed = base.replace(/\/+$/, "");
    const idx = trimmed.lastIndexOf("/");
    return idx > 0 ? trimmed.slice(0, idx) : (base.startsWith("/") ? "/" : "~");
  }
  return base.endsWith("/") ? base + part : `${base}/${part}`;
}

export function fmtSize(n) {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

export function fmtDate(secs) {
  if (!secs) return "";
  const d = new Date(secs * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const entryIn = (ctx, name) => ctx.entries.find((x) => x.name === name);

export async function listDir(host, path) {
  const data = await get(`/api/files?host=${encodeURIComponent(host)}&path=${encodeURIComponent(path)}`);
  return data.entries || [];
}

/** One /api/files/op call. True on ok; false (after the toast) on error. */
export async function fileOp(ctx, body) {
  try {
    await post("/api/files/op", { host: ctx.host, ...body });
    return true;
  } catch (err) {
    toast(err.message, "error", 5000);
    return false;
  }
}

/** Cross-host copy with the running byte count. The relay streams NDJSON -- one
 *  {"moved":N} per chunk, then a terminal {"ok"} or {"error"} -- so a multi-GB
 *  folder reports instead of sitting silent behind a blocked POST. True only on
 *  that terminal ok, so a cut still deletes the source only on success. */
export async function transferWithProgress(ctx, srcHost, srcPath, dest, label, onProgress) {
  let last = null;
  try {
    const res = await fetch("/api/files/transfer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ host: ctx.host, path: dest, src_host: srcHost, src_path: srcPath }),
    });
    if (!res.ok || !res.body) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.error || `server said ${res.status}`);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();                       // keep the partial line for next read
      for (const ln of lines) {
        if (!ln.trim()) continue;
        const msg = JSON.parse(ln);
        last = msg;
        // no total to divide by: a tar stream doesn't know its own size, and a
        // du-based guess would disagree with it. Show the honest number.
        if (msg.ok === undefined && msg.error === undefined) onProgress(`${label} · ${fmtSize(msg.moved)}`);
      }
    }
  } catch (err) {
    onProgress("");
    toast(`Transfer failed: ${err.message}`, "error", 5000);
    return false;
  }
  onProgress("");
  if (!last || last.error) {
    toast(`Transfer failed: ${(last && last.error) || "no response"}`, "error", 5000);
    return false;
  }
  toast(`Copied ${label} · ${fmtSize(last.moved || 0)}`, "ok", 3000);
  return true;
}

export function dlUrl(host, fullPath) {
  return `/api/file?host=${encodeURIComponent(host)}&path=${encodeURIComponent(fullPath)}`;
}

// Fetch the bytes in the page, then hand the download manager a blob: URL.
//
// The obvious approaches both fail here. window.open() spawns a blank tab the
// browser tears down as soon as the transfer starts. A plain same-origin anchor
// makes the *download manager* fetch the URL itself -- and serai's cert is
// self-signed, so on a phone you've clicked through an interstitial and the
// origin carries a certificate error; Chrome refuses downloads from those and
// reports "Download cancelled". Fetching in the page reuses the connection you
// already accepted, and a blob: URL isn't subject to that origin check.
const DL_WARN_BYTES = 100 * 1024 * 1024;

export async function downloadFile(ctx, name) {
  const e = entryIn(ctx, name);
  // The size the listing already knows, so we can warn before starting. The
  // whole file lands in page memory (see the blob note above), so a big one can
  // take the tab down with it -- likelier on a phone. Say so, don't just try.
  const known = e && !e.is_dir ? e.size || 0 : 0;
  if (known > DL_WARN_BYTES) {
    const ok = await confirm({
      title: `Download ${name}?`,
      message: `${name} is ${fmtSize(known)}. serai has to hold the whole file in this tab's memory `
        + "to download it, which can crash the tab — especially on a phone.",
      confirmLabel: "Download",
    });
    if (!ok) return;
  }

  const t = toast(`Downloading ${name}…`, "ok", 15 * 60 * 1000);
  try {
    const res = await fetch(dlUrl(ctx.host, joinPath(ctx.path, name)), { credentials: "same-origin" });
    if (!res.ok) throw new Error(`server said ${res.status}`);
    const ctype = res.headers.get("content-type") || "application/octet-stream";
    const total = Number(res.headers.get("content-length")) || known || 0;

    // Read the body in chunks so a large file reports progress instead of sitting
    // on a frozen "Downloading…" for a minute. Memory is the same either way.
    let blob;
    if (res.body && res.body.getReader) {
      const reader = res.body.getReader();
      const chunks = [];
      let got = 0, painted = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        got += value.length;
        if (Date.now() - painted > 200) { // don't thrash the DOM on every chunk
          painted = Date.now();
          t.textContent = total
            ? `Downloading ${name} — ${Math.round((got / total) * 100)}% of ${fmtSize(total)}`
            : `Downloading ${name} — ${fmtSize(got)}`;
        }
      }
      blob = new Blob(chunks, { type: ctype });
    } else {
      blob = await res.blob(); // no streaming body available: take it in one go
    }

    // Wrap in a File, not a bare Blob: Chrome on Android ignores the download
    // attribute for blob: URLs and names the result after the blob's UUID -- a
    // File carries its own name, which it does honour.
    const type = blob.type || ctype;
    let src = blob;
    try { src = new File([blob], name, { type }); } catch { /* older browsers: Blob is fine */ }
    const obj = URL.createObjectURL(src);
    const a = document.createElement("a");
    a.href = obj;
    a.download = name;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 0); // tearing it down in the same tick can lose the click
    setTimeout(() => URL.revokeObjectURL(obj), 60000);
    t.remove();
    toast(`Downloaded ${name}`, "ok", 3000);
  } catch (err) {
    t.remove();
    toast(`Download failed: ${err.message}`, "error", 5000); // beats the browser silently cancelling
  }
}

/** Paste the clipboard into the current directory: within a host as a rename or
 *  copy op, across hosts as the byte-counted relay (a cross-host cut deletes
 *  the source only after the transfer said ok). ctx.onPaste picks up the names
 *  that landed, so the drawer can select them. */
export async function pasteClip(ctx, clip, setClip, onPaste) {
  if (!clip) return;
  const cross = clip.host !== ctx.host;
  const items = clip.items; // files relay as bytes, folders as a tar stream
  const pasted = [];
  for (const it of items) {
    let destName = it.name;
    let dest = joinPath(ctx.path, destName);
    if (!cross && dest === it.path) {
      if (clip.op === "cut") continue; // moving something onto itself: nothing to do
      const dot = destName.lastIndexOf(".");
      destName = dot > 0 ? `${destName.slice(0, dot)} (copy)${destName.slice(dot)}` : `${destName} (copy)`;
      dest = joinPath(ctx.path, destName);
    }
    if (cross) {
      const n = items.length > 1 ? `${pasted.length + 1}/${items.length} ${destName}` : destName;
      if (!(await transferWithProgress(ctx, clip.host, it.path, dest, n, ctx.setStatus))) break;
      if (clip.op === "cut" && !(await fileOp(ctx, { op: "delete", host: clip.host, path: it.path }))) break;
    } else {
      if (!(await fileOp(ctx, { op: clip.op === "cut" ? "rename" : "copy", path: it.path, dest }))) break;
    }
    pasted.push(destName);
  }
  if (clip.op === "cut" && pasted.length) setClip(null);
  onPaste(pasted);
}

export async function deleteSelected(ctx, names) {
  names = names.filter((n) => entryIn(ctx, n));
  if (!names.length) return false;
  let what;
  if (names.length === 1) {
    const e = entryIn(ctx, names[0]);
    what = e.is_dir ? `the folder "${names[0]}" and everything inside it` : `"${names[0]}"`;
  } else {
    const preview = names.slice(0, 3).map((n) => `"${n}"`).join(", ");
    what = `${names.length} items (${preview}${names.length > 3 ? ", …" : ""})`;
  }
  const ok = await confirm({
    title: "Delete",
    message: `Delete ${what}? This is permanent.`,
    confirmLabel: "Delete",
    danger: true,
  });
  if (!ok) return false;
  for (const n of names) {
    if (!(await fileOp(ctx, { op: "delete", path: joinPath(ctx.path, n) }))) break;
  }
  return true;
}

/** The created name, or null (cancelled, or the op failed and fileOp toasted). */
export async function newFolder(ctx) {
  const nn = (await prompt({ title: "New folder", label: "Name", confirmLabel: "Create" }) || "").trim();
  if (!nn || nn.includes("/")) return null;
  return (await fileOp(ctx, { op: "mkdir", path: joinPath(ctx.path, nn) })) ? nn : null;
}

/** Uploads go up sequentially; the handle shows per-file progress and an
 *  (i/n) counter for batches. XHR because fetch still can't report upload
 *  progress everywhere. */
export async function uploadFiles(ctx, fileList) {
  const files = [...fileList];
  if (!files.length) return false;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    const label = files.length > 1
      ? `uploading ${f.name} (${i + 1}/${files.length})`
      : `uploading ${f.name}`;
    const err = await uploadOne(ctx, f, joinPath(ctx.path, f.name), label);
    if (err) toast(`upload failed: ${f.name} -- ${err}`, "error", 6000);
  }
  ctx.setStatus("");
  return true;
}

function uploadOne(ctx, file, dest, label) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload?host=${encodeURIComponent(ctx.host)}&path=${encodeURIComponent(dest)}`);
    xhr.upload.onprogress = (ev) => {
      // a folder relay knows how much has moved but not how much there is;
      // an upload always knows both, so this is a true percentage
      if (ev.lengthComputable) ctx.setStatus(`${label} · ${Math.round((ev.loaded / ev.total) * 100)}%`);
    };
    // Resolves with "" on success, else the reason (the server's own message
    // when it sent one) -- every ending resolves, so nothing is left "uploading".
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve("");
      let why = `HTTP ${xhr.status}`;
      try { why = JSON.parse(xhr.responseText).error || why; } catch { /* not JSON */ }
      resolve(why);
    };
    xhr.onerror = () => resolve("network error");
    xhr.onabort = () => resolve("cancelled");
    xhr.ontimeout = () => resolve("timed out");
    const fd = new FormData();
    fd.append("file", file);
    xhr.send(fd);
  });
}
