# Devil's Advocate: Deployment Pipeline Review

Branch `feature/dual-stream-recording` targeting nas01 (ROCm AMD iGPU, Traefik at nvr.saxon.email).

---

## 1. Image Mismatch -- BLOCKER

**Problem:** UAT workflow (`uat-image.yml`) builds `docker/main/Dockerfile` target `frigate`. Production nas01 runs `stable-rocm` which is built from `docker/rocm/Dockerfile`. The ROCm Dockerfile layers in: ROCm 7.2 libs (MIOpen, MIGraphX, HIP, HSA), `onnxruntime-migraphx` (replaces the generic `onnxruntime`), `mesa-va-drivers` from bookworm-backports, and `LIBVA_DRIVER_NAME=radeonsi`. The generic `frigate` target ships plain `onnxruntime` (CPU provider only) with no ROCm libs.

**What breaks:**
- The config declares `detectors: amd: type: onnx`. On the generic image this loads CPU-only `onnxruntime`. It will *run* but inference will be ~10x slower (CPU instead of AMD iGPU via MIGraphX), causing detection lag and high CPU load.
- `ffmpeg hwaccel_args: preset-vaapi` needs a working VAAPI driver. The generic image includes `intel-media-driver` (Intel iGPU) but NOT `mesa-va-drivers` for AMD. VAAPI will fail to initialise; ffmpeg will either crash the camera process or silently fall back to software decode, spiking CPU to 100%.
- The compose file sets `LIBVA_DRIVER_NAME=radeonsi` but the radeonsi driver is not present in the generic image -- ffmpeg will error.

**Fix:** Add a ROCm build job to `uat-image.yml` using `docker/rocm/rocm.hcl` (same as upstream CI `amd64_extra_builds`), or at minimum build from the ROCm Dockerfile. Update `docker-compose.nas01.uat.yml` to reference the `-rocm` tag.

---

## 2. `make version` Correctness -- MINOR

**Problem:** `make version` runs `git log -1 --pretty=format:"%h"`. The UAT workflow checks out with `fetch-depth: 0` (full history), so `git log` produces a valid short hash. Upstream's `setup/action.yml` also calls `make version` with no explicit `fetch-depth` (defaults to 1), which also works for `git log -1`. No issue here.

**Status:** OK -- works correctly in both contexts.

---

## 3. Web Build and `web/.env` -- MINOR

**Problem:** `make version` writes `web/.env` with `VITE_GIT_COMMIT_HASH=<hash>`. However, the Dockerfile `web-build` stage does `COPY web/ ./` which copies `web/.env` into the build context *only if it exists on the host*. In CI, `make version` runs on the runner (step "Generate version file"), creating `web/.env` in the workspace. `docker/build-push-action` then sends the workspace as build context, so `web/.env` IS present when `npm run build` runs inside Docker. The hash will be picked up correctly.

**Caveat:** Buildx with remote cache (`cache-from: type=gha`) may serve a cached `web-build` layer that includes a stale hash. Cache-busting depends on the `COPY web/ ./` layer seeing a different checksum. Since the `.env` content changes per commit, the layer *should* invalidate. Low risk.

**Status:** OK -- works as designed.

---

## 4. Compose File Correctness for nas01 -- MAJOR (one sub-item)

- **Image reference:** `ghcr.io/icn-brendon/frigate:${FRIGATE_UAT_TAG:-feature-dual-stream-recording}` -- correct org, not blakeblackshear. OK.
- **macvlan IP:** `.71` avoids collision with prod `.70`. OK.
- **Traefik host:** `nvr-uat.saxon.email` -- this is NOT the same as prod `nvr.saxon.email`. A DNS record (A or CNAME) for `nvr-uat.saxon.email` must exist pointing to the Traefik ingress, and Cloudflare DNS must be configured for the `certresolver=cloudflare` ACME challenge to succeed. **If the DNS record is missing, HTTPS will not work.** (MAJOR)
- **Volume paths:** `/tank/apps/frigate/config` and `/mnt/nvr` -- these are the *prod* paths. The compose comments acknowledge this ("swap them if you want UAT to run on a scratch recordings tree"). Running UAT against prod config/media is a deliberate choice but risky -- UAT writes will intermingle with prod recordings. (Noted, not a bug.)

**Fix:** Create DNS record for `nvr-uat.saxon.email` before deploying. Consider separate config/media paths to avoid polluting prod data.

---

## 5. config.yaml.uat Correctness -- OK

- YAML is syntactically valid.
- `{FRIGATE_RTSP_PASSWORD}` and `{FRIGATE_MQTT_PASSWORD}` use Frigate's own `{VAR}` substitution syntax (not Docker `${VAR}`). Frigate reads these from the container's environment at startup. The compose file passes `FRIGATE_RTSP_PASSWORD` and `FRIGATE_MQTT_PASSWORD` as container env vars. This chain is correct.
- `version: 0.17-0` may need updating to match the fork's version (0.18.0 per Makefile), but Frigate only uses this for config migration checks and it is not a blocker.

---

## 6. `.env.uat` Completeness -- MINOR

- `FRIGATE_UAT_TAG` -- present. OK.
- `FRIGATE_PLUS_API_KEY` -- present (empty placeholder). Compose maps it to `PLUS_API_KEY`. OK.
- `FRIGATE_RTSP_PASSWORD` -- present. OK.
- `FRIGATE_MQTT_PASSWORD` -- present. OK.
- No missing vars. The compose file references only `FRIGATE_UAT_TAG`, `FRIGATE_PLUS_API_KEY`, `FRIGATE_RTSP_PASSWORD`, `FRIGATE_MQTT_PASSWORD` -- all present in `.env.uat`.

**Status:** OK.

---

## 7. Security -- MINOR

- `.env.uat` has empty secret placeholders and is documented as "DO NOT commit." Good.
- `config.yaml.uat` uses `{VAR}` substitution -- no inline secrets. The `plus://` model ID is not a secret (it is a model reference, not the API key).
- `web/.env` is in `.gitignore`. OK.
- No `.env.uat` in `.gitignore` found -- but the file lives in `frigate-workflow/` (a separate non-repo directory), not in the fork repo, so it is not at risk of being committed to the fork.

**Residual risk:** If any earlier commit in the fork's history contained inline secrets in a config file, they persist in git history. A `git log --all -p -- '*.yaml' '*.yml'` search for passwords would confirm. Low probability given the env-substitution approach used throughout.

---

## 8. `web/.env` in `.gitignore` -- OK

`web/.env` appears in `.gitignore` at line 20. It will not be committed accidentally.

---

## Summary

| # | Finding | Severity |
|---|---------|----------|
| 1 | UAT image is generic amd64, not ROCm -- no AMD inference, no VAAPI | **BLOCKER** |
| 4 | DNS for `nvr-uat.saxon.email` must exist for Traefik TLS | **MAJOR** |
| 5 | `version: 0.17-0` in config is stale vs 0.18.0 codebase | Minor |
| 3 | Buildx GHA cache could serve stale web hash | Minor |
| 7 | No `.env.uat` in fork `.gitignore` (mitigated by separate repo) | Minor |

**Blockers: 1 | Major: 1 | Minor: 3**
