#!/usr/bin/env python3
"""Lift folder contents onto pCloud and leave 0-byte placeholders.

The vault defaults to the rclone remote pcloud:.lifted_files, so the bytes do
not stay on local disk. Each person connects their own pCloud account with
`rclone config` (remote name `pcloud`). Set LIFDROP_STORE to a directory for a
local vault, or to remote:path for another rclone destination. Set
LIFDROP_RCLONE to override the rclone binary.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

STORE_ENV = "LIFDROP_STORE"
RCLONE_ENV = "LIFDROP_RCLONE"
DEFAULT_REMOTE = "pcloud:.lifted_files"
ARCHIVE_NAME = "tree.zip"
PARTIAL_SUFFIX = ".lifdrop-partial"
CHUNK_SIZE = 1024 * 1024
_FSYNC_SOFT_ERRNOS = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP})


class UserError(Exception):
    """The request cannot be carried out. Exit status 1."""


class OperationalError(Exception):
    """Storage or the folder could not be updated safely. Exit status 2."""


class Logger:
    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet

    def info(self, message: str) -> None:
        if not self.quiet:
            print(f"lifdrop: {message}")

    def warn(self, message: str) -> None:
        print(f"lifdrop: warning: {message}", file=sys.stderr)


def _human_bytes(amount: int) -> str:
    value = float(amount)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{amount} B"


class Progress:
    """A single-line bar on stderr. Quiet mode prints nothing."""

    def __init__(self, logger: Logger, label: str, total: int, files: int | None = None) -> None:
        self.logger = logger
        self.label = label
        self.total = max(total, 0)
        self.files = files
        self.done = 0
        self.file_index = 0
        self.current = ""
        self.unit = "bytes"
        self._last = 0.0
        self._width = 0
        self._active = False
        self._finished = False

    def start_file(self, name: str) -> None:
        self.file_index += 1
        self.current = name
        self._draw(force=True)

    def advance(self, amount: int) -> None:
        self.done += amount
        self._draw()

    def note(self, text: str) -> None:
        if self.logger.quiet:
            return
        line = f"lifdrop: {self.label} {text}"
        self._paint(line)

    def finish(self) -> None:
        if self._finished:
            return
        self._draw(force=True)
        self.close()

    def close(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._active:
            print(file=sys.stderr)
            self._active = False

    def _draw(self, force: bool = False) -> None:
        if self.logger.quiet:
            return
        now = time.monotonic()
        if not force and now - self._last < 0.1:
            return
        self._last = now
        width = 28
        fraction = 1.0 if self.total == 0 else min(1.0, self.done / self.total)
        filled = int(width * fraction)
        bar = "#" * filled + "-" * (width - filled)
        if self.unit == "bytes":
            amount = f"{_human_bytes(min(self.done, self.total))}/{_human_bytes(self.total)}"
        else:
            amount = f"{min(self.done, self.total)}/{self.total}"
        files = f" {self.file_index}/{self.files}" if self.files is not None else ""
        name = f" {self.current}" if self.current else ""
        line = f"lifdrop: {self.label} {fraction * 100:5.1f}% [{bar}] {amount}{files}{name}"
        self._paint(line)

    def _paint(self, line: str) -> None:
        columns = shutil.get_terminal_size((80, 24)).columns
        if len(line) >= columns:
            line = line[: columns - 1]
        gap = self._width - len(line)
        self._width = len(line)
        print("\r" + line + (" " * gap if gap > 0 else ""), end="", file=sys.stderr, flush=True)
        self._active = True


def _is_rclone_spec(raw: str) -> bool:
    if raw.startswith("~") or raw.startswith("/"):
        return False
    remote, sep, _path = raw.partition(":")
    return bool(sep) and bool(remote) and "/" not in remote and "\\" not in remote


def checksums(path: Path, progress: Progress | None = None) -> tuple[str, str, int]:
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
            if progress is not None:
                progress.advance(len(chunk))
    return sha.hexdigest(), md5.hexdigest(), size


def _zip_info(rel: str, info: os.stat_result) -> zipfile.ZipInfo:
    stamp = time.localtime(info.st_mtime)
    if stamp.tm_year < 1980:
        date_time = (1980, 1, 1, 0, 0, 0)
    else:
        date_time = stamp[:6]
    member = zipfile.ZipInfo(filename=rel, date_time=date_time)
    member.compress_type = zipfile.ZIP_STORED
    member.create_system = 3
    member.external_attr = (info.st_mode & 0xFFFF) << 16
    return member


def _grant_dir_write(directory: Path) -> int | None:
    """If the owner cannot create files here, add the write bit and return the old mode."""
    probe = directory / f".lifdrop-access-{os.getpid()}"
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except PermissionError:
        info = directory.lstat()
        if not _owns(info):
            raise
        mode = stat.S_IMODE(info.st_mode)
        os.chmod(directory, mode | stat.S_IWUSR | stat.S_IXUSR)
        return mode
    os.close(fd)
    probe.unlink()
    return None


def _owns(info: os.stat_result) -> bool:
    return info.st_uid == os.geteuid()


def _stream_member(
    archive: zipfile.ZipFile,
    src: Path,
    rel: str,
    info: os.stat_result,
    progress: Progress | None,
) -> tuple[str, str, int]:
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    member = _zip_info(rel, info)
    with src.open("rb") as incoming, archive.open(member, "w", force_zip64=True) as outgoing:
        for chunk in iter(lambda: incoming.read(CHUNK_SIZE), b""):
            outgoing.write(chunk)
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
            if progress is not None:
                progress.advance(len(chunk))
    return sha.hexdigest(), md5.hexdigest(), size


def _write_member(
    archive: zipfile.ZipFile,
    src: Path,
    rel: str,
    info: os.stat_result,
    progress: Progress | None,
) -> tuple[str, str, int]:
    """Read src even when the owner left it without the read bit. The mode is restored."""
    mode = stat.S_IMODE(info.st_mode)
    try:
        return _stream_member(archive, src, rel, info, progress)
    except PermissionError:
        if not _owns(info):
            raise
        os.chmod(src, mode | stat.S_IRUSR)
        try:
            return _stream_member(archive, src, rel, info, progress)
        finally:
            os.chmod(src, mode)


def _empty_placeholder(src: Path, mode: int, atime_ns: int, mtime_ns: int) -> None:
    """Truncate src to 0 bytes and keep its original mode, even if that mode is read-only."""
    writable = False
    try:
        os.truncate(src, 0)
    except PermissionError:
        info = src.lstat()
        if not _owns(info):
            raise
        os.chmod(src, mode | stat.S_IWUSR)
        writable = True
        os.truncate(src, 0)
    try:
        if src.lstat().st_size != 0:
            raise OperationalError(f"failed to placeholder {src}")
        os.utime(src, ns=(atime_ns, mtime_ns))
    finally:
        if writable:
            os.chmod(src, mode)


def build_archive(
    source: Path,
    files: list[Path],
    progress: Progress | None = None,
) -> tuple[Path, list[dict]]:
    """Pack regular files into one uncompressed zip. The caller deletes the temp file."""
    handle = tempfile.NamedTemporaryFile(prefix="lifdrop-", suffix=".zip", delete=False)
    tmp = Path(handle.name)
    handle.close()
    entries: list[dict] = []
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for src in files:
                rel = _rel(source, src)
                info = src.lstat()
                if progress is not None:
                    progress.start_file(rel)
                digest, md5, size = _write_member(archive, src, rel, info, progress)
                if size != info.st_size:
                    raise OperationalError(f"{rel} changed while reading")
                entries.append(
                    {
                        "md5": md5,
                        "mode": stat.S_IMODE(info.st_mode),
                        "mtime_ns": info.st_mtime_ns,
                        "rel": rel,
                        "sha256": digest,
                        "size": info.st_size,
                    }
                )
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, entries


def archive_meta(manifest: dict) -> dict:
    archive = manifest.get("archive")
    if not isinstance(archive, dict):
        raise OperationalError("manifest is missing the archive")
    sha = archive.get("sha256")
    md5 = archive.get("md5")
    size = archive.get("size")
    if not isinstance(sha, str) or len(sha) != 64 or not isinstance(md5, str) or len(md5) != 32:
        raise OperationalError("manifest archive checksum is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise OperationalError("manifest archive size is invalid")
    if archive.get("name") != ARCHIVE_NAME:
        raise OperationalError("manifest archive name is invalid")
    return archive


def _check_archive(path: Path, meta: dict) -> None:
    sha, md5, size = checksums(path)
    if sha != meta["sha256"] or md5 != meta["md5"].lower() or size != meta["size"]:
        raise OperationalError("stored archive checksum mismatch")


def _restore_member(
    archive: zipfile.ZipFile,
    entry: dict,
    dest: Path,
    progress: Progress | None = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    granted = _grant_dir_write(dest.parent)
    tmp = dest.with_name(dest.name + PARTIAL_SUFFIX)
    size = 0
    try:
        with archive.open(entry["rel"], "r") as incoming, tmp.open("wb") as outgoing:
            for chunk in iter(lambda: incoming.read(CHUNK_SIZE), b""):
                outgoing.write(chunk)
                size += len(chunk)
                if progress is not None:
                    progress.advance(len(chunk))
            outgoing.flush()
            _fsync_file(outgoing, strict=False)
        # The zip checksum already covers these bytes. A short write is the remaining failure.
        if size != entry["size"]:
            raise OperationalError(f"restored size mismatch: {entry['rel']}")
        os.chmod(tmp, entry["mode"])
        os.replace(tmp, dest)
        _fsync_dir(dest.parent, strict=False)
        os.utime(dest, ns=(entry["mtime_ns"], entry["mtime_ns"]))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if granted is not None:
            os.chmod(dest.parent, granted)


def storage_key(source: Path) -> str:
    return hashlib.sha256(str(source).encode("utf-8")).hexdigest()


def resolve_folder(folder: str) -> Path:
    path = Path(folder).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise UserError(f"{path} does not exist") from None
    if not resolved.is_dir():
        raise UserError(f"{resolved} is not a directory")
    return resolved


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return path != parent


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _fsync_file(handle, *, strict: bool) -> None:
    try:
        os.fsync(handle.fileno())
    except OSError as exc:
        if strict or exc.errno not in _FSYNC_SOFT_ERRNOS:
            raise


def _fsync_dir(path: Path, *, strict: bool) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if strict or exc.errno not in _FSYNC_SOFT_ERRNOS:
                raise
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + PARTIAL_SUFFIX)
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            _fsync_file(handle, strict=True)
        os.replace(tmp, path)
        _fsync_dir(path.parent, strict=True)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def save_manifest(slot: Path, manifest: dict) -> None:
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(slot / "manifest.json", payload)


def parse_manifest(text: str, label: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OperationalError(f"cannot read manifest {label}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise OperationalError(f"invalid manifest {label}")
    if not isinstance(data.get("source"), str) or not isinstance(data.get("status"), str):
        raise OperationalError(f"invalid manifest {label}")
    return data


def load_manifest(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperationalError(f"cannot read manifest {path}: {exc}") from exc
    return parse_manifest(text, str(path))


def manifest_entries(manifest: dict) -> list[dict]:
    entries: list[dict] = []
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise OperationalError("manifest entry is invalid")
        rel = entry.get("rel")
        mode = entry.get("mode")
        size = entry.get("size")
        mtime_ns = entry.get("mtime_ns")
        digest = entry.get("sha256")
        if not isinstance(rel, str) or rel == "":
            raise OperationalError("manifest entry is missing a path")
        parts = Path(rel).parts
        if Path(rel).is_absolute() or ".." in parts or rel.startswith("/"):
            raise OperationalError(f"manifest path escapes the folder: {rel}")
        if isinstance(mode, bool) or not isinstance(mode, int):
            raise OperationalError(f"manifest mode is invalid for {rel}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise OperationalError(f"manifest size is invalid for {rel}")
        if isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int):
            raise OperationalError(f"manifest mtime is invalid for {rel}")
        if not isinstance(digest, str) or len(digest) != 64:
            raise OperationalError(f"manifest checksum is invalid for {rel}")
        entries.append(entry)
    return entries


def safe_join(root: Path, rel: str) -> Path:
    parts = Path(rel).parts
    if Path(rel).is_absolute() or ".." in parts:
        raise OperationalError(f"unsafe relative path: {rel}")
    return root.joinpath(*parts)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_verified(src: Path, dest: Path, *, strict_fsync: bool) -> str:
    """Copy src onto dest via a temp file on dest's filesystem, then replace.

    Rename is never used to move src, so a virtual mount cannot fail with EXDEV.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + PARTIAL_SUFFIX)
    digest = hashlib.sha256()
    size = 0
    try:
        with src.open("rb") as source, tmp.open("wb") as target:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            target.flush()
            _fsync_file(target, strict=strict_fsync)
        if tmp.stat().st_size != size:
            raise OperationalError(f"size mismatch copying {src}")
        on_disk = sha256_file(tmp)
        expected = digest.hexdigest()
        if on_disk != expected:
            raise OperationalError(f"checksum mismatch copying {src}")
        os.replace(tmp, dest)
        _fsync_dir(dest.parent, strict=strict_fsync)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return expected


def guard_store_overlap(source: Path, root: Path) -> None:
    if source == root or _is_within(source, root) or _is_within(root, source):
        raise UserError("refusing to lift the lifdrop storage directory")


def conflict_message(source: Path, manifests: list[dict]) -> str | None:
    for manifest in manifests:
        other = Path(manifest["source"])
        if other == source:
            continue
        if _is_within(source, other) or _is_within(other, source):
            return f"{other} is already lifted; refusing to lift {source}"
    return None


class LocalStore:
    """Vault on a normal filesystem. Used when LIFDROP_STORE is a directory."""

    def __init__(self, raw: str) -> None:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        self.root = path

    def label(self) -> str:
        return str(self.root)

    def guard(self, source: Path) -> None:
        guard_store_overlap(source, self.root.resolve(strict=False))

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()

    def read_manifest(self, key: str) -> dict | None:
        path = self.root / key / "manifest.json"
        if not path.is_file():
            return None
        return load_manifest(path)

    def write_manifest(self, key: str, manifest: dict) -> None:
        save_manifest(self.root / key, manifest)

    def iter_manifests(self) -> list[dict]:
        found: list[dict] = []
        if not self.root.is_dir():
            return found
        for child in self.root.iterdir():
            manifest_path = child / "manifest.json"
            if child.is_dir() and manifest_path.is_file():
                found.append(load_manifest(manifest_path))
        return found

    def put_archive(
        self,
        key: str,
        archive: Path,
        *,
        sha256: str,
        md5: str,
        size: int,
        progress: Progress | None = None,
    ) -> None:
        del progress
        dest = self.root / key / ARCHIVE_NAME
        digest = copy_verified(archive, dest, strict_fsync=True)
        _sha, got_md5, got_size = checksums(dest)
        if digest != sha256 or got_md5 != md5 or got_size != size:
            dest.unlink(missing_ok=True)
            raise OperationalError("stored archive checksum mismatch")

    def fetch_archive(self, key: str, meta: dict, progress: Progress | None = None) -> tuple[Path, bool]:
        del progress
        path = self.root / key / ARCHIVE_NAME
        if not path.is_file() or path.is_symlink():
            raise OperationalError("stored archive is missing")
        _check_archive(path, meta)
        return path, False

    def verify_file(self, key: str, entry: dict) -> None:
        stored = safe_join(self.root / key / "files", entry["rel"])
        if not stored.is_file() or stored.is_symlink():
            raise OperationalError(f"stored file missing: {entry['rel']}")
        if stored.stat().st_size != entry["size"] or sha256_file(stored) != entry["sha256"]:
            raise OperationalError(f"stored checksum mismatch: {entry['rel']}")

    def restore_file(self, key: str, rel: str, dest: Path) -> str:
        granted = _grant_dir_write(dest.parent)
        try:
            stored = safe_join(self.root / key / "files", rel)
            return copy_verified(stored, dest, strict_fsync=False)
        finally:
            if granted is not None:
                os.chmod(dest.parent, granted)

    def discard(self, key: str) -> None:
        slot = self.root / key
        if slot.exists():
            shutil.rmtree(slot)


class RcloneStore:
    """Vault on rclone, defaulting to pCloud so lifted bytes leave local disk."""

    def __init__(self, spec: str) -> None:
        if not _is_rclone_spec(spec):
            raise UserError(f"invalid rclone store {spec}")
        self.spec = spec.rstrip("/")

    def label(self) -> str:
        return self.spec

    def guard(self, source: Path) -> None:
        return None

    def prepare(self) -> None:
        self._run(["mkdir", self.spec])

    def read_manifest(self, key: str) -> dict | None:
        remote = self._join(key, "manifest.json")
        result = self._run(["cat", remote], missing_ok=True)
        if result is None:
            return None
        return parse_manifest(result.stdout, remote)

    def write_manifest(self, key: str, manifest: dict) -> None:
        payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        tmp = Path(handle.name)
        try:
            handle.write(payload)
            handle.close()
            self._run(["copyto", str(tmp), self._join(key, "manifest.json")])
        finally:
            tmp.unlink(missing_ok=True)

    def iter_manifests(self) -> list[dict]:
        result = self._run(["lsf", self.spec, "--dirs-only"], missing_ok=True)
        if result is None:
            return []
        found: list[dict] = []
        for line in result.stdout.splitlines():
            name = line.strip().strip("/")
            if not name or "/" in name:
                continue
            manifest = self.read_manifest(name)
            if manifest is not None:
                found.append(manifest)
        return found

    def put_archive(
        self,
        key: str,
        archive: Path,
        *,
        sha256: str,
        md5: str,
        size: int,
        progress: Progress | None = None,
    ) -> None:
        del sha256
        remote = self._join(key, ARCHIVE_NAME)
        self._run(["copyto", str(archive), remote], progress=progress)
        got_md5, got_size = self._md5_size(remote)
        if got_md5 != md5 or got_size != size:
            self._run(["deletefile", remote], missing_ok=True)
            raise OperationalError("stored archive checksum mismatch")

    def fetch_archive(
        self,
        key: str,
        meta: dict,
        progress: Progress | None = None,
    ) -> tuple[Path, bool]:
        remote = self._join(key, ARCHIVE_NAME)
        got_md5, got_size = self._md5_size(remote)
        if got_md5 != meta["md5"].lower() or got_size != meta["size"]:
            raise OperationalError("stored archive checksum mismatch")
        handle = tempfile.NamedTemporaryFile(prefix="lifdrop-", suffix=".zip", delete=False)
        tmp = Path(handle.name)
        handle.close()
        try:
            self._run(["copyto", remote, str(tmp)], progress=progress)
            _check_archive(tmp, meta)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return tmp, True

    def verify_file(self, key: str, entry: dict) -> None:
        md5 = entry.get("md5")
        if not isinstance(md5, str) or len(md5) != 32:
            raise OperationalError(f"manifest checksum is invalid for {entry['rel']}")
        got_md5, got_size = self._md5_size(self._join(key, "files", entry["rel"]))
        if got_md5 != md5.lower() or got_size != entry["size"]:
            raise OperationalError(f"stored checksum mismatch: {entry['rel']}")

    def restore_file(self, key: str, rel: str, dest: Path) -> str:
        dest.parent.mkdir(parents=True, exist_ok=True)
        granted = _grant_dir_write(dest.parent)
        tmp = dest.with_name(dest.name + PARTIAL_SUFFIX)
        try:
            self._run(["copyto", self._join(key, "files", rel), str(tmp)])
            digest = sha256_file(tmp)
            os.replace(tmp, dest)
            _fsync_dir(dest.parent, strict=False)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            if granted is not None:
                os.chmod(dest.parent, granted)
        return digest

    def discard(self, key: str) -> None:
        self._run(["purge", self._join(key)], missing_ok=True)

    def _join(self, *parts: str) -> str:
        cleaned: list[str] = []
        for part in parts:
            piece = part.strip("/")
            if piece:
                cleaned.append(piece)
        suffix = "/".join(cleaned)
        if not suffix:
            return self.spec
        if self.spec.endswith(":"):
            return self.spec + suffix
        return f"{self.spec}/{suffix}"

    def _md5_size(self, remote: str) -> tuple[str, int]:
        listed = self._run(["lsjson", remote])
        try:
            items = json.loads(listed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise OperationalError(f"cannot list {remote}: {exc}") from exc
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise OperationalError(f"stored file missing: {remote}")
        size = items[0].get("Size")
        if isinstance(size, bool) or not isinstance(size, int):
            raise OperationalError(f"stored file missing: {remote}")
        summed = self._run(["hashsum", "MD5", remote])
        line = summed.stdout.strip().splitlines()
        if not line or not line[0].split():
            raise OperationalError(f"stored checksum mismatch: {remote}")
        return line[0].split()[0].lower(), size

    def _run(
        self,
        args: list[str],
        *,
        missing_ok: bool = False,
        progress: Progress | None = None,
    ) -> subprocess.CompletedProcess[str] | None:
        binary = os.environ.get(RCLONE_ENV, "rclone")
        try:
            if progress is not None and not progress.logger.quiet:
                result = self._run_with_progress(binary, args, progress)
            else:
                result = subprocess.run(
                    [binary, *args],
                    check=False,
                    capture_output=True,
                    text=True,
                )
        except FileNotFoundError as exc:
            raise OperationalError(f"rclone is not installed ({binary})") from exc
        if result.returncode == 0:
            return result
        detail = (result.stderr or result.stdout or "").strip()
        if missing_ok and (
            result.returncode == 3 or "not found" in detail.lower() or "does not exist" in detail.lower()
        ):
            return None
        if len(detail) > 500:
            detail = detail[:500] + "..."
        raise OperationalError(f"rclone failed: {detail or 'exit ' + str(result.returncode)}")

    def _run_with_progress(
        self,
        binary: str,
        args: list[str],
        progress: Progress,
    ) -> subprocess.CompletedProcess[str]:
        command = [binary, *args, "--stats=1s", "--stats-one-line"]
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        assert proc.stderr is not None
        collected = bytearray()
        pending = ""
        try:
            while True:
                block = proc.stderr.read(1024)
                if not block:
                    break
                collected.extend(block)
                pending += block.decode("utf-8", "replace").replace("\r", "\n")
                parts = pending.split("\n")
                pending = parts[-1]
                for line in parts[:-1]:
                    text = line.strip()
                    if text:
                        progress.note(text)
            if pending.strip():
                progress.note(pending.strip())
        finally:
            progress.close()
        code = proc.wait()
        stderr = collected.decode("utf-8", "replace")
        return subprocess.CompletedProcess(command, code, "", stderr)


def open_store() -> LocalStore | RcloneStore:
    raw = os.environ.get(STORE_ENV)
    if raw is None or raw.strip() == "":
        return RcloneStore(DEFAULT_REMOTE)
    if _is_rclone_spec(raw):
        return RcloneStore(raw)
    return LocalStore(raw)


def _consider_file(root: Path, path: Path, files: list[Path], log: Logger) -> None:
    rel = _rel(root, path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise OperationalError(f"cannot stat {rel}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        log.warn(f"skipping symlink {rel}")
        return
    if not stat.S_ISREG(info.st_mode):
        log.warn(f"skipping non-regular file {rel}")
        return
    if info.st_nlink > 1:
        log.warn(f"skipping hard link {rel}")
        return
    if not _owns(info) and not (os.access(path, os.R_OK) and os.access(path, os.W_OK)):
        log.warn(f"skipping {rel}: permission denied")
        return
    files.append(path)


def collect_files(root: Path, recursive: bool, log: Logger) -> list[Path]:
    files: list[Path] = []
    if not recursive:
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry.is_symlink():
                log.warn(f"skipping symlink {_rel(root, entry)}")
                continue
            if entry.is_dir():
                continue
            _consider_file(root, entry, files, log)
        return files

    def _walk_error(err: OSError) -> None:
        if err.filename is not None and Path(err.filename) == root:
            raise err
        log.warn(f"skipping {err.filename}: {err.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
        current = Path(dirpath)
        kept: list[str] = []
        for dirname in sorted(dirnames):
            candidate = current / dirname
            if candidate.is_symlink():
                log.warn(f"skipping symlink {_rel(root, candidate)}")
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in sorted(filenames):
            _consider_file(root, current / filename, files, log)
    files.sort(key=lambda path: _rel(root, path))
    return files


def _originals_match(source: Path, manifest: dict) -> bool:
    try:
        entries = manifest_entries(manifest)
    except OperationalError:
        return False
    for entry in entries:
        path = safe_join(source, entry["rel"])
        try:
            info = path.lstat()
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return False
        if info.st_size != entry["size"]:
            return False
        if sha256_file(path) != entry["sha256"]:
            return False
    return True


def lift(folder: str, recursive: bool, log: Logger) -> None:
    source = resolve_folder(folder)
    store = open_store()
    store.guard(source)
    store.prepare()
    key = storage_key(source)
    existing = store.read_manifest(key)
    if existing is not None:
        if Path(existing["source"]) != source:
            raise OperationalError(f"storage at {store.label()} does not belong to {source}")
        if existing["status"] == "lifted" or not _originals_match(source, existing):
            if existing["status"] == "lifted":
                raise UserError(f"{source} is already lifted")
            raise UserError(
                f"lift of {source} did not finish; "
                f"run lifdrop drop --force on that folder to restore it"
            )
        log.info(f"removing incomplete lift of {source}")
        store.discard(key)

    conflict = conflict_message(source, store.iter_manifests())
    if conflict:
        raise UserError(conflict)

    files = collect_files(source, recursive, log)
    total_bytes = sum(path.lstat().st_size for path in files)
    log.info(f"packing {source} ({len(files)} files, {_human_bytes(total_bytes)})")
    archive_path: Path | None = None
    staged = False
    packing = Progress(log, "packing", total_bytes, len(files))
    try:
        try:
            archive_path, entries = build_archive(source, files, packing)
        finally:
            packing.finish()
        checking = Progress(log, "checking", archive_path.stat().st_size)
        try:
            sha, md5, size = checksums(archive_path, checking)
        finally:
            checking.finish()
        log.info(f"uploading archive ({_human_bytes(size)}) -> {store.label()}")
        uploading = Progress(log, "uploading", size)
        try:
            store.put_archive(key, archive_path, sha256=sha, md5=md5, size=size, progress=uploading)
        finally:
            uploading.close()
        manifest = {
            "archive": {"md5": md5, "name": ARCHIVE_NAME, "sha256": sha, "size": size},
            "files": entries,
            "recursive": recursive,
            "source": str(source),
            "status": "staged",
        }
        store.write_manifest(key, manifest)
        staged = True
        emptying = Progress(log, "emptying", len(files))
        emptying.unit = "files"
        try:
            for src, entry in zip(files, entries, strict=True):
                info = src.lstat()
                emptying.start_file(entry["rel"])
                try:
                    _empty_placeholder(src, entry["mode"], info.st_atime_ns, info.st_mtime_ns)
                except PermissionError as exc:
                    raise OperationalError(f"failed to placeholder {entry['rel']}: {exc}") from exc
                emptying.advance(1)
        finally:
            emptying.finish()
        manifest["status"] = "lifted"
        store.write_manifest(key, manifest)
    except Exception as exc:
        if not staged:
            store.discard(key)
            if isinstance(exc, (UserError, OperationalError)):
                raise
            raise OperationalError(f"failed to lift {source}: {exc}") from exc
        detail = exc if isinstance(exc, OperationalError) else f"failed to lift {source}: {exc}"
        raise OperationalError(
            f"{detail}; storage kept; run lifdrop drop --force on {source}"
        ) from exc
    finally:
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)
    log.info(f"lifted {len(entries)} files from {source}")


def _placeholder_conflict(source: Path, entry: dict) -> str | None:
    dest = safe_join(source, entry["rel"])
    try:
        info = dest.lstat()
    except FileNotFoundError:
        return None
    rel = entry["rel"]
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return f"refusing to drop; {rel} is not a regular placeholder (use --force)"
    if info.st_size > 0:
        return f"refusing to drop; {rel} is not empty (use --force)"
    return None


def drop(folder: str, force: bool, log: Logger) -> None:
    source = resolve_folder(folder)
    store = open_store()
    store.prepare()
    key = storage_key(source)
    manifest = store.read_manifest(key)
    if manifest is None:
        raise UserError(f"{source} was never lifted")
    if Path(manifest["source"]) != source:
        raise OperationalError(f"storage at {store.label()} does not belong to {source}")
    if manifest["status"] not in {"staged", "lifted"}:
        raise OperationalError(f"unexpected manifest status for {source}")
    entries = manifest_entries(manifest)
    legacy = "archive" not in manifest
    meta = None if legacy else archive_meta(manifest)
    if not force:
        for entry in entries:
            conflict = _placeholder_conflict(source, entry)
            if conflict:
                raise UserError(conflict)

    log.info(f"dropping {source} ({len(entries)} files)")
    downloaded: Path | None = None
    remove_download = False
    try:
        if legacy:
            for entry in entries:
                store.verify_file(key, entry)
            for entry in entries:
                dest = safe_join(source, entry["rel"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                digest = store.restore_file(key, entry["rel"], dest)
                if digest != entry["sha256"]:
                    raise OperationalError(f"restored checksum mismatch: {entry['rel']}")
                os.chmod(dest, entry["mode"])
                os.utime(dest, ns=(entry["mtime_ns"], entry["mtime_ns"]))
                log.info(f"restored {entry['rel']}")
        else:
            assert meta is not None
            downloading = Progress(log, "downloading", int(meta["size"]))
            try:
                downloaded, remove_download = store.fetch_archive(key, meta, progress=downloading)
            finally:
                downloading.close()
            restoring = Progress(log, "restoring", sum(int(entry["size"]) for entry in entries), len(entries))
            try:
                with zipfile.ZipFile(downloaded) as archive:
                    names = set(archive.namelist())
                    for entry in entries:
                        if entry["rel"] not in names:
                            raise OperationalError(f"archive is missing {entry['rel']}")
                    for entry in entries:
                        dest = safe_join(source, entry["rel"])
                        restoring.start_file(entry["rel"])
                        _restore_member(archive, entry, dest, restoring)
            finally:
                restoring.finish()
    except Exception as exc:
        if isinstance(exc, (UserError, OperationalError)):
            if isinstance(exc, OperationalError):
                raise OperationalError(
                    f"{exc}; storage kept; re-run drop --force"
                ) from exc
            raise
        raise OperationalError(
            f"failed to drop {source}: {exc}; storage kept; re-run drop --force"
        ) from exc
    finally:
        if remove_download and downloaded is not None:
            downloaded.unlink(missing_ok=True)
    try:
        store.discard(key)
    except OSError as exc:
        raise OperationalError(
            f"restored {source} but could not remove storage {store.label()}: {exc}"
        ) from exc
    log.info(f"dropped {len(entries)} files into {source}")


def _add_quiet(parser: argparse.ArgumentParser, dest: str) -> None:
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        dest=dest,
        help="suppress progress messages",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lifdrop",
        description=(
            "Store folder contents on pCloud and replace each file with a "
            "0-byte placeholder, or restore those contents."
        ),
    )
    # Separate destinations: a subparser would otherwise reset a leading -q.
    _add_quiet(parser, "quiet")
    commands = parser.add_subparsers(dest="command", required=True)
    lift_cmd = commands.add_parser(
        "lift",
        help="replace files in a folder with 0-byte placeholders",
    )
    _add_quiet(lift_cmd, "quiet_cmd")
    lift_cmd.add_argument("folder", help="directory whose files should be lifted")
    lift_cmd.add_argument(
        "--no-recursive",
        action="store_true",
        help="lift only files directly inside the folder",
    )
    drop_cmd = commands.add_parser(
        "drop",
        help="restore lifted file contents and remove their storage",
    )
    _add_quiet(drop_cmd, "quiet_cmd")
    drop_cmd.add_argument("folder", help="directory to restore")
    drop_cmd.add_argument(
        "--force",
        action="store_true",
        help="overwrite placeholders that are no longer empty",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log = Logger(quiet=args.quiet or args.quiet_cmd)
    try:
        if args.command == "lift":
            lift(args.folder, recursive=not args.no_recursive, log=log)
        elif args.command == "drop":
            drop(args.folder, force=args.force, log=log)
        else:
            parser.error(f"unknown command {args.command}")
    except UserError as exc:
        print(f"lifdrop: {exc}", file=sys.stderr)
        return 1
    except OperationalError as exc:
        print(f"lifdrop: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
