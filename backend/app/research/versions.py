"""Engine and port version identifiers used in artifact paths and the registry."""

from __future__ import annotations

import hashlib
from pathlib import Path

import czsc

from app.research.vendor.dsa_chan import engine as _chan_engine

_RESEARCH_DIR = Path(__file__).resolve().parent
_WYCKOFF_DIR = _RESEARCH_DIR / "wyckoff"


def _normalized_bytes(path: Path) -> bytes:
    """File bytes with CRLF folded to LF.

    The repo stores LF (``.gitattributes``: ``eol=lf``) but a Windows working copy
    may hold CRLF; without folding, a plain checkout would change every version id
    and invalidate replay caches and freeze pins.
    """
    return Path(path).read_bytes().replace(b"\r\n", b"\n")


def file_hash(path: Path) -> str:
    """First 16 hex characters of the sha256 of the file bytes (line endings normalised)."""
    return hashlib.sha256(_normalized_bytes(path)).hexdigest()[:16]


def chan_engine_version() -> str:
    """Vendored Chan engine hash plus the czsc version it runs on."""
    return f"{file_hash(Path(_chan_engine.__file__))}-czsc{czsc.__version__}"


def wyckoff_port_version() -> str:
    """Hash over the sorted ``app/research/wyckoff/*.py`` bytes.

    Returns ``"none"`` until the ported ``wyk`` code lands.
    """
    files = sorted(_WYCKOFF_DIR.glob("*.py")) if _WYCKOFF_DIR.is_dir() else []
    if not files:
        return "none"
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_normalized_bytes(path))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


__all__ = ["chan_engine_version", "file_hash", "wyckoff_port_version"]