# Frigate UAT overlay — fast rebuild for backend-only changes.
#
# Layers our modified Python source, migrations, and version file on top
# of the upstream stable-rocm image. Skips rebuilding the entire ROCm
# stack, ffmpeg, Python deps, and web frontend (~30s vs ~90min).
#
# Use this for UAT when changes are limited to:
#   - frigate/ (Python source)
#   - migrations/
#   - frigate/version.py
#
# For web/frontend changes, use the full docker/main/Dockerfile or
# docker/rocm/Dockerfile build instead.

ARG BASE_IMAGE=ghcr.io/blakeblackshear/frigate:stable-rocm
FROM ${BASE_IMAGE}

# Overwrite Python source and migrations with our fork's versions
COPY frigate/ /opt/frigate/frigate/
COPY migrations/ /opt/frigate/migrations/
