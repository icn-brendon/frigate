# Devil's-Advocate Round 2: Reviewing the Fixes

Branch: `feature/dual-stream-recording` (working tree, uncommitted fixer changes
on top of `3225ec76`). Round-1 findings: `DEVIL-dual-stream.md`. The fixer
agent attempted B1 + M1 + M3 + M4 + M5 (and a substantial rewrite of
`event_recorder.py` from reactive-spawn to ring-buffer-promote). This pass
reviews the fixes themselves.

---

## Blockers

### B1-r2. Test suite is stale and will not boot — it asserts the *old* contracts
**Files:** `frigate/test/test_event_recorder.py` (entire file),
`frigate/test/test_dual_stream_config.py:72,164-181`,
`frigate/test/test_maintainer_dual_stream.py:148-150`,
`frigate/test/test_retention_dual_stream.py:48-60`.

The fixer rewrote `event_recorder.py` but did not touch the tests:
* `test_event_recorder.py` patches `frigate.record.event_recorder.LogPipe`
  and `start_or_restart_ffmpeg` — neither is imported any more, so every
  `@patch` decorator raises `AttributeError` at collection time.
* It references `state.is_recording`, `state.ffmpeg_process`,
  `state.recording_start_time`, `_start_recording`, `_check_timeouts`,
  `_check_ffmpeg_health`, `_stop_all_recordings` — none exist on the new
  `CameraRecordingState` (which has `is_active`, `promoted_paths`).
* `test_event_recording_defaults` asserts `pre_capture == 5`; the fixer
  changed the default to `15`.
* `test_record_events_ffmpeg_output_has_main_suffix` asserts `@main@`
  appears in the ffmpeg command for the `record_events` role; the
  rewrite moved `@main@` out of the ffmpeg output path (it now lives
  only in the *promoted* filename). Test will fail.
* `test_event_recording_enabled_without_record_events_role_warns`
  exercises the *warn-not-fail* contract; the new validator on
  `CameraConfig` raises `ValueError`, so the test exits before the
  assertion (likely passes accidentally, but exercises the wrong path).
* `test_validate_and_move_segment_main_always_kept` asserts
  `args[5] == RetainModeEnum.all`. The new code reads
  `cam_cfg.record.event_recording.retain.mode`. With a `MagicMock`
  fixture that attribute is a fresh `MagicMock` (truthy, not `None`),
  so the test now passes a `MagicMock` to `move_segment` instead of
  `RetainModeEnum.all`. Either an assertion error or, worse, a silent
  pass with the wrong contract.
* `test_cleanup_respects_stream_quality_column` is still `@unittest.skip`
  with a "pending fix" message — yet B1 *is* fixed. Skipped tests
  documenting fixed bugs are a guarantee the next refactor regresses.

**Failure mode in one sentence:** the round-2 work compiles and
deploys, but CI is *green by accident* (or red and ignored), and the
post-fix regressions in B1/M1/M3/M4/M5 are not exercised by any test.
**Suggested fix:** delete `test_event_recorder.py`, rewrite for
`_tick_buffers/_promote_segment`; re-enable
`test_cleanup_respects_stream_quality_column` and add an integration
case that puts a main row past `event_recording.retain.days` and
proves it is deleted; align `pre_capture` default in
`test_event_recording_defaults`; replace the `MagicMock` in the
maintainer test with a real `EventRecordingConfig` fixture.

---

## Major

### M1-r2. GHCR compose file is missing the dedicated `/tmp/event_cache` mount
**File:** `docker-compose.uat.ghcr.yml` (no `event_cache` block; only
`docker-compose.uat.yml` was updated).
**Mode:** Operators who deploy via the GHCR pull path inherit
`EVENT_BUFFER_BASE_DIR = /tmp/cache/event_buffer` because
`os.path.isdir("/tmp/event_cache")` is `False` in their container.
This **silently reintroduces the original M1 bug** — main-stream
ring buffer + substream record cache compete for the same 1 GB tmpfs.
The whole point of the fix was to prevent that.
**Fix:** mirror the `event_cache` tmpfs block into
`docker-compose.uat.ghcr.yml`, and add a startup log line that prints
the resolved `EVENT_BUFFER_BASE_DIR` and warns when the fallback path
is in use.

### M2-r2. `EVENT_BUFFER_BASE_DIR` is resolved at *module import*, never re-checked
**File:** `frigate/const.py:21-25`.
**Mode:** The `if os.path.isdir(...)` runs once at first import. If
the tmpfs is mounted *after* container start (k8s, late-mount in
docker-compose), or if the directory is created later, Frigate uses
the fallback for the entire process lifetime even though the
dedicated mount becomes available a second later. There is no
restart-to-pick-up path documented.
**Fix:** resolve at first use inside `_camera_buffer_dir(camera)`,
or fail fast at startup if `event_recording.enabled` and the
dedicated mount is missing.

### M3-r2. M5 parser still vulnerable to `camera == "main"` literal
**Files:** `frigate/record/maintainer.py:127, 200`,
`frigate/config/camera/camera.py:228-258` (no name-token validation).
**Mode:** The new parser is `prefix, date = basename.rsplit("@", 1)`
then `if prefix.endswith("@main")` strips it. For a camera named
literally `main`, a substream segment is `main@<ts>.mp4` →
`prefix == "main"`, which does **not** end with `@main`, so it
parses correctly as `camera = "main"`, `quality = "sub"`. Good.
But for a camera named `foo@main`, a *substream* segment is
`foo@main@<ts>.mp4` → `prefix == "foo@main"`, ends with `@main`, so
it is parsed as `camera = "foo"`, `quality = "main"`. **A substream
segment is silently re-tagged as main**, then bypassed by the new
`expire_existing_camera_main_recordings` retention pass. Round-1
M5 not fully closed.
**Fix:** prohibit `@main` as a tail-substring in camera names in
`CameraConfig` validation, OR have the maintainer cross-check the
parsed camera against `self.config.cameras` and refuse a `quality=main`
classification when no `record_events` role exists for it.

### M4-r2. M4 migration backfill loop can spin on a misbehaving driver
**File:** `migrations/036_add_stream_quality.py:55-78`.
**Mode:** When `cursor.rowcount` is unavailable the loop falls back to
a `COUNT(*)` probe — and if the count is non-zero but the UPDATE that
"should have hit them" actually didn't (e.g. the LIMIT subquery returns
rows not selected by the outer UPDATE due to a driver/engine
inconsistency), the code does **`break`** to avoid an infinite loop —
silently leaving NULLs behind. After migration, those NULL rows are
treated as `sub` by `expire_existing_camera_recordings` (the OR clause
covers them) but as **not-main** by
`expire_existing_camera_main_recordings`. Mostly safe but masks data
corruption. Also: the migration runs the backfill **even if the column
already existed** with all rows populated — a needless `O(N)` pass on
every upgrade boot.
**Fix:** skip the backfill entirely when
`SELECT 1 FROM recordings WHERE stream_quality IS NULL LIMIT 1` returns
no rows; on rowcount-unavailable, raise rather than silently break.

### M5-r2. Cleanup orphan-row sweep still ignores `stream_quality`
**File:** `frigate/record/cleanup.py:393-435`
(`expire_recordings` deleted-cameras branch).
**Mode:** The fixer partitioned the *per-camera* sweep into sub vs.
main passes (good). But the deleted-cameras branch (rows whose
`camera` is no longer in config) uses
`max(continuous.days, motion.days)` *globally* and unlinks the file
at `recording.path`. If a camera was renamed and had main segments,
the deleted-cameras sweep will use the *substream* retention horizon
and a path-unlink that does **not** check the `_main.mp4` defence
(present in `expire_existing_camera_main_recordings:376`). On a
deleted-camera with main-segment-rich history the operator may lose
HD evidence faster than substream evidence, with no audit trail.
**Fix:** in `expire_recordings`, partition the no-camera query by
`stream_quality` and use
`max(record.event_recording.retain.days, ...)` as the floor for main
rows, or simply use the larger of the two horizons for the orphan
sweep.

### M6-r2. M6 (DoS via unbounded `/main_availability`) was not fixed at all
**File:** `frigate/api/record.py:259-281`.
**Mode:** Endpoint still has no `LIMIT`, no time-window cap, and
materialises the whole result via `list(...)` inside `JSONResponse`.
The first reviewer flagged this as Major; it is still Major. With
the UI now actually calling it (RecordingView SWR), a single user
opening a year-wide timeline view will block the API event loop.
**Fix:** cap `(before - after) ≤ 24 h`, paginate, return merged
ranges instead of raw rows.

### M7-r2. Two-pass cleanup races itself on shared parent directories
**File:** `frigate/record/cleanup.py:472-479`.
**Mode:** Both `expire_existing_camera_recordings` and
`expire_existing_camera_main_recordings` add to `maybe_empty_dirs`,
then `remove_empty_directories` walks them. The sub pass may delete
`HH/cam/MM.SS.mp4` and queue `HH/cam` as empty; the main pass then
deletes `HH/cam/MM.SS_main.mp4` and queues the same parent again.
The parent is removed only when both qualities have been cleared at
the *same* hour — fine — but if the main pass returns `set()`
because `retain_days <= 0` (a zero-config), the directory containing
old `_main.mp4` files is left orphaned on disk forever (no DB row to
drive their deletion). Slow-leak.
**Fix:** when `retain_days <= 0` for main, log a one-time warning
and either keep main rows untouched (current behaviour, but also
sweep stale `_main.mp4` files older than continuous retention) or
delete both row + file.

### M8-r2. Promotion uses file `mtime` for the timestamp, not the original filename
**File:** `frigate/record/event_recorder.py:514-545`.
**Mode:** `_promote_segment` derives the destination timestamp from
`os.path.getmtime(src_path)`, which is the *write completion* time,
not the segment's start-of-content time. The substream segments are
keyed by start time (`-strftime` filename). Result: the
`stream_quality=main` row will land in the recording DB with a
`start_time` ~2 s **after** the substream row covering the same
content. The new `vod_ts` shadow-filter (M7-round-1 fix) compares
intervals — main intervals are systematically shifted forward,
producing tiny gaps where the player falls back to sub right at the
moment the user expects HD. Visually: a 2 s SD blip at the start
of every event.
**Fix:** parse the timestamp from the buffer filename (ffmpeg's
`-strftime` already produced the correct one) instead of mtime.

---

## Minor

### m1-r2. `pre_capture` default jumped from 5 to 15 with no migration note
`frigate/config/camera/record.py:81`. A user upgrading inherits a 3×
larger ring buffer per camera; sized to the new tmpfs but not
mentioned in BUILD-UAT or the docstring.

### m2-r2. `_tick_buffers` runs `os.listdir` + `os.path.getmtime` per camera every 1 s
`frigate/record/event_recorder.py:464-472`. Cheap on tmpfs, but on the
fallback path (`/tmp/cache/event_buffer`) it shares a directory with
substream segments and the listdir grows. No cap on entries.

### m3-r2. Backoff state in `_check_ffmpeg_health` is reset purely by 300 s of
liveness. A camera that crashes every 299 s will hit the 60 s cap
forever and never reset. `frigate/video/ffmpeg.py:464-470`.

### m4-r2. `vod_ts` shadow-filter logic builds intervals in Python; a busy
camera with thousands of main rows is `O(N log N)` per request.
`frigate/api/media.py:586-622`.

### m5-r2. The dead warning at `frigate/record/record.py:67-72` is unreachable
because the validator now raises — keep it as belt-and-braces, but
mark with a comment so it is not deleted by the next refactor.

### m6-r2. `EventRecorder.ffmpeg_cmds` parameter is retained "for API
compatibility" but is never read. Dead code; will rot.

### m7-r2. `_promote_segment` collision counter caps at 100 and silently
*returns* on overflow without re-queuing the file — the buffer
file remains, gets pruned later, the pre-roll content is lost.

### m8-r2. `expire_existing_camera_main_recordings` only `unlink`s files
whose `stem.endswith("_main")`. Defence in depth, but the row is
deleted regardless — a path-typo bug would silently leak files.

---

## Findings by severity
* **Blocker:** 1 (B1-r2)
* **Major:** 8 (M1-r2 – M8-r2)
* **Minor:** 8 (m1-r2 – m8-r2)

## Single thing I would most fear on UAT deploy

**M1-r2 — operator deploys via `docker-compose.uat.ghcr.yml`,
inherits the silent fallback `EVENT_BUFFER_BASE_DIR=/tmp/cache/event_buffer`,
and the round-1 M1 bug recurs unchanged on the live NVR.** The fix
is in *one* of the two compose files. The first heavy-event night,
the mainstream ring buffer evicts substream segments out of the
shared 1 GB tmpfs, and the user loses both SD and HD evidence
simultaneously — the worst possible failure mode of this feature.
The fix is one block of YAML; not adding it is the regression that
will bite first.
