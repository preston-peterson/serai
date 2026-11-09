// Touch scrolling, one way everywhere, with momentum.
//
// Left alone, a drag on a terminal behaved three ways (the owner: "sometimes
// the scroll sticks and sometimes it's smooth and fast"): where the finger
// landed on xterm's scroll layer the browser scrolled natively, with
// momentum; on the text, xterm's own touch handler moved the view exactly as
// far as the finger and stopped dead; and in an app with mouse tracking on it
// did nothing at all. So serai takes every single-finger vertical drag:
//
//   - the normal buffer (shell scrollback): the viewport scrolls pixel by
//     pixel and keeps gliding after the finger lifts, slowing with friction;
//   - the alternate screen (vim, less, an agent TUI) or any app tracking the
//     mouse: the same drag and glide, delivered as wheel steps -- xterm turns
//     those into mouse reports or arrow keys for the app, as a wheel would.
//
// Taps are untouched: a gesture only becomes a scroll once it has moved
// SLOP px, mostly vertically, so a tap still focuses the terminal.

const SLOP = 8;              // px before a touch counts as a scroll
const FRICTION = 0.95;       // velocity kept per 16 ms frame while gliding
const MIN_FLING = 0.08;      // px/ms: slower than this on release = no glide
const STOP = 0.02;           // px/ms: the glide ends below this
const SAMPLE_MS = 100;       // velocity is measured over the last 100 ms
const MAX_STEPS = 3;         // wheel steps per frame at most (app redraws)

export function installTouchScroll(term) {
  const el = term.element;
  let startX = 0, startY = 0, lastY = 0;
  let owning = false, tracking = false;
  let samples = [];
  let acc = 0;               // wheel mode: pixels not yet sent as a step
  let raf = 0;

  const viewport = () => el.querySelector(".xterm-viewport");
  const lineHeight = () => {
    const row = el.querySelector(".xterm-rows > div");
    return (row && row.getBoundingClientRect().height) || 16;
  };
  const wheelMode = () => term.buffer.active.type === "alternate" || term.modes.mouseTrackingMode !== "none";

  // dy > 0: the finger moved up -- show later lines (natural scrolling).
  function scrollBy(dy, x, y) {
    if (!wheelMode()) {
      const v = viewport();
      if (v) v.scrollTop += dy;
      return;
    }
    acc += dy;
    const lh = lineHeight();
    const target = viewport() || el;
    let steps = 0;
    while (Math.abs(acc) >= lh && steps < MAX_STEPS) {
      const dir = Math.sign(acc);
      target.dispatchEvent(new WheelEvent("wheel", {
        deltaY: dir * lh, deltaMode: 0, clientX: x, clientY: y, bubbles: true, cancelable: true,
      }));
      acc -= dir * lh;
      steps++;
    }
    if (steps === MAX_STEPS) acc = Math.sign(acc) * Math.min(Math.abs(acc), lh);
  }

  function stopGlide() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
  }

  function glide(v, x, y) {
    let last = performance.now();
    const step = (now) => {
      const dt = Math.min(now - last, 50);
      last = now;
      scrollBy(v * dt, x, y);
      v *= Math.pow(FRICTION, dt / 16);
      raf = Math.abs(v) > STOP ? requestAnimationFrame(step) : 0;
    };
    raf = requestAnimationFrame(step);
  }

  // Capture phase on the terminal element: this runs before xterm's own touch
  // handlers on the screen, so once a drag is ours xterm never sees its moves.
  el.addEventListener("touchstart", (e) => {
    stopGlide();                                   // a touch catches a glide
    tracking = e.touches.length === 1;
    owning = false;
    if (!tracking) return;
    const t = e.touches[0];
    startX = t.clientX;
    startY = lastY = t.clientY;
    samples = [{ t: e.timeStamp, y: t.clientY }];
    acc = 0;
  }, { capture: true, passive: true });

  el.addEventListener("touchmove", (e) => {
    if (!tracking || e.touches.length !== 1) return;
    const t = e.touches[0];
    if (!owning) {
      const dy = Math.abs(t.clientY - startY), dx = Math.abs(t.clientX - startX);
      if (dy < SLOP || dy < dx) return;            // not (yet) a vertical scroll
      owning = true;
      lastY = t.clientY;
    }
    e.preventDefault();                            // not the page, not xterm
    e.stopPropagation();
    scrollBy(lastY - t.clientY, t.clientX, t.clientY);
    lastY = t.clientY;
    samples.push({ t: e.timeStamp, y: t.clientY });
    while (samples.length > 2 && e.timeStamp - samples[0].t > SAMPLE_MS) samples.shift();
  }, { capture: true, passive: false });

  el.addEventListener("touchend", (e) => {
    if (!tracking) return;
    tracking = false;
    if (!owning) return;                           // a tap: xterm handles it
    e.stopPropagation();
    const first = samples[0], last = samples[samples.length - 1];
    const dt = last.t - first.t;
    const v = dt > 0 ? (first.y - last.y) / dt : 0;  // px/ms, + = scroll down
    if (Math.abs(v) >= MIN_FLING) {
      const t = e.changedTouches[0];
      glide(v, t ? t.clientX : 0, t ? t.clientY : 0);
    }
  }, { capture: true });

  el.addEventListener("touchcancel", () => { tracking = false; owning = false; }, { capture: true });
}
