#!/usr/bin/env python3
"""Prepare an upstream release with the portable dual-stream patch."""

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEATURE_PATHS = [
    "frigate",
    "migrations",
    "web",
    "docker/main/build_nginx.sh",
    "docker/main/rootfs/usr/local/go2rtc/create_config.py",
]
INTEGRATION_PATHS = [
    "downstream",
    "docker/uat/Dockerfile",
    ".github/workflows/dual-stream.yml",
]


def git(*args: str, cwd: Path = ROOT) -> str:
    """Run git without shell interpolation."""
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def export_patch(ref: str) -> None:
    """Export application changes separately from update/build tooling."""
    release = json.loads((ROOT / "downstream/release.json").read_text())
    diff = subprocess.check_output(
        ["git", "diff", "--binary", "--full-index", release["upstream_commit"], ref,
         "--", *FEATURE_PATHS], cwd=ROOT,
    )
    (ROOT / "downstream/dual-stream.patch").write_bytes(diff)
    print(f"Exported {len(diff)} bytes against {release['upstream_tag']}")


def prepare(tag: str, directory: Path, image: str) -> None:
    """Create a release worktree; leave conflicts visible for manual resolution."""
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", tag):
        raise SystemExit("Use an explicit upstream release tag, e.g. v0.18.2")
    if git("status", "--porcelain"):
        raise SystemExit("Commit the patch and tooling before preparing an update")
    subprocess.run(
        ["git", "fetch", "https://github.com/blakeblackshear/frigate.git",
         f"refs/tags/{tag}:refs/tags/{tag}"], cwd=ROOT, check=True,
    )
    commit = git("rev-parse", f"{tag}^{{commit}}")
    directory = directory.resolve()
    subprocess.run(
        ["git", "worktree", "add", "-b", f"downstream/{tag}", str(directory), commit],
        cwd=ROOT, check=True,
    )
    result = subprocess.run(
        ["git", "apply", "--3way", "--index", str(ROOT / "downstream/dual-stream.patch")],
        cwd=directory, check=False,
    )
    # Copy process files even on conflict, so the resolution worktree has its runbook.
    for name in INTEGRATION_PATHS:
        source, target = ROOT / name, directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    release = {"upstream_tag": tag, "upstream_commit": commit, "base_image": image}
    (directory / "downstream/release.json").write_text(json.dumps(release, indent=2) + "\n")
    for name in ["docker/uat/Dockerfile", "downstream/Dockerfile.test"]:
        path = directory / name
        path.write_text(re.sub(r"(?m)^ARG BASE_IMAGE=.*$", f"ARG BASE_IMAGE={image}", path.read_text()))
    if result.returncode:
        raise SystemExit(f"Patch conflicts in {directory}. Resolve them, test, then re-export the patch")
    print(f"Prepared {directory} at {tag}. Review and commit, then export-patch and run CI")


def main() -> None:
    """Dispatch patch export or release preparation."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-patch")
    export.add_argument("--ref", default="HEAD")
    new = sub.add_parser("prepare")
    new.add_argument("tag")
    new.add_argument("directory", type=Path)
    new.add_argument("--base-image", required=True, help="Matching upstream ROCm image pinned by digest")
    args = parser.parse_args()
    if args.command == "export-patch":
        export_patch(args.ref)
    else:
        if not re.fullmatch(r"ghcr\.io/blakeblackshear/frigate:[A-Za-z0-9.-]+-rocm@sha256:[a-f0-9]{64}", args.base_image):
            raise SystemExit("Pin the upstream ROCm image by release tag and sha256 digest")
        if args.base_image.split(":", 1)[1].split("-rocm@", 1)[0] != args.tag.removeprefix("v"):
            raise SystemExit("The source release and image release must match")
        prepare(args.tag, args.directory, args.base_image)


if __name__ == "__main__":
    main()
