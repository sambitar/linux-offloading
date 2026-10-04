# Linux Offloading

A small SSD fills up long before you are done with the files on it. Linux Offloading moves the bytes to **your** pCloud account and leaves a 0-byte placeholder behind. Same name. Same permissions. The folder still looks occupied. The gigabytes are no longer on the drive.

```bash
lift ~/Videos/archive
drop ~/Videos/archive
```

`lift` uploads the folder, checks the cloud copy, then truncates each local file to empty. `drop` writes the bytes back and deletes the cloud copy.

## What this is for

A 128 GB or 256 GB laptop disappears into the OS, a browser, a toolchain, and one video project. The rest of the disk is files you are not opening today: last year's camera dump, ISO images, raw footage, datasets, old project trees, installers you might need once.

Those folders do not have to keep their contents on the SSD.

- Clear tens or hundreds of gigabytes without deleting the folder or losing the filenames.
- Leave a project "installed" in place so paths in notes and scripts still resolve.
- Bring one folder back when you actually need to edit it, then lift it again when you are finished.
- Keep the OS, builds, and the files you are using this week on fast local storage.
- Park a folder that lives on a virtual drive or external disk without a cross-device move failing.

The placeholders stay on the machine. `lift` packs the folder into one uncompressed zip, uploads that single file, deletes the local zip, and only then empties the originals. A failed upload leaves those originals in place. The zip needs temporary disk space until the upload finishes, and again while `drop` unpacks it.

## Your pCloud account

The vault is the rclone remote `pcloud:.lifted_files` in the account you log into. Nothing is shared, and this project does not ship a token.

Install [rclone](https://rclone.org/install/), then connect your account once:

```bash
rclone config
```

Create a remote named `pcloud`, choose storage type `pcloud`, and sign in through the browser. US accounts use `api.pcloud.com`. EU accounts use `eapi.pcloud.com`.

After that, `lift` and `drop` use that login. To put the vault somewhere else in your drive:

```bash
export LIFDROP_STORE="pcloud:Backups/lifted"
```

A directory path keeps the vault on local disk instead of pCloud. `LIFDROP_RCLONE` overrides the rclone binary.

## Install

```bash
git clone https://github.com/sambitar/linux-offloading.git
cd linux-offloading
./install.sh
```

The script installs two commands, `lift` and `drop`, into `~/.local/bin`, and adds that directory to `PATH` in `~/.zshrc` and `~/.bashrc` if it is not already there. Open a new shell afterward.

## Commands

```bash
lift ~/photos
lift ~/photos --no-recursive
lift -q ~/photos
drop ~/photos
drop ~/photos --force
```

`lift` walks the folder recursively. `--no-recursive` lifts only the files directly inside it. Symlinks, sockets, fifos, devices, and hard links are skipped with a warning. A hard link is skipped because emptying one name would empty the others.

Paths are expanded (`~`) and resolved, including symlinks, before anything is stored. The folder must already exist.

`drop` will not overwrite a placeholder that is no longer empty unless you pass `--force`. If a placeholder was deleted, `drop` recreates the file.

A folder that is already lifted, or that sits inside or above one that is, is refused.

## Where the bytes go

```
pcloud:.lifted_files/<sha256 of the absolute folder>/
  manifest.json
  tree.zip
```

`tree.zip` is one uncompressed archive of the folder. Packing the tree into a single file keeps the pCloud transfer to one upload and one download. Open pCloud and look at the top of the drive for `.lifted_files`. The name starts with a dot, so show hidden files if the list hides it. Each lifted folder is a long hex directory. `drop` removes that directory after the files are home.

`manifest.json` records the source path, whether the lift is staged or finished, the archive checksum, and for each file its relative path, mode, size, modification time, sha256, and md5. pCloud's MD5 of the zip is checked before any original is truncated. `drop` checks that same archive once, then writes the files back without hashing each one again.

## Exit status

- `0` — lift or drop finished
- `1` — the folder is missing, is not a directory, is already lifted, or was never lifted
- `2` — copy, checksum, or placeholder update failed
