"""Fail-closed guard: /share/Longevity must be the external Longevity volume, never the internal fallback directory.
Checks (all required): real directory, not a symlink; listed as its own mount point in /proc/self/mountinfo with an
NTFS-family filesystem type; different device from /share; the five expected directories exist on that same device.
Read-only: inspects metadata only, writes nothing, prints no paths beyond the fixed Longevity root."""
import os
from pathlib import Path

ROOT = Path('/share/Longevity')
PARENT = Path('/share')
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


def check(root=ROOT, parent=PARENT, mountinfo=MOUNTINFO, dirs=ALLOWED, fstypes=FSTYPES):
    if root.is_symlink() or not root.is_dir():
        raise MountGuardError('Longevity storage not mounted or not accessible')
    try:
        text = mountinfo.read_text(encoding='utf8', errors='replace')
    except OSError:
        raise MountGuardError('Mount table unavailable; refusing to use Longevity storage') from None
    fstype = mount_fstype(text, root)
    if fstype is None:
        raise MountGuardError('Longevity path is not a mount point; refusing internal fallback directory')
    if fstype not in fstypes:
        raise MountGuardError('Longevity mount has unexpected filesystem type')
    dev = os.stat(root).st_dev
    if dev == os.stat(parent).st_dev:
        raise MountGuardError('Longevity path is on the same device as /share; refusing internal fallback directory')
    for name in dirs:
        p = root / name
        if p.is_symlink() or not p.is_dir():
            raise MountGuardError('Missing or invalid expected directory ' + name)
        if os.stat(p).st_dev != dev:
            raise MountGuardError('Expected directory is not on the Longevity volume: ' + name)
    return {'fstype': fstype, 'directories': len(dirs)}
