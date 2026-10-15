'use strict';

/*
 * The minimise animation, drawn on a transparent window that covers the path
 * between the app window and the bubble. Windows has no genie effect to
 * borrow, so this is the Mac's, done by hand: a picture of the window shrinks
 * along a curve into the bubble, its corners rounding into a circle, fading
 * as it lands. Restoring plays the same flight backwards.
 *
 * Smoothness comes from three things, each one learned by measuring:
 *
 * Staged before it is needed. The picture is decoded and on screen - exactly
 *   over the real window, so nobody can tell - before the window goes. The
 *   first version only started loading it once the window had vanished, which
 *   left 140 ms of nothing, and its first frame stalled 70 ms uploading the
 *   image. Staged, the flight starts on the next frame with nothing to load.
 *
 * One continuous path. Position follows a quadratic curve, size shrinks
 *   geometrically (equal ratios per frame, which reads as steady), and a
 *   single symmetric ease runs across the whole thing. Keyframes stitched
 *   together had corners in the motion, and a front-loaded ease that covered
 *   most of the distance in the first two frames looked like a jump.
 *
 * Cheap per frame. Position, size and fade are transform and opacity, which
 *   the compositor applies without touching the page. The rounding corners
 *   do repaint the picture's layer each frame; measured, that costs nothing
 *   visible, and it is what turns a rectangle into a circle.
 */

const shot = document.getElementById('shot');
const DURATION = 420;
const REDUCED_DURATION = 120;
const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

let job = null;       // { from, to, reverse, running }
let lastSeen = -1;    // the corner radius last painted, in screen pixels
let loaded = '';      // the src currently decoded

const frame = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));

/** Ease-in-out with a gentle start and a long, soft landing. */
function ease(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
}

/**
 * The picture's state at progress p (0 = the window, 1 = the bubble).
 * Returns the transform relative to the window's box, the opacity, and the
 * corner radius in the picture's own (unscaled) pixels.
 */
function at(p, from, to) {
  const lift = Math.min(120, Math.hypot(to.x - from.x, to.y - from.y) * 0.12);
  // Centres along a quadratic curve, bowed upward by `lift`.
  const c0 = { x: from.x + from.width / 2, y: from.y + from.height / 2 };
  const c2 = { x: to.x + to.width / 2, y: to.y + to.height / 2 };
  const c1 = { x: (c0.x + c2.x) / 2, y: (c0.y + c2.y) / 2 - lift * 2 };
  const q = 1 - p;
  const cx = q * q * c0.x + 2 * q * p * c1.x + p * p * c2.x;
  const cy = q * q * c0.y + 2 * q * p * c1.y + p * p * c2.y;
  // Geometric interpolation of size: steady-looking shrinking.
  const w = from.width * (to.width / from.width) ** p;
  const h = from.height * (to.height / from.height) ** p;
  const sx = w / from.width;
  const sy = h / from.height;
  const tx = cx - w / 2 - from.x;
  const ty = cy - h / 2 - from.y;
  // The corners round from a window's 10 px to a full circle, as seen on
  // screen. Stated per axis in the picture's own pixels, so the non-uniform
  // scale cannot turn them into ellipses: at the end they are exactly half
  // the picture's size each way - a circle once it is scaled square.
  const seen = 10 + (to.width / 2 - 10) * p ** 1.5;
  const rx = Math.min(from.width / 2, seen / sx);
  const ry = Math.min(from.height / 2, seen / sy);
  return {
    transform: `translate(${tx}px, ${ty}px) scale(${sx}, ${sy})`,
    radius: `${rx}px / ${ry}px`,
    seen,
  };
}

function place(p, opacity) {
  const state = at(p, job.from, job.to);
  shot.style.transform = state.transform;
  // Rounding the corners repaints the picture; moving and fading it does
  // not. While the picture is still big the radius barely changes, and
  // repainting a window-sized layer for a fraction of a pixel was where the
  // remaining dropped frames came from - so only repaint for a visible step.
  // The screen-space radius is checked because the scale keeps changing
  // under it: the same number of pixels on the layer looks different each
  // frame.
  if (Math.abs(state.seen - lastSeen) >= 0.75 || p === 0 || p === 1) {
    shot.style.borderRadius = state.radius;
    lastSeen = state.seen;
  }
  shot.style.opacity = String(opacity);
}

/** Decode a picture ahead of time, so staging it later costs nothing. */
window.overlay.on('preload', async ({ src }) => {
  if (!src || src === loaded) return;
  shot.src = src;
  try {
    await shot.decode();
    loaded = src;
  } catch (_) {
    loaded = '';
  }
});

/** Put the picture at the start of the flight and say when it is painted. */
window.overlay.on('stage', async ({ src, from, to, reverse }) => {
  job = { from, to, reverse, running: false };
  lastSeen = -1;
  if (src && src !== loaded) {
    shot.src = src;
    try {
      await shot.decode();
      loaded = src;
    } catch (_) {
      window.overlay.send('failed');
      return;
    }
  }
  shot.style.left = `${from.x}px`;
  shot.style.top = `${from.y}px`;
  shot.style.width = `${from.width}px`;
  shot.style.height = `${from.height}px`;
  shot.style.display = 'block';
  // Minimising starts as the window itself; restoring starts invisible, in
  // the bubble.
  if (reverse) place(1, 0);
  else place(0, 1);
  window.overlay.send('staged');
});

/** Shown by main.js; once this window has really painted, say so. */
window.overlay.on('shown', async () => {
  await frame();
  window.overlay.send('ready');
});

window.overlay.on('go', () => {
  if (!job) return;
  const { reverse } = job;
  const duration = reduced() ? REDUCED_DURATION : DURATION;
  const started = performance.now();
  const stamps = [];
  let arrived = false;
  job.running = true;

  const step = (now) => {
    if (!job || !job.running) return;
    stamps.push(now);
    const t = Math.min(1, (now - started) / duration);
    const e = ease(t);
    if (reduced()) {
      // A crossfade in place, for anyone who asked for less motion.
      place(reverse ? 0 : 0, reverse ? e : 1 - e);
    } else {
      const p = reverse ? 1 - e : e;
      // Fade only at the bubble end: out as it lands, in as it leaves.
      const opacity = p > 0.72 ? Math.max(0, 1 - (p - 0.72) / 0.28) : 1;
      place(p, opacity);
    }
    if (!reverse && !arrived && t >= 0.7) {
      arrived = true;
      window.overlay.send('arrive');
    }
    if (t < 1) {
      requestAnimationFrame(step);
      return;
    }
    job.running = false;
    const gaps = stamps.slice(1).map((s, i) => s - stamps[i]);
    const worst = Math.max(0, ...gaps);
    const where = gaps.indexOf(worst);
    console.log(`overlay-stats ${JSON.stringify({
      frames: stamps.length,
      worstGapMs: Math.round(worst),
      worstAt: where < 0 ? null : Math.round(((stamps[where] - started) / duration) * 100) / 100,
      over16ms: gaps.filter((g) => g > 17).length,
    })}`);
    window.overlay.send('done');
  };
  requestAnimationFrame(step);
});

/* For screenshots of the animation: show one moment of it. */
window.overlay.on('scrub', async ({ t }) => {
  if (!job) return;
  job.running = false;
  const e = ease(Math.min(1, t / DURATION));
  const p = job.reverse ? 1 - e : e;
  place(p, p > 0.72 ? Math.max(0, 1 - (p - 0.72) / 0.28) : 1);
  await frame();
  window.overlay.send('scrubbed');
});

/** Restoring: the real window is painted underneath; fade the picture off it. */
window.overlay.on('release', async () => {
  shot.style.transition = 'opacity 90ms linear';
  shot.style.opacity = '0';
  await new Promise((r) => setTimeout(r, 100));
  shot.style.transition = '';
  window.overlay.send('released');
});

window.overlay.on('clear', () => {
  if (job) job.running = false;
  job = null;
  shot.style.display = 'none';
  shot.style.opacity = '1';
});
