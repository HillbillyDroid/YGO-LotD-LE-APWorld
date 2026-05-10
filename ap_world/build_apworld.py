"""Package this folder as an .apworld for local testing.

An .apworld is just a zip file with a single top-level directory named after
the world (here: ``ygo_lotd``), renamed to the ``.apworld`` extension.
The output is written next to the source folder.
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

WORLD_NAME = "ygo_lotd"

# Paths/files that should never be packaged.
EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode", ".mypy_cache", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def should_skip(path: Path) -> bool:
    if path.name in EXCLUDE_DIRS:
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return True
    return False


def build(source: Path, output: Path) -> Path:
    if not source.is_dir():
        raise SystemExit(f"Source folder not found: {source}")

    # Write to a .zip first, then rename — cleaner than crafting the name by hand.
    zip_path = output.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    if output.exists():
        output.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source.rglob("*")):
            if should_skip(path):
                continue
            # Skip the build script's own output if run in-place.
            if path.resolve() == zip_path.resolve() or path.resolve() == output.resolve():
                continue
            # Archive path: <WORLD_NAME>/<relative path inside source>
            arcname = Path(WORLD_NAME) / path.relative_to(source)
            if path.is_dir():
                # ZipFile handles directory entries implicitly when files are added,
                # but adding an explicit entry keeps empty dirs around.
                zf.write(path, arcname.as_posix() + "/")
            else:
                zf.write(path, arcname.as_posix())

    shutil.move(str(zip_path), str(output))
    return output


def main() -> None:
    source = Path(__file__).resolve().parent
    output = source.parent / f"{WORLD_NAME}.apworld"
    result = build(source, output)
    print(f"Built {result}")


if __name__ == "__main__":
    main()
