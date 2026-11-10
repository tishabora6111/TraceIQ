import io
import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core import storage as storage_module
from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError
from app.core.storage import (
    CHUNK_SIZE,
    LOCAL_URI_PREFIX,
    MAX_KEY_LENGTH,
    MAX_SEGMENT_LENGTH,
    LocalFileStorage,
    Storage,
    StorageError,
    StorageObjectNotFoundError,
    StorageValidationError,
    get_storage,
    key_from_uri,
    validate_key,
)

INVALID_KEYS = [
    "",
    "/absolute",
    "/",
    "trailing/",
    "double//slash",
    ".",
    "..",
    "../escape",
    "a/../../escape",
    "a/./b",
    ".hidden",
    "a/.hidden",
    ".tmp-abc",
    "back\\slash",
    "..\\escape",
    "C:",
    "C:/windows",
    "c:file",
    "nul\x00byte",
    "space here",
    "new\nline",
    "trailing\n",
    "percent%2e%2e",
    "unicodé",
    "fullwidth\uff0e\uff0e",
    "a" * (MAX_SEGMENT_LENGTH + 1),
    "/".join(["a" * 100] * 6),
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "root"


@pytest.fixture
def store(root: Path) -> LocalFileStorage:
    return LocalFileStorage(root)


def _read(store: LocalFileStorage, uri: str) -> bytes:
    with store.open(uri) as handle:
        return handle.read()


def _all_files(path: Path) -> list[str]:
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())


class _ChunkRecorder(io.BytesIO):
    """A stream that records the size of each read request."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.requests: list[int] = []

    def read(self, size: int | None = -1) -> bytes:
        self.requests.append(-1 if size is None else size)
        return super().read(size)


class _FailingStream(io.RawIOBase):
    """Yields some data, then fails like a broken upload."""

    def __init__(self, error: BaseException, good_chunks: int = 2) -> None:
        self.error = error
        self.remaining = good_chunks

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self.remaining == 0:
            raise self.error
        self.remaining -= 1
        return b"x" * 1000


class _ScriptedStream(io.RawIOBase):
    """Returns the scripted results of successive read() calls, like a misbehaving stream."""

    def __init__(self, *results: object) -> None:
        self.results = list(results)

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> object:  # type: ignore[override]
        return self.results.pop(0) if self.results else b""


# --- Keys and URIs ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["a", "artifact.bin", "ml_monitoring/0f3c/model-v1.2/artifact.pkl", "A-Z_0.9"])
def test_valid_keys(key: str) -> None:
    assert validate_key(key) == key


def test_key_at_maximum_lengths_is_valid(store: LocalFileStorage) -> None:
    longest = "s" * MAX_SEGMENT_LENGTH
    key = "/".join([longest, "t" * (MAX_KEY_LENGTH - MAX_SEGMENT_LENGTH - 3), "u"])

    assert len(key) == MAX_KEY_LENGTH
    assert validate_key(key) == key
    assert _read(store, store.save(key, b"data")) == b"data"
    with pytest.raises(StorageValidationError):
        validate_key(key + "x")


@pytest.mark.parametrize("key", INVALID_KEYS)
def test_invalid_keys_are_rejected(store: LocalFileStorage, root: Path, key: str) -> None:
    with pytest.raises(StorageValidationError):
        validate_key(key)
    with pytest.raises(StorageValidationError):
        store.save(key, b"data")
    assert not root.exists() or _all_files(root) == []


@pytest.mark.parametrize("key", [None, 42, b"bytes"])
def test_non_string_keys_are_rejected(key: object) -> None:
    with pytest.raises(StorageValidationError):
        validate_key(key)  # type: ignore[arg-type]


def test_uri_format(store: LocalFileStorage) -> None:
    uri = store.save("projects/p1/file.bin", b"data")

    assert uri == "local:///projects/p1/file.bin"
    assert uri.startswith(LOCAL_URI_PREFIX)
    assert key_from_uri(uri) == "projects/p1/file.bin"


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "projects/p1/file.bin",
        "local://projects/p1/file.bin",
        "local:/projects/p1/file.bin",
        "LOCAL:///projects/p1/file.bin",
        "file:///projects/p1/file.bin",
        "s3:///projects/p1/file.bin",
        " local:///projects/p1/file.bin",
        "local:///",
        "local:////etc/passwd",
        "local:///../escape",
        "local:///a/../../escape",
        "local:///%2e%2e/escape",
        "local:///a\\b",
        "local:///C:/windows",
        "local:///a?query",
        "local:///a#fragment",
        "local:///a\x00b",
    ],
)
def test_invalid_uris_are_rejected(store: LocalFileStorage, uri: str) -> None:
    with pytest.raises(StorageValidationError):
        key_from_uri(uri)
    with pytest.raises(StorageValidationError):
        store.open(uri)
    with pytest.raises(StorageValidationError):
        store.delete(uri)


@pytest.mark.parametrize("uri", [None, 42, b"local:///a"])
def test_non_string_uris_are_rejected(uri: object) -> None:
    with pytest.raises(StorageValidationError):
        key_from_uri(uri)  # type: ignore[arg-type]


# --- Save and open ---------------------------------------------------------------------------


def test_local_storage_satisfies_protocol(store: LocalFileStorage) -> None:
    typed: Storage = store
    assert typed is store


def test_save_and_open_bytes(store: LocalFileStorage, root: Path) -> None:
    uri = store.save("a/b/data.bin", b"\x00\x01binary\xff")

    assert _read(store, uri) == b"\x00\x01binary\xff"
    assert (root / "a" / "b" / "data.bin").read_bytes() == b"\x00\x01binary\xff"


@pytest.mark.parametrize("data", [bytearray(b"bytearray"), memoryview(b"memoryview")])
def test_save_accepts_bytes_like(store: LocalFileStorage, data: bytearray | memoryview) -> None:
    assert _read(store, store.save("k", data)) == bytes(data)


def test_save_empty_file(store: LocalFileStorage) -> None:
    assert _read(store, store.save("empty", b"")) == b""


def test_save_stream(store: LocalFileStorage) -> None:
    assert _read(store, store.save("stream.bin", io.BytesIO(b"streamed"))) == b"streamed"


def test_save_file_object(store: LocalFileStorage, tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"from a file")

    with source.open("rb") as handle:
        uri = store.save("copy.bin", handle)

    assert _read(store, uri) == b"from a file"


def test_large_stream_is_read_in_bounded_chunks(store: LocalFileStorage) -> None:
    data = os.urandom(3 * CHUNK_SIZE + 12345)
    stream = _ChunkRecorder(data)

    uri = store.save("large.bin", stream)

    assert _read(store, uri) == data
    assert stream.requests and all(0 < size <= CHUNK_SIZE for size in stream.requests)
    assert len(stream.requests) == 5  # four data chunks, then the empty read that ends the copy


def test_large_bytes_are_written_in_bounded_chunks(
    store: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = os.urandom(2 * CHUNK_SIZE + 7)
    sizes: list[int] = []
    original_fdopen = os.fdopen

    def recording_fdopen(fd: int, *args: object, **kwargs: object) -> io.BufferedWriter:
        handle = original_fdopen(fd, *args, **kwargs)  # type: ignore[arg-type]
        original_write = handle.write

        def write(chunk: bytes) -> int:
            sizes.append(len(chunk))
            return original_write(chunk)

        handle.write = write  # type: ignore[method-assign]
        return handle  # type: ignore[return-value]

    monkeypatch.setattr(storage_module.os, "fdopen", recording_fdopen)
    uri = store.save("large.bin", data)
    monkeypatch.undo()

    assert _read(store, uri) == data
    assert sizes == [CHUNK_SIZE, CHUNK_SIZE, 7]


def test_open_returns_context_manager_that_closes(store: LocalFileStorage) -> None:
    uri = store.save("k", b"data")

    with store.open(uri) as handle:
        assert handle.read(2) == b"da"
    assert handle.closed


def test_root_is_created_on_first_save(store: LocalFileStorage, root: Path) -> None:
    assert not root.exists()
    store.save("k", b"data")
    assert root.is_dir()


def test_relative_root_resolves_against_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store = LocalFileStorage(Path("data/storage"))
    uri = store.save("k", b"data")

    assert (tmp_path / "data" / "storage" / "k").read_bytes() == b"data"
    # The root was resolved once, at construction: changing directory later does not move it.
    monkeypatch.chdir(tmp_path.parent)
    assert _read(store, uri) == b"data"


# --- Missing objects and deletion ------------------------------------------------------------


def test_open_missing_object(store: LocalFileStorage) -> None:
    with pytest.raises(StorageObjectNotFoundError) as raised:
        store.open("local:///missing/file.bin")

    assert isinstance(raised.value, NotFoundError)
    assert isinstance(raised.value, StorageError)
    assert raised.value.status_code == 404
    assert raised.value.code == "storage_object_not_found"


def test_open_missing_when_parent_is_a_file(store: LocalFileStorage) -> None:
    store.save("file", b"data")
    with pytest.raises(StorageObjectNotFoundError):
        store.open("local:///file/child")


def test_open_directory_is_not_found(store: LocalFileStorage) -> None:
    store.save("dir/file", b"data")
    with pytest.raises(StorageObjectNotFoundError):
        store.open("local:///dir")


def test_delete_removes_file(store: LocalFileStorage) -> None:
    uri = store.save("a/file", b"data")

    store.delete(uri)

    with pytest.raises(StorageObjectNotFoundError):
        store.open(uri)


def test_delete_is_idempotent(store: LocalFileStorage) -> None:
    uri = store.save("a/file", b"data")

    store.delete(uri)
    store.delete(uri)
    store.delete("local:///never/existed")
    store.delete("local:///a/file/below-a-missing-file")


def test_delete_directory_fails_without_removing_it(store: LocalFileStorage, root: Path) -> None:
    store.save("dir/file", b"data")

    with pytest.raises(StorageError) as raised:
        store.delete("local:///dir")

    assert not isinstance(raised.value, StorageObjectNotFoundError)
    assert (root / "dir" / "file").read_bytes() == b"data"


# --- Overwrite and atomicity -----------------------------------------------------------------


def test_save_replaces_existing_file(store: LocalFileStorage, root: Path) -> None:
    first = store.save("k", b"old content, longer")
    second = store.save("k", b"new")

    assert first == second
    assert _read(store, second) == b"new"
    assert _all_files(root) == ["k"]


def test_open_handle_keeps_old_content_across_replace(store: LocalFileStorage) -> None:
    uri = store.save("k", b"old")

    with store.open(uri) as handle:
        store.save("k", b"new")
        assert handle.read() == b"old"
    assert _read(store, uri) == b"new"


def test_replace_uses_os_replace_from_same_directory(
    store: LocalFileStorage, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def recording_replace(src: str, dst: str) -> None:
        calls.append((Path(src), Path(dst)))
        original_replace(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", recording_replace)
    store.save("a/k", b"data")

    [(src, dst)] = calls
    assert dst == root.resolve() / "a" / "k"
    assert src.parent == dst.parent
    assert src.name.startswith(".tmp-")


@pytest.mark.parametrize(
    "error", [OSError(28, "No space left on device"), ValueError("bad upload"), KeyboardInterrupt()]
)
def test_failed_write_leaves_no_files(store: LocalFileStorage, root: Path, error: BaseException) -> None:
    expected = StorageError if isinstance(error, OSError) else type(error)

    with pytest.raises(expected):
        store.save("a/k", _FailingStream(error))

    assert _all_files(root) == []


def test_failed_write_preserves_existing_file(store: LocalFileStorage, root: Path) -> None:
    uri = store.save("a/k", b"original")

    with pytest.raises(StorageError):
        store.save("a/k", _FailingStream(OSError(5, "I/O error")))

    assert _read(store, uri) == b"original"
    assert _all_files(root) == ["a/k"]


def test_text_stream_is_rejected_without_leftovers(store: LocalFileStorage, root: Path) -> None:
    with pytest.raises(TypeError):
        store.save("k", io.StringIO("text"))  # type: ignore[arg-type]

    assert _all_files(root) == []


@pytest.mark.parametrize(
    ("bad_chunk", "message"),
    [
        (None, "returned None"),
        ("text", "must return bytes, not str"),
        (42, "must return bytes, not int"),
        ([b"x"], "must return bytes, not list"),
    ],
)
@pytest.mark.parametrize("existing", [None, b"original content"])
def test_stream_returning_bad_chunk_fails_cleanly(
    store: LocalFileStorage, root: Path, bad_chunk: object, message: str, existing: bytes | None
) -> None:
    """A stream that yields valid bytes and then None or a non-bytes value must not be saved as a
    complete (truncated) file. Nothing partial remains, and an existing file is unchanged."""
    if existing is not None:
        store.save("a/k", existing)

    with pytest.raises(TypeError, match=message):
        store.save("a/k", _ScriptedStream(b"valid bytes", bad_chunk, b"never read"))  # type: ignore[arg-type]

    assert _all_files(root) == ([] if existing is None else ["a/k"])
    if existing is not None:
        assert _read(store, "local:///a/k") == existing


def test_stream_chunks_may_be_any_bytes_like(store: LocalFileStorage) -> None:
    stream = _ScriptedStream(b"bytes,", bytearray(b"bytearray,"), memoryview(b"memoryview"))

    assert _read(store, store.save("k", stream)) == b"bytes,bytearray,memoryview"  # type: ignore[arg-type]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_saved_files_are_private(store: LocalFileStorage, root: Path) -> None:
    store.save("a/k", b"data")
    store.save("a/k", b"replaced")

    assert stat.S_IMODE((root / "a" / "k").stat().st_mode) == 0o600


def test_failed_replace_preserves_existing_file(
    store: LocalFileStorage, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = store.save("k", b"original")

    def failing_replace(src: str, dst: str) -> None:
        raise PermissionError(13, "Permission denied", dst)

    monkeypatch.setattr(storage_module.os, "replace", failing_replace)
    with pytest.raises(StorageError):
        store.save("k", b"new")
    monkeypatch.undo()

    assert _read(store, uri) == b"original"
    assert _all_files(root) == ["k"]


def test_failed_fsync_leaves_no_files(store: LocalFileStorage, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_fsync(fd: int) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(storage_module.os, "fsync", failing_fsync)
    with pytest.raises(StorageError):
        store.save("k", b"data")
    monkeypatch.undo()

    assert _all_files(root) == []


def test_saving_onto_a_directory_fails_cleanly(store: LocalFileStorage, root: Path) -> None:
    store.save("dir/file", b"data")

    with pytest.raises(StorageError):
        store.save("dir", b"data")

    assert _all_files(root) == ["dir/file"]


def test_saving_below_a_file_fails(store: LocalFileStorage, root: Path) -> None:
    store.save("file", b"data")

    with pytest.raises(StorageError):
        store.save("file/child", b"data")

    assert _all_files(root) == ["file"]


# --- Symbolic links --------------------------------------------------------------------------


def _symlink(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not supported here")


def test_symlinked_directory_escape_is_rejected(store: LocalFileStorage, root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    root.mkdir()
    _symlink(root / "link", outside, directory=True)

    with pytest.raises(StorageValidationError):
        store.open("local:///link/secret")
    with pytest.raises(StorageValidationError):
        store.save("link/planted", b"data")
    with pytest.raises(StorageValidationError):
        store.delete("local:///link/secret")

    assert sorted(p.name for p in outside.iterdir()) == ["secret"]
    assert (outside / "secret").read_bytes() == b"secret"


def test_symlinked_file_is_rejected(store: LocalFileStorage, root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret"
    outside.write_bytes(b"secret")
    root.mkdir()
    _symlink(root / "file", outside, directory=False)

    with pytest.raises(StorageValidationError):
        store.open("local:///file")
    with pytest.raises(StorageValidationError):
        store.save("file", b"overwrite")
    with pytest.raises(StorageValidationError):
        store.delete("local:///file")

    assert outside.read_bytes() == b"secret"
    assert (root / "file").is_symlink()


def test_symlink_inside_root_is_also_rejected(store: LocalFileStorage, root: Path) -> None:
    store.save("real/file", b"data")
    _symlink(root / "alias", root / "real", directory=True)

    with pytest.raises(StorageValidationError):
        store.open("local:///alias/file")


def test_symlinked_root_is_allowed(tmp_path: Path) -> None:
    real = tmp_path / "real-root"
    real.mkdir()
    _symlink(tmp_path / "root-link", real, directory=True)

    uri = LocalFileStorage(tmp_path / "root-link").save("k", b"data")

    assert (real / "k").read_bytes() == b"data"
    assert _read(LocalFileStorage(real), uri) == b"data"


def _rejection_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.core.storage" and r.getMessage() == "Storage path rejected"]


def _assert_no_paths(records: list[logging.LogRecord], tmp_path: Path) -> None:
    for record in records:
        rendered = record.getMessage() + " " + repr(record.__dict__)
        assert str(tmp_path) not in rendered
        assert tmp_path.name not in rendered
        assert "secret" not in rendered  # neither the key nor the link target
        assert record.exc_info is None


def test_symlink_rejection_logs_a_warning_without_paths(
    store: LocalFileStorage, root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    _symlink(root / "link", outside, directory=True)

    with caplog.at_level(logging.WARNING, logger="app.core.storage"):
        for action in (
            lambda: store.open("local:///link/secret"),
            lambda: store.save("link/secret", b"data"),
            lambda: store.delete("local:///link/secret"),
        ):
            with pytest.raises(StorageValidationError):
                action()

    warnings = _rejection_warnings(caplog)
    assert len(warnings) == 3
    assert all(r.levelno == logging.WARNING and r.reason == "symbolic_link" for r in warnings)  # type: ignore[attr-defined]
    _assert_no_paths(warnings, tmp_path)


def test_containment_rejection_logs_a_warning_without_paths(
    store: LocalFileStorage,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The resolved-path check backs up the symlink check: with symlink detection disabled, an
    escaping link is still refused by containment, and that is logged too."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    root.mkdir()
    _symlink(root / "link", outside, directory=True)
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)

    with caplog.at_level(logging.WARNING, logger="app.core.storage"):
        with pytest.raises(StorageValidationError, match="outside the storage root"):
            store.open("local:///link/secret")

    [warning] = _rejection_warnings(caplog)
    assert warning.levelno == logging.WARNING
    assert warning.reason == "outside_root"  # type: ignore[attr-defined]
    _assert_no_paths([warning], tmp_path)


# --- Errors never expose paths ---------------------------------------------------------------


def _failure_cases(store: LocalFileStorage, root: Path, monkeypatch: pytest.MonkeyPatch) -> list[StorageError]:
    errors: list[StorageError] = []

    def capture(action: object) -> None:
        try:
            action()  # type: ignore[operator]
        except StorageError as exc:
            errors.append(exc)
        else:
            pytest.fail("expected a StorageError")

    store.save("dir/file", b"data")
    capture(lambda: store.open("local:///missing"))
    capture(lambda: store.save("dir", b"data"))
    capture(lambda: store.delete("local:///dir"))
    capture(lambda: store.save("dir/file/child", b"data"))
    capture(lambda: store.save("../escape", b"data"))

    def failing_open(*args: object, **kwargs: object) -> int:
        raise PermissionError(13, "Permission denied", str(root / "dir" / "file"))

    monkeypatch.setattr(storage_module.os, "open", failing_open)
    capture(lambda: store.open("local:///dir/file"))
    monkeypatch.undo()
    return errors


def test_errors_never_contain_paths(
    store: LocalFileStorage, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors = _failure_cases(store, root, monkeypatch)

    assert len(errors) == 6
    for error in errors:
        rendered = " ".join([str(error), error.message, repr(error.details), repr(error)])
        assert str(tmp_path) not in rendered
        assert tmp_path.name not in rendered
        # Chained OSErrors carry the path; they are suppressed, not attached as the cause.
        assert error.__cause__ is None
        assert error.__context__ is None or error.__suppress_context__


def test_logs_never_contain_paths(
    store: LocalFileStorage,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="app.core.storage"):
        _failure_cases(store, root, monkeypatch)

    assert caplog.records, "OS failures are logged"
    for record in caplog.records:
        rendered = record.getMessage() + " " + repr(record.__dict__)
        assert str(tmp_path) not in rendered
        assert tmp_path.name not in rendered
        assert record.exc_info is None


def test_storage_errors_are_app_errors() -> None:
    assert issubclass(StorageError, AppError)
    assert StorageError("x").status_code == 500
    assert StorageValidationError("x").status_code == 500
    assert StorageValidationError("x").code == "invalid_storage_reference"


# --- Factory ---------------------------------------------------------------------------------


@pytest.fixture
def _fresh_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    get_settings.cache_clear()
    get_storage.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()
    get_storage.cache_clear()


@pytest.mark.usefixtures("_fresh_factory")
def test_get_storage_uses_configured_root_and_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRACEIQ_STORAGE_ROOT", str(tmp_path / "configured"))

    storage = get_storage()
    uri = storage.save("k", b"data")

    assert isinstance(storage, LocalFileStorage)
    assert get_storage() is storage
    assert (tmp_path / "configured" / "k").read_bytes() == b"data"
    assert uri == "local:///k"


@pytest.mark.usefixtures("_fresh_factory")
def test_get_storage_cache_can_be_cleared_for_a_new_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRACEIQ_STORAGE_ROOT", str(tmp_path / "first"))
    first = get_storage()
    first.save("k", b"first")

    monkeypatch.setenv("TRACEIQ_STORAGE_ROOT", str(tmp_path / "second"))
    get_settings.cache_clear()
    get_storage.cache_clear()
    second = get_storage()

    assert second is not first
    with pytest.raises(StorageObjectNotFoundError):
        second.open("local:///k")
