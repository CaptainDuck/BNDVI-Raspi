/* BNDVI colour mapping.
 *
 * MIRROR OF bndvi.py — BNDVI_COLOR_STOPS and BAND_COLORS. If you change the
 * stops in one place, change them in the other, or the browser and the saved
 * heatmap PNG will disagree about what a value looks like.
 *
 * The old dashboard had three different mappings for the same data: this
 * gradient, matplotlib's RdYlGn, and bndvi_to_rgb()'s own stops. Now there is
 * one definition, and matplotlib is handed a colormap built from it.
 */
(function (global) {
  'use strict';

  const STOPS = [
    [-1.00, [90, 24, 18]],
    [-0.20, [193, 68, 46]],
    [0.15, [224, 160, 32]],
    [0.35, [242, 227, 74]],
    [0.60, [87, 168, 63]],
    [1.00, [24, 107, 43]]
  ];

  const BANDS = {
    healthy: [47, 143, 62],
    moderate: [224, 160, 32],
    stressed: [193, 68, 46]
  };

  function lerp(a, b, t) { return a + (b - a) * t; }

  /** Smooth colormap: BNDVI in [-1, 1] to [r, g, b]. */
  function cmap(v) {
    if (v <= STOPS[0][0]) return STOPS[0][1].slice();
    for (let i = 0; i < STOPS.length - 1; i++) {
      const [v0, c0] = STOPS[i];
      const [v1, c1] = STOPS[i + 1];
      if (v < v1) {
        const t = (v - v0) / (v1 - v0);
        return [lerp(c0[0], c1[0], t), lerp(c0[1], c1[1], t), lerp(c0[2], c1[2], t)];
      }
    }
    return STOPS[STOPS.length - 1][1].slice();
  }

  /** Flat three-colour banding at the given thresholds. */
  function band(v, tHealthy, tModerate) {
    if (v > tHealthy) return BANDS.healthy;
    if (v >= tModerate) return BANDS.moderate;
    return BANDS.stressed;
  }

  function bandName(v, tHealthy, tModerate) {
    if (v > tHealthy) return 'healthy';
    if (v >= tModerate) return 'moderate';
    return 'stressed';
  }

  /* Plain-language band labels. The mockup deliberately avoids the words
     "healthy/moderate/stressed" in the farmer-facing UI. */
  const BAND_LABELS = {
    healthy: 'Doing well',
    moderate: 'Keep an eye',
    stressed: 'Needs a look',
    failed: 'Photo failed'
  };

  function rgbCss(c) {
    return 'rgb(' + (c[0] | 0) + ',' + (c[1] | 0) + ',' + (c[2] | 0) + ')';
  }

  /** Pre-computed 512-entry lookup table, for painting whole frames fast. */
  function buildLut(size) {
    size = size || 512;
    const lut = new Uint8Array(size * 3);
    for (let i = 0; i < size; i++) {
      const c = cmap((i / (size - 1)) * 2 - 1);
      lut[i * 3] = c[0];
      lut[i * 3 + 1] = c[1];
      lut[i * 3 + 2] = c[2];
    }
    return lut;
  }

  global.Colormap = {
    STOPS: STOPS, BANDS: BANDS, BAND_LABELS: BAND_LABELS,
    cmap: cmap, band: band, bandName: bandName, rgbCss: rgbCss,
    buildLut: buildLut
  };
}(window));
