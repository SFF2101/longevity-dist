"""Synthetic-only tests for Drive Bridge v0.3.0 restore mode (fake Drive; no network; no credentials)."""
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "longevity_drive_bridge"))
import replicate as rp

ROOT_ID = "ROOT"
FOLDERS = {f: "F" + str(i) for i, f in enumerate(rp.ALLOWED)}


def h(b): return hashlib.sha256(b).hexdigest(), hashlib.md5(b).hexdigest()


class FakeDrive:
    def __init__(self, objects):
        self.objects = objects          # id -> dict(name, parent, data, trashed?)
        self.calls = []
    def metadata(self, fid):
        self.calls.append(("metadata", fid))
        if fid == ROOT_ID: return {"id": ROOT_ID, "name": "Longevity", "mimeType": "application/vnd.google-apps.folder", "parents": []}
        for name, f in FOLDERS.items():
            if f == fid: return {"id": f, "name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [ROOT_ID]}
        raise rp.Stop("not found")
    def list_children(self, parent):
        self.calls.append(("list", parent))
        out = []
        for oid, o in self.objects.items():
            if o["parent"] == parent:
                sha, md5 = h(o["data"])
                out.append({"id": oid, "name": o["name"], "size": str(o.get("size", len(o["data"]))),
                            "md5Checksum": o.get("md5", md5), "mimeType": "application/octet-stream", "trashed": o.get("trashed", False)})
        return out
    def download_to(self, fid, stream, limit):
        self.calls.append(("download", fid))
        data = self.objects[fid].get("serve", self.objects[fid]["data"])
        if len(data) > limit: raise rp.Stop("Remote object larger than its recorded size; refusing")
        stream.write(data); s, m = h(data)
        return s, m, len(data)
    def __getattr__(self, name):    # any mutating call (folder/upload/POST/PUT/DELETE) is a test failure
        raise AssertionError("restore must not call Drive." + name)


class Restore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); base = pathlib.Path(self.tmp.name)
        self.src = base / "Longevity"; self.src.mkdir()
        for f in rp.ALLOWED: (self.src / f).mkdir()
        self.data = base / "data"; self.data.mkdir()
        self.token = base / "token.json"; self.token.write_text("{}")
        self.objects, self.files, self.pins = {}, {}, []
        self.patches = [mock.patch.object(rp, "SOURCE", self.src), mock.patch.object(rp, "STATE", self.data / "archive_state.json"),
                        mock.patch.object(rp, "TOKEN", self.token)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def add(self, folder, name, data, record=True, oid=None, **extra):
        sha, md5 = h(data); oid = oid or ("id%d" % len(self.objects))
        self.objects[oid] = dict(name=name + ".sha256-" + sha, parent=FOLDERS[folder], data=data, **extra)
        if record: self.files[folder + "/" + name] = {"id": oid, "sha256": sha, "size": len(data), "md5": md5}
        return oid, sha

    def run_restore(self, dry=False, approved=True, state=True):
        if state:
            (self.data / "archive_state.json").write_text(json.dumps({"schema": 1, "root_id": ROOT_ID, "folder_ids": FOLDERS, "files": self.files}))
        self.drive = FakeDrive(self.objects)
        out = io.StringIO()
        with mock.patch.object(rp, "Drive", lambda: self.drive), mock.patch("sys.stdout", out):
            rc = rp.restore({"restore_approved": approved, "root_folder_id": ROOT_ID, "pinned_hashes": self.pins}, dry)
        return rc, out.getvalue()

    def test_restores_recorded_files_with_verified_hashes(self):
        self.add("02_Historical_Dataset", "historical_daily.csv", b"synthetic,a\n1,2\n")
        self.add("05_Data_Documentation", "README.md", b"# synthetic\n")
        rc, out = self.run_restore()
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.src / "02_Historical_Dataset/historical_daily.csv").read_bytes(), b"synthetic,a\n1,2\n")
        self.assertIn("RESTORE PASS", out)
        self.assertFalse(list(self.src.rglob(".restore_*")))
        self.assertFalse(any(c[0] not in ("metadata", "list", "download") for c in self.drive.calls))

    def test_dry_run_downloads_nothing_and_writes_nothing(self):
        self.add("04_Reports", "r.json", b"{}")
        rc, out = self.run_restore(dry=True)
        self.assertEqual(rc, 0); self.assertIn("would_restore=1", out)
        self.assertFalse((self.src / "04_Reports/r.json").exists())
        self.assertFalse(any(c[0] == "download" for c in self.drive.calls))

    def test_requires_explicit_approval(self):
        self.add("04_Reports", "r.json", b"{}")
        with self.assertRaisesRegex(rp.Stop, "restore_approved"): self.run_restore(approved=False)

    def test_never_overwrites_existing_differing_file(self):
        self.add("03_Current_Data", "snapshot_2026-11-10.json", b'{"v":1}')
        (self.src / "03_Current_Data/snapshot_2026-11-10.json").write_bytes(b'{"v":"local"}')
        rc, out = self.run_restore()
        self.assertEqual(rc, 2); self.assertIn("conflict_existing_differs=1", out)
        self.assertEqual((self.src / "03_Current_Data/snapshot_2026-11-10.json").read_bytes(), b'{"v":"local"}')

    def test_identical_existing_file_is_verified_not_downloaded(self):
        self.add("04_Reports", "same.json", b"{}")
        (self.src / "04_Reports/same.json").write_bytes(b"{}")
        rc, out = self.run_restore()
        self.assertEqual(rc, 0); self.assertIn("present_verified=1", out)
        self.assertFalse(any(c[0] == "download" for c in self.drive.calls))

    def test_multiple_unrecorded_versions_are_ambiguous(self):
        self.add("03_Current_Data", "daily_weight_min_2026-11-10.json", b'{"a":1}', record=False)
        self.add("03_Current_Data", "daily_weight_min_2026-11-10.json", b'{"a":2}', record=False)
        rc, out = self.run_restore()
        self.assertEqual(rc, 2); self.assertIn("ambiguous_multiple_versions=1", out)
        self.assertFalse((self.src / "03_Current_Data/daily_weight_min_2026-11-10.json").exists())

    def test_recorded_revision_wins_over_other_remote_versions(self):
        self.add("03_Current_Data", "daily_weight_min_2026-11-10.json", b'{"old":1}', record=False)
        self.add("03_Current_Data", "daily_weight_min_2026-11-10.json", b'{"current":1}')
        rc, out = self.run_restore()
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.src / "03_Current_Data/daily_weight_min_2026-11-10.json").read_bytes(), b'{"current":1}')

    def test_corrupt_download_is_rejected_and_nothing_placed(self):
        oid, _ = self.add("02_Historical_Dataset", "historical_annual.csv", b"good\n")
        self.objects[oid]["serve"] = b"evil\n"
        with self.assertRaisesRegex(rp.Stop, "verification"): self.run_restore()
        self.assertFalse((self.src / "02_Historical_Dataset/historical_annual.csv").exists())
        self.assertFalse(list(self.src.rglob(".restore_*")))

    def test_oversized_download_is_rejected(self):
        oid, _ = self.add("02_Historical_Dataset", "x.csv", b"abc")
        self.objects[oid]["serve"] = b"abcdef"
        with self.assertRaisesRegex(rp.Stop, "larger"): self.run_restore()
        self.assertFalse((self.src / "02_Historical_Dataset/x.csv").exists())

    def test_pinned_original_archive_hash_enforced(self):
        self.pins = ["01_Original_Archives/export-original.zip=" + "a" * 64]
        self.add("01_Original_Archives", "export-original.zip", b"not the pinned archive")
        rc, out = self.run_restore()
        self.assertEqual(rc, 2); self.assertIn("refused_pinned_hash_mismatch=1", out)
        self.assertFalse((self.src / "01_Original_Archives/export-original.zip").exists())

    def test_pinned_hash_match_restores(self):
        data = b"synthetic original archive"
        self.pins = ["01_Original_Archives/export-original.zip=" + hashlib.sha256(data).hexdigest()]
        self.add("01_Original_Archives", "export-original.zip", data)
        rc, out = self.run_restore()
        self.assertEqual(rc, 0, out)

    def test_invalid_pin_option_fails_closed(self):
        for bad in (["01_Original_Archives/x.zip=short"], ["99_Other/x.zip=" + "a" * 64],
                    ["01_Original_Archives/x.zip=" + "a" * 64, "01_Original_Archives/x.zip=" + "b" * 64]):
            with self.assertRaisesRegex(rp.Stop, "pinned_hashes"): rp.parse_pins(bad)

    def test_recorded_copy_missing_or_metadata_mismatch(self):
        self.files["04_Reports/gone.json"] = {"id": "nope", "sha256": "0" * 64, "size": 2, "md5": "x"}
        oid, _ = self.add("04_Reports", "m.json", b"{}", md5="bad")
        rc, out = self.run_restore()
        self.assertEqual(rc, 2)
        self.assertIn("recorded_copy_missing=1", out); self.assertIn("recorded_copy_metadata_mismatch=1", out)

    def test_unsafe_names_and_uncontent_addressed_objects_skipped(self):
        self.add("05_Data_Documentation", "token.json", b"{}")
        self.objects["raw"] = dict(name="plain.txt", parent=FOLDERS["05_Data_Documentation"], data=b"x")
        rc, out = self.run_restore()
        self.assertEqual(rc, 2); self.assertIn("refused_unsafe_name=1", out); self.assertIn("remote_not_content_addressed=1", out)
        self.assertFalse((self.src / "05_Data_Documentation/token.json").exists())

    def test_missing_state_fails_closed(self):
        with self.assertRaisesRegex(rp.Stop, "No verified archive state"): self.run_restore(state=False)

    def test_root_mismatch_fails_closed(self):
        (self.data / "archive_state.json").write_text(json.dumps({"root_id": "OTHER", "folder_ids": FOLDERS, "files": {}}))
        with mock.patch.object(rp, "Drive", lambda: FakeDrive({})):
            with self.assertRaisesRegex(rp.Stop, "root"):
                rp.restore({"restore_approved": True, "root_folder_id": ROOT_ID}, False)

    def test_repeat_run_is_idempotent(self):
        self.add("02_Historical_Dataset", "historical_monthly.csv", b"m\n")
        self.assertEqual(self.run_restore()[0], 0)
        rc, out = self.run_restore()
        self.assertEqual(rc, 0); self.assertIn("present_verified=1", out)
        self.assertFalse(any(c[0] == "download" for c in self.drive.calls))

    def test_output_contains_counts_not_file_contents(self):
        self.add("02_Historical_Dataset", "historical_daily.csv", b"SECRETVALUE,98.7\n")
        rc, out = self.run_restore()
        self.assertNotIn("SECRETVALUE", out); self.assertNotIn("98.7", out)


class Modes(unittest.TestCase):
    def test_unknown_mode_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            d = pathlib.Path(t); (d / "options.json").write_text(json.dumps({"mode": "sync", "dry_run": True}))
            with mock.patch.object(rp, "DATA", d):
                with self.assertRaisesRegex(rp.Stop, "Unknown operation mode"): rp.main()

    def test_guard_failure_stops_before_any_drive_access(self):
        with tempfile.TemporaryDirectory() as t:
            d = pathlib.Path(t); (d / "options.json").write_text(json.dumps({"mode": "restore", "dry_run": False, "restore_approved": True}))
            with mock.patch.object(rp, "DATA", d), mock.patch.object(rp, "Drive", side_effect=AssertionError("no drive")):
                with self.assertRaisesRegex(rp.Stop, "not mounted|not a mount point|Mount table"): rp.main()


if __name__ == "__main__": unittest.main()


class Status(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); base = pathlib.Path(t.name)
        self.src = base / "Longevity"; self.src.mkdir()
        for f in rp.ALLOWED: (self.src / f).mkdir()
        self.data = base / "data"; self.data.mkdir()
        for p in (mock.patch.object(rp, "SOURCE", self.src), mock.patch.object(rp, "DATA", self.data),
                  mock.patch.object(rp, "STATUS", self.src / ".runtime" / "bridge_status.json"),
                  mock.patch.object(rp.mount_guard, "check", return_value={"fstype": "ntfs3", "directories": 5})):
            p.start(); self.addCleanup(p.stop)

    def run_main(self, **opts):
        (self.data / "options.json").write_text(json.dumps(opts))
        with mock.patch("sys.stdout", io.StringIO()):
            return rp.main()

    def status(self): return json.loads((self.src / ".runtime" / "bridge_status.json").read_text())

    def test_dry_run_records_attempt_without_success(self):
        self.run_main(mode="archive", dry_run=True)
        st = self.status()
        self.assertEqual(st["last_result"], "dry_run_pass"); self.assertNotIn("last_success", st); self.assertEqual(st["version"], rp.VERSION)

    def test_archive_failure_recorded_and_previous_success_kept(self):
        (self.src / ".runtime").mkdir(); (self.src / ".runtime" / "bridge_status.json").write_text(json.dumps({"last_success": "2026-11-14T05:00:00+00:00"}))
        with self.assertRaises(rp.Stop):
            self.run_main(mode="archive", dry_run=False, test_only=False, production_approved=False)
        st = self.status()
        self.assertEqual(st["last_result"], "archive_stop"); self.assertEqual(st["last_success"], "2026-11-14T05:00:00+00:00")

    def test_archive_pass_records_success(self):
        with mock.patch.object(rp, "archive", return_value=0):
            self.run_main(mode="archive", dry_run=False)
        st = self.status(); self.assertEqual(st["last_result"], "archive_pass"); self.assertIn("last_success", st)

    def test_status_is_outside_replicated_folders_and_holds_no_file_data(self):
        with mock.patch.object(rp, "archive", return_value=0): self.run_main(mode="archive", dry_run=False)
        self.assertNotIn(".runtime", rp.ALLOWED)
        self.assertEqual(set(self.status()), {"last_attempt", "last_result", "last_success", "mode", "version"})
        self.assertFalse(any(p.name.startswith(".status_") for p in (self.src / ".runtime").iterdir()))
