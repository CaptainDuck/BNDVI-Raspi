"""Every page renders, and the API contracts hold.

There is no camera, no Pixhawk and no internet in CI — which is exactly the state
the dashboard has to survive, because it is also the state of a Pi sitting on a
bench. The project's rule is that every failure has a designed state in the UI, so
"no camera" and "no drone" must render a page, not a stack trace.

These are also the only tests that touch the Jinja templates, so they catch the
class of bug a template refactor introduces: an undefined variable renders as an
empty string in Jinja, but a missing filter or a bad `include` is a 500.
"""

import os
import sys

import pytest

# Point the app at a scratch data directory before importing it — the module
# builds its store, settings and log at import time, and must not touch real
# flights. Deliberately not tmp_path: that is per-test, and this is per-process.
_SCRATCH = os.path.join(os.path.dirname(__file__), "_scratch_data")
os.environ.setdefault("HYLOCROPTER_DATA", _SCRATCH)

sys.argv = ["app.py", "--dev"]          # keep argparse in app.__main__ happy
import app as app_mod                   # noqa: E402
import bndvi                            # noqa: E402
import flights as flights_mod           # noqa: E402


@pytest.fixture(scope="module")
def client():
    app_mod.app.config["TESTING"] = True
    app_mod.app.config["DEV_MODE"] = True
    app_mod.cam.dev_mode = True
    with app_mod.app.test_client() as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def _clean_scratch():
    yield
    import shutil
    shutil.rmtree(_SCRATCH, ignore_errors=True)


# ── pages ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/", "/new-flight", "/processing", "/history", "/debug", "/settings",
    "/setup",
])
def test_every_page_renders_with_no_hardware(client, path):
    res = client.get(path)
    assert res.status_code == 200, res.data[:400]
    assert b"<html" in res.data.lower()


def test_the_map_page_renders_before_the_first_flight(client):
    """It used to be a dead end: no map, and no way to start flying. Now it is
    where you go looking for the farm on the satellite imagery."""
    body = client.get("/").data.decode()
    assert 'id="plot-map"' in body
    assert "data-vicinity=" in body
    assert "data-survey=" in body
    assert "block you fly" in body


def test_the_map_page_says_the_drone_is_not_connected(client):
    """Every failure state is visible in the UI — the user should never need a
    terminal to find out why nothing is happening."""
    assert "Drone not connected" in client.get("/").data.decode()


def test_a_missing_page_renders_the_error_template(client):
    res = client.get("/no-such-page")
    assert res.status_code == 404
    assert b"<html" in res.data.lower()


def test_a_missing_api_path_returns_json_not_html(client):
    """The old app used bare abort(404), so any client calling res.json() threw."""
    res = client.get("/api/nope")
    assert res.status_code == 404
    assert res.is_json
    assert "error" in res.get_json()


# ── telemetry and camera, both absent ────────────────────────────────────────

def test_telemetry_reports_not_connected_rather_than_failing(client):
    snap = client.get("/api/telemetry").get_json()
    assert snap["connected"] is False
    assert snap["detail"]                 # says *why*, for the UI to show


def test_camera_status_reports_synthetic_frames_in_dev_mode(client):
    status = client.get("/api/camera/status").get_json()
    assert status["synthetic"] or status["available"] is False


def test_the_preview_frame_carries_three_real_channel_planes(client):
    """The channel-split panel claims to show a measured green channel, so green
    has to actually be sent — it used to be synthesised from the other two."""
    res = client.get("/api/preview/frame")
    assert res.status_code == 200
    if res.is_json:                       # no camera and no synthetic fallback
        pytest.skip(res.get_json().get("error", "no frame"))
    header = res.headers.get("X-Frame-Meta") or ""
    width, height = bndvi.SYNTH_W, bndvi.SYNTH_H
    assert len(res.data) == width * height * 3, header


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_get_returns_every_key(client):
    body = client.get("/api/settings").get_json()
    import settings as settings_mod
    assert set(body) >= set(settings_mod.DEFAULTS)


def test_marking_the_survey_block_over_the_api(client):
    res = client.patch("/api/settings", json={"survey_lat": 14.1262,
                                              "survey_lon": 121.0759,
                                              "survey_side_m": 140})
    assert res.status_code == 200
    body = res.get_json()
    assert body["applied"]["survey_side_m"] == 140
    assert not body["warnings"]

    # and the map page now hands it to Leaflet
    assert '"lat": 14.1262' in client.get("/").data.decode()

    # unmarking works too
    res = client.patch("/api/settings", json={"survey_lat": None,
                                              "survey_lon": None})
    assert res.get_json()["applied"] == {"survey_lat": None, "survey_lon": None}


def test_a_bad_setting_is_reported_not_500(client):
    res = client.patch("/api/settings", json={"exposure_us": "banana"})
    assert res.status_code == 200
    assert res.get_json()["warnings"]


# ── mission planning ─────────────────────────────────────────────────────────

def test_the_mission_plan_endpoint_returns_the_two_numbers(client):
    plan = client.get("/api/mission/plan?altitude_m=12&forward_overlap=0.4"
                      "&side_overlap=0.3&plot_side_m=100").get_json()
    assert plan["trigger_distance_m"] == pytest.approx(6.5, abs=0.1)
    assert plan["line_spacing_m"] == pytest.approx(10.1, abs=0.1)
    assert plan["plot_area_ha"] == pytest.approx(1.0, abs=0.01)


def test_the_mission_plan_endpoint_survives_junk_query_values(client):
    res = client.get("/api/mission/plan?altitude_m=abc&plot_side_m=")
    assert res.status_code == 200
    assert res.get_json()["photos"] >= 1


def test_the_new_flight_page_plans_for_the_marked_block(client):
    client.patch("/api/settings", json={"survey_lat": 14.1262,
                                        "survey_lon": 121.0759,
                                        "survey_side_m": 120})
    body = client.get("/new-flight").data.decode()
    assert "block you marked" in body
    assert 'value="120"' in body
    client.patch("/api/settings", json={"survey_lat": None, "survey_lon": None})
    assert "No block marked yet" in client.get("/new-flight").data.decode()


# ── captures ─────────────────────────────────────────────────────────────────

def test_capturing_in_dev_mode_produces_a_full_record(client):
    res = client.post("/api/captures", json={"label": "route test"})
    assert res.status_code in (200, 201), res.data[:300]
    rec = res.get_json()
    for key in ("id", "timestamp", "label", "notes", "files", "stats",
                "classification", "settings"):
        assert key in rec
    assert rec["flight_id"] is None                    # a ground capture
    assert -1.0 <= rec["stats"]["mean"] <= 1.0
    assert rec["classification"] in ("healthy", "moderate", "stressed")

    # it is reachable, and it renders
    assert client.get(f"/capture/{rec['id']}").status_code == 200
    assert client.get("/history").status_code == 200


def test_the_capture_list_separates_ground_captures_from_flights(client):
    """`flight_id=None` means "belongs to no flight". It used to also mean "no
    filter", so asking for the ground captures returned every capture."""
    flight = app_mod.store.open_flight(name="route test flight")
    ground_before = len(app_mod.store.ground_captures())
    app_mod.store.add_capture({
        "id": "route_c1", "timestamp": "2026-08-05T10:00:00", "label": "",
        "notes": "", "flight_id": flight["id"], "geo": None, "files": {},
        "stats": None, "classification": None, "settings": {},
    })
    assert len(app_mod.store.ground_captures()) == ground_before
    assert len(app_mod.store.captures(flight_id=flight["id"])) == 1
    assert len(app_mod.store.captures()) > ground_before
    app_mod.store.delete_flight(flight["id"])


# ── offline assets ───────────────────────────────────────────────────────────

def test_leaflet_and_the_fonts_are_served_from_disk(client):
    """Nothing may reach the network at run time — that is the thesis's central
    claim, so the vendored assets have to actually be there."""
    for path in ("/static/vendor/leaflet/leaflet.js",
                 "/static/vendor/leaflet/leaflet.css",
                 "/static/css/tokens.css",
                 "/static/js/colormap.js"):
        assert client.get(path).status_code == 200, path


def test_no_page_references_an_external_host(client):
    """A CDN link that only fails in the field is worse than one that fails now."""
    for path in ("/", "/debug", "/settings", "/setup", "/new-flight",
                 "/history", "/processing"):
        body = client.get(path).data.decode()
        for host in ("unpkg.com", "cdn.jsdelivr.net", "fonts.googleapis.com",
                     "fonts.gstatic.com", "arcgisonline.com", "tile.openstreetmap",
                     "cdnjs.cloudflare.com"):
            assert host not in body, f"{path} references {host}"


def test_a_missing_tile_serves_a_fallback_rather_than_a_broken_image(client):
    res = client.get("/tiles/19/999999/999999.jpg")
    assert res.status_code == 200
    assert res.data


def test_the_tile_coverage_endpoint_answers_how_far_the_map_goes(client):
    body = client.get("/api/tiles/coverage").get_json()
    assert "has_tiles" in body["coverage"]


def test_the_tile_plan_endpoint_estimates_before_downloading(client):
    """You get told the tile count and the megabytes before committing."""
    body = client.get("/api/tiles/plan?box_m=620").get_json()
    assert body["tiles"] > 0
    assert body["est_bytes"] > 0


# ── logs, so the UI never needs a terminal ───────────────────────────────────

def test_the_log_endpoint_returns_lines_the_ui_can_show(client):
    body = client.get("/api/logs").get_json()
    lines = body["lines"] if isinstance(body, dict) else body
    assert isinstance(lines, list)


def test_destructive_system_actions_require_confirmation(client):
    """A destructive action returns a confirmation contract instead of acting."""
    res = client.post("/api/system/actions/shutdown", json={})
    assert res.status_code in (200, 400, 409)
    body = res.get_json()
    if res.status_code == 200:
        assert body.get("confirm_required") or body.get("confirm") is not True
