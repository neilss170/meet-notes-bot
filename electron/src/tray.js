'use strict';

/*
 * Scribe in the notification area - meetbot/tray.py, rebuilt for the shell.
 *
 * Same behaviour, different plumbing. tray.py lives inside the server process
 * and reads the job list directly; this one is in another process, so it asks
 * the ordinary routes every two seconds, as the machine owner, and does
 * everything through them too. Nothing here decides what a recording is or
 * whether one may start - the server does, and its refusals are shown as
 * they are written.
 *
 * Kept from tray.py, on purpose:
 *  - The menu's first line is the status, not a title.
 *  - The clock is in the tooltip only; the menu is rebuilt only when its
 *    text changes, never every two seconds while somebody may have it open.
 *  - What a click does is decided from the state at the moment of the click,
 *    not from the label that was clicked - a menu can be stale.
 */

const path = require('path');
const { Menu, Notification, Tray, nativeImage, shell } = require('electron');
const platform = require('./platform');

const POLL_MS = 2000;
/** Readiness changes rarely; read it every tenth poll. */
const HEALTH_EVERY = 10;
/** Windows truncates a tooltip at 127 characters. */
const TOOLTIP_LIMIT = 127;
const NAME_LIMIT = 40;

const isLive = (job) => !['finished', 'failed'].includes(job.status);

function clock(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

function clip(text, limit) {
  const flat = String(text || '').split(/\s+/).join(' ').trim();
  return flat.length <= limit ? flat : `${flat.slice(0, limit - 1).trimEnd()}…`;
}

const jobName = (job) => clip(job.title || job.meet_url || 'a meeting', NAME_LIMIT);

/** What the icon has to say, from one poll's worth of answers. */
function statusOf(meetings, health) {
  const jobs = meetings || [];
  const live = jobs.find(isLive) || null;
  const writing = jobs.find((job) => job.enhancing) || null;
  const latest = jobs.find((job) => !isLive(job)) || null;
  // No report yet means the checks have not finished, not that they failed.
  const ready = health && health.can_record !== null && health.can_record !== undefined
    ? Boolean(health.can_record) : true;
  const canStart = health && health.can_record_locally !== null && health.can_record_locally !== undefined
    ? Boolean(health.can_record_locally) : true;
  const blockers = health && Array.isArray(health.checks)
    ? health.checks.filter((c) => c.blocking && !c.ok).map((c) => c.name) : [];

  let label = 'Not recording';
  if (live) label = `Recording · ${jobName(live)}`;
  else if (writing) label = `Writing up · ${jobName(writing)}`;
  else if (!ready) label = blockers.length ? `Setup needed · ${blockers.join(', ')}` : 'Setup needed';

  return {
    label,
    recording: Boolean(live),
    liveId: live ? live.id : '',
    elapsed: live ? live.elapsed_s : 0,
    micAvailable: Boolean(live && live.mic_available),
    micMuted: Boolean(live && live.mic_muted),
    // The bubble's bars: the room's loudness over the last two seconds, and
    // whether recording is held - starting, wrapping up, or about to stop.
    levels: live && Array.isArray(live.levels) ? live.levels : [],
    paused: Boolean(live && (live.status !== 'in_call' || live.auto_stop)),
    autoStop: live && live.auto_stop ? live.auto_stop : null,
    writing: Boolean(writing),
    latestId: latest ? latest.id : '',
    canStart,
  };
}

function tooltipOf(status) {
  let text = `Scribe — ${status.label}`;
  if (status.recording && status.micMuted) {
    text = `Scribe — Recording · your mic is muted · ${clock(status.elapsed)}`;
  } else if (status.recording) {
    text = `${text} · ${clock(status.elapsed)}`;
  }
  return clip(text, TOOLTIP_LIMIT);
}

class ScribeTray {
  /**
   * @param {object} options
   * @param {import('./api').ServerApi} options.api
   * @param {string} options.assets
   * @param {object} options.paths  { recordings, log, settings }
   * @param {() => void} options.showWindow
   * @param {(jobId: string) => void} options.showMeeting
   * @param {() => boolean} options.loginItem   Whether start-at-sign-in is on.
   * @param {(on: boolean) => void} options.setLoginItem
   * @param {() => void} options.quit
   */
  constructor(options) {
    Object.assign(this, options);
    this.status = statusOf([], null);
    this.health = null;
    this.polls = 0;
    this.menuKey = '';
    this.iconKey = '';
    this.timer = null;
    this.tray = null;
  }

  start() {
    this.tray = new Tray(this.image('idle'));
    this.tray.setToolTip('Scribe');
    // A left click opens the window, as it did; the menu is on the right.
    this.tray.on('click', () => this.showWindow());
    this.render(true);
    this.poll();
    this.timer = setInterval(() => this.poll(), POLL_MS);
  }

  stop() {
    clearInterval(this.timer);
    if (this.tray) this.tray.destroy();
    this.tray = null;
  }

  image(state) {
    return nativeImage.createFromPath(path.join(this.assets, platform.trayImage(state)));
  }

  notify(body) {
    if (Notification.isSupported()) new Notification({ title: 'Scribe', body }).show();
  }

  async poll() {
    try {
      if (this.polls++ % HEALTH_EVERY === 0) {
        try {
          this.health = await this.api.get('/api/health');
        } catch (_) {
          /* readiness is a nicety; the list is what matters */
        }
      }
      const data = await this.api.get('/api/meetings');
      this.status = statusOf(data.meetings, this.health);
    } catch (_) {
      // The server going away is handled by whoever started it.
      return;
    }
    this.render(false);
    if (this.onStatus) this.onStatus(this.status);
  }

  /** Bring the tooltip, the icon and - only if its text changed - the menu up to date. */
  render(force) {
    if (!this.tray) return;
    const s = this.status;
    this.tray.setToolTip(tooltipOf(s));

    const iconKey = s.recording ? (s.micMuted ? 'muted' : 'recording') : 'idle';
    if (force || iconKey !== this.iconKey) {
      this.iconKey = iconKey;
      this.tray.setImage(this.image(iconKey));
    }

    const menuKey = JSON.stringify([s.label, s.recording, s.micAvailable, s.micMuted,
      Boolean(s.latestId), s.canStart, this.loginItem(),
      (this.options || []).map((o) => o.get())]);
    if (force || menuKey !== this.menuKey) {
      this.menuKey = menuKey;
      this.tray.setContextMenu(Menu.buildFromTemplate(this.template()));
    }
  }

  template() {
    const s = this.status;
    const act = (fn) => () => fn.call(this).catch((error) => this.notify(error.message));
    const items = [
      { label: s.label, enabled: false },
      { type: 'separator' },
      { label: 'Open Scribe', click: () => this.showWindow() },
    ];
    if (s.recording) {
      items.push({ label: 'Stop recording', click: act(this.toggleRecording) });
      if (s.micAvailable) {
        items.push({
          label: s.micMuted ? 'Unmute my mic' : 'Mute my mic',
          accelerator: platform.micShortcut.accelerator,
          registerAccelerator: false,
          click: act(this.toggleMic),
        });
      }
    } else {
      // Greyed rather than hidden: a missing item reads as a missing feature.
      items.push({ label: 'Start recording', enabled: s.canStart, click: act(this.toggleRecording) });
    }
    items.push({ label: 'Latest notes', enabled: Boolean(s.latestId), click: () => this.showMeeting(s.latestId) });
    items.push(
      { type: 'separator' },
      { label: 'Recordings folder', click: () => shell.openPath(this.paths.recordings) },
      { label: 'Server log', click: () => platform.openText(this.paths.log) },
      { label: 'Settings and API keys', click: () => platform.openText(this.paths.settings) },
      { type: 'separator' },
      // The shell's own settings: the bubble, and starting at sign-in.
      ...(this.options || []).map((option) => ({
        label: option.label,
        type: 'checkbox',
        checked: option.get(),
        click: (item) => {
          option.set(item.checked);
          this.render(true);
        },
      })),
      {
        label: 'Start Scribe when I sign in',
        type: 'checkbox',
        checked: this.loginItem(),
        click: (item) => {
          this.setLoginItem(item.checked);
          this.render(true);
        },
      },
      { type: 'separator' },
      { label: s.recording ? 'Stop recording and quit' : 'Quit Scribe', click: () => this.quit() },
    );
    return items;
  }

  /** Re-read before acting: the menu that was clicked may be minutes old. */
  async fresh() {
    const data = await this.api.get('/api/meetings');
    this.status = statusOf(data.meetings, this.health);
    return this.status;
  }

  async toggleRecording() {
    const s = await this.fresh();
    if (s.liveId) {
      await this.api.post(`/api/meetings/${encodeURIComponent(s.liveId)}/stop`);
    } else {
      // Nobody is at a keyboard when the menu is used, so the start time is
      // the most useful name until it is renamed.
      const now = new Date();
      const title = `Recording ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
      await this.api.post('/api/recordings', { title });
    }
    await this.poll();
  }

  /** The bubble's "Mark this moment". */
  async mark() {
    const s = await this.fresh();
    if (!s.liveId) return false;
    await this.api.post(`/api/meetings/${encodeURIComponent(s.liveId)}/mark`);
    return true;
  }

  /** "Keep recording": cancel a countdown to stopping by itself. */
  async keepRecording() {
    const s = await this.fresh();
    if (!s.liveId) return false;
    await this.api.post(`/api/meetings/${encodeURIComponent(s.liveId)}/auto-stop/cancel`);
    await this.poll();
    return true;
  }

  /** The tray item and the shortcut both land here. Returns false when idle. */
  async toggleMic() {
    const s = await this.fresh();
    if (!s.liveId) return false;
    await this.api.post(`/api/meetings/${encodeURIComponent(s.liveId)}/mic`, { muted: !s.micMuted });
    await this.poll();
    return true;
  }
}

module.exports = { ScribeTray, statusOf, tooltipOf, clock };
