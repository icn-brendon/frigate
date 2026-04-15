# Review: `feature/dual-stream-recording`

Branch: `feature/dual-stream-recording` @ `3225ec76`
Base: upstream `dev` @ `15ac76f2`
Feature commit: `843c10b6 Add dual-stream recording support`

## Mechanism (summary)

The branch adds a **second ffmpeg input role**, `record_events`, which is expected to be bound to the mainstream. The existing `record` role continues to run continuously on the substream as a separate ffmpeg process, writing segments `{camera}@{ts}.mp4` into `CACHE_DIR`. A new `EventRecorder` thread (`frigate/record/event_recorder.py`), spawned inside `RecordProcess`, subscribes to video detection events (`DetectionTypeEnum.video`) and **spawns/kills the mainstream ffmpeg process reactively**: when motion or a non–false-positive, non-motionless tracked object appears it runs `start_or_restart_ffmpeg`, and after `post_capture` seconds of no activity it calls `stop_ffmpeg`. The mainstream writes into cache with a distinguishing name, `{camera}@main@{ts}.mp4`. `RecordingMaintainer` parses both formats, tags segments with `stream_quality ∈ {"sub","main"}`, persists the value in a new `Recordings.stream_quality` column (migration 036) and stores main segments under filenames suffixed `_main.mp4`. Main segments bypass the usual continuous/motion retention filter (treated as `RetainModeEnum.all`). API endpoints (`/vod/...`, `/<cam>/recordings`, `/<cam>/recordings/main_availability`) accept a `quality` query param and filter on `stream_quality`, and the UI exposes an SD/HD toggle in `RecordingView` and a small main-stream availability stripe in the motion timeline.

## Config required to enable

```yaml
cameras:
  front_door:
    enabled: true
    ffmpeg:
      inputs:
        - path: rtsp://user:pass@10.0.0.10:554/substream
          roles: [detect, record]          # continuous substream recording
        - path: rtsp://user:pass@10.0.0.10:554/mainstream
          roles: [record_events]           # main stream, started only on events
    detect:
      width: 640
      height: 360
    record:
      enabled: true
      retain:
        days: 7
        mode: motion
      event_recording:                     # NEW block
        enabled: true
        pre_capture: 5                     # advisory only — see "pre_capture bug"
        post_capture: 10
        retain:
          days: 30
          mode: motion
```

Validation notes: `record` and `record_events` must be on **different** inputs (enforced in `CameraFfmpegConfig.validate_roles`). `record_events` ffmpeg output args can be overridden via `ffmpeg.output_args.record_events` (defaults to the same preset as `record`).

## Correctness concerns

1. **`pre_capture` is silently non-functional.** `EventRecorderConfig.pre_capture` is defined, but the mainstream ffmpeg process is only started *after* the first detection arrives. `event_recorder.py` itself acknowledges this in its docstring but the config still exposes a `pre_capture` field with a default of 5 that is never consumed. This is a UX footgun — users will assume it works. Either drop the field, route it into a go2rtc-side buffer, or keep a short rolling mainstream process.
2. **Detection gating may never trigger on motion-only setups.** The "has_objects" clause requires `motionless_count == 0`, and `has_motion` requires non-empty `motion_boxes`. For cameras using object detection with long tracking tails, once an object becomes motionless, recording can stop mid-event even though the object is still present. Consider OR-ing against `len(current_tracked_objects) > 0` with a false-positive filter only.
3. **Segment-boundary race.** Mainstream ffmpeg emits HLS segments at `CACHE_SEGMENT_FORMAT` cadence (10 s). If the event ends mid-segment, `stop_ffmpeg` may discard the in-progress `.mp4`, truncating the tail by up to one segment. Likewise the first segment after start contains a partial pre-event tail (0 – `CACHE_SEGMENT_FORMAT` seconds of "coverage"), not a deterministic `pre_capture` window.
4. **Main segments bypass all retention-mode filtering.** `stream_quality == "main"` shortcuts to `RetainModeEnum.all` and ignores `record.event_recording.retain.mode` even though the config exposes it. The `retain.days` also never gets enforced by the housekeeper path shown — verify `cleanup.py` / the retention maintainer queries are `stream_quality`-aware, otherwise main segments will live forever (or be deleted with substream rules, whichever is worse).
5. **Migration safety.** 036 uses `ADD COLUMN ... NOT NULL DEFAULT 'sub'` which SQLite accepts but rewrites the table; OK for this codebase's size. `rollback()` is a no-op — acceptable since peewee migrations are forward-only here. The new index `(camera, stream_quality, start_time)` duplicates columns already covered by an existing `(camera, start_time)` index; minor but not a bug.
6. **`recordings_summary` is substream-only** (hard-coded `stream_quality == "sub"`). If a user has only main segments for some hour, the summary will under-report. Probably intentional but worth a comment.
7. **UI "main available" signal is wrong.** `RecordingView.getMainStreamAvailability` infers availability from **review items**, not from the new `/recordings/main_availability` endpoint it added on the backend. So the timeline stripe shows where events happened, not where main segments actually exist on disk. The endpoint is defined server-side but not called.
8. **ffmpeg_cmds lookup by role.** `record.py` does `if "record_events" in cmd_entry["roles"]` — `roles` is a list of `CameraRoleEnum` enum values; `"record_events" in [...]` works because `CameraRoleEnum` is `(str, Enum)`, but it's fragile. Prefer `CameraRoleEnum.record_events`.
9. **Process lifecycle.** `EventRecorder` is a `threading.Thread` (daemon) inside `RecordProcess`, not a managed subprocess. If the maintainer crashes, event recording goes with it, and vice versa. Consider whether the two should be independent processes.
10. **Typo / naming.** Column comment says `"sub" or "main"` but no enum or CHECK constraint — a stray value (`"SUB"`, `"main "`) from a future caller would silently break playback filtering.

## Test gaps

No tests reference `stream_quality`, `event_recording`, `record_events`, or `EventRecorder`. Specifically missing:
- Unit test for segment-name parsing (`camera@main@ts` vs `camera@ts`, plus edge cases like a camera name containing `@`).
- Test that `event_recording.enabled` without a `record_events` input logs a warning and doesn't start the thread.
- Validator test for the "record and record_events on same input" rejection.
- Maintainer test that a `stream_quality="main"` segment is retained independently of substream retention mode.
- Migration forward/idempotency test.
- API test that `?quality=main` returns only main rows.

## Minor bugs / nits

- `EventRecorder._process_detection_events` unpacks 6 elements from `data`; if the publisher schema ever grows this silently breaks with a `ValueError` that's caught by nothing.
- `_check_ffmpeg_health` restart branch can double-close the logpipe (`close()` then reassign); harmless but noisy.
- `RetainModeEnum.all` import in `maintainer.py` should come from `frigate.config.camera.record` rather than being passed as a magic string through `move_segment`.
- `DynamicVideoPlayer` appends `?quality=main` naively; if the VOD URL grows query params upstream this will produce `??` or `&`-mismatch URLs.
- `stream_quality` default of `"sub"` assumes every legacy row is substream — correct for fresh installs, fine in practice.
