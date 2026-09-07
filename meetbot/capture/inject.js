/*
 * meetbot browser-side audio capture hook.
 *
 * Injected with Playwright's `add_init_script` so it runs BEFORE any Meet
 * application code in every frame. That ordering is the whole trick: we wrap
 * `RTCPeerConnection` before Meet constructs one, so every remote media track
 * Meet negotiates passes through our `track` listener.
 *
 * Captured audio is resampled by the AudioContext to the bridge's sample rate,
 * converted to 16-bit little-endian PCM and pushed over a WebSocket to the
 * local Python bridge. No OS-level virtual audio device is involved.
 *
 * Wire protocol (see capture/bridge.py for the other half):
 *   - Text frames: JSON control messages, {"type": ...}.
 *   - Binary frames: 1 byte unsigned channel id, then interleaved int16 LE
 *     mono PCM samples for that channel.
 *
 * Configuration arrives as `window.__MEETBOT_CONFIG__`, installed by an
 * earlier init script.
 */
(() => {
  "use strict";

  if (window.__MEETBOT_HOOK_INSTALLED__) {
    return;
  }
  window.__MEETBOT_HOOK_INSTALLED__ = true;

  const CONFIG = Object.assign(
    {
      bridgeUrl: "ws://127.0.0.1:8765",
      sampleRate: 16000,
      perParticipant: false,
      // ~100 ms of audio per frame: small enough for live latency, large
      // enough that we are not paying WebSocket framing overhead per 128
      // samples.
      frameSamples: 1600,
      reconnectDelayMs: 1000,
      maxChannels: 32,
    },
    window.__MEETBOT_CONFIG__ || {},
  );

  const MIXED_CHANNEL = 0;

  /** Structured log line, picked up by the Python console forwarder. */
  const log = (level, message, extra) => {
    const payload = Object.assign({ tag: "meetbot", level, message }, extra || {});
    try {
      console.log("[meetbot] " + JSON.stringify(payload));
    } catch (err) {
      console.log("[meetbot] " + level + " " + message);
    }
  };

  /* ------------------------------------------------------------------ *
   * WebSocket transport, with reconnect.
   * ------------------------------------------------------------------ */

  const transport = {
    socket: null,
    connected: false,
    closedByUs: false,
    pending: [],
    // Cap the backlog so a bridge outage cannot grow the tab's heap without
    // bound. Roughly 30 s of audio at 100 ms per frame.
    maxPending: 300,

    connect() {
      if (this.closedByUs) {
        return;
      }
      let socket;
      try {
        socket = new WebSocket(CONFIG.bridgeUrl);
      } catch (err) {
        log("error", "WebSocket construction failed", { error: String(err) });
        setTimeout(() => this.connect(), CONFIG.reconnectDelayMs);
        return;
      }
      socket.binaryType = "arraybuffer";
      this.socket = socket;

      socket.onopen = () => {
        this.connected = true;
        log("info", "bridge connected", { url: CONFIG.bridgeUrl });
        this.send({
          type: "hello",
          sampleRate: CONFIG.sampleRate,
          perParticipant: CONFIG.perParticipant,
        });
        // Re-announce every channel so a reconnected bridge knows the layout.
        capture.announceChannels();
        const backlog = this.pending;
        this.pending = [];
        backlog.forEach((frame) => this.sendBinary(frame));
      };

      socket.onclose = () => {
        this.connected = false;
        if (!this.closedByUs) {
          log("warn", "bridge disconnected, retrying");
          setTimeout(() => this.connect(), CONFIG.reconnectDelayMs);
        }
      };

      socket.onerror = () => {
        // `onclose` always follows, and carries the retry logic; log only.
        log("warn", "bridge socket error");
      };
    },

    send(obj) {
      if (this.connected && this.socket) {
        try {
          this.socket.send(JSON.stringify(obj));
        } catch (err) {
          log("warn", "control send failed", { error: String(err) });
        }
      }
    },

    sendBinary(buffer) {
      if (this.connected && this.socket && this.socket.readyState === 1) {
        try {
          this.socket.send(buffer);
        } catch (err) {
          log("warn", "audio send failed", { error: String(err) });
        }
        return;
      }
      if (this.pending.length >= this.maxPending) {
        this.pending.shift();
      }
      this.pending.push(buffer);
    },

    close() {
      this.closedByUs = true;
      if (this.socket) {
        try {
          this.socket.close();
        } catch (err) {
          /* the socket is already gone; nothing to do */
        }
      }
    },
  };

  /* ------------------------------------------------------------------ *
   * Audio graph.
   * ------------------------------------------------------------------ */

  // The worklet is defined as a string and loaded from a blob URL. Meet's CSP
  // may refuse blob: worklets, so `installSink` falls back to a
  // ScriptProcessorNode - deprecated, but universally available and not
  // subject to a module fetch.
  const WORKLET_SOURCE = `
    class MeetbotTap extends AudioWorkletProcessor {
      constructor(options) {
        super();
        this.channelId = options.processorOptions.channelId;
        this.frameSamples = options.processorOptions.frameSamples;
        this.buffer = new Float32Array(this.frameSamples);
        this.offset = 0;
      }
      process(inputs) {
        const input = inputs[0];
        if (!input || input.length === 0) {
          return true;
        }
        const samples = input[0];
        if (!samples) {
          return true;
        }
        for (let i = 0; i < samples.length; i += 1) {
          this.buffer[this.offset] = samples[i];
          this.offset += 1;
          if (this.offset === this.frameSamples) {
            this.port.postMessage(this.buffer.slice(0));
            this.offset = 0;
          }
        }
        return true;
      }
    }
    registerProcessor('meetbot-tap', MeetbotTap);
  `;

  /** Float32 [-1, 1] samples -> [channel byte][int16 LE payload]. */
  const encodeFrame = (channelId, samples) => {
    const buffer = new ArrayBuffer(1 + samples.length * 2);
    const view = new DataView(buffer);
    view.setUint8(0, channelId);
    for (let i = 0; i < samples.length; i += 1) {
      let sample = samples[i];
      sample = sample > 1 ? 1 : sample < -1 ? -1 : sample;
      view.setInt16(1 + i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
    }
    return buffer;
  };


  const capture = {
    context: null,
    workletReady: null,
    /** channelId -> {label, node, source, stream} */
    channels: new Map(),
    nextChannelId: 1,
    mixNode: null,
    seenTracks: new WeakSet(),

    ensureContext() {
      if (this.context) {
        return this.context;
      }
      const Ctor = window.AudioContext || window.webkitAudioContext;
      this.context = new Ctor({ sampleRate: CONFIG.sampleRate });
      // Meet autoplay policy: resume once the page gets any user gesture, and
      // opportunistically now (Playwright launches with autoplay unblocked).
      const resume = () => {
        if (this.context.state === "suspended") {
          this.context.resume().catch(() => undefined);
        }
      };
      resume();
      document.addEventListener("click", resume, { once: false, passive: true });

      if (!CONFIG.perParticipant) {
        // A single summing node every remote track feeds into.
        this.mixNode = this.context.createGain();
        this.mixNode.gain.value = 1.0;
        this.installSink(MIXED_CHANNEL, this.mixNode);
        this.channels.set(MIXED_CHANNEL, { label: "mixed", node: this.mixNode });
        transport.send({ type: "channel", id: MIXED_CHANNEL, label: "mixed" });
      }
      return this.context;
    },

    loadWorklet() {
      if (this.workletReady) {
        return this.workletReady;
      }
      const ctx = this.context;
      if (!ctx || !ctx.audioWorklet) {
        this.workletReady = Promise.reject(new Error("AudioWorklet unavailable"));
        return this.workletReady;
      }
      let url;
      try {
        url = URL.createObjectURL(
          new Blob([WORKLET_SOURCE], { type: "application/javascript" }),
        );
      } catch (err) {
        this.workletReady = Promise.reject(err);
        return this.workletReady;
      }
      this.workletReady = ctx.audioWorklet
        .addModule(url)
        .finally(() => URL.revokeObjectURL(url));
      return this.workletReady;
    },

    /**
     * Attach a PCM tap to `sourceNode` and stream it as `channelId`.
     *
     * The tap is connected on to a muted gain node and then to the
     * destination: an AudioWorkletNode is only pulled by the rendering thread
     * while it has a path to the destination, and the zero gain keeps the
     * capture silent locally.
     */
    installSink(channelId, sourceNode) {
      const ctx = this.context;
      const mute = ctx.createGain();
      mute.gain.value = 0;
      mute.connect(ctx.destination);

      this.loadWorklet().then(
        () => {
          const node = new AudioWorkletNode(ctx, "meetbot-tap", {
            numberOfInputs: 1,
            numberOfOutputs: 1,
            channelCount: 1,
            channelCountMode: "explicit",
            processorOptions: {
              channelId,
              frameSamples: CONFIG.frameSamples,
            },
          });
          node.port.onmessage = (event) => {
            transport.sendBinary(encodeFrame(channelId, event.data));
          };
          sourceNode.connect(node);
          node.connect(mute);
          log("info", "worklet tap installed", { channelId });
        },
        (err) => {
          log("warn", "AudioWorklet unavailable, using ScriptProcessor", {
            channelId,
            error: String(err),
          });
          this.installScriptProcessorSink(channelId, sourceNode, mute);
        },
      );
    },

    installScriptProcessorSink(channelId, sourceNode, mute) {
      const ctx = this.context;
      // ScriptProcessor buffer sizes must be a power of two; 4096 frames at
      // 16 kHz is ~256 ms, which is acceptable for a fallback path.
      const processor = ctx.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (event) => {
        const samples = event.inputBuffer.getChannelData(0);
        transport.sendBinary(encodeFrame(channelId, new Float32Array(samples)));
      };
      sourceNode.connect(processor);
      processor.connect(mute);
      log("info", "script-processor tap installed", { channelId });
    },

    /** Wire one remote audio track into the graph. */
    addTrack(track, streams) {
      if (track.kind !== "audio" || this.seenTracks.has(track)) {
        return;
      }
      this.seenTracks.add(track);

      const ctx = this.ensureContext();
      const stream = streams && streams.length ? streams[0] : new MediaStream([track]);
      let source;
      try {
        source = ctx.createMediaStreamSource(new MediaStream([track]));
      } catch (err) {
        log("error", "could not create audio source for track", {
          error: String(err),
          trackId: track.id,
        });
        return;
      }

      if (!CONFIG.perParticipant) {
        source.connect(this.mixNode);
        log("info", "remote track mixed", { trackId: track.id });
      } else if (this.nextChannelId > CONFIG.maxChannels) {
        // Out of channel ids: fold the extra speaker into the mix rather than
        // dropping their audio entirely.
        log("warn", "channel limit reached, folding track into channel 1", {
          trackId: track.id,
        });
        const fallback = this.channels.get(1);
        if (fallback && fallback.node) {
          source.connect(fallback.node);
        }
      } else {
        const channelId = this.nextChannelId;
        this.nextChannelId += 1;
        const gain = ctx.createGain();
        source.connect(gain);
        this.installSink(channelId, gain);
        this.channels.set(channelId, {
          label: "participant-" + channelId,
          node: gain,
          streamId: stream.id,
          trackId: track.id,
        });
        transport.send({
          type: "channel",
          id: channelId,
          label: "participant-" + channelId,
          trackId: track.id,
          streamId: stream.id,
        });
        log("info", "remote track on own channel", { channelId, trackId: track.id });
      }

      track.addEventListener("ended", () => {
        log("info", "remote track ended", { trackId: track.id });
        transport.send({ type: "track_ended", trackId: track.id });
      });
    },

    announceChannels() {
      this.channels.forEach((info, id) => {
        transport.send({ type: "channel", id, label: info.label });
      });
    },
  };

  /* ------------------------------------------------------------------ *
   * Caption overlay: renders recent utterances onto an offscreen canvas
   * and offers that canvas as a screen-share, so the transcript is visible
   * to every participant without the bot's own camera ever turning on.
   *
   * `getDisplayMedia` is patched to return the canvas stream directly
   * instead of prompting a real screen picker (which cannot work in this
   * automated, no-real-desktop context anyway) - Meet's own "Present now"
   * button is what triggers it, so only ONE Meet-specific DOM interaction
   * is needed ever, not one per caption line.
   * ------------------------------------------------------------------ */

  // A canvas capture stream only emits a frame when the canvas is actually
  // painted - it does NOT resample a static canvas at the requested rate.
  // Verified in Chromium: calling captureStream() and then never drawing
  // again yields ZERO frames and a consumer's play() never resolves, so the
  // shared tile stays black. Painting on a timer at the capture rate is what
  // keeps frames flowing between utterances; repainting identical pixels
  // still counts as a paint, so the text does not need to change.
  const OVERLAY_FPS = 2;
  const OVERLAY_FRAME_INTERVAL_MS = 500;
  const LINE_HEIGHT = 40;
  // 130px top margin + 14 * 40px keeps the last line clear of the 720px floor.
  const VISIBLE_LINES = 14;

  const captionOverlay = {
    canvas: null,
    ctx: null,
    lines: [], // { speaker, text }
    maxLines: 6,
    renderTimer: null,
    paintCount: 0,

    ensureCanvas() {
      if (this.canvas) {
        return this.canvas;
      }
      const canvas = document.createElement("canvas");
      canvas.width = 1280;
      canvas.height = 720;
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.render();
      return canvas;
    },

    push(speaker, text) {
      this.lines.push({ speaker, text });
      if (this.lines.length > this.maxLines) {
        this.lines.shift();
      }
      // Captions may arrive before the screen-share (and so the canvas)
      // has ever been requested; ensure it exists so the buffer is already
      // populated by the time captureStream() is actually called.
      this.ensureCanvas();
      this.render();
    },

    /** Wrap `text` to fit `maxWidth`, returning an array of lines. */
    wrap(text, maxWidth) {
      const words = text.split(/\s+/);
      const out = [];
      let line = "";
      for (const word of words) {
        const candidate = line ? line + " " + word : word;
        if (this.ctx.measureText(candidate).width > maxWidth && line) {
          out.push(line);
          line = word;
        } else {
          line = candidate;
        }
      }
      if (line) {
        out.push(line);
      }
      return out;
    },

    render() {
      if (!this.canvas || !this.ctx) {
        return;
      }
      const ctx = this.ctx;
      const w = this.canvas.width;
      const h = this.canvas.height;
      ctx.fillStyle = "#111318";
      ctx.fillRect(0, 0, w, h);

      ctx.fillStyle = "#8ab4f8";
      ctx.font = "600 28px Arial, sans-serif";
      ctx.fillText("Live transcript (meetbot)", 40, 56);
      ctx.strokeStyle = "#3c4043";
      ctx.beginPath();
      ctx.moveTo(40, 76);
      ctx.lineTo(w - 40, 76);
      ctx.stroke();

      const maxWidth = w - 80;
      const wrapped = [];
      for (const { speaker, text } of this.lines) {
        ctx.font = "600 30px Arial, sans-serif";
        const prefix = speaker ? speaker + ":  " : "";
        for (const line of this.wrap(prefix + text, maxWidth)) {
          wrapped.push(line);
        }
      }
      // Anchored below the header and growing downwards, so a short
      // transcript reads as a transcript that has just started rather than
      // as a mostly-empty screen with two stranded lines at the bottom.
      const visible = wrapped.slice(-VISIBLE_LINES);

      ctx.font = "30px Arial, sans-serif";
      ctx.fillStyle = "#e8eaed";
      let y = 130;
      for (const line of visible) {
        ctx.fillText(line, 40, y);
        y += LINE_HEIGHT;
      }

      if (this.lines.length === 0) {
        ctx.fillStyle = "#9aa0a6";
        ctx.font = "26px Arial, sans-serif";
        ctx.fillText("Waiting for speech...", 40, 130);
      }
      this.paintCount += 1;
    },

    /** Repaint on a timer so the capture stream keeps producing frames. */
    startRenderLoop() {
      if (this.renderTimer !== null) {
        return;
      }
      this.renderTimer = setInterval(() => {
        try {
          this.render();
        } catch (err) {
          log("warn", "caption render failed", { error: String(err) });
        }
      }, OVERLAY_FRAME_INTERVAL_MS);
    },

    stopRenderLoop() {
      if (this.renderTimer !== null) {
        clearInterval(this.renderTimer);
        this.renderTimer = null;
      }
    },

    captureStream() {
      const canvas = this.ensureCanvas();
      // 2 fps is plenty for text that changes on the order of seconds, and
      // keeps the encoded video cheap.
      const stream = canvas.captureStream(OVERLAY_FPS);
      // Must start AFTER captureStream(): the loop's paints are what the
      // stream turns into frames. Without it the stream stalls at zero.
      this.startRenderLoop();
      const [track] = stream.getVideoTracks();
      if (track) {
        track.addEventListener("ended", () => this.stopRenderLoop());
      }
      return stream;
    },
  };

  // Set the moment Meet actually asks for a display stream. The Python side
  // polls this to tell "the share really started" apart from "we clicked a
  // button that looked right", which is the difference between captions being
  // visible in the call and silently not.
  let overlayShared = false;

  if (navigator.mediaDevices) {
    navigator.mediaDevices.getDisplayMedia = async (constraints) => {
      log("info", "getDisplayMedia intercepted; sharing the caption canvas", {
        constraints: (() => {
          try {
            return JSON.stringify(constraints || {});
          } catch (err) {
            return "unserialisable";
          }
        })(),
      });
      overlayShared = true;
      const stream = captionOverlay.captureStream();
      const [track] = stream.getVideoTracks();
      if (track) {
        // A canvas track does not look like a screen capture: no
        // displaySurface, an empty label, and a contentHint of "". Meet
        // inspects a presented track, so the differences are papered over
        // here rather than hoping it does not look.
        try {
          track.contentHint = "text";
        } catch (err) {
          /* read-only in some builds; not fatal */
        }
        const nativeSettings = track.getSettings.bind(track);
        track.getSettings = () =>
          Object.assign({ displaySurface: "browser", cursor: "never" }, nativeSettings());
        try {
          Object.defineProperty(track, "label", {
            value: "meetbot:live-transcript",
            configurable: true,
          });
        } catch (err) {
          /* label is read-only in some builds; not fatal */
        }
        // Whether Meet keeps or discards the track is the answer to "the
        // share started but nothing appeared", so say so plainly.
        track.addEventListener("ended", () =>
          log("warn", "caption overlay track ENDED - Meet dropped the share"),
        );
        track.addEventListener("mute", () =>
          log("warn", "caption overlay track muted"),
        );
        setTimeout(() => {
          log("info", "caption overlay track status", {
            readyState: track.readyState,
            muted: track.muted,
            enabled: track.enabled,
            frames: captionOverlay.paintCount,
          });
        }, 5000);
      }
      return stream;
    };
  } else {
    // Only happens on an insecure origin; Meet is https so this is a
    // configuration problem, not an expected path.
    log("error", "navigator.mediaDevices missing; caption overlay unavailable");
  }

  /* ------------------------------------------------------------------ *
   * RTCPeerConnection interception.
   * ------------------------------------------------------------------ */

  const NativePeerConnection = window.RTCPeerConnection;
  if (!NativePeerConnection) {
    log("error", "RTCPeerConnection missing; audio capture disabled");
    return;
  }

  const PatchedPeerConnection = function (...args) {
    const pc = new NativePeerConnection(...args);
    log("info", "peer connection created");

    pc.addEventListener("track", (event) => {
      try {
        capture.addTrack(event.track, event.streams);
      } catch (err) {
        log("error", "track handler failed", { error: String(err) });
      }
    });

    return pc;
  };

  PatchedPeerConnection.prototype = NativePeerConnection.prototype;
  // Meet reads these off the constructor; preserve them so nothing breaks.
  Object.defineProperty(PatchedPeerConnection, "name", {
    value: NativePeerConnection.name,
  });
  ["generateCertificate", "getDefaultIceServers"].forEach((method) => {
    if (typeof NativePeerConnection[method] === "function") {
      PatchedPeerConnection[method] = NativePeerConnection[method].bind(
        NativePeerConnection,
      );
    }
  });

  window.RTCPeerConnection = PatchedPeerConnection;
  window.webkitRTCPeerConnection = PatchedPeerConnection;

  /* ------------------------------------------------------------------ *
   * Control surface used by the Python side via page.evaluate().
   * ------------------------------------------------------------------ */

  window.__meetbot = {
    /** Push one finalised utterance onto the caption overlay canvas. */
    updateCaption(speaker, text) {
      captionOverlay.push(speaker, text);
    },
    /** Attach a human-readable label to a capture channel. */
    labelChannel(channelId, label) {
      const info = capture.channels.get(channelId);
      if (info) {
        info.label = label;
      }
      transport.send({ type: "channel", id: channelId, label });
    },
    /** Whether Meet has actually requested (and been given) the overlay. */
    overlayShared() {
      return overlayShared;
    },
    /** Snapshot of capture state, for health checks and diagnostics. */
    stats() {
      return {
        connected: transport.connected,
        overlayShared,
        overlayPaints: captionOverlay.paintCount,
        pending: transport.pending.length,
        channels: Array.from(capture.channels.entries()).map(([id, info]) => ({
          id,
          label: info.label,
        })),
        contextState: capture.context ? capture.context.state : "none",
      };
    },
    /** Flush and close the bridge socket ahead of browser shutdown. */
    stop() {
      captionOverlay.stopRenderLoop();
      transport.close();
      if (capture.context) {
        capture.context.close().catch(() => undefined);
      }
    },
  };

  // Deferred rather than called synchronously here: this script runs via
  // `add_init_script`, before Meet's own bootstrap JS executes. Opening the
  // WebSocket to the local bridge immediately - i.e. before Meet's code gets
  // a turn to run at all - was observed to make Meet abort and restart its
  // own page navigation (reproduced consistently: removing only this one
  // call fixed a hang/crash on load that no other change affected). A single
  // macrotask of delay is enough to let Meet's script start first.
  setTimeout(() => transport.connect(), 0);
  log("info", "hook installed", {
    sampleRate: CONFIG.sampleRate,
    perParticipant: CONFIG.perParticipant,
  });
})();
