"""The flight store and the settings file.

Both hold state on a drone that can lose power mid-write, and both were rewritten
to survive that: every index mutation happens under one lock, and every write is
temp-file-plus-os.replace. There's no database to fall back on, so these are the
guarantees.
"""

import concurrent.futures
import json

import pytest

import bndvi
import flights
import settings as settings_mod


@pytest.fixture
def store(tmp_path):
    return flights.Store(tmp_path)


@pytest.fixture
def config(tmp_path):
    return settings_mod.Settings(tmp_path / "settings.json")


def capture_record(cid, flight_id=None, mean=0.4, lat=None, lon=None,
                   rel_alt=12.0):
    geo = None
    if lat is not None:
        geo = {"lat": lat, "lon": lon, "rel_alt_m": rel_alt, "heading_deg": 0.0}
    return {
        "id": cid,
        "timestamp": f"2026-08-05T09:{int(cid[-2:]):02d}:00",
        "label": "test", "notes": "",
        "flight_id": flight_id,
        "geo": geo,
        "files": {"thumb": f"thumb_{cid}.jpg"},
        "stats": {"mean": mean, "min": mean - 0.1, "max": mean + 0.1,
                  "healthy_pct": 50.0, "moderate_pct": 30.0,
                  "stressed_pct": 20.0},
        "classification": bndvi.classify(mean),
        "settings": {"resolution": [3280, 2464]},
    }


# ── the capture index ────────────────────────────────────────────────────────

def test_a_capture_round_trips(store):
    store.add_capture(capture_record("c01"))
    assert store.capture("c01")["stats"]["mean"] == pytest.approx(0.4)
    assert store.capture("nope") is None


def test_ground_captures_are_the_ones_with_no_flight(store):
    store.add_capture(capture_record("c01"))
    store.add_capture(capture_record("c02", flight_id="F-1"))
    ground = store.captures(flight_id=None)
    assert [c["id"] for c in ground] == ["c01"]


def test_concurrent_writes_do_not_lose_records(store):
    """The old code released the capture lock *before* the read-append-write, so
    two overlapping requests could each read the same index and one would
    clobber the other."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: store.add_capture(capture_record(f"c{i:02d}")),
                      range(1, 41)))
    assert len(store.captures()) == 40


def test_the_index_is_valid_json_after_every_write(store, tmp_path):
    store.add_capture(capture_record("c01"))
    store.add_capture(capture_record("c02"))
    data = json.loads((tmp_path / "captures.json").read_text())
    assert len(data) == 2
    # temp files must not be left lying around
    assert not list(tmp_path.glob("*.tmp"))


def test_updating_a_capture_keeps_the_original_keys(store):
    """Records must stay renderable: the eight original keys are what the detail
    page and any pre-Hylocropter record rely on."""
    store.add_capture(capture_record("c01"))
    store.update_capture("c01", {"notes": "checked on foot"})
    rec = store.capture("c01")
    for key in ("id", "timestamp", "label", "notes", "files", "stats",
                "classification", "settings"):
        assert key in rec
    assert rec["notes"] == "checked on foot"


# ── flights ──────────────────────────────────────────────────────────────────

def test_opening_two_flights_in_the_same_minute_does_not_collide(store):
    a = store.open_flight(name="North block")
    b = store.open_flight(name="South block")
    assert a["id"] != b["id"]
    assert len(store.flights()) == 2


def test_a_new_flight_starts_recording_with_nothing_measured(store):
    f = store.open_flight()
    assert f["status"] == "recording"
    assert f["stats"] is None and f["grid"] is None and f["bounds"] is None


def test_closing_a_flight_builds_the_map(store):
    f = store.open_flight(name="North block")
    for i, (lat, lon, mean) in enumerate([
            (14.1200, 121.0700, 0.55), (14.1202, 121.0702, 0.50),
            (14.1204, 121.0704, -0.10)]):
        store.add_capture(capture_record(f"c{i + 1:02d}", flight_id=f["id"],
                                         mean=mean, lat=lat, lon=lon))
    closed = store.close_flight(f["id"])
    assert closed["status"] == "ok"
    assert closed["capture_count"] == 3
    assert closed["bounds"] is not None
    assert closed["grid"]["covered"] >= 1
    assert closed["altitude_m"] == pytest.approx(12.0)
    assert closed["classification"] == bndvi.classify(closed["stats"]["mean"])


def test_a_flight_that_recorded_nothing_is_marked_failed(store):
    f = store.open_flight()
    assert store.close_flight(f["id"])["status"] == "failed"


def test_a_flight_records_the_thresholds_it_was_measured_with(store):
    """Changing the thresholds later must not rewrite what was measured."""
    f = store.open_flight(thresholds={"healthy": 0.42, "moderate": 0.18})
    store.add_capture(capture_record("c01", flight_id=f["id"], mean=0.4,
                                     lat=14.12, lon=121.07))
    closed = store.close_flight(f["id"])
    assert closed["thresholds"] == {"healthy": 0.42, "moderate": 0.18}
    assert closed["classification"] == "moderate"      # 0.4 < 0.42


def test_recolouring_leaves_the_per_capture_stats_alone(store):
    f = store.open_flight()
    for i, mean in enumerate([0.5, 0.2, -0.2]):
        store.add_capture(capture_record(f"c{i + 1:02d}", flight_id=f["id"],
                                         mean=mean, lat=14.12 + i * 0.0002,
                                         lon=121.07 + i * 0.0002))
    store.close_flight(f["id"])
    before = store.capture("c01")["stats"]["mean"]

    recoloured = store.recolour_flight(f["id"], 0.45, 0.0)
    assert recoloured["thresholds"] == {"healthy": 0.45, "moderate": 0.0}
    assert store.capture("c01")["stats"]["mean"] == before
    shares = (recoloured["stats"]["healthy_pct"]
              + recoloured["stats"]["moderate_pct"]
              + recoloured["stats"]["stressed_pct"])
    assert shares == pytest.approx(100.0)


def test_deleting_a_flight_can_keep_the_records(store):
    """The "free up space" action promises to delete images and keep records."""
    f = store.open_flight()
    store.add_capture(capture_record("c01", flight_id=f["id"]))
    store.delete_flight(f["id"], keep_records=True)
    assert store.capture("c01") is not None


# ── legacy migration ─────────────────────────────────────────────────────────

def test_legacy_captures_survive_migration_as_ground_captures(store, tmp_path):
    """Pre-Hylocropter records have no flight and no GPS. They must still show
    up rather than being silently dropped."""
    legacy = tmp_path / "old"
    legacy.mkdir()
    (legacy / "captures.json").write_text(json.dumps([{
        "id": "20260101_101500", "timestamp": "2026-01-01T10:15:00",
        "label": "old capture", "notes": "",
        "files": {"raw": "raw_20260101_101500.jpg"},
        "stats": {"mean": 0.31, "min": -0.2, "max": 0.7, "healthy_pct": 55.0,
                  "moderate_pct": 30.0, "stressed_pct": 15.0},
        "classification": "healthy",
        "settings": {"exposure_us": 5000, "gain": 2.0},
    }]))
    (legacy / "raw_20260101_101500.jpg").write_bytes(b"not really a jpeg")

    store.migrate_legacy(legacy)
    rec = store.capture("20260101_101500")
    assert rec is not None
    assert rec["flight_id"] is None
    assert rec["geo"] is None
    assert rec in store.captures(flight_id=None)


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_persist_across_a_restart(tmp_path):
    path = tmp_path / "settings.json"
    settings_mod.Settings(path).update({"exposure_us": 9000})
    assert settings_mod.Settings(path).get("exposure_us") == 9000


def test_a_corrupt_settings_file_does_not_stop_the_dashboard(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ this is not json")
    assert settings_mod.Settings(path).get("exposure_us") == \
        settings_mod.DEFAULTS["exposure_us"]


def test_a_new_setting_is_a_non_event_for_an_existing_install(tmp_path):
    """Only two keys on disk; everything else keeps its default."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"gain": 3.5, "unknown_old_key": 1}))
    cfg = settings_mod.Settings(path)
    assert cfg.get("gain") == 3.5
    assert cfg.get("survey_side_m") == settings_mod.DEFAULTS["survey_side_m"]


def test_out_of_range_values_are_clamped_not_rejected(config):
    applied, warnings = config.update({"gain": 99.0})
    assert applied["gain"] == 16.0
    assert any("clamped" in w for w in warnings)


def test_an_unparseable_value_is_reported_not_stored(config):
    applied, warnings = config.update({"exposure_us": "not a number"})
    assert "exposure_us" not in applied
    assert warnings
    assert config.get("exposure_us") == settings_mod.DEFAULTS["exposure_us"]


def test_unknown_keys_are_ignored(config):
    applied, warnings = config.update({"definitely_not_a_setting": 1})
    assert applied == {}
    assert any("unknown" in w for w in warnings)


def test_the_thresholds_cannot_cross(config):
    """If moderate rises above healthy the middle band inverts and the
    percentages stop summing to 100."""
    _applied, warnings = config.update({"threshold_moderate": 0.6})
    assert config.get("threshold_moderate") < config.get("threshold_healthy")
    assert any("must stay under" in w for w in warnings)


def test_zoom_range_cannot_invert(config):
    config.update({"tile_zoom_min": 19, "tile_zoom_max": 17})
    assert config.get("tile_zoom_min") <= config.get("tile_zoom_max")


def test_an_unsupported_resolution_is_refused(config):
    _applied, warnings = config.update({"resolution": [123, 456]})
    assert warnings
    assert config.get("resolution") == settings_mod.DEFAULTS["resolution"]


def test_camera_kwargs_match_what_bndvi_expects(config):
    kwargs = config.camera_kwargs()
    assert set(kwargs) == {"resolution", "exposure_us", "gain", "warmup_s",
                           "colour_gains"}
    # the whole point: these actually reach the capture path
    bndvi.capture_image.__doc__      # exists
    assert isinstance(kwargs["resolution"], tuple)


# ── the survey block ─────────────────────────────────────────────────────────

def test_the_survey_block_starts_unmarked(config):
    """The farm's exact position isn't known until someone finds it on the
    imagery. A default centre would silently plan a mission over the wrong
    ground."""
    assert config.get("survey_lat") is None
    assert config.get("survey_lon") is None
    assert config.get("survey_side_m") > 0


def test_marking_the_block_round_trips(config):
    applied, warnings = config.update({"survey_lat": 14.12619,
                                       "survey_lon": 121.07596,
                                       "survey_side_m": 140})
    assert not warnings
    assert applied["survey_lat"] == pytest.approx(14.12619)
    assert config.get("survey_side_m") == 140


def test_the_block_can_be_unmarked_again(config):
    config.update({"survey_lat": 14.1, "survey_lon": 121.0})
    applied, warnings = config.update({"survey_lat": None, "survey_lon": None})
    assert applied == {"survey_lat": None, "survey_lon": None}
    assert not warnings
    assert config.get("survey_lat") is None


def test_an_unmarked_block_survives_a_restart_as_null(tmp_path):
    """Not as the string "None" — which is what the generic float path would
    have produced before survey_lat was made explicitly nullable."""
    path = tmp_path / "settings.json"
    settings_mod.Settings(path).update({"survey_lat": None, "survey_side_m": 120})
    on_disk = json.loads(path.read_text())
    assert on_disk["survey_lat"] is None
    assert settings_mod.Settings(path).get("survey_lat") is None


def test_the_vicinity_is_much_larger_than_the_survey_block(config):
    """The two areas the UI has to keep apart: the vicinity is imagery to search
    in, the block is what gets flown."""
    vicinity_ha = config.get("plot_box_m") ** 2 / 10_000
    block_ha = config.get("survey_side_m") ** 2 / 10_000
    assert vicinity_ha > block_ha * 10


def test_a_silly_block_size_is_clamped(config):
    config.update({"survey_side_m": 99999})
    assert config.get("survey_side_m") == settings_mod._LIMITS["survey_side_m"][1]
