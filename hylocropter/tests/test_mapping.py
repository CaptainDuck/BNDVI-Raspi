"""Turning geotagged photos into a farm stress map.

This is the chain the thesis calls for: each photo has a GPS position and an
altitude, the altitude gives it a ground footprint, the positions give the flight
a bounding box, and the per-photo means bin into a grid that gets painted over
satellite imagery.

Two properties are worth more than all the others and are tested hardest:

  * **Unvisited ground stays unknown.** An empty grid cell must stay None. A
    mid-range colour on ground the drone never overflew is invented data a farmer
    could act on.
  * **North is up.** Row 0 is the northern edge. Getting that backwards flips the
    map vertically and sends someone to the wrong end of the field.
"""

import math

import pytest

import flights


def capture(lat=None, lon=None, mean=0.4, rel_alt=12.0, heading=0.0,
            stats=True, cid="c1"):
    """A capture record shaped like the real ones."""
    geo = None
    if lat is not None:
        geo = {"lat": lat, "lon": lon, "rel_alt_m": rel_alt,
               "heading_deg": heading, "fix_type": 3, "satellites": 12}
    return {
        "id": cid,
        "geo": geo,
        "stats": ({"mean": mean, "min": mean - 0.2, "max": mean + 0.2,
                   "healthy_pct": 60.0, "moderate_pct": 30.0,
                   "stressed_pct": 10.0} if stats else None),
        "settings": {"resolution": [3280, 2464]},
    }


# ── one photo's patch of ground ──────────────────────────────────────────────

def test_footprint_matches_the_lens_trigonometry():
    """2*h*tan(fov/2). At 12 m the Pi Camera v2 covers about 14.5 x 10.9 m."""
    fp = flights.footprint({"lat": 14.1, "lon": 121.0, "rel_alt_m": 12.0})
    assert fp["width_m"] == pytest.approx(
        2 * 12.0 * math.tan(math.radians(62.2 / 2)), abs=0.01)
    assert fp["height_m"] == pytest.approx(
        2 * 12.0 * math.tan(math.radians(48.8 / 2)), abs=0.01)
    assert fp["width_m"] == pytest.approx(14.48, abs=0.01)
    assert fp["height_m"] == pytest.approx(10.89, abs=0.01)


def test_footprint_scales_linearly_with_height():
    a = flights.footprint({"lat": 0, "lon": 0, "rel_alt_m": 10.0})
    b = flights.footprint({"lat": 0, "lon": 0, "rel_alt_m": 20.0})
    assert b["width_m"] == pytest.approx(a["width_m"] * 2, abs=0.01)


def test_footprint_is_wider_than_it_is_tall():
    """The wide axis sits across the flight track, which is what makes the line
    spacing the bigger of the two numbers in the mission plan."""
    fp = flights.footprint({"lat": 0, "lon": 0, "rel_alt_m": 15.0})
    assert fp["width_m"] > fp["height_m"]


@pytest.mark.parametrize("geo", [
    None,
    {"lat": 14.1, "lon": 121.0},                      # no altitude at all
    {"lat": 14.1, "lon": 121.0, "rel_alt_m": None},
    {"lat": 14.1, "lon": 121.0, "rel_alt_m": 0.0},
    {"lat": 14.1, "lon": 121.0, "rel_alt_m": -3.0},
])
def test_footprint_refuses_to_guess_without_a_height(geo):
    """No altitude means the footprint is unknowable. Guessing one would put
    invented ground on the map, so the map falls back to the grid instead."""
    assert flights.footprint(geo) is None


def test_footprint_carries_the_heading_through():
    fp = flights.footprint({"lat": 0, "lon": 0, "rel_alt_m": 12.0,
                            "heading_deg": 274.0})
    assert fp["heading_deg"] == 274.0


def test_ground_sampling_distance():
    """The honest resolution limit: 14.48 m across 3280 px is 0.44 cm/px."""
    gsd = flights.ground_sampling_distance_cm(
        {"lat": 0, "lon": 0, "rel_alt_m": 12.0}, (3280, 2464))
    assert gsd == pytest.approx(0.44, abs=0.01)


def test_gsd_is_none_without_altitude_or_resolution():
    assert flights.ground_sampling_distance_cm({"lat": 0, "lon": 0}, (3280, 2464)) is None
    assert flights.ground_sampling_distance_cm(
        {"lat": 0, "lon": 0, "rel_alt_m": 12.0}, None) is None


# ── the flight's extent ──────────────────────────────────────────────────────

def test_bounds_are_none_without_any_fix():
    assert flights.compute_bounds([capture(), capture()]) is None
    assert flights.compute_bounds([]) is None


def test_bounds_enclose_every_geotagged_capture():
    caps = [capture(14.100, 121.000), capture(14.110, 121.020)]
    b = flights.compute_bounds(caps)
    assert b["south"] < 14.100 and b["north"] > 14.110
    assert b["west"] < 121.000 and b["east"] > 121.020


def test_bounds_ignore_captures_with_no_fix():
    mixed = [capture(14.100, 121.000), capture(None), capture(14.101, 121.001)]
    b = flights.compute_bounds(mixed)
    assert b is not None
    assert b["north"] < 14.11        # the None did not widen anything


def test_a_single_capture_still_gets_a_drawable_box():
    """A degenerate box has nowhere to draw the overlay, so it gets opened out."""
    b = flights.compute_bounds([capture(14.1265, 121.0768)])
    assert b["north"] > b["south"]
    assert b["east"] > b["west"]
    height_m = (b["north"] - b["south"]) * 111_320
    assert height_m > 10


# ── binning into the grid ────────────────────────────────────────────────────

def test_unvisited_cells_stay_none():
    """The rule that keeps the map honest."""
    caps = [capture(14.1000, 121.0000, mean=0.5)]
    bounds = flights.compute_bounds(caps)
    grid = flights.build_grid(caps, bounds, 0.3, 0.1)
    assert grid["covered"] == 1
    assert grid["cells"].count(None) == grid["cols"] * grid["rows"] - 1


def test_grid_is_the_expected_shape():
    grid = flights.build_grid([], None, 0.3, 0.1)
    assert grid["cols"] == flights.GRID_COLS
    assert grid["rows"] == flights.GRID_ROWS
    assert len(grid["cells"]) == flights.GRID_COLS * flights.GRID_ROWS
    assert grid["covered"] == 0
    assert set(grid["cells"]) == {None}


def test_row_zero_is_the_northern_edge():
    """North is up. If this inverts, the map is flipped and the walk-the-red-rows
    advice sends someone to the wrong end of the field."""
    bounds = {"south": 14.00, "north": 14.02, "west": 121.00, "east": 121.02}
    north = capture(14.0199, 121.010, mean=0.8, cid="n")
    south = capture(14.0001, 121.010, mean=-0.5, cid="s")
    grid = flights.build_grid([north, south], bounds, 0.3, 0.1)
    cols = grid["cols"]
    top = [c for c in grid["cells"][:cols] if c is not None]
    bottom = [c for c in grid["cells"][-cols:] if c is not None]
    assert top == [pytest.approx(0.8)]
    assert bottom == [pytest.approx(-0.5)]


def test_west_is_the_left_edge():
    bounds = {"south": 14.00, "north": 14.02, "west": 121.00, "east": 121.02}
    west = capture(14.010, 121.0001, mean=0.7, cid="w")
    grid = flights.build_grid([west], bounds, 0.3, 0.1)
    idx = grid["cells"].index(pytest.approx(0.7))
    assert idx % grid["cols"] == 0


def test_several_photos_in_one_cell_are_averaged():
    bounds = {"south": 14.00, "north": 14.02, "west": 121.00, "east": 121.02}
    caps = [capture(14.0100, 121.0100, mean=0.2, cid="a"),
            capture(14.0101, 121.0101, mean=0.6, cid="b")]
    grid = flights.build_grid(caps, bounds, 0.3, 0.1)
    values = [c for c in grid["cells"] if c is not None]
    assert values == [pytest.approx(0.4)]
    assert grid["covered"] == 1


def test_captures_outside_the_bounds_are_dropped_not_clamped():
    """Clamping would smear a stray fix onto the edge of the field."""
    bounds = {"south": 14.00, "north": 14.02, "west": 121.00, "east": 121.02}
    grid = flights.build_grid([capture(15.0, 122.0, mean=0.9)], bounds, 0.3, 0.1)
    assert grid["covered"] == 0


def test_captures_without_stats_are_skipped():
    bounds = {"south": 14.00, "north": 14.02, "west": 121.00, "east": 121.02}
    grid = flights.build_grid([capture(14.01, 121.01, stats=False)],
                              bounds, 0.3, 0.1)
    assert grid["covered"] == 0


def test_a_degenerate_bounds_box_yields_an_empty_grid():
    bounds = {"south": 14.0, "north": 14.0, "west": 121.0, "east": 121.0}
    grid = flights.build_grid([capture(14.0, 121.0)], bounds, 0.3, 0.1)
    assert grid["covered"] == 0


# ── flight-level statistics ──────────────────────────────────────────────────

def test_aggregate_stats_average_the_captures():
    caps = [capture(mean=0.2, cid="a"), capture(mean=0.6, cid="b")]
    s = flights.aggregate_stats(caps)
    assert s["mean"] == pytest.approx(0.4)
    assert s["min"] == pytest.approx(0.0)      # 0.2 - 0.2
    assert s["max"] == pytest.approx(0.8)      # 0.6 + 0.2
    assert s["std"] == pytest.approx(0.2)


def test_aggregate_stats_survive_an_empty_flight():
    s = flights.aggregate_stats([])
    assert s["mean"] == 0.0 and s["std"] == 0.0


def test_aggregate_stats_ignore_failed_captures():
    caps = [capture(mean=0.4, cid="a"), capture(stats=False, cid="b")]
    assert flights.aggregate_stats(caps)["mean"] == pytest.approx(0.4)


# ── the whole mapping chain, end to end ──────────────────────────────────────

def test_a_lawnmower_flight_maps_stress_to_the_right_corner():
    """The process the user actually cares about: fly a grid over a field whose
    eastern half is stressed, and the map has to come out stressed in the east,
    healthy in the west, and unknown where the drone never went."""
    caps = []
    lat0, lon0 = 14.1200, 121.0700
    step = 0.00018                       # ~20 m
    for row in range(6):
        for col in range(6):
            lat = lat0 + row * step
            lon = lon0 + col * step
            stressed = col >= 3
            caps.append(capture(lat, lon, mean=(-0.1 if stressed else 0.55),
                                cid=f"r{row}c{col}"))

    bounds = flights.compute_bounds(caps)
    grid = flights.build_grid(caps, bounds, 0.3, 0.1)
    stats = flights.aggregate_stats(caps)

    assert grid["covered"] > 20
    assert grid["cells"].count(None) > 0, "a 6x6 flight cannot fill a 14x9 grid"

    cols = grid["cols"]
    west, east = [], []
    for i, v in enumerate(grid["cells"]):
        if v is None:
            continue
        (west if (i % cols) < cols / 2 else east).append(v)
    assert sum(west) / len(west) > 0.3, "the healthy half must read healthy"
    assert sum(east) / len(east) < 0.1, "the stressed half must read stressed"

    # and the flight-level verdict is the average of the two
    import bndvi
    assert bndvi.classify(stats["mean"]) == "moderate"


def test_a_flight_with_no_gps_still_produces_stats_but_no_map():
    """A flight flown with the flight controller unplugged: the numbers are real,
    the placement is not, and the UI has to be able to say so."""
    caps = [capture(mean=0.45, cid="a"), capture(mean=0.35, cid="b")]
    bounds = flights.compute_bounds(caps)
    grid = flights.build_grid(caps, bounds, 0.3, 0.1)
    stats = flights.aggregate_stats(caps)
    assert bounds is None
    assert grid["covered"] == 0
    assert stats["mean"] == pytest.approx(0.4)
    assert "no GPS" in flights.summarise(stats["mean"], 10, has_gps=False)["plain"]


# ── mission planning ────────────────────────────────────────────────────────

def test_plan_footprint_agrees_with_the_footprint_helper():
    """One geometry, two callers. They must not drift apart."""
    plan = flights.mission_plan(altitude_m=12)
    fp = flights.footprint({"lat": 0, "lon": 0, "rel_alt_m": 12.0})
    assert plan["footprint_w_m"] == pytest.approx(fp["width_m"], abs=0.01)
    assert plan["footprint_h_m"] == pytest.approx(fp["height_m"], abs=0.01)


def test_trigger_distance_is_the_along_track_footprint_less_overlap():
    plan = flights.mission_plan(altitude_m=12, forward_overlap=0.40)
    assert plan["trigger_distance_m"] == pytest.approx(
        plan["footprint_h_m"] * 0.60, abs=0.06)


def test_line_spacing_is_the_across_track_footprint_less_overlap():
    plan = flights.mission_plan(altitude_m=12, side_overlap=0.30)
    assert plan["line_spacing_m"] == pytest.approx(
        plan["footprint_w_m"] * 0.70, abs=0.06)


def test_the_two_numbers_mission_planner_needs_at_twelve_metres():
    """Regression pin on the pair that gets typed into Mission Planner."""
    plan = flights.mission_plan(altitude_m=12, forward_overlap=0.40,
                               side_overlap=0.30, plot_side_m=100)
    assert plan["trigger_distance_m"] == pytest.approx(6.5, abs=0.1)
    assert plan["line_spacing_m"] == pytest.approx(10.1, abs=0.1)
    assert plan["plot_area_ha"] == pytest.approx(1.0, abs=0.01)


def test_more_overlap_means_more_photos():
    low = flights.mission_plan(12, forward_overlap=0.2, side_overlap=0.2)
    high = flights.mission_plan(12, forward_overlap=0.7, side_overlap=0.7)
    assert high["photos"] > low["photos"]
    assert high["trigger_distance_m"] < low["trigger_distance_m"]


def test_flying_higher_needs_fewer_photos():
    assert (flights.mission_plan(30, plot_side_m=200)["photos"]
            < flights.mission_plan(10, plot_side_m=200)["photos"])


def test_a_bigger_block_needs_more_photos_and_more_time():
    small = flights.mission_plan(12, plot_side_m=100)
    big = flights.mission_plan(12, plot_side_m=300)
    assert big["photos"] > small["photos"]
    assert big["minutes"] > small["minutes"]
    assert big["storage_mb"] > small["storage_mb"]


def test_a_one_hectare_block_fits_inside_one_battery():
    """The reason the survey block is separate from the downloaded vicinity: a
    hectare is a short flight, the whole 38 ha vicinity is not."""
    plan = flights.mission_plan(12, plot_side_m=100)
    assert plan["minutes"] < flights.USABLE_FLIGHT_MINUTES
    assert not plan["warnings"]


def test_planning_the_whole_vicinity_warns_about_the_battery():
    plan = flights.mission_plan(12, plot_side_m=620)
    assert plan["minutes"] > flights.USABLE_FLIGHT_MINUTES
    assert any("minutes of flying" in w for w in plan["warnings"])


def test_plan_warns_when_photos_come_faster_than_the_camera_can_save():
    plan = flights.mission_plan(5, forward_overlap=0.85)
    assert any("faster than the camera" in w for w in plan["warnings"])


@pytest.mark.parametrize("altitude,fragment", [(3, "Below about 5 m"),
                                               (80, "Above 60 m")])
def test_plan_warns_at_silly_altitudes(altitude, fragment):
    plan = flights.mission_plan(altitude, plot_side_m=100)
    assert any(fragment in w for w in plan["warnings"])


def test_plan_clamps_nonsense_inputs_rather_than_crashing():
    """These come off sliders and a number field, so they have to be safe."""
    plan = flights.mission_plan(0, forward_overlap=-1, side_overlap=5,
                               plot_side_m=0)
    assert plan["altitude_m"] >= 1
    assert 0 <= plan["forward_overlap_pct"] <= 90
    assert 0 <= plan["side_overlap_pct"] <= 90
    assert plan["plot_side_m"] >= 10
    assert plan["photos"] >= 1


def test_plan_reports_gsd_from_the_capture_resolution():
    hi = flights.mission_plan(12, resolution=(3280, 2464))
    lo = flights.mission_plan(12, resolution=(640, 480))
    assert hi["gsd_cm"] < lo["gsd_cm"]
    assert hi["gsd_cm"] == pytest.approx(0.44, abs=0.01)


# ── plain-language summary ───────────────────────────────────────────────────

@pytest.mark.parametrize("mean,fragment", [
    (0.55, "look healthy"),
    (0.30, "a few spots"),
    (0.05, "need attention"),
])
def test_summary_voice_follows_the_mean(mean, fragment):
    assert fragment in flights.summarise(mean, 12)["headline"]


def test_summary_always_offers_something_to_do():
    for mean in (0.6, 0.3, 0.0, -0.5):
        s = flights.summarise(mean, 20)
        assert s["advice"] and s["plain"] and s["headline"]
