---
id: dual_stream_recording
title: Dual-stream Recording
---

import ConfigTabs from "@site/src/components/ConfigTabs";
import TabItem from "@theme/TabItem";

Dual-stream recording pairs a continuous substream with a triggered mainstream so that 24/7 context is kept at SD resolution and HD footage is only written while a review item is active. This trades a small amount of complexity for a large reduction in storage and write IOPS compared with 24/7 HD recording.

## Storage model

| Stream     | Role            | When written     | Retention governed by                |
| ---------- | --------------- | ---------------- | ------------------------------------ |
| Substream  | `record`        | Continuously     | `record.continuous`, `record.motion`, `record.alerts`, `record.detections` |
| Mainstream | `record_events` | During events    | `record.event_recording.retain`      |

The substream behaves exactly like a single-stream Frigate install. The mainstream is captured by a dedicated ffmpeg process whose segments are written to a tmpfs ring buffer and only promoted to persistent storage while an alert or detection is active.

## Pipeline

```mermaid
%%{init: {"themeVariables": {"edgeLabelBackground": "transparent"}}}%%

flowchart LR
    Cam[(Camera)]
    Cam -->|substream| Detect[detect + record ffmpeg]
    Cam -->|mainstream| EventRec[record_events ffmpeg]

    Detect -->|segments| SubCache[/tmp/cache]
    EventRec -->|segments| EventCache[/tmp/event_cache]

    SubCache --> Maint[Recording maintainer]
    EventCache --> Maint
    Maint -->|substream: always<br/>mainstream: while event active| Disk[(/media/frigate/recordings)]

    Trigger[Review/detection trigger] --> Maint
```

The `record_events` ffmpeg process is independent of the substream `record` process. It has its own exponential-backoff restart logic so that an unstable mainstream (common on substream/mainstream port separation or cameras that struggle under HD load) does not interfere with detection.

## Configuration summary

See the individual references for details:

- [`record_events` role](cameras.md#setting-up-camera-inputs) — assigning the mainstream input.
- [`record.event_recording`](record.md#event-recording-main-stream) — pre-capture, post-capture, retention.
- [Stationary event recording](objects.md#stationary-event-recording) — preventing parked cars from pinning the main stream open.

Minimum viable config:

```yaml
record:
  enabled: True
  event_recording:
    enabled: True

cameras:
  driveway:
    ffmpeg:
      inputs:
        - path: rtsp://.../main
          roles: [record_events]
        - path: rtsp://.../sub
          roles: [detect, record]
```

## Tmpfs sizing

The ring buffer lives on tmpfs at `/tmp/event_cache` when that path is mounted. If the dedicated mount is not present, Frigate falls back to `/tmp/cache/event_buffer`, which shares space with the substream cache.

Size the tmpfs for the worst case — every camera triggers at once:

```
bytes = mainstream_bitrate (MB/s) × pre_capture (s) × num_cameras × 1.5
```

Running the ring buffer on a regular volume is supported but writes continuously at the full mainstream bitrate of every camera combined, which is typically enough to measurably shorten consumer SSD lifetime. A tmpfs mount eliminates those writes.

An example docker compose fragment:

```yaml
services:
  frigate:
    tmpfs:
      - /tmp/cache:size=1g
      - /tmp/event_cache:size=256m
```

## UI

The Live, History, and Explore views expose an HD/SD toggle that switches the player between the substream VOD (always available) and the mainstream VOD (available only over ranges where main-stream event segments exist). The UI calls `/api/<camera>/recordings/main_availability` to decide when the HD toggle is selectable.

## Known limitations

- `MAX_SEGMENTS_IN_CACHE` is a global cap on how many buffered segments the recording maintainer holds in memory at once. The default (15) is tuned for typical deployments; very high camera counts with long `pre_capture` values may need it raised.
- `/api/<camera>/recordings/main_availability` rejects query windows larger than 7 days and truncates responses at 10,000 rows. The UI pages per-day, so this is only a concern for custom API consumers.
- `record_events` and `record` are mutually exclusive on the same input (enforced by config validation). Cameras that only provide one stream cannot use event recording.
- The HLS VOD endpoint takes stream quality as a path segment (`/quality/{main|sub}/`), not a query parameter, because the nginx-vod upstream subrequest strips query strings.

## HTTP API

| Endpoint                                                                                | Purpose                                                                                                                                                          |
| --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/<camera>/recordings/main_availability?after=<ts>&before=<ts>`                 | Returns `{ranges: [{start_time, end_time}], truncated}`. Max window 7 days, max 10,000 rows. Used by the UI to determine whether the HD toggle is available.    |
| `GET /vod/<camera>/start/<ts>/end/<ts>/quality/{main\|sub}/master.m3u8`                 | HLS playlist for the given range and stream quality. Quality must be a path segment; a `?quality=...` query parameter is ignored by the nginx-vod upstream.       |
