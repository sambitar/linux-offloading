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
from pathlib import Path

STORE_ENV = "LIFDROP_STORE"
RCLONE_ENV = "LIFDROP_RCLONE"
DEFAULT_REMOTE = "pcloud:.lifted_files"
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


def _is_rclone_spec(raw: str) -> bool:
    if raw.startswith("~") or raw.startswith("/"):
        return False
    remote, sep, _path = raw.partition(":")
    return bool(sep) and bool(remote) and "/" not in remote and "\\" not in remote


def checksums(path: Path) -> tuple[str, str, int]:
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), md5.hexdigest(), size


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

    def put(
        self,
        key: str,
        rel: str,
        src: Path,
        *,
        sha256: str,
        md5: str,
        size: int,
        mode: int,
    ) -> None:
        del md5, size
        dest = safe_join(self.root / key / "files", rel)
        digest = copy_verified(src, dest, strict_fsync=True)
        if digest != sha256:
            raise OperationalError(f"stored checksum mismatch: {rel}")
        os.chmod(dest, mode)

    def verify(self, key: str, entry: dict) -> None:
        stored = safe_join(self.root / key / "files", entry["rel"])
        if not stored.is_file() or stored.is_symlink():
            raise OperationalError(f"stored file missing: {entry['rel']}")
        if stored.stat().st_size != entry["size"] or sha256_file(stored) != entry["sha256"]:
            raise OperationalError(f"stored checksum mismatch: {entry['rel']}")

    def restore(self, key: str, rel: str, dest: Path) -> str:
        stored = safe_join(self.root / key / "files", rel)
        return copy_verified(stored, dest, strict_fsync=False)

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

    def put(
        self,
        key: str,
        rel: str,
        src: Path,
        *,
        sha256: str,
        md5: str,
        size: int,
        mode: int,
    ) -> None:
        del mode
        remote = self._join(key, "files", rel)
        self._run(["copyto", str(src), remote])
        got_md5, got_size = self._md5_size(remote)
        if got_md5 != md5 or got_size != size:
            self._run(["deletefile", remote], missing_ok=True)
            raise OperationalError(f"stored checksum mismatch: {rel}")
        del sha256

    def verify(self, key: str, entry: dict) -> None:
        md5 = entry.get("md5")
        if not isinstance(md5, str) or len(md5) != 32:
            raise OperationalError(f"manifest checksum is invalid for {entry['rel']}")
        got_md5, got_size = self._md5_size(self._join(key, "files", entry["rel"]))
        if got_md5 != md5.lower() or got_size != entry["size"]:
            raise OperationalError(f"stored checksum mismatch: {entry['rel']}")

    def restore(self, key: str, rel: str, dest: Path) -> str:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + PARTIAL_SUFFIX)
        try:
            self._run(["copyto", self._join(key, "files", rel), str(tmp)])
            digest = sha256_file(tmp)
            os.replace(tmp, dest)
            _fsync_dir(dest.parent, strict=False)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
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

    def _run(self, args: list[str], *, missing_ok: bool = False) -> subprocess.CompletedProcess[str] | None:
        binary = os.environ.get(RCLONE_ENV, "rclone")
        try:
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

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
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
    log.info(f"lifting {source} ({len(files)} files) -> {store.label()}")
    entries: list[dict] = []
    staged = False
    try:
        for src in files:
            rel = _rel(source, src)
            info = src.lstat()
            digest, md5, hashed_size = checksums(src)
            if hashed_size != info.st_size:
                raise OperationalError(f"{rel} changed while reading")
            store.put(key, rel, src, sha256=digest, md5=md5, size=info.st_size, mode=stat.S_IMODE(info.st_mode))
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
            log.info(f"stored {rel} ({info.st_size} bytes)")
        manifest = {
            "files": entries,
            "recursive": recursive,
            "source": str(source),
            "status": "staged",
        }
        store.write_manifest(key, manifest)
        staged = True
        for src, entry in zip(files, entries, strict=True):
            info = src.lstat()
            os.truncate(src, 0)
            if src.lstat().st_size != 0:
                raise OperationalError(f"failed to placeholder {entry['rel']}")
            os.utime(src, ns=(info.st_atime_ns, info.st_mtime_ns))
            log.info(f"placeholder {entry['rel']}")
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
    for entry in entries:
        store.verify(key, entry)
    if not force:
        for entry in entries:
            conflict = _placeholder_conflict(source, entry)
            if conflict:
                raise UserError(conflict)

    log.info(f"dropping {source} ({len(entries)} files)")
    try:
        for entry in entries:
            dest = safe_join(source, entry["rel"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            digest = store.restore(key, entry["rel"], dest)
            if digest != entry["sha256"]:
                raise OperationalError(f"restored checksum mismatch: {entry['rel']}")
            os.chmod(dest, entry["mode"])
            os.utime(dest, ns=(entry["mtime_ns"], entry["mtime_ns"]))
            if sha256_file(dest) != entry["sha256"] or dest.stat().st_size != entry["size"]:
                raise OperationalError(f"restored checksum mismatch: {entry['rel']}")
            log.info(f"restored {entry['rel']}")
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
