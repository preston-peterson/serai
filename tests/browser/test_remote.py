"""Spike item 9, opt-in: remote attach over a multiplexed ssh master, against a
real host you name. Skipped unless SERAI_TEST_REMOTE (address) and
SERAI_TEST_REMOTE_KNOWN_HOSTS (a known_hosts file with its pinned keys) are set;
SERAI_TEST_REMOTE_USER defaults to the local user.

Footprint: a throwaway serai with its own HOME and XDG_RUNTIME_DIR, and an
`ssh` first on PATH that forces `-F <throwaway config>` -- ssh reads the
passwd home's ~/.ssh/config and known_hosts even when $HOME is changed, so this
is what keeps the developer's ssh files untouched. On the remote: two tmux
sessions and one file in ~, all removed at the end.

Measures: both attaches ride one TCP connection (the master); killing the
master reattaches and re-seeds both panes with no duplicated history; a
100 KiB paste arrives byte-exact; keystroke echo latency."""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import tempfile
import time

import pytest

from .test_smoke import Stack, page

REMOTE = os.environ.get("SERAI_TEST_REMOTE", "")
KNOWN = os.environ.get("SERAI_TEST_REMOTE_KNOWN_HOSTS", "")
USER = os.environ.get("SERAI_TEST_REMOTE_USER") or getpass.getuser()
ALIAS = "remote-test"

pytestmark = pytest.mark.skipif(not (REMOTE and KNOWN), reason="set SERAI_TEST_REMOTE and SERAI_TEST_REMOTE_KNOWN_HOSTS")



def paste_payload() -> bytes:
    lines, n = [], 0
    while n < 102400:
        line = f"p{len(lines):05d} " + "abcdefghijklmnopqrstuvwxyz0123456789" * 2 + "\n"
        lines.append(line)
        n += len(line)
    return "".join(lines).encode()[:102399] + b"\n"


def test_remote_attach_over_a_multiplexed_master(tmp_path):
    s = Stack(str(tmp_path))
    home = s.home
    os.makedirs(f"{home}/.ssh", mode=0o700)
    cfg = f"{home}/.ssh/config"
    shutil.copy(KNOWN, f"{home}/.ssh/known_hosts")
    with open(cfg, "w") as f:
        f.write(f"Host {ALIAS}\n    HostName {REMOTE}\n    User {USER}\n"
                f"    UserKnownHostsFile {home}/.ssh/known_hosts\n    GlobalKnownHostsFile /dev/null\n"
                f"    StrictHostKeyChecking yes\n")
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "ssh").write_text(f"#!/bin/sh\nexec {shutil.which('ssh')} -F {cfg} \"$@\"\n")
    (shim / "ssh").chmod(0o755)
    run_dir = tempfile.mkdtemp(prefix="srt-", dir="/tmp")    # short: ssh caps ControlPath near 104 bytes
    s.env.update(PATH=f"{shim}:{s.env['PATH']}", XDG_RUNTIME_DIR=str(run_dir))
    ctl = f"{run_dir}/serai-ssh/%C"

    def rsh(cmd, timeout=20):
        return subprocess.run(["ssh", "-o", "BatchMode=yes", ALIAS, cmd], env=s.env,
                              capture_output=True, text=True, timeout=timeout)

    seed = "for i in $(seq 1 300); do echo vmline-$i; done"
    for name in ("shell-vm1", "shell-vm2"):
        r = rsh(f"tmux kill-session -t ={name} 2>/dev/null; tmux new -d -s {name} -x 120 -y 36 && "
                f"tmux send-keys -t ={name}: {seed!r} Enter")
        assert r.returncode == 0, r.stderr
    payload = paste_payload()
    m = {}
    try:
        s.start(str(tmp_path))

        async def run():
            c = await page(s)
            try:
                await c.wait("[...document.querySelectorAll('.row .name')].filter(n => /vm[12]/.test(n.textContent)).length === 2", 30)
                await c.eval("[...document.querySelectorAll('.row')].find(r => /vm1/.test(r.textContent)).click()")
                await c.eval("[...document.querySelectorAll('.row')].find(r => /vm2/.test(r.textContent))"
                             ".dispatchEvent(new MouseEvent('click', {bubbles: true, shiftKey: true}))")
                both = ("[...document.querySelectorAll('.pane .xterm-rows')].length === 2 && "
                        "[...document.querySelectorAll('.pane .xterm-rows')].every(r => r.innerText.includes('vmline-300'))")
                try:
                    await c.wait(both, 30)
                except TimeoutError:
                    m["panes_text"] = await c.eval("[...document.querySelectorAll('.pane .xterm-rows')].map(r => r.innerText.slice(-400))")
                    raise

                # one TCP connection: the master's; the two control clients hold none
                chk = subprocess.run(["ssh", "-O", "check", "-o", f"ControlPath={ctl}", ALIAS],
                                     env=s.env, capture_output=True, text=True)
                master = int(re.search(r"pid=(\d+)", chk.stderr).group(1))
                ssout = subprocess.run(["ss", "-tnpH", "state", "established", "dst", f"{REMOTE}:22"],
                                       capture_output=True, text=True).stdout
                clients = [int(p) for p in subprocess.run(["pgrep", "-f", f"ControlPath={run_dir}"],
                                                          capture_output=True, text=True).stdout.split()]
                ours = set(clients) | {master}
                conns = [ln for ln in ssout.splitlines() if ours & {int(p) for p in re.findall(r"pid=(\d+)", ln)}]
                m["tcp_conns"] = len(conns)                 # distinct sockets held by the master or its clients
                m["tcp_conns_by_master"] = sum(1 for ln in conns if f"pid={master}," in ln)
                m["clients"] = len([p for p in clients if p != master])
                m["conns"] = [re.sub(r"\s+", " ", ln)[-90:] for ln in conns]
                m["procs"] = subprocess.run(["ps", "-o", "pid,ppid,args", "-p", ",".join(map(str, ours))],
                                            capture_output=True, text=True).stdout.replace(run_dir, "<run>")[-900:]

                # kill the master: both panes reattach and re-seed, history once
                await c.eval("import('/static/views/panes.js').then(m => { window.__oldws = m.all().map(p => p.ws) })")
                t0 = time.monotonic()
                os.kill(master, signal.SIGTERM)
                try:
                    await c.wait("import('/static/views/panes.js').then(m => m.all().every((p, i) => "
                                 "p.ws && p.ws !== window.__oldws[i] && p.ws.readyState === 1 && "
                                 "p.term.buffer.active.length > 300))", 20)
                except TimeoutError:
                    m["after_kill"] = await c.eval("import('/static/views/panes.js').then(m => m.all().map((p, i) => "
                        "({same_ws: p.ws === window.__oldws[i], state: p.ws && p.ws.readyState, "
                        "tail: [...p.el.querySelector('.xterm-rows').innerText.split('\\n')].slice(-6).join(' | ')})))")
                    raise
                await c.wait(both, 10)
                m["reattach_s"] = round(time.monotonic() - t0, 2)
                counts = await c.eval(
                    "import('/static/views/panes.js').then(m => m.all().map(p => { const b = p.term.buffer.active; let n = 0;"
                    " for (let i = 0; i < b.length; i++) { const l = b.getLine(i); if (l && /^vmline-150\\s*$/.test(l.translateToString(true))) n++; }"
                    " return n; }))")
                m["history_copies"] = counts

                # 100 KiB paste, byte-exact
                await c.eval("import('/static/views/panes.js').then(m => m.all()[0].focus())")
                await c.send("Input.insertText", text="stty -echo; head -c 102400 > ~/serai-paste.out; stty echo")
                await c.key("Enter", "Enter", text="\r", vk=13)
                await asyncio.sleep(0.8)
                await c.eval(f"navigator.clipboard.writeText({json.dumps(payload.decode())})")
                t1 = time.monotonic()
                await c.send("Input.dispatchKeyEvent", type="rawKeyDown", code="KeyV", key="v", modifiers=2,
                             windowsVirtualKeyCode=86, commands=["paste"])
                await c.send("Input.dispatchKeyEvent", type="keyUp", code="KeyV", key="v", modifiers=2,
                             windowsVirtualKeyCode=86)
                size = ""
                while time.monotonic() - t1 < 30:
                    size = rsh("stat -c %s ~/serai-paste.out 2>/dev/null").stdout.strip()
                    if size == "102400":
                        break
                    await asyncio.sleep(0.25)
                m["paste_s"] = round(time.monotonic() - t1, 2)
                m["paste_size"] = size
                m["paste_sha_ok"] = rsh("sha256sum ~/serai-paste.out").stdout.split()[:1] == [hashlib.sha256(payload).hexdigest()]

                # keystroke echo latency at a remote shell prompt
                lat = await c.eval(
                    "import('/static/views/panes.js').then(async m => { const p = m.all()[1]; const res = []; const seen = [];"
                    " const dec = new TextDecoder(); const clean = (t) => t.replace(/\\x1b\\[[0-9;?]*[A-Za-z]/g, '');"
                    " await new Promise(r => setTimeout(r, 500));"
                    " for (let i = 0; i < 40; i++) { const ch = String.fromCharCode(97 + i % 26); const t0 = performance.now();"
                    "   const ok = await new Promise(r => { const to = setTimeout(() => { p.ws.removeEventListener('message', h); r(false); }, 2000);"
                    "     const h = (ev) => { if (typeof ev.data === 'string') return; const t = dec.decode(ev.data);"
                    "       if (seen.length < 3) seen.push(JSON.stringify(t));"
                    "       if (clean(t).endsWith(ch)) { clearTimeout(to); p.ws.removeEventListener('message', h); r(true); } };"
                    "     p.ws.addEventListener('message', h); p.send(new TextEncoder().encode(ch)); });"
                    "   if (ok) res.push(performance.now() - t0); await new Promise(r => setTimeout(r, 40)); }"
                    " p.send(new TextEncoder().encode('\\x15')); return {res, seen}; })")
                m["echo_frames"] = lat["seen"]
                m["echo_samples"] = len(lat["res"])
                lat = lat["res"] or [float("nan")]
                m["echo_p50_ms"] = round(statistics.median(lat), 1)
                m["echo_p95_ms"] = round(sorted(lat)[int(len(lat) * 0.95) - 1], 1)
                errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
                m["js_errors"] = str(errs)[:300] if errs else ""
            finally:
                await c.close()

        asyncio.run(asyncio.wait_for(run(), 150))
    finally:
        print("REMOTE MEASUREMENTS:", m)
        print(s.log_tails(20))
        s.stop()
        rsh("tmux kill-session -t =shell-vm1 2>/dev/null; tmux kill-session -t =shell-vm2 2>/dev/null; rm -f ~/serai-paste.out")
        subprocess.run(["ssh", "-O", "exit", "-o", f"ControlPath={ctl}", ALIAS], env=s.env, capture_output=True)
        shutil.rmtree(run_dir, ignore_errors=True)

    assert m["tcp_conns"] == 1 and m["tcp_conns_by_master"] == 1 and m["clients"] >= 2, m
    assert m["reattach_s"] < 5, m
    assert m["history_copies"] == [1, 1], m
    assert m["paste_size"] == "102400" and m["paste_sha_ok"] and m["paste_s"] < 5, m
    assert not m["js_errors"], m
