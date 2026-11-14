"""Filesystem storage for original MQXLIFF files.

Files are content-addressed by ``original_id`` (SHA-256 of the bytes), so a
client-supplied filename never becomes part of a path. Writes go through a
temporary file in the destination directory, ``fsync`` and ``os.replace``, so
a crash or failed write never leaves a partial file under the final name.

Layout under the configured root::

    originals/<first two hex chars>/<original_id>.mqxliff
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from typing import Optional

_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_TMP_PREFIX = ".tmp-"


class StorageError(OSError):
    """Storage could not complete an operation. The message is safe to show."""


class FileStorage:
    def __init__(self, root: str):
        if not os.path.isabs(root):
            raise ValueError("Storage root must be an absolute path.")
        self.root = os.path.normpath(root)

    # ----------------------------------------------------------------- paths

    def _originals_dir(self) -> str:
        return os.path.join(self.root, "originals")

    def path_for(self, original_id: str) -> str:
        if not _ID_RE.match(original_id or ""):
            raise ValueError("original_id must be 64 lowercase hex characters.")
        return os.path.join(self._originals_dir(), original_id[:2], original_id + ".mqxliff")

    # ------------------------------------------------------------- operations

    def read(self, original_id: str) -> Optional[bytes]:
        """The stored bytes, or ``None`` if no file exists for this id."""
        try:
            with open(self.path_for(original_id), "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None
        except OSError:
            raise StorageError("Stored original could not be read.") from None

    def has_valid(self, original_id: str) -> bool:
        """True when a file exists and its content still hashes to its id."""
        data = self.read(original_id)
        return data is not None and hashlib.sha256(data).hexdigest() == original_id

    def write(self, original_id: str, data: bytes) -> bool:
        """Store ``data`` atomically. Returns False if a valid copy already existed."""
        if hashlib.sha256(data).hexdigest() != original_id:
            raise ValueError("Data does not match original_id.")
        if self.has_valid(original_id):
            return False

        final = self.path_for(original_id)
        directory = os.path.dirname(final)
        tmp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=_TMP_PREFIX, dir=directory)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, final)
            tmp_path = None
            return True
        except OSError:
            raise StorageError("Original could not be stored.") from None
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def check_writable(self) -> bool:
        """Readiness probe: can a file be created and removed under the root?"""
        try:
            os.makedirs(self._originals_dir(), exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=_TMP_PREFIX + "probe-", dir=self._originals_dir())
            os.close(fd)
            os.remove(probe)
            return True
        except OSError:
            return False
