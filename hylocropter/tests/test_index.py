"""The plant-health maths: BNDVI itself, the bands, and the leakage correction.

These are the numbers the whole project rests on, and most of them are only
checkable *here* — a wrong channel mapping still produces a plausible-looking
false-colour image, and a wrong threshold still produces percentages that sum to
100. The archived `ndvi_capture.old.py` had the channels reversed and nothing in
the UI would have told you.
"""

import numpy as np
import pytest

import bndvi


def frame(nir, green, blue, shape=(4, 4)):
    """A uniform RGB frame with the given per-channel levels.

    Channel order is the sensor's, and behind the Rosco #2007 gel it means:
    index 0 = NIR, index 1 = green (mostly blocked), index 2 = blue + leakage.
    """
    a = np.zeros((shape[0], shape[1], 3), np.uint8)
    a[:, :, 0] = nir
    a[:, :, 1] = green
    a[:, :, 2] = blue
    return a


# ── the channel mapping ──────────────────────────────────────────────────────

def test_healthy_vegetation_reads_positive():
    """The load-bearing invariant. The gel blocks red, so NIR lands in the RED
    channel; healthy foliage is NIR-bright and blue-dark, hence R > B and a
    positive index. If this ever fails, the old (B-R)/(B+R) bug is back."""
    b = bndvi.compute_bndvi(frame(nir=200, green=30, blue=50))
    assert b.mean() > 0
    assert bndvi.classify(float(b.mean())) == "healthy"


def test_bare_soil_reads_near_zero_or_negative():
    """Soil reflects NIR and visible more evenly, so the index collapses."""
    b = bndvi.compute_bndvi(frame(nir=90, green=40, blue=85))
    assert -0.2 < b.mean() < 0.1


def test_index_matches_the_closed_form():
    b = bndvi.compute_bndvi(frame(nir=200, green=0, blue=50))
    assert b.mean() == pytest.approx((200 - 50) / (200 + 50), abs=1e-6)


def test_green_channel_is_ignored_by_the_index():
    """Green is measured and shown, but the index must not depend on it."""
    dark = bndvi.compute_bndvi(frame(nir=180, green=0, blue=60))
    bright = bndvi.compute_bndvi(frame(nir=180, green=255, blue=60))
    assert dark.mean() == pytest.approx(bright.mean(), abs=1e-7)


def test_black_frame_gives_zero_not_nan():
    b = bndvi.compute_bndvi(frame(0, 0, 0))
    assert np.all(np.isfinite(b))
    assert b.mean() == 0.0


def test_output_is_float32_in_range():
    b = bndvi.compute_bndvi(bndvi.synthetic_frame((64, 48), scene="mixed"))
    assert b.dtype == np.float32
    assert b.min() >= -1.0 and b.max() <= 1.0


# ── NIR leakage correction ───────────────────────────────────────────────────

def test_leak_correction_raises_the_index():
    """Removing NIR from the blue channel makes visible blue smaller, so the
    index goes up. It never goes down."""
    raw = bndvi.compute_bndvi(frame(200, 0, 120)).mean()
    fixed = bndvi.compute_bndvi(frame(200, 0, 120),
                                correct_nir_leakage=True,
                                nir_leak_coef=0.35).mean()
    assert fixed > raw


def test_leak_correction_cannot_blow_up_or_flip_sign():
    """On dense vegetation k*R can exceed B outright. The clamp has to hold the
    result inside [-1, 1] rather than dividing by something near zero."""
    b = bndvi.compute_bndvi(frame(250, 0, 10), correct_nir_leakage=True,
                            nir_leak_coef=1.5)
    assert np.all(np.isfinite(b))
    assert b.max() <= 1.0
    assert b.min() > 0            # over-correction reads *more* healthy, not less


def test_zero_k_is_the_uncorrected_index():
    a = bndvi.compute_bndvi(frame(180, 0, 90))
    b = bndvi.compute_bndvi(frame(180, 0, 90), correct_nir_leakage=True,
                            nir_leak_coef=0.0)
    assert a.mean() == pytest.approx(b.mean(), abs=1e-6)


# ── solving k from a white reference ─────────────────────────────────────────

def test_solve_leak_coef_is_the_white_reference_identity():
    """k = B/R - 1, from requiring BNDVI == 0 on a white target."""
    k, msg = bndvi.solve_leak_coef(nir_mean=100.0, blue_mean=135.0)
    assert k == pytest.approx(0.35, abs=1e-3)
    assert "0.35" in msg


def test_solved_k_actually_zeroes_the_white_reference():
    """The round trip that matters: solve k off a white patch, feed it back in,
    and the same patch must read ~0. Anything else and the Debug view's
    drag-a-box calibration is decorative."""
    nir, blue = 120.0, 162.0
    k, _ = bndvi.solve_leak_coef(nir, blue)
    b = bndvi.compute_bndvi(frame(int(nir), 0, int(blue)),
                            correct_nir_leakage=True, nir_leak_coef=k)
    assert abs(b.mean()) < 0.01


def test_solve_refuses_when_no_positive_k_can_work():
    """Blue already below NIR means white reads positive, and subtracting more
    blue only makes it worse. The honest answer is "lower the exposure"."""
    k, msg = bndvi.solve_leak_coef(nir_mean=150.0, blue_mean=120.0)
    assert k is None
    assert "exposure" in msg.lower()


def test_solve_refuses_a_black_reference():
    k, msg = bndvi.solve_leak_coef(nir_mean=0.0, blue_mean=40.0)
    assert k is None
    assert "black" in msg.lower()


def test_solve_flags_an_implausibly_large_k():
    k, msg = bndvi.solve_leak_coef(nir_mean=50.0, blue_mean=130.0)
    assert k == pytest.approx(1.6, abs=1e-3)
    assert "unusually high" in msg


def test_default_k_is_not_silently_treated_as_calibrated():
    """0.8 is Horning's plugin default for a *red* narrowband filter, not a value
    for this rig (RESEARCH-GAPS.md section 2). Correction is therefore off by
    default -- this test exists so nobody quietly flips it on."""
    assert bndvi.DEFAULT_NIR_LEAK_COEF == 0.8
    import settings
    assert settings.DEFAULTS["correct_nir_leakage"] is False


# ── bands and statistics ─────────────────────────────────────────────────────

def test_percentages_sum_to_one_hundred():
    b = bndvi.compute_bndvi(bndvi.synthetic_frame((80, 60), scene="mixed"))
    s = bndvi.bndvi_stats(b)
    assert (s["healthy_pct"] + s["moderate_pct"] + s["stressed_pct"]
            == pytest.approx(100.0, abs=1e-4))


def test_bands_have_no_gap_or_overlap_at_the_thresholds():
    """A value sitting exactly on a threshold must land in exactly one band.
    The comparisons are >healthy / >=moderate&<=healthy / <moderate, so the
    boundaries belong to the moderate band."""
    values = np.array([[0.1, 0.3]], np.float32)      # both thresholds exactly
    s = bndvi.bndvi_stats(values, threshold_healthy=0.3, threshold_moderate=0.1)
    assert s["moderate_pct"] == pytest.approx(100.0)
    assert s["healthy_pct"] == 0.0
    assert s["stressed_pct"] == 0.0


def test_classify_agrees_with_the_band_percentages():
    for value, expected in [(0.5, "healthy"), (0.3, "moderate"),
                            (0.2, "moderate"), (0.1, "moderate"),
                            (0.09, "stressed"), (-0.4, "stressed")]:
        assert bndvi.classify(value) == expected
        s = bndvi.bndvi_stats(np.array([[value]], np.float32))
        assert s[expected + "_pct"] == pytest.approx(100.0)


def test_custom_thresholds_move_the_bands():
    assert bndvi.classify(0.25, threshold_healthy=0.2) == "healthy"
    assert bndvi.classify(0.25, threshold_healthy=0.4,
                          threshold_moderate=0.3) == "stressed"


def test_stats_report_the_distribution_not_just_the_mean():
    b = np.array([[-1.0, 1.0]], np.float32)
    s = bndvi.bndvi_stats(b)
    assert s["min"] == -1.0 and s["max"] == 1.0
    assert s["mean"] == pytest.approx(0.0)
    assert s["std"] == pytest.approx(1.0)


# ── the synthetic scenes, as a pipeline check ────────────────────────────────

@pytest.mark.parametrize("scene", ["healthy", "mixed", "stressed", "soil"])
def test_every_scene_produces_a_usable_frame(scene):
    a = bndvi.synthetic_frame((160, 120), scene=scene)
    assert a.shape == (120, 160, 3)
    assert a.dtype == np.uint8


def test_scenes_rank_in_the_order_they_claim():
    """Dev mode is first-class, so the four scenes have to actually differ in
    plant health -- otherwise nothing built against them means anything."""
    means = {}
    for scene in ("healthy", "mixed", "stressed", "soil"):
        # no white card: it is a deliberate BNDVI ~ 0 patch and would drag the
        # scene means toward each other
        nir, _green, blue = bndvi.synthetic_field(scene, white_ref=False)
        a = np.stack([nir, np.zeros_like(nir), blue], -1).astype(np.uint8)
        means[scene] = float(bndvi.compute_bndvi(a).mean())
    assert means["healthy"] > means["mixed"] > means["stressed"]
    assert means["stressed"] > means["soil"]


def test_the_synthetic_white_card_calibrates_to_the_modelled_leak():
    """End-to-end calibration with no camera: measure the synthetic white card,
    solve k, and get back roughly the leak the generator modelled."""
    nir, _green, blue = bndvi.synthetic_field("mixed")
    x0, y0, x1, y1 = bndvi.SYNTH_WHITE_BOX
    k, _ = bndvi.solve_leak_coef(float(nir[y0:y1, x0:x1].mean()),
                                 float(blue[y0:y1, x0:x1].mean()))
    assert k == pytest.approx(bndvi.SYNTH_LEAK, abs=0.02)


def test_scenes_are_reproducible_for_a_given_seed():
    a = bndvi.synthetic_field("mixed", seed=7)[0]
    b = bndvi.synthetic_field("mixed", seed=7)[0]
    assert np.array_equal(a, b)


def test_jitter_actually_moves_the_scene():
    """The old generator was seeded at 42 with static geometry, so every dev
    capture produced identical stats and the trend chart was a flat line."""
    a = bndvi.synthetic_field("mixed", jitter=0.0)[0]
    b = bndvi.synthetic_field("mixed", jitter=1.0)[0]
    assert not np.array_equal(a, b)


# ── rig diagnostics ──────────────────────────────────────────────────────────

def test_filter_sanity_accepts_a_gel_fitted_rig():
    """The synthetic scene models the gel, so it must read as fitted."""
    nir, green, blue = bndvi.synthetic_field("healthy", white_ref=False)
    verdict, _ = bndvi.filter_sanity(float(nir.mean()), float(green.mean()),
                                     float(blue.mean()))
    assert verdict == "fitted"


def test_filter_sanity_catches_a_missing_gel():
    """Without the gel, green is the brightest channel on foliage."""
    verdict, msg = bndvi.filter_sanity(nir_mean=90, green_mean=140,
                                       blue_mean=70)
    assert verdict == "missing"
    assert "lens cap" in msg or "unfiltered" in msg


def test_filter_sanity_catches_green_creeping_up_on_blue():
    verdict, _ = bndvi.filter_sanity(nir_mean=180, green_mean=95, blue_mean=100)
    assert verdict == "missing"


def test_filter_sanity_will_not_guess_in_the_dark():
    verdict, msg = bndvi.filter_sanity(3, 2, 4)
    assert verdict == "uncertain"
    assert "dark" in msg.lower()


@pytest.mark.parametrize("nir_max,blue_max,expected", [
    (255, 200, "clipped"),
    (200, 254, "clipped"),
    (80, 70, "dark"),
    (150, 140, "dim"),
    (210, 30, "unbalanced"),
    (200, 190, "good"),
])
def test_white_reference_check_verdicts(nir_max, blue_max, expected):
    verdict, msg = bndvi.white_reference_check(nir_max, blue_max)
    assert verdict == expected
    assert msg


def test_exposure_warning_flags_clipping():
    a = frame(255, 255, 255, shape=(8, 8))
    w = bndvi.exposure_warning(a)
    assert w["level"] == "warn"
    assert "clipped" in w["text"]


def test_exposure_warning_flags_a_dark_frame():
    w = bndvi.exposure_warning(frame(5, 5, 5, shape=(8, 8)))
    assert w["level"] == "warn"
    assert "black" in w["text"]


def test_exposure_warning_passes_a_normal_frame():
    a = bndvi.synthetic_frame((64, 48), scene="mixed")
    assert bndvi.exposure_warning(a)["level"] == "ok"


def test_channel_means_reports_what_the_debug_view_shows():
    m = bndvi.channel_means(frame(200, 40, 90))
    assert m["nir"] == pytest.approx(200)
    assert m["green"] == pytest.approx(40)
    assert m["blue"] == pytest.approx(90)
    assert m["nir_max"] == 200 and m["blue_max"] == 90


# ── locked camera controls ───────────────────────────────────────────────────

def test_locked_controls_disable_the_automatics():
    """Auto white balance re-gains R and B independently, which cancels the very
    NIR-vs-blue difference BNDVI measures. Auto exposure makes two captures an
    hour apart incomparable."""
    c = bndvi.locked_controls()
    assert c["AwbEnable"] is False
    assert c["AeEnable"] is False
    assert c["ColourGains"] == bndvi.DEFAULT_COLOUR_GAINS
    assert c["ExposureTime"] == bndvi.DEFAULT_EXPOSURE_US
    assert c["AnalogueGain"] == bndvi.DEFAULT_GAIN
    assert c["Sharpness"] == 0


def test_verify_controls_notices_the_camera_ignoring_us():
    """picamera2 issues #1269 and #825 both mean a lock can be silently
    ineffective, so every frame's metadata gets checked against the request."""
    wanted = bndvi.locked_controls(gain=2.0, exposure_us=5000)
    honest = bndvi.verify_controls(wanted, {"ExposureTime": 5000,
                                            "AnalogueGain": 2.0,
                                            "ColourGains": (1.0, 1.0)})
    assert not honest
    lying = bndvi.verify_controls(wanted, {"ExposureTime": 20000,
                                           "AnalogueGain": 2.0,
                                           "ColourGains": (1.0, 1.0)})
    assert lying
    assert any("ExposureTime" in m for m in lying)


# ── the one colormap ─────────────────────────────────────────────────────────

def test_colour_stops_are_ordered_and_span_the_range():
    values = [v for v, _ in bndvi.BNDVI_COLOR_STOPS]
    assert values == sorted(values)
    assert values[0] == -1.0 and values[-1] == 1.0


def test_colormap_runs_red_to_green():
    low = bndvi.bndvi_to_rgb(np.array([[-0.9]], np.float32))[0, 0]
    high = bndvi.bndvi_to_rgb(np.array([[0.9]], np.float32))[0, 0]
    assert low[0] > low[1]           # stressed: red beats green
    assert high[1] > high[0]         # healthy: green beats red


def test_band_render_uses_the_thresholds_it_is_given():
    values = np.array([[0.5, 0.2, -0.3]], np.float32)
    out = bndvi.bndvi_to_bands_rgb(values, 0.3, 0.1)
    assert tuple(out[0, 0]) == bndvi.BAND_COLORS["healthy"]
    assert tuple(out[0, 1]) == bndvi.BAND_COLORS["moderate"]
    assert tuple(out[0, 2]) == bndvi.BAND_COLORS["stressed"]


def test_python_and_javascript_colormaps_still_agree():
    """There used to be three mappings for the same data. `colormap.js` is a
    hand-kept mirror of BNDVI_COLOR_STOPS, and a mirror nobody checks drifts."""
    import re
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "static" / "js" / "colormap.js").read_text()

    def stops_from(block):
        return [(float(m[0]), (int(m[1]), int(m[2]), int(m[3])))
                for m in re.findall(
                    r"\[\s*(-?[\d.]+)\s*,\s*\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)",
                    block)]

    stops_block = js.split("const STOPS")[1].split("];")[0]
    assert stops_from(stops_block) == list(bndvi.BNDVI_COLOR_STOPS)

    bands_block = js.split("const BANDS")[1].split("};")[0]
    js_bands = {m[0]: (int(m[1]), int(m[2]), int(m[3])) for m in re.findall(
        r"(\w+)\s*:\s*\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", bands_block)}
    assert js_bands == bndvi.BAND_COLORS
