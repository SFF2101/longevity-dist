"""Fail-closed guard for the shared external HomeData volume and its Longevity project root.
The volume, not the project subdirectory, must be a real NTFS-family mount on a different
device from /share. Every expected project directory must be on that same volume.
No automatic legacy-path fallback, symlink traversal or mount mutation.
"""
import os
from pathlib import Path

PARENT = Path('/share')
VOLUME = PARENT / 'HomeData'
ROOT = VOLUME / 'Longevity'
ALLOWED = ('01_Original_Archives', '02_Historical_Dataset', '03_Current_Data', '04_Reports', '05_Data_Documentation')
FSTYPES = ('ntfs3', 'ntfs', 'fuseblk')
MOUNTINFO = Path('/proc/self/mountinfo')


class MountGuardError(Exception):
    pass


def _unescape(field):
    return field.replace('\\040', ' ').replace('\\011', '\t').replace('\\012', '\n').replace('\\134', '\\')


def mount_fstype(mountinfo_text, mount_point):
    """Return the filesystem type of the LAST mount listed at mount_point (the visible one), else None."""
    found = None
    for line in mountinfo_text.splitlines():
        left, sep, right = line.partition(' - ')
        parts, rparts = left.split(), right.split()
        if not sep or len(parts) < 5 or not rparts:
            continue
        if _unescape(parts[4]) == str(mount_point):
            found = rparts[0]
    return found


def check(root=ROOT, parent=PARENT, mountinfo=MOUNTINFO, dirs=ALLOWED, fstypes=FSTYPES, volume=VOLUME):
    if volume.parent != parent or root.parent != volume:
        raise MountGuardError('Unexpected shared-volume/project layout')
    if volume.is_symlink() or not volume.is_dir():
        raise MountGuardError('HomeData volume not mounted or symlink')
    if root.is_symlink() or not root.is_dir():
        raise MountGuardError('Longevity project directory unavailable or symlink')
    try:
        text = mountinfo.read_text(encoding='utf8', errors='replace')
    except OSError:
        raise MountGuardError('Mount table unavailable; refusing to use shared storage') from None
    fstype = mount_fstype(text, volume)
    if fstype is None:
        raise MountGuardError('HomeData path is not a mount point; refusing internal fallback')
    if fstype not in fstypes:
        raise MountGuardError('HomeData mount has unexpected filesystem type')
    dev = os.stat(volume).st_dev
    if dev == os.stat(parent).st_dev:
        raise MountGuardError('HomeData path is on the same device as /share; refusing internal fallback')
    if os.stat(root).st_dev != dev:
        raise MountGuardError('Longevity project root is not on the HomeData volume')
    for name in dirs:
        p = root / name
        if p.is_symlink() or not p.is_dir():
            raise MountGuardError('Missing or invalid expected directory ' + name)
        if os.stat(p).st_dev != dev:
            raise MountGuardError('Expected directory is not on the Longevity volume: ' + name)
    runtime = root / '.runtime'
    if runtime.is_symlink() or (runtime.exists() and
            (not runtime.is_dir() or os.stat(runtime).st_dev != dev)):
        raise MountGuardError('Invalid Longevity runtime directory')
    return {'fstype': fstype, 'directories': len(dirs)}
