/* Shared live-feed engine.
 *
 * The server sends three raw channel planes per frame (NIR, green, blue) and the
 * browser derives BNDVI and paints from them. Both the Debug view and the setup
 * wizard need that, so it lives here once rather than being copied.
 *
 * Why client-side: moving the k or threshold sliders repaints with no network
 * round-trip, and the renders physically cannot drift out of sync because they
 * all come from one array.
 */
(function (global) {
  'use strict';

  const LUT = null;   // built lazily, needs Colormap

  function Feed(options) {
    options = options || {};
    this.w = 160;
    this.h = 120;
    this.nir = null;
    this.green = null;
    this.blue = null;
    this.bndvi = null;
    this.source = 'unknown';
    this.seq = -1;
    this.mismatch = null;
    this.live = true;
    this.fps = options.fps || 12;
    this.correctNir = !!options.correctNir;
    this.k = options.k === undefined ? 0.8 : options.k;
    this.tHealthy = options.tHealthy === undefined ? 0.3 : options.tHealthy;
    this.tModerate = options.tModerate === undefined ? 0.1 : options.tModerate;
    this.onFrame = options.onFrame || function () {};
    this.onError = options.onError || function () {};
    this._poll = null;
    this._inFlight = false;
    this._lut = null;
  }

  Feed.prototype.start = function () {
    const self = this;
    if (this._poll) this._poll.stop();
    this._poll = HC.poll(function () { return self._fetch(); },
      Math.max(40, 1000 / this.fps), {
        onError: function (err, n) { if (n === 1) self.onError(err); }
      });
  };

  Feed.prototype.stop = function () {
    if (this._poll) { this._poll.stop(); this._poll = null; }
  };

  Feed.prototype.setFps = function (fps) {
    this.fps = Math.max(1, Math.min(24, fps));
    if (this._poll) this.start();
  };

  Feed.prototype._fetch = async function () {
    if (!this.live || this._inFlight) return;
    this._inFlight = true;
    try {
      const res = await fetch('/api/preview/frame', { cache: 'no-store' });
      if (!res.ok) {
        const body = await res.json().catch(function () { return {}; });
        throw new Error(body.error || ('HTTP ' + res.status));
      }
      const w = parseInt(res.headers.get('X-Frame-Width'), 10) || this.w;
      const h = parseInt(res.headers.get('X-Frame-Height'), 10) || this.h;
      const planes = parseInt(res.headers.get('X-Frame-Planes'), 10) || 3;
      const buf = new Uint8Array(await res.arrayBuffer());
      if (buf.length < w * h * planes) throw new Error('short frame');
      const n = w * h;
      this.w = w;
      this.h = h;
      this.nir = buf.subarray(0, n);
      if (planes >= 3) {
        this.green = buf.subarray(n, n * 2);
        this.blue = buf.subarray(n * 2, n * 3);
      } else {
        // tolerate an older two-plane server rather than showing nothing
        this.blue = buf.subarray(n, n * 2);
        this.green = null;
      }
      this.source = res.headers.get('X-Frame-Source') || 'unknown';
      this.mismatch = res.headers.get('X-Control-Mismatch') || null;
      this.seq = parseInt(res.headers.get('X-Frame-Seq'), 10) || 0;
      this.compute();
      this.onFrame(this);
    } finally {
      this._inFlight = false;
    }
  };

  /** Derive BNDVI. Mirrors bndvi.compute_bndvi() exactly. */
  Feed.prototype.compute = function () {
    if (!this.nir) return null;
    const n = this.w * this.h;
    if (!this.bndvi || this.bndvi.length !== n) this.bndvi = new Float32Array(n);
    const out = this.bndvi, nir = this.nir, blue = this.blue;
    const correct = this.correctNir, k = this.k;
    for (let i = 0; i < n; i++) {
      const r = nir[i];
      // clamp to a small positive value so dense-vegetation pixels (where k*R
      // can exceed B) don't blow up or flip sign — same as the Python
      const vis = correct ? Math.max(1, blue[i] - k * r) : blue[i];
      const den = r + vis;
      const v = den === 0 ? 0 : (r - vis) / den;
      out[i] = v < -1 ? -1 : (v > 1 ? 1 : v);
    }
    return out;
  };

  Feed.prototype.stats = function () {
    const v = this.bndvi;
    if (!v) return null;
    const n = v.length;
    let sum = 0, mn = 1, mx = -1, h = 0, m = 0, s = 0;
    for (let i = 0; i < n; i++) {
      const x = v[i];
      sum += x;
      if (x < mn) mn = x;
      if (x > mx) mx = x;
      if (x > this.tHealthy) h++;
      else if (x >= this.tModerate) m++;
      else s++;
    }
    const mean = sum / n;
    let sq = 0;
    for (let i = 0; i < n; i++) sq += (v[i] - mean) * (v[i] - mean);
    return {
      mean: mean, min: mn, max: mx, std: Math.sqrt(sq / n),
      h: (h / n) * 100, m: (m / n) * 100, s: (s / n) * 100
    };
  };

  Feed.prototype.channelMeans = function () {
    if (!this.nir) return null;
    const n = this.w * this.h;
    let r = 0, g = 0, b = 0;
    for (let i = 0; i < n; i++) { r += this.nir[i]; b += this.blue[i]; }
    if (this.green) for (let i = 0; i < n; i++) g += this.green[i];
    return {
      nir: r / n, green: this.green ? g / n : null, blue: b / n
    };
  };

  /* ── painting ───────────────────────────────────────────────────────────── */

  Feed.prototype._blit = function (canvas, fill) {
    if (!canvas) return;
    const w = this.w, h = this.h;
    if (!canvas._off || canvas._off.width !== w || canvas._off.height !== h) {
      canvas._off = document.createElement('canvas');
      canvas._off.width = w;
      canvas._off.height = h;
      canvas._ctx = canvas._off.getContext('2d');
      canvas._img = canvas._ctx.createImageData(w, h);
    }
    fill(canvas._img.data);
    canvas._ctx.putImageData(canvas._img, 0, 0);
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(canvas._off, 0, 0, canvas.width, canvas.height);
  };

  /** What the eye sees: NIR in red, blue in blue, real green in between.
   *  Healthy vegetation comes out pink — the single most useful sanity check. */
  Feed.prototype.paintRaw = function (canvas) {
    const self = this;
    this._blit(canvas, function (d) {
      const n = self.w * self.h;
      for (let i = 0; i < n; i++) {
        d[i * 4] = self.nir[i];
        d[i * 4 + 1] = self.green ? self.green[i] : 0.28 * self.nir[i];
        d[i * 4 + 2] = self.blue[i];
        d[i * 4 + 3] = 255;
      }
    });
  };

  Feed.prototype.paintHeat = function (canvas) {
    if (!this._lut) this._lut = Colormap.buildLut(512);
    const lut = this._lut, v = this.bndvi;
    this._blit(canvas, function (d) {
      for (let i = 0; i < v.length; i++) {
        const idx = (((v[i] + 1) * 0.5 * 511) | 0) * 3;
        d[i * 4] = lut[idx];
        d[i * 4 + 1] = lut[idx + 1];
        d[i * 4 + 2] = lut[idx + 2];
        d[i * 4 + 3] = 255;
      }
    });
  };

  Feed.prototype.paintFalse = function (canvas) {
    const v = this.bndvi, th = this.tHealthy, tm = this.tModerate;
    const B = Colormap.BANDS;
    this._blit(canvas, function (d) {
      for (let i = 0; i < v.length; i++) {
        const c = v[i] > th ? B.healthy : (v[i] >= tm ? B.moderate : B.stressed);
        d[i * 4] = c[0]; d[i * 4 + 1] = c[1]; d[i * 4 + 2] = c[2];
        d[i * 4 + 3] = 255;
      }
    });
  };

  Feed.prototype.paintChannel = function (canvas, which, tint) {
    const plane = which === 'nir' ? this.nir
      : (which === 'green' ? this.green : this.blue);
    if (!plane) return;
    this._blit(canvas, function (d) {
      for (let i = 0; i < plane.length; i++) {
        d[i * 4] = plane[i] * tint[0];
        d[i * 4 + 1] = plane[i] * tint[1];
        d[i * 4 + 2] = plane[i] * tint[2];
        d[i * 4 + 3] = 255;
      }
    });
  };

  Feed.prototype.paintHistogram = function (canvas) {
    if (!canvas || !this.bndvi) return;
    const v = this.bndvi;
    const ctx = canvas.getContext('2d');
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#f9f4ed';
    ctx.fillRect(0, 0, w, h);

    const bins = 48;
    const counts = new Array(bins).fill(0);
    for (let i = 0; i < v.length; i++) {
      let b = (((v[i] + 1) / 2) * bins) | 0;
      if (b < 0) b = 0; else if (b >= bins) b = bins - 1;
      counts[b]++;
    }
    const peak = Math.max.apply(null, counts) || 1;
    const pad = 26;
    const bw = (w - pad * 2) / bins;
    for (let i = 0; i < bins; i++) {
      const centre = ((i + 0.5) / bins) * 2 - 1;
      const bh = (counts[i] / peak) * (h - pad * 1.7);
      ctx.fillStyle = Colormap.rgbCss(Colormap.cmap(centre));
      ctx.fillRect(pad + i * bw, h - pad - bh, Math.max(1, bw - 1.5), bh);
    }
    ctx.strokeStyle = 'rgba(32,30,29,.22)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pad, h - pad + 0.5);
    ctx.lineTo(w - pad, h - pad + 0.5);
    ctx.stroke();

    const self = this;
    function mark(value, colour, label) {
      const x = pad + ((value + 1) / 2) * (w - pad * 2);
      ctx.strokeStyle = colour;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(x, 14);
      ctx.lineTo(x, h - pad);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = colour;
      ctx.font = '11px Figtree, system-ui';
      ctx.textAlign = 'center';
      ctx.fillText(label, x, 11);
    }
    mark(self.tModerate, '#c1442e', 'stressed');
    mark(self.tHealthy, '#2f8f3e', 'healthy');

    ctx.fillStyle = 'rgba(32,30,29,.55)';
    ctx.font = '11px Figtree, system-ui';
    ctx.textAlign = 'left'; ctx.fillText('−1', 4, h - 9);
    ctx.textAlign = 'right'; ctx.fillText('+1', w - 4, h - 9);
    ctx.textAlign = 'center'; ctx.fillText('BNDVI', w / 2, h - 9);
  };

  /* ── drag-a-box region selection, shared by both views ──────────────────── */

  /** Wire a crosshair overlay so the user can drag a box over the live feed.
   *  Returns an object exposing the current box in 0..1 frame coordinates. */
  function RegionPicker(layer, boxEl, onChange) {
    const state = { box: null };
    let dragging = false, start = null;

    function pos(e) {
      const r = layer.getBoundingClientRect();
      const p = e.touches ? e.touches[0] : e;
      return {
        x: Math.min(1, Math.max(0, (p.clientX - r.left) / r.width)),
        y: Math.min(1, Math.max(0, (p.clientY - r.top) / r.height))
      };
    }
    function draw() {
      if (!boxEl) return;
      if (!state.box) { boxEl.hidden = true; return; }
      const b = state.box;
      boxEl.hidden = false;
      boxEl.style.left = (Math.min(b.x0, b.x1) * 100) + '%';
      boxEl.style.top = (Math.min(b.y0, b.y1) * 100) + '%';
      boxEl.style.width = (Math.abs(b.x1 - b.x0) * 100) + '%';
      boxEl.style.height = (Math.abs(b.y1 - b.y0) * 100) + '%';
    }
    function begin(e) {
      dragging = true;
      start = pos(e);
      state.box = { x0: start.x, y0: start.y, x1: start.x, y1: start.y };
      e.preventDefault();
    }
    function move(e) {
      if (!dragging) return;
      const p = pos(e);
      state.box = { x0: start.x, y0: start.y, x1: p.x, y1: p.y };
      draw();
      e.preventDefault();
    }
    function end() {
      if (!dragging) return;
      dragging = false;
      const b = state.box;
      // a tap rather than a drag clears the selection
      if (Math.abs(b.x1 - b.x0) < 0.03 || Math.abs(b.y1 - b.y0) < 0.03) {
        state.box = null;
      }
      draw();
      if (onChange) onChange(state.box);
    }

    layer.addEventListener('mousedown', begin);
    layer.addEventListener('mousemove', move);
    window.addEventListener('mouseup', end);
    layer.addEventListener('touchstart', begin, { passive: false });
    layer.addEventListener('touchmove', move, { passive: false });
    layer.addEventListener('touchend', end);

    state.redraw = draw;
    state.clear = function () { state.box = null; draw(); };
    return state;
  }

  global.Feed = Feed;
  global.RegionPicker = RegionPicker;
}(window));
