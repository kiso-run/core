__version__ = "0.2.0"

from pathlib import Path


def _loc_in_dir(directory: Path) -> int:
    """Count non-empty, non-comment Python LOC in *directory* (recursive)."""
    if not directory.is_dir():
        return 0
    count = 0
    for path in directory.rglob("*.py"):
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    count += 1
        except OSError:
            pass
    return count


def count_loc(root: Path) -> dict:
    """Return LOC breakdown for core/cli relative to *root*.

    Counts non-empty, non-comment lines in .py files.
    Returns {"core": N, "cli": N, "total": N}.
    """
    areas = {"core": root / "kiso", "cli": root / "cli"}
    result = {name: _loc_in_dir(path) for name, path in areas.items()}
    result["total"] = sum(result.values())
    return result
