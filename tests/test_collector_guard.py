import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "longevity_health_collector"))
import collect


class CollectorGuard(unittest.TestCase):
    def test_unmounted_storage_stops_before_reading_any_state(self):
        with tempfile.TemporaryDirectory() as t:
            opts = pathlib.Path(t) / "options.json"
            opts.write_text(json.dumps({"dry_run": False, "collection_approved": True, "run_once": True, "timezone": "UTC"}))
            with mock.patch.object(collect, "OPTIONS", opts), \
                 mock.patch.object(collect, "read_state", side_effect=AssertionError("must not read")), \
                 mock.patch.object(collect, "query_day", side_effect=AssertionError("must not query")), \
                 mock.patch.object(collect.mount_guard, "check", side_effect=collect.mount_guard.MountGuardError("Longevity path is not a mount point")):
                with self.assertRaisesRegex(collect.CollectError, "not a mount point.*no measurements captured"):
                    collect.main()

    def test_version_identifiers_consistent(self):
        app = pathlib.Path(collect.__file__).parent
        cfg = (app / "config.yaml").read_text(); dock = (app / "Dockerfile").read_text()
        self.assertIn('version: "0.3.1"', cfg); self.assertIn('io.hass.version="0.3.1"', dock)
        self.assertIn("VERSION = '0.3.1'", (app / "collect.py").read_text())
        b = app.parent / "longevity_drive_bridge"
        self.assertIn('version: "0.4.1"', (b / "config.yaml").read_text())
        self.assertIn('io.hass.version="0.4.1"', (b / "Dockerfile").read_text())
        self.assertIn("VERSION = '0.4.1'", (b / "replicate.py").read_text())


if __name__ == "__main__": unittest.main()
