/* Farm map.
 *
 * Four ways of looking at the same flight, because they answer different
 * questions:
 *
 *   Photos — every capture's false-colour image drawn at the patch of ground it
 *            actually covers, rotated to the heading the drone was flying. This
 *            is the real farm heatmap: full detail, and honest about coverage
 *            because ground nobody photographed simply stays bare imagery.
 *   Grid   — the same data averaged into 14x9 cells. Coarser, but stable and
 *            comparable between flights, and it's what the summary numbers and
 *            the hover readout are computed from.
 *   Bands  — the grid at your three thresholds, for "which rows do I walk".
 *   Off    — satellite imagery alone.
 *
 * Footprints come from simple trigonometry: at height h a lens with horizontal
 * angle of view a covers 2*h*tan(a/2) across. For the Pi Camera v2 (62.2 x 48.8
 * degrees) at 12 m that is about 14.5 x 10.9 m.
 *
 * Leaflet and its images are vendored; tiles come off the Pi's own disk. Nothing
 * here touches the network at run time.
 */
(function () {
  'use strict';

  const host = document.getElementById('plot-map');
  if (!host || !window.L) return;

  // `flight` is null before the first flight. The map still renders then, because
  // finding the farm on the imagery is the thing to do before flying.
  const flight = JSON.parse(host.dataset.flight || 'null');
  const coverage = JSON.parse(host.dataset.coverage);
  const vicinity = JSON.parse(host.dataset.vicinity);
  const survey = JSON.parse(host.dataset.survey || 'null') || {};
  const pins = JSON.parse(host.dataset.pins || '[]');

  let mode = 'photos';
  let overlay = null;
  let opacity = 0.82;
  let showPins = true;
  const mosaicCache = {};

  const M_PER_DEG_LAT = 111320;
  function mPerDegLon(lat) { return M_PER_DEG_LAT * Math.cos(lat * Math.PI / 180); }

  /* ── bounds ─────────────────────────────────────────────────────────────── */

  const b = flight && flight.bounds;
  const gridBounds = b
    ? [[b.south, b.west], [b.north, b.east]]
    : vicinityBounds();

  /** A square of `sideM` metres centred on lat/lon, as Leaflet bounds. */
  function squareBounds(lat, lon, sideM) {
    const half = sideM / 2;
    const dLat = half / M_PER_DEG_LAT;
    const dLon = half / mPerDegLon(lat);
    return [[lat - dLat, lon - dLon], [lat + dLat, lon + dLon]];
  }

  function vicinityBounds() {
    return squareBounds(vicinity.lat, vicinity.lon, vicinity.box_m);
  }

  /** Union of every photo's ground footprint — what the mosaic has to cover. */
  function photoBounds() {
    let s = 90, w = 180, n = -90, e = -180, any = false;
    pins.forEach(function (p) {
      if (!p.footprint) return;
      // rotation means the footprint's extent is the diagonal at worst, so pad
      // by the half-diagonal rather than the half-width
      const r = Math.sqrt(Math.pow(p.footprint.half_w_m, 2) +
                          Math.pow(p.footprint.half_h_m, 2));
      const dLat = r / M_PER_DEG_LAT;
      const dLon = r / mPerDegLon(p.lat);
      s = Math.min(s, p.lat - dLat); n = Math.max(n, p.lat + dLat);
      w = Math.min(w, p.lon - dLon); e = Math.max(e, p.lon + dLon);
      any = true;
    });
    return any ? [[s, w], [n, e]] : null;
  }

  const mosaicBounds = photoBounds();
  const hasFootprints = !!mosaicBounds;

  /* ── the map ────────────────────────────────────────────────────────────── */

  const map = L.map(host, {
    zoomSnap: 0,
    zoomDelta: 0.5,
    zoomControl: true,
    attributionControl: true,
    // Scroll to zoom, like every other web map. The old setting made the map
    // feel inert, and a farm map is something you want to move around in.
    scrollWheelZoom: true,
    // Two fingers to scroll past on a phone, so the page still scrolls.
    dragging: !L.Browser.mobile ? true : true,
    tap: true
  });

  /* Only the zoom levels in the manifest exist on disk, so bound the *native*
     range and let Leaflet scale what we have. Without this, zooming past either
     end asks for tiles we never downloaded and renders blank. */
  const zooms = (coverage.zooms && coverage.zooms.length) ? coverage.zooms : [16, 19];
  L.tileLayer('/tiles/{z}/{x}/{y}.jpg', {
    minZoom: Math.min(12, zooms[0]),
    maxZoom: 22,
    minNativeZoom: zooms[0],
    maxNativeZoom: zooms[zooms.length - 1],
    attribution: coverage.attribution || 'Imagery © Esri'
  }).addTo(map);

  /* Opening view. With a flight, frame the flight. Without one, frame the marked
     survey block if there is one, and otherwise the whole vicinity — which is
     the "I don't know where the farm is yet, show me everything" case. */
  const openingBounds = mosaicBounds || (flight ? gridBounds : (
    (survey.lat === null || survey.lat === undefined)
      ? vicinityBounds()
      : squareBounds(survey.lat, survey.lon, Math.max(survey.side_m * 4, 200))));
  map.fitBounds(openingBounds, { padding: [24, 24] });
  L.control.scale({ imperial: false, position: 'bottomright' }).addTo(map);

  // The flight extent. Only meaningful once something has been flown — before
  // that it would just be a box drawn around the middle of the vicinity.
  if (flight) {
    L.rectangle(gridBounds, {
      color: '#f5ead8', weight: 2, dashArray: '6 5', fill: false, interactive: false
    }).addTo(map);
  }

  // How far the offline imagery goes, so a blank patch is distinguishable from
  // ground that is genuinely bare.
  if (coverage.has_tiles && (coverage.tile_bounds || coverage.bounds)) {
    const c = coverage.tile_bounds || coverage.bounds;
    L.rectangle([[c.south, c.west], [c.north, c.east]], {
      color: '#8c491a', weight: 1.5, dashArray: '2 6', fill: false,
      interactive: false
    }).addTo(map).bindTooltip(
      'Edge of the downloaded map — ' +
      (coverage.tile_area_ha ? coverage.tile_area_ha + ' ha' : '') +
      ' of imagery, ' + coverage.size_label + '. This is the vicinity to search ' +
      'in, not the area you fly. Beyond this line the map is blank.',
      { sticky: true });
  }

  /* ── the survey block ───────────────────────────────────────────────────────
   *
   * The vicinity is tens of hectares; the drone flies one or two. The farm is
   * somewhere inside and its exact position isn't known, so the operator finds it
   * on the imagery and clicks. Everything the mission planner says — altitude,
   * photo spacing, flight time, battery count — is scaled to this block, so
   * getting it from a click on real imagery beats typing coordinates.
   */

  let blockLat = (survey.lat === null || survey.lat === undefined) ? null : survey.lat;
  let blockLon = (survey.lon === null || survey.lon === undefined) ? null : survey.lon;
  let blockSide = survey.side_m || 100;
  let blockRect = null;
  let picking = false;
  // What was saved, so Cancel can put it back without a page reload.
  const saved = { lat: blockLat, lon: blockLon, side: blockSide };

  const els = {
    start: document.getElementById('block-start'),
    edit: document.getElementById('block-edit'),
    size: document.getElementById('block-size'),
    sizeLabel: document.getElementById('block-size-label'),
    where: document.getElementById('block-where'),
    save: document.getElementById('block-save'),
    cancel: document.getElementById('block-cancel'),
    clear: document.getElementById('block-clear')
  };

  function drawBlock() {
    if (blockRect) { blockRect.remove(); blockRect = null; }
    if (blockLat === null) return;
    blockRect = L.rectangle(squareBounds(blockLat, blockLon, blockSide), {
      color: '#f0a03c', weight: 2.5, fillColor: '#f0a03c', fillOpacity: 0.12,
      interactive: false
    }).addTo(map);
    blockRect.bindTooltip(
      'The block you fly — ' + blockSide + ' m across, ' +
      (blockSide * blockSide / 10000).toFixed(2) + ' ha', { sticky: true });
  }

  function paintBlockPanel() {
    if (els.sizeLabel) els.sizeLabel.textContent = blockSide + ' m';
    if (els.where) {
      els.where.textContent = blockLat === null
        ? 'not placed yet'
        : blockLat.toFixed(5) + ', ' + blockLon.toFixed(5) +
          ' · ' + (blockSide * blockSide / 10000).toFixed(2) + ' ha';
    }
    if (els.save) {
      els.save.disabled = blockLat === null ||
        (blockLat === saved.lat && blockLon === saved.lon && blockSide === saved.side);
    }
    if (els.clear) els.clear.hidden = saved.lat === null;
  }

  function setPicking(on) {
    picking = on;
    if (els.edit) els.edit.hidden = !on;
    if (els.start) els.start.hidden = on;
    host.classList.toggle('is-picking', on);
    paintBlockPanel();
  }

  if (els.start) els.start.addEventListener('click', function () { setPicking(true); });

  if (els.cancel) {
    els.cancel.addEventListener('click', function () {
      blockLat = saved.lat; blockLon = saved.lon; blockSide = saved.side;
      if (els.size) els.size.value = blockSide;
      drawBlock();
      setPicking(false);
      note(IDLE_HINT);
    });
  }

  if (els.size) {
    els.size.addEventListener('input', function () {
      blockSide = parseInt(els.size.value, 10);
      drawBlock();
      paintBlockPanel();
    });
  }

  async function saveBlock(patch) {
    try {
      const res = await HC.api('/api/settings', { method: 'PATCH', body: patch });
      (res.warnings || []).forEach(function (w) { HC.toast(w, true); });
    } catch (err) {
      HC.toast('Could not save the block: ' + err.message, true);
      return false;
    }
    saved.lat = patch.survey_lat;
    saved.lon = patch.survey_lon;
    saved.side = patch.survey_side_m;
    return true;
  }

  if (els.save) {
    els.save.addEventListener('click', async function () {
      els.save.disabled = true;
      const ok = await saveBlock({
        survey_lat: blockLat, survey_lon: blockLon, survey_side_m: blockSide
      });
      if (!ok) { paintBlockPanel(); return; }
      setPicking(false);
      if (els.start) {
        els.start.textContent = 'Move the block you fly';
      }
      note('Block saved — ' + (blockSide * blockSide / 10000).toFixed(2) +
           ' ha. The mission planner on the New flight page now plans for it.');
    });
  }

  if (els.clear) {
    els.clear.addEventListener('click', async function () {
      const ok = await saveBlock({
        survey_lat: null, survey_lon: null, survey_side_m: blockSide
      });
      if (!ok) return;
      blockLat = blockLon = null;
      drawBlock();
      setPicking(false);
      if (els.start) els.start.textContent = 'Mark the block you fly';
      note('Block unmarked. The mission planner falls back to a placeholder size.');
    });
  }

  drawBlock();

  /* ── photo mosaic ───────────────────────────────────────────────────────── */

  /** Draw every capture's false-colour thumbnail at its own ground footprint.
   *
   *  Built into one canvas and added as a single image overlay: twenty separate
   *  overlays would each be a DOM node Leaflet has to reposition on every pan.
   *  Photos overlap (at 12 m the footprint is ~14 m across and the trigger fires
   *  every 5 m), and later ones simply draw over earlier ones — this is a
   *  telemetry-placed mosaic, not a blended orthomosaic, which is exactly what
   *  the thesis describes.
   */
  function buildMosaic() {
    return new Promise(function (resolve) {
      if (!hasFootprints) { resolve(null); return; }
      const [[south, west], [north, east]] = mosaicBounds;
      const midLat = (south + north) / 2;
      const spanLonM = (east - west) * mPerDegLon(midLat);
      const spanLatM = (north - south) * M_PER_DEG_LAT;

      // ~2000 px on the long edge: enough that a 14 m footprint gets a couple of
      // hundred pixels, without making a canvas the Pi's browser chokes on.
      const target = 2000;
      const pxPerM = target / Math.max(spanLonM, spanLatM);
      const cv = document.createElement('canvas');
      cv.width = Math.max(64, Math.round(spanLonM * pxPerM));
      cv.height = Math.max(64, Math.round(spanLatM * pxPerM));
      const ctx = cv.getContext('2d');

      const usable = pins.filter(function (p) { return p.footprint && p.thumb; });
      if (!usable.length) { resolve(null); return; }

      let pending = usable.length;
      usable.forEach(function (p) {
        const img = new Image();
        img.onload = function () {
          const x = (p.lon - west) * mPerDegLon(midLat) * pxPerM;
          const y = (north - p.lat) * M_PER_DEG_LAT * pxPerM;
          const w = p.footprint.half_w_m * 2 * pxPerM;
          const h = p.footprint.half_h_m * 2 * pxPerM;
          ctx.save();
          ctx.translate(x, y);
          // Heading is degrees clockwise from north, which is also the screen
          // rotation for a north-up map.
          ctx.rotate((p.heading || 0) * Math.PI / 180);
          ctx.drawImage(img, -w / 2, -h / 2, w, h);
          ctx.restore();
          if (--pending === 0) resolve(cv.toDataURL());
        };
        img.onerror = function () { if (--pending === 0) resolve(cv.toDataURL()); };
        img.src = p.thumb;
      });
    });
  }

  /* ── grid overlay ───────────────────────────────────────────────────────── */

  /** Nearest covered cell for every empty cell.
   *
   *  For interpolation only. Without it, smooth upscaling blends each covered
   *  cell's *alpha* toward its empty neighbours and a sparse flight fades to
   *  nearly invisible over the imagery. The alpha mask below then hides the
   *  filled cells completely, so no unvisited ground is ever shown.
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

  function gridCanvas(bands) {
    const g = flight && flight.grid;
    if (!g) return null;
    const th = flight.thresholds || { healthy: 0.3, moderate: 0.1 };
    const colours = bands ? g.cells : fillNearest(g.cells, g.cols, g.rows);

    const cv = document.createElement('canvas');
    cv.width = g.cols;
    cv.height = g.rows;
    const ctx = cv.getContext('2d');
    const img = ctx.createImageData(g.cols, g.rows);
    for (let i = 0; i < colours.length; i++) {
      const v = colours[i];
      if (v === null || v === undefined) { img.data[i * 4 + 3] = 0; continue; }
      const c = bands ? Colormap.band(v, th.healthy, th.moderate) : Colormap.cmap(v);
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
    bctx.imageSmoothingEnabled = !bands;
    bctx.imageSmoothingQuality = 'high';
    bctx.drawImage(cv, 0, 0, big.width, big.height);

    // Punch out everything the drone did not photograph. Unsmoothed, so the
    // coverage boundary stays crisp and honest.
    const mask = document.createElement('canvas');
    mask.width = g.cols;
    mask.height = g.rows;
    const mctx = mask.getContext('2d');
    const mimg = mctx.createImageData(g.cols, g.rows);
    for (let i = 0; i < g.cells.length; i++) {
      const covered = g.cells[i] !== null && g.cells[i] !== undefined;
      mimg.data[i * 4] = mimg.data[i * 4 + 1] = mimg.data[i * 4 + 2] = 255;
      mimg.data[i * 4 + 3] = covered ? 255 : 0;
    }
    mctx.putImageData(mimg, 0, 0);
    bctx.globalCompositeOperation = 'destination-in';
    bctx.imageSmoothingEnabled = false;
    bctx.drawImage(mask, 0, 0, big.width, big.height);
    return big.toDataURL();
  }

  /* ── overlay switching ──────────────────────────────────────────────────── */

  async function drawOverlay() {
    if (overlay) { overlay.remove(); overlay = null; }
    if (!flight || mode === 'none') return;

    let url = null, bounds = gridBounds;
    if (mode === 'photos') {
      if (!hasFootprints) {
        note('These photos have no altitude recorded, so the ground each one ' +
             'covers is unknown. Showing the averaged grid instead.');
        mode = 'smooth';
        syncModeButtons();
      } else {
        if (!mosaicCache.url) mosaicCache.url = await buildMosaic();
        url = mosaicCache.url;
        bounds = mosaicBounds;
      }
    }
    if (mode === 'smooth' || mode === 'bands') url = gridCanvas(mode === 'bands');
    if (!url) return;

    overlay = L.imageOverlay(url, bounds, {
      opacity: opacity, interactive: false,
      className: mode === 'photos' ? 'mosaic-layer' : 'grid-layer'
    }).addTo(map);
  }

  /* ── photo pins and flight track ────────────────────────────────────────── */

  const pinLayer = L.layerGroup();
  const trackLayer = L.layerGroup();

  function buildPins() {
    const COLOURS = {
      healthy: '#2f8f3e', moderate: '#e0a020', stressed: '#c1442e'
    };
    const track = [];
    pins.forEach(function (p) {
      track.push([p.lat, p.lon]);
      const marker = L.circleMarker([p.lat, p.lon], {
        radius: 6, weight: 2, color: '#f5ead8', fillOpacity: 0.95,
        fillColor: COLOURS[p.classification] || '#82796a'
      });
      const label = Colormap.BAND_LABELS[p.classification] || 'no reading';
      marker.bindTooltip(
        '<strong>' + p.time + '</strong> · ' + label +
        (p.mean === null ? '' : '<br>BNDVI ' + HC.fmt(p.mean)) +
        (p.footprint ? '<br>covers ' + p.footprint.width_m + ' × ' +
          p.footprint.height_m + ' m from ' + p.alt + ' m up' : '') +
        (p.gsd_cm ? '<br>' + p.gsd_cm + ' cm per pixel' : '') +
        '<br><em>click to open</em>',
        { direction: 'top', offset: [0, -6] });
      marker.on('click', function () {
        window.location.href = '/capture/' + p.id;
      });
      // Show where this photo actually looked, on hover.
      let ghost = null;
      marker.on('mouseover', function () {
        if (!p.footprint) return;
        ghost = L.rectangle(footprintBounds(p), {
          color: '#f5ead8', weight: 1.5, dashArray: '3 3', fill: false,
          interactive: false
        }).addTo(map);
      });
      marker.on('mouseout', function () {
        if (ghost) { ghost.remove(); ghost = null; }
      });
      pinLayer.addLayer(marker);
    });

    if (track.length > 1) {
      L.polyline(track, {
        color: '#f5ead8', weight: 2, opacity: 0.55, dashArray: '5 6',
        interactive: false
      }).addTo(trackLayer);
    }
  }

  function footprintBounds(p) {
    const r = Math.sqrt(Math.pow(p.footprint.half_w_m, 2) +
                        Math.pow(p.footprint.half_h_m, 2));
    const dLat = r / M_PER_DEG_LAT;
    const dLon = r / mPerDegLon(p.lat);
    return [[p.lat - dLat, p.lon - dLon], [p.lat + dLat, p.lon + dLon]];
  }

  function syncPins() {
    if (showPins && pins.length) {
      trackLayer.addTo(map);
      pinLayer.addTo(map);
    } else {
      map.removeLayer(pinLayer);
      map.removeLayer(trackLayer);
    }
  }

  /* ── hover readout ──────────────────────────────────────────────────────── */

  const hint = document.getElementById('map-hint');
  const IDLE_HINT = pins.length
    ? 'Move over the field to check one spot. Click a photo pin to open it.'
    : (flight
      ? 'Move over the field to check one spot. Scroll to zoom, drag to pan.'
      : 'Scroll to zoom, drag to pan. The dashed line is as far as the offline ' +
        'imagery goes.');

  function note(text) { if (hint) hint.textContent = text; }

  function readCell(latlng) {
    const g = flight && flight.grid;
    if (!g) return null;
    const fx = (latlng.lng - gridBounds[0][1]) /
               (gridBounds[1][1] - gridBounds[0][1]);
    const fy = 1 - (latlng.lat - gridBounds[0][0]) /
               (gridBounds[1][0] - gridBounds[0][0]);
    if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return null;
    const col = Math.min(g.cols - 1, Math.floor(fx * g.cols));
    const row = Math.min(g.rows - 1, Math.floor(fy * g.rows));
    return { row: row, col: col, value: g.cells[row * g.cols + col] };
  }

  /** Closest photo, so the readout can name the actual capture under the cursor
   *  rather than only a grid cell. */
  function nearestPin(latlng) {
    let best = null, bestD = Infinity;
    pins.forEach(function (p) {
      const dLat = (p.lat - latlng.lat) * M_PER_DEG_LAT;
      const dLon = (p.lon - latlng.lng) * mPerDegLon(p.lat);
      const d = Math.sqrt(dLat * dLat + dLon * dLon);
      if (d < bestD) { bestD = d; best = p; }
    });
    return best && bestD < 40 ? { pin: best, metres: bestD } : null;
  }

  function describe(latlng) {
    const near = nearestPin(latlng);
    const cell = readCell(latlng);
    const parts = [];

    if (cell) {
      const where = 'Row ' + (cell.row + 1) + ', column ' + (cell.col + 1);
      if (cell.value === null || cell.value === undefined) {
        parts.push(where + ' — no photo covered this spot');
      } else {
        const th = (flight && flight.thresholds) || { healthy: 0.3, moderate: 0.1 };
        const name = Colormap.bandName(cell.value, th.healthy, th.moderate);
        parts.push(where + ' — ' + Colormap.BAND_LABELS[name].toLowerCase() +
          ' · BNDVI ' + HC.fmt(cell.value));
      }
    }
    if (near) {
      parts.push('nearest photo ' + near.pin.time + ' (' +
        Math.round(near.metres) + ' m away)');
    }
    return parts.length ? parts.join('  ·  ') : IDLE_HINT;
  }

  map.on('mousemove', function (e) {
    if (picking) return;                 // the picker owns the hint while it's up
    note(describe(e.latlng));
  });
  map.on('mouseout', function () { if (!picking) note(IDLE_HINT); });
  map.on('click', function (e) {
    if (picking) {
      blockLat = e.latlng.lat;
      blockLon = e.latlng.lng;
      drawBlock();
      paintBlockPanel();
      note('Block centred here. Adjust the size, then Save.');
      return;
    }
    note(describe(e.latlng));
  });

  /* ── controls ───────────────────────────────────────────────────────────── */

  function syncModeButtons() {
    HC.$$('.map-mode').forEach(function (b2) {
      b2.classList.toggle('is-active', b2.dataset.mode === mode);
    });
  }

  HC.$$('.map-mode').forEach(function (btn) {
    btn.addEventListener('click', function () {
      mode = btn.dataset.mode;
      syncModeButtons();
      note(mode === 'photos'
        ? 'Each photo drawn where it was taken, at the ground it actually covers.'
        : (mode === 'smooth' ? 'Averaged into a grid — coarser, but comparable between flights.'
          : (mode === 'bands' ? 'Three colours at your thresholds.'
            : 'Satellite imagery only.')));
      drawOverlay();
    });
  });

  const fade = document.getElementById('map-opacity');
  if (fade) {
    fade.addEventListener('input', function () {
      opacity = parseFloat(fade.value);
      if (overlay) overlay.setOpacity(opacity);
    });
  }

  const pinToggle = document.getElementById('map-show-pins');
  if (pinToggle) {
    pinToggle.addEventListener('change', function () {
      showPins = pinToggle.checked;
      syncPins();
    });
    pinToggle.closest('.map-toggle').hidden = pins.length === 0;
  }

  /* ── go ─────────────────────────────────────────────────────────────────── */

  buildPins();
  syncPins();
  drawOverlay();

  if (flight && !hasFootprints && pins.length) {
    note('Photos have no altitude recorded, so only the averaged grid is ' +
         'available. Fly with the flight controller connected to get footprints.');
  }

  // Leaflet mis-measures a container laid out after init, and again for print.
  setTimeout(function () { map.invalidateSize(); }, 80);
  window.addEventListener('beforeprint', function () { map.invalidateSize(); });
}());
