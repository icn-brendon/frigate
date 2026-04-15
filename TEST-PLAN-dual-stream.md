# Test Plan: Dual-Stream Recording (substream continuous + mainstream on event)

Feature branch: `feature/dual-stream-recording`

Scope: new `EventRecordingConfig` on `record`, new `record_events` role on `ffmpeg.inputs`,
`stream_quality` column on `Recordings` (migration 036), `EventRecorder` thread, maintainer
parsing of `camera@main@timestamp.mp4` segments, API `quality` filter + `/recordings/main_availability`.

Testing constraint: the Frigate test suite cannot run in the local dev shell — all pytest
execution happens inside the built Docker image on the NVR host (`make run_tests`).
Author tests only; do not execute locally.

---

## 1. Unit tests (pytest, under `frigate/test/`)

### 1a. Config validation — `frigate/test/test_config.py` (new cases in `TestConfig`)

1. **`test_event_recording_defaults`**
   Verifies: default `FrigateConfig` has `record.event_recording.enabled is False`,
   `pre_capture == 5`, `post_capture == 10`.
   Assertion: `frigate_config.cameras["back"].record.event_recording.enabled is False` and
   the two int defaults match `EventRecordingConfig` field defaults.

2. **`test_event_recording_post_capture_must_be_non_negative`**
   Verifies: `post_capture = -1` raises `pydantic.ValidationError`.
   Assertion: `with self.assertRaises(ValidationError): FrigateConfig(**cfg)` where
   `cfg.record.event_recording.post_capture = -1`.

3. **`test_event_recording_pre_capture_respects_max`**
   Verifies: `pre_capture > MAX_PRE_CAPTURE` raises `ValidationError`
   (field has `le=MAX_PRE_CAPTURE`).
   Assertion: `ValidationError` raised.

4. **`test_record_events_role_and_record_on_same_input_rejected`**
   Verifies: `CameraFfmpegConfig.validate_roles` rejects an input that lists both
   `record` and `record_events`.
   Assertion: `ValidationError` with message containing
   `"record and record_events roles must not be on the same input"`.

5. **`test_record_events_role_on_separate_input_accepted`**
   Verifies: two inputs (one with `detect,record`, one with `record_events`) pass validation.
   Assertion: `FrigateConfig(**cfg)` constructs without error; the camera's
   `ffmpeg_cmds` contains an entry whose `roles` include `"record_events"`.

6. **`test_event_recording_enabled_without_record_events_role_warns`**
   Verifies: `RecordProcess.run` branch — when `event_recording.enabled = True` but no
   input has `record_events`, no FFmpeg cmd is produced for that camera.
   Assertion: `cam_config.ffmpeg_cmds` has no entry with `"record_events"` in roles; test
   asserts the list comprehension in `frigate/record/record.py` lines 61-66 produces
   `found_role = False` for that camera.

7. **`test_record_events_ffmpeg_output_has_main_suffix`**
   Verifies: the cache path segment built for a `record_events` role uses
   `camera@main@timestamp.mp4` (from `camera.py:314`).
   Assertion: the generated ffmpeg cmd string contains `@main@%Y%m%d%H%M%S` (or whatever
   `CACHE_SEGMENT_FORMAT` is) exactly — greps the cmd list for `"@main@"`.

### 1b. Migration 036 — new file `frigate/test/test_migration_036.py`

Use `playhouse.migrate.SqliteMigrator` against an in-memory DB seeded from
`frigate/models.py` prior to `stream_quality`.

8. **`test_migration_036_adds_column`**
   Verifies: after running `migrate()` from `migrations/036_add_stream_quality.py`,
   the `recordings` table has a `stream_quality VARCHAR(10) NOT NULL DEFAULT 'sub'` column.
   Assertion: `PRAGMA table_info(recordings)` lists `stream_quality` with the right
   default and not-null flag.

9. **`test_migration_036_backfills_existing_rows_to_sub`**
   Verifies: rows inserted before the migration get `stream_quality='sub'` afterwards.
   Assertion: `SELECT DISTINCT stream_quality FROM recordings` returns `{"sub"}`.

10. **`test_migration_036_creates_composite_index`**
    Verifies: the `recordings_stream_quality` index on
    `(camera, stream_quality, start_time)` exists.
    Assertion: `SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='recordings'`
    contains `"recordings_stream_quality"`.

11. **`test_migration_036_rollback_is_noop`**
    Verifies: `rollback()` does not raise and leaves the column intact (the
    implementation is intentionally a no-op; peewee-migrate does not cleanly drop
    columns in SQLite).
    Assertion: calling `rollback(migrator, db)` returns `None` and column still present.

### 1c. EventRecorder state machine — new file `frigate/test/test_event_recorder.py`

Mock `DetectionSubscriber`, `start_or_restart_ffmpeg`, `stop_ffmpeg`, `LogPipe`.

12. **`test_initial_state_is_idle`**
    Verifies: on construct, every camera has `is_recording=False`,
    `ffmpeg_process is None`, `last_activity_time == 0.0`.
    Assertion: direct attribute check on `CameraRecordingState` instances in
    `recorder.camera_states`.

13. **`test_motion_event_transitions_idle_to_triggered`**
    Verifies: feeding a detection event with `motion_boxes=[...]` calls
    `_start_recording` which invokes the patched `start_or_restart_ffmpeg` once and sets
    `is_recording=True`, `recording_start_time > 0`.
    Assertion: `mock_start_ffmpeg.call_count == 1` and state flags flipped.

14. **`test_false_positive_object_does_not_trigger`**
    Verifies: a detection with `current_tracked_objects=[{"false_positive": True,
    "motionless_count": 0}]` and empty motion_boxes does NOT start recording.
    Assertion: `mock_start_ffmpeg.call_count == 0`, `is_recording is False`.

15. **`test_motionless_object_does_not_trigger`**
    Verifies: same as above but with `motionless_count > 0`.
    Assertion: no recording started.

16. **`test_active_object_triggers_recording`**
    Verifies: `{"false_positive": False, "motionless_count": 0}` + no motion starts the recorder.
    Assertion: `is_recording is True` after one `_process_detection_events` call.

17. **`test_post_capture_timeout_stops_recording`**
    Verifies: after `_start_recording`, setting `last_activity_time = now - post_capture - 1`
    and calling `_check_timeouts` invokes `stop_ffmpeg` and flips `is_recording=False`.
    Assertion: `mock_stop_ffmpeg.call_count == 1`, `is_recording is False`,
    `recording_start_time == 0.0`.

18. **`test_continued_activity_prevents_timeout`**
    Verifies: `last_activity_time = now` keeps `is_recording=True` through `_check_timeouts`.
    Assertion: `mock_stop_ffmpeg.call_count == 0`.

19. **`test_ffmpeg_crash_within_window_restarts`**
    Verifies: `_check_ffmpeg_health` sees `poll()` return `1` while
    `now - last_activity_time < post_capture` — it must call
    `start_or_restart_ffmpeg` again and keep `is_recording=True`.
    Assertion: `mock_start_ffmpeg.call_count == 2` (initial + restart).

20. **`test_ffmpeg_crash_after_window_cleans_up`**
    Verifies: same crash but with `last_activity_time` older than `post_capture` — no
    restart, state resets to idle.
    Assertion: `is_recording is False`, `ffmpeg_process is None`, no restart call.

21. **`test_missing_ffmpeg_cmd_logs_error_no_crash`**
    Verifies: `_start_recording` on a camera absent from `ffmpeg_cmds` logs an error and
    returns without raising.
    Assertion: `logger.error` called once; `is_recording` remains `False`.

22. **`test_shutdown_stops_all_active_recordings`**
    Verifies: `_stop_all_recordings()` calls `stop_ffmpeg` for every camera currently
    recording.
    Assertion: one `stop_ffmpeg` call per active camera.

23. **`test_detection_for_unknown_camera_ignored`**
    Verifies: a detection event for a camera not in `camera_states` is silently
    skipped (line 107-108 `continue`).
    Assertion: no recording started, no exception.

### 1d. Maintainer mixed-quality handling — extend `frigate/test/test_maintainer.py`

24. **`test_move_files_parses_main_stream_filename`**
    Verifies: a cache file named `camera@main@20260101000000+0000.mp4` is grouped with
    `stream_quality == "main"` (per `maintainer.py:192-196`).
    Assertion: the dict passed into the mocked `validate_and_move_segment` has
    `stream_quality == "main"`.

25. **`test_move_files_parses_substream_filename_default_sub`**
    Verifies: `camera@20260101000000+0000.mp4` yields `stream_quality == "sub"`.
    Assertion: same shape check with `"sub"`.

26. **`test_move_files_rejects_four_part_filename`**
    Verifies: `camera@foo@bar@20260101000000+0000.mp4` (len(parts)==4) is treated as
    unexpected and warned about.
    Assertion: warning logged, file not passed to `validate_and_move_segment`.

27. **`test_validate_and_move_segment_main_always_kept`**
    Verifies: `validate_and_move_segment` with `stream_quality="main"` short-circuits
    through the `if stream_quality == "main":` branch (line 404) and calls `move_segment`
    with `RetainModeEnum.all`, regardless of `continuous`/`motion` config.
    Assertion: mocked `move_segment` called once with `store_mode=RetainModeEnum.all`
    and `stream_quality="main"`.

28. **`test_move_segment_writes_main_suffix_file`**
    Verifies: `move_segment(..., stream_quality="main")` builds `file_name` ending in
    `_main.mp4` (line 638-639).
    Assertion: the path passed to the mocked subprocess exec ends with
    `MM.SS_main.mp4`.

29. **`test_move_segment_sub_has_no_suffix`**
    Verifies: `stream_quality="sub"` produces `MM.SS.mp4` (no suffix).
    Assertion: path ends with `MM.SS.mp4` and does NOT contain `_sub`.

30. **`test_move_segment_returns_stream_quality_in_record`**
    Verifies: returned dict from `move_segment` contains
    `Recordings.stream_quality.name: <value>`.
    Assertion: `result["stream_quality"] == "main"` (and `"sub"` in paired case).

### 1e. Retention interaction — `frigate/test/test_record_retention.py` additions

31. **`test_main_segment_bypasses_discard_logic`**
    Verifies: in maintainer flow, a segment with `stream_quality="main"` is passed to
    `move_segment` with `RetainModeEnum.all`, and `SegmentInfo.should_discard_segment`
    with that mode always returns `False` even when `motion_count=0`, `active_object_count=0`,
    `average_dBFS=0`.
    Assertion: `SegmentInfo(0,0,0,0).should_discard_segment(RetainModeEnum.all) is False`.

32. **`test_overlapping_sub_and_main_same_timeframe`**
    Verifies (integration-ish unit): when both a sub and a main segment exist for the
    same start_time, the file-path computation yields two distinct paths
    (`MM.SS.mp4` and `MM.SS_main.mp4`) in the same `YYYY-MM-DD/HH/camera` directory.
    Assertion: two different `file_path` values produced; neither overwrites the other.

33. **`test_cleanup_respects_stream_quality_column`**
    Verifies (if `frigate/record/cleanup.py` filters by quality): cleanup uses the
    new index path. If cleanup does NOT yet filter, this test documents the gap.
    Assertion: either (a) query includes `Recordings.stream_quality`, or (b) skip with
    an xfail noting the follow-up.

---

## 2. Integration checks (run inside Docker image via `make run_tests` on NVR)

These require the built image because they touch sqlite-with-migrations, the real
`RECORD_DIR`, and the running FastAPI app.

34. **`integration_db_schema_post_migration`**
    Verifies: on a fresh DB after all migrations run, `recordings.stream_quality` exists
    with default `'sub'` and the `recordings_stream_quality` index is present.
    Assertion: inspect `sqlite_master` + `PRAGMA table_info`.

35. **`integration_db_upgrade_from_035`**
    Verifies: seed a DB at migration 035 with a handful of `recordings` rows, run 036,
    every row has `stream_quality='sub'`.
    Assertion: `SELECT COUNT(*) WHERE stream_quality IS NULL` returns 0.

36. **`integration_recording_directory_layout_sub_only`**
    Verifies: with `event_recording.enabled=False`, under sustained traffic the
    `/media/frigate/recordings/YYYY-MM-DD/HH/<camera>/` directory contains only
    `MM.SS.mp4` files (no `_main` suffixed files).
    Assertion: `glob("*_main.mp4")` returns `[]`.

37. **`integration_recording_directory_layout_mixed`**
    Verifies: with `event_recording.enabled=True` and a motion event triggered (via
    synthetic detection publisher), both `MM.SS.mp4` and `MM.SS_main.mp4` appear in the
    same hour directory during the event window.
    Assertion: at least one file of each suffix; timestamps overlap.

38. **`integration_api_recordings_quality_sub`**
    Verifies: `GET /api/<camera>/recordings?after=...&before=...` (no `quality` param)
    returns only substream rows and each row has `"stream_quality": "sub"`.
    Assertion: response JSON is a list; every item has `stream_quality=="sub"`.

39. **`integration_api_recordings_quality_main`**
    Verifies: `GET /api/<camera>/recordings?quality=main` returns only rows with
    `stream_quality=="main"`, ordered by `start_time`.
    Assertion: all items `stream_quality=="main"`; monotonic `start_time`.

40. **`integration_api_main_availability_endpoint`**
    Verifies: `GET /api/<camera>/recordings/main_availability` returns an array of
    `{start_time, end_time}` objects covering the event windows only.
    Assertion: each returned range maps 1:1 with a detected event; no overlap with
    substream-only time ranges.

41. **`integration_api_hourly_summary_uses_sub_only`**
    Verifies: the hourly summary query in `frigate/api/record.py:172` only sums
    substream durations (otherwise durations double-count when main exists).
    Assertion: for an hour with known N seconds of sub + M seconds of main, the summary
    `duration` equals N, not N+M.

42. **`integration_event_recorder_ffmpeg_spawned`**
    Verifies: with `event_recording.enabled=True`, after publishing a synthetic motion
    detection, `psutil` shows an `ffmpeg` child whose cmdline contains `@main@`.
    Assertion: process exists; disappears within `post_capture + grace` seconds after
    motion stops.

---

## 3. Manual smoke tests (NVR post-UAT deploy)

Execute on the target NVR with the new image deployed. Each step has an explicit
pass signal.

43. **`manual_smoke_substream_baseline`**
    Steps: deploy with `event_recording.enabled=False` for one camera, leave running
    for 10 min with no motion.
    Verify: `ls /media/frigate/recordings/$(date -u +%Y-%m-%d)/*/camera/` shows
    continuous `MM.SS.mp4` files; no `_main.mp4`; UI timeline shows unbroken substream
    playback.

44. **`manual_smoke_motion_triggers_mainstream`**
    Steps: enable `event_recording` with `pre_capture=5`, `post_capture=10`. Walk in
    front of the camera for ~5 s.
    Verify: during the event a `_main.mp4` file appears in the same hour directory;
    `frigate.log` shows `Started main stream event recording` followed by
    `Stopped main stream event recording (ran for ~15s)`.

45. **`manual_smoke_substream_continues_during_event`**
    Steps: same event as above.
    Verify: the `MM.SS.mp4` substream files are NOT interrupted — timestamps remain
    contiguous across the event window.

46. **`manual_smoke_timeline_shows_both_qualities`**
    Steps: open the recordings UI for the camera, navigate to the event window.
    Verify: timeline / player indicates main-stream availability overlay for the event
    range (from `/recordings/main_availability`); clicking within the range plays the
    higher-quality source; outside the range it falls back to substream.

47. **`manual_smoke_go2rtc_connection_released`**
    Steps: watch `go2rtc` UI / logs during and after the event.
    Verify: the mainstream RTSP consumer disconnects within ~`post_capture` seconds of
    motion ending (confirms reactive event-only recording works as documented in
    `event_recorder.py` docstring).

48. **`manual_smoke_ffmpeg_crash_recovery`**
    Steps: during an active event, `kill -9` the `event_record` ffmpeg PID
    (identifiable via its `@main@` cache path).
    Verify: `frigate.log` shows "exited unexpectedly" followed by
    "Restarting FFmpeg event recording"; a new `_main.mp4` file appears and playback of
    the window is still continuous (possibly with a small gap).

49. **`manual_smoke_retention_keeps_main_regardless_of_mode`**
    Steps: set `record.continuous.days=0`, `record.motion.days=0`,
    `record.alerts.retain.days=7`, `record.event_recording.retain.days=7`. Generate a
    motion-only event (no tracked object).
    Verify: after cleanup pass runs, the `_main.mp4` segment persists even though a
    substream `motion=0`/`objects=0` segment would be discarded.

50. **`manual_smoke_retention_cleans_up_expired_main`**
    Steps: backdate a `_main.mp4` file and its DB row beyond retention.
    Verify: next cleanup pass removes both file and row; substream retention behaves
    independently (not affected by main retention).

51. **`manual_smoke_config_reload_no_regression`**
    Steps: toggle `record.event_recording.enabled` via config reload.
    Verify: disabling stops spawning new `_main.mp4` files within post_capture seconds;
    enabling starts them on next motion. No process crash.

52. **`manual_smoke_api_shape_spot_check`**
    Steps: `curl http://nvr/api/<camera>/recordings?after=...&before=...` (default
    quality), then with `?quality=main`, then `/recordings/main_availability`.
    Verify: keys include `stream_quality`; `quality=main` returns a strict subset;
    `main_availability` returns `[{start_time, end_time}, ...]` covering the event
    windows only.

---

## Totals

- Unit tests proposed: **33** (tests 1–33)
- Integration checks proposed: **9** (tests 34–42)
- Manual smoke tests proposed: **10** (tests 43–52)
- **Grand total: 52**

## Notes / gaps flagged during review

- `migrations/036_add_stream_quality.py` has an intentional no-op `rollback()`. If
  downgrade support is ever required, add a `DROP COLUMN`-equivalent (SQLite requires
  table rebuild) — covered as test 11 documenting current behaviour.
- `frigate/record/cleanup.py` was not verified to filter by `stream_quality`. Test 33
  is a placeholder / xfail until the cleanup path is confirmed or updated.
- `EventRecorder` uses `DetectionSubscriber` only (no explicit config-reload subscriber).
  If `event_recording.enabled` is toggled at runtime, the currently running thread keeps
  its old `post_capture` value. Manual test 51 will catch this; consider a follow-up
  unit test + fix if hot-reload is required.
- No pre-capture ring buffer for the mainstream (documented trade-off in
  `event_recorder.py` docstring lines 39-45). Manual test 44 deliberately accepts the
  1-2 s front-edge gap.
