#!/usr/bin/env python3
"""Decode real SD/HD VOD samples inside a disposable Frigate runtime container."""

import argparse
import glob
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

import cv2
import numpy as np


def main() -> None:
    """Check actual HLS media, rather than just API metadata or button labels."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--cameras", nargs="+", required=True)
    parser.add_argument(
        "--exports",
        action="store_true",
        help="Create, probe and delete only this run's short test exports",
    )
    args = parser.parse_args()
    ffmpeg = sorted(glob.glob("/usr/lib/ffmpeg/*/bin/ffmpeg"))[-1]
    ffprobe = ffmpeg.removesuffix("ffmpeg") + "ffprobe"
    base = args.url.rstrip("/")
    now = time.time()
    for camera in args.cameras:
        samples = {}
        for quality in ["sub", "main"]:
            query = urllib.parse.urlencode(
                {"after": now - 3600, "before": now - 15, "quality": quality}
            )
            with urllib.request.urlopen(
                f"{base}/api/{camera}/recordings?{query}", timeout=30
            ) as response:
                rows = json.load(response)
            assert rows, f"{camera}: no recent {quality} footage"
            row = max(rows, key=lambda item: item["end_time"])
            start, end = int(row["start_time"]), int(row["end_time"])
            suffix = "/quality/main" if quality == "main" else ""
            url = f"{base}/vod/{camera}/start/{start}/end/{end}{suffix}/master.m3u8"
            probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,codec_name",
                    "-of",
                    "json",
                    url,
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            info = json.loads(probe.stdout)["streams"][0]
            frame = subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-i",
                    url,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=320:-1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "-",
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            image = cv2.imdecode(
                np.frombuffer(frame.stdout, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            assert image is not None, f"{camera}: {quality} frame did not decode"
            variation = float(image.std())
            assert variation > 5, f"{camera}: {quality} frame appears blank/grey"
            samples[quality] = {**info, "pixel_std": round(variation, 2)}
        assert samples["main"]["width"] > samples["sub"]["width"], (
            f"{camera}: HD is not higher resolution"
        )
        print(json.dumps({"camera": camera, "samples": samples}), flush=True)
        if args.exports:
            for quality in ["auto", "sub"]:

                def api(path, data=None):
                    payload = json.dumps(data).encode() if data is not None else None
                    request = urllib.request.Request(
                        base
                        + path
                        + (f"?_uat={time.time_ns()}" if data is None else ""),
                        data=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=30) as response:
                        return json.load(response)

                created = api(
                    f"/api/export/{camera}/start/{start}/end/{end}",
                    {
                        "source": "recordings",
                        "quality": quality,
                        "name": f"dual-stream-uat-{int(now)}-{quality}",
                    },
                )
                export_id = created["export_id"]
                print(
                    json.dumps(
                        {
                            "created_test_export": export_id,
                            "camera": camera,
                            "quality": quality,
                        }
                    ),
                    flush=True,
                )
                for _ in range(60):
                    job = api(f"/api/jobs/export/{export_id}")
                    if job["status"] in ["success", "failed", "cancelled"]:
                        break
                    time.sleep(1)
                assert job["status"] == "success", (
                    f"{camera}: {quality} export did not succeed"
                )
                try:
                    export = api(f"/api/exports/{export_id}")
                    url = f"{base}/exports/{urllib.parse.quote(os.path.basename(export['video_path']))}"
                    probe = subprocess.run(
                        [
                            ffprobe,
                            "-v",
                            "error",
                            "-select_streams",
                            "v:0",
                            "-show_entries",
                            "stream=width,height,codec_name",
                            "-of",
                            "json",
                            url,
                        ],
                        check=True,
                        capture_output=True,
                        timeout=60,
                    )
                    info = json.loads(probe.stdout)["streams"][0]
                    expected = samples["main" if quality == "auto" else "sub"]
                    assert info["width"] == expected["width"], (
                        f"{camera}: wrong export quality"
                    )
                    print(
                        json.dumps(
                            {"camera": camera, "export_quality": quality, "media": info}
                        ),
                        flush=True,
                    )
                finally:
                    api("/api/exports/delete", {"ids": [export_id]})


if __name__ == "__main__":
    main()
