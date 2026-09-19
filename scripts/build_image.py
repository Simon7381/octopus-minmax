"""Resolve the latest stable Microsoft Playwright Noble image and build locally."""

import argparse
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path


TAGS_URL = "https://mcr.microsoft.com/v2/playwright/python/tags/list"
ROOT = Path(__file__).resolve().parents[1]


def latest_noble_version(tags: list[str]) -> str:
    versions = [
        tuple(map(int, match.groups()))
        for tag in tags
        if (match := re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)-noble", tag))
    ]
    if not versions:
        raise ValueError("Microsoft registry returned no stable Playwright Noble tags")
    return ".".join(map(str, max(versions)))


def resolve_version() -> str:
    with urllib.request.urlopen(TAGS_URL, timeout=30) as response:
        return latest_noble_version(json.load(response)["tags"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    parser.add_argument("--tag", default="localhost/octopus-minmax:playwright")
    parser.add_argument("--resolve-only", action="store_true")
    parser.add_argument("--github-output", action="store_true")
    args = parser.parse_args()

    version = resolve_version()
    image = f"mcr.microsoft.com/playwright/python:v{version}-noble"
    print(f"Resolved Microsoft Playwright image: {image}", flush=True)
    if args.github_output:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"version={version}\nimage={image}\n")
    if args.resolve_only:
        return

    subprocess.run([
        args.engine, "build",
        "--pull=always" if args.engine == "podman" else "--pull",
        "--no-cache", "--build-arg", f"PLAYWRIGHT_VERSION={version}",
        "-f", "dockerfile", "-t", args.tag, ".",
    ], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
