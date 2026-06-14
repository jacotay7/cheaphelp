from __future__ import annotations

from pathlib import Path

CONVENTIONS_FILES = ("CHEAPHELP.md", "AGENTS.md", "CONTRIBUTING.md")


def read_conventions(directory: Path) -> str:
    """Return the contents of the first conventions file found in `directory`.

    Lookup order (first match wins): CHEAPHELP.md, AGENTS.md, CONTRIBUTING.md.
    Returns "" if the directory does not exist, is not a directory, or contains
    no matching file. The file is read as UTF-8; callers receive the raw text.
    """
    if not directory.is_dir():
        return ""
    for name in CONVENTIONS_FILES:
        path = directory / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return ""
