# Frigate UAT overlay — fast rebuild on top of upstream stable-rocm.
#
# Layers our modified Python source, migrations, version file, AND web
# frontend on top of the upstream stable-rocm image.  Skips rebuilding
# the entire ROCm stack, ffmpeg, and Python deps (~60s vs ~90min).
#
# The web-build stage compiles the frontend from source so that our
# modified components (RecordingView, timeline, DynamicVideoPlayer)
# match the Python API responses.

# Global ARG must be before any FROM to be visible in FROM directives
ARG BASE_IMAGE=ghcr.io/blakeblackshear/frigate:stable-rocm

# ---------------------------------------------------------------------------
# Stage 1: build the web frontend (discarded after COPY)
# ---------------------------------------------------------------------------
FROM node:20 AS web-build
WORKDIR /work
COPY web/package.json web/package-lock.json ./
RUN npm install
COPY web/ ./
RUN npm run build \
    && mv dist/BASE_PATH/monacoeditorwork/* dist/assets/ \
    && rm -rf dist/BASE_PATH

# ---------------------------------------------------------------------------
# Stage 2: rebuild nginx with MAX_CLIPS=8192 for dual-stream long VOD ranges
# ---------------------------------------------------------------------------
# Upstream stable-rocm ships nginx-vod-module compiled with MAX_CLIPS=1080.
# Reolink substreams with ~2s segments (max I-frame interval) produce
# ~1500-1800 clips for a 1-hour VOD request, exceeding that limit and
# returning 503. Rebuild nginx with the higher limit so dual-stream
# deployments with short-GOP cameras can serve long ranges.
#
# Matches upstream docker/main/Dockerfile's nginx stage (debian:12).
FROM debian:12 AS nginx-build
ARG DEBIAN_FRONTEND=noninteractive
ENV CCACHE_DIR=/root/.ccache
ENV CCACHE_MAXSIZE=2G

# Copy upstream build script and run it. The script itself already bumps
# MAX_CLIPS to 8192 in this fork; the sed below is a defensive no-op so
# the overlay still produces 8192 if the base script is ever reverted.
COPY docker/main/build_nginx.sh /deps/build_nginx.sh
RUN sed -i -E 's/MAX_CLIPS \(1080\)/MAX_CLIPS (8192)/g' /deps/build_nginx.sh \
    && /deps/build_nginx.sh

# ---------------------------------------------------------------------------
# Stage 3: overlay onto upstream base image
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE}

# Replace nginx binary with our MAX_CLIPS=8192 build
COPY --from=nginx-build /usr/local/nginx/sbin/nginx /usr/local/nginx/sbin/nginx

# Overwrite Python source and migrations with our fork's versions
COPY frigate/ /opt/frigate/frigate/
COPY migrations/ /opt/frigate/migrations/

# Overwrite web frontend with our fork's built assets
COPY --from=web-build /work/dist/ /opt/frigate/web/
