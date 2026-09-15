#!/usr/bin/env python3
"""Symlink-safe writer for the native Coding worker credential files.

Root runs this once, with the three file contents on stdin as JSON, never in
argv, and the whole task is no_log. It opens every path component with
O_NOFOLLOW so a component the worker account could swap for a symlink between
Ansible's stat and this write cannot redirect the write, verifies the private
directory's owner and mode on the open file descriptor, writes each file to a
temporary name in that directory with O_CREAT|O_EXCL|O_NOFOLLOW, fchowns and
fchmods it, renames every temporary into place only after all of them are
written, and re-verifies each result on its own descriptor. It prints a JSON
receipt of non-secret metadata only: never a value, never a digest.

Exit status is non-zero on any refusal or partial write; the message names the
fixed filenames and their state, never a value.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import stat
import sys

ALLOWED_NAMES = frozenset(
    {"hippius-environment.json", "image-storage.json", "provider-key"}
)


class Refusal(Exception):
    """A fixed, value-free diagnostic."""


def _fail(message: str) -> None:
    raise Refusal(message)


def _open_dir_chain(path: str, uid: int, gid: int) -> int:
    """Open an absolute directory, refusing a symlink at any component.

    Every component is opened with O_NOFOLLOW|O_DIRECTORY relative to the
    previous one, so a symlinked ancestor fails closed. The final directory
    must be owned by the worker, mode 0700, and not writable by group or other.
    """
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


def _existing_is_replaceable(dir_fd: int, name: str, uid: int) -> None:
    """The destination must be absent or a regular single-link worker file."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        _fail(f"{name} exists and is not a regular file")
    if info.st_nlink != 1:
        _fail(f"{name} exists with more than one hard link")
    if info.st_uid != uid:
        _fail(f"{name} exists and is owned by another account")
    if stat.S_IMODE(info.st_mode) != 0o600:
        _fail(f"{name} exists and is not mode 0600; reconcile it by hand")


def _write_temp(dir_fd: int, name: str, content: bytes, uid: int, gid: int) -> str:
    tmp = f".{name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o600)
        written = os.write(fd, content)
        if written != len(content):
            _fail(f"{name} temporary was not fully written")
        os.fsync(fd)
    finally:
        os.close(fd)
    return tmp


def _verify(dir_fd: int, name: str, content: bytes, uid: int, gid: int) -> dict:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _fail(f"{name} is not a regular file after rename")
        if info.st_nlink != 1:
            _fail(f"{name} has more than one hard link after rename")
        if info.st_uid != uid or info.st_gid != gid:
            _fail(f"{name} is not owned by the worker after rename")
        if stat.S_IMODE(info.st_mode) != 0o600:
            _fail(f"{name} is not mode 0600 after rename")
        if info.st_size != len(content):
            _fail(f"{name} size differs after rename")
        body = b""
        while chunk := os.read(fd, 65536):
            body += chunk
    finally:
        os.close(fd)
    # Compared in process; the digest is never printed.
    if hashlib.sha256(body).digest() != hashlib.sha256(content).digest():
        _fail(f"{name} content differs after rename")
    return {
        "name": name,
        "mode": "0600",
        "nlink": info.st_nlink,
        "size": info.st_size,
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


def _run(args: argparse.Namespace) -> dict:
    import pwd

    try:
        account = pwd.getpwnam(args.owner)
    except KeyError:
        _fail("worker account does not exist")
    uid, gid = account.pw_uid, account.pw_gid

    payload = json.loads(sys.stdin.buffer.read())
    files = payload.get("files")
    if not isinstance(files, dict) or set(files) != ALLOWED_NAMES:
        _fail("payload must carry exactly the three fixed filenames")
    contents: dict[str, bytes] = {}
    for name, value in files.items():
        if not isinstance(value, str) or not value:
            _fail(f"{name} content is empty or not a string")
        contents[name] = value.encode("utf-8")

    dir_fd = _open_dir_chain(args.private_dir, uid, gid)
    temps: dict[str, str] = {}
    try:
        for name in sorted(ALLOWED_NAMES):
            _existing_is_replaceable(dir_fd, name, uid)
        for name in sorted(ALLOWED_NAMES):
            temps[name] = _write_temp(dir_fd, name, contents[name], uid, gid)
        renamed: list[str] = []
        try:
            for name in sorted(ALLOWED_NAMES):
                os.rename(temps[name], name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                renamed.append(name)
                del temps[name]
            os.fsync(dir_fd)
        except OSError:
            # A rename failed partway: some files are new, some old/unknown.
            not_renamed = [n for n in sorted(ALLOWED_NAMES) if n not in renamed]
            _fail(
                "partial write: replaced="
                + json.dumps(renamed)
                + " unchanged_or_unknown="
                + json.dumps(not_renamed)
                + "; reconcile by hand"
            )
        receipt = [
            _verify(dir_fd, name, contents[name], uid, gid)
            for name in sorted(ALLOWED_NAMES)
        ]
    finally:
        for tmp in temps.values():
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)
        os.close(dir_fd)
    return {"ok": True, "source_revision": args.source_revision, "files": receipt}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--source-revision", default="")
    args = parser.parse_args()
    try:
        result = _run(args)
    except Refusal as refusal:
        # A curated, value-free message.
        print(json.dumps({"ok": False, "error": str(refusal)}, sort_keys=True))
        return 1
    except Exception:
        # Never surface an exception string: it could name a path but must not
        # risk a value. The operator reconciles the host by hand.
        print(
            json.dumps(
                {"ok": False, "error": "unexpected helper error"}, sort_keys=True
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
