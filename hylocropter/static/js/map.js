/* Farm map: Leaflet + a BNDVI raster overlay built from the flight grid.
 *
 * Leaflet and its marker images are vendored under static/vendor, and tiles are
 * served from /tiles/{z}/{x}/{y}.jpg off the Pi's own disk. Nothing here touches
 * the network at run time.
 */
(function () {
  'use strict';

  const host = document.getElementById('plot-map');
  if (!host || !window.L) return;

  const flight = JSON.parse(host.dataset.flight);
  const coverage = JSON.parse(host.dataset.coverage);
  const plot = JSON.parse(host.dataset.plot);

  let mode = 'smooth';
  let overlay = null;

  /* Flight bounds when the captures had GPS; otherwise fall back to the
     configured plot box, so the map still shows the farm. */
  const b = flight.bounds;
  const bounds = b
    ? [[b.south, b.west], [b.north, b.east]]
    : plotBounds();

  function plotBounds() {
    const half = plot.box_m / 2;
    const dLat = half / 111320;
    const dLon = half / (111320 * Math.cos(plot.lat * Math.PI / 180));
    return [[plot.lat - dLat, plot.lon - dLon], [plot.lat + dLat, plot.lon + dLon]];
  }

  const map = L.map(host, {
    zoomSnap: 0, zoomDelta: 0.5, zoomControl: true,
    attributionControl: true, scrollWheelZoom: false
  });

  L.tileLayer('/tiles/{z}/{x}/{y}.jpg', {
    minZoom: 14,
    maxZoom: 19,
    attribution: coverage.attribution || 'Imagery © Esri'
  }).addTo(map);

  map.fitBounds(bounds, { padding: [18, 18] });

  // The flight/plot outline.
  L.rectangle(bounds, {
    color: '#f5ead8', weight: 2, dashArray: '6 5', fill: false
  }).addTo(map);

  /* How far the offline map extends. The user asked to see this: without it a
     blank patch is indistinguishable from ground that is genuinely bare. */
  if (coverage.has_tiles && coverage.bounds) {
    const c = coverage.bounds;
    L.rectangle([[c.south, c.west], [c.north, c.east]], {
      color: '#8c491a', weight: 1.5, dashArray: '2 6', fill: false,
      interactive: false
    }).addTo(map).bindTooltip(
      'Edge of the downloaded map — ' + (coverage.extent_label || '') +
      ', ' + coverage.size_label,
      { sticky: true });
  }

  /* ── the BNDVI overlay ───────────────────────────────────────────────────
     Rendered to an offscreen canvas at grid resolution and stretched over the
     bounds. Cells the drone never flew over stay transparent rather than being
     painted mid-range — inventing healthy ground is exactly the kind of thing a
     farmer would act on. */
  function gridCanvas() {
    const g = flight.grid;
    if (!g) return null;
    const cv = document.createElement('canvas');
    cv.width = g.cols;
    cv.height = g.rows;
    const ctx = cv.getContext('2d');
    const img = ctx.createImageData(g.cols, g.rows);
    const th = flight.thresholds || { healthy: 0.3, moderate: 0.1 };
    for (let i = 0; i < g.cells.length; i++) {
      const v = g.cells[i];
      if (v === null || v === undefined) {
        img.data[i * 4 + 3] = 0;
        continue;
      }
      const c = mode === 'bands'
        ? Colormap.band(v, th.healthy, th.moderate)
        : Colormap.cmap(v);
      img.data[i * 4] = c[0];
      img.data[i * 4 + 1] = c[1];
      img.data[i * 4 + 2] = c[2];
      img.data[i * 4 + 3] = 205;
    }
    ctx.putImageData(img, 0, 0);

    // Upscale so Leaflet gets a reasonably sized image. Smooth interpolation
    // for the continuous view; hard edges for the three-colour view, where a
    // blurred boundary would misrepresent a threshold.
    const big = document.createElement('canvas');
    big.width = g.cols * 24;
    big.height = g.rows * 24;
    const bctx = big.getContext('2d');
    bctx.imageSmoothingEnabled = mode !== 'bands';
    bctx.imageSmoothingQuality = 'high';
    bctx.drawImage(cv, 0, 0, big.width, big.height);
    return big.toDataURL();
  }

  function drawOverlay() {
    const url = gridCanvas();
    if (!url) return;
    if (overlay) overlay.remove();
    overlay = L.imageOverlay(url, bounds, { opacity: 0.82, interactive: false })
      .addTo(map);
  }
  drawOverlay();

  /* ── hover readout ───────────────────────────────────────────────────────── */

  const hint = document.getElementById('map-hint');
  const IDLE_HINT = 'Move over the field to check one spot. Pinch or scroll to zoom.';

  function readCell(latlng) {
    const g = flight.grid;
    if (!g) return null;
    const fx = (latlng.lng - bounds[0][1]) / (bounds[1][1] - bounds[0][1]);
    const fy = 1 - (latlng.lat - bounds[0][0]) / (bounds[1][0] - bounds[0][0]);
    if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return null;
    const col = Math.min(g.cols - 1, Math.floor(fx * g.cols));
    const row = Math.min(g.rows - 1, Math.floor(fy * g.rows));
    return { row: row, col: col, value: g.cells[row * g.cols + col] };
  }

  function describe(cell) {
    if (!cell) return IDLE_HINT;
    const where = 'Row ' + (cell.row + 1) + ', column ' + (cell.col + 1);
    if (cell.value === null || cell.value === undefined) {
      return where + ' — no photo covered this spot.';
    }
    const th = flight.thresholds || { healthy: 0.3, moderate: 0.1 };
    const name = Colormap.bandName(cell.value, th.healthy, th.moderate);
    return where + ' — ' + Colormap.BAND_LABELS[name].toLowerCase() +
      ' · BNDVI ' + HC.fmt(cell.value);
  }

  map.on('mousemove', function (e) {
    if (hint) hint.textContent = describe(readCell(e.latlng));
  });
  map.on('mouseout', function () { if (hint) hint.textContent = IDLE_HINT; });
  // Touch has no hover, so a tap reads the cell instead.
  map.on('click', function (e) {
    if (hint) hint.textContent = describe(readCell(e.latlng));
  });

  /* ── overlay mode switch ─────────────────────────────────────────────────── */

  HC.$$('.map-mode').forEach(function (btn) {
    btn.addEventListener('click', function () {
      mode = btn.dataset.mode;
      HC.$$('.map-mode').forEach(function (b) {
        b.classList.toggle('is-active', b === btn);
      });
      drawOverlay();
    });
  });

  // Leaflet mis-measures a container that was laid out after init (the card
  // animates in), so nudge it once the layout has settled.
  setTimeout(function () { map.invalidateSize(); }, 80);
  window.addEventListener('beforeprint', function () { map.invalidateSize(); });
}());
