#!/usr/bin/env python3
"""Run dual-stream and adjacent upstream regressions in separate processes."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

MODULES = [
    "test_dual_stream_config",
    "test_dual_stream_migration",
    "test_event_recorder",
    "test_maintainer_dual_stream",
    "test_retention_dual_stream",
    "test_export_quality",
    "test_config",
    "test_maintainer",
    "test_record_retention",
    "test_export",
    "test_export_progress",
    "http_api.test_http_dual_stream_playback",
    "http_api.test_http_media",
]


def main() -> int:
    """Isolate import mocks between modules and preserve every failed result."""
    failed = []
    for command in [
        ["ruff", "check", "downstream", "frigate", "migrations"],
        ["ruff", "format", "--check", "downstream", "frigate", "migrations"],
    ]:
        if subprocess.run(command, check=False).returncode:
            failed.append("process lint/format")
    result = subprocess.run(
        [sys.executable, "generate_api_auth_spec.py", "--check"],
        check=False,
    )
    if result.returncode:
        failed.append("API schema")
    for name in MODULES:
        print(f"\n=== {name} ===", flush=True)
        with tempfile.TemporaryDirectory(prefix="frigate-bench-") as directory:
            root = Path.cwd()
            Path(directory, "migrations").symlink_to(root / "migrations")
            result = subprocess.run(
                [sys.executable, "-u", "-m", "unittest", f"frigate.test.{name}"],
                check=False,
                cwd=directory if name.startswith("http_api.") else root,
                env={**os.environ, "PYTHONPATH": str(root)},
            )
        if result.returncode:
            failed.append(name)
    if failed:
        print(f"Failed modules: {', '.join(failed)}", file=sys.stderr)
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
