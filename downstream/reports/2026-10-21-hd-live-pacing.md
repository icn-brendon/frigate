# Native HD live-view pacing, 21 October 2026

The earlier release UAT verified decoded pictures and controls, but did not
measure sustained smoothness. The follow-up timing bench reproduced recurring
pauses despite an average of 20 FPS.

## Isolation

Both cameras are Reolink RLC-1240A, hardware `IPC_NT5NA512MP`, firmware
`v3.1.0.3681_2406248474`. Their original main streams are 4512 x 2512 HEVC,
20 FPS, with AAC-LC audio at 16000 Hz and no B-frames. Camera reads confirmed
fixed frame rate, CBR, a 1x GOP and disabled 3D noise reduction. No camera
settings or firmware were changed.

Simultaneous direct-camera and go2rtc probes showed matching timing gaps.
Gate reached 746 ms directly and 744 ms through go2rtc; Driveway reached 467 ms
directly and 484 ms through go2rtc. TCP and UDP both reproduced the problem.
HTTP and HTTPS FLV returned headers but did not produce usable main-stream
video within the probe timeout on this firmware. Those alternatives were not
deployed. The evidence isolates the bursts upstream of Frigate's restream,
rather than to the dual-stream recording patch.

Correcting video timestamps alone produced 50 ms presentation intervals but
left bursty delivery. Pacing that stream while retaining the original audio
clock still stalled. Normalizing both clocks and pacing the remuxed output
passed in a disposable go2rtc replica using the exact proposed command.

## Applied configuration

- `<camera>_source` retains the original camera URL and feeds `record_events`.
- `<camera>_main` is an on-demand live copy with normalized timestamps and paced
  output. Both FFmpeg stages use streamcopy, preserving video and audio codecs.
- The existing `<camera>_HD` Home Assistant transcode reads the paced main.
- SD recording, detection at 5 FPS, event retention and HD recording resolution
  remain as configured.

The current running application image remains the accepted `13cc0644-rocm`
digest in the release report. This fix changes the managed camera configuration,
not the application image or the portable dual-stream feature patch.

The original config is retained at:

```text
/tank/apps/frigate/config/upgrade-backups/20261021T035724Z/config.before-hd-pacing.yml
```

The config was replaced atomically with its existing owner/mode. Frigate and
its dependent relay were restarted. The original HD recording input now reads
the source alias directly, keeping live timestamp correction out of recordings.

## Acceptance evidence

The same `live_stream_timing.py` command failed before the change and passed
after it. The pre-change sample was 20 seconds; the post-change sample was
180 seconds, excluding startup probing.

| Camera | Before: max arrival gap | Before: gaps over 150 ms | After: max arrival gap | After: gaps over 150 ms | After FPS |
| --- | --- | --- | --- | --- | --- |
| Gate | 272.6 ms | 20 | 79.0 ms | 0 | 20.00 |
| Driveway | 224.6 ms | 14 | 79.5 ms | 0 | 20.00 |

Afterward each camera delivered 3602 frames during the observed 180.03 seconds.
Every presentation interval was 50 ms. The benchmark exited 1 before the fix
and 0 afterward.

Other checks passed:

- Ten decoded frames from a captured Gate fixture were identical before and
  after timestamp correction. Fixture media stayed inside disposable `/tmp`.
- Both deployed native HD streams decoded real, non-grey 4512 x 2512 pictures,
  retaining HEVC Main, zero B-frames and AAC-LC at 16000 Hz.
- Desktop and emulated-mobile Live SD-HD-SD and History SD-HD-SD passed for
  both cameras against real APIs.
- All four Home Assistant relay streams received and decoded actual frames.
- Both cameras processed at 5 FPS with zero skipped frames; fresh SD and HD
  recordings and VOD metadata were present after the restart. AMD inference
  measured 12.09 ms in that snapshot.
- The final repeat required new HD after the config change and passed for both
  cameras. Processing measured 5.0/5.1 FPS, skips remained zero and inference
  measured 11.42 ms. Fresh SD and HD HLS samples decoded at their original
  896 x 512 and 4512 x 2512 resolutions.
- Frigate and the relay were healthy. One settled resource snapshot with both
  paced native streams active measured 41.54% CPU and 4.07 GiB container memory.

NAS tests used disposable AMD64 containers from
`ghcr.io/icn-brendon/frigate:13cc0644-rocm`, whose accepted digest is recorded in
the release report. Commands streamed the Python scripts to `python3 -` on the
internal `traefik_proxy` network, with read-only roots, dropped capabilities,
resource limits and temporary storage. No production recording or database
directory was mounted. Schema validation ran offline with copied model metadata
and a model-file fixture; real detection was verified on the running service.
Browser/relay clients ran in disposable ARM64 containers through temporary
localhost-only SSH signaling tunnels. Test containers and tunnels were removed.

## Limits and future checks

This corrects the native live-view presentation path. Original recordings retain
the camera's original timestamps. The three-minute run does not certify
day-long stability or physical Safari/iPhone decoding. Camera-to-screen latency
was not measured; the pacing stage adds buffering. Clock normalization resets
each track at its first packet; physical audio/video synchronization was not
measured. Recheck timing, codecs,
frame rate, AAC frame size and absence of B-frames after camera/firmware changes.

Future Frigate updates must keep the managed source/live alias split and run
the sustained timing script. A single decoded frame or an average-FPS snapshot
is insufficient acceptance evidence.

References: [Frigate camera recommendations](https://docs.frigate.video/configuration/camera_specific/#reolink-cameras),
[FFmpeg timestamp bitstream filter](https://ffmpeg.org/ffmpeg-bitstream-filters.html#setts).
