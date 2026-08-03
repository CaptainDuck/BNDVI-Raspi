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

  /* minNativeZoom / maxNativeZoom matter here. Only the zoom levels in the
     manifest exist on disk, so without these Leaflet asks for a level we never
     downloaded and gets the blank fallback — the map looks broken when it isn't.
     Bounding the *native* range instead lets Leaflet scale the tiles we do have,
     so zooming past either end degrades to softer imagery rather than nothing. */
  const zooms = (coverage.zooms && coverage.zooms.length) ? coverage.zooms : [16, 19];
  L.tileLayer('/tiles/{z}/{x}/{y}.jpg', {
    minZoom: Math.min(12, zooms[0]),
    maxZoom: 21,
    minNativeZoom: zooms[0],
    maxNativeZoom: zooms[zooms.length - 1],
    attribution: coverage.attribution || 'Imagery © Esri'
  }).addTo(map);

  map.fitBounds(bounds, { padding: [18, 18] });

  // The flight/plot outline.
  L.rectangle(bounds, {
    color: '#f5ead8', weight: 2, dashArray: '6 5', fill: false
  }).addTo(map);

  /* How far the offline map extends. The user asked to see this: without it a
     blank patch is indistinguishable from ground that is genuinely bare. */
  if (coverage.has_tiles && (coverage.tile_bounds || coverage.bounds)) {
    const c = coverage.tile_bounds || coverage.bounds;
    L.rectangle([[c.south, c.west], [c.north, c.east]], {
      color: '#8c491a', weight: 1.5, dashArray: '2 6', fill: false,
      interactive: false
    }).addTo(map).bindTooltip(
      'Edge of the downloaded map — ' +
      (coverage.tile_area_ha ? coverage.tile_area_ha + ' ha' : '') +
      ' of imagery, ' + coverage.size_label +
      '. Beyond this line the map is blank.',
      { sticky: true });
  }

  /* ── the BNDVI overlay ───────────────────────────────────────────────────
     Rendered to an offscreen canvas at grid resolution and stretched over the
     bounds. Cells the drone never flew over stay transparent rather than being
     painted mid-range — inventing healthy ground is exactly the kind of thing a
     farmer would act on. */
  /** Nearest covered cell for every empty cell.
   *
   * Used for interpolation only. Without it, smooth upscaling blends each
   * covered cell's *alpha* toward its empty neighbours and a sparse flight
   * fades to nearly invisible over the imagery. Filling gives the interpolator
   * colour to work with; the alpha mask below then hides the filled cells
   * completely, so no unvisited ground is ever shown — which is the whole point
   * of keeping empty cells null in the first place.
   */
  function fillNearest(cells, cols, rows) {
    const filled = cells.slice();
    const known = [];
    for (let i = 0; i < cells.length; i++) {
      if (cells[i] !== null && cells[i] !== undefined) {
        known.push([i % cols, (i / cols) | 0, cells[i]]);
      }
    }
    if (!known.length) return filled;
    for (let i = 0; i < filled.length; i++) {
      if (filled[i] !== null && filled[i] !== undefined) continue;
      const x = i % cols, y = (i / cols) | 0;
      let best = Infinity, val = known[0][2];
      for (let k = 0; k < known.length; k++) {
        const dx = known[k][0] - x, dy = known[k][1] - y;
        const d = dx * dx + dy * dy;
        if (d < best) { best = d; val = known[k][2]; }
      }
      filled[i] = val;
    }
    return filled;
  }

  function gridCanvas() {
    const g = flight.grid;
    if (!g) return null;
    const th = flight.thresholds || { healthy: 0.3, moderate: 0.1 };
    const smooth = mode !== 'bands';
    const colours = smooth ? fillNearest(g.cells, g.cols, g.rows) : g.cells;

    const cv = document.createElement('canvas');
    cv.width = g.cols;
    cv.height = g.rows;
    const ctx = cv.getContext('2d');
    const img = ctx.createImageData(g.cols, g.rows);
    for (let i = 0; i < colours.length; i++) {
      const v = colours[i];
      if (v === null || v === undefined) { img.data[i * 4 + 3] = 0; continue; }
      const c = mode === 'bands'
        ? Colormap.band(v, th.healthy, th.moderate)
        : Colormap.cmap(v);
      img.data[i * 4] = c[0];
      img.data[i * 4 + 1] = c[1];
      img.data[i * 4 + 2] = c[2];
      img.data[i * 4 + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);

    const scale = 24;
    const big = document.createElement('canvas');
    big.width = g.cols * scale;
    big.height = g.rows * scale;
    const bctx = big.getContext('2d');

    // Smooth interpolation for the continuous view; hard edges for the
    // three-colour view, where a blurred boundary would misrepresent a threshold.
    bctx.imageSmoothingEnabled = smooth;
    bctx.imageSmoothingQuality = 'high';
    bctx.drawImage(cv, 0, 0, big.width, big.height);

    // Punch out everything the drone did not photograph. Drawn unsmoothed so
    // the coverage boundary stays crisp and honest.
    const mask = document.createElement('canvas');
    mask.width = g.cols;
    mask.height = g.rows;
    const mctx = mask.getContext('2d');
    const mimg = mctx.createImageData(g.cols, g.rows);
    for (let i = 0; i < g.cells.length; i++) {
      const covered = g.cells[i] !== null && g.cells[i] !== undefined;
      mimg.data[i * 4] = mimg.data[i * 4 + 1] = mimg.data[i * 4 + 2] = 255;
      mimg.data[i * 4 + 3] = covered ? 210 : 0;
    }
    mctx.putImageData(mimg, 0, 0);
    bctx.globalCompositeOperation = 'destination-in';
    bctx.imageSmoothingEnabled = false;
    bctx.drawImage(mask, 0, 0, big.width, big.height);

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
