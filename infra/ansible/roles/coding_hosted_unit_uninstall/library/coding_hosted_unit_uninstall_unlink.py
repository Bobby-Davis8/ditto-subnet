#!/usr/bin/python
"""Remove the native Coding worker unit and custody template through pinned fds.

Root runs this module. It opens every component of the systemd unit directory
from ``/`` with ``O_NOFOLLOW`` and ``O_DIRECTORY``, so no component is ever
resolved through a symlink, and requires the ``etc/systemd/system`` components
to be directories owned by the unit owner (root on the host) that group and
others cannot write. Before removing anything it refuses any entry the install
roles never create -- a drop-in directory, an instance file or an enablement
link for either unit -- so an out-of-band change is reconciled by hand rather
than half-removed. It then removes each of the two fixed names with ``unlinkat``
relative to the pinned directory, refusing a symlink, directory, special file,
hard link or foreign-owned file, and reports which paths it removed, which were
already absent, which it refused and which it did not attempt. It never removes
a directory, never reads file contents and never touches a unit's runtime state.
"""

import errno
import os
import pwd
import stat

DOCUMENTATION = r"""
module: coding_hosted_unit_uninstall_unlink
short_description: Remove the native worker unit and custody template safely
description:
  - Opens every path component with O_NOFOLLOW, refuses foreign drop-ins,
    instances and enablement links, and removes the two fixed unit files with
    unlinkat relative to the pinned unit directory.
options:
  unit_dir:
    description: Absolute, normalised unit directory ending in /etc/systemd/system.
    type: str
    required: true
  owner:
    description: The account that must own the unit directories and files (root).
    type: str
    required: true
"""

# Exactly the files coding_hosted_connectivity and coding_hosted_custody_service
# template into /etc/systemd/system. Neither role installs a drop-in, an
# instance or an [Install] section, so nothing else is ever removed.
UNIT_NAMES = ("ditto-coding-hosted-worker.service", "ditto-coding-custody@.service")
# Any other entry starting with one of these belongs to neither install role.
FOREIGN_PREFIXES = ("ditto-coding-hosted-worker.", "ditto-coding-custody@")
UNIT_DIR_SUFFIX = ("etc", "systemd", "system")
DEPENDENCY_SUFFIXES = (".wants", ".requires", ".upholds")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Unsafe(Exception):
    """A value-free diagnostic: names and states only."""


def split_unit_dir(unit_dir):
    if not isinstance(unit_dir, str) or not unit_dir.startswith("/"):
        raise Unsafe("the unit directory path is not absolute")
    if os.path.normpath(unit_dir) != unit_dir or "//" in unit_dir:
        raise Unsafe("the unit directory path is not normalised")
    components = unit_dir.split("/")[1:]
    if (
        len(components) < len(UNIT_DIR_SUFFIX)
        or tuple(components[-len(UNIT_DIR_SUFFIX) :]) != UNIT_DIR_SUFFIX
    ):
        raise Unsafe("the unit directory path does not end in /etc/systemd/system")
    return components


def _require_directory(fd, owner_uid, label, *, owned):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise Unsafe(f"{label} is not a directory")
    if owned:
        if info.st_uid != owner_uid:
            raise Unsafe(f"{label} is not owned by the unit owner")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise Unsafe(f"{label} is writable by group or others")
    else:
        if info.st_uid not in (0, owner_uid):
            raise Unsafe(f"{label} is owned by another account")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not (
            info.st_mode & stat.S_ISVTX
        ):
            raise Unsafe(f"{label} is writable by group or others without sticky bit")


def open_unit_dir(unit_dir, owner_uid):
    """Pin the unit directory, or return None when a component is absent."""
    components = split_unit_dir(unit_dir)
    owned_from = len(components) - len(UNIT_DIR_SUFFIX)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    pinned = None
    try:
        _require_directory(fd, owner_uid, "the root directory", owned=False)
        for index, component in enumerate(components):
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                return None
            except OSError as error:
                if error.errno in (errno.ENOTDIR, errno.ELOOP):
                    raise Unsafe(
                        "a unit directory component is a symlink or not a directory"
                    ) from None
                raise
            os.close(fd)
            fd = child
            _require_directory(
                fd,
                owner_uid,
                f"unit directory component {component}",
                owned=index >= owned_from,
            )
        pinned = fd
        return pinned
    finally:
        if pinned is None:
            os.close(fd)


def _is_foreign(name):
    return name not in UNIT_NAMES and name.startswith(FOREIGN_PREFIXES)


def foreign_entries(dir_fd):
    """Names of drop-ins, instances or enablement links no install role creates."""
    found = []
    for entry in sorted(os.listdir(dir_fd)):
        if _is_foreign(entry):
            found.append(entry)
            continue
        if not entry.endswith(DEPENDENCY_SUFFIXES):
            continue
        info = os.stat(entry, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            # A linked dependency directory could hide an enablement link.
            raise Unsafe(f"dependency directory {entry} is a symlink")
        if not stat.S_ISDIR(info.st_mode):
            continue
        try:
            child = os.open(entry, _DIR_FLAGS, dir_fd=dir_fd)
        except OSError as error:
            if error.errno in (errno.ENOTDIR, errno.ELOOP, errno.ENOENT):
                raise Unsafe(
                    f"dependency directory {entry} changed during inspection"
                ) from None
            raise
        try:
            found.extend(
                f"{entry}/{name}"
                for name in sorted(os.listdir(child))
                if name in UNIT_NAMES or _is_foreign(name)
            )
        finally:
            os.close(child)
    return found


def unlink_name(dir_fd, name, owner_uid, check_mode=False):
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "absent"
    if not stat.S_ISREG(info.st_mode):
        raise Unsafe("is a symlink, directory or special file")
    if info.st_nlink != 1:
        raise Unsafe("has more than one hard link")
    if info.st_uid != owner_uid:
        raise Unsafe("is not owned by the unit owner")
    if check_mode:
        return "would_remove"
    try:
        # unlinkat(2) without AT_REMOVEDIR: never a directory, never a link target.
        os.unlink(name, dir_fd=dir_fd)
    except IsADirectoryError:
        raise Unsafe("became a directory before removal") from None
    except FileNotFoundError:
        raise Unsafe("disappeared before removal") from None
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "removed"
    raise Unsafe("was replaced by a new entry during removal")


def remove_units(unit_dir, owner_uid, check_mode=False):
    paths = [f"{unit_dir}/{name}" for name in UNIT_NAMES]
    result = {
        "changed": False,
        "removed": [],
        "already_absent": [],
        "refused": [],
        "not_attempted": [],
        "foreign": [],
        "unit_dir_present": True,
    }
    dir_fd = open_unit_dir(unit_dir, owner_uid)
    if dir_fd is None:
        result["already_absent"] = paths
        result["unit_dir_present"] = False
        return result
    try:
        foreign = foreign_entries(dir_fd)
        if foreign:
            result["foreign"] = foreign
            result["refused"] = [
                "an entry no install role creates exists; nothing was removed"
            ]
            result["not_attempted"] = paths
            return result
        # In check mode nothing is unlinked: the paths go to would_remove.
        key = "would_remove" if check_mode else "removed"
        result[key] = []
        for index, (name, path) in enumerate(zip(UNIT_NAMES, paths, strict=True)):
            try:
                state = unlink_name(dir_fd, name, owner_uid, check_mode)
            except Unsafe as error:
                # Keep what was removed, name the refusal and stop.
                result["refused"].append(f"{path}: {error}")
                result["not_attempted"] = paths[index + 1 :]
                break
            (result["already_absent"] if state == "absent" else result[key]).append(
                path
            )
        if result["removed"]:
            os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    result["changed"] = bool(result[key])
    return result


def main():
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(
        argument_spec={
            "unit_dir": {"type": "str", "required": True},
            "owner": {"type": "str", "required": True},
        },
        supports_check_mode=True,
    )
    empty = {
        "removed": [],
        "already_absent": [],
        "foreign": [],
        "not_attempted": [f"{module.params['unit_dir']}/{n}" for n in UNIT_NAMES],
    }
    try:
        owner_uid = pwd.getpwnam(module.params["owner"]).pw_uid
    except KeyError:
        module.fail_json(
            msg="Refused: the unit owner does not exist; nothing was removed.",
            refused=["the unit owner does not exist"],
            **empty,
        )
    try:
        result = remove_units(
            module.params["unit_dir"], owner_uid, check_mode=module.check_mode
        )
    except Unsafe as error:
        module.fail_json(
            msg=f"Refused: {error}; nothing was removed.", refused=[str(error)], **empty
        )
    except OSError as error:
        module.fail_json(
            msg=f"Failed: {os.strerror(error.errno or 0)}; reconcile by hand.",
            refused=["unexpected error"],
            **empty,
        )
    if result["refused"]:
        module.fail_json(msg="Refused: " + "; ".join(result["refused"]), **result)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
