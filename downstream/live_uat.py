#!/usr/bin/env python3
"""Read-only acceptance checks against an explicitly selected Frigate instance."""

import argparse
import json
import time
import urllib.parse
import urllib.request


def main() -> None:
    """Check runtime configuration, processing and retained SD/HD footage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Internal API URL reachable from the isolated bench")
    parser.add_argument("--version-prefix", required=True)
    parser.add_argument("--cameras", nargs="+", required=True)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--require-main-since", type=float)
    args = parser.parse_args()
    base = args.url.rstrip("/")

    def get(path: str):
        with urllib.request.urlopen(base + path, timeout=30) as response:
            return json.load(response)

    config, stats = get("/api/config"), get("/api/stats")
    assert stats["service"]["version"].startswith(args.version_prefix), "Wrong candidate version"
    assert "amd" in stats["detectors"], "AMD detector missing"
    now = time.time()
    report = {"version": stats["service"]["version"], "detectors": stats["detectors"], "cameras": {}}
    for name in args.cameras:
        camera, metrics = config["cameras"][name], stats["cameras"][name]
        roles = [set(item["roles"]) for item in camera["ffmpeg"]["inputs"]]
        assert any("record_events" in item for item in roles), f"{name}: HD input missing"
        assert all(not {"record", "record_events"}.issubset(item) for item in roles), name
        assert camera["record"]["event_recording"]["enabled"], f"{name}: event recording disabled"
        assert camera["detect"]["fps"] == 5, f"{name}: detection FPS changed"
        assert metrics["process_fps"] >= 4.0, f"{name}: camera not processing"
        assert metrics["skipped_fps"] <= 0.2, f"{name}: sustained skipped frames"
        result = {"process_fps": metrics["process_fps"], "skipped_fps": metrics["skipped_fps"]}
        for quality in ["sub", "main"]:
            query = urllib.parse.urlencode({"after": now - args.hours * 3600, "before": now, "quality": quality})
            rows = get(f"/api/{urllib.parse.quote(name)}/recordings?{query}")
            assert rows, f"{name}: no {quality} recordings in requested window"
            assert all(row["stream_quality"] == quality for row in rows), f"{name}: mixed quality tags"
            newest = max(rows, key=lambda row: row["end_time"])
            if quality == "sub":
                assert newest["end_time"] > now - 120, f"{name}: SD recording stalled"
            if quality == "main" and args.require_main_since is not None:
                assert newest["start_time"] >= args.require_main_since, f"{name}: no new HD event recorded"
            result[quality] = {"segments": len(rows), "latest_end": newest["end_time"]}
            start, end = int(newest["start_time"]), int(newest["end_time"])
            assert end > start, f"{name}: latest {quality} segment too short"
            vod = get(f"/api/vod/{name}/start/{start}/end/{end}/quality/{quality}")
            assert vod.get("sequences"), f"{name}: {quality} VOD has no sequences"
        report["cameras"][name] = result
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
