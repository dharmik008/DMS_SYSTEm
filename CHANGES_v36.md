# v36 — 360° Spin View in Caryanams Studio

## What dealers get
- The Studio canvas has a **Preview / 360° View** switch. Preview is the canvas
  as before; 360° View spins through every photo in the current set.
- Two ways to make a set: upload several photos taken around the car together
  (as before), or upload a walkaround video. Video upload is offered in three
  places: **Upload Video** in the canvas header (next to Upload Images), the
  empty 360° view, and the new **360° Spin View** panel section, which also
  sets how many frames to extract. The video is split into 24, 36, 48 or 72 evenly spaced frames,
  saved through the normal upload pipeline (HD resize + logo) like photos.
- Viewer: drag to rotate (with momentum), auto-rotate, play/pause, previous/next,
  frame slider, zoom with drag-to-pan, double-click zoom, full screen, and
  keyboard control (← → Space + − 0). Respects reduced-motion settings.
- Stopping on a frame selects it, so Remove Background, Apply Background and
  Download act on that frame. Each frame shows its latest version: composite,
  then no-background cut-out, then the original.
- New view angle **360° Spin Frame**; spin frames can have their background
  removed (Front/Back/Left/Right rule extended to them).

## Fixes
- `GET /studio/api/car-360/<id>` returned 500 for an unknown id; now 404.
- The same endpoint linked no-background frames to `/static/images/uploads/`,
  but they are saved in `static/processed/`, so they never loaded. URLs are now
  built from the file's real location.

## Files
New: `background/spin360.py`, `static/js/studio_360.js`,
`static/css/studio_360.css`, `templates/background/_spin360.html`,
`test_studio_360.py`.
Changed: `background/routes.py`, `templates/background/remove.html`, `README.md`.

No new dependencies (uses `opencv-python-headless`, already required) and no
database changes (spin frames are ordinary `studio_image` rows sharing a
`session_group`).

## Testing
- `pytest`: whole suite passes (16 new tests in `test_studio_360.py`).
- Browser (Chromium, desktop 1440px and phone 390px): every viewer control,
  thumbnail/panel sync, video upload, Clear All. No JavaScript or server errors.
- Pixel comparison against v35 in Preview mode: the only differences are the
  new view switch and the two BG-removal notes that now mention 360° frames.

## Known issues (not changed here)
- `remove-bg`, `remove-bg-batch` (page script) and `gallery` still build
  no-background URLs under `/static/images/uploads/`, so the "No Background"
  preview doesn't load. Same one-line fix as the 360° endpoint.
- `studio_image` has no dealer column, so any Studio user who knows an image id
  can read it.
- `plan_quotas._studio_usage_this_month` imports `StudioImage` from `models`,
  where it doesn't exist, so Studio usage always counts as 0.
