/* Debug view.
 *
 * The server sends two raw channel planes per frame (NIR and blue, 160x120).
 * Everything else — BNDVI, the heatmap, the false colour, the histogram, the
 * three channel splits, the statistics — is derived here, in the browser.
 *
 * Why: moving the k slider or a threshold repaints all seven canvases with no
 * network round-trip, so the controls feel like instruments rather than a form.
 * It also means the renders physically cannot drift out of sync, because they
 * are all computed from one array. The Pi's only job per frame is
 * grab -> downsample -> send, which is what leaves it enough CPU to also fly.
 */
(function () {
  'use strict';

  const state = {
    w: 160, h: 120,
    nir: null, blue: null, bndvi: null,
    live: true,
    fps: 12,
    correctNir: false,
    k: 0.8,
    tHealthy: 0.3,
    tModerate: 0.1,
    source: 'unknown',
    seq: -1,
    roi: null,          // {x0,y0,x1,y1} in 0..1, for the white reference
    stats: null
  };

  const canvases = {};
  let framePoll = null;
  let inFlight = false;

  function init() {
    const root = HC.$('#debug-root');
    if (!root) return;

    // seed from the server-rendered settings so a reload keeps your place
    const cfg = JSON.parse(root.dataset.settings);
    state.fps = cfg.preview_fps;
    state.correctNir = cfg.correct_nir_leakage;
    state.k = cfg.nir_leak_coef;
    state.tHealthy = cfg.threshold_healthy;
    state.tModerate = cfg.threshold_moderate;

    ['dbg-raw', 'dbg-heat', 'dbg-fc', 'dbg-hist', 'ch-r', 'ch-g', 'ch-b']
      .forEach(function (id) { canvases[id] = HC.$('#' + id); });

    wireControls();
    wireRoi();
    startFrames();
    pollLogs();
  }

  /* ── frame fetching ─────────────────────────────────────────────────────── */

  function startFrames() {
    if (framePoll) framePoll.stop();
    framePoll = HC.poll(fetchFrame, Math.max(40, 1000 / state.fps), {
      onError: function (err, n) {
        if (n === 1) HC.toast('Live feed stopped: ' + err.message, true);
      }
    });
  }

  async function fetchFrame() {
    if (!state.live || inFlight) return;
    inFlight = true;
    try {
      const res = await fetch('/api/preview/frame', { cache: 'no-store' });
      if (!res.ok) {
        const body = await res.json().catch(function () { return {}; });
        throw new Error(body.error || ('HTTP ' + res.status));
      }
      const w = parseInt(res.headers.get('X-Frame-Width'), 10) || state.w;
      const h = parseInt(res.headers.get('X-Frame-Height'), 10) || state.h;
      const buf = new Uint8Array(await res.arrayBuffer());
      if (buf.length < w * h * 2) throw new Error('short frame');
      state.w = w; state.h = h;
      state.nir = buf.subarray(0, w * h);
      state.blue = buf.subarray(w * h, w * h * 2);
      state.source = res.headers.get('X-Frame-Source') || 'unknown';
      state.seq = parseInt(res.headers.get('X-Frame-Seq'), 10) || 0;
      setFeedLabel();
      render();
    } finally {
      inFlight = false;
    }
  }

  /* ── the maths, mirroring bndvi.compute_bndvi ───────────────────────────── */

  function computeBndvi() {
    const n = state.w * state.h;
    if (!state.nir) return null;
    if (!state.bndvi || state.bndvi.length !== n) state.bndvi = new Float32Array(n);
    const out = state.bndvi;
    const nir = state.nir, blue = state.blue;
    const correct = state.correctNir, k = state.k;
    for (let i = 0; i < n; i++) {
      const r = nir[i];
      // clamp to a small positive value so dense-vegetation pixels (where k*R
      // can exceed B) don't blow up or flip sign — same as the Python
      const vis = correct ? Math.max(1, blue[i] - k * r) : blue[i];
      const den = r + vis;
      let v = den === 0 ? 0 : (r - vis) / den;
      out[i] = v < -1 ? -1 : (v > 1 ? 1 : v);
    }
    return out;
  }

  function computeStats(v) {
    const n = v.length;
    let sum = 0, mn = 1, mx = -1, h = 0, m = 0, s = 0;
    for (let i = 0; i < n; i++) {
      const x = v[i];
      sum += x;
      if (x < mn) mn = x;
      if (x > mx) mx = x;
      if (x > state.tHealthy) h++;
      else if (x >= state.tModerate) m++;
      else s++;
    }
    const mean = sum / n;
    let sq = 0;
    for (let i = 0; i < n; i++) sq += (v[i] - mean) * (v[i] - mean);
    return {
      mean: mean, min: mn, max: mx, std: Math.sqrt(sq / n),
      h: (h / n) * 100, m: (m / n) * 100, s: (s / n) * 100
    };
  }

  /* ── painting ───────────────────────────────────────────────────────────── */

  const LUT = Colormap.buildLut(512);

  function blit(canvas, fill) {
    if (!canvas) return;
    const w = state.w, h = state.h;
    if (!canvas._off || canvas._off.width !== w) {
      canvas._off = document.createElement('canvas');
      canvas._off.width = w; canvas._off.height = h;
      canvas._ctx = canvas._off.getContext('2d');
      canvas._img = canvas._ctx.createImageData(w, h);
    }
    fill(canvas._img.data);
    canvas._ctx.putImageData(canvas._img, 0, 0);
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(canvas._off, 0, 0, canvas.width, canvas.height);
  }

  function paintRaw() {
    // Reconstruct what the eye sees: NIR in red, visible blue in blue, and the
    // gel-suppressed green in between. Healthy vegetation comes out pink, which
    // is the single most useful sanity check on this whole rig.
    blit(canvases['dbg-raw'], function (d) {
      const nir = state.nir, blue = state.blue, n = state.w * state.h;
      for (let i = 0; i < n; i++) {
        d[i * 4] = nir[i];
        d[i * 4 + 1] = 0.28 * nir[i] + 0.25 * blue[i];
        d[i * 4 + 2] = blue[i];
        d[i * 4 + 3] = 255;
      }
    });
  }

  function paintHeat(v) {
    blit(canvases['dbg-heat'], function (d) {
      for (let i = 0; i < v.length; i++) {
        const idx = (((v[i] + 1) * 0.5 * 511) | 0) * 3;
        d[i * 4] = LUT[idx];
        d[i * 4 + 1] = LUT[idx + 1];
        d[i * 4 + 2] = LUT[idx + 2];
        d[i * 4 + 3] = 255;
      }
    });
  }

  function paintFalse(v) {
    const th = state.tHealthy, tm = state.tModerate;
    const B = Colormap.BANDS;
    blit(canvases['dbg-fc'], function (d) {
      for (let i = 0; i < v.length; i++) {
        const c = v[i] > th ? B.healthy : (v[i] >= tm ? B.moderate : B.stressed);
        d[i * 4] = c[0]; d[i * 4 + 1] = c[1]; d[i * 4 + 2] = c[2]; d[i * 4 + 3] = 255;
      }
    });
  }

  function paintChannel(canvas, plane, tint) {
    blit(canvas, function (d) {
      for (let i = 0; i < plane.length; i++) {
        d[i * 4] = plane[i] * tint[0];
        d[i * 4 + 1] = plane[i] * tint[1];
        d[i * 4 + 2] = plane[i] * tint[2];
        d[i * 4 + 3] = 255;
      }
    });
  }

  function paintChannels() {
    const n = state.w * state.h;
    paintChannel(canvases['ch-r'], state.nir, [1, 0.22, 0.22]);
    if (!paintChannels._g || paintChannels._g.length !== n) {
      paintChannels._g = new Float32Array(n);
    }
    const g = paintChannels._g;
    for (let i = 0; i < n; i++) g[i] = 0.28 * state.nir[i] + 0.25 * state.blue[i];
    paintChannel(canvases['ch-g'], g, [0.28, 1, 0.36]);
    paintChannel(canvases['ch-b'], state.blue, [0.3, 0.42, 1]);
  }

  function paintHistogram(v) {
    const canvas = canvases['dbg-hist'];
    if (!canvas) return;
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
    mark(state.tModerate, '#c1442e', 'stressed');
    mark(state.tHealthy, '#2f8f3e', 'healthy');

    ctx.fillStyle = 'rgba(32,30,29,.55)';
    ctx.font = '11px Figtree, system-ui';
    ctx.textAlign = 'left'; ctx.fillText('−1', 4, h - 9);
    ctx.textAlign = 'right'; ctx.fillText('+1', w - 4, h - 9);
    ctx.textAlign = 'center'; ctx.fillText('BNDVI', w / 2, h - 9);
  }

  /** Repaint everything from the current frame and control values. */
  function render() {
    if (!state.nir) return;
    const v = computeBndvi();
    state.stats = computeStats(v);
    paintRaw();
    paintHeat(v);
    paintFalse(v);
    paintHistogram(v);
    paintChannels();
    paintStats();
    drawRoi();
  }

  function paintStats() {
    const s = state.stats;
    setText('live-mean', HC.fmt(s.mean));
    setText('live-healthy', s.h.toFixed(1) + '%');
    setText('live-moderate', s.m.toFixed(1) + '%');
    setText('live-stressed', s.s.toFixed(1) + '%');
    setText('live-std', s.std.toFixed(3));
    setText('live-range', HC.fmt(s.min) + ' … ' + HC.fmt(s.max));

    // The sanity note is the most valuable thing on this page: it tells you
    // whether the rig is actually behaving, in words.
    let nirMean = 0, blueMean = 0;
    const n = state.w * state.h;
    for (let i = 0; i < n; i++) { nirMean += state.nir[i]; blueMean += state.blue[i]; }
    nirMean /= n; blueMean /= n;

    let note;
    if (state.source === 'synthetic') {
      note = 'Synthetic frames — no camera attached. The white square top-left ' +
        'is a simulated reference card, so you can try the calibration below.';
    } else if (nirMean > 245 || blueMean > 245) {
      note = 'Channels are clipping at 255. A clipped channel makes BNDVI read ' +
        'falsely flat — lower the exposure or the gain.';
    } else if (nirMean < 25 && blueMean < 25) {
      note = 'The frame is nearly black. Raise the exposure or gain, and shoot ' +
        'in daylight — indoor LEDs emit almost no NIR.';
    } else if (s.mean > 0.25) {
      note = 'Raw frame reads pink over the plants — that is the NIR landing in ' +
        'the red channel, so the filter and white balance are behaving.';
    } else {
      note = 'Low mean. If the raw frame looks natural-coloured rather than ' +
        'pink, auto white balance is still on, or the gel has fallen out.';
    }
    setText('sanity-note', note);
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function setFeedLabel() {
    const el = HC.$('#feed-label');
    if (!el) return;
    el.textContent = !state.live ? 'FROZEN'
      : (state.source === 'synthetic' ? 'SYNTHETIC' : 'LIVE');
  }

  /* ── white-reference region selection ───────────────────────────────────── */

  function wireRoi() {
    const layer = HC.$('#roi-layer');
    if (!layer) return;
    let dragging = false;
    let start = null;

    function pos(e) {
      const r = layer.getBoundingClientRect();
      const p = e.touches ? e.touches[0] : e;
      return {
        x: Math.min(1, Math.max(0, (p.clientX - r.left) / r.width)),
        y: Math.min(1, Math.max(0, (p.clientY - r.top) / r.height))
      };
    }
    function begin(e) {
      dragging = true;
      start = pos(e);
      state.roi = { x0: start.x, y0: start.y, x1: start.x, y1: start.y };
      e.preventDefault();
    }
    function move(e) {
      if (!dragging) return;
      const p = pos(e);
      state.roi = { x0: start.x, y0: start.y, x1: p.x, y1: p.y };
      drawRoi();
      e.preventDefault();
    }
    function end() {
      if (!dragging) return;
      dragging = false;
      const r = state.roi;
      // A tap rather than a drag: clear the selection.
      if (Math.abs(r.x1 - r.x0) < 0.03 || Math.abs(r.y1 - r.y0) < 0.03) {
        state.roi = null;
      }
      drawRoi();
      updateSolveButton();
    }

    layer.addEventListener('mousedown', begin);
    layer.addEventListener('mousemove', move);
    window.addEventListener('mouseup', end);
    layer.addEventListener('touchstart', begin, { passive: false });
    layer.addEventListener('touchmove', move, { passive: false });
    layer.addEventListener('touchend', end);
  }

  function drawRoi() {
    const box = HC.$('#roi-box');
    if (!box) return;
    if (!state.roi) { box.hidden = true; return; }
    const r = state.roi;
    box.hidden = false;
    box.style.left = (Math.min(r.x0, r.x1) * 100) + '%';
    box.style.top = (Math.min(r.y0, r.y1) * 100) + '%';
    box.style.width = (Math.abs(r.x1 - r.x0) * 100) + '%';
    box.style.height = (Math.abs(r.y1 - r.y0) * 100) + '%';
  }

  function updateSolveButton() {
    const btn = HC.$('#solve-k');
    if (btn) btn.disabled = !state.roi;
  }

  /* ── controls ───────────────────────────────────────────────────────────── */

  function wireControls() {
    // NIR correction on/off
    HC.$$('[data-nir]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        state.correctNir = btn.dataset.nir === 'on';
        HC.$$('[data-nir]').forEach(function (b) {
          b.classList.toggle('is-on', (b.dataset.nir === 'on') === state.correctNir);
        });
        setText('nir-label', state.correctNir ? 'On' : 'Off');
        HC.saveSetting({ correct_nir_leakage: state.correctNir });
        render();
      });
    });

    HC.slider(HC.$('#k'), HC.$('#k-label'),
      function (v) { return v.toFixed(2); },
      function (v) { state.k = v; render(); HC.saveSetting({ nir_leak_coef: v }); });

    HC.slider(HC.$('#t-healthy'), HC.$('#t-healthy-label'),
      function (v) { return v.toFixed(2); },
      function (v) { state.tHealthy = v; render(); HC.saveSetting({ threshold_healthy: v }); });

    HC.slider(HC.$('#t-moderate'), HC.$('#t-moderate-label'),
      function (v) { return v.toFixed(2); },
      function (v) { state.tModerate = v; render(); HC.saveSetting({ threshold_moderate: v }); });

    // Exposure and gain go to the camera, so these need a round-trip. The
    // preview picks the change up on its next frame.
    HC.slider(HC.$('#exposure'), HC.$('#exposure-label'),
      function (v) { return Math.round(v) + ' µs'; },
      function (v) { HC.saveSetting({ exposure_us: Math.round(v) }); });

    HC.slider(HC.$('#gain'), HC.$('#gain-label'),
      function (v) { return v.toFixed(1) + '×'; },
      function (v) { HC.saveSetting({ gain: v }); });

    HC.slider(HC.$('#fps'), HC.$('#fps-label'),
      function (v) { return Math.round(v) + ' fps'; },
      function (v) {
        state.fps = Math.round(v);
        HC.saveSetting({ preview_fps: state.fps });
        startFrames();
      });

    HC.$$('[name="resolution"]').forEach(function (radio) {
      radio.addEventListener('change', function () {
        HC.saveSetting({ resolution: radio.value });
      });
    });

    HC.$$('[data-scene]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        HC.$$('[data-scene]').forEach(function (b) { b.classList.remove('is-on'); });
        btn.classList.add('is-on');
        HC.api('/api/preview/scene', {
          method: 'POST', body: { scene: btn.dataset.scene }
        }).catch(function (err) { HC.toast(err.message, true); });
      });
    });

    const liveBtn = HC.$('#toggle-live');
    if (liveBtn) {
      liveBtn.addEventListener('click', function () {
        state.live = !state.live;
        liveBtn.textContent = state.live ? 'Freeze frame' : 'Resume live';
        setFeedLabel();
        if (state.live) startFrames();
      });
    }

    const saveBtn = HC.$('#save-frame');
    if (saveBtn) {
      saveBtn.addEventListener('click', async function () {
        saveBtn.disabled = true;
        const original = saveBtn.textContent;
        saveBtn.textContent = 'Saving…';
        try {
          const rec = await HC.api('/api/captures', {
            method: 'POST',
            body: {
              from_preview: true,
              label: 'Debug frame',
              correct_nir_leakage: state.correctNir,
              nir_leak_coef: state.k
            }
          });
          saveBtn.textContent = 'Saved ✓';
          HC.toast('Saved as capture ' + rec.id + ' (mean ' +
            HC.fmt(rec.stats.mean) + ')');
        } catch (err) {
          HC.toast('Could not save: ' + err.message, true);
          saveBtn.textContent = original;
        } finally {
          saveBtn.disabled = false;
          setTimeout(function () { saveBtn.textContent = original; }, 2200);
        }
      });
    }

    const solveBtn = HC.$('#solve-k');
    if (solveBtn) {
      solveBtn.addEventListener('click', async function () {
        if (!state.roi) return;
        solveBtn.disabled = true;
        try {
          const res = await HC.api('/api/calibrate/solve-k', {
            method: 'POST',
            body: Object.assign({ apply: true }, state.roi)
          });
          setText('solve-result', res.message);
          if (res.k !== null) {
            state.k = res.k;
            state.correctNir = true;
            const input = HC.$('#k');
            if (input) { input.value = res.k; input.dispatchEvent(new Event('input')); }
            HC.$$('[data-nir]').forEach(function (b) {
              b.classList.toggle('is-on', b.dataset.nir === 'on');
            });
            setText('nir-label', 'On');
            render();
          }
        } catch (err) {
          setText('solve-result', err.message);
        } finally {
          solveBtn.disabled = false;
        }
      });
    }

    const retryBtn = HC.$('#retry-camera');
    if (retryBtn) {
      retryBtn.addEventListener('click', function () {
        retryBtn.disabled = true;
        HC.api('/api/camera/restart', { method: 'POST' })
          .then(function (probe) {
            HC.toast(probe.available ? 'Camera detected.'
              : 'Still not detected — ' + probe.detail, !probe.available);
            if (probe.available) location.reload();
          })
          .catch(function (err) { HC.toast(err.message, true); })
          .finally(function () { retryBtn.disabled = false; });
      });
    }

    const synthBtn = HC.$('#use-synthetic');
    if (synthBtn) {
      synthBtn.addEventListener('click', function () {
        HC.api('/api/camera/synthetic', { method: 'POST', body: { on: true } })
          .then(function () { location.reload(); })
          .catch(function (err) { HC.toast(err.message, true); });
      });
    }

    const resetBtn = HC.$('#reset-debug');
    if (resetBtn) {
      resetBtn.addEventListener('click', async function () {
        const ok = await HC.confirmDialog({
          title: 'Reset the camera settings?',
          body: 'Exposure, gain, the leakage coefficient and the thresholds all ' +
            'go back to their defaults. Saved captures keep the values they ' +
            'were taken with.',
          action: 'Reset'
        });
        if (!ok) return;
        await HC.api('/api/settings', {
          method: 'PATCH',
          body: {
            exposure_us: 5000, gain: 2.0, nir_leak_coef: 0.8,
            correct_nir_leakage: false, threshold_healthy: 0.3,
            threshold_moderate: 0.1, preview_fps: 12
          }
        });
        location.reload();
      });
    }

    updateSolveButton();
  }

  /* ── log viewer ─────────────────────────────────────────────────────────── */

  function pollLogs() {
    const view = HC.$('#log-view');
    if (!view) return;
    HC.poll(function () {
      return HC.api('/api/logs?limit=120').then(function (lines) {
        const atBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 24;
        view.innerHTML = lines.map(function (l) {
          return '<div class="log-line ' + l.level + '">' +
            '<span class="log-time">' + esc(l.time) + '</span>' +
            '<span class="log-level">' + esc(l.level) + '</span>' +
            '<span class="log-text">' + esc(l.text) + '</span></div>';
        }).join('');
        if (atBottom) view.scrollTop = view.scrollHeight;
      });
    }, 4000);
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  document.addEventListener('DOMContentLoaded', init);
}());
