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
git add downstream/dual-stream.patch
git commit -m "Refresh dual-stream patch for upstream release"
git push -u origin HEAD
```

The `Dual-stream release bench` workflow runs automatically on downstream/update
branches. Only a candidate that passes both benches is published to GHCR under
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
| UI | TypeScript build; desktop/mobile HD/SD live and History controls and export choices |

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
5. Explore opens HD event clips; exports produce a playable HD file when present
   and SD when forced. Check a long History range for nginx VOD clip-limit errors.
6. Review recent logs for recording backlog, ffmpeg errors, invalid segments and
   GPU failures. Record inference timings without claiming the unit bench tested GPU.

Use `downstream/live_uat.py` from an isolated LAN-enabled container for the API,
recording and media checks. UI and camera-trigger checks need their own evidence.
Do not report complete UAT if a camera event or browser flow was not exercised.

Rollback through Portainer to the recorded old image. If migrations changed the
schema incompatibly, restore the matching consistent database and config backup
before starting the old image. Keep the update branch and test report for review.
