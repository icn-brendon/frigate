# Devil's-Advocate Review: `feature/dual-stream-recording`

Branch: `feature/dual-stream-recording`
Base: upstream `dev` @ `15ac76f2`
First-pass review: `REVIEW-dual-stream.md`

This second opinion assumes the author is over-confident and the first reviewer was charitable. Findings prioritised by severity. **Do not deploy to a live NVR until at least the blocker and majors are resolved.**

---

## Blockers (do-not-deploy)

### B1. Cleanup actively *deletes* main-stream segments using substream rules
**File:** `frigate/record/cleanup.py:121-217` (unchanged by this branch — that's the bug).
**Mode:** `expire_existing_camera_recordings` selects from `Recordings` with no `stream_quality` filter. Main segments are walked alongside substream segments. Two paths delete them:
1. The `WHERE` clause `(end_time < continuous_expire_date) & (motion == 0) & (dBFS == 0)` — main segments are written by `move_segment` with `segment_stats(camera, start_time, end_time)` which computes motion/objects from the substream's `object_recordings_info` queue keyed only by the **substream** time window. Since main segments often start fractions of a second offset from the substream segment grid (different ffmpeg processes, different keyframe alignment), `segment_info` will frequently come back `motion=0, objects=0, dBFS=0`. As soon as `record.continuous.days` elapses (often 0 — many users only use motion retention), every main segment is mass-deleted.
2. The retention-mode branch (`mode == RetainModeEnum.motion and recording.motion == 0 …`) deletes any segment that doesn't overlap the kept-review heuristic — main segments, which only exist *because* of an event, can still be dropped because the review-overlap test compares against substream `recording.start_time/end_time`, and the boundary handling does not consider that the segment is from a different process.

Even when not deleted, the on-disk `_main.mp4` file is `unlink`ed when the substream row at the same minute is expired, because the directory is shared.
**Fix:** make `cleanup.py` `stream_quality`-aware end-to-end: separate WHERE branches per quality, retain main segments via `record.event_recording.retain.{days,mode}`, and never compare a main row against substream stats. Add an integration test.

---

## Major

### M1. tmpfs cache is sized for one stream, not two
**File:** `frigate/record/maintainer.py:222-272` and `frigate/const.py:123` (`MAX_SEGMENTS_IN_CACHE = 6`).
**Mode:** `keep_count` is per-camera, not per-(camera, stream_quality). When the maintainer falls behind during an event, the cap of 6 covers *both* sub and main segments combined — but `most_recently_processed_frame_time` is derived from the substream detect frame queue only, so `processed_segment_count` happily counts main segments as "processed" (they have older timestamps because main started later in wall-clock but its segment timestamps are aligned to wall clock). Result: under heavy events, **substream segments are silently truncated** to make room for main segments, yielding gaps in the supposedly-continuous SD recording. Worst case on a 4 K 20 Mb/s mainstream, a 6-segment tmpfs ring is ≈ 240 MB per camera — eight cameras at peak event time will OOM tmpfs and trigger the unlinking branch above.
**Fix:** track `MAX_SEGMENTS_IN_CACHE` per `(camera, stream_quality)` tuple, or document a hard tmpfs sizing requirement and enforce it at startup.

### M2. UI "main-availability" stripe lies (data-shape mismatch with reality)
**File:** `web/src/views/recording/RecordingView.tsx:155-167`.
**Mode:** `getMainStreamAvailability` derives availability from `mainCameraReviewItems`, **not** from the new `/{camera}/recordings/main_availability` endpoint the author added on the backend. The endpoint is dead code. So the timeline shows the user "HD available here" wherever a *review item* exists, including reviews that pre-date the dual-stream feature being enabled, reviews on cameras with no `record_events` role, and times when the mainstream ffmpeg crashed (M3) and produced no segment. When the user then clicks HD they get a confusing fallback / blank player.
**Fix:** call the endpoint, debounce/SWR-cache it, and union with review items only as a hint — never as truth.

### M3. ffmpeg respawn loop is unbounded
**File:** `frigate/record/event_recorder.py:204-243` (`_check_ffmpeg_health`).
**Mode:** If the mainstream is broken (bad credentials, codec mismatch, network down) ffmpeg exits within ~1 s. The health check restarts immediately as long as `(now - last_activity_time) < post_capture`, and every detection event refreshes `last_activity_time`. On a busy front-yard camera this becomes a tight respawn loop spawning ffmpeg dozens of times per minute, leaking `LogPipe` file descriptors (each restart calls `close()` then immediately reassigns; the old reader thread is supposed to exit but if `LogPipe.dump()` is called in parallel with `close()` the read end can leak), spamming logs, and accumulating zombie children if `Popen.wait()` is not invoked (`stop_ffmpeg` does, but crash-restart skips it).
**Fix:** exponential backoff with cap (e.g., 1 s → 30 s), circuit-break after N consecutive failures within a window, and stop touching `last_activity_time` from the restart path.

### M4. Migration is not idempotent and corrupts a partially-applied DB
**File:** `migrations/036_add_stream_quality.py`.
**Mode:** `migrator.sql('ALTER TABLE … ADD COLUMN …')` has no `IF NOT EXISTS` (SQLite does not support it for `ADD COLUMN`). If the migration is interrupted mid-way (Frigate killed during the rewrite SQLite performs for `NOT NULL DEFAULT`), the next start re-runs `ALTER TABLE` and dies with `duplicate column name: stream_quality`, refusing to boot. There is no `rollback`, so the operator must edit the schema by hand. On a multi-million-row `recordings` table the rewrite holds an exclusive lock for tens of seconds — Frigate appears hung; users will SIGKILL.
**Fix:** wrap in `try/except OperationalError` checking column existence via `PRAGMA table_info`. Drop `NOT NULL` so SQLite skips the table rewrite (default still applies via app default); backfill in chunks if needed.

### M5. Filename collision: `…@main@…` inside a camera name
**File:** `frigate/record/maintainer.py:119-129, 188-202` and `frigate/config/camera/camera.py:295-315`.
**Mode:** Camera name validation does not forbid `@` or the literal token `main`. A camera named `garage@main` (silly but legal) plus a single substream segment yields basename `garage@main@20260415123000+0000`, which the parser reads as camera `garage`, quality `main`, time `20260415123000+0000` — i.e., a substream segment is silently re-tagged as `main`, then exempted from retention by the maintainer fix-up branch, then deleted by cleanup (B1). The previous code used `rsplit('@', 1)` which was robust to an `@` in the camera name; the new code uses `split('@')` which is not.
**Fix:** parse with `rsplit('@', 2)` and check the middle token explicitly; in config validation reject `@` and reserved tokens (`main`, `preview_`) in camera names with a migration warning for existing installs.

### M6. New API endpoint exposes `/main_availability` with no authz scoping check beyond camera ACL
**File:** `frigate/api/record.py:259-281`.
**Mode:** The endpoint depends on `require_camera_access` — fine — but unlike `/recordings`, it returns *every* main-segment row in the window with no `LIMIT`. A client requesting a year-wide window for a busy camera triggers an O(N) scan that returns hundreds of MB of JSON, blocking the API event loop (`peewee.iterator()` here streams to the response builder which is sync inside `JSONResponse(content=list(...))` — `list(...)` materialises everything). Trivially DoSable by any logged-in user.
**Fix:** require `before − after ≤ 24 h`, paginate, and stream the response.

### M7. `vod_ts` query loses the `stream_quality` filter when called from `vod_clip` paths that already have query-strings
**File:** `frigate/api/media.py:552-578, 776-781` and `web/src/components/player/dynamic/DynamicVideoPlayer.tsx:224`.
**Mode:** Frontend appends `?quality=main` *after* the path. But several existing call sites (HomeKit accessory bridge, mobile app shortcuts to `/vod/...`) pre-build the URL with their own `?download=true` etc.; the absence of a query-string merge means existing clients get `…master.m3u8?quality=main` *or* `…master.m3u8?download=true` — never both — and the substream is silently served when the user expected main. Worse: when the FastAPI router parses `quality` it falls back to default `"sub"` if absent, so any existing client that touches `vod_hour` continues to get sub even on a camera that has migrated to main as the "user-facing default" downstream.
**Fix:** always merge query strings via `URLSearchParams`; treat `quality=main` as a header in addition to a query param; update mobile/HomeKit clients in lock-step.

### M8. Race between `EventRecorder` start and `RecordingMaintainer` cache sweep
**File:** `frigate/record/maintainer.py:165-176, 222-272`.
**Mode:** The maintainer uses `psutil.process_iter()` + `process.open_files()` to identify ffmpeg-held files and skip them. This list is taken **once per sweep** (~5 s cadence). The `EventRecorder` can spawn a fresh ffmpeg between the snapshot and the parsing loop; that ffmpeg's first segment file is on disk but `files_in_use` does not contain it. The maintainer then tries to move/parse a still-being-written file → `move_segment` succeeds because it does a `shutil.move` of an open file (Linux allows this; the resulting moved file is truncated at the moved point, ffmpeg keeps writing to a now-orphaned inode). Result: silently truncated main segment, plus a "ghost" inode growing in tmpfs until ffmpeg exits.
**Fix:** include "started in the last 2× CACHE_SEGMENT_FORMAT seconds" as an additional in-use heuristic, and/or have `EventRecorder` write to a `.tmp` suffix that the maintainer ignores.

---

## Minor

### m1. ZMQ HWM event loss on the new subscriber
`event_recorder.run` polls every 500 ms. Detection publishers fire at frame rate. ZMQ default HWM (1000) plus 500 ms polling means high-FPS cameras can drop the *first* "object appeared" message and the recorder starts ~500 ms late or not at all for short events. Lower poll interval to 50 ms or use blocking `recv` on a poller.

### m2. `FrigateBaseModel` config reload during an active event
The maintainer/event_recorder hold a stale `camera_configs` dict. A config reload that disables `event_recording.enabled` does not stop the in-flight ffmpeg until the camera is removed. Hook into the existing config reload signal.

### m3. `_process_detection_events` unpacks 6 fields with no try/except — schema drift in `DetectionPublisher` payload kills the thread silently (daemon, so no restart). Wrap in try/except and log; consider `dataclass` payloads.

### m4. Index `(camera, stream_quality, start_time)` is redundant with the existing `(camera, start_time)` for sub-only queries (the planner will choose the wider index, doubling write amplification on a hot table).

### m5. Log spam: `_start_recording` and `_stop_recording` both log at `INFO`. A motion-storm camera will produce 100s of lines/hour, drowning real signals. Demote to `DEBUG` after the first event per minute.

### m6. `recordings_summary` hard-codes `stream_quality == "sub"` — fine, but the dashboard "hours of recording" tile no longer matches what the API returns when a user is watching the HD timeline. UX regression in the operator dashboard.

### m7. Export range that spans both qualities (user drags a range across an HD/SD boundary) is not handled at all in `export_recording` (not modified by this branch). Will silently mux substream only.

### m8. `RetainModeEnum.all` shortcut for main is passed through `move_segment` but never re-validated downstream — if cleanup is later made stream-aware (B1 fix), be careful that `RetainModeEnum.all` is honoured rather than the substream's `motion` mode.

### m9. `stream_quality` defaults to `"sub"` everywhere in queries. A future bug where an ingestion path writes `NULL` (e.g., bulk-loader, recovery script) would make rows invisible to every API. Add a `CHECK (stream_quality IN ('sub','main'))` constraint in migration 037.

### m10. `process_iter`+`open_files` requires CAP_SYS_PTRACE on hardened containers; if running in a sandbox where this is denied, the entire in-use detection silently degrades and B1/M8 become certain rather than probabilistic.

---

## Findings by severity

- **Blocker:** 1 (B1)
- **Major:** 8 (M1 – M8)
- **Minor:** 10 (m1 – m10)

## Single issue I would block deploy on

**B1 — `cleanup.py` is not `stream_quality`-aware.** Within days of deploy on a live NVR with `record.continuous.days = 0` (a common config), every HD event clip will be silently deleted by the existing cleanup query; the operator will only notice when they go back to review an incident. The feature is worse than the current state of the world because users will *believe* they have HD evidence when they don't.
