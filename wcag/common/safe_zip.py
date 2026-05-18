"""Defensive ZIP loader for analyzers that read OOXML containers.

DOCX, PPTX, and XLSX are all ZIP-of-XML files. A 100 KB upload can legally
decompress to several GB, exhausting the container memory before the
analyzer even reaches its XML parser. This module provides:

* :func:`open_safe_zip` - opens a :class:`zipfile.ZipFile` from bytes after
  enforcing per-entry and total uncompressed-size caps.

The defaults are intentionally generous (300 MB total, 50 MB per part)
because real DOCX/PPTX with many embedded media items can be large, but
they are still orders of magnitude below the memory budget of the worker.
"""

from __future__ import annotations

import io
import zipfile
from typing import Iterable

# Total uncompressed payload allowed across all zip entries (bytes).
DEFAULT_MAX_TOTAL_UNCOMPRESSED = 300 * 1024 * 1024  # 300 MB

# Largest single uncompressed zip entry allowed (bytes).
DEFAULT_MAX_PER_ENTRY_UNCOMPRESSED = 50 * 1024 * 1024  # 50 MB

# Largest compression ratio allowed (uncompressed / compressed).
# Real OOXML rarely exceeds ~50x. 200x is comfortably above that and well
# below the >>1000x ratios characteristic of zip bombs.
DEFAULT_MAX_RATIO = 200


class ZipBombError(ValueError):
    """Raised when a zip file's declared uncompressed sizes exceed our caps."""


def open_safe_zip(
    file_bytes: bytes,
    *,
    max_total: int = DEFAULT_MAX_TOTAL_UNCOMPRESSED,
    max_per_entry: int = DEFAULT_MAX_PER_ENTRY_UNCOMPRESSED,
    max_ratio: int = DEFAULT_MAX_RATIO,
) -> zipfile.ZipFile:
    """Open a ZipFile from bytes only after validating its declared sizes.

    Parameters mirror the module-level ``DEFAULT_*`` constants. ``max_ratio``
    is applied as ``sum(uncompressed) / max(1, len(file_bytes))``.

    Raises
    ------
    zipfile.BadZipFile
        If the bytes are not a valid zip container.
    ZipBombError
        If any single entry, or the sum of all entries, exceeds the caps,
        or if the overall compression ratio exceeds ``max_ratio``.
    """
    zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    infos: Iterable[zipfile.ZipInfo] = zf.infolist()

    total = 0
    for info in infos:
        if info.file_size > max_per_entry:
            zf.close()
            raise ZipBombError(
                f"Zip entry {info.filename!r} declares "
                f"{info.file_size} bytes uncompressed (cap {max_per_entry})."
            )
        total += info.file_size
        if total > max_total:
            zf.close()
            raise ZipBombError(
                f"Total uncompressed size exceeds {max_total} bytes "
                f"after entry {info.filename!r}."
            )

    compressed = max(1, len(file_bytes))
    if total // compressed > max_ratio:
        zf.close()
        raise ZipBombError(
            f"Compression ratio {total // compressed}x exceeds cap {max_ratio}x."
        )

    return zf


__all__ = ["open_safe_zip", "ZipBombError",
           "DEFAULT_MAX_TOTAL_UNCOMPRESSED",
           "DEFAULT_MAX_PER_ENTRY_UNCOMPRESSED",
           "DEFAULT_MAX_RATIO"]
