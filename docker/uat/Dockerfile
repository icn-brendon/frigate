# Frigate UAT overlay — fast rebuild on top of upstream stable-rocm.
#
# Layers our modified Python source, migrations, version file, AND web
# frontend on top of the upstream stable-rocm image.  Skips rebuilding
# the entire ROCm stack, ffmpeg, and Python deps (~60s vs ~90min).
#
# The web-build stage compiles the frontend from source so that our
# modified components (RecordingView, timeline, DynamicVideoPlayer)
# match the Python API responses.

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
# Stage 2: overlay onto upstream base image
# ---------------------------------------------------------------------------
ARG BASE_IMAGE=ghcr.io/blakeblackshear/frigate:stable-rocm
FROM ${BASE_IMAGE}

# Overwrite Python source and migrations with our fork's versions
COPY frigate/ /opt/frigate/frigate/
COPY migrations/ /opt/frigate/migrations/

# Overwrite web frontend with our fork's built assets
COPY --from=web-build /work/dist/ /opt/frigate/web/
