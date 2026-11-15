import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "longevity_health_collector"))
import mount_guard as mg

DIRS = mg.ALLOWED


def mountinfo(point, fstype):
    return ("22 1 179:2 / / rw - ext4 /dev/root rw\n"
            f"40 22 8:1 / {point} rw,relatime - {fstype} /dev/sda1 rw\n")


class MountGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); base = pathlib.Path(self.tmp.name)
        self.parent = base / "share"; self.root = self.parent / "Longevity"
        self.root.mkdir(parents=True)
        for d in DIRS: (self.root / d).mkdir()
        self.mi = base / "mountinfo"

    def tearDown(self): self.tmp.cleanup()

    def run_check(self, text, devs=None):
        self.mi.write_text(text)
        real = os.stat
        def fake_stat(p, *a, **k):
            st = real(p, *a, **k)
            if devs and str(p) in devs:
                return os.stat_result((st.st_mode, st.st_ino, devs[str(p)]) + tuple(st)[3:])
            return st
        with mock.patch("os.stat", side_effect=fake_stat):
            return mg.check(self.root, self.parent, self.mi)

    def external(self):
        d = {str(self.root): 99}
        d.update({str(self.root / x): 99 for x in DIRS})
        return d

    def test_external_ntfs_volume_passes(self):
        self.assertEqual(self.run_check(mountinfo(self.root, "ntfs3"), self.external())["fstype"], "ntfs3")

    def test_absent_path_fails(self):
        for d in DIRS: (self.root / d).rmdir()
        self.root.rmdir()
        with self.assertRaises(mg.MountGuardError): self.run_check(mountinfo(self.root, "ntfs3"))

    def test_unmounted_internal_fallback_directory_fails(self):
        with self.assertRaisesRegex(mg.MountGuardError, "not a mount point"):
            self.run_check("22 1 179:2 / / rw - ext4 /dev/root rw\n", self.external())

    def test_wrong_filesystem_type_fails(self):
        with self.assertRaisesRegex(mg.MountGuardError, "unexpected filesystem"):
            self.run_check(mountinfo(self.root, "ext4"), self.external())

    def test_same_device_as_share_fails(self):
        with self.assertRaisesRegex(mg.MountGuardError, "same device"):
            self.run_check(mountinfo(self.root, "ntfs3"))

    def test_missing_expected_directory_fails(self):
        (self.root / DIRS[2]).rmdir()
        with self.assertRaisesRegex(mg.MountGuardError, "Missing or invalid"):
            self.run_check(mountinfo(self.root, "ntfs3"), self.external())

    def test_directory_on_other_device_fails(self):
        d = self.external(); d[str(self.root / DIRS[0])] = 5
        with self.assertRaisesRegex(mg.MountGuardError, "not on the Longevity volume"):
            self.run_check(mountinfo(self.root, "ntfs3"), d)

    def test_symlinked_root_fails(self):
        other = pathlib.Path(self.tmp.name) / "elsewhere"; other.mkdir()
        alt = self.parent / "Link"; alt.symlink_to(other)
        with self.assertRaises(mg.MountGuardError): mg.check(alt, self.parent, self.mi)

    def test_mount_table_unreadable_fails(self):
        with self.assertRaisesRegex(mg.MountGuardError, "Mount table"):
            mg.check(self.root, self.parent, pathlib.Path(self.tmp.name) / "missing")

    def test_space_escaped_mount_point(self):
        self.assertEqual(mg.mount_fstype("40 22 8:1 / /share/My\\040Disk rw - ntfs3 /dev/sda1 rw\n", "/share/My Disk"), "ntfs3")

    def test_both_app_copies_identical(self):
        apps = pathlib.Path(__file__).resolve().parents[1]
        self.assertEqual((apps / "longevity_health_collector/mount_guard.py").read_bytes(),
                         (apps / "longevity_drive_bridge/mount_guard.py").read_bytes())


if __name__ == "__main__": unittest.main()
