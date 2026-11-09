// Touch scrolling. In the normal buffer xterm's viewport is an ordinary scroll
// container, so a drag scrolls scrollback natively -- nothing to do. A
// full-screen app (vim, less, an agent TUI) runs on the alternate screen, which
// has no scrollback: there a vertical drag becomes wheel events, and xterm does
// what a wheel does for that app -- a mouse report when the app tracks the
// mouse, arrow keys when it doesn't. One rule, both cases; v2 needed three.

export function installTouchScroll(term) {
  const el = term.element;
  let lastY = null;
  let acc = 0;

  const lineHeight = () => {
    const row = el.querySelector(".xterm-rows > div");
    return (row && row.getBoundingClientRect().height) || 16;
  };

  el.addEventListener("touchstart", (e) => {
    lastY = e.touches.length === 1 ? e.touches[0].clientY : null;
    acc = 0;
  }, { passive: true });

  el.addEventListener("touchmove", (e) => {
    if (lastY === null || e.touches.length !== 1) return;
    if (term.buffer.active.type !== "alternate") return;      // native scroll
    const t = e.touches[0];
    acc += lastY - t.clientY;
    lastY = t.clientY;
    const lh = lineHeight();
    const target = el.querySelector(".xterm-viewport") || el;
    while (Math.abs(acc) >= lh) {
      const dir = Math.sign(acc);
      target.dispatchEvent(new WheelEvent("wheel", {
        deltaY: dir * lh, deltaMode: 0, clientX: t.clientX, clientY: t.clientY,
        bubbles: true, cancelable: true,
      }));
      acc -= dir * lh;
    }
    e.preventDefault();                                       // don't scroll the page
  }, { passive: false });

  el.addEventListener("touchend", () => { lastY = null; }, { passive: true });
}
