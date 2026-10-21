#!/usr/bin/env python3
"""Run dual-stream and adjacent upstream regressions in separate processes."""

import subprocess
import sys

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
]


def main() -> int:
    """Isolate import mocks between modules and preserve every failed result."""
    failed = []
    result = subprocess.run(
        [sys.executable, "generate_api_auth_spec.py", "--check"], check=False,
    )
    if result.returncode:
        failed.append("API schema")
    for name in MODULES:
        print(f"\n=== {name} ===", flush=True)
        result = subprocess.run(
            [sys.executable, "-u", "-m", "unittest", f"frigate.test.{name}"],
            check=False,
        )
        if result.returncode:
            failed.append(name)
    if failed:
        print(f"Failed modules: {', '.join(failed)}", file=sys.stderr)
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
