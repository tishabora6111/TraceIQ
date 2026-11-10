"""File storage behind a small interface (ARCHITECTURE §9, D5).

Modules save files under keys they generate themselves (never from user-supplied names), store only
the returned URI in the database, and open or delete files through the same URI. They never build
filesystem paths. The only implementation is ``LocalFileStorage``, which keeps files under the
configured root directory (``TRACEIQ_STORAGE_ROOT``).

Keys are relative, ``/``-separated segments of ``[A-Za-z0-9._-]``, at most ``MAX_KEY_LENGTH``
characters in total and ``MAX_SEGMENT_LENGTH`` per segment. A segment may not be empty or start
with ``.``, which also excludes ``.``, ``..``, hidden files and the temporary files used while
writing. URIs have the form ``local:///<key>``.
"""

import logging
import os
import re
import stat
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, Protocol

from fastapi import status

from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError

logger = logging.getLogger(__name__)

LOCAL_URI_PREFIX = "local:///"
# Maximum key length in characters (ASCII only, so also bytes). Keeps the full path well below
# common filesystem limits for any reasonable storage root.
MAX_KEY_LENGTH = 512
# Maximum segment length: the usual filesystem limit for one file or directory name.
MAX_SEGMENT_LENGTH = 255
# Writes are streamed in chunks of at most this many bytes.
CHUNK_SIZE = 1024 * 1024

_SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
_TEMP_PREFIX = ".tmp-"


class StorageError(AppError):
    """A storage operation failed. Messages never contain filesystem paths."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "storage_error"


class StorageValidationError(StorageError):
    """A key or URI is malformed or not a local storage URI. Keys come from the server, so this is
    a programming error, not a client error."""

    code = "invalid_storage_reference"


class StorageObjectNotFoundError(NotFoundError, StorageError):
    """No file is stored under the URI."""

    code = "storage_object_not_found"


class Storage(Protocol):
    def save(self, key: str, data: bytes | BinaryIO) -> str:
        """Store ``data`` under ``key``, replacing any existing file atomically; return its URI."""
        ...

    def open(self, uri: str) -> BinaryIO:
        """Open the stored file for binary reading. The caller closes it (use ``with``)."""
        ...

    def delete(self, uri: str) -> None:
        """Delete the stored file. Deleting a missing file is not an error."""
        ...


def validate_key(key: str) -> str:
    """Return ``key`` unchanged if it is a valid storage key, else raise ``StorageValidationError``."""
    if not isinstance(key, str) or not key:
        raise StorageValidationError("Storage key must be a non-empty string.")
    if len(key) > MAX_KEY_LENGTH:
        raise StorageValidationError(f"Storage key must be at most {MAX_KEY_LENGTH} characters.")
    segments = key.split("/")
    if any(len(segment) > MAX_SEGMENT_LENGTH for segment in segments):
        raise StorageValidationError(f"Storage key segments must be at most {MAX_SEGMENT_LENGTH} characters.")
    if not all(_SEGMENT.fullmatch(segment) for segment in segments):
        raise StorageValidationError(
            "Storage key must be relative '/'-separated segments of [A-Za-z0-9._-], none empty or starting with '.'."
        )
    return key


def key_from_uri(uri: str) -> str:
    """Return the key of a ``local:///<key>`` URI, else raise ``StorageValidationError``."""
    if not isinstance(uri, str) or not uri.startswith(LOCAL_URI_PREFIX):
        raise StorageValidationError("Not a local storage URI.")
    return validate_key(uri[len(LOCAL_URI_PREFIX) :])


def _failure(operation: str, exc: OSError) -> StorageError:
    # Log only the error class and errno: OSError messages and filenames contain absolute paths.
    logger.error(
        "Storage operation failed", extra={"operation": operation, "error": type(exc).__name__, "errno": exc.errno}
    )
    return StorageError(f"Could not {operation} the stored file.")


class LocalFileStorage:
    """Files under a root directory. Symbolic links below the root are refused, never followed."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        """Filesystem path for ``key``. Raises unless every existing component is a real directory
        or file inside the root: a symlink could point anywhere."""
        path = self._root
        for segment in key.split("/"):
            path = path / segment
            if path.is_symlink():
                # A symlink below the root may mean tampering. Never log the key or path.
                logger.warning("Storage path rejected", extra={"reason": "symbolic_link"})
                raise StorageValidationError("Storage key refers to a symbolic link.")
        if not path.resolve().is_relative_to(self._root):
            logger.warning("Storage path rejected", extra={"reason": "outside_root"})
            raise StorageValidationError("Storage key resolves outside the storage root.")
        return path

    def save(self, key: str, data: bytes | BinaryIO) -> str:
        path = self._path(validate_key(key))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Check again now that the parent directories exist.
            self._path(key)
            fd, temp_name = tempfile.mkstemp(prefix=_TEMP_PREFIX, dir=path.parent)
        except OSError as exc:
            raise _failure("save", exc) from None
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                if isinstance(data, (bytes, bytearray, memoryview)):
                    view = memoryview(data)
                    for start in range(0, len(view), CHUNK_SIZE):
                        handle.write(view[start : start + CHUNK_SIZE])
                else:
                    # Only b"" ends the stream: None (a non-blocking stream with no data ready) would
                    # otherwise save a truncated file as if it were complete.
                    while (chunk := data.read(CHUNK_SIZE)) != b"":
                        if chunk is None:
                            raise TypeError("Storage stream returned None; non-blocking streams are not supported.")
                        if not isinstance(chunk, (bytes, bytearray, memoryview)):
                            raise TypeError(
                                f"Storage stream must return bytes, not {type(chunk).__name__}; open it in binary mode."
                            )
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            # Atomic: readers see the old file or the complete new one, never a partial file.
            os.replace(temp_path, path)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise _failure("save", exc) from None
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        _sync_directory(path.parent)
        return LOCAL_URI_PREFIX + key

    def open(self, uri: str) -> BinaryIO:
        path = self._path(key_from_uri(uri))
        # O_NOFOLLOW (POSIX) also refuses a symlink created after the check above.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            raise StorageObjectNotFoundError("No stored file exists for this URI.") from None
        except OSError as exc:
            raise _failure("open", exc) from None
        # A directory is not a stored file (POSIX opens directories read-only without error).
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise StorageObjectNotFoundError("No stored file exists for this URI.")
        return os.fdopen(fd, "rb")

    def delete(self, uri: str) -> None:
        path = self._path(key_from_uri(uri))
        try:
            path.unlink()
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError as exc:
            raise _failure("delete", exc) from None


def _sync_directory(directory: Path) -> None:
    """Persist a rename on POSIX by syncing its directory; best effort, not supported on Windows."""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning("Storage directory sync failed", extra={"error": type(exc).__name__, "errno": exc.errno})


@lru_cache
def get_storage() -> Storage:
    """Return the process-wide storage, rooted at the configured ``storage_root``."""
    return LocalFileStorage(get_settings().storage_root)
