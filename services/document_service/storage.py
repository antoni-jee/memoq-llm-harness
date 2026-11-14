"""Filesystem storage for original and exported MQXLIFF files.

Files are content-addressed by their id (SHA-256 of the bytes), so a
client-supplied filename never becomes part of a path. Writes go through a
temporary file in the destination directory, ``fsync`` and ``os.replace``, so
a crash or failed write never leaves a partial file under the final name.
Nothing is ever deleted: a content-addressed file may be shared by
overlapping requests that produced the same bytes.

Layout under the configured root::

    originals/<first two hex chars>/<original_id>.mqxliff
    exports/<first two hex chars>/<export_id>.mqxliff
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from typing import Optional

_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_TMP_PREFIX = ".tmp-"

ORIGINALS = "originals"
EXPORTS = "exports"
KINDS = (ORIGINALS, EXPORTS)


class StorageError(OSError):
    """Storage could not complete an operation. The message is safe to show."""


class FileStorage:
    def __init__(self, root: str):
        if not os.path.isabs(root):
            raise ValueError("Storage root must be an absolute path.")
        self.root = os.path.normpath(root)

    # ----------------------------------------------------------------- paths

    def _kind_dir(self, kind: str) -> str:
        if kind not in KINDS:
            raise ValueError("Unknown storage kind.")
        return os.path.join(self.root, kind)

    def path_for(self, file_id: str, kind: str = ORIGINALS) -> str:
        if not _ID_RE.match(file_id or ""):
            raise ValueError("File id must be 64 lowercase hex characters.")
        return os.path.join(self._kind_dir(kind), file_id[:2], file_id + ".mqxliff")

    # ------------------------------------------------------------- operations

    def read(self, file_id: str, kind: str = ORIGINALS) -> Optional[bytes]:
        """The stored bytes, or ``None`` if no file exists for this id."""
        try:
            with open(self.path_for(file_id, kind), "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None
        except OSError:
            raise StorageError("Stored file could not be read.") from None

    def has_valid(self, file_id: str, kind: str = ORIGINALS) -> bool:
        """True when a file exists and its content still hashes to its id."""
        data = self.read(file_id, kind)
        return data is not None and hashlib.sha256(data).hexdigest() == file_id

    def write(self, file_id: str, data: bytes, kind: str = ORIGINALS) -> bool:
        """Store ``data`` atomically. Returns False if a valid copy already existed."""
        if hashlib.sha256(data).hexdigest() != file_id:
            raise ValueError("Data does not match its id.")
        if self.has_valid(file_id, kind):
            return False

        final = self.path_for(file_id, kind)
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
            raise StorageError("File could not be stored.") from None
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def check_writable(self) -> bool:
        """Readiness probe: can a file be created and removed in every area?"""
        try:
            for kind in KINDS:
                directory = self._kind_dir(kind)
                os.makedirs(directory, exist_ok=True)
                fd, probe = tempfile.mkstemp(prefix=_TMP_PREFIX + "probe-", dir=directory)
                os.close(fd)
                os.remove(probe)
            return True
        except OSError:
            return False
