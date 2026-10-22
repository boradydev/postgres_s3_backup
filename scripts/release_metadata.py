"""Validate a release ref and export its version and GHCR image name."""

import os
import re
import tomllib
from pathlib import Path


root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["poetry"]["version"]
number = r"(?:0|[1-9][0-9]*)"
if not re.fullmatch(rf"{number}\.{number}\.{number}", version):
    raise SystemExit("Release version must be X.Y.Z without a prerelease suffix")
ref = os.environ["GITHUB_REF"]
if ref.startswith("refs/tags/"):
    if ref != f"refs/tags/v{version}":
        raise SystemExit("Tag must match the version in pyproject.toml")
elif ref != "refs/heads/master" or os.environ["GITHUB_EVENT_NAME"] != "workflow_dispatch":
    raise SystemExit("Manual publication is allowed only from master")
image = "ghcr.io/" + os.environ["GITHUB_REPOSITORY"].lower()
with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
    output.write(f"version={version}\nimage={image}\n")
