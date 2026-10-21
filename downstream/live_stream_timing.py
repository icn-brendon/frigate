#!/usr/bin/env python3
"""Measure sustained HD packet arrival and presentation timing in a LAN bench."""

import argparse
import concurrent.futures
import glob
import json
import statistics
import subprocess
import time
import urllib.parse
from pathlib import Path


def check(args, stream):
    """Exclude startup probing and fail on steady-state delivery or media gaps."""
    probe = max(
        glob.glob("/usr/lib/ffmpeg/*/bin/ffprobe"),
        key=lambda path: tuple(map(int, Path(path).parents[1].name.split("."))),
    )
    url = args.rtsp_base.rstrip("/") + "/" + urllib.parse.quote(stream, safe="")
    command = [
        "stdbuf",
        "-oL",
        probe,
        "-v",
        "error",
        "-rtsp_transport",
        "tcp",
        "-timeout",
        "10000000",
        "-read_intervals",
        f"%+{args.seconds + args.warmup + 8}",
        "-select_streams",
        "v:0",
        "-show_packets",
        "-show_entries",
        "packet=pts_time",
        "-of",
        "compact",
        url,
    ]
    arrivals, timestamps = [], []
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        for line in process.stdout:
            if "pts_time=" not in line:
                continue
            value = line.split("pts_time=", 1)[1].split("|")[0].strip()
            if value == "N/A":
                continue
            arrivals.append(time.monotonic())
            timestamps.append(float(value))
            if arrivals[-1] - arrivals[0] >= args.warmup + args.seconds:
                break
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    sample = [
        (arrival, timestamp)
        for arrival, timestamp in zip(arrivals, timestamps, strict=True)
        if arrival >= arrivals[0] + args.warmup
    ]
    if len(sample) < 2:
        return {"stream": stream, "failures": ["No sustained media sample"]}
    wall, media = zip(*sample, strict=True)
    media = sorted(media)
    wall_gaps = [b - a for a, b in zip(wall, wall[1:])]
    media_gaps = [b - a for a, b in zip(media, media[1:])]
    elapsed = wall[-1] - wall[0]
    fps = (len(sample) - 1) / elapsed
    failures = []
    if elapsed < args.seconds * 0.9:
        failures.append("Stream ended before the requested observation finished")
    if not args.expected_fps * 0.9 <= fps <= args.expected_fps * 1.1:
        failures.append("Frame rate outside expected range")
    if max(wall_gaps) * 1000 > args.max_gap_ms:
        failures.append("Packet arrival gap exceeded threshold")
    if max(media_gaps) * 1000 > args.max_media_gap_ms:
        failures.append("Presentation timestamp gap exceeded threshold")
    return {
        "stream": stream,
        "seconds": round(elapsed, 2),
        "frames": len(sample),
        "fps": round(fps, 2),
        "arrival_gap_max_ms": round(max(wall_gaps) * 1000, 1),
        "arrival_gap_p95_ms": round(
            sorted(wall_gaps)[int(len(wall_gaps) * 0.95)] * 1000, 1
        ),
        "arrival_gaps_over_threshold": sum(
            gap * 1000 > args.max_gap_ms for gap in wall_gaps
        ),
        "media_gap_max_ms": round(max(media_gaps) * 1000, 1),
        "media_gap_median_ms": round(statistics.median(media_gaps) * 1000, 1),
        "failures": failures,
    }


def main():
    """Require real continuous playback timing, rather than average FPS alone."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rtsp-base", default="rtsp://frigate-uat:8554")
    parser.add_argument("--streams", nargs="+", required=True)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--warmup", type=float, default=3)
    parser.add_argument("--expected-fps", type=float, default=20)
    parser.add_argument("--max-gap-ms", type=float, default=150)
    parser.add_argument("--max-media-gap-ms", type=float, default=75)
    args = parser.parse_args()
    if args.seconds < 10 or args.warmup < 0 or args.expected_fps <= 0:
        parser.error("Use at least 10 seconds, nonnegative warmup and positive FPS")
    failed = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(check, args, stream): stream for stream in args.streams}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
            except Exception as error:
                result = {
                    "stream": futures[future],
                    "failures": [type(error).__name__],
                }
            print(json.dumps(result), flush=True)
            failed |= bool(result["failures"])
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
