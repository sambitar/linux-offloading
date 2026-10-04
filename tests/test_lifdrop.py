#!/usr/bin/env python3
"""Behavior tests for the lifdrop CLI."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lifdrop


class LifdropTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_store = os.environ.get(lifdrop.STORE_ENV)
        self._prev_cwd = Path.cwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / "store"
        os.environ[lifdrop.STORE_ENV] = str(self.store)
        self.folder = self.root / "proj"
        self.folder.mkdir()

    def tearDown(self) -> None:
        os.chdir(self._prev_cwd)
        if self._prev_store is None:
            os.environ.pop(lifdrop.STORE_ENV, None)
        else:
            os.environ[lifdrop.STORE_ENV] = self._prev_store

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = lifdrop.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def write_file(self, path: Path, content: bytes, mode: int = 0o640) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.chmod(path, mode)

    def test_round_trip_preserves_bytes_mode_mtime_and_inode(self) -> None:
        target = self.folder / "hello.txt"
        payload = b"hel\x00lo\xff"
        self.write_file(target, payload, 0o640)
        before = target.stat()

        code, _, _ = self.run_cli("lift", str(self.folder))
        self.assertEqual(code, 0)
        lifted = target.stat()
        self.assertEqual(target.read_bytes(), b"")
        self.assertEqual(lifted.st_size, 0)
        self.assertEqual(lifted.st_ino, before.st_ino)
        self.assertEqual(stat.S_IMODE(lifted.st_mode), 0o640)
        self.assertEqual(lifted.st_mtime_ns, before.st_mtime_ns)

        slots = list(self.store.iterdir())
        self.assertEqual(len(slots), 1)
        self.assertEqual(zip_member(slots[0] / "tree.zip", "hello.txt"), payload)

        code, _, _ = self.run_cli("drop", str(self.folder))
        self.assertEqual(code, 0)
        restored = target.stat()
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(restored.st_mode), 0o640)
        self.assertEqual(restored.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(list(self.store.iterdir()), [])

    def test_spaces_in_directory_and_file_names(self) -> None:
        folder = self.root / "my folder"
        folder.mkdir()
        target = folder / "my file.txt"
        self.write_file(target, b"spaced")

        self.assertEqual(self.run_cli("lift", str(folder))[0], 0)
        self.assertEqual(target.read_bytes(), b"")
        stored = next(self.store.iterdir()) / "tree.zip"
        self.assertEqual(zip_member(stored, "my file.txt"), b"spaced")
        self.assertEqual(self.run_cli("drop", str(folder))[0], 0)
        self.assertEqual(target.read_bytes(), b"spaced")

    def test_missing_folder_and_non_directory(self) -> None:
        missing = self.root / "nope"
        code, _, err = self.run_cli("lift", str(missing))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        code, _, err = self.run_cli("drop", str(missing))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)

        plain = self.root / "file.txt"
        plain.write_text("x", encoding="utf-8")
        code, _, err = self.run_cli("lift", str(plain))
        self.assertEqual(code, 1)
        self.assertIn("not a directory", err)
        self.assertEqual(plain.read_text(encoding="utf-8"), "x")

    def test_double_lift_and_drop_of_never_lifted_folder(self) -> None:
        target = self.folder / "once.txt"
        self.write_file(target, b"once")
        self.assertEqual(self.run_cli("lift", str(self.folder))[0], 0)

        code, _, err = self.run_cli("lift", str(self.folder))
        self.assertEqual(code, 1)
        self.assertIn("already lifted", err)
        self.assertEqual(target.read_bytes(), b"")
        self.assertEqual(len(list(self.store.iterdir())), 1)

        other = self.root / "other"
        other.mkdir()
        code, _, err = self.run_cli("drop", str(other))
        self.assertEqual(code, 1)
        self.assertIn("was never lifted", err)

        self.assertEqual(self.run_cli("drop", str(self.folder))[0], 0)
        self.assertEqual(target.read_bytes(), b"once")

    def test_drop_refuses_non_empty_placeholder_unless_forced(self) -> None:
        target = self.folder / "note.txt"
        self.write_file(target, b"original")
        self.assertEqual(self.run_cli("lift", str(self.folder))[0], 0)
        target.write_bytes(b"changed")

        code, _, err = self.run_cli("drop", str(self.folder))
        self.assertEqual(code, 1)
        self.assertIn("not empty", err)
        self.assertIn("--force", err)
        self.assertEqual(target.read_bytes(), b"changed")
        self.assertEqual(len(list(self.store.iterdir())), 1)

        self.assertEqual(self.run_cli("drop", "--force", str(self.folder))[0], 0)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(list(self.store.iterdir()), [])

    def test_nested_directories_round_trip(self) -> None:
        nested = self.folder / "a" / "b" / "c.txt"
        top = self.folder / "top.txt"
        self.write_file(nested, b"nested")
        self.write_file(top, b"top")

        self.assertEqual(self.run_cli("lift", str(self.folder))[0], 0)
        self.assertEqual(nested.read_bytes(), b"")
        self.assertEqual(top.read_bytes(), b"")
        stored = next(self.store.iterdir()) / "tree.zip"
        self.assertEqual(zip_member(stored, "a/b/c.txt"), b"nested")
        self.assertEqual(sorted(p.name for p in stored.parent.iterdir()), ["manifest.json", "tree.zip"])

        nested.unlink()
        nested.parent.rmdir()
        self.assertEqual(self.run_cli("drop", str(self.folder))[0], 0)
        self.assertEqual(nested.read_bytes(), b"nested")
        self.assertEqual(top.read_bytes(), b"top")

    def test_no_recursive_leaves_nested_files(self) -> None:
        top = self.folder / "top.txt"
        nested = self.folder / "nested" / "inner.txt"
        self.write_file(top, b"top")
        self.write_file(nested, b"inner")

        code, _, _ = self.run_cli("lift", str(self.folder), "--no-recursive")
        self.assertEqual(code, 0)
        self.assertEqual(top.read_bytes(), b"")
        self.assertEqual(nested.read_bytes(), b"inner")

        self.assertEqual(self.run_cli("drop", str(self.folder))[0], 0)
        self.assertEqual(top.read_bytes(), b"top")
        self.assertEqual(nested.read_bytes(), b"inner")

    def test_relative_path_and_tilde(self) -> None:
        os.chdir(self.root)
        target = self.folder / "rel.txt"
        self.write_file(target, b"rel")
        self.assertEqual(self.run_cli("lift", "proj")[0], 0)
        self.assertEqual(target.read_bytes(), b"")
        self.assertEqual(self.run_cli("drop", "proj")[0], 0)
        self.assertEqual(target.read_bytes(), b"rel")
        self.assertEqual(lifdrop.resolve_folder("~"), Path.home().resolve())

    def test_ancestor_and_descendant_lifts_are_refused(self) -> None:
        child = self.folder / "child"
        child.mkdir()
        parent_file = self.folder / "a.txt"
        child_file = child / "b.txt"
        self.write_file(parent_file, b"aaa")
        self.write_file(child_file, b"bbb")

        self.assertEqual(self.run_cli("lift", str(child))[0], 0)
        code, _, err = self.run_cli("lift", str(self.folder))
        self.assertEqual(code, 1)
        self.assertIn("already lifted", err)
        self.assertEqual(parent_file.read_bytes(), b"aaa")
        self.assertEqual(child_file.read_bytes(), b"")
        self.assertEqual(self.run_cli("drop", str(child))[0], 0)

        self.assertEqual(self.run_cli("lift", str(self.folder))[0], 0)
        code, _, err = self.run_cli("lift", str(child))
        self.assertEqual(code, 1)
        self.assertIn("already lifted", err)
        self.assertEqual(parent_file.read_bytes(), b"")
        self.assertEqual(self.run_cli("drop", str(self.folder))[0], 0)
        self.assertEqual(parent_file.read_bytes(), b"aaa")
        self.assertEqual(child_file.read_bytes(), b"bbb")

    def test_symlink_file_and_directory_are_skipped(self) -> None:
        real = self.folder / "real.txt"
        self.write_file(real, b"real")
        outside = self.root / "outside.txt"
        outside.write_bytes(b"out")
        (self.folder / "link.txt").symlink_to(outside)
        outside_dir = self.root / "outside-dir"
        outside_dir.mkdir()
        secret = outside_dir / "secret.txt"
        secret.write_bytes(b"secret")
        (self.folder / "linkdir").symlink_to(outside_dir)

        code, _, err = self.run_cli("lift", str(self.folder))
        self.assertEqual(code, 0)
        self.assertIn("skipping symlink link.txt", err)
        self.assertIn("skipping symlink linkdir", err)
        self.assertEqual(real.read_bytes(), b"")
        self.assertEqual(outside.read_bytes(), b"out")
        self.assertEqual(secret.read_bytes(), b"secret")
        self.assertTrue((self.folder / "link.txt").is_symlink())
        self.assertTrue((self.folder / "linkdir").is_symlink())

    def test_symlink_folder_uses_resolved_path(self) -> None:
        target = self.folder / "a.txt"
        self.write_file(target, b"aaa")
        link = self.root / "linked"
        link.symlink_to(self.folder)
        self.assertEqual(self.run_cli("lift", str(link))[0], 0)
        self.assertEqual(target.read_bytes(), b"")
        code, _, err = self.run_cli("lift", str(self.folder))
        self.assertEqual(code, 1)
        self.assertIn("already lifted", err)
        self.assertEqual(self.run_cli("drop", str(link))[0], 0)
        self.assertEqual(target.read_bytes(), b"aaa")

    def test_hard_link_is_skipped_and_regular_file_is_lifted(self) -> None:
        linked = self.folder / "linked.txt"
        other = self.folder / "other.txt"
        plain = self.folder / "plain.txt"
        self.write_file(linked, b"same")
        os.link(linked, other)
        self.write_file(plain, b"plain")

        code, _, err = self.run_cli("lift", str(self.folder))
        self.assertEqual(code, 0)
        self.assertIn("skipping hard link", err)
        self.assertEqual(linked.read_bytes(), b"same")
        self.assertEqual(other.read_bytes(), b"same")
        self.assertEqual(plain.read_bytes(), b"")
        self.assertEqual(self.run_cli("drop", str(self.folder))[0], 0)
        self.assertEqual(plain.read_bytes(), b"plain")
        self.assertEqual(linked.read_bytes(), b"same")

    def test_fifo_is_skipped(self) -> None:
        os.mkfifo(self.folder / "pipe")
        target = self.folder / "ok.txt"
        self.write_file(target, b"ok")
        code, _, err = self.run_cli("lift", str(self.folder))
        self.assertEqual(code, 0)
        self.assertIn("skipping non-regular file pipe", err)
        self.assertTrue(stat.S_ISFIFO((self.folder / "pipe").stat().st_mode))
        self.assertEqual(target.read_bytes(), b"")

    def test_owner_can_lift_file_without_permission_bits(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores mode 000")
        target = self.folder / "secret.txt"
        self.write_file(target, b"secret")
        os.chmod(target, 0)
        try:
            code, _, err = self.run_cli("lift", "-q", str(self.folder))
            self.assertEqual(code, 0, err)
            self.assertEqual(target.stat().st_size, 0)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0)
            code, _, err = self.run_cli("drop", "-q", str(self.folder))
            self.assertEqual(code, 0, err)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0)
        finally:
            os.chmod(target, 0o644)
        self.assertEqual(target.read_bytes(), b"secret")

    def test_read_only_file_keeps_its_mode(self) -> None:
        target = self.folder / "ro.txt"
        self.write_file(target, b"readonly", 0o444)
        code, _, err = self.run_cli("lift", str(self.folder))
        self.assertEqual(code, 0, err)
        self.assertEqual(target.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o444)
        code, _, err = self.run_cli("drop", str(self.folder))
        self.assertEqual(code, 0, err)
        self.assertEqual(target.read_bytes(), b"readonly")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o444)

    def test_read_only_directory_is_restored_on_drop(self) -> None:
        locked = self.folder / "locked"
        locked.mkdir()
        target = locked / "a.txt"
        self.write_file(target, b"inside")
        os.chmod(locked, 0o555)
        try:
            code, _, err = self.run_cli("lift", str(self.folder))
            self.assertEqual(code, 0, err)
            self.assertEqual(target.stat().st_size, 0)
            code, _, err = self.run_cli("drop", str(self.folder))
            self.assertEqual(code, 0, err)
            self.assertEqual(target.read_bytes(), b"inside")
            self.assertEqual(stat.S_IMODE(locked.stat().st_mode), 0o555)
        finally:
            os.chmod(locked, 0o755)

    def test_corrupt_storage_aborts_drop(self) -> None:
        target = self.folder / "hello.txt"
        self.write_file(target, b"hello")
        self.assertEqual(self.run_cli("lift", str(self.folder))[0], 0)
        stored = next(self.store.iterdir()) / "tree.zip"
        blob = bytearray(stored.read_bytes())
        blob[-2] ^= 0xFF
        stored.write_bytes(blob)
        code, _, err = self.run_cli("drop", str(self.folder))
        self.assertEqual(code, 2)
        self.assertIn("checksum mismatch", err)
        self.assertEqual(target.read_bytes(), b"")
        self.assertEqual(len(list(self.store.iterdir())), 1)

    def test_refuses_to_lift_storage_directory(self) -> None:
        store = self.folder / "vault"
        os.environ[lifdrop.STORE_ENV] = str(store)
        target = self.folder / "keep.txt"
        self.write_file(target, b"keep")
        code, _, err = self.run_cli("lift", str(self.folder))
        self.assertEqual(code, 1)
        self.assertIn("storage directory", err)
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertFalse(store.exists())

    def test_quiet_suppresses_progress(self) -> None:
        self.write_file(self.folder / "a.txt", b"a")
        for args in (
            ("-q", "lift", str(self.folder)),
            ("lift", "-q", str(self.folder)),
        ):
            with self.subTest(args=args):
                target = self.folder / "a.txt"
                if target.stat().st_size == 0:
                    self.assertEqual(self.run_cli("drop", str(self.folder))[0], 0)
                code, out, err = self.run_cli(*args)
                self.assertEqual(code, 0)
                self.assertEqual(out, "")
                self.assertEqual(err, "")
                self.assertEqual(target.read_bytes(), b"")

    def test_packing_shows_progress(self) -> None:
        self.write_file(self.folder / "a.txt", b"abc")
        self.write_file(self.folder / "nested" / "b.txt", b"defg")
        code, out, err = self.run_cli("lift", str(self.folder))
        self.assertEqual(code, 0)
        self.assertIn("packing", out)
        self.assertIn("packing", err)
        self.assertIn("%", err)
        self.assertIn("[", err)
        code, _, drop_err = self.run_cli("drop", str(self.folder))
        self.assertEqual(code, 0)
        self.assertIn("restoring", drop_err)

    def test_drop_restores_legacy_per_file_vault(self) -> None:
        target = self.folder / "ra.txt"
        payload = b"hello legacy"
        target.write_bytes(b"")
        key = lifdrop.storage_key(self.folder.resolve())
        stored = self.store / key / "files"
        stored.mkdir(parents=True)
        (stored / "ra.txt").write_bytes(payload)
        manifest = {
            "files": [
                {
                    "md5": hashlib.md5(payload).hexdigest(),
                    "mode": 0o644,
                    "mtime_ns": target.stat().st_mtime_ns,
                    "rel": "ra.txt",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }
            ],
            "recursive": True,
            "source": str(self.folder.resolve()),
            "status": "staged",
        }
        (self.store / key / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        code, _out, err = self.run_cli("drop", str(self.folder))
        self.assertEqual(code, 0, err)
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse((self.store / key).exists())

    def test_manifest_rejects_escaping_path(self) -> None:
        with self.assertRaises(lifdrop.OperationalError):
            lifdrop.manifest_entries(
                {
                    "files": [
                        {
                            "rel": "../outside.txt",
                            "mode": 0o644,
                            "size": 1,
                            "mtime_ns": 1,
                            "sha256": "a" * 64,
                        }
                    ]
                }
            )


FAKE_RCLONE = """#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

root = Path(os.environ["FAKE_RCLONE_ROOT"])


def is_remote(spec):
    if spec.startswith("/"):
        return False
    remote, sep, _rest = spec.partition(":")
    return bool(sep) and bool(remote) and "/" not in remote


def resolve(spec):
    _remote, sep, rel = spec.partition(":")
    if not sep:
        raise SystemExit("not a remote")
    rel = rel.lstrip("/")
    return root.joinpath(*rel.split("/")) if rel else root


def die(message, code=3):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def positionals(items):
    return [item for item in items if not item.startswith("-")]


cmd = sys.argv[1]
rest = sys.argv[2:]
if os.environ.get("FAKE_RCLONE_FAIL") == cmd:
    die("boom", 1)
args = positionals(rest)

if cmd == "mkdir":
    resolve(args[0]).mkdir(parents=True, exist_ok=True)
elif cmd == "copyto":
    src, dest = args
    if is_remote(src):
        path = resolve(src)
        if not path.is_file():
            die("directory not found")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    else:
        target = resolve(dest)
        if not Path(src).is_file():
            die("directory not found")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
elif cmd == "hashsum":
    algo, spec = args
    path = resolve(spec)
    if not path.is_file():
        die("directory not found")
    if algo.upper() != "MD5":
        die("unsupported hash", 1)
    print(f"{hashlib.md5(path.read_bytes()).hexdigest()}  {path.name}")
elif cmd == "lsjson":
    path = resolve(args[0])
    if not path.exists():
        die("directory not found")
    if path.is_file():
        entries = [{"Name": path.name, "Size": path.stat().st_size, "IsDir": False}]
    else:
        entries = [
            {"Name": child.name, "Size": child.stat().st_size, "IsDir": child.is_dir()}
            for child in sorted(path.iterdir())
        ]
    print(json.dumps(entries))
elif cmd == "cat":
    path = resolve(args[0])
    if not path.is_file():
        die("directory not found")
    sys.stdout.buffer.write(path.read_bytes())
elif cmd == "lsf":
    path = resolve(args[0])
    if not path.is_dir():
        die("directory not found")
    for child in sorted(path.iterdir()):
        if "--dirs-only" in rest and not child.is_dir():
            continue
        print(child.name + ("/" if child.is_dir() else ""))
elif cmd == "purge":
    path = resolve(args[0])
    if not path.exists():
        die("directory not found")
    shutil.rmtree(path)
elif cmd == "deletefile":
    path = resolve(args[0])
    if not path.is_file():
        die("directory not found")
    path.unlink()
else:
    die(f"unknown command {cmd}", 1)
"""


class RcloneStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = {
            lifdrop.STORE_ENV: os.environ.get(lifdrop.STORE_ENV),
            lifdrop.RCLONE_ENV: os.environ.get(lifdrop.RCLONE_ENV),
            "FAKE_RCLONE_ROOT": os.environ.get("FAKE_RCLONE_ROOT"),
            "FAKE_RCLONE_FAIL": os.environ.get("FAKE_RCLONE_FAIL"),
        }
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        fake = self.root / "rclone"
        fake.write_text(FAKE_RCLONE, encoding="utf-8")
        fake.chmod(0o755)
        os.environ[lifdrop.RCLONE_ENV] = str(fake)
        os.environ["FAKE_RCLONE_ROOT"] = str(self.remote)
        os.environ.pop("FAKE_RCLONE_FAIL", None)
        os.environ[lifdrop.STORE_ENV] = "fake:.lifted_files"
        self.folder = self.root / "proj"
        self.folder.mkdir()

    def tearDown(self) -> None:
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_vault_is_pcloud(self) -> None:
        os.environ.pop(lifdrop.STORE_ENV, None)
        store = lifdrop.open_store()
        self.assertIsInstance(store, lifdrop.RcloneStore)
        self.assertEqual(store.spec, "pcloud:.lifted_files")

    def test_round_trip_stores_bytes_only_on_the_remote(self) -> None:
        target = self.folder / "my file.txt"
        nested = self.folder / "sub" / "inner.txt"
        target.write_bytes(b"hello cloud")
        nested.parent.mkdir()
        nested.write_bytes(b"nested")
        code, _out, err = io_run("lift", str(self.folder))
        self.assertEqual(code, 0, err)
        self.assertEqual(target.read_bytes(), b"")
        self.assertEqual(nested.read_bytes(), b"")
        slot = next((self.remote / ".lifted_files").iterdir())
        self.assertEqual(sorted(path.name for path in slot.iterdir()), ["manifest.json", "tree.zip"])
        self.assertEqual(zip_member(slot / "tree.zip", "my file.txt"), b"hello cloud")
        self.assertEqual(zip_member(slot / "tree.zip", "sub/inner.txt"), b"nested")
        manifest = json.loads((slot / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "lifted")
        code, _out, err = io_run("drop", str(self.folder))
        self.assertEqual(code, 0, err)
        self.assertEqual(target.read_bytes(), b"hello cloud")
        self.assertEqual(nested.read_bytes(), b"nested")
        self.assertEqual(list((self.remote / ".lifted_files").iterdir()), [])

    def test_failed_upload_keeps_local_bytes(self) -> None:
        target = self.folder / "keep.txt"
        target.write_bytes(b"keep")
        os.environ["FAKE_RCLONE_FAIL"] = "copyto"
        code, _out, err = io_run("lift", str(self.folder))
        self.assertEqual(code, 2, err)
        self.assertEqual(target.read_bytes(), b"keep")
        vault = self.remote / ".lifted_files"
        self.assertTrue(not vault.exists() or list(vault.iterdir()) == [])


def zip_member(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


def io_run(*args: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = lifdrop.main(list(args))
    return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
