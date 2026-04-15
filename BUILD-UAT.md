# Frigate UAT Build (dual-stream-recording fork)

This repo ships a `docker-compose.uat.yml` that builds a local image of this
fork and tags it `frigate-uat:dual-stream` for UAT on the NVR host.

## Prerequisites

- Docker Engine 24+ with Compose v2 (`docker compose` subcommand)
- A `./.env.uat` file at the repo root (NOT committed) containing at minimum:
  ```
  FRIGATE_RTSP_PASSWORD=changeme
  ```
- A `./config/config.yml` and a `./media/frigate` directory on the deploy host
  (the compose file mounts these as bind-mounts).

## Build

One-command build of the `frigate` target from `docker/main/Dockerfile`:

```
docker compose -f docker-compose.uat.yml build
```

The resulting image is tagged locally as `frigate-uat:dual-stream`.

## Run

```
docker compose -f docker-compose.uat.yml up -d
docker compose -f docker-compose.uat.yml logs -f frigate
```

To stop:

```
docker compose -f docker-compose.uat.yml down
```

## Deploy option A — build on the NVR host

Clone the fork on the NVR host, then build + run:

```
git clone -b feature/dual-stream-recording https://github.com/icn-brendon/frigate.git
cd frigate
# create ./config/config.yml, ./media/frigate, ./.env.uat first
docker compose -f docker-compose.uat.yml up -d --build
```

This builds `frigate-uat:dual-stream` locally on the NVR and runs it.

## Deploy option B — pull prebuilt image from GHCR (no build on NVR)

The workflow `.github/workflows/uat-image.yml` builds and pushes an amd64 image
to `ghcr.io/icn-brendon/frigate` on every push to the
`feature/dual-stream-recording` branch. Tags produced:

- `ghcr.io/icn-brendon/frigate:<short-sha>` (immutable)
- `ghcr.io/icn-brendon/frigate:feature-dual-stream-recording` (moving tag)

First-time setup:

1. On GitHub → repo → Actions tab, confirm the `UAT image build` run succeeded.
2. Make the GHCR package public (GitHub → your profile → Packages → `frigate`
   → Package settings → change visibility to Public), or configure the NVR to
   authenticate to GHCR via `docker login ghcr.io`.

On the NVR host:

```
# place docker-compose.uat.ghcr.yml, ./config/config.yml, ./media/frigate, ./.env.uat
export FRIGATE_UAT_TAG=<short-sha>     # or leave unset to use the branch tag
docker compose -f docker-compose.uat.ghcr.yml pull
docker compose -f docker-compose.uat.ghcr.yml up -d
```

To roll forward to a new UAT build, update `FRIGATE_UAT_TAG` and repeat
`pull` + `up -d`.

## Hardware acceleration / Coral

The `devices:` block in `docker-compose.uat.yml` is commented out. Uncomment
and adjust the entries that apply to the NVR (Coral USB/M.2, Intel iGPU, etc.).
`privileged: true` is also commented out and should only be enabled as a last
resort; prefer targeted `devices:` entries.

## Notes

- Build target: `frigate` (production runtime; not `devcontainer`).
- `/tmp/cache` is mounted as a 1 GB tmpfs to match upstream guidance and
  hosts the substream record cache only.
- `/tmp/event_cache` is mounted as a separate 512 MB tmpfs and hosts the
  per-camera mainstream `event_recording` ring buffer at
  `/tmp/event_cache/<camera>/`, retaining roughly `pre_capture` seconds
  (default 15 s) of mainstream per camera. Isolating it from `/tmp/cache`
  prevents a runaway high-bitrate mainstream from evicting substream
  record segments (M1). Budget ~15 MB per camera at 8 Mbps mainstream
  and bump the size if you run many high-bitrate cameras with event
  recording enabled. If the `/tmp/event_cache` mount is missing (e.g.
  running outside compose) the code falls back to
  `/tmp/cache/event_buffer/<camera>/` automatically.
- Port 1935 (RTMP) is exposed for legacy restream scenarios; remove if unused.
- Port 5000 is internal unauthenticated API/UI; firewall it on the NVR.
