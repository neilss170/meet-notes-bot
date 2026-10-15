'use strict';

/*
 * A scripted run of the real app, for checking a build without a person at
 * the mouse. Only loaded when SCRIBE_SMOKE names a folder to report into, so
 * an ordinary launch never touches it.
 *
 *   SCRIBE_SMOKE=<dir>          screenshots and report.json go here
 *   SCRIBE_SMOKE_RECORD=<secs>  also start a recording from the tray, let it
 *                               run this long, and quit *while it is running*
 *                               - the case where the notes must still finish
 *
 * It checks the things a person would: the page loads signed in, the window
 * refuses to navigate away from the server, the tray's view of the world,
 * and that quitting mid-recording leaves no server behind.
 */

const fs = require('fs');
const path = require('path');

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function attach({ mainWindow, tray, api, log, quit, bubble, flow, morph }) {
  const out = process.env.SCRIBE_SMOKE;
  // SCRIBE_SMOKE_THEME=light|dark: photograph a theme other than the system's.
  if (process.env.SCRIBE_SMOKE_THEME) {
    require('electron').nativeTheme.themeSource = process.env.SCRIBE_SMOKE_THEME;
  }
  fs.mkdirSync(out, { recursive: true });
  const report = { started: new Date().toISOString(), steps: [] };
  const step = (name, value) => {
    report.steps.push({ name, value });
    log(`smoke: ${name}: ${JSON.stringify(value)}`);
    fs.writeFileSync(path.join(out, 'report.json'), JSON.stringify(report, null, 2));
  };
  // capturePage can wait forever on a window Windows considers occluded, so
  // a screenshot gets five seconds and is skipped rather than hanging the run.
  const shoot = async (name) => {
    const image = await Promise.race([
      mainWindow.webContents.capturePage(),
      wait(5000).then(() => null),
    ]);
    if (image) fs.writeFileSync(path.join(out, `${name}.png`), image.toPNG());
    else log(`smoke: screenshot ${name} timed out`);
  };

  /*
   * SCRIBE_SMOKE_BUBBLE=1: the floating bubble. Each state is photographed
   * from the bubble's own window and the animation from the overlay's - never
   * the desktop, so nothing else on the screen ends up in a report.
   */
  const capture = async (win, name) => {
    const image = await Promise.race([win.webContents.capturePage(), wait(5000).then(() => null)]);
    if (image) fs.writeFileSync(path.join(out, `${name}.png`), image.toPNG());
  };
  const inBubble = (code) => bubble.win.webContents.executeJavaScript(code);
  const visible = () => ({
    window: mainWindow.isVisible() && !mainWindow.isMinimized(),
    bubble: bubble.visible,
    overlay: Boolean(morph.win && morph.win.isVisible()),
  });

  async function bubbleSteps() {
    // A real minimise, the way a click makes one: the press (mouse down on
    // the button), a human beat, then the window's own minimise.
    await flow.pressed();
    await wait(120);
    mainWindow.minimize();
    await wait(1500);
    step('after minimise', visible());
    step('bubble at', bubble.circleRect());

    const base = { recording: false, micMuted: false, micAvailable: false, writing: false, elapsed: 0 };
    const states = {
      idle: base,
      recording: { ...base, recording: true, micAvailable: true, elapsed: 252 },
      muted: { ...base, recording: true, micAvailable: true, micMuted: true, elapsed: 252 },
      writing: { ...base, writing: true },
      paused: { ...base, recording: true, micAvailable: true, paused: true, elapsed: 252 },
    };
    for (const [name, state] of Object.entries(states)) {
      bubble.update(state);
      await wait(400);
      await capture(bubble.win, `bubble-${name}`);
    }
    for (const name of ['idle', 'recording']) {
      bubble.update(states[name]);
      await inBubble("open('actions')");
      await wait(700);
      await capture(bubble.win, `bubble-pill-${name}`);
      await inBubble('close()');
      await wait(500);
    }
    // About to stop by itself: held grey, with the way out on show.
    bubble.update({ ...states.paused, autoStop: { reason: 'call_ended', seconds_left: 8 } });
    await wait(900);
    await capture(bubble.win, 'bubble-stopping');
    bubble.update(states.idle);
    await wait(600);
    bubble.offerCall({ id: 'smoke', app: 'Zoom', service: '', title: 'Zoom call' });
    await wait(900);
    await capture(bubble.win, 'bubble-call');
    bubble.clearCall();
    await wait(600);
    bubble.hint('Scribe is still running. Right-click to quit.');
    await wait(700);
    await capture(bubble.win, 'bubble-hint');
    await inBubble("mode = 'hint'; call = null; close()");
    await wait(600);

    // The animation, held at moments along it, for a GIF.
    const frames = path.join(out, 'frames');
    fs.mkdirSync(frames, { recursive: true });
    // Denser early and late, where the ease is slowest.
    const times = [0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330, 360, 390, 420];
    const film = (prefix, reverse) => morph.film(
      flow.windowRect, bubble.circleRect(), reverse, times,
      (i) => capture(morph.win, path.join('frames', `${prefix}-${String(i).padStart(2, '0')}`)),
    );
    fs.writeFileSync(path.join(frames, 'geometry.json'), JSON.stringify({
      window: flow.windowRect, bubble: bubble.circleRect(),
    }));
    await film('min', false);
    await film('restore', true);

    // And back, for real.
    await flow.toWindow();
    await wait(800);
    step('after restore', visible());

    // Closing goes to the bubble too, with the one-time hint.
    await flow.pressed();
    await wait(120);
    mainWindow.close();
    await wait(1500);
    step('after close', visible());
    await flow.toWindow();
    await wait(800);
    step('after second restore', visible());

    // Minimised with no press to prepare it - Win+Down, the taskbar.
    mainWindow.minimize();
    await wait(1500);
    step('after unprepared minimise', visible());
    await flow.toWindow();
    await wait(800);
    step('after third restore', visible());
  }

  mainWindow.webContents.once('did-finish-load', async () => {
    try {
      await wait(3000);
      const wc = mainWindow.webContents;
      step('url', wc.getURL());
      step('signed in', await wc.executeJavaScript('!!document.getElementById("record")'));
      step('node in page', await wc.executeJavaScript('typeof require !== "undefined" || typeof process !== "undefined"'));
      await shoot('window');

      // Navigation away must be refused; the page stays where it is.
      const before = wc.getURL();
      await wc.executeJavaScript('location.href = "https://example.com/"; true');
      await wait(1500);
      step('navigation away refused', wc.getURL() === before);
      step('external fetch blocked', await wc.executeJavaScript(
        'fetch("https://example.com/").then(() => false, () => true)',
      ));

      step('tray status', tray.status);
      const seconds = Number(process.env.SCRIBE_SMOKE_RECORD || 0);
      if (seconds > 0) {
        await tray.toggleRecording();
        await wait(2500);
        step('recording started', tray.status);
        await wait(seconds * 1000);
        const meetings = await api.get('/api/meetings');
        const live = (meetings.meetings || []).find((m) => !['finished', 'failed'].includes(m.status));
        step('live recording', live ? { id: live.id, mic_available: live.mic_available } : null);
        if (live && live.mic_available) {
          await tray.toggleMic();
          await wait(2500);
          step('muted from tray', tray.status.micMuted);
          await shoot('muted');
          await tray.toggleMic();
          await wait(1500);
        }
        await shoot('recording');
      }
      if (process.env.SCRIBE_SMOKE_BUBBLE === '1') await bubbleSteps();
      step('quitting', { recording: tray.status.recording });
      quit();
    } catch (error) {
      step('error', String(error && error.stack || error));
      quit();
    }
  });
}

module.exports = { attach };
