'use strict';

/*
 * The bubble's page. It draws what main.js tells it - the state, which side
 * of the screen it is on, a call to ask about - and says what the pointer and
 * the keyboard did. It never talks to the server itself.
 *
 * Nothing here runs on a timer while idle. The bars move only while
 * something is being recorded - two seconds of the room's loudness arrive
 * with each status and are played back across the next two - and the clock
 * ticks only while the pill is open.
 */

const $ = (id) => document.getElementById(id);
const root = $('root');
const pill = $('pill');

/** The handle, the gap beside it, and the pill's padding - the pill is
    exactly as wide as what it holds. */
const PILL_FRAME = 36 + 6 + 8 + 2;
const LEVEL_MS = 100;
const BAR_SHAPE = [0.55, 0.85, 1, 0.8, 0.5];
const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)');
const HOVER_OPEN_MS = 250;
const LEAVE_CLOSE_MS = 400;
const DRAG_THRESHOLD = 4;

const MIC = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';
const MIC_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 9.5V6a3 3 0 0 0-5.7-1.3M9 9v2a3 3 0 0 0 5.1 2.1"/><path d="M19 11a7 7 0 0 1-1.1 3.8M5 11a7 7 0 0 0 11.2 5.6M12 18v3M3 3l18 18"/></svg>';
const DOT = '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="6"/></svg>';
const SQUARE = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>';

let status = {
  recording: false, micMuted: false, micAvailable: false, writing: false, elapsed: 0,
  levels: [], paused: false, autoStop: null,
};
let statusAt = Date.now();
let mode = '';            // '' collapsed, or 'actions' | 'call' | 'hint'
let call = null;          // the prompt being asked about
let keyboard = false;     // opened from the shortcut, so focus is ours
let openTimer = null;
let closeTimer = null;
let clockTimer = null;
let hintTimer = null;
let levelTimer = null;

// -- drawing ------------------------------------------------------------------

function paintState() {
  root.classList.toggle('recording', status.recording);
  root.classList.toggle('idle', !status.recording);
  root.classList.toggle('paused', status.recording && Boolean(status.paused));
  root.classList.toggle('muted', status.recording && status.micMuted);
  root.classList.toggle('writing', Boolean(status.writing));
  playLevels();
  if (status.autoStop) {
    const left = Math.max(0, Math.ceil(status.autoStop.seconds_left || 0));
    $('stop-say').textContent = `Stopping… ${left}`;
    if (mode !== 'stopping') open('stopping');
  } else if (mode === 'stopping') {
    mode = 'actions';
    close();
  }

  const record = $('act-record');
  record.classList.toggle('primary', !status.recording);
  record.classList.toggle('stop', status.recording);
  record.innerHTML = status.recording ? `${SQUARE}<span>Stop</span>` : `${DOT}<span>Record</span>`;
  const recordTip = status.recording ? 'Stop recording (R)' : 'Start recording (R)';
  record.dataset.tip = recordTip;
  record.setAttribute('aria-label', recordTip.replace(/ \(.\)$/, ''));

  const mute = $('act-mute');
  mute.hidden = !(status.recording && status.micAvailable);
  mute.innerHTML = status.micMuted ? MIC_OFF : MIC;
  mute.classList.toggle('on', Boolean(status.micMuted));
  mute.dataset.tip = status.micMuted ? 'Unmute my mic (M)' : 'Mute my mic (M)';
  $('act-mark').hidden = !status.recording;
  // When recording, Stop is the action; nothing else wears the accent.
  $('clock').hidden = !status.recording;
  tick();
  refit();
}

/** The bars: the room's loudness, a tenth of a second at a time. */
function playLevels() {
  clearInterval(levelTimer);
  levelTimer = null;
  const levels = status.recording && !status.paused ? (status.levels || []).slice() : [];
  const bars = [...document.querySelectorAll('#bars i')];
  const set = (level) => bars.forEach((bar, i) => {
    const wobble = REDUCED.matches ? 1 : 0.75 + Math.random() * 0.5;
    bar.style.setProperty('--b', String(Math.min(1, level * BAR_SHAPE[i] * wobble)));
  });
  if (!levels.length) {
    bars.forEach((bar) => bar.style.removeProperty('--b'));
    return;
  }
  if (REDUCED.matches) {
    set(Math.max(0.25, levels.reduce((a, b) => a + b, 0) / levels.length));
    return;
  }
  let index = 0;
  levelTimer = setInterval(() => {
    if (index >= levels.length) {
      clearInterval(levelTimer);
      levelTimer = null;
      return;
    }
    set(levels[index++]);
  }, LEVEL_MS);
}

function clock(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

function tick() {
  if (!status.recording) return;
  $('clock').textContent = clock(status.elapsed + (Date.now() - statusAt) / 1000);
}

function show(which) {
  for (const id of ['actions', 'call', 'stopping', 'hint']) $(id).hidden = id !== which;
}

/** How wide the pill must be to hold what it is showing now. */
function widthFor(which) {
  return Math.ceil(PILL_FRAME + $(which).scrollWidth);
}

/** Ask main for the room, then grow into it. */
function open(next) {
  clearTimeout(closeTimer);
  if (mode === next) return;
  mode = next;
  show(next);
  const width = widthFor(next);
  root.style.setProperty('--open-w', `${width}px`);
  window.bubble.send('expand', { width });
}

/** Something changed while open - recording started, say - so re-measure. */
function refit() {
  if (!mode) return;
  const width = widthFor(mode);
  root.style.setProperty('--open-w', `${width}px`);
  window.bubble.send('expand', { width });
}

function close() {
  clearTimeout(openTimer);
  if (!mode || call || status.autoStop) return;
  mode = '';
  keyboard = false;
  root.classList.remove('open');
  clearInterval(clockTimer);
  clockTimer = null;
  // Shrink first, give the window back once the pill has finished.
  setTimeout(() => {
    if (!mode) window.bubble.send('collapse');
  }, 210);
}

// -- what main.js says --------------------------------------------------------

window.bubble.on('state', (next) => {
  status = next;
  statusAt = Date.now();
  paintState();
});

window.bubble.on('layout', ({ side, expanded }) => {
  root.classList.toggle('side-left', side === 'left');
  root.classList.toggle('side-right', side !== 'left');
  if (expanded && mode) {
    // The window is wide enough now; animate into it.
    requestAnimationFrame(() => root.classList.add('open'));
    if (mode === 'actions' && status.recording && !clockTimer) {
      tick();
      clockTimer = setInterval(tick, 1000);
    }
    if (keyboard) {
      const first = mode === 'call' ? $('call-yes') : mode === 'stopping' ? $('stop-keep') : $('act-record');
      setTimeout(() => first.focus(), 220);
    }
  }
});

window.bubble.on('call', (prompt) => {
  call = prompt;
  if (!prompt) {
    close();
    return;
  }
  $('call-say').textContent = `${prompt.app} call — record?`;
  root.classList.remove('bounce');
  void root.offsetWidth; // restart the bounce
  root.classList.add('bounce');
  open('call');
});

window.bubble.on('hint', (text) => {
  $('hint-text').textContent = text;
  open('hint');
  clearTimeout(hintTimer);
  hintTimer = setTimeout(() => {
    if (mode === 'hint') close();
  }, 4500);
});

window.bubble.on('keyboard', () => {
  keyboard = true;
  open(call ? 'call' : 'actions');
});

window.bubble.on('pop', () => {
  root.classList.remove('pop');
  void root.offsetWidth;
  root.classList.add('pop');
});

// -- the pointer --------------------------------------------------------------

pill.addEventListener('mouseenter', () => {
  clearTimeout(closeTimer);
  if (mode) return;
  openTimer = setTimeout(() => open('actions'), HOVER_OPEN_MS);
});
pill.addEventListener('mouseleave', () => {
  clearTimeout(openTimer);
  if (keyboard || call) return;
  closeTimer = setTimeout(close, LEAVE_CLOSE_MS);
});

/* Dragging is done here rather than with an OS drag region, so a press that
   does not move is still a click, and the window is moved by main.js from the
   real cursor position - which stays right across monitors of different
   scaling, where page coordinates would not. */
const orb = $('orb');
let press = null;
orb.addEventListener('pointerdown', (event) => {
  if (event.button !== 0) return;
  orb.setPointerCapture(event.pointerId);
  press = { x: event.screenX, y: event.screenY, dragging: false };
});
orb.addEventListener('pointermove', (event) => {
  if (!press) return;
  if (!press.dragging && Math.hypot(event.screenX - press.x, event.screenY - press.y) > DRAG_THRESHOLD) {
    press.dragging = true;
    clearTimeout(openTimer);
    if (mode && !call) {
      mode = '';
      root.classList.remove('open');
    }
    window.bubble.send('drag-start');
  }
  if (press.dragging) window.bubble.send('drag-move');
});
orb.addEventListener('pointerup', () => {
  if (!press) return;
  const { dragging } = press;
  press = null;
  if (dragging) window.bubble.send('drag-end');
  else window.bubble.send('act', 'open');
});
orb.addEventListener('lostpointercapture', () => {
  if (press && press.dragging) window.bubble.send('drag-end');
  press = null;
});

document.addEventListener('contextmenu', (event) => {
  event.preventDefault();
  window.bubble.send('menu');
});

// -- the actions --------------------------------------------------------------

const ACTIONS = {
  'act-record': 'record',
  'act-mute': 'mute',
  'act-mark': 'mark',
  'act-open': 'open',
  'call-yes': 'call-yes',
  'call-no': 'call-no',
  'stop-keep': 'keep',
};
for (const [id, action] of Object.entries(ACTIONS)) {
  $(id).addEventListener('click', () => {
    if (action.startsWith('call-') || action === 'keep') {
      call = null;
      mode = '';
      root.classList.remove('open');
      window.bubble.send('collapse');
    }
    window.bubble.send('act', action);
  });
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    keyboard = false;
    close();
    window.bubble.send('blur');
    return;
  }
  const key = event.key.toLowerCase();
  const target = [...document.querySelectorAll('[data-key]')].find(
    (el) => el.dataset.key === key && !el.closest('[hidden]') && !el.hidden,
  );
  if (target) {
    event.preventDefault();
    target.click();
  }
});
window.addEventListener('blur', () => {
  if (keyboard) {
    keyboard = false;
    close();
  }
});

paintState();
