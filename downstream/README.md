# Updating the dual-stream Frigate fork

The feature records SD continuously and keeps HD around activity, including
pre/post capture. Live view, History, Explore, VOD and exports select the
appropriate quality. Retention handles the two qualities separately.

`dual-stream.patch` contains application code and regression tests only.
`release.json` records the exact upstream source commit and matching ROCm image
digest. Build/update tooling stays separate so it is not mixed into feature
conflicts. There is no assumption that a patch will apply to every future release.

## Prepare a release

Start with a clean, committed checkout of the last accepted downstream branch.
Resolve the matching upstream image digest with `docker buildx imagetools inspect`.
Then create an isolated worktree:

```sh
python3 downstream/update.py prepare v0.18.3 ../frigate-v0.18.3 \
  --base-image ghcr.io/blakeblackshear/frigate:0.18.3-rocm@sha256:<digest>
```

The helper fetches the official tag, creates `downstream/v0.18.3`, applies the
patch with Git's three-way merge, copies this process and updates the pinned
base. It does not push, deploy, overwrite an existing worktree, or discard
conflicts. A conflict is a failed preparation that needs code review.

After resolving conflicts, review upstream release notes and dependency changes.
Keep upstream security fixes, authentication checks and schema changes. Pay
particular attention to the recording maintainer, retention, export jobs, go2rtc
generation, nginx VOD limits and frontend quality selectors.

Commit the integrated code in the new worktree, then refresh the portable patch:

```sh
python3 downstream/update.py export-patch
git add downstream/dual-stream.patch downstream/feature-files.json
git commit -m "Refresh dual-stream patch for upstream release"
git push -u origin HEAD
```

The `Dual-stream release bench` workflow runs automatically on downstream/update
branches. Only a candidate that passes patch replay, backend and browser benches is published to GHCR under
an immutable commit-based tag. Source and runtime versions must match; never
build the overlay on a moving `stable-rocm` tag. Open a PR with test results,
candidate digest, UAT evidence and rollback reference.

## Automated acceptance bench

Backend tests run inside a disposable ROCm runtime container without GPU access,
network, real camera credentials, production databases or recording mounts.
Each module gets a separate process so import mocks cannot affect other suites.

| Area | Required evidence |
| --- | --- |
| Configuration | Separate `record` and `record_events` inputs; defaults/bounds; legacy single-stream config |
| Database | Migration 036, SD backfill, quality index, fresh install and existing schema |
| Event buffer | Pre/post capture, no activity, stationary thresholds, timestamps and promotion |
| Ingest | HD/SD filenames and database quality tags, HD survives ingest without matching detect stats |
| Retention | Independent HD/SD expiry; required files survive and expired files disappear |
| Export | Auto HD selection, forced SD, SD fallback and adjacent upstream export regressions |
| UI | Lint and i18n extraction; TypeScript build; desktop/mobile HD/SD live and History controls and export choices |

The legacy retention suite has one documented expected failure for an orphan
file with no database row. This is not covered by row-based cleanup. Keep that
limitation visible and do not treat it as a new release regression.

Local tests must also use disposable containers. Build `downstream/Dockerfile.test`
with the pinned base from `release.json`; run it with the same restrictions and
temporary `/config`, `/media`, `/tmp` paths as the workflow. The browser bench is
`downstream/Dockerfile.web-test`. No production directories are mounted into either.

## Camera and GPU UAT

Automated mocks cannot establish camera codec compatibility or GPU performance.
Before changing the existing UAT Portainer stack, save its stack definition and
environment securely, retain the old image digest, and snapshot/copy the config
and SQLite database using a consistent SQLite backup. Keep credentials out of Git
and reports. A separate bench must use copied config/database and disposable
recordings, never share writable production paths with another Frigate instance.

Deploy the immutable candidate through Portainer, keeping the existing 5 FPS
detection rates and known Reolink software HEVC decode workaround. Verify:

1. Correct candidate version, healthy container, working ROCm inference and both
   cameras processing at 5 FPS without sustained skips or restart loops.
2. SD segments continue arriving for both cameras while HD event buffering runs.
3. An activity event promotes HD with the configured pre/post capture window;
   inspect database quality tags and probe actual files for both resolutions.
4. Live HD/SD and History HD/SD controls work on desktop and mobile. HD gaps fall
   back appropriately in playback; no grey video or frozen player.
   Run the sustained packet/presentation timing check as well. Average FPS and
   one decoded frame do not establish smooth playback.
5. Explore opens HD event clips; exports produce a playable HD file when present
   and SD when forced. Check a long History range for nginx VOD clip-limit errors.
6. Review recent logs for recording backlog, ffmpeg errors, invalid segments and
   GPU failures. Record inference timings without claiming the unit bench tested GPU.

Use `downstream/live_uat.py` from an isolated LAN-enabled container for the API,
recording and media checks. UI and camera-trigger checks need their own evidence.
Do not report complete UAT if a camera event or browser flow was not exercised.

The live checks are supplied as five reusable scripts:

- `live_uat.py --url URL --version-prefix 0.18.2 --cameras Front_Gate Front_Driveway`
  checks processing, quality tags, VOD metadata and fresh SD. Add
  `--require-main-since EPOCH` to require new HD after deployment.
- `live_browser_uat.cjs URL Front_Gate Front_Driveway` runs desktop/mobile Live
  and History controls against real APIs. Run with Node in the browser bench
  image. It selects a known, closed HD range instead of assuming every timestamp
  has HD footage. Mobile means an emulated layout, not physical Safari hardware.
- `live_media_uat.py --url URL --cameras Front_Gate Front_Driveway` probes and
  decodes real HLS samples using FFmpeg inside the disposable ROCm bench image.
  Add `--exports` to create, verify and delete this run's short HD/SD exports.
- `live_relay_uat.py --url URL/api/webrtc --origin https://home.saxon.email
  --streams Front_Gate Front_Driveway Front_Gate_HD Front_Driveway_HD` receives
  and decodes real H.264 WebRTC frames through the Home Assistant adapter.
  Build `downstream/Dockerfile.relay-test` and stream this script to its stdin,
  passing the arguments after the image name. Run with a read-only root,
  temporary `/tmp`, dropped capabilities and LAN networking. The client needs
  access to the SDP endpoint and the relay's advertised UDP candidate. An SSH
  tunnel forwards signaling only. The pinned aiortc client uses a larger receive
  buffer for HD I-frames. Recheck its private buffer API when upgrading aiortc.
- `live_stream_timing.py --streams Front_Gate_main Front_Driveway_main
  --seconds 180` measures sustained native HD packet arrival and presentation
  timestamps. Run it in the disposable ROCm image on `traefik_proxy`, with
  `python3 -` and the script on stdin. It uses the matching bundled FFprobe,
  excludes startup analysis, expects 20 FPS and fails on arrival gaps over
  150 ms or presentation gaps over 75 ms. Override the FPS/threshold arguments
  only for a documented camera requirement, not to hide a regression.

These scripts do not need production filesystem mounts or copied credentials.
On nas01, a disposable container can reach `http://frigate-uat:5000` on the
existing `traefik_proxy` network. Use dropped capabilities, no-new-privileges,
memory/PID limits and a temporary `/tmp`. Stream the script to `python3 -` or
`node -` to avoid mounting the host checkout. For a local browser bench, forward
the internal UI through a localhost-only SSH tunnel and close it after testing.

Wait for the AMD model to finish compiling before running acceptance. Container
health alone does not prove detection is ready. First compilation can take
several minutes and briefly produce cache-backlog warnings. Record both startup
and settled results. Pre-pull the candidate digest with the NAS's registry access
before updating Portainer; `pull_policy: always` can override its no-pull option.

On nas01, `<camera>_source` is the original HEVC/AAC feed used by `record_events`.
`<camera>_main` is a lossless timestamp-normalized, paced live copy. Preserve
both aliases and this separation when updating. The camera-specific workaround
assumes 20 FPS, no B-frames and AAC-LC with 1024 samples per frame. See the
[HD pacing acceptance report](reports/2026-10-21-hd-live-pacing.md) and the
[managed configuration guide](https://github.com/icn-brendon/nas01-compose/blob/update/frigate-0.18.2-dual-stream/frigate-uat/HD-LIVE.md).

## Home Assistant relay after container replacement

The relay shares Frigate's network namespace. Replacing Frigate leaves an
existing relay attached to the old container ID. Recreate the relay through its
Portainer stack after each Frigate replacement and confirm its network mode
references the current Frigate container. Restarting the old relay alone does
not update that namespace reference.

On nas01, native go2rtc must use WebRTC port 18555 and advertise
`10.0.0.70:18555`; the Home Assistant relay owns 8555. Persist this in the managed
Frigate config:

```yaml
go2rtc:
  webrtc:
    listen: ":18555"
    candidates: ["10.0.0.70:18555"]
```

Keep the existing go2rtc streams alongside those settings. Native RTSP remains
8554 and its API remains 1984. The relay API is 1985. Successful SDP signaling or
a healthy container does not prove video arrives. Require decoded SD and HD
frames through the adapter before accepting the update.

Preflight the relay stack's Git credential before the maintenance window. On
21 October its stored credential could not fetch the private repository. The
deployment used an exact-spec relay recreation instead. Repair that credential
before relying on automatic Git redeploy; never substitute an unrelated broad
GitHub token silently.

Rollback through Portainer to the recorded old image. If migrations changed the
schema incompatibly, restore the matching consistent database and config backup
before starting the old image. Keep the update branch and test report for review.
