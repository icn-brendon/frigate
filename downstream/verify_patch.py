#!/usr/bin/env python3
"""Verify the portable patch reconstructs its feature tree on clean upstream."""

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path


def main() -> None:
    """Apply the actual patch without network or access to a working checkout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with tarfile.open(args.archive) as archive:
            archive.extractall(root, filter="data")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.name=Patch bench", "-c", "user.email=bench@example.invalid",
                        "commit", "-qm", "Clean upstream fixture"], cwd=root, check=True)
        subprocess.run(["git", "apply", "--3way", "--index", str(bundle / "dual-stream.patch")], cwd=root, check=True)
        files = json.loads((bundle / "feature-files.json").read_text())
        for name, digest in files.items():
            path = root / name
            if digest is None:
                assert not path.exists(), f"Patch did not delete {name}"
            else:
                assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, f"Patch differs at {name}"
        print(f"PASS: portable patch reconstructs all {len(files)} feature files")


if __name__ == "__main__":
    main()
