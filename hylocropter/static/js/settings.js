/* Settings: bound inputs, device actions, the offline-map downloader. */
(function () {
  'use strict';

  const root = document.getElementById('settings-root');
  if (!root) return;
  const coverage = JSON.parse(root.dataset.coverage);

  /* ── bound inputs ───────────────────────────────────────────────────────
     Every control carrying data-setting writes itself back. One handler beats
     twenty near-identical ones. */
  HC.$$('[data-setting]').forEach(function (el) {
    const key = el.dataset.setting;
    const event = (el.type === 'range') ? 'input' : 'change';
    el.addEventListener(event, function () {
      let value;
      if (el.type === 'checkbox') value = el.checked;
      else if (el.type === 'radio') { if (!el.checked) return; value = el.value; }
      else if (el.type === 'number' || el.type === 'range') value = parseFloat(el.value);
      else value = el.value;
      const patch = {};
      patch[key] = value;
      HC.saveSetting(patch, function () {
        if (event === 'change') HC.toast('Saved.');
      });
    });
  });

  HC.slider(HC.$('#s-t-healthy'), HC.$('#th-label'),
    function (v) { return v.toFixed(2); });
  HC.slider(HC.$('#s-t-moderate'), HC.$('#tm-label'),
    function (v) { return v.toFixed(2); });

  /* ── device actions ─────────────────────────────────────────────────────── */

  const resultEl = document.getElementById('action-result');

  function showResult(message, isError) {
    if (!resultEl) return;
    resultEl.hidden = false;
    resultEl.className = 'toast' + (isError ? ' is-error' : '');
    resultEl.textContent = message;
  }

  HC.$$('[data-action]').forEach(function (btn) {
    btn.addEventListener('click', async function () {
      const action = btn.dataset.action;
      btn.disabled = true;
      try {
        // Destructive actions come back with the dialog copy rather than acting,
        // so the confirmation text is written once, server-side.
        let res = await HC.api('/api/system/actions/' + action, {
          method: 'POST', body: {}
        }).catch(function (err) {
          if (err.status === 409 && err.data && err.data.confirm) return err.data;
          throw err;
        });

        if (res && res.confirm) {
          const ok = await HC.confirmDialog(res.confirm);
          if (!ok) { btn.disabled = false; return; }
          res = await HC.api('/api/system/actions/' + action, {
            method: 'POST', body: { confirm: true }
          });
        }
        showResult(res.message, !res.ok);
        if (action === 'free-space') setTimeout(function () { location.reload(); }, 1800);
      } catch (err) {
        showResult(err.message, true);
      } finally {
        btn.disabled = false;
      }
    });
  });

  /* ── activity log ───────────────────────────────────────────────────────── */

  const activityEl = document.getElementById('activity-list');
  if (activityEl) {
    HC.poll(function () {
      return HC.api('/api/logs?limit=12&activity=true').then(function (lines) {
        if (!lines.length) {
          activityEl.innerHTML = '<div class="text-muted" style="font-size:13px">' +
            'Nothing yet this session.</div>';
          return;
        }
        activityEl.innerHTML = lines.reverse().map(function (l) {
          return '<div class="activity-row"><span class="activity-time">' +
            esc(l.time) + '</span><span>' + esc(l.text) + '</span></div>';
        }).join('');
      });
    }, 6000);
  }

  /* ── offline map ────────────────────────────────────────────────────────── */

  const planEl = document.getElementById('plan-summary');
  const latEl = HC.$('#s-lat'), lonEl = HC.$('#s-lon');
  const boxEl = HC.$('#s-box'), zmaxEl = HC.$('#s-zmax');

  const refreshPlan = HC.debounce(function () {
    const params = new URLSearchParams({
      lat: latEl.value, lon: lonEl.value,
      box_m: boxEl.value, zoom_max: zmaxEl.value
    });
    HC.api('/api/tiles/plan?' + params).then(function (plan) {
      if (!planEl) return;
      planEl.textContent = plan.tiles + ' tiles · about ' +
        HC.bytes(plan.est_bytes) + ' · ' + plan.area_ha + ' ha · ' +
        plan.ground_resolution_m + ' m per pixel at zoom ' +
        plan.zooms[plan.zooms.length - 1];
      if (plan.too_large) {
        planEl.textContent += ' — too large, reduce the area or the zoom';
      }
      const btn = HC.$('#tile-download');
      if (btn) btn.disabled = plan.too_large;
    }).catch(function () {});
  }, 300);

  [latEl, lonEl, boxEl, zmaxEl].forEach(function (el) {
    if (el) el.addEventListener('input', refreshPlan);
  });

  const track = document.getElementById('tile-progress-track');
  const fill = document.getElementById('tile-progress-fill');
  const statusEl = document.getElementById('tile-status');
  const cancelBtn = document.getElementById('tile-cancel');
  let progressPoll = null;

  function watchProgress() {
    if (progressPoll) progressPoll.stop();
    progressPoll = HC.poll(function () {
      return HC.api('/api/tiles/progress').then(function (p) {
        if (track) track.hidden = !p.running;
        if (cancelBtn) cancelBtn.hidden = !p.running;
        if (fill) fill.style.width = (p.percent || 0) + '%';
        if (statusEl) {
          statusEl.textContent = p.running
            ? p.done + ' of ' + p.total + ' tiles' +
              (p.failed ? ' · ' + p.failed + ' failed' : '')
            : (p.message || '');
          statusEl.style.color = (!p.running && p.ok === false)
            ? 'var(--stress-stressed)' : '';
        }
        if (!p.running) {
          if (p.ok) setTimeout(function () { location.reload(); }, 1200);
          return false;
        }
        return true;
      });
    }, 600, { runHidden: true });
  }

  const dlBtn = document.getElementById('tile-download');
  if (dlBtn) {
    dlBtn.addEventListener('click', async function () {
      dlBtn.disabled = true;
      if (statusEl) statusEl.textContent = 'Starting…';
      try {
        const res = await HC.api('/api/tiles/download', {
          method: 'POST',
          body: {
            plot_lat: parseFloat(latEl.value),
            plot_lon: parseFloat(lonEl.value),
            plot_box_m: parseInt(boxEl.value, 10),
            tile_zoom_max: parseInt(zmaxEl.value, 10)
          }
        });
        if (statusEl) statusEl.textContent = res.message;
        watchProgress();
      } catch (err) {
        if (statusEl) {
          statusEl.textContent = err.message;
          statusEl.style.color = 'var(--stress-stressed)';
        }
      } finally {
        dlBtn.disabled = false;
      }
    });
  }

  if (cancelBtn) {
    cancelBtn.addEventListener('click', function () {
      HC.api('/api/tiles/cancel', { method: 'POST' });
    });
  }

  // Pick up a download already running when the page loaded.
  HC.api('/api/tiles/progress').then(function (p) {
    if (p.running) watchProgress();
  }).catch(function () {});

  /* ── coverage inset map ─────────────────────────────────────────────────
     Answers "how far is the map downloaded?" visually: the solid rectangle is
     what is on disk, the dashed one is the plot. */
  const mapHost = document.getElementById('coverage-map');
  if (mapHost && window.L && (coverage.tile_bounds || coverage.bounds)) {
    // Fit to what is genuinely on disk. Fitting the requested box instead would
    // hide the margin the tile grid gives you for free.
    const c = coverage.tile_bounds || coverage.bounds;
    const box = [[c.south, c.west], [c.north, c.east]];
    const map = L.map(mapHost, {
      zoomControl: false, attributionControl: false,
      scrollWheelZoom: false, dragging: false, doubleClickZoom: false
    });
    // Same reason as the farm map: bound the *native* zoom range to what is
    // actually on disk, or this inset asks for a level we never downloaded and
    // renders as blank ground.
    const zooms = (coverage.zooms && coverage.zooms.length) ? coverage.zooms : [16, 19];
    L.tileLayer('/tiles/{z}/{x}/{y}.jpg', {
      minZoom: Math.min(12, zooms[0]), maxZoom: 21,
      minNativeZoom: zooms[0], maxNativeZoom: zooms[zooms.length - 1]
    }).addTo(map);
    map.fitBounds(box, { padding: [10, 10] });
    L.rectangle(box, { color: '#8c491a', weight: 2, fill: false }).addTo(map);

    const settings = JSON.parse(root.dataset.settings);
    const half = settings.plot_box_m / 2;
    const dLat = half / 111320;
    const dLon = half / (111320 * Math.cos(settings.plot_lat * Math.PI / 180));
    L.rectangle([
      [settings.plot_lat - dLat, settings.plot_lon - dLon],
      [settings.plot_lat + dLat, settings.plot_lon + dLon]
    ], { color: '#f5ead8', weight: 2, dashArray: '5 5', fill: false }).addTo(map);
    setTimeout(function () { map.invalidateSize(); }, 80);
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
  }
}());
