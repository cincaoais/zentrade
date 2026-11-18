"""Pins of the research and forward code.

The registration freezes these hashes; every night and morning step recomputes
them and refuses to decide when one changed. Line endings are folded to LF so a
plain Windows checkout does not look like a code change (``.gitattributes``
stores LF), matching :mod:`app.research.versions`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.forward import config
from app.research import versions

_FORWARD_CODE_DIR = Path("backend") / "app" / "forward"


def _normalized_bytes(path: Path) -> bytes:
    return Path(path).read_bytes().replace(b"\r\n", b"\n")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(_normalized_bytes(path)).hexdigest()


def _tree_sha256(directory: Path) -> str:
    """sha256 over the sorted ``*.py`` files, name and bytes, LF-normalised."""
    digest = hashlib.sha256()
    for path in sorted(Path(directory).glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_normalized_bytes(path))
        digest.update(b"\0")
    return digest.hexdigest()


def _recursive_sha256(directory: Path) -> str:
    """sha256 over every ``*.py`` below ``directory`` (relative path and bytes)."""
    directory = Path(directory)
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_normalized_bytes(path))
        digest.update(b"\0")
    return digest.hexdigest()


def current_pins(repo_root: Path, data_dir: Path) -> dict[str, str]:
    """Every pin the forward registration freezes, recomputed from disk."""
    repo_root = Path(repo_root)
    result = {
        "chan_version": versions.chan_engine_version(),
        "wyckoff_version": versions.wyckoff_port_version(),
        "bridge_hash": versions.file_hash(
            repo_root / "backend" / "app" / "strategy" / "builtin" / "research_signal_table.py"
        ),
    }
    for rel in config.PINNED_FILES:
        result[f"file:{rel}"] = _file_sha256(repo_root / rel)
    for rel in config.PINNED_TREES:
        result[f"tree:{rel}"] = _recursive_sha256(repo_root / rel)
    result["frozen_registry"] = _file_sha256(
        Path(data_dir) / "research" / "registry.frozen.json"
    )
    result["forward_code"] = _tree_sha256(repo_root / _FORWARD_CODE_DIR)
    return result


def diff_pins(expected: dict, actual: dict) -> list[str]:
    """Sorted names of keys that differ or exist on only one side."""
    return sorted(
        key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key)
    )


__all__ = ["current_pins", "diff_pins"]