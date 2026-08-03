# Offline basemap tiles

Satellite tiles live here as `{z}/{x}/{y}.jpg`, plus a `manifest.json` describing
what was downloaded.

**This directory ships empty.** Download the imagery for your plot once from
**Settings → Offline map** on a machine with internet, then commit the result —
a 620 m box (38 ha) at zoom 16–19 is about 138 tiles / 2.4 MB.

Until then the map serves plain sand-coloured tiles and the dashboard says so
rather than showing a broken grid. See DEPLOYMENT.md §1 and RESEARCH-GAPS.md §8.
