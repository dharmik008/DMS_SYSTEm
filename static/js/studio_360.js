/**
 * Spin360Viewer — drag-to-rotate viewer for an ordered set of frames shot
 * around a vehicle.
 *
 * Renders to a <canvas> so swapping frames never flashes, and supports
 * auto-rotate, drag with inertia, zoom + pan, keyboard control and fullscreen.
 * No dependencies; the host page owns all markup and styling.
 *
 *   const viewer = new Spin360Viewer(viewportEl, { onFrameChange(i, n) {} });
 *   await viewer.load(urls);
 *   viewer.play();
 */
(function (global) {
  'use strict';

  const INERTIA_DECAY_PER_60FPS_FRAME = 0.92;
  const INERTIA_MIN_SPEED = 0.002;   // frames per ms
  const MAX_FRAME_DT = 100;          // ms; avoids a jump after a background tab resumes
  const MIN_ZOOM = 1;
  const MAX_ZOOM = 3;
  const ZOOM_STEP = 1.5;
  const DOUBLE_CLICK_ZOOM = 2;

  function loadImage(url) {
    const img = new Image();
    img.decoding = 'async';
    img.src = url;
    return img.decode().then(() => img);
  }

  function noop() {}

  class Spin360Viewer {
    constructor(viewport, options = {}) {
      this.viewport = viewport;
      this.canvas = viewport.querySelector('canvas');
      this.ctx = this.canvas.getContext('2d');
      this.fullscreenTarget = options.fullscreenTarget || viewport;

      this.onFrameChange = options.onFrameChange || noop;  // (index, count)
      this.onPlayStateChange = options.onPlayStateChange || noop;  // (playing)
      this.onZoomChange = options.onZoomChange || noop;  // (zoom)
      this.onSettle = options.onSettle || noop;  // (index) user stopped on a frame

      this.frames = [];
      this.position = 0;  // fractional frame index; rounded for display
      this.secondsPerTurn = options.secondsPerTurn || 6;
      this.direction = 1;
      this.playing = false;
      this.zoom = MIN_ZOOM;
      this.panX = 0;
      this.panY = 0;

      this._velocity = 0;
      this._raf = null;
      this._lastTick = 0;
      this._drag = null;
      this._loadToken = 0;
      this._drawn = null;
      this._lastReportedIndex = null;

      this._tick = this._tick.bind(this);
      this._listeners = [
        ['pointerdown', this._onPointerDown.bind(this)],
        ['pointermove', this._onPointerMove.bind(this)],
        ['pointerup', this._onPointerUp.bind(this)],
        ['pointercancel', this._onPointerUp.bind(this)],
        ['dblclick', this._onDoubleClick.bind(this)],
        ['keydown', this._onKeyDown.bind(this)],
      ];
      this._listeners.forEach(([type, fn]) => viewport.addEventListener(type, fn));

      this._resizeObserver = new ResizeObserver(() => this._resize());
      this._resizeObserver.observe(viewport);
    }

    get frameCount() {
      return this.frames.length;
    }

    get frameIndex() {
      const n = this.frameCount;
      return n ? ((Math.round(this.position) % n) + n) % n : 0;
    }

    /**
     * Preload and decode every frame before showing any, so the first spin is
     * smooth. Frames that fail to load are skipped.
     *
     * Resolves to {loadedIndexes, failed} — the positions in `urls` that are now
     * frames 0..n-1 — or null when a newer load() superseded this one.
     */
    async load(urls, onProgress = noop) {
      const token = ++this._loadToken;
      this._stopPlaying();
      this._velocity = 0;

      let settled = 0;
      const track = (img) => {
        settled += 1;
        if (token === this._loadToken) onProgress(settled, urls.length);
        return img;
      };
      const images = await Promise.all(
        urls.map((url) => loadImage(url).then(track, () => track(null)))
      );
      if (token !== this._loadToken) return null;

      const loadedIndexes = [];
      images.forEach((img, i) => {
        if (img) loadedIndexes.push(i);
      });
      this.frames = loadedIndexes.map((i) => images[i]);
      if (!this.frames.length) throw new Error('None of the frames could be loaded.');

      this.position = 0;
      this._lastReportedIndex = null;  // the count changed, so report even frame 0
      this.resetZoom();
      this._render(true);
      return { loadedIndexes, failed: urls.length - loadedIndexes.length };
    }

    clear() {
      this._loadToken += 1;
      this._stopPlaying();
      this.frames = [];
      this._velocity = 0;
      this._lastReportedIndex = null;
      this._render(true);
    }

    /** Jump to a frame. Stops auto-rotate; does not report a settled frame. */
    setFrame(index) {
      if (!this.frameCount) return;
      this._stopPlaying();
      this._velocity = 0;
      this.position = index;
      this._wrap();
      this._render();
    }

    step(delta) {
      if (!this.frameCount) return;
      this._stopPlaying();
      this.setFrame(Math.round(this.position) + delta);
      this.onSettle(this.frameIndex);
    }

    play() {
      if (this.playing || this.frameCount < 2) return;
      this.playing = true;
      this._velocity = 0;
      this.onPlayStateChange(true);
      this._startLoop();
    }

    pause() {
      if (this._stopPlaying()) this.onSettle(this.frameIndex);
    }

    togglePlay() {
      if (this.playing) this.pause();
      else this.play();
    }

    setSpeed(secondsPerTurn) {
      this.secondsPerTurn = secondsPerTurn;
    }

    setReversed(reversed) {
      this.direction = reversed ? -1 : 1;
    }

    zoomIn() {
      this._setZoom(this.zoom * ZOOM_STEP);
    }

    zoomOut() {
      this._setZoom(this.zoom / ZOOM_STEP);
    }

    resetZoom() {
      this._setZoom(MIN_ZOOM);
    }

    toggleFullscreen() {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else if (this.fullscreenTarget.requestFullscreen) {
        this.fullscreenTarget.requestFullscreen().catch(noop);
      }
    }

    destroy() {
      this.clear();
      this._resizeObserver.disconnect();
      this._listeners.forEach(([type, fn]) => this.viewport.removeEventListener(type, fn));
    }

    // ── Animation ────────────────────────────────────────────────────────────

    /** Stops auto-rotate without reporting a settled frame. Returns whether it was playing. */
    _stopPlaying() {
      if (!this.playing) return false;
      this.playing = false;
      this.position = Math.round(this.position);
      this._render();
      this.onPlayStateChange(false);
      return true;
    }

    _startLoop() {
      if (this._raf) return;
      this._lastTick = 0;
      this._raf = requestAnimationFrame(this._tick);
    }

    _tick(now) {
      const dt = this._lastTick ? Math.min(now - this._lastTick, MAX_FRAME_DT) : 0;
      this._lastTick = now;
      let keepRunning = false;

      if (this.playing) {
        const framesPerMs = this.frameCount / (this.secondsPerTurn * 1000);
        this.position += this.direction * framesPerMs * dt;
        keepRunning = true;
      } else if (Math.abs(this._velocity) > INERTIA_MIN_SPEED) {
        this.position += this._velocity * dt;
        this._velocity *= Math.pow(INERTIA_DECAY_PER_60FPS_FRAME, dt / (1000 / 60));
        keepRunning = true;
      }

      this._wrap();
      this._render();

      if (keepRunning) {
        this._raf = requestAnimationFrame(this._tick);
        return;
      }
      this._raf = null;
      if (this._velocity) {
        // Inertia just ran out: snap to a whole frame and report where we stopped.
        this._velocity = 0;
        this.position = Math.round(this.position);
        this._render();
        this.onSettle(this.frameIndex);
      }
    }

    _wrap() {
      const n = this.frameCount;
      if (n) this.position = ((this.position % n) + n) % n;
    }

    // ── Rendering ────────────────────────────────────────────────────────────

    _resize() {
      const ratio = window.devicePixelRatio || 1;
      const { clientWidth, clientHeight } = this.viewport;
      this.canvas.width = Math.max(1, Math.round(clientWidth * ratio));
      this.canvas.height = Math.max(1, Math.round(clientHeight * ratio));
      this._clampPan();
      this._render(true);
    }

    _render(force = false) {
      const index = this.frameIndex;
      const key = `${index}|${this.zoom}|${this.panX}|${this.panY}|${this.canvas.width}x${this.canvas.height}`;
      if (!force && key === this._drawn) return;
      this._drawn = key;

      const { ctx, canvas } = this;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      const img = this.frames[index];
      if (!img) return;
      const { width, height } = this._drawSize(img);
      const ratio = window.devicePixelRatio || 1;
      const x = (canvas.width - width) / 2 + this.panX * ratio;
      const y = (canvas.height - height) / 2 + this.panY * ratio;
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(img, x, y, width, height);

      if (index !== this._lastReportedIndex) {
        this._lastReportedIndex = index;
        this.onFrameChange(index, this.frameCount);
      }
    }

    _drawSize(img) {
      const fit = Math.min(this.canvas.width / img.naturalWidth, this.canvas.height / img.naturalHeight);
      const scale = fit * this.zoom;
      return { width: img.naturalWidth * scale, height: img.naturalHeight * scale };
    }

    // ── Zoom & pan ───────────────────────────────────────────────────────────

    _setZoom(value) {
      const zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));
      if (zoom === MIN_ZOOM) {
        this.panX = 0;
        this.panY = 0;
      }
      this.zoom = zoom;
      this._clampPan();
      this.viewport.classList.toggle('zoomed', zoom > MIN_ZOOM);
      this._render();
      this.onZoomChange(zoom);
    }

    _clampPan() {
      const img = this.frames[this.frameIndex];
      if (!img) return;
      const ratio = window.devicePixelRatio || 1;
      const { width, height } = this._drawSize(img);
      const maxX = Math.max(0, (width - this.canvas.width) / 2 / ratio);
      const maxY = Math.max(0, (height - this.canvas.height) / 2 / ratio);
      this.panX = Math.min(maxX, Math.max(-maxX, this.panX));
      this.panY = Math.min(maxY, Math.max(-maxY, this.panY));
    }

    // ── Input ────────────────────────────────────────────────────────────────

    _onPointerDown(event) {
      if (!this.frameCount || event.button !== 0) return;
      this.viewport.setPointerCapture(event.pointerId);
      this._stopPlaying();  // the drag's own pointerup reports where it settles
      this._velocity = 0;
      this._drag = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        startPosition: this.position,
        startPanX: this.panX,
        startPanY: this.panY,
        lastPosition: this.position,
        lastTime: event.timeStamp,
        velocity: 0,
      };
      this.viewport.classList.add('dragging');
    }

    _onPointerMove(event) {
      const drag = this._drag;
      if (!drag || event.pointerId !== drag.pointerId) return;
      const dx = event.clientX - drag.startX;

      if (this.zoom > MIN_ZOOM) {
        this.panX = drag.startPanX + dx;
        this.panY = drag.startPanY + (event.clientY - drag.startY);
        this._clampPan();
        this._render();
        return;
      }

      // Dragging across the full width turns the car once; dragging left
      // turns it the same way as auto-rotate.
      const pixelsPerFrame = this.viewport.clientWidth / this.frameCount;
      this.position = drag.startPosition - (dx / pixelsPerFrame) * this.direction;

      const dt = event.timeStamp - drag.lastTime;
      if (dt > 0) {
        const instant = (this.position - drag.lastPosition) / dt;
        drag.velocity = 0.8 * instant + 0.2 * drag.velocity;
        drag.lastPosition = this.position;
        drag.lastTime = event.timeStamp;
      }
      this._wrap();
      this._render();
    }

    _onPointerUp(event) {
      const drag = this._drag;
      if (!drag || event.pointerId !== drag.pointerId) return;
      this._drag = null;
      this.viewport.classList.remove('dragging');

      // A pause before letting go means the user meant to stop there.
      const heldStill = event.timeStamp - drag.lastTime > 80;
      if (this.zoom === MIN_ZOOM && !heldStill && Math.abs(drag.velocity) > INERTIA_MIN_SPEED) {
        this._velocity = drag.velocity;
        this._startLoop();
        return;
      }
      this.position = Math.round(this.position);
      this._wrap();
      this._render();
      this.onSettle(this.frameIndex);
    }

    _onDoubleClick() {
      if (!this.frameCount) return;
      if (this.zoom > MIN_ZOOM) this.resetZoom();
      else this._setZoom(DOUBLE_CLICK_ZOOM);
    }

    _onKeyDown(event) {
      if (!this.frameCount) return;
      const actions = {
        ArrowLeft: () => this.step(-1),
        ArrowRight: () => this.step(1),
        ' ': () => this.togglePlay(),
        '+': () => this.zoomIn(),
        '=': () => this.zoomIn(),
        '-': () => this.zoomOut(),
        '0': () => this.resetZoom(),
      };
      const action = actions[event.key];
      if (!action) return;
      event.preventDefault();
      action();
    }
  }

  Spin360Viewer.MIN_ZOOM = MIN_ZOOM;
  Spin360Viewer.MAX_ZOOM = MAX_ZOOM;
  global.Spin360Viewer = Spin360Viewer;
})(window);
