import datetime as dt
import pathlib
import sys
import unittest
from zoneinfo import ZoneInfo
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "longevity_health_collector"))
from weight_min import select_daily_min, reconcile_min
import tempfile
import json

ZONE = ZoneInfo("Europe/Berlin")
ENT = "sensor.synthetic_weight"
DAY = dt.date(2026, 11, 10)

def observation(value, ts, unit="kg"):
    return {"state": str(value), "last_changed": ts, "attributes": {"unit_of_measurement": unit}}

class TestSelection(unittest.TestCase):
    def test_minimum_same_local_day(self):
        records=[observation(101.5,"2026-11-10T07:10:00+01:00"),observation(100.9,"2026-11-10T08:10:00+01:00"),observation(102.1,"2026-11-10T20:10:00+01:00")]
        result=select_daily_min(records,DAY, ZONE, ENT)
        self.assertEqual(result["value"],100.9)
        self.assertEqual(result["number_of_qualifying_state_changes"],3)
    def test_invalid_wrong_units_and_prior_day_excluded(self):
        rows=[observation("unavailable","2026-11-10T12:00:00+01:00"),observation(90,"2026-11-10T13:00:00+01:00","lb"),observation(98,"2026-11-09T22:00:00+01:00"),observation(101.2,"2026-11-10T07:00:00+01:00")]
        self.assertEqual(select_daily_min(rows,DAY, ZONE, ENT)["value"],101.2)
    def test_missing_day_not_zero(self):
        self.assertIsNone(select_daily_min([],DAY, ZONE, ENT))
    def test_local_day_timezone_boundary(self):
        row=observation(101.1,"2026-11-09T23:30:00+00:00")
        self.assertEqual(select_daily_min([row],DAY, ZONE, ENT)["value"],101.1)

class TestReconciliation(unittest.TestCase):
    def test_late_lower_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp)/"daily_weight_min_2026-11-10.json"
            a=select_daily_min([observation(102,"2026-11-10T07:00:00+01:00")],DAY, ZONE, ENT)
            b=select_daily_min([observation(101,"2026-11-10T08:00:00+01:00")],DAY, ZONE, ENT)
            self.assertEqual(reconcile_min(a,p),"created")
            self.assertEqual(reconcile_min(b,p),"revised")
            self.assertEqual(reconcile_min(a,p),"unchanged")
            self.assertEqual(reconcile_min(None,p),"missing")
            self.assertEqual(json.loads(p.read_text())["value"],101)
            audits=list(p.parent.glob("*_revision_*.json"))
            self.assertEqual(len(audits),1)
            self.assertEqual(json.loads(audits[0].read_text())["value"],102)

if __name__=="__main__": unittest.main()


import io
import os
import urllib.parse
from weight_min import query_day


class TestPolicyExtended(unittest.TestCase):
    def tmp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); return pathlib.Path(t.name)

    def test_higher_late_value_rejected_and_audit_untouched(self):
        p = self.tmp() / "daily_weight_min_2026-11-10.json"
        a = select_daily_min([observation(101, "2026-11-10T07:00:00+01:00")], DAY, ZONE, ENT)
        b = select_daily_min([observation(101, "2026-11-10T07:00:00+01:00"), observation(103, "2026-11-10T21:00:00+01:00")], DAY, ZONE, ENT)
        self.assertEqual(reconcile_min(a, p), "created")
        self.assertEqual(reconcile_min(b, p), "unchanged")
        self.assertEqual(list(p.parent.glob("*_revision_*")), [])

    def test_equal_value_unchanged(self):
        p = self.tmp() / "daily_weight_min_2026-11-10.json"
        a = select_daily_min([observation(100, "2026-11-10T07:00:00+01:00")], DAY, ZONE, ENT)
        reconcile_min(a, p)
        self.assertEqual(reconcile_min(a, p), "unchanged")

    def test_two_successive_revisions_keep_both_audits(self):
        p = self.tmp() / "daily_weight_min_2026-11-10.json"
        for v, t in ((103, "07:00"), (102, "08:00"), (101, "09:00")):
            reconcile_min(select_daily_min([observation(v, "2026-11-10T" + t + ":00+01:00")], DAY, ZONE, ENT), p)
        self.assertEqual(json.loads(p.read_text())["value"], 101)
        self.assertEqual(sorted(json.loads(a.read_text())["value"] for a in p.parent.glob("*_revision_*")), [102, 103])

    def test_restart_repeat_is_idempotent(self):
        p = self.tmp() / "daily_weight_min_2026-11-10.json"
        a = select_daily_min([observation(102, "2026-11-10T07:00:00+01:00")], DAY, ZONE, ENT)
        b = select_daily_min([observation(101, "2026-11-10T08:00:00+01:00")], DAY, ZONE, ENT)
        reconcile_min(a, p); reconcile_min(b, p)
        snapshot = sorted(x.name for x in p.parent.iterdir())
        for _ in range(3): self.assertEqual(reconcile_min(b, p), "unchanged")
        self.assertEqual(sorted(x.name for x in p.parent.iterdir()), snapshot)

    def test_identity_mismatch_and_corrupt_existing_fail_closed(self):
        d = self.tmp(); p = d / "daily_weight_min_2026-11-10.json"
        a = select_daily_min([observation(102, "2026-11-10T07:00:00+01:00")], DAY, ZONE, ENT)
        p.write_text(json.dumps(dict(a, entity_id="sensor.other")))
        with self.assertRaises(ValueError): reconcile_min(a, p)
        p.write_text("{not json")
        with self.assertRaises(ValueError): reconcile_min(a, p)

    def test_date_path_mismatch_and_symlink_rejected(self):
        d = self.tmp()
        a = select_daily_min([observation(102, "2026-11-10T07:00:00+01:00")], DAY, ZONE, ENT)
        with self.assertRaises(ValueError): reconcile_min(a, d / "daily_weight_min_2026-11-11.json")
        real = d / "real"; real.mkdir(); link = d / "link"; link.symlink_to(real)
        with self.assertRaises(ValueError): reconcile_min(a, link / "daily_weight_min_2026-11-10.json")

    def test_units_range_nonfinite_and_naive_timestamps_excluded(self):
        rows = [observation(98, "2026-11-10T07:00:00+01:00", "lb"), observation("nan", "2026-11-10T07:00:00+01:00"),
                observation(30, "2026-11-10T07:00:00+01:00"), observation(300, "2026-11-10T07:00:00+01:00"),
                observation(99, "2026-11-10T07:00:00"), {"state": "99", "attributes": {"unit_of_measurement": "kg"}}]
        self.assertIsNone(select_daily_min(rows, DAY, ZONE, ENT))

    def test_dst_fall_back_day_boundaries(self):
        day = dt.date(2026, 10, 25)   # 25-hour local day in Europe/Berlin
        rows = [observation(100, "2026-10-24T21:59:00+00:00"),   # 23:59 local on 24 Oct -> excluded
                observation(99, "2026-10-24T22:00:00+00:00"),    # 00:00 local 25 Oct -> included
                observation(98, "2026-10-25T22:59:00+00:00"),    # 23:59 local 25 Oct (UTC+1) -> included
                observation(97, "2026-10-25T23:00:00+00:00")]    # 00:00 local 26 Oct -> excluded
        r = select_daily_min(rows, day, ZONE, ENT)
        self.assertEqual((r["value"], r["number_of_qualifying_state_changes"]), (98, 2))

    def test_query_window_and_no_initial_carry_forward(self):
        seen = {}
        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def opener(req, timeout):
            seen["url"] = req.full_url; seen["method"] = req.get_method()
            return Resp(json.dumps([[observation(99, "2026-10-25T08:00:00+01:00")]]).encode())
        r = query_day(dt.date(2026, 10, 25), ZONE, "synthetic-token", ENT, opener=opener)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen["url"]).query, keep_blank_values=True)
        self.assertEqual(seen["method"], "GET")
        self.assertIn("2026-10-25T00:00:00+02:00", urllib.parse.unquote(seen["url"]))
        self.assertEqual(q["end_time"], ["2026-10-26T00:00:00+01:00"])
        self.assertIn("skip_initial_state", q); self.assertEqual(q["significant_changes_only"], ["0"])
        self.assertEqual(r["value"], 99)
        def empty(req, timeout): return Resp(b"[]")
        self.assertIsNone(query_day(DAY, ZONE, "t", ENT, opener=empty))
        def bad(req, timeout): return Resp(b"{}")
        with self.assertRaises(ValueError): query_day(DAY, ZONE, "t", ENT, opener=bad)

    def test_provenance_labels_proxy_and_missing_true_timestamp(self):
        r = select_daily_min([observation(99, "2026-11-10T07:00:00+01:00")], DAY, ZONE, ENT)
        self.assertEqual(r["date_basis"], "home_assistant_recorder_last_changed_proxy")
        self.assertEqual(r["true_measurement_timestamp"], "unavailable_from_source")
        self.assertEqual(r["selected_observed_at"], "2026-11-10T07:00:00+01:00")

    def test_atomic_write_leaves_no_temp_files(self):
        p = self.tmp() / "daily_weight_min_2026-11-10.json"
        reconcile_min(select_daily_min([observation(99, "2026-11-10T07:00:00+01:00")], DAY, ZONE, ENT), p)
        self.assertEqual([x.name for x in p.parent.iterdir()], [p.name])
        self.assertEqual(os.stat(p).st_mode & 0o077, 0)   # private file mode from mkstemp
