"""Экспортирует фиксированные зависимости и SHA-256 из собственного poetry.lock."""

import sys
import tomllib
from pathlib import Path


root = Path(__file__).resolve().parents[1]
lock = tomllib.loads((root / "poetry.lock").read_text(encoding="utf-8"))
for filename, group in (("requirements.txt", "main"), ("requirements-dev.txt", "dev")):
    lines = ["# Generated from poetry.lock: python scripts/export_requirements.py"]
    for package in lock["package"]:
        if group in package["groups"]:
            hashes = " ".join(f"--hash={item['hash']}" for item in package["files"])
            lines.append(f"{package['name']}=={package['version']} {hashes}")
    content = "\n".join(lines) + "\n"
    path = root / filename
    if "--check" in sys.argv:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            raise SystemExit(f"Regenerate {filename}")
    else:
        path.write_text(content, encoding="utf-8", newline="\n")
