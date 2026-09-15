#!/usr/bin/env python3
"""Symlink-safe remover for the native Coding worker credential files.

Root runs this once. It opens every component of the private directory with
O_NOFOLLOW so a swapped symlink cannot redirect the removal, verifies the
directory's owner and mode on the descriptor, and unlinks only the three fixed
filenames with unlinkat, refusing any that is a symlink, directory, hard link
or another account's file. It never recurses and never removes the directory.
It prints a JSON receipt of which fixed names were removed or already absent;
never a value. Removal is not revocation.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys

ALLOWED_NAMES = ("hippius-environment.json", "image-storage.json", "provider-key")


class Refusal(Exception):
    """A fixed, value-free diagnostic."""


def _fail(message: str) -> None:
    raise Refusal(message)


def _open_dir_chain(path: str, uid: int, gid: int) -> int:
    if not os.path.isabs(path):
        _fail("private directory path is not absolute")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for component in [c for c in path.split("/") if c]:
            if component in (".", ".."):
                _fail("private directory path is not canonical")
            nxt = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = nxt
    except OSError:
        os.close(fd)
        _fail("a private directory component is a symlink or not a directory")
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        _fail("private directory is not a directory")
    if info.st_uid != uid or info.st_gid != gid:
        os.close(fd)
        _fail("private directory is not owned by the worker")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        _fail("private directory is not mode 0700")
    return fd


def _run(
    args: argparse.Namespace, removed: list[str], already_absent: list[str]
) -> dict:
    import pwd

    try:
        account = pwd.getpwnam(args.owner)
    except KeyError:
        _fail("worker account does not exist")
    uid, gid = account.pw_uid, account.pw_gid

    dir_fd = _open_dir_chain(args.private_dir, uid, gid)
    try:
        for name in ALLOWED_NAMES:
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                already_absent.append(name)
                continue
            if stat.S_ISLNK(info.st_mode):
                _fail(f"{name} is a symlink")
            if not stat.S_ISREG(info.st_mode):
                _fail(f"{name} is not a regular file")
            if info.st_nlink != 1:
                _fail(f"{name} has more than one hard link")
            if info.st_uid != uid:
                _fail(f"{name} is owned by another account")
            os.unlink(name, dir_fd=dir_fd)
            removed.append(name)
        os.fsync(dir_fd)
        # Confirm absence on the same descriptor.
        for name in ALLOWED_NAMES:
            try:
                os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            _fail(f"{name} is still present after removal")
    finally:
        os.close(dir_fd)
    return {
        "ok": True,
        "source_revision": args.source_revision,
        "removed": removed,
        "already_absent": already_absent,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--source-revision", default="")
    args = parser.parse_args()
    removed: list[str] = []
    already_absent: list[str] = []
    try:
        result = _run(args, removed, already_absent)
    except Refusal as refusal:
        print(
            json.dumps(
                {"ok": False, "error": str(refusal), "removed": removed}, sort_keys=True
            )
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {"ok": False, "error": "unexpected helper error", "removed": removed},
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
