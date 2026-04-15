# UAT Plan — Dual-Stream Recording (`feature/dual-stream-recording`)

Target environment: **https://nvr.saxon.email**
Branch commits under test: `843c10b6` (feature) + `3225ec76` (UAT image plumbing), branched from `15ac76f2`.
Image tag: whatever is deployed by the UAT workflow at cutover. Capture the exact image digest (`docker inspect`) before starting.

This plan is written so a human operator or a browser-automation agent can step through it. Each step lists **Action → Expected → Verify**. Numbered checkboxes are the only things you need to check off.

---

## 0. Branch behaviour — what changed (reference)

This is so the operator knows what they're validating.

### Backend / DB
- **New migration `036_add_stream_quality.py`** adds `stream_quality VARCHAR(10) NOT NULL DEFAULT 'sub'` to the `recordings` table and creates index `recordings_stream_quality (camera, stream_quality, start_time)`. Rollback is a no-op.
- **`Recordings.stream_quality`** model field (`"sub"` or `"main"`).
- **New ffmpeg role `record_events`** (must be on a *different* input than `record`; validator rejects both on the same input).
- **New config block `record.event_recording`** with `enabled`, `pre_capture` (default 5 s), `post_capture` (default 10 s), `retain`.
- **New `EventRecorder` thread** (`frigate/record/event_recorder.py`) subscribes to detection events; starts an ffmpeg process on the main stream only while motion or non-stationary tracked objects exist, stops it `post_capture` seconds after last activity. Cache segments are named `camera@main@timestamp.mp4`. Documented caveat in the class docstring: **the first ~1–2 s of an event may be sub-only** because the main-stream ffmpeg is started reactively (no true `pre_capture` on main).
- **`RecordingMaintainer`** parses the extra `@main@` segment and stores it with `stream_quality="main"`, filename suffixed `_main.mp4`, and with `RetainModeEnum.all` (main segments are never dropped by motion/continuous filters).

### API
- `GET /api/<camera>/recordings?after=&before=&quality=` — `quality` defaults to `"sub"`; response now includes `stream_quality` per row.
- `GET /api/<camera>/recordings/summary` — filtered to `stream_quality == "sub"` (summary = substream timeline).
- `GET /api/<camera>/recordings/main_availability?after=&before=` — **new**, returns `[{start_time, end_time}, …]` ranges where main segments exist.
- `GET /vod/<camera>/start/<start>/end/<end>/master.m3u8?quality=main|sub` — HLS playlist filtered by quality. `vod_hour` and `vod_clip` both accept `quality`. Note: `vod_ts` hard-filters `Recordings.stream_quality == stream_quality` — so an HD request over a gap returns an empty playlist, not a sub fallback at the API layer.

### UI
- `RecordingView.tsx` has a new **SD/HD toggle button** (FaVideo icon + “SD”/“HD” label). When toggled to HD over a time with no main segment, it shows a `!` warning badge and a tooltip “Main stream selected but unavailable for current time - falling back to substream”. (The fallback is purely UX language — the playlist will be empty; see risk §R1.)
- `DynamicVideoPlayer` appends `?quality=main` to the VOD playlist URL when `streamQuality==="main"` and re-sources on quality change.
- `MotionSegment.tsx` draws a 3 px vertical bar on the right edge of a segment when `hasMainStream` is true (colour: `bg-selected/70`).
- `getMainStreamAvailability(timestamp)` is currently **computed from `reviewItems`** on the main camera — it does *not* hit `/recordings/main_availability`. The new API endpoint exists but is not yet wired into the timeline (see risk §R2).

---

## 1. Pre-deploy baseline (current prod, BEFORE upgrade)

Goal: capture artefacts that let you prove nothing regressed. Pick one representative camera with recent motion — call it `$CAM` throughout. Pick a 1-hour window with known motion — call it `$AFTER` / `$BEFORE` (unix timestamps).

| # | Action | Expected | Verify |
|---|---|---|---|
| 1.1 | Note image digest: `docker inspect --format '{{.Image}}' frigate` on the host. | A sha256 value. | Save to `baseline/image-before.txt`. |
| 1.2 | Load `https://nvr.saxon.email/recordings/$CAM` in browser. | Timeline renders, motion segments visible. | Full-page screenshot → `baseline/timeline-$CAM.png`. |
| 1.3 | Scrub to a known-motion timestamp and press play. | Video plays, no console errors. | DevTools → Network: capture the `master.m3u8` URL + response → `baseline/vod-playlist.txt`. |
| 1.4 | Export a 2-minute clip via the UI export dialog for $CAM. | Export completes; file downloads. | Save the mp4 → `baseline/export.mp4`. Record duration + size. |
| 1.5 | `curl -s "https://nvr.saxon.email/api/$CAM/recordings?after=$AFTER&before=$BEFORE" > baseline/recordings.json` | HTTP 200, JSON array of segments. | File saved. Note: no `stream_quality` key on rows (pre-upgrade). |
| 1.6 | `curl -s "https://nvr.saxon.email/api/$CAM/recordings/summary" > baseline/summary.json` | HTTP 200, JSON array of per-hour rollups. | File saved. |
| 1.7 | Open Review page, pick a recent review item, play it. | Plays normally. | Screenshot → `baseline/review.png`. |
| 1.8 | Open live view for $CAM. | Stream starts. | Screenshot → `baseline/live.png`. |

Check all 8 before proceeding to deploy.

---

## 2. Config migration sanity (immediately after deploy)

Pre-condition: UAT image is running; config has `record.event_recording.enabled: false` on **all** cameras (i.e. new feature dormant). This proves migration + backwards compat *in isolation* from the recorder.

| # | Action | Expected | Verify |
|---|---|---|---|
| 2.1 | `docker logs frigate 2>&1 \| grep -iE "migrat\|036"` | Line similar to `036_add_stream_quality` applied; no `Traceback`. | No error entries. |
| 2.2 | Exec into container: `sqlite3 /config/frigate.db ".schema recordings"` | `stream_quality VARCHAR(10) NOT NULL DEFAULT 'sub'` column present. | Column exists. |
| 2.3 | `sqlite3 /config/frigate.db "SELECT DISTINCT stream_quality FROM recordings"` | Returns only `sub` (all historical rows back-filled by default). | No `NULL`, no `main`. |
| 2.4 | `sqlite3 /config/frigate.db ".indexes recordings"` | `recordings_stream_quality` index listed. | Index present. |
| 2.5 | Load `https://nvr.saxon.email/recordings/$CAM` — pick a date *before* the upgrade. | Timeline renders as in baseline 1.2; segments line up visually. | Diff screenshot vs `baseline/timeline-$CAM.png` — should be pixel-equivalent modulo the new SD/HD button in the header. |
| 2.6 | Play historical footage. | Video plays. | Confirm network shows the VOD playlist **without** `?quality=` param (default = sub). |
| 2.7 | `curl -s "https://nvr.saxon.email/api/$CAM/recordings?after=$AFTER&before=$BEFORE" \| jq '.[0]'` | Each row now has `"stream_quality":"sub"`. Otherwise identical to baseline. | `diff` the shape against `baseline/recordings.json`. |
| 2.8 | `curl -s "https://nvr.saxon.email/api/$CAM/recordings/summary"` | Unchanged from baseline summary (summary is sub-only by definition now). | `diff` vs `baseline/summary.json`. |
| 2.9 | `curl -s "https://nvr.saxon.email/api/$CAM/recordings/main_availability?after=$AFTER&before=$BEFORE"` | HTTP 200; empty array `[]` because event_recording is still off. | Empty array. |
| 2.10 | Click the new **SD** button in the recordings header. | Button toggles to **HD** with `!` warning badge (no main segments exist). Tooltip shows the fallback-unavailable message. Playlist request is re-issued with `?quality=main` and returns an **empty playlist**; player either shows nothing or errors. | Network: `…/master.m3u8?quality=main`. This is the documented behaviour for now — note in report if it renders gracefully or throws. |
| 2.11 | Toggle back to SD. | Playback resumes. | Network re-fetches playlist without `quality=main`. |

---

## 3. Happy-path dual-stream

Pre-condition: pick one camera (call it `$DUAL`) and configure **both** inputs in its ffmpeg block:

```yaml
cameras:
  $DUAL:
    ffmpeg:
      inputs:
        - path: rtsp://…substream
          roles: [detect, record]        # continuous sub recording
        - path: rtsp://…mainstream
          roles: [record_events]         # event-only main recording
    record:
      enabled: true
      event_recording:
        enabled: true
        pre_capture: 5
        post_capture: 10
```

Restart Frigate. Verify at startup:

| # | Action | Expected | Verify |
|---|---|---|---|
| 3.0a | `docker logs frigate 2>&1 \| grep -i "event recorder"` | `Starting event recorder for cameras: ['$DUAL']` | Log line present. |
| 3.0b | `docker logs frigate 2>&1 \| grep -i "no record_events role"` | Nothing for $DUAL. | Absent. |
| 3.0c | `ls /tmp/cache/ \| head` inside container | Initially only `$DUAL@<ts>.mp4` sub-segments; no `@main@` yet. | No main-stream ffmpeg running. Confirm `pgrep -af 'event_record\|record_events' \| wc -l` is 0 while idle. |

### 3a. No motion (2 minutes)

| # | Action | Expected | Verify |
|---|---|---|---|
| 3a.1 | Physically/logically ensure $DUAL sees no motion for 2 minutes (cover lens / static scene). | Sub segments accrue once per segment duration; no main ffmpeg spawns. | Log: no `Started main stream event recording`. `ps` in container shows no extra ffmpeg for $DUAL's main URL. |
| 3a.2 | After 2 min: `curl -s ".../api/$DUAL/recordings?after=<2minago>&before=now" \| jq '[.[].stream_quality] \| unique'` | `["sub"]` only. | Unique set is `["sub"]`. |
| 3a.3 | `curl -s ".../api/$DUAL/recordings/main_availability?after=<2minago>&before=now"` | `[]` | Empty array. |
| 3a.4 | Open recordings UI for $DUAL, zoom timeline into the 2-min window. | No right-edge blue bar on any motion segment; no HD indicator. | Screenshot; grep DOM for `bg-selected/70` → none. |
| 3a.5 | Toggle HD — expect `!` warning; toggle back. | As in 2.10/2.11. | Verified. |

### 3b. Induce motion (single event)

| # | Action | Expected | Verify |
|---|---|---|---|
| 3b.1 | Trigger motion at time `T` and hold for 15 s. | Logs: `Started main stream event recording for $DUAL (pid N)` within ~1 s of motion. | Log present; note actual lag. |
| 3b.2 | Watch `ls /tmp/cache/` | File `$DUAL@main@<epoch>.mp4` appears. | File exists and grows. |
| 3b.3 | 10 s after motion ends (post_capture): log `Stopped main stream event recording for $DUAL (ran for ~Ns)`. | Stop log. Main ffmpeg gone. | `pgrep` shows it gone. |
| 3b.4 | Wait one segment-flush cycle, then `sqlite3 /config/frigate.db "SELECT start_time, end_time, stream_quality, path FROM recordings WHERE camera='$DUAL' AND start_time > <T-5> ORDER BY start_time"` | Rows of both `sub` and `main`. Main filename ends `_main.mp4`. Main rows cover roughly `[T, T+event_duration+10s]`. | Rows visible. |
| 3b.5 | `curl ".../api/$DUAL/recordings/main_availability?after=<T-60>&before=<now>"` | Non-empty array bounding the event window. | JSON array with ≥1 entry overlapping `T`. |
| 3b.6 | Reload UI `/recordings/$DUAL`, scrub to `T`. | Motion segments covering the event have a **3 px blue vertical bar on the right edge** (the `hasMainStream` indicator). | Visual; verify in DOM. |
| 3b.7 | SD/HD button: still **SD** by default. Tooltip = “Playing substream (SD)…”. | Correct default. | Tooltip copy matches. |
| 3b.8 | Toggle to **HD** while playhead is inside the event window. | Button goes to HD without `!` badge. Player re-loads source with `?quality=main`. Playback visibly higher resolution. | Network: `…/master.m3u8?quality=main` returns non-empty playlist. Video element resolution increases (`videoWidth/videoHeight` in console). |
| 3b.9 | While HD, scrub to a non-event time still within the same timeline view. | `!` badge appears. Playlist request for that range returns empty; player either stalls or errors. | Document actual behaviour (stall vs error). |
| 3b.10 | Toggle back to SD. | Resumes normal playback across the whole range. | Verified. |

### 3c. Substream continuity through the event

| # | Action | Expected | Verify |
|---|---|---|---|
| 3c.1 | Query sub recordings across `[T-30, T+60]`. | Continuous sub coverage, no gap during the event. | No gaps > one segment duration in `start_time`/`end_time` sequence. |
| 3c.2 | In SD mode, scrub across `[T-30, T+60]`. | Seamless playback. | No visible interruption. |

---

## 4. Edge cases

| # | Scenario | Action | Expected | Verify |
|---|---|---|---|---|
| 4.1 | **Motion at segment boundary (start)** | Induce motion within 2 s of the start of a new sub-segment. | Main segment starts within ~1 s of motion; sub segment is normal. Main covers from ~motion-start, **not** 5 s earlier (documented — no true pre_capture on main). | DB rows + ffmpeg start log. Record the actual lag. |
| 4.2 | **Motion at segment boundary (end)** | Induce motion that ends ~1 s before sub segment cutover. | Main ffmpeg keeps running for `post_capture=10 s`, producing a main segment that spans the sub boundary. Maintainer correctly parses `camera@main@ts.mp4` either way. | No “Skipping unexpected files in cache” warning. Main row's end_time covers post_capture window. |
| 4.3 | **Rapid on/off motion** | Trigger motion bursts every 3 s for 2 min (bursts shorter than `post_capture`). | Main ffmpeg stays running continuously — `last_activity_time` keeps getting bumped before timeout. **Single long** main recording, not many short ones. | Log: exactly one “Started …” and one “Stopped …” for the full 2-min window. |
| 4.4 | **Gap within rapid motion** | Let motion pause for >10 s, then resume. | First main ffmpeg stops at pause+10 s; a **new** main ffmpeg starts on resume (new pid). Two separate main DB rows. | Two start/stop log pairs; two main rows in DB. |
| 4.5 | **Long sustained motion (5 min)** | Hold continuous motion for 5 min. | Single main ffmpeg process spanning the whole window; maintainer flushes multiple main cache segments (one per `CACHE_SEGMENT_FORMAT` duration). No ffmpeg restart mid-event. | One start log; main pid stable; multiple `$DUAL@main@*.mp4` cache files all attributed to the same process. |
| 4.6 | **Retention — main vs sub** | Set `record.retain.days=1` on $DUAL; set `record.event_recording.retain.days=7`. Wait 24 h + 1 cleanup pass (or fast-forward test DB). | Sub segments older than 24 h are pruned; main segments survive for 7 days. Main segments are **not** affected by the motion/continuous filter since maintainer returns `RetainModeEnum.all` for them. | `SELECT stream_quality, MIN(start_time), COUNT(*) FROM recordings GROUP BY stream_quality` shows main oldest timestamp > sub oldest timestamp. |
| 4.7 | **Config validator — conflicting roles** | Put `record` *and* `record_events` on the same ffmpeg input. Restart. | Frigate fails validation with `The record and record_events roles must not be on the same input.` | Container exits / logs validation error. |
| 4.8 | **event_recording enabled but no record_events role** | Set `event_recording.enabled: true` but leave out the second input. Restart. | Frigate **still starts**, but logs `Camera $DUAL has event_recording enabled but no record_events role configured…`. No main segments are recorded, UI behaves as if feature is off for that camera. | Warning log present; no `@main@` cache files. |
| 4.9 | **Crash/recovery of main ffmpeg mid-event** | While main ffmpeg is running, `kill -9` it. | `_check_ffmpeg_health` (in EventRecorder) notices; state resets so a subsequent motion frame restarts it. Maintainer still flushes partial `@main@` cache to DB. | Log entry about ffmpeg exit; subsequent motion produces a new main recording. |
| 4.10 | **Unexpected cache filename** | Manually drop a bogus file `/tmp/cache/garbage.mp4`. | Maintainer logs `Skipping unexpected files in cache` exactly once (flag is sticky), processes real files normally. | Log contains the warning once per run cycle; other cameras unaffected. |

---

## 5. Regression checks (must all still work)

| # | Action | Expected | Verify |
|---|---|---|---|
| 5.1 | Export a 2-minute clip from $CAM (non-dual camera) via UI. | Export completes; mp4 byte-compare *structure* (duration, codec) matches `baseline/export.mp4`. | `ffprobe` both files, compare streams. |
| 5.2 | Export a 2-minute clip from $DUAL in SD. Then export same range in **HD** mode (if UI exposes a quality choice in export — today it does not; export follows the VOD default which is sub). | SD export succeeds. HD export either (a) produces an HD file covering only the event portion or (b) is not exposed. Document which. | If HD option isn't in export UI, explicitly note it in the report. |
| 5.3 | Download a single clip from a review item on $DUAL. | Download works. | File plays in VLC. |
| 5.4 | Open live view on $CAM and $DUAL. | Both streams play. | No console errors. |
| 5.5 | Open Review page — confirm review items still render with correct thumbs and the mini-timeline. | Unchanged from baseline 1.7. | Screenshot diff. |
| 5.6 | Bulk: toggle motion-only, full-timeline, zoom levels on $DUAL's timeline. | All work; HD indicator bar scales correctly at all zoom levels. | Visual at 3 zoom levels. |
| 5.7 | Summary API shape unchanged (2.8 already did this — re-verify after 24 h of data). | Still matches baseline. | `diff` of `jq 'keys'` output. |
| 5.8 | `/api/<camera>/recordings/summary` is sub-only — double check that review/heatmap pages using it haven't lost rows because main was never included anyway. | No visible regression. | Compare row counts per day vs baseline. |

---

## 6. Sign-off

- [ ] Section 1 — 8/8 baselines captured and archived.
- [ ] Section 2 — 11/11 migration checks green.
- [ ] Section 3 — 3a, 3b, 3c all green.
- [ ] Section 4 — all 10 edge cases executed; any deviation documented.
- [ ] Section 5 — 8/8 regression checks green.
- [ ] Image digest of passing build recorded.
- [ ] Any failures logged with camera, timestamp, log excerpt.

---

## Appendix — Top 5 risky scenarios (for reviewer focus)

1. **HD playback over a gap returns an empty HLS playlist, not a sub fallback.** The UI tooltip promises "falling back to substream" but `vod_ts` hard-filters on `stream_quality`. Step 3b.9 and 2.10 exercise this. Expect a visible stall/error if the user scrubs into non-event time while HD is selected.
2. **Timeline's `hasMainStream` is computed from review items, not from `/main_availability`.** If a main segment exists without a corresponding review item (e.g. motion that didn't clear detection thresholds) the blue indicator won't show, and conversely a review item without a main segment (first ~1–2 s lag, or ffmpeg crash) will show the indicator but HD will be empty. The new API endpoint exists but is unwired — easy regression surface. Exercised implicitly throughout §3 and §4.1, §4.9.
3. **First ~1–2 s of every event is sub-only by design.** Documented in the `EventRecorder` docstring but users will report it as a bug. §4.1 verifies the behaviour and magnitude of the lag.
4. **Retention asymmetry** — main segments are stored with `RetainModeEnum.all` and the main branch in `maintainer.async_process_recording` short-circuits all motion/continuous filtering. If `event_recording.retain` is longer than `record.retain`, main segments persist past their sub context, meaning HD-only playback windows with no sub continuity around them. §4.6 verifies this but the UX implication is worth flagging.
5. **Config validator + startup behaviour** — the two failure modes (§4.7 hard-fail, §4.8 soft-warn) are asymmetric. A user who enables `event_recording` but forgets to add the second input gets a warning buried in logs and a silently-dead feature with no UI indication. Recommend future work to surface this in the UI.
