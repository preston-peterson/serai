"""Minimal Chrome DevTools Protocol driver (websockets only; no Playwright) for
the v3 browser smoke tests. Just enough to evaluate JS, send input, read the
clipboard and take a screenshot."""
import asyncio, json, itertools, urllib.request

class CDP:
    def __init__(self, ws):
        self.ws, self.ids, self.pending, self.events = ws, itertools.count(1), {}, []
    @classmethod
    async def connect(cls, port):
        import websockets
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                await asyncio.sleep(0.25)
        ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000)
        c = cls(ws); c.reader = asyncio.create_task(c._read()); return c
    async def _read(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            else:
                self.events.append(m)
    async def send(self, method, **params):
        i = next(self.ids); fut = asyncio.get_running_loop().create_future(); self.pending[i] = fut
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params}))
        m = await asyncio.wait_for(fut, 20)
        if "error" in m: raise RuntimeError(f"{method}: {m['error']}")
        return m.get("result", {})
    async def eval(self, expr, await_promise=True):
        r = await self.send("Runtime.evaluate", expression=expr, awaitPromise=await_promise, returnByValue=True, userGesture=True)
        if r.get("exceptionDetails"): raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description") or r["exceptionDetails"])
        return r["result"].get("value")
    async def wait(self, expr, timeout=10.0):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            try:
                v = await self.eval(expr)
                if v: return v
            except RuntimeError:
                pass
            await asyncio.sleep(0.15)
        raise TimeoutError(expr)
    async def key(self, code, key, mods=0, text=None, vk=0):
        base = dict(code=code, key=key, modifiers=mods, windowsVirtualKeyCode=vk)
        await self.send("Input.dispatchKeyEvent", type="rawKeyDown" if text is None else "keyDown", **base, **({"text": text} if text else {}))
        await self.send("Input.dispatchKeyEvent", type="keyUp", **base)
    async def drag(self, x0, y0, x1, y1, steps=8):
        await self.send("Input.dispatchMouseEvent", type="mousePressed", x=x0, y=y0, button="left", clickCount=1)
        for i in range(1, steps + 1):
            await self.send("Input.dispatchMouseEvent", type="mouseMoved", x=x0 + (x1 - x0) * i / steps, y=y0 + (y1 - y0) * i / steps, button="left")
        await self.send("Input.dispatchMouseEvent", type="mouseReleased", x=x1, y=y1, button="left", clickCount=1)
    async def shot(self, path):
        import base64
        r = await self.send("Page.captureScreenshot", format="png")
        open(path, "wb").write(base64.b64decode(r["data"]))
    async def close(self):
        self.reader.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass
