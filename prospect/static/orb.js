/* Thinking orbs for Bellwether.
 *
 * The orb engine is thinking-orbs by Jakub Antalik (MIT, see
 * vendor/thinking-orbs/LICENSE.txt). The library ships a React component; this
 * file is the same render loop without React, so the app needs no build step:
 * any <canvas data-orb="breathing"> on a page becomes an orb.
 *
 *   data-orb    working | searching | solving | listening | connecting |
 *               weaving | composing | breathing | shaping
 *   data-size   the engine preset to use: 64, 32 or 20 (default 64)
 *   data-px     the size drawn on screen, in CSS pixels (default = data-size)
 *   data-tint   optional ink colour, #rrggbb
 *
 * window.BWOrb.set(canvas, state) changes an orb's state in place, which is
 * how the Bellwether AI panel shows searching, then composing, then rest.
 */
import { r as resolvePreset, M as MODE_FRAMES, p as paintFrame } from "./vendor/thinking-orbs/engine.js";

const reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
const ctrls = new WeakMap();

function tintOf(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
  return m ? { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) } : undefined;
}

function mount(canvas) {
  if (ctrls.has(canvas)) return ctrls.get(canvas);
  const size = [64, 32, 20].includes(+canvas.dataset.size) ? +canvas.dataset.size : 64;
  const px = +canvas.dataset.px || size;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(px * dpr);
  canvas.height = Math.round(px * dpr);
  canvas.style.width = px + "px";
  canvas.style.height = px + "px";
  canvas.setAttribute("role", "img");
  const ctx = canvas.getContext("2d");
  const tint = tintOf(canvas.dataset.tint);
  const scale = dpr * (px / size);
  const c = { state: null, frameFn: null, opts: null, speed: 1, raf: 0, running: false, visible: true };

  function draw(t) {
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, size, size);
    paintFrame(ctx, c.frameFn(size, t, c.opts), true, tint);
  }
  function loop() {
    draw(performance.now() / 1000 * c.speed);
    if (c.running) c.raf = requestAnimationFrame(loop);
  }
  c.start = () => {
    if (c.running || reduced || !c.visible || document.hidden) return;
    c.running = true;
    c.raf = requestAnimationFrame(loop);
  };
  c.stop = () => { c.running = false; cancelAnimationFrame(c.raf); };
  c.setState = (state) => {
    if (state === c.state) return;
    const res = resolvePreset(state, size);
    if (!res) return;
    c.state = state;
    c.frameFn = MODE_FRAMES[res.mode];
    c.opts = res.opts;
    c.speed = res.speed;
    draw(reduced ? 0.6 : performance.now() / 1000 * c.speed);
    c.start();
  };
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(([e]) => {
      c.visible = e.isIntersecting;
      if (c.visible) c.start(); else c.stop();
    }).observe(canvas);
  }
  ctrls.set(canvas, c);
  c.setState(canvas.dataset.orb || "breathing");
  return c;
}

document.addEventListener("visibilitychange", () => {
  document.querySelectorAll("canvas[data-orb]").forEach((cv) => {
    const c = ctrls.get(cv);
    if (!c) return;
    if (document.hidden) c.stop(); else c.start();
  });
});

function mountAll(root) {
  (root || document).querySelectorAll("canvas[data-orb]").forEach(mount);
}

window.BWOrb = {
  mount: mountAll,
  set(canvas, state) {
    if (!canvas) return;
    mount(canvas).setState(state);
    canvas.dataset.orb = state;
  },
};
mountAll();
